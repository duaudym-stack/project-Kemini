"""Per-feature stream verifiers.

Four independent sub-validators, each focusing on one breakage category
that rhwp-studio tolerates but Hancom editor does not:

  multicol      다단 (column-def) secd/cold control chain
  masterpage    바탕쪽 MasterPage stream structure
  controls      미구현·불명 컨트롤 검출
  structure     record tail / count / level / parent 관계
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .ole_reader    import HwpOleDocument
from .record_parser import CTRL_IDS, HwpRecord, parse_stream


@dataclass
class VerifyIssue:
    feature:     str          # "multicol" | "masterpage" | "controls" | "structure"
    severity:    str          # "ERROR" | "WARN" | "INFO"
    stream:      str
    offset:      int
    tag_name:    str
    description: str
    hint:        str = ""

    def __str__(self) -> str:
        loc = f"{self.stream}@{self.offset:#010x}" if self.offset >= 0 else self.stream
        return f"[{self.severity:5s}/{self.feature}] {loc}  {self.description}"


@dataclass
class VerifyReport:
    path:   str
    issues: List[VerifyIssue] = field(default_factory=list)

    def add(self, issue: VerifyIssue) -> None:
        self.issues.append(issue)

    def errors(self) -> List[VerifyIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    def for_feature(self, feat: str) -> List[VerifyIssue]:
        return [i for i in self.issues if i.feature == feat]

    def to_dict(self) -> dict:
        return {
            "path":   self.path,
            "issues": [
                {k: v for k, v in vars(i).items()}
                for i in self.issues
            ],
        }


# ── helpers ────────────────────────────────────────────────────────────────

def _ctrl_chain(flat: List[HwpRecord], ctrl_id: bytes) -> List[HwpRecord]:
    """Return CTRL_HEADER records whose ctrl_id_bytes == ctrl_id."""
    return [r for r in flat if r.ctrl_id_bytes == ctrl_id]


# ── 1. 다단 (multi-column) verifier ───────────────────────────────────────

_SECD = b"secd"
_COLD = b"cold"


def verify_multicol(doc: HwpOleDocument, report: VerifyReport) -> None:
    """Check that every secd control has a properly-formed cold child."""
    for sec_path in doc.section_streams():
        data = doc.read_stream(sec_path)
        flat, _ = parse_stream(data, sec_path)

        secds = _ctrl_chain(flat, _SECD)
        for secd in secds:
            # After CTRL_HEADER(secd) there should be a LIST_HEADER containing
            # at least one CTRL_HEADER(cold).  Find in flat by offset ordering.
            idx = flat.index(secd)
            # Collect children at secd.level+1 until we leave that subtree
            subtree = [
                r for r in flat[idx + 1:]
                if r.level > secd.level
            ]
            colds = [r for r in subtree if r.ctrl_id_bytes == _COLD]

            if not colds:
                # secd with no cold → single-column or implicit column → WARN
                report.add(VerifyIssue(
                    feature="multicol", severity="INFO",
                    stream=sec_path, offset=secd.offset,
                    tag_name="CTRL_HEADER(secd)",
                    description="secd control has no cold child (single-column section)",
                ))
                continue

            for cold in colds:
                payload = cold.data
                # cold payload byte 0–1: column count (uint16 LE)
                if len(payload) < 2:
                    report.add(VerifyIssue(
                        feature="multicol", severity="ERROR",
                        stream=sec_path, offset=cold.offset,
                        tag_name="CTRL_HEADER(cold)",
                        description="cold payload too short to read column count",
                        hint="Minimum payload for cold is 2 bytes (column count).",
                    ))
                    continue
                col_count = struct.unpack_from("<H", payload, 0)[0]
                if col_count < 1 or col_count > 20:
                    report.add(VerifyIssue(
                        feature="multicol", severity="ERROR",
                        stream=sec_path, offset=cold.offset,
                        tag_name="CTRL_HEADER(cold)",
                        description=f"cold column count {col_count} out of valid range [1, 20]",
                        hint="Column count must be >= 1.",
                    ))


# ── 2. 바탕쪽 (master page) verifier ──────────────────────────────────────

def verify_masterpage(doc: HwpOleDocument, report: VerifyReport) -> None:
    """Check MasterPage stream structure and header/footer controls."""
    mp_streams = doc.masterpage_streams()
    if not mp_streams:
        # Absence of MasterPage is only a warning if headers/footers were found in body
        has_hdr = False
        for sec_path in doc.section_streams():
            data = doc.read_stream(sec_path)
            flat, _ = parse_stream(data, sec_path)
            if any(r.ctrl_id_bytes in (b"hdr ", b"ftr ") for r in flat):
                has_hdr = True
                break
        if has_hdr:
            report.add(VerifyIssue(
                feature="masterpage", severity="WARN",
                stream="MasterPage", offset=-1, tag_name="STREAM",
                description="hdr/ftr controls exist in body but MasterPage stream absent",
                hint="Header/footer shape definitions belong in MasterPage/MasterPage0.",
            ))
        return

    for mp in mp_streams:
        data = doc.read_stream(mp)
        flat, _ = parse_stream(data, mp)

        if not flat:
            report.add(VerifyIssue(
                feature="masterpage", severity="ERROR",
                stream=mp, offset=-1, tag_name="STREAM",
                description="MasterPage stream present but empty",
            ))
            continue

        # Must start with PARA_HEAD
        if flat[0].tag != 0x32:
            report.add(VerifyIssue(
                feature="masterpage", severity="ERROR",
                stream=mp, offset=flat[0].offset, tag_name=flat[0].tag_name,
                description="MasterPage stream does not begin with PARA_HEAD",
                hint="PARA_HEAD (0x32) must be the first record in every body/masterpage stream.",
            ))

        # LIST_HEADER (0x45) must precede each child paragraph group
        prev: Optional[HwpRecord] = None
        for rec in flat:
            if prev and rec.level > prev.level and prev.tag not in (0x45, 0x36):
                report.add(VerifyIssue(
                    feature="masterpage", severity="WARN",
                    stream=mp, offset=rec.offset, tag_name=rec.tag_name,
                    description=(
                        f"Child record at level {rec.level} without LIST_HEADER parent "
                        f"(parent is {prev.tag_name})"
                    ),
                ))
            prev = rec


# ── 3. 미구현 컨트롤 (unknown controls) verifier ──────────────────────────

_KNOWN_CTRL_BYTES = set(CTRL_IDS.keys())

# Controls that rhwp-studio accepts but Hancom requires specific handling
_PARTIAL_IMPL_CTRLS = {
    b"revt": "REVISION",
    b"mequ": "MATH_EQUATION",
    b"idot": "MEMO",
}


def verify_controls(doc: HwpOleDocument, report: VerifyReport) -> None:
    """Detect unimplemented or partially-implemented controls."""
    for sec_path in doc.section_streams():
        data = doc.read_stream(sec_path)
        flat, _ = parse_stream(data, sec_path)

        for rec in flat:
            if rec.tag != 0x36:
                continue
            cb = rec.ctrl_id_bytes
            if cb is None:
                continue

            if cb not in _KNOWN_CTRL_BYTES:
                ascii_id = cb.decode("ascii", errors="replace")
                report.add(VerifyIssue(
                    feature="controls", severity="ERROR",
                    stream=sec_path, offset=rec.offset, tag_name="CTRL_HEADER",
                    description=f"Unknown control ID {cb.hex()} ('{ascii_id}')",
                    hint=(
                        "Unknown controls must have CTRL_DATA immediately after "
                        "CTRL_HEADER. Hancom will discard unrecognized controls "
                        "but file is still openable."
                    ),
                ))
                # Check that CTRL_DATA follows
                idx = flat.index(rec)
                has_ctrl_data = any(
                    f.tag == 0x3A and f.level == rec.level
                    for f in flat[idx + 1: idx + 5]
                )
                if not has_ctrl_data:
                    report.add(VerifyIssue(
                        feature="controls", severity="ERROR",
                        stream=sec_path, offset=rec.offset, tag_name="CTRL_HEADER",
                        description=(
                            f"Unknown control '{ascii_id}' missing CTRL_DATA fallback"
                        ),
                        hint="CTRL_DATA (0x3A) must follow unknown controls.",
                    ))

            elif cb in _PARTIAL_IMPL_CTRLS:
                name = _PARTIAL_IMPL_CTRLS[cb]
                report.add(VerifyIssue(
                    feature="controls", severity="WARN",
                    stream=sec_path, offset=rec.offset, tag_name="CTRL_HEADER",
                    description=f"Partially-implemented control {name} ({cb!r})",
                    hint=f"{name} may not render correctly in older Hancom versions.",
                ))


# ── 4. record tail / count / level / parent verifier ──────────────────────

def verify_structure(doc: HwpOleDocument, report: VerifyReport) -> None:
    """Verify record-level structural invariants in every body stream."""
    streams_to_check = doc.section_streams() + doc.masterpage_streams()
    if doc.has_stream("DocInfo"):
        streams_to_check.insert(0, "DocInfo")

    for stream_path in streams_to_check:
        data = doc.read_stream(stream_path)
        flat, roots = parse_stream(data, stream_path)
        _verify_level_parent(flat, stream_path, report)
        _verify_list_header_counts(flat, stream_path, report)


def _verify_level_parent(
    flat: List[HwpRecord], stream_path: str, report: VerifyReport
) -> None:
    """S-001: every level N record must follow a level N-1 record (no gaps)."""
    prev_level = 0
    for rec in flat:
        if rec.level > prev_level + 1:
            report.add(VerifyIssue(
                feature="structure", severity="ERROR",
                stream=stream_path, offset=rec.offset, tag_name=rec.tag_name,
                description=(
                    f"Level gap: {prev_level} → {rec.level} "
                    f"(tag {rec.tag_name})"
                ),
                hint=(
                    "HWP5 requires level to increase by exactly 1. "
                    "Insert missing intermediate records or fix the level field."
                ),
            ))
        prev_level = rec.level


def _verify_list_header_counts(
    flat: List[HwpRecord], stream_path: str, report: VerifyReport
) -> None:
    """S-002: LIST_HEADER paraCount field must match actual PARA_HEAD children.

    LIST_HEADER payload byte 0 = paragraph count (uint16 LE for HWP >= 5.0.3).
    """
    for i, rec in enumerate(flat):
        if rec.tag != 0x45:    # LIST_HEADER
            continue
        payload = rec.data
        if len(payload) < 2:
            report.add(VerifyIssue(
                feature="structure", severity="ERROR",
                stream=stream_path, offset=rec.offset, tag_name="LIST_HEADER",
                description="LIST_HEADER payload too short to read paragraph count",
            ))
            continue
        declared_para_count = struct.unpack_from("<H", payload, 0)[0]

        # count PARA_HEAD records at level rec.level+1 in the immediate subtree
        actual = 0
        for j in range(i + 1, len(flat)):
            nxt = flat[j]
            if nxt.level <= rec.level:
                break
            if nxt.level == rec.level + 1 and nxt.tag == 0x32:
                actual += 1

        if declared_para_count != actual:
            report.add(VerifyIssue(
                feature="structure", severity="WARN",
                stream=stream_path, offset=rec.offset, tag_name="LIST_HEADER",
                description=(
                    f"LIST_HEADER paraCount={declared_para_count}, "
                    f"actual PARA_HEAD children={actual}"
                ),
                hint=(
                    "Update LIST_HEADER payload bytes 0-1 to "
                    f"{actual} ({actual:#06x} LE)."
                ),
            ))


# ── public API ─────────────────────────────────────────────────────────────

ALL_FEATURES = ("multicol", "masterpage", "controls", "structure")


def verify_file(
    path: str,
    features: tuple[str, ...] = ALL_FEATURES,
    max_sections: int = 0,
) -> VerifyReport:
    """Run selected feature verifiers on *path*."""
    report = VerifyReport(path=path)
    try:
        with HwpOleDocument(path) as doc:
            if "multicol"   in features: verify_multicol(doc, report)
            if "masterpage" in features: verify_masterpage(doc, report)
            if "controls"   in features: verify_controls(doc, report)
            if "structure"  in features: verify_structure(doc, report)
    except Exception as exc:
        report.add(VerifyIssue(
            feature="open", severity="ERROR",
            stream="<open>", offset=-1, tag_name="FILE",
            description=f"Cannot open file: {exc}",
        ))
    return report
