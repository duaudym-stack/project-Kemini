"""HWP5 two-file record tree diff.

Compares two HWP5 binary files at the record level, reporting differences
ordered by their likely impact (CRITICAL violations first).

Usage
-----
from .record_diff import diff_files, DiffReport
report = diff_files("reference.hwp", "candidate.hwp", max_sections=1)
print(report.summary())
for d in report.diffs:
    print(d)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional

from .ole_reader    import HwpOleDocument
from .record_parser import HwpRecord, parse_stream, TAGS, CTRL_IDS


# ── impact ranking ─────────────────────────────────────────────────────────

STREAM_PRIORITY = {
    "FileHeader":          0,
    "DocInfo":             1,
    "BodyText/Section0":   2,
}

def _stream_priority(path: str) -> int:
    for k, v in STREAM_PRIORITY.items():
        if path.startswith(k):
            return v
    return 99


@dataclass
class RecordDiff:
    stream:      str
    kind:        str          # "MISSING" | "EXTRA" | "SIZE" | "DATA" | "LEVEL" | "TAG"
    severity:    str          # "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
    ref_offset:  int          # -1 if absent in reference
    cand_offset: int          # -1 if absent in candidate
    description: str
    hint:        str = ""

    def __str__(self) -> str:
        ref  = f"{self.ref_offset:#010x}"  if self.ref_offset  >= 0 else "     --    "
        cand = f"{self.cand_offset:#010x}" if self.cand_offset >= 0 else "     --    "
        return (
            f"[{self.severity:8s}/{self.kind:7s}] "
            f"ref={ref} cand={cand}  {self.stream}: {self.description}"
        )

    def priority(self) -> tuple:
        sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        return (sev_order.get(self.severity, 9), _stream_priority(self.stream))


@dataclass
class DiffReport:
    ref_path:  str
    cand_path: str
    diffs:     List[RecordDiff] = field(default_factory=list)

    def add(self, d: RecordDiff) -> None:
        self.diffs.append(d)

    def sorted(self) -> List[RecordDiff]:
        return sorted(self.diffs, key=lambda d: d.priority())

    def summary(self) -> str:
        by_sev = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for d in self.diffs:
            by_sev[d.severity] = by_sev.get(d.severity, 0) + 1
        status = "IDENTICAL" if not self.diffs else (
            "FAIL" if (by_sev["CRITICAL"] or by_sev["HIGH"]) else "WARN"
        )
        return (
            f"[{status}]  ref={self.ref_path}  cand={self.cand_path}  "
            f"CRITICAL={by_sev['CRITICAL']} HIGH={by_sev['HIGH']} "
            f"MEDIUM={by_sev['MEDIUM']} LOW={by_sev['LOW']}"
        )

    def to_dict(self) -> dict:
        return {
            "ref_path":  self.ref_path,
            "cand_path": self.cand_path,
            "summary":   self.summary(),
            "diffs": [vars(d) for d in self.sorted()],
        }


# ── flat-list alignment ─────────────────────────────────────────────────────

def _align(
    ref_flat:  List[HwpRecord],
    cand_flat: List[HwpRecord],
    stream:    str,
    report:    DiffReport,
) -> None:
    """Linear walk: compare records positionally, note size/data/level/tag diffs."""
    max_len = max(len(ref_flat), len(cand_flat))
    for i in range(max_len):
        ref_rec  = ref_flat[i]  if i < len(ref_flat)  else None
        cand_rec = cand_flat[i] if i < len(cand_flat) else None

        ref_off  = ref_rec.offset  if ref_rec  else -1
        cand_off = cand_rec.offset if cand_rec else -1

        if ref_rec is None:
            report.add(RecordDiff(
                stream=stream, kind="EXTRA", severity="HIGH",
                ref_offset=-1, cand_offset=cand_off,
                description=(
                    f"Candidate has extra record #{i}: {cand_rec.tag_name} "
                    f"(tag {cand_rec.tag:#05x})"
                ),
                hint="Candidate emits records not present in reference at this position.",
            ))
            continue

        if cand_rec is None:
            report.add(RecordDiff(
                stream=stream, kind="MISSING", severity="CRITICAL",
                ref_offset=ref_off, cand_offset=-1,
                description=(
                    f"Candidate missing record #{i}: {ref_rec.tag_name} "
                    f"(tag {ref_rec.tag:#05x})"
                ),
                hint="Insert the missing record at this position in the candidate stream.",
            ))
            continue

        # Tag mismatch
        if ref_rec.tag != cand_rec.tag:
            report.add(RecordDiff(
                stream=stream, kind="TAG", severity="HIGH",
                ref_offset=ref_off, cand_offset=cand_off,
                description=(
                    f"Record #{i} tag mismatch: "
                    f"ref={ref_rec.tag_name}({ref_rec.tag:#05x}), "
                    f"cand={cand_rec.tag_name}({cand_rec.tag:#05x})"
                ),
            ))
            continue   # don't report size/data on a wrong-tag record

        # Level mismatch
        if ref_rec.level != cand_rec.level:
            report.add(RecordDiff(
                stream=stream, kind="LEVEL", severity="HIGH",
                ref_offset=ref_off, cand_offset=cand_off,
                description=(
                    f"Record #{i} ({ref_rec.tag_name}) level: "
                    f"ref={ref_rec.level}, cand={cand_rec.level}"
                ),
                hint="Fix the level field in the record header DWORD (bits 10-19).",
            ))

        # Size mismatch → always HIGH, data comparison may be misleading if sizes differ
        if ref_rec.size != cand_rec.size:
            report.add(RecordDiff(
                stream=stream, kind="SIZE", severity="HIGH",
                ref_offset=ref_off, cand_offset=cand_off,
                description=(
                    f"Record #{i} ({ref_rec.tag_name}) size: "
                    f"ref={ref_rec.size}, cand={cand_rec.size}"
                ),
            ))
            # Still compare common prefix
            compare_len = min(ref_rec.size, cand_rec.size)
            if ref_rec.data[:compare_len] != cand_rec.data[:compare_len]:
                _report_data_diff(stream, i, ref_rec, cand_rec, report, max_bytes=compare_len)
            continue

        # Same tag, level, size — check payload
        if ref_rec.data != cand_rec.data:
            _report_data_diff(stream, i, ref_rec, cand_rec, report)


def _report_data_diff(
    stream: str,
    index: int,
    ref:   HwpRecord,
    cand:  HwpRecord,
    report: DiffReport,
    max_bytes: Optional[int] = None,
) -> None:
    ref_data  = ref.data  if max_bytes is None else ref.data[:max_bytes]
    cand_data = cand.data if max_bytes is None else cand.data[:max_bytes]

    # Find first differing byte
    diff_byte = next(
        (i for i, (a, b) in enumerate(zip(ref_data, cand_data)) if a != b),
        len(ref_data),
    )

    # Severity: DocInfo DATA diffs are HIGH (may affect ID mappings/styles)
    sev = "HIGH" if stream in ("DocInfo",) else "MEDIUM"

    report.add(RecordDiff(
        stream=stream, kind="DATA", severity=sev,
        ref_offset=ref.offset, cand_offset=cand.offset,
        description=(
            f"Record #{index} ({ref.tag_name}) payload differs at byte {diff_byte}: "
            f"ref={ref_data[diff_byte:diff_byte+4].hex() if diff_byte < len(ref_data) else '(end)'}, "
            f"cand={cand_data[diff_byte:diff_byte+4].hex() if diff_byte < len(cand_data) else '(end)'}"
        ),
        hint=f"Compare full payloads starting at stream offset {ref.offset + 4 + diff_byte}.",
    ))


# ── stream-level driver ─────────────────────────────────────────────────────

def _compare_stream(
    ref_doc:  HwpOleDocument,
    cand_doc: HwpOleDocument,
    path:     str,
    report:   DiffReport,
) -> None:
    ref_exists  = ref_doc.has_stream(path)
    cand_exists = cand_doc.has_stream(path)

    if ref_exists and not cand_exists:
        report.add(RecordDiff(
            stream=path, kind="MISSING", severity="CRITICAL",
            ref_offset=-1, cand_offset=-1,
            description=f"Stream '{path}' present in reference but absent in candidate",
            hint="The converter must emit this OLE stream.",
        ))
        return

    if cand_exists and not ref_exists:
        # extra stream in candidate is usually not fatal
        report.add(RecordDiff(
            stream=path, kind="EXTRA", severity="LOW",
            ref_offset=-1, cand_offset=-1,
            description=f"Stream '{path}' present in candidate but absent in reference",
        ))
        return

    if not (ref_exists and cand_exists):
        return

    ref_data  = ref_doc.read_stream(path)
    cand_data = cand_doc.read_stream(path)

    ref_flat,  _ = parse_stream(ref_data,  path)
    cand_flat, _ = parse_stream(cand_data, path)

    _align(ref_flat, cand_flat, path, report)


# ── public API ─────────────────────────────────────────────────────────────

def diff_files(
    ref_path:    str,
    cand_path:   str,
    max_sections: int = 0,
) -> DiffReport:
    """Compare two HWP5 files at record level.

    max_sections=1 → compare only first section body stream (fast, for 1-page samples).
    max_sections=0 → compare all section streams.
    """
    report = DiffReport(ref_path=ref_path, cand_path=cand_path)
    try:
        with HwpOleDocument(ref_path) as ref_doc, \
             HwpOleDocument(cand_path) as cand_doc:

            # Always compare structural meta-streams
            for always in ("FileHeader", "DocInfo"):
                _compare_stream(ref_doc, cand_doc, always, report)

            # Section body streams
            ref_secs  = ref_doc.section_streams()
            cand_secs = cand_doc.section_streams()
            if max_sections:
                ref_secs  = ref_secs[:max_sections]
                cand_secs = cand_secs[:max_sections]

            all_secs = sorted(set(ref_secs) | set(cand_secs))
            for sec in all_secs:
                _compare_stream(ref_doc, cand_doc, sec, report)

            # MasterPage streams
            ref_mps  = set(ref_doc.masterpage_streams())
            cand_mps = set(cand_doc.masterpage_streams())
            for mp in sorted(ref_mps | cand_mps):
                _compare_stream(ref_doc, cand_doc, mp, report)

    except Exception as exc:
        report.add(RecordDiff(
            stream="<open>", kind="MISSING", severity="CRITICAL",
            ref_offset=-1, cand_offset=-1,
            description=f"Cannot open files for diff: {exc}",
        ))

    return report
