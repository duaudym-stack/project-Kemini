"""Extract readable text and tables from HWPX section XML files.

This module uses the dependency-free approach from the bundled
hwpx-rekian-master extractor, with extra handling for common real-world HWPX
shapes: multiple sections, namespace differences, text mixed with tables, and
tables whose cells only expose nested paragraphs.
"""
from __future__ import annotations

import html
import io
import re
import xml.etree.ElementTree as ET
import zipfile

_SECTION_RE = re.compile(r"(^|/)section(\d+)\.xml$", re.IGNORECASE)
_TEXT_TAGS = {"t", "text"}
_BREAK_TAGS = {"lineBreak", "br"}


def _local(tag: str) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean_text(text: str, *, multiline: bool = False) -> str:
    text = html.unescape(text or "").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    if multiline:
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def _direct_paragraph_text(p: ET.Element) -> str:
    """Return only the paragraph's own text, excluding nested paragraphs/tables."""
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        if node is not p and _local(node.tag) in {"p", "tbl"}:
            return
        ln = _local(node.tag)
        if ln in _TEXT_TAGS and node.text:
            parts.append(node.text)
        elif ln in _BREAK_TAGS:
            parts.append("\n")
        elif ln == "tab":
            parts.append("\t")
        for child in list(node):
            if _local(child.tag) in {"p", "tbl"}:
                continue
            walk(child)

    for child in list(p):
        walk(child)
    return _clean_text("".join(parts), multiline=True)


def _all_text(elem: ET.Element) -> str:
    parts: list[str] = []
    for node in elem.iter():
        ln = _local(node.tag)
        if ln in _TEXT_TAGS and node.text:
            parts.append(node.text)
        elif ln in _BREAK_TAGS:
            parts.append("\n")
        elif ln == "tab":
            parts.append("\t")
    return _clean_text("".join(parts))


def _children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(elem) if _local(child.tag) == name]


def _style_maps(header_bytes: bytes | None) -> dict[str, dict[str, dict]]:
    styles = {"char": {}, "para": {}}
    if not header_bytes:
        return styles
    try:
        root = ET.fromstring(header_bytes)
    except ET.ParseError:
        return styles

    for node in root.iter():
        ln = _local(node.tag)
        if ln == "charPr":
            item_id = node.attrib.get("id")
            if item_id is None:
                continue
            height = int(node.attrib.get("height", "0") or 0)
            styles["char"][item_id] = {
                "fontSizePt": round(height / 100, 2) if height > 0 else None,
                "bold": any(_local(child.tag) == "bold" for child in list(node)),
                "italic": any(_local(child.tag) == "italic" for child in list(node)),
                "color": node.attrib.get("textColor") or "",
            }
        elif ln == "paraPr":
            item_id = node.attrib.get("id")
            if item_id is None:
                continue
            align = next((child for child in list(node) if _local(child.tag) == "align"), None)
            margin = next((child for child in list(node) if _local(child.tag) == "margin"), None)
            styles["para"][item_id] = {
                "textAlign": (align.attrib.get("horizontal", "") if align is not None else "").lower(),
                "marginTopPt": round(int(margin.attrib.get("prev", "0") or 0) / 100, 2) if margin is not None else 0,
                "marginBottomPt": round(int(margin.attrib.get("next", "0") or 0) / 100, 2) if margin is not None else 0,
            }
    return styles


def _paragraph_style(p: ET.Element, styles: dict[str, dict[str, dict]]) -> dict:
    para_id = p.attrib.get("paraPrIDRef", "0")
    run = next((node for node in p.iter() if _local(node.tag) == "run"), None)
    char_id = run.attrib.get("charPrIDRef", "0") if run is not None else "0"
    return {
        "charPrIDRef": char_id,
        "paraPrIDRef": para_id,
        "char": styles.get("char", {}).get(char_id, styles.get("char", {}).get("0", {})),
        "para": styles.get("para", {}).get(para_id, styles.get("para", {}).get("0", {})),
    }


def _cell_text(tc: ET.Element) -> str:
    paragraphs = [p for p in tc.iter() if _local(p.tag) == "p"]
    if not paragraphs:
        return _all_text(tc)
    parts = [_direct_paragraph_text(p) for p in paragraphs]
    return _clean_text(" ".join(part for part in parts if part))


def _table_rows(tbl: ET.Element) -> list[list[str]]:
    """Extract table rows, skipping phantom (merged-away) cells."""
    direct_rows = _children(tbl, "tr")
    if not direct_rows:
        cells = [_cell_text(tc) for tc in tbl.iter() if _local(tc.tag) == "tc"]
        return [[cell] for cell in cells if cell]

    all_cells: list[dict] = []
    for r_idx, tr in enumerate(direct_rows):
        for tc in _children(tr, "tc"):
            addr = next((c for c in list(tc) if _local(c.tag) == "cellAddr"), None)
            span = next((c for c in list(tc) if _local(c.tag) == "cellSpan"), None)
            row_addr = int(addr.attrib.get("rowAddr", r_idx) if addr is not None else r_idx)
            col_addr = int(addr.attrib.get("colAddr", 0) if addr is not None else 0)
            colspan = max(1, int(span.attrib.get("colSpan", 1) if span is not None else 1))
            rowspan = max(1, int(span.attrib.get("rowSpan", 1) if span is not None else 1))
            all_cells.append({
                "row": row_addr, "col": col_addr,
                "colspan": colspan, "rowspan": rowspan,
                "text": _cell_text(tc),
            })

    covered: set[tuple[int, int]] = set()
    for c in all_cells:
        for dr in range(c["rowspan"]):
            for dc in range(c["colspan"]):
                if dr > 0 or dc > 0:
                    covered.add((c["row"] + dr, c["col"] + dc))

    row_map: dict[int, list[str]] = {}
    for c in all_cells:
        if (c["row"], c["col"]) in covered:
            continue
        row_map.setdefault(c["row"], []).append(c["text"])

    return [row_map[k] for k in sorted(row_map) if row_map[k] and any(row_map[k])]


def _has_descendant_table(p: ET.Element) -> bool:
    return any(_local(node.tag) == "tbl" for node in p.iter() if node is not p)


def _extract_blocks(elem: ET.Element, styles: dict[str, dict[str, dict]] | None = None) -> list[dict]:
    """Extract text/table/image blocks in document order."""
    blocks: list[dict] = []
    for child in list(elem):
        ln = _local(child.tag)
        if ln == "tbl":
            blocks.append({"kind": "table", "rows": _table_rows(child)})
        elif ln == "p":
            text = _direct_paragraph_text(child)
            if text:
                style = _paragraph_style(child, styles or {})
                for line in (line.strip() for line in text.split("\n")):
                    if line:
                        blocks.append({"kind": "text", "text": line, "hwpStyle": style})
            # Detect embedded images in this paragraph (inside ctrl/pic/img elements)
            seen_bin_ids: set[str] = set()
            for node in child.iter():
                if node is child:
                    continue
                if _local(node.tag) == "img":
                    # HWPX uses binaryItemIDRef; some variants use binItemIDRef
                    bin_id = (
                        node.attrib.get("binaryItemIDRef", "")
                        or node.attrib.get("binItemIDRef", "")
                    )
                    href = node.attrib.get("href", "")
                    if bin_id and bin_id not in seen_bin_ids:
                        seen_bin_ids.add(bin_id)
                        blocks.append({"kind": "image", "binItemIDRef": bin_id, "href": href})
            if _has_descendant_table(child):
                blocks.extend(_extract_blocks(child, styles))
        else:
            blocks.extend(_extract_blocks(child, styles))
    return blocks


def _section_names(zf: zipfile.ZipFile) -> list[str]:
    names = [
        name
        for name in zf.namelist()
        if name.lower().endswith(".xml") and _SECTION_RE.search(name.replace("\\", "/"))
    ]
    return sorted(names, key=lambda name: int(_SECTION_RE.search(name.replace("\\", "/")).group(2)))


def extract_sections(file_bytes: bytes) -> dict:
    """Return section-by-section text/table JSON from HWPX bytes."""
    sections: list[dict] = []
    paragraph_count = 0
    table_count = 0
    total_chars = 0

    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        names = _section_names(zf)
        if not names:
            raise ValueError("Contents/section*.xml files were not found. The file may not be a valid HWPX.")
        header_name = next((name for name in zf.namelist() if name.replace("\\", "/").lower() == "contents/header.xml"), None)
        styles = _style_maps(zf.read(header_name) if header_name else None)

        for idx, name in enumerate(names):
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError as exc:
                sections.append({
                    "section_index": idx,
                    "name": name,
                    "block_count": 0,
                    "blocks": [],
                    "error": f"XML parse failed: {exc}",
                })
                continue

            sec = root if _local(root.tag) == "sec" else next(
                (node for node in root.iter() if _local(node.tag) == "sec"),
                root,
            )
            blocks = _extract_blocks(sec, styles)
            for block in blocks:
                if block["kind"] == "text":
                    paragraph_count += 1
                    total_chars += len(block["text"])
                elif block["kind"] == "table":
                    table_count += 1
                    for row in block.get("rows", []):
                        total_chars += sum(len(cell) for cell in row)
            sections.append({
                "section_index": idx,
                "name": name,
                "block_count": len(blocks),
                "blocks": blocks,
            })

    return {
        "source": "backend",
        "applied_skill": "hwpx-rekian-master/hwpx (section*.xml text/table extract)",
        "section_count": len(sections),
        "paragraph_count": paragraph_count,
        "table_count": table_count,
        "total_chars": total_chars,
        "sections": sections,
    }


def blocks_to_markdown(data: dict) -> str:
    """Convert extract_sections output into compact Markdown for the planner."""
    out: list[str] = []
    for section in data.get("sections", []):
        for block in section.get("blocks", []):
            if block.get("kind") == "text":
                text = _clean_text(str(block.get("text", "")))
                if text:
                    out.append(text)
                    out.append("")
            elif block.get("kind") == "table":
                rows = block.get("rows") or []
                rows = [[_clean_text(str(cell)) for cell in row] for row in rows if row]
                if not rows:
                    continue
                cols = max(len(row) for row in rows)
                padded = [row + [""] * (cols - len(row)) for row in rows]
                out.append("| " + " | ".join(padded[0]) + " |")
                out.append("| " + " | ".join(["---"] * cols) + " |")
                for row in padded[1:]:
                    out.append("| " + " | ".join(row) + " |")
                out.append("")
    return "\n".join(out).strip()
