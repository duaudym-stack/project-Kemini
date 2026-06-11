"""Render uploaded documents into page images for visual analysis."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RenderedPage:
    page_number: int
    image_base64: str
    width: int
    height: int
    text: str
    source: str


def _running_process_ids(image_name: str) -> set[int]:
    if os.name != "nt":
        return set()
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
        )
    except Exception:
        return set()

    pids: set[int] = set()
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("INFO:"):
            continue
        if not line.startswith('"'):
            continue
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) < 2:
            continue
        try:
            pids.add(int(parts[1].replace(",", "")))
        except ValueError:
            continue
    return pids


def _terminate_process_ids(process_ids: set[int]) -> None:
    for pid in sorted(process_ids):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue


def _render_pdf_bytes(file_bytes: bytes, max_pages: int = 12, zoom: float = 1.5) -> list[RenderedPage]:
    try:
        import fitz
    except ImportError as exc:
        raise ImportError("PyMuPDF 패키지가 필요합니다: pip install pymupdf") from exc

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages: list[RenderedPage] = []
    matrix = fitz.Matrix(zoom, zoom)

    for idx in range(min(len(doc), max_pages)):
        page = doc[idx]
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        image_bytes = pix.tobytes("png")
        pages.append(RenderedPage(
            page_number=idx + 1,
            image_base64=base64.b64encode(image_bytes).decode("ascii"),
            width=pix.width,
            height=pix.height,
            text=(page.get_text("text") or "").strip(),
            source="pdf",
        ))

    return pages


def _office_looks_usable(path: Path, *, reject_msi_extract_placeholders: bool = False) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if not reject_msi_extract_placeholders:
        return True
    bootstrap = path.parent / "bootstrap.ini"
    if bootstrap.exists():
        text = bootstrap.read_text(encoding="utf-8", errors="ignore")
        # Raw MSI administrative extracts can leave template placeholders behind.
        # Official Windows installs may also contain this token, so only apply
        # this stricter check to bundled/portable candidates inside the workspace.
        if "<installmode>" in text:
            return False
    return True


def _office_binary() -> str | None:
    # 1) 명시적 환경변수 우선 (soffice.exe 또는 soffice.com 전체 경로)
    explicit = os.getenv("LIBREOFFICE_PATH") or os.getenv("SOFFICE_PATH")
    if explicit and _office_looks_usable(Path(explicit)):
        return explicit

    # Standard Windows install location. MSI installs normally land here.
    common = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    for path in common:
        if _office_looks_usable(Path(path)):
            return path
    # 2) 동봉된 포터블 LibreOffice (process_7) — 시스템 설치/관리자 권한 불필요
    #    HWPX 자체 import 필터는 없으므로 HTML 경유로 렌더링한다(아래 _convert_hwpx_to_pdf_via_html).
    # 3) PATH
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    # 4) 표준 설치 위치
    if os.getenv("ALLOW_PORTABLE_LIBREOFFICE", "").lower() in {"1", "true", "yes"}:
        for portable in _portable_office_candidates():
            if _office_looks_usable(portable, reject_msi_extract_placeholders=True):
                return str(portable)
    return None


def _portable_office_candidates() -> list[Path]:
    """동봉 포터블 LibreOffice(process_7) 후보 경로."""
    roots = [
        _workspace_dir().parent / "process_7",  # 표준 위치: 러닝톤/process_7
        _workspace_dir() / "process_7",
        _workspace_dir() / "libreoffice",
    ]
    names = ("soffice.com", "soffice.exe") if os.name == "nt" else ("soffice",)
    out: list[Path] = []
    for root in roots:
        for name in names:
            out.append(root / "program" / name)
            out.append(root / "LibreOffice" / "program" / name)
    return out


def _workspace_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _hwp_api_env_file() -> Path:
    return _workspace_dir() / "hwp_api_env"


def _hwp_api_env_files() -> list[Path]:
    workspace = _workspace_dir()
    return [workspace / "hwp_api_env.local", workspace / "hwp_api_env"]


def _argodocument_url_from_base(base: str) -> str:
    value = base.rstrip("/")
    if value.endswith("/argodocument"):
        return value
    return value + "/argodocument"


def _font_dir() -> Path:
    return _workspace_dir() / "font"


def _libreoffice_msi_file() -> Path:
    return _workspace_dir() / "LibreOffice_26.2.4_Win_x86-64.msi"


def _hancom2018_installer() -> Path | None:
    root = _workspace_dir() / "hancom2018 (1)"
    for candidate in (
        root / "Install.exe",
        root / "Install" / "Setup.exe",
        root / "Install" / "HOffice100.msi",
    ):
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _with_local_font_env() -> dict[str, str]:
    env = os.environ.copy()
    fonts = _font_dir()
    if fonts.exists():
        existing = env.get("SAL_FONTPATH", "")
        env["SAL_FONTPATH"] = str(fonts) if not existing else f"{fonts}{os.pathsep}{existing}"
        # LibreOffice on Linux/macOS honors fontconfig; Windows simply ignores it.
        env.setdefault("FONTCONFIG_PATH", str(fonts))
    return env


def _convert_to_pdf_with_office(file_bytes: bytes, file_format: str) -> bytes | None:
    return _convert_to_pdf_with_office_detailed(file_bytes, file_format, None)


def _convert_to_pdf_with_office_detailed(
    file_bytes: bytes,
    file_format: str,
    errors: list[str] | None,
) -> bytes | None:
    office = _office_binary()
    if not office:
        if errors is not None:
            msi = _libreoffice_msi_file()
            if msi.exists():
                errors.append(
                    f"LibreOffice MSI 설치 파일은 있지만 실행 파일(soffice.exe)은 없습니다. "
                    f"MSI를 설치하거나 LIBREOFFICE_PATH/SOFFICE_PATH에 soffice.exe 경로를 지정하세요: {msi}"
                )
            else:
                errors.append("LibreOffice 실행 파일을 찾지 못했습니다.")
        return None

    suffix = "." + file_format.lower().lstrip(".")
    with tempfile.TemporaryDirectory(prefix="figure_planner_render_") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / ("input" + suffix)
        out_dir = tmp_dir / "out"
        profile_dir = tmp_dir / "lo_profile"
        out_dir.mkdir()
        profile_dir.mkdir()
        src.write_bytes(file_bytes)

        cmd = [
            office,
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(src),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=_with_local_font_env())
        except subprocess.TimeoutExpired:
            if errors is not None:
                errors.append("LibreOffice 변환 시간이 초과되었습니다.")
            return None
        except Exception as exc:
            if errors is not None:
                errors.append(f"LibreOffice 실행 실패: {exc}")
            return None
        if proc.returncode != 0:
            if errors is not None:
                detail = (proc.stderr or proc.stdout or "").strip()
                errors.append(f"LibreOffice 변환 실패(returncode={proc.returncode}): {detail[:500]}")
            return None

        pdfs = list(out_dir.glob("*.pdf"))
        if not pdfs:
            if errors is not None:
                detail = (proc.stderr or proc.stdout or "").strip()
                errors.append(f"LibreOffice가 PDF를 만들지 못했습니다: {detail[:500]}")
            return None
        return pdfs[0].read_bytes()


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _hwpx_style_attr(style: dict | None) -> str:
    if not style:
        return ""
    css: list[str] = []
    char = style.get("char") or {}
    para = style.get("para") or {}
    size = char.get("fontSizePt")
    if isinstance(size, (int, float)) and size > 0:
        css.append(f"font-size:{max(7, min(size, 28))}pt")
    if char.get("bold"):
        css.append("font-weight:700")
    if char.get("italic"):
        css.append("font-style:italic")
    color = char.get("color")
    if isinstance(color, str) and re.match(r"^#[0-9a-fA-F]{6}$", color):
        css.append(f"color:{color}")
    align = para.get("textAlign")
    if align in {"left", "center", "right"}:
        css.append(f"text-align:{align}")
    if para.get("marginTopPt"):
        css.append(f"margin-top:{min(float(para['marginTopPt']), 30)}pt")
    if para.get("marginBottomPt"):
        css.append(f"margin-bottom:{min(float(para['marginBottomPt']), 30)}pt")
    return f' style="{";".join(css)}"' if css else ""


def _looks_like_heading(text: str) -> bool:
    """행정문서에서 흔한 제목/장 머리(예: '1.', '가.', 'Ⅰ.', '□', '○')를 가볍게 감지."""
    if len(text) > 40:
        return False
    return bool(re.match(r"^(\s*(\d+\.|[가-힣]\.|[ⅠⅡⅢⅣⅤ]+\.|[【\[].+[】\]])\s*)\S", text)) or text.startswith(("□", "■", "▣"))


def _extract_hwpx_bin_data(file_bytes: bytes) -> dict[str, str]:
    """HWPX ZIP의 BinData/ 이미지들을 {binaryItemIDRef: data_uri} 형태로 반환한다.

    HWPX XML에서 이미지는 binaryItemIDRef="image1" 형태로 참조되며,
    해당 파일은 BinData/image1.jpg 또는 BinData/BIN0001.jpg 등으로 저장된다.
    키는 파일명에서 확장자를 뺀 스템(예: "image1", "BIN0001")이다.
    """
    result: dict[str, str] = {}
    _MIME = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif",
        "bmp": "image/bmp",
    }
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            for zip_name in zf.namelist():
                norm = zip_name.replace("\\", "/").lower()
                if not norm.startswith("bindata/"):
                    continue
                fname = zip_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                if "." not in fname:
                    continue
                stem, dot, ext = fname.rpartition(".")
                mime = _MIME.get(ext.lower(), "")
                if not mime:
                    continue
                try:
                    data = zf.read(zip_name)
                    b64 = base64.b64encode(data).decode("ascii")
                    result[stem] = f"data:{mime};base64,{b64}"
                    # Also register numeric ID for BIN0001 → "1" style lookups
                    if stem.upper().startswith("BIN") and stem[3:].lstrip("0"):
                        result[stem[3:].lstrip("0")] = result[stem]
                except Exception:
                    pass
    except Exception:
        pass
    return result


def _hwpx_blocks_to_html(file_bytes: bytes) -> str | None:
    """HWPX → section_extract로 구조(텍스트/표/이미지) 추출 → A4 HTML 문서로 변환.

    LibreOffice는 HWPX import 필터가 없으므로(레거시 .hwp만 지원), 추출한 구조를
    HTML로 재구성해 LibreOffice가 PDF로 렌더하도록 한다. 한글 폰트는 시스템 폰트
    (맑은 고딕 등)와 동봉 font/ 폴더(SAL_FONTPATH)를 사용한다.
    """
    from . import section_extract

    try:
        data = section_extract.extract_sections(file_bytes)
    except Exception:
        return None

    bin_data = _extract_hwpx_bin_data(file_bytes)

    sections = data.get("sections") or []
    body_parts: list[str] = []
    for sec_idx, section in enumerate(sections):
        if sec_idx > 0:
            body_parts.append('<div class="page-break"></div>')
        for block in section.get("blocks", []):
            kind = block.get("kind")
            if kind == "image":
                bin_id = str(block.get("binItemIDRef", ""))
                img_src = bin_data.get(bin_id, "")
                if img_src:
                    body_parts.append(
                        f'<div style="text-align:center;margin:8pt 0">'
                        f'<img style="max-width:100%;height:auto" src="{img_src}" alt="그림"></div>'
                    )
            elif kind == "table":
                rows = block.get("rows") or []
                if not rows:
                    continue
                cells_html = []
                for r_idx, row in enumerate(rows):
                    tag = "th" if r_idx == 0 else "td"
                    tds = "".join(f"<{tag}>{_html_escape(c)}</{tag}>" for c in row)
                    cells_html.append(f"<tr>{tds}</tr>")
                body_parts.append("<table>" + "".join(cells_html) + "</table>")
            else:
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                if _looks_like_heading(text):
                    body_parts.append(f"<h3{_hwpx_style_attr(block.get('hwpStyle'))}>{_html_escape(text)}</h3>")
                else:
                    body_parts.append(f"<p{_hwpx_style_attr(block.get('hwpStyle'))}>{_html_escape(text)}</p>")

    if not body_parts:
        return None

    return (
        "<!DOCTYPE html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<style>"
        "@page{size:A4;margin:20mm 18mm;}"
        "body{font-family:'함초롬바탕','Malgun Gothic','맑은 고딕','NanumGothic',"
        "'Noto Sans KR','Noto Serif KR',sans-serif;font-size:11pt;line-height:1.6;color:#111;}"
        "h3{font-size:11.5pt;font-weight:700;margin:10pt 0 5pt;}"
        "p{margin:0 0 6pt;}"
        "table{border-collapse:collapse;width:100%;margin:8pt 0;}"
        "th,td{border:1px solid #555;padding:4pt 6pt;font-size:10.5pt;text-align:left;vertical-align:top;}"
        "th{background:#eef;font-weight:700;}"
        ".page-break{page-break-before:always;}"
        "</style></head><body>" + "".join(body_parts) + "</body></html>"
    )


def _convert_hwpx_to_pdf_via_html(file_bytes: bytes, errors: list[str] | None) -> bytes | None:
    """HWPX → HTML → (LibreOffice) → PDF. LibreOffice의 HWPX 미지원을 우회한다."""
    office = _office_binary()
    if not office:
        if errors is not None:
            msi = _libreoffice_msi_file()
            if msi.exists():
                errors.append(
                    f"LibreOffice MSI 설치 파일은 있지만 실행 파일(soffice.exe)은 없습니다(HTML 경유 변환). "
                    f"MSI를 설치하거나 LIBREOFFICE_PATH/SOFFICE_PATH에 soffice.exe 경로를 지정하세요: {msi}"
                )
            else:
                errors.append("LibreOffice 실행 파일을 찾지 못했습니다(HTML 경유 변환).")
        return None

    html = _hwpx_blocks_to_html(file_bytes)
    if not html:
        if errors is not None:
            errors.append("HWPX에서 본문 텍스트/표 구조를 추출하지 못했습니다.")
        return None

    with tempfile.TemporaryDirectory(prefix="figure_planner_hwpx_html_") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "input.html"
        out_dir = tmp_dir / "out"
        profile_dir = tmp_dir / "lo_profile"
        out_dir.mkdir()
        profile_dir.mkdir()
        src.write_text(html, encoding="utf-8")

        cmd = [
            office,
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--headless",
            "--convert-to",
            # HTML→PDF는 Writer 웹 필터가 아닌 Writer 필터로 강제해 표/페이지 나눔을 살린다.
            "pdf:writer_pdf_Export",
            "--outdir",
            str(out_dir),
            str(src),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=_with_local_font_env())
        except subprocess.TimeoutExpired:
            if errors is not None:
                errors.append("LibreOffice HTML→PDF 변환 시간이 초과되었습니다.")
            return None
        except Exception as exc:
            if errors is not None:
                errors.append(f"LibreOffice HTML→PDF 실행 실패: {exc}")
            return None
        if proc.returncode != 0:
            if errors is not None:
                detail = (proc.stderr or proc.stdout or "").strip()
                errors.append(f"LibreOffice HTML→PDF 실패(returncode={proc.returncode}): {detail[:500]}")
            return None
        pdfs = list(out_dir.glob("*.pdf"))
        if not pdfs:
            if errors is not None:
                errors.append("LibreOffice가 HTML→PDF 결과를 만들지 못했습니다.")
            return None
        return pdfs[0].read_bytes()


def _convert_to_pdf_with_hancom(file_bytes: bytes, file_format: str) -> bytes | None:
    return _convert_to_pdf_with_hancom_detailed(file_bytes, file_format, None)


def _save_hancom_pdf(hwp, pdf: Path, errors: list[str] | None) -> bool:
    for fmt in ("PDF", "pdf"):
        try:
            if hwp.SaveAs(str(pdf), fmt, "") and pdf.exists():
                return True
        except Exception as exc:
            if errors is not None:
                errors.append(f"한컴 SaveAs({fmt}) 실패: {exc}")

    try:
        hfos = hwp.HParameterSet.HFileOpenSave
        hwp.HAction.GetDefault("FileSaveAs_S", hfos.HSet)
        hfos.filename = str(pdf)
        hfos.Format = "PDF"
        if hwp.HAction.Execute("FileSaveAs_S", hfos.HSet) and pdf.exists():
            return True
    except Exception as exc:
        if errors is not None:
            errors.append(f"한컴 FileSaveAs_S PDF 실패: {exc}")

    try:
        hwp.Run("FileSaveAsPdf")
        if pdf.exists():
            return True
    except Exception as exc:
        if errors is not None:
            errors.append(f"한컴 FileSaveAsPdf 액션 실패: {exc}")

    if errors is not None:
        errors.append("한컴 COM이 PDF 파일을 생성하지 못했습니다.")
    return False


def _convert_to_pdf_with_hancom_detailed(
    file_bytes: bytes,
    file_format: str,
    errors: list[str] | None,
) -> bytes | None:
    if os.name != "nt" or file_format.lower() not in {"hwp", "hwpx"}:
        if errors is not None:
            errors.append("한컴 COM 변환은 Windows의 HWP/HWPX에서만 사용할 수 있습니다.")
        return None
    try:
        import win32com.client as win32  # type: ignore
    except Exception:
        if errors is not None:
            errors.append("pywin32가 없어서 한컴 COM을 사용할 수 없습니다.")
        return None

    suffix = "." + file_format.lower().lstrip(".")
    existing_hwp_pids = _running_process_ids("hwp.exe")
    with tempfile.TemporaryDirectory(prefix="figure_planner_hwp_") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / ("input" + suffix)
        pdf = tmp_dir / "output.pdf"
        src.write_bytes(file_bytes)
        hwp = None
        try:
            hwp = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
            try:
                hwp.RegisterModule("FilePathCheckDLL", "SecurityModule")
            except Exception:
                pass
            try:
                hwp.XHwpWindows.Item(0).Visible = False
            except Exception:
                pass
            opened = False
            for option in ("", "HWPX" if file_format.lower() == "hwpx" else "HWP"):
                try:
                    opened = bool(hwp.Open(str(src), option, ""))
                except Exception as exc:
                    if errors is not None:
                        errors.append(f"한컴 Open({option or 'auto'}) 실패: {exc}")
                if opened:
                    break
            if not opened:
                if errors is not None:
                    errors.append("한컴 COM이 문서를 열지 못했습니다.")
                return None
            if not _save_hancom_pdf(hwp, pdf, errors):
                return None
            return pdf.read_bytes() if pdf.exists() else None
        except Exception as exc:
            if errors is not None:
                errors.append(f"한컴 COM 변환 예외: {exc}")
            return None
        finally:
            hwp = None
            time.sleep(1.0)
            _terminate_process_ids(_running_process_ids("hwp.exe") - existing_hwp_pids)


def _convert_to_pdf_with_hancom_detailed(
    file_bytes: bytes,
    file_format: str,
    errors: list[str] | None,
) -> bytes | None:
    """More reliable Hancom COM conversion with isolated instances and retry."""
    if os.name != "nt" or file_format.lower() not in {"hwp", "hwpx"}:
        if errors is not None:
            errors.append("?쒖뺨 COM 蹂?섏? Windows??HWP/HWPX?먯꽌留??ъ슜?????덉뒿?덈떎.")
        return None
    try:
        import pythoncom  # type: ignore
        import win32com.client as win32  # type: ignore
    except Exception:
        if errors is not None:
            errors.append("pywin32媛 ?놁뼱???쒖뺨 COM???ъ슜?????놁뒿?덈떎.")
        return None

    suffix = "." + file_format.lower().lstrip(".")
    existing_hwp_pids = _running_process_ids("hwp.exe")
    with tempfile.TemporaryDirectory(prefix="figure_planner_hwp_", ignore_cleanup_errors=True) as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / ("input" + suffix)
        pdf = tmp_dir / "output.pdf"
        src.write_bytes(file_bytes)

        for _attempt in range(2):
            hwp = None
            coinit = False
            try:
                pythoncom.CoInitialize()
                coinit = True
                try:
                    hwp = win32.DispatchEx("HWPFrame.HwpObject")
                except Exception:
                    hwp = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
                try:
                    hwp.RegisterModule("FilePathCheckDLL", "SecurityModule")
                except Exception:
                    pass
                try:
                    hwp.XHwpWindows.Item(0).Visible = False
                except Exception:
                    pass

                opened = False
                for option in ("", "HWPX" if file_format.lower() == "hwpx" else "HWP"):
                    try:
                        opened = bool(hwp.Open(str(src), option, ""))
                    except Exception as exc:
                        if errors is not None:
                            errors.append(f"?쒖뺨 Open({option or 'auto'}) ?ㅽ뙣: {exc}")
                    if opened:
                        break
                if not opened:
                    if errors is not None:
                        errors.append("?쒖뺨 COM??臾몄꽌瑜??댁? 紐삵뻽?듬땲??")
                    continue

                time.sleep(1.0)
                if _save_hancom_pdf(hwp, pdf, errors) and pdf.exists():
                    return pdf.read_bytes()
            except Exception as exc:
                if errors is not None:
                    errors.append(f"?쒖뺨 COM 蹂???덉쇅: {exc}")
            finally:
                hwp = None
                if coinit:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
                time.sleep(1.0)
                _terminate_process_ids(_running_process_ids("hwp.exe") - existing_hwp_pids)

            if pdf.exists():
                try:
                    return pdf.read_bytes()
                except Exception:
                    pass
            time.sleep(1.0)
    return None


def _external_document_api_url() -> str | None:
    explicit = os.getenv("HWP_DOCUMENT_API_URL") or os.getenv("ARGO_DOCUMENT_API_URL")
    if explicit:
        return explicit.rstrip("/")
    base = os.getenv("HWP_API_BASE") or os.getenv("ARGO_API_BASE")
    if base:
        return _argodocument_url_from_base(base)
    for env_file in _hwp_api_env_files():
        if not env_file.exists():
            continue
        text = env_file.read_text(encoding="utf-8", errors="ignore")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not value or "{" in value or "}" in value:
                continue
            if key in {"HWP_DOCUMENT_API_URL", "ARGO_DOCUMENT_API_URL"}:
                return value.rstrip("/")
            if key in {"HWP_API_BASE", "ARGO_API_BASE"}:
                return _argodocument_url_from_base(value)
        match = re.search(r"https?://[^\s]+/argodocument", text)
        if match:
            url = match.group(0).strip()
            if "{" not in url and "}" not in url:
                return url
    return None


def _docsconverter_base_url() -> str | None:
    explicit = (
        os.getenv("HANCOM_DOCSCONVERTER_BASE")
        or os.getenv("DOCSCONVERTER_BASE")
        or os.getenv("HANCOM_CONVERTER_BASE")
    )
    if explicit:
        return explicit.rstrip("/")

    env_file = _hwp_api_env_file()
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8", errors="ignore")
        active_lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            active_lines.append(line)
            if "=" in line and line.split("=", 1)[0].strip() in {
                "HANCOM_DOCSCONVERTER_BASE",
                "DOCSCONVERTER_BASE",
                "HANCOM_CONVERTER_BASE",
            }:
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value and "{" not in value and "}" not in value:
                    return value.rstrip("/")
        match = re.search(r"https?://[^\s{}/]+:8101\b", "\n".join(active_lines))
        if match:
            return match.group(0).strip().rstrip("/")
    return None


def _docsconverter_module(file_format: str) -> str | None:
    fmt = file_format.lower()
    if fmt in {"hwp", "hwpx"}:
        return "hwp"
    if fmt in {"doc", "docx"}:
        return "word"
    return None


def _fetch_converter_result(data: bytes, content_type: str, errors: list[str] | None) -> bytes | None:
    if data.startswith(b"%PDF") or "application/pdf" in content_type.lower():
        return data
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                pdf_name = next((name for name in zf.namelist() if name.lower().endswith(".pdf")), "")
                if pdf_name:
                    return zf.read(pdf_name)
        except Exception as exc:
            if errors is not None:
                errors.append(f"DocsConverter ZIP 응답 해석 실패: {exc}")
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except Exception:
        if errors is not None:
            preview = data[:300].decode("utf-8", errors="replace").strip()
            errors.append(f"DocsConverter 응답이 PDF가 아닙니다(Content-Type: {content_type}): {preview}")
        return None

    candidates = []
    if isinstance(payload, dict):
        candidates.extend([
            payload.get("file_path"),
            payload.get("output_path"),
            payload.get("result_path"),
            payload.get("download_url"),
            payload.get("url"),
        ])
        result = payload.get("result")
        if isinstance(result, dict):
            candidates.extend([
                result.get("file_path"),
                result.get("output_path"),
                result.get("download_url"),
                result.get("url"),
            ])

    for candidate in (c for c in candidates if isinstance(c, str) and c.strip()):
        value = candidate.strip()
        if value.startswith(("http://", "https://")):
            try:
                with urllib.request.urlopen(value, timeout=90) as res:
                    fetched = res.read()
                    if fetched.startswith(b"%PDF"):
                        return fetched
            except Exception as exc:
                if errors is not None:
                    errors.append(f"DocsConverter 결과 URL 다운로드 실패({value}): {exc}")
        else:
            path = Path(value)
            if path.exists() and path.is_file():
                try:
                    file_bytes = path.read_bytes()
                    if file_bytes.startswith(b"%PDF"):
                        return file_bytes
                except Exception as exc:
                    if errors is not None:
                        errors.append(f"DocsConverter 결과 파일 읽기 실패({value}): {exc}")

    if errors is not None:
        errors.append(f"DocsConverter JSON 응답에서 PDF 결과를 찾지 못했습니다: {payload}")
    return None


def _convert_to_pdf_with_docsconverter_detailed(
    file_bytes: bytes,
    file_format: str,
    errors: list[str] | None,
) -> bytes | None:
    base = _docsconverter_base_url()
    module = _docsconverter_module(file_format)
    if not base or not module:
        if errors is not None:
            errors.append("DocsConverter 미들웨어 주소가 설정되지 않았거나 지원 형식이 아닙니다.")
        return None

    suffix = "." + file_format.lower().lstrip(".")
    with tempfile.TemporaryDirectory(prefix="figure_planner_docsconverter_") as tmp:
        src = Path(tmp) / ("input" + suffix)
        src.write_bytes(file_bytes)

        params = {"file_path": str(src)}
        for env_name, param_name in (
            ("HANCOM_CONVERTER_KEY", "key"),
            ("DOCSCONVERTER_KEY", "key"),
            ("HANCOM_CONVERTER_CHECKSUM", "checksum"),
            ("DOCSCONVERTER_CHECKSUM", "checksum"),
        ):
            value = os.getenv(env_name)
            if value:
                params[param_name] = value

        url = f"{base}/{module}/doc2pdf?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=120) as res:
                data = res.read()
                content_type = res.headers.get("Content-Type", "")
        except Exception as exc:
            if errors is not None:
                errors.append(f"DocsConverter 호출 실패({base}/{module}/doc2pdf): {exc}")
            return None

        pdf = _fetch_converter_result(data, content_type, errors)
        if pdf:
            return pdf

    return None


def renderer_status(file_format: str) -> dict:
    """Return non-secret renderer availability details for UI diagnostics."""
    fmt = file_format.lower()
    api_url = _external_document_api_url()
    approximate_hwpx_enabled = os.getenv("ALLOW_APPROXIMATE_HWPX_RENDER", "").lower() in {"1", "true", "yes"}
    hancom_installer = _hancom2018_installer()
    return {
        "external_api_configured": bool(api_url),
        "external_api_hint": "HWP_DOCUMENT_API_URL 또는 hwp_api_env의 /argodocument URL",
        "docsconverter_configured": bool(_docsconverter_base_url()),
        "docsconverter_hint": "HANCOM_DOCSCONVERTER_BASE=http://미들웨어_IP:8101",
        "hancom_com_available": os.name == "nt" and fmt in {"hwp", "hwpx"},
        "hancom2018_installer_path": str(hancom_installer) if hancom_installer else "",
        "hancom2018_installer_found": bool(hancom_installer),
        "libreoffice_path": _office_binary() or "",
        "libreoffice_hwpx_via_html": bool(_office_binary()) and fmt == "hwpx",
        "approximate_hwpx_render_enabled": True,
        "font_dir": str(_font_dir()),
        "font_dir_exists": _font_dir().exists(),
    }


def _image_payload_to_base64(image: str) -> str:
    if not image:
        return ""
    if image.startswith("data:image") and "," in image:
        return image.split(",", 1)[1]
    if image.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(image, timeout=30) as res:
                return base64.b64encode(res.read()).decode("ascii")
        except (urllib.error.URLError, TimeoutError, OSError):
            return ""
    return image


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def _render_hwpx_embedded_preview(
    file_bytes: bytes,
    errors: list[str] | None = None,
) -> list[RenderedPage]:
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            names = zf.namelist()
            preview_name = next(
                (name for name in names if name.lower().replace("\\", "/") == "preview/prvimage.png"),
                "",
            )
            if not preview_name:
                if errors is not None:
                    errors.append("HWPX 내부 Preview/PrvImage.png가 없습니다.")
                return []
            image_bytes = zf.read(preview_name)
    except Exception as exc:
        if errors is not None:
            errors.append(f"HWPX 내장 미리보기 추출 실패: {exc}")
        return []

    width, height = _png_dimensions(image_bytes)
    return [RenderedPage(
        page_number=1,
        image_base64=base64.b64encode(image_bytes).decode("ascii"),
        width=width,
        height=height,
        text="",
        source="hwpx-preview",
    )]


def _pages_from_external_payload(payload: dict, max_pages: int) -> list[RenderedPage]:
    raw_pages = payload.get("pages") or payload.get("page_images") or payload.get("images") or []
    pages: list[RenderedPage] = []
    for idx, item in enumerate(raw_pages[:max_pages]):
        if isinstance(item, str):
            image = item
            text = ""
            width = 0
            height = 0
        elif isinstance(item, dict):
            image = item.get("image_base64") or item.get("base64") or item.get("image") or item.get("image_url") or ""
            text = item.get("text") or ""
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
        else:
            continue
        image = _image_payload_to_base64(image)
        if image:
            pages.append(RenderedPage(idx + 1, image, width, height, text, "argodocument"))

    pdf_b64 = payload.get("pdf_base64") or payload.get("pdf") or payload.get("pdf_data")
    if not pages and isinstance(pdf_b64, str):
        if "," in pdf_b64 and pdf_b64.startswith("data:"):
            pdf_b64 = pdf_b64.split(",", 1)[1]
        return _render_pdf_bytes(base64.b64decode(pdf_b64), max_pages=max_pages)
    return pages


def _render_with_external_document_api(file_bytes: bytes, file_format: str, max_pages: int) -> list[RenderedPage]:
    return _render_with_external_document_api_detailed(file_bytes, file_format, max_pages, None)


def _render_with_external_document_api_detailed(
    file_bytes: bytes,
    file_format: str,
    max_pages: int,
    errors: list[str] | None,
) -> list[RenderedPage]:
    url = _external_document_api_url()
    if not url or file_format.lower() not in {"hwp", "hwpx"}:
        if errors is not None and file_format.lower() in {"hwp", "hwpx"}:
            errors.append("argodocument API 주소가 설정되지 않았습니다.")
        return []

    boundary = "----figure-planner-document"
    body = b"".join([
        f"--{boundary}\r\n".encode("ascii"),
        f'Content-Disposition: form-data; name="file"; filename="input.{file_format}"\r\n'.encode("ascii"),
        b"Content-Type: application/octet-stream\r\n\r\n",
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode("ascii"),
    ])
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as res:
            data = res.read()
            content_type = res.headers.get("Content-Type", "")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if errors is not None:
            errors.append(f"argodocument API 호출 실패: {exc}")
        return []

    if "application/pdf" in content_type:
        pages = _render_pdf_bytes(data, max_pages=max_pages)
        for page in pages:
            page.source = "argodocument->pdf"
        return pages
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        if errors is not None:
            errors.append(f"argodocument 응답을 해석할 수 없습니다(Content-Type: {content_type}).")
        return []
    pages = _pages_from_external_payload(payload, max_pages=max_pages)
    if not pages and errors is not None:
        errors.append("argodocument 응답에 페이지 이미지 또는 PDF 데이터가 없습니다.")
    return pages


def render_document_pages(
    file_bytes: bytes,
    file_format: str,
    max_pages: int = 12,
) -> list[RenderedPage]:
    pages, _ = render_document_pages_with_diagnostics(file_bytes, file_format, max_pages=max_pages)
    return pages


def render_document_pages_with_diagnostics(
    file_bytes: bytes,
    file_format: str,
    max_pages: int = 12,
) -> tuple[list[RenderedPage], list[str]]:
    """Render document pages as PNG data for vision models when possible."""
    errors: list[str] = []
    fmt = file_format.lower()
    if fmt == "pdf":
        try:
            return _render_pdf_bytes(file_bytes, max_pages=max_pages), errors
        except Exception as exc:
            return [], [f"PDF 렌더링 실패: {exc}"]

    if fmt in {"hwp", "hwpx", "doc", "docx"}:
        pdf_bytes = _convert_to_pdf_with_docsconverter_detailed(file_bytes, fmt, errors)
        if pdf_bytes:
            pages = _render_pdf_bytes(pdf_bytes, max_pages=max_pages)
            for page in pages:
                page.source = f"{fmt}->docsconverter-pdf"
            return pages, errors

    if fmt in {"hwp", "hwpx"}:
        pages = _render_with_external_document_api_detailed(file_bytes, fmt, max_pages=max_pages, errors=errors)
        if pages:
            return pages, errors

        pdf_bytes = _convert_to_pdf_with_hancom_detailed(file_bytes, fmt, errors)
        if pdf_bytes:
            pages = _render_pdf_bytes(pdf_bytes, max_pages=max_pages)
            for page in pages:
                page.source = f"{fmt}->hancom-pdf"
            return pages, errors

    if fmt == "hwpx":
        # LibreOffice는 HWPX import 필터가 없으므로(레거시 .hwp만 지원),
        # 추출한 본문 구조(텍스트·표·이미지)를 HTML로 재구성해 LibreOffice가 PDF로 렌더하게 한다.
        # 전체 페이지가 필요하므로 1페이지짜리 내장 미리보기보다 이 경로를 먼저 시도한다.
        pdf_bytes = _convert_hwpx_to_pdf_via_html(file_bytes, errors)
        if pdf_bytes:
            pages = _render_pdf_bytes(pdf_bytes, max_pages=max_pages)
            for page in pages:
                page.source = "hwpx->html->libreoffice-pdf"
            return pages, errors

        # HTML 변환 실패 시 HWPX 내장 미리보기(1페이지)로 폴백
        pages = _render_hwpx_embedded_preview(file_bytes, errors)
        if pages:
            errors.append("LibreOffice 변환 실패 — HWPX 내장 미리보기 이미지(1페이지)를 표시합니다.")
            return pages, errors

    # 네이티브 LibreOffice 변환은 docx/doc 및 레거시 바이너리 .hwp(MIZI 필터)만 시도.
    if fmt in {"docx", "doc", "hwp"}:
        pdf_bytes = _convert_to_pdf_with_office_detailed(file_bytes, fmt, errors)
        if pdf_bytes:
            pages = _render_pdf_bytes(pdf_bytes, max_pages=max_pages)
            for page in pages:
                page.source = f"{fmt}->pdf"
            return pages, errors

    return [], errors
