"""HWP5 binary validator package.

Public surface
--------------
  check_file(path, max_sections=0)  → ContractReport
  verify_file(path, features, max_sections=0)  → VerifyReport
  diff_files(ref, cand, max_sections=0)  → DiffReport

CLI
---
  python -m figure_planner.hwp5_validator --help
"""
from .contract_checker import check_file, ContractReport, Severity, Violation
from .stream_verifier  import verify_file, VerifyReport, VerifyIssue, ALL_FEATURES
from .record_diff      import diff_files, DiffReport, RecordDiff
from .record_parser    import HwpRecord, parse_stream, iter_records, TAGS, CTRL_IDS
from .ole_reader       import HwpOleDocument

__all__ = [
    "check_file", "ContractReport", "Severity", "Violation",
    "verify_file", "VerifyReport", "VerifyIssue", "ALL_FEATURES",
    "diff_files",  "DiffReport",   "RecordDiff",
    "HwpRecord",   "parse_stream", "iter_records", "TAGS", "CTRL_IDS",
    "HwpOleDocument",
]
