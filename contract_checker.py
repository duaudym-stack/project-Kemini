"""HWP5 contract checker.

Validates a single HWP file against the structural contracts that, when
violated, cause Hancom editor to reject the file (or silently corrupt it).

Severity levels
---------------
CRITICAL  file cannot open at all
HIGH      file opens but content is missing or garbled
MEDIUM    specific feature broken (multi-column, master-page, etc.)
LOW       cosmetic / non-blocking deviation from spec
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .ole_reader   import HwpOleDocument
from .record_parser import (
    HwpRecord, DOCINFO_REQUIRED_SEQUENCE, DOCINFO_REQUIRED_ANYWHERE,
    parse_stream,
)


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"


@dataclass
class Violation:
    severity:    Severity
    stream:      str
    offset:      int         # byte offset in stream (-1 = stream-level)
    tag_name:    str
    rule:        str         # rule ID
    description: str
    hint:        str = ""    # suggested fix

    def __str__(self) -> str:
        loc = f"{self.stream}@{self.offset:#010x}" if self.offset >= 0 else self.stream
        return f"[{self.severity.value:8s}] {loc}  {self.tag_name}  {self.rule}: {self.description}"


@dataclass
class ContractReport:
    path:       str
    violations: List[Violation] = field(default_factory=list)

    # ── convenience ───────────────────────────────────────────────────
    def add(self, v: Violation) -> None:
        self.violations.append(v)

    def critical(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == Severity.CRITICAL]

    def high(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == Severity.HIGH]

    def passed(self) -> bool:
        return not any(v.severity in (Severity.CRITICAL, Severity.HIGH)
                       for v in self.violations)

    def summary(self) -> str:
        c = len(self.critical())
        h = len(self.high())
        m = sum(1 for v in self.violations if v.severity == Severity.MEDIUM)
        l = sum(1 for v in self.violations if v.severity == Severity.LOW)
        status = "PASS" if self.passed() else "FAIL"
        return (f"[{status}] {self.path}  "
                f"CRITICAL={c} HIGH={h} MEDIUM={m} LOW={l}")

    def to_dict(self) -> dict:
        return {
            "path":       self.path,
            "passed":     self.passed(),
            "violations": [
                {
                    "severity":    v.severity.value,
                    "stream":      v.stream,
                    "offset":      v.offset,
                    "tag_name":    v.tag_name,
                    "rule":        v.rule,
                    "description": v.description,
                    "hint":        v.hint,
                }
                for v in self.violations
            ],
        }


# ── individual rule checkers ───────────────────────────────────────────────

def _check_file_header(doc: HwpOleDocument, report: ContractReport) -> None:
    """R-001 / R-002: FileHeader signature + HWP version."""
    ok, msg = doc.validate_signature()
    if not ok:
        report.add(Violation(
            severity=Severity.CRITICAL, stream="FileHeader", offset=-1,
            tag_name="FILE_SIGNATURE", rule="R-001",
            description=msg,
            hint="Ensure the file is a genuine HWP5 binary, not HWPX or a renamed file.",
        ))
        return   # no point continuing without valid header

    major, minor, micro, build = doc.hwp_version()
    if major < 5:
        report.add(Violation(
            severity=Severity.HIGH, stream="FileHeader", offset=-1,
            tag_name="HWP_VERSION", rule="R-002",
            description=f"HWP version {major}.{minor}.{micro}.{build} is below 5.0",
            hint="Re-save as HWP 5.0 or later from Hancom.",
        ))


def _check_docinfo_stream(doc: HwpOleDocument, report: ContractReport) -> None:
    """R-010 – R-020: DocInfo stream structural contracts."""
    if not doc.has_stream("DocInfo"):
        report.add(Violation(
            severity=Severity.CRITICAL, stream="DocInfo", offset=-1,
            tag_name="STREAM", rule="R-010",
            description="DocInfo stream is absent",
            hint="HWPX→HWP conversion must emit the DocInfo OLE stream.",
        ))
        return

    data = doc.read_stream("DocInfo")
    flat, _ = parse_stream(data, "DocInfo")
    if not flat:
        report.add(Violation(
            severity=Severity.CRITICAL, stream="DocInfo", offset=-1,
            tag_name="STREAM", rule="R-011",
            description="DocInfo stream is empty or failed to decompress",
        ))
        return

    # R-012: first two records must be DOCUMENT_PROPERTIES then ID_MAPPINGS
    for i, required_tag in enumerate(DOCINFO_REQUIRED_SEQUENCE):
        if i >= len(flat):
            report.add(Violation(
                severity=Severity.CRITICAL, stream="DocInfo", offset=-1,
                tag_name=f"TAG_{required_tag:#05x}", rule="R-012",
                description=f"Required record #{i} (tag {required_tag:#05x}) is absent",
            ))
            continue
        rec = flat[i]
        if rec.tag != required_tag:
            report.add(Violation(
                severity=Severity.CRITICAL, stream="DocInfo", offset=rec.offset,
                tag_name=rec.tag_name, rule="R-012",
                description=(
                    f"Position {i}: expected tag {required_tag:#05x}, "
                    f"got {rec.tag:#05x} ({rec.tag_name})"
                ),
                hint="DOCUMENT_PROPERTIES must be first, ID_MAPPINGS second in DocInfo.",
            ))

    # R-013: COMPATIBLE_DOCUMENT must be present
    present_tags = {r.tag for r in flat}
    for req in DOCINFO_REQUIRED_ANYWHERE:
        if req not in present_tags:
            report.add(Violation(
                severity=Severity.HIGH, stream="DocInfo", offset=-1,
                tag_name=f"TAG_{req:#05x}", rule="R-013",
                description=f"Required record tag {req:#05x} (COMPATIBLE_DOCUMENT) absent",
                hint=(
                    "Hancom 2018+ requires COMPATIBLE_DOCUMENT (0x1E) in DocInfo. "
                    "Write a minimal record: 1 byte = 0x06 (version)."
                ),
            ))

    # R-014: ID_MAPPINGS count fields must match actual record counts
    if len(flat) >= 2 and flat[1].tag == 0x11:
        _check_id_mappings(flat, report)

    # R-015: level must be 0 for all top-level DocInfo records
    for rec in flat:
        if rec.level != 0:
            report.add(Violation(
                severity=Severity.MEDIUM, stream="DocInfo", offset=rec.offset,
                tag_name=rec.tag_name, rule="R-015",
                description=f"DocInfo record at unexpected level {rec.level} (expected 0)",
            ))


def _check_id_mappings(flat: List[HwpRecord], report: ContractReport) -> None:
    """R-014: ID_MAPPINGS counts must match the number of type records that follow."""
    id_map = flat[1]
    data   = id_map.data
    if len(data) < 32:
        report.add(Violation(
            severity=Severity.HIGH, stream="DocInfo", offset=id_map.offset,
            tag_name="ID_MAPPINGS", rule="R-014",
            description=f"ID_MAPPINGS payload too short ({len(data)} bytes, need >= 32)",
            hint="The record must declare counts for all ID table types.",
        ))
        return

    # offsets within ID_MAPPINGS payload (each count is 4 bytes LE)
    count_fields = [
        (0,  0x12, "BIN_DATA"),
        (4,  0x13, "FACE_NAME"),
        (8,  0x14, "BORDER_FILL"),
        (12, 0x15, "CHAR_SHAPE"),
        (16, 0x16, "TAB_DEF"),
        (20, 0x17, "PARA_NUM_DEF"),
        (24, 0x18, "BULLET_DEF"),
        (28, 0x1A, "PARA_SHAPE"),
    ]
    tag_counts = {}
    for rec in flat[2:]:
        tag_counts[rec.tag] = tag_counts.get(rec.tag, 0) + 1

    for byte_off, tag, name in count_fields:
        if byte_off + 4 > len(data):
            break
        declared = struct.unpack_from("<I", data, byte_off)[0]
        actual   = tag_counts.get(tag, 0)
        if declared != actual:
            report.add(Violation(
                severity=Severity.HIGH, stream="DocInfo", offset=id_map.offset,
                tag_name="ID_MAPPINGS", rule="R-014",
                description=(
                    f"{name} count mismatch: ID_MAPPINGS declares {declared}, "
                    f"actual records = {actual}"
                ),
                hint=f"Update ID_MAPPINGS byte offset {byte_off} to {actual:#010x}.",
            ))


def _check_section_stream(
    doc: HwpOleDocument, stream_path: str, report: ContractReport
) -> None:
    """R-030 – R-060: section body structural contracts."""
    data = doc.read_stream(stream_path)
    flat, _ = parse_stream(data, stream_path)

    if not flat:
        report.add(Violation(
            severity=Severity.CRITICAL, stream=stream_path, offset=-1,
            tag_name="STREAM", rule="R-030",
            description="Section stream is empty",
        ))
        return

    # R-031: first record must be PARA_HEAD at level 0
    if flat[0].tag != 0x32:
        report.add(Violation(
            severity=Severity.CRITICAL, stream=stream_path, offset=flat[0].offset,
            tag_name=flat[0].tag_name, rule="R-031",
            description=f"First record in section is {flat[0].tag_name}, expected PARA_HEAD",
        ))

    prev: Optional[HwpRecord] = None
    level_stack: list[int] = []

    for i, rec in enumerate(flat):
        # R-040: level must not jump by more than 1
        if prev is not None:
            delta = rec.level - prev.level
            if delta > 1:
                report.add(Violation(
                    severity=Severity.HIGH, stream=stream_path, offset=rec.offset,
                    tag_name=rec.tag_name, rule="R-040",
                    description=(
                        f"Level jumped {prev.level} → {rec.level} "
                        f"(after {prev.tag_name} @{prev.offset:#010x})"
                    ),
                    hint="Each child record must be at exactly parent_level + 1.",
                ))

        # R-041: CTRL_HEADER at level N must be followed by LIST_HEADER at level N+1
        if rec.tag == 0x36:   # CTRL_HEADER
            # look ahead (skip optional CTRL_DATA at same level)
            found_list_header = False
            for j in range(i + 1, min(i + 6, len(flat))):
                nxt = flat[j]
                if nxt.level <= rec.level and nxt.tag not in (0x3A,):  # CTRL_DATA same level OK
                    break
                if nxt.tag == 0x45 and nxt.level == rec.level + 1:     # LIST_HEADER
                    found_list_header = True
                    break
            if not found_list_header:
                report.add(Violation(
                    severity=Severity.HIGH, stream=stream_path, offset=rec.offset,
                    tag_name="CTRL_HEADER", rule="R-041",
                    description=(
                        f"CTRL_HEADER (ctrl={rec.ctrl_id}) at level {rec.level} "
                        f"has no LIST_HEADER at level {rec.level + 1}"
                    ),
                    hint="Every control must have a LIST_HEADER child.",
                ))

        # R-042: PARA_TEXT must follow PARA_HEAD at same level
        if rec.tag == 0x33:   # PARA_TEXT
            if prev is None or prev.tag != 0x32:
                report.add(Violation(
                    severity=Severity.MEDIUM, stream=stream_path, offset=rec.offset,
                    tag_name="PARA_TEXT", rule="R-042",
                    description="PARA_TEXT not immediately preceded by PARA_HEAD",
                ))

        prev = rec


def _check_masterpage_streams(doc: HwpOleDocument, report: ContractReport) -> None:
    """R-070: MasterPage streams must be structurally valid if present."""
    for mp in doc.masterpage_streams():
        data = doc.read_stream(mp)
        flat, _ = parse_stream(data, mp)
        if flat and flat[0].tag != 0x32:   # must start with PARA_HEAD
            report.add(Violation(
                severity=Severity.MEDIUM, stream=mp, offset=flat[0].offset,
                tag_name=flat[0].tag_name, rule="R-070",
                description=f"MasterPage stream does not start with PARA_HEAD",
            ))


# ── public API ─────────────────────────────────────────────────────────────

def check_file(path: str, max_sections: int = 0) -> ContractReport:
    """Run all contracts on *path*.  max_sections=0 means check all sections."""
    report = ContractReport(path=path)
    try:
        with HwpOleDocument(path) as doc:
            _check_file_header(doc, report)
            _check_docinfo_stream(doc, report)
            sections = doc.section_streams()
            if max_sections:
                sections = sections[:max_sections]
            for sec in sections:
                _check_section_stream(doc, sec, report)
            _check_masterpage_streams(doc, report)
    except Exception as exc:
        report.add(Violation(
            severity=Severity.CRITICAL, stream="<open>", offset=-1,
            tag_name="FILE", rule="R-000",
            description=f"Cannot open file: {exc}",
        ))
    return report
