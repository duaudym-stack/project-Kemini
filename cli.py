"""hwp5_validator CLI entry point.

Usage examples
--------------
# Check a single file against HWP5 contracts:
python -m figure_planner.hwp5_validator check candidate.hwp

# Check only the first page (1 section):
python -m figure_planner.hwp5_validator check candidate.hwp --max-sections 1

# Run per-feature stream verification:
python -m figure_planner.hwp5_validator verify candidate.hwp
python -m figure_planner.hwp5_validator verify candidate.hwp --feature multicol

# Diff two HWP files at record level:
python -m figure_planner.hwp5_validator diff reference.hwp candidate.hwp
python -m figure_planner.hwp5_validator diff reference.hwp candidate.hwp --max-sections 1

# Output JSON instead of text:
python -m figure_planner.hwp5_validator check candidate.hwp --json
"""
from __future__ import annotations

import argparse
import json
import sys

from .contract_checker import check_file, Severity
from .stream_verifier  import verify_file, ALL_FEATURES
from .record_diff      import diff_files


def cmd_check(args: argparse.Namespace) -> int:
    report = check_file(args.hwp, max_sections=args.max_sections)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.summary())
        for v in sorted(report.violations, key=lambda v: v.severity.value):
            print(v)
            if v.hint:
                print(f"       hint: {v.hint}")
    return 0 if report.passed() else 1


def cmd_verify(args: argparse.Namespace) -> int:
    features = (args.feature,) if args.feature else ALL_FEATURES
    report = verify_file(args.hwp, features=features, max_sections=args.max_sections)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        if not report.issues:
            print(f"[OK] {args.hwp}  no issues found")
        else:
            for issue in report.issues:
                print(issue)
                if issue.hint:
                    print(f"       hint: {issue.hint}")
    errors = report.errors()
    return 0 if not errors else 1


def cmd_diff(args: argparse.Namespace) -> int:
    report = diff_files(args.reference, args.candidate, max_sections=args.max_sections)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.summary())
        for d in report.sorted():
            print(d)
            if d.hint:
                print(f"       hint: {d.hint}")
    return 0 if not report.diffs else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m figure_planner.hwp5_validator",
        description="HWP5 binary validator & record diff tool",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # check
    p_check = sub.add_parser("check", help="Validate HWP5 contract rules")
    p_check.add_argument("hwp")
    p_check.add_argument("--max-sections", type=int, default=0,
                         metavar="N", help="Limit to first N sections (0=all)")
    p_check.add_argument("--json", action="store_true")

    # verify
    p_verify = sub.add_parser("verify", help="Run per-feature stream verifiers")
    p_verify.add_argument("hwp")
    p_verify.add_argument(
        "--feature", choices=list(ALL_FEATURES), default=None,
        help="Run only one feature verifier (default: all)",
    )
    p_verify.add_argument("--max-sections", type=int, default=0, metavar="N")
    p_verify.add_argument("--json", action="store_true")

    # diff
    p_diff = sub.add_parser("diff", help="Record-level diff between two HWP files")
    p_diff.add_argument("reference")
    p_diff.add_argument("candidate")
    p_diff.add_argument("--max-sections", type=int, default=0, metavar="N")
    p_diff.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "diff":
        return cmd_diff(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
