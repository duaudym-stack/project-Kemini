"""
모듈 ① Parser
hwpx/docx/md/txt → 정규화 Markdown 문자열 + 원본 char_offset 매핑
"""
from __future__ import annotations

import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple


class ParseResult(NamedTuple):
    """파서 결과: 정규화된 마크다운 + 원본 오프셋 매핑"""
    markdown: str
    char_offsets: list[tuple[int, int]]  # (md_start, original_start) 매핑


# ────────────────────────────────────────────
#  공통 유틸
# ────────────────────────────────────────────

def _detect_format(path_or_text: str) -> str:
    """파일 경로 또는 텍스트 문자열로부터 포맷 판별"""
    if os.path.isfile(path_or_text):
        ext = Path(path_or_text).suffix.lower()
        format_map = {
            '.hwpx': 'hwpx',
            '.hwp': 'hwp',
            '.docx': 'docx',
            '.pdf': 'pdf',
            '.md': 'md',
            '.txt': 'txt',
        }
        return format_map.get(ext, 'txt')
    return 'txt'


def _normalize_whitespace(text: str) -> str:
    """연속 공백/줄바꿈 정리"""
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ────────────────────────────────────────────
#  hwpx 파서 (ZIP 기반 XML 직접 파싱)
# ────────────────────────────────────────────

# hwpx XML 네임스페이스
_HWPX_NS = {
    'hp': 'http://www.hancom.co.kr/hwpml/2011/paragraph',
    'hs': 'http://www.hancom.co.kr/hwpml/2011/section',
    'hc': 'http://www.hancom.co.kr/hwpml/2011/content',
    'ha': 'http://www.hancom.co.kr/hwpml/2011/app',
    'hp10': 'http://www.hancom.co.kr/schema/hwpml/2018/paragraph',
    'hs10': 'http://www.hancom.co.kr/schema/hwpml/2018/section',
    'hc10': 'http://www.hancom.co.kr/schema/hwpml/2018/content',
    'ha10': 'http://www.hancom.co.kr/schema/hwpml/2018/app',
}


def _parse_hwpx_section_xml(xml_bytes: bytes) -> str:
    """hwpx 섹션 XML에서 텍스트 추출하여 Markdown으로 변환"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""

    lines: list[str] = []

    # hwpx 구조: section > p (paragraph) > run > t (text)
    # 네임스페이스가 버전마다 다를 수 있으므로 유연하게 처리
    for elem in root.iter():
        tag = elem.tag
        # 네임스페이스 제거하여 로컬 태그명 추출
        local_tag = tag.split('}')[-1] if '}' in tag else tag

        if local_tag == 'p':
            # 단락(paragraph) 내의 모든 텍스트 수집
            para_texts: list[str] = []
            for child in elem.iter():
                child_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if child_local in ('t', 'text'):
                    if child.text:
                        para_texts.append(child.text)
                elif child_local in ('tab',):
                    para_texts.append('\t')

            if para_texts:
                line = ''.join(para_texts).strip()
                if line:
                    lines.append(line)

    return '\n'.join(lines)


def _parse_hwpx(file_path: str) -> str:
    """hwpx 파일 파싱 → Markdown 변환
    
    hwpx는 ZIP 아카이브로, Contents/section0.xml ~ sectionN.xml에
    본문이 저장됩니다.
    """
    markdown_parts: list[str] = []

    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            # 섹션 파일 목록 추출 (Contents/section0.xml, section1.xml, ...)
            section_files = sorted([
                name for name in zf.namelist()
                if re.match(r'Contents/section\d+\.xml', name, re.IGNORECASE)
            ])

            if not section_files:
                # 대체 경로 시도: Contents/ 없이 section*.xml
                section_files = sorted([
                    name for name in zf.namelist()
                    if re.match(r'section\d+\.xml', name.split('/')[-1], re.IGNORECASE)
                ])

            if not section_files:
                # 모든 XML 파일에서 텍스트 추출 시도
                section_files = sorted([
                    name for name in zf.namelist()
                    if name.endswith('.xml') and 'section' in name.lower()
                ])

            for sf in section_files:
                xml_bytes = zf.read(sf)
                section_text = _parse_hwpx_section_xml(xml_bytes)
                if section_text:
                    markdown_parts.append(section_text)

    except (zipfile.BadZipFile, FileNotFoundError) as e:
        raise ValueError(f"hwpx 파일을 읽을 수 없습니다: {e}")

    if not markdown_parts:
        raise ValueError("hwpx 파일에서 텍스트를 추출할 수 없습니다. 파일 구조를 확인해주세요.")

    return '\n\n'.join(markdown_parts)


def _parse_hwpx_from_bytes(file_bytes: bytes) -> str:
    """Parse HWPX bytes into Markdown text."""
    try:
        from .hwpx_cli_bridge import extract_markdown_with_hwpx_cli
    except ImportError:
        from hwpx_cli_bridge import extract_markdown_with_hwpx_cli

    cli_markdown = extract_markdown_with_hwpx_cli(file_bytes)
    if cli_markdown:
        return cli_markdown

    try:
        from .section_extract import blocks_to_markdown, extract_sections
    except ImportError:
        from section_extract import blocks_to_markdown, extract_sections

    try:
        data = extract_sections(file_bytes)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid HWPX ZIP data: {exc}") from exc

    markdown = blocks_to_markdown(data)
    if not markdown:
        raise ValueError("No readable text was extracted from the HWPX document.")
    return markdown


def _parse_hwpx(file_path: str) -> str:
    """Parse an HWPX file into Markdown text."""
    try:
        with open(file_path, "rb") as f:
            return _parse_hwpx_from_bytes(f.read())
    except FileNotFoundError as exc:
        raise ValueError(f"HWPX file could not be read: {exc}") from exc


def _parse_hwp_from_bytes(file_bytes: bytes) -> str:
    """Best-effort legacy HWP text extraction via available document converters.

    Hancom COM (HWPFrame.HwpObject) is intentionally skipped here because it
    opens the full Hancom HWP application window.  It is still used for the
    preview/render endpoint where a visible window is acceptable.
    """
    try:
        from .hwpx_cli_bridge import extract_markdown_from_hwp_with_hwpx_cli
    except ImportError:
        from hwpx_cli_bridge import extract_markdown_from_hwp_with_hwpx_cli

    cli_markdown = extract_markdown_from_hwp_with_hwpx_cli(file_bytes)
    if cli_markdown:
        return cli_markdown

    # olefile 기반 OLE 직접 파싱 — 한글 프로그램 창을 열지 않음
    try:
        import tempfile as _tmpfile
        from . import hwpx_read as _hwpx_read
        with _tmpfile.NamedTemporaryFile(suffix=".hwp", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            doc = _hwpx_read.read_hwp(tmp_path)
            text = doc.get("text", "").strip()
            if text:
                return text
        finally:
            import os as _os
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass

    # LibreOffice headless PDF 변환 (창 없음)
    try:
        from .document_renderer import _convert_to_pdf_with_office
    except ImportError:
        from document_renderer import _convert_to_pdf_with_office

    pdf_bytes = _convert_to_pdf_with_office(file_bytes, "hwp")
    if pdf_bytes:
        text = _parse_pdf_from_bytes(pdf_bytes)
        if text.strip():
            return text

    raise ValueError("Legacy .hwp text extraction failed: hwpx_cli, olefile, and LibreOffice all returned no text")


def _parse_hwp(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return _parse_hwp_from_bytes(f.read())


# ────────────────────────────────────────────
#  docx 파서
# ────────────────────────────────────────────

def _parse_docx(file_path: str) -> str:
    """docx 파일을 Markdown으로 변환 (헤딩 스타일 인식)"""
    try:
        from docx import Document
    except ImportError:
        raise ImportError("python-docx 패키지가 필요합니다: pip install python-docx")

    doc = Document(file_path)
    lines: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            lines.append('')
            continue

        style_name = (para.style.name or '').lower()

        # 헤딩 스타일 → Markdown 헤딩
        if 'heading 1' in style_name or style_name == 'title':
            lines.append(f'# {text}')
        elif 'heading 2' in style_name:
            lines.append(f'## {text}')
        elif 'heading 3' in style_name:
            lines.append(f'### {text}')
        elif 'heading 4' in style_name:
            lines.append(f'#### {text}')
        elif 'list' in style_name:
            lines.append(f'- {text}')
        else:
            lines.append(text)

    # 표(Table) 추출
    for table in doc.tables:
        lines.append('')
        for i, row in enumerate(table.rows):
            cells = [cell.text.strip().replace('\n', ' ') for cell in row.cells]
            lines.append('| ' + ' | '.join(cells) + ' |')
            if i == 0:
                lines.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
        lines.append('')

    return '\n'.join(lines)


def _parse_docx_from_bytes(file_bytes: bytes) -> str:
    """바이트 데이터로부터 docx 파싱 (업로드 시)"""
    try:
        from docx import Document
    except ImportError:
        raise ImportError("python-docx 패키지가 필요합니다: pip install python-docx")

    doc = Document(io.BytesIO(file_bytes))
    lines: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            lines.append('')
            continue

        style_name = (para.style.name or '').lower()

        if 'heading 1' in style_name or style_name == 'title':
            lines.append(f'# {text}')
        elif 'heading 2' in style_name:
            lines.append(f'## {text}')
        elif 'heading 3' in style_name:
            lines.append(f'### {text}')
        elif 'heading 4' in style_name:
            lines.append(f'#### {text}')
        elif 'list' in style_name:
            lines.append(f'- {text}')
        else:
            lines.append(text)

    for table in doc.tables:
        lines.append('')
        for i, row in enumerate(table.rows):
            cells = [cell.text.strip().replace('\n', ' ') for cell in row.cells]
            lines.append('| ' + ' | '.join(cells) + ' |')
            if i == 0:
                lines.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
        lines.append('')

    return '\n'.join(lines)


# ────────────────────────────────────────────
#  pdf 파서
# ────────────────────────────────────────────

def _parse_pdf_reader(reader) -> str:
    """pypdf reader에서 페이지별 텍스트 추출"""
    lines: list[str] = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        if text:
            lines.append(text)
        if i < len(reader.pages) - 1:
            lines.append("")
    return '\n'.join(lines)


def _parse_pdf(file_path: str) -> str:
    """pdf 파일을 텍스트 Markdown으로 변환"""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ImportError("pypdf 패키지가 필요합니다: pip install pypdf")

    reader = PdfReader(file_path)
    return _parse_pdf_reader(reader)


def _parse_pdf_from_bytes(file_bytes: bytes) -> str:
    """바이트 데이터로부터 pdf 텍스트 추출"""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ImportError("pypdf 패키지가 필요합니다: pip install pypdf")

    reader = PdfReader(io.BytesIO(file_bytes))
    return _parse_pdf_reader(reader)


# ────────────────────────────────────────────
#  md / txt 파서
# ────────────────────────────────────────────

def _parse_text(file_path: str) -> str:
    """md/txt 파일 → UTF-8 강제 읽기"""
    encodings = ['utf-8', 'utf-8-sig', 'cp949', 'euc-kr']
    for enc in encodings:
        try:
            with open(file_path, 'r', encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    # 최후 수단
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


# ────────────────────────────────────────────
#  메인 parse 함수
# ────────────────────────────────────────────

def parse(report_input: str | bytes, file_format: str | None = None) -> str:
    """
    보고서 입력을 정규화된 Markdown 문자열로 변환합니다.

    Args:
        report_input: 파일 경로(str), 텍스트 내용(str), 또는 파일 바이트(bytes)
        file_format: 명시적 포맷 지정 ('hwpx', 'docx', 'md', 'txt')
                     None이면 자동 감지

    Returns:
        정규화된 Markdown 문자열
    """
    # bytes 입력 (업로드 시)
    if isinstance(report_input, bytes):
        fmt = file_format or 'docx'
        if fmt == 'hwpx':
            return _normalize_whitespace(_parse_hwpx_from_bytes(report_input))
        if fmt == 'hwp':
            return _normalize_whitespace(_parse_hwp_from_bytes(report_input))
        if fmt == 'docx':
            raw = _parse_docx_from_bytes(report_input)
        elif fmt == 'hwpx':
            # hwpx bytes → 임시 처리
            tmp_buf = io.BytesIO(report_input)
            try:
                with zipfile.ZipFile(tmp_buf, 'r') as zf:
                    section_files = sorted([
                        name for name in zf.namelist()
                        if re.match(r'Contents/section\d+\.xml', name, re.IGNORECASE)
                    ])
                    if not section_files:
                        section_files = sorted([
                            name for name in zf.namelist()
                            if 'section' in name.lower() and name.endswith('.xml')
                        ])
                    parts = []
                    for sf in section_files:
                        text = _parse_hwpx_section_xml(zf.read(sf))
                        if text:
                            parts.append(text)
                    raw = '\n\n'.join(parts)
            except zipfile.BadZipFile:
                raise ValueError("hwpx 바이트 데이터를 파싱할 수 없습니다.")
        elif fmt == 'pdf':
            raw = _parse_pdf_from_bytes(report_input)
        else:
            raw = report_input.decode('utf-8', errors='replace')
        return _normalize_whitespace(raw)

    # 문자열 입력
    if isinstance(report_input, str):
        # 파일 경로인 경우
        if os.path.isfile(report_input):
            fmt = file_format or _detect_format(report_input)
            if fmt == 'hwpx':
                raw = _parse_hwpx(report_input)
            elif fmt == 'hwp':
                raw = _parse_hwp(report_input)
            elif fmt == 'docx':
                raw = _parse_docx(report_input)
            elif fmt == 'pdf':
                raw = _parse_pdf(report_input)
            else:
                raw = _parse_text(report_input)
            return _normalize_whitespace(raw)

        # 텍스트 내용 자체인 경우
        return _normalize_whitespace(report_input)

    raise TypeError(f"지원하지 않는 입력 타입: {type(report_input)}")
