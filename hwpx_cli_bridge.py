"""Bridge for the bundled reallygood83/hwpx-cli package."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


_STATUS_PREFIXES = (
    "Reading:",
    "Converting:",
    "Output:",
    "Image mode:",
    "Token efficient:",
    "Wrote markdown:",
    "Wrote image manifest:",
)


def _workspace_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _hwpx_cli_dir() -> Path:
    return _workspace_dir() / "hwpx-cli"


def _hwpx_cli_entry() -> Path:
    return _hwpx_cli_dir() / "packages" / "hwpx-cli" / "dist" / "cli.js"


def hwpx_cli_available() -> bool:
    return bool(shutil.which("node")) and _hwpx_cli_entry().is_file()


def _run_hwpx_cli(args: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    node = shutil.which("node")
    cli = _hwpx_cli_entry()
    if not node or not cli.is_file():
        return None

    try:
        return subprocess.run(
            [node, str(cli), *args],
            cwd=str(_hwpx_cli_dir()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _clean_read_output(text: str) -> str:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if any(line.startswith(prefix) for prefix in _STATUS_PREFIXES):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def extract_markdown_with_hwpx_cli(file_bytes: bytes, timeout: int = 60) -> str | None:
    """Extract HWPX text/tables as Markdown using the bundled Node CLI."""
    if not hwpx_cli_available():
        return None

    with tempfile.TemporaryDirectory(prefix="figure_planner_hwpxcli_") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "input.hwpx"
        out = tmp_dir / "output.md"
        manifest = tmp_dir / "images.json"
        src.write_bytes(file_bytes)

        proc = _run_hwpx_cli(
            [
                "hwpx-to-md",
                str(src),
                "-o",
                str(out),
                "--image-mode",
                "placeholder",
                "--manifest",
                str(manifest),
            ],
            timeout,
        )
        if proc and proc.returncode == 0 and out.is_file():
            text = out.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text

        proc = _run_hwpx_cli(["read", str(src)], timeout)
        if proc and proc.returncode == 0:
            text = _clean_read_output((proc.stdout or "") + "\n" + (proc.stderr or ""))
            if text:
                return text

    return None


def convert_hwp_to_hwpx_with_hwpx_cli(file_bytes: bytes, timeout: int = 90) -> bytes | None:
    """Convert legacy HWP bytes to best-effort HWPX bytes using hwpx-cli."""
    if not hwpx_cli_available():
        return None

    with tempfile.TemporaryDirectory(prefix="figure_planner_hwpcli_") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "input.hwp"
        out = tmp_dir / "output.hwpx"
        src.write_bytes(file_bytes)

        proc = _run_hwpx_cli(["hwp-to-hwpx", str(src), "-o", str(out)], timeout)
        if proc and proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return out.read_bytes()

    return None


def extract_markdown_from_hwp_with_hwpx_cli(file_bytes: bytes, timeout: int = 120) -> str | None:
    """Extract legacy HWP text by converting it to HWPX first."""
    hwpx_bytes = convert_hwp_to_hwpx_with_hwpx_cli(file_bytes, timeout=timeout)
    if not hwpx_bytes:
        return None
    return extract_markdown_with_hwpx_cli(hwpx_bytes, timeout=timeout)
