"""HWP5 binary record stream parser.

Record header layout (4 bytes, little-endian DWORD):
  bits  0 – 9   tag ID  (10 bits)
  bits 10 – 19  level   (10 bits)
  bits 20 – 31  size    (12 bits)  — 0xFFF → next DWORD = actual size

Records are read from streams such as DocInfo, BodyText/Section0, etc.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

# ── Tag-ID registry ────────────────────────────────────────────────────────
TAGS: Dict[int, str] = {
    # DocInfo stream
    0x10: "DOCUMENT_PROPERTIES",
    0x11: "ID_MAPPINGS",
    0x12: "BIN_DATA",
    0x13: "FACE_NAME",
    0x14: "BORDER_FILL",
    0x15: "CHAR_SHAPE",
    0x16: "TAB_DEF",
    0x17: "PARA_NUM_DEF",
    0x18: "BULLET_DEF",
    0x19: "MEMO_SHAPE",
    0x1A: "PARA_SHAPE",
    0x1B: "STYLE",
    0x1C: "DOC_DATA",
    0x1D: "DISTRIBUTE_DOC_DATA",
    0x1E: "COMPATIBLE_DOCUMENT",   # ← HWP5 contract item
    0x1F: "LAYOUT_COMPATIBILITY",
    0x20: "TRACK_CHANGES",
    0x21: "TRACK_CHANGE_AUTHOR",
    # Section body stream
    0x32: "PARA_HEAD",
    0x33: "PARA_TEXT",
    0x34: "PARA_CHAR_SHAPE",
    0x35: "PARA_LINE_SEG",
    0x36: "CTRL_HEADER",
    0x37: "PARA_VERTICAL_ALIGN",
    0x38: "PARA_HEAD_2",
    0x3A: "CTRL_DATA",
    0x3C: "EQEDIT",
    0x3E: "SHAPE_COMPONENT_TEXTBOX",
    0x3F: "SHAPE_COMPONENT_LINE",
    0x40: "SHAPE_COMPONENT_RECT",
    0x41: "SHAPE_COMPONENT_ELLIPSE",
    0x42: "SHAPE_COMPONENT_ARC",
    0x43: "SHAPE_COMPONENT_POLYGON",
    0x44: "SHAPE_COMPONENT_CURVE",
    0x45: "LIST_HEADER",
    0x46: "PAGE_DEF",
    0x47: "FOOTNOTE_SHAPE",
    0x48: "PAGE_BORDER_FILL",
    0x49: "SHAPE_COMPONENT",
    0x4A: "TABLE",
    0x4B: "CELL_LIST_HEADER",
    0x4C: "CELL",
    0x4D: "ROW",
    0x4F: "NUMBERING",
    0x50: "BULLET",
    0x57: "TRACK_CHANGE",
    0x58: "TRACK_CHANGE_MERGED",
    0x59: "MEMO",
    0x5A: "MEMO_LIST",
}

# Control IDs: first 4 bytes of CTRL_HEADER payload (ASCII, little-endian stored)
CTRL_IDS: Dict[bytes, str] = {
    b"secd": "SECTION_DEF",       # 섹션 정의
    b"cold": "COLUMN_DEF",        # 다단 정의
    b"tbl ": "TABLE",             # 표
    b"gso ": "DRAWING",           # 그리기 개체 (그림/도형)
    b"eqed": "EQUATION",          # 수식
    b"hdr ": "HEADER",            # 머리말
    b"ftr ": "FOOTER",            # 꼬리말
    b"fn  ": "FOOTNOTE",          # 각주
    b"en  ": "ENDNOTE",           # 미주
    b"tc  ": "INDEX_MARK",        # 찾아보기 표시
    b"fld ": "FIELD",             # 누름틀
    b"bokm": "BOOKMARK",          # 책갈피
    b"tdut": "HIDDEN_COMMENT",    # 숨김 설명
    b"idot": "MEMO",              # 메모
    b"nwno": "NEW_NUMBER",        # 새 번호 지정
    b"pgnp": "PAGE_NUM_POS",      # 쪽 번호 위치
    b"pgct": "PAGE_COUNT",        # 총 쪽 수
    b"spbp": "START_NUM",         # 시작 번호
    b"hyln": "HYPERLINK",         # 하이퍼링크
    b"tab ": "TAB",               # 탭
    b"revt": "REVISION",          # 변경 추적
    b"mequ": "MATH_EQUATION",     # 수학 수식
    b"head": "SECTION_HEAD",      # 구역 첫 머리말
}

# Records required to be present in DocInfo (tag IDs)
DOCINFO_REQUIRED_SEQUENCE = [
    0x10,   # DOCUMENT_PROPERTIES  — must be first
    0x11,   # ID_MAPPINGS          — must be second
]

# Records required to appear somewhere in DocInfo
DOCINFO_REQUIRED_ANYWHERE = {
    0x1E,   # COMPATIBLE_DOCUMENT  — missing → Hancom may reject
}


@dataclass
class HwpRecord:
    tag:    int
    level:  int
    size:   int
    data:   bytes
    offset: int          # byte offset of the record header in the stream
    stream: str = ""     # which OLE stream this came from
    children: List["HwpRecord"] = field(default_factory=list)
    parent:   Optional["HwpRecord"] = field(default=None, repr=False)

    # ── derived properties ─────────────────────────────────────────────
    @property
    def tag_name(self) -> str:
        return TAGS.get(self.tag, f"TAG_{self.tag:#05x}")

    @property
    def ctrl_id_bytes(self) -> Optional[bytes]:
        if self.tag == 0x36 and len(self.data) >= 4:
            return self.data[:4]
        return None

    @property
    def ctrl_id(self) -> Optional[str]:
        b = self.ctrl_id_bytes
        if b is None:
            return None
        return CTRL_IDS.get(b, b.decode("ascii", errors="replace"))

    @property
    def is_known_ctrl(self) -> bool:
        b = self.ctrl_id_bytes
        return b is not None and b in CTRL_IDS

    @property
    def is_unknown_ctrl(self) -> bool:
        b = self.ctrl_id_bytes
        return b is not None and b not in CTRL_IDS

    # ── display ────────────────────────────────────────────────────────
    def summary(self, show_hex: bool = False) -> str:
        parts = [f"[{self.offset:#010x}]", f"L{self.level:02d}", self.tag_name]
        if self.ctrl_id:
            parts.append(f"ctrl={self.ctrl_id!r}")
        parts.append(f"sz={self.size}")
        if show_hex and self.data:
            parts.append(self.data[:16].hex())
        return "  " * self.level + " ".join(parts)

    def to_dict(self, include_data: bool = False) -> dict:
        d: dict = {
            "tag":      self.tag,
            "tag_name": self.tag_name,
            "level":    self.level,
            "size":     self.size,
            "offset":   self.offset,
            "stream":   self.stream,
        }
        if self.ctrl_id:
            d["ctrl_id"] = self.ctrl_id
        if include_data and self.data:
            d["data_hex"] = self.data[:64].hex()
        if self.children:
            d["children"] = [c.to_dict(include_data) for c in self.children]
        return d


# ── stream parser ──────────────────────────────────────────────────────────

def iter_records(data: bytes, stream: str = "") -> Iterator[HwpRecord]:
    """Yield flat (un-threaded) records from raw stream bytes."""
    pos = 0
    n   = len(data)
    while pos + 4 <= n:
        hdr   = struct.unpack_from("<I", data, pos)[0]
        tag   =  hdr        & 0x3FF
        level = (hdr >> 10) & 0x3FF
        size  = (hdr >> 20) & 0xFFF
        hdr_end = pos + 4
        if size == 0xFFF:
            if hdr_end + 4 > n:
                break
            size = struct.unpack_from("<I", data, hdr_end)[0]
            hdr_end += 4
        payload = data[hdr_end: hdr_end + size]
        yield HwpRecord(
            tag=tag, level=level, size=size,
            data=payload, offset=pos, stream=stream,
        )
        pos = hdr_end + size


def build_tree(flat: List[HwpRecord]) -> List[HwpRecord]:
    """Thread flat record list into parent/child tree.

    Returns top-level records (level 0).
    Child level must be exactly parent_level + 1 per the HWP5 spec.
    """
    roots: List[HwpRecord] = []
    stack: List[HwpRecord] = []    # stack[i] = current parent at depth i

    for rec in flat:
        rec.children = []
        rec.parent   = None
        # trim stack to current level
        while len(stack) > rec.level:
            stack.pop()
        if stack:
            stack[-1].children.append(rec)
            rec.parent = stack[-1]
        else:
            roots.append(rec)
        if len(stack) == rec.level:
            stack.append(rec)
        else:
            # level jumped — push anyway but note the gap
            stack.append(rec)
    return roots


def parse_stream(data: bytes, stream: str = "") -> tuple[List[HwpRecord], List[HwpRecord]]:
    """Return (flat_records, root_records) for *data*."""
    flat = list(iter_records(data, stream))
    roots = build_tree(flat)
    return flat, roots
