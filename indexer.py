"""
모듈 ③ Structure Indexer
각 섹션에 메타데이터 지표를 부여합니다.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from .splitter import Section


@dataclass
class IndexedSection:
    """메타데이터가 부여된 섹션"""
    section_id: str
    title: str
    depth: int
    text: str
    char_start: int
    char_end: int
    section_path: str
    length: int = 0
    numeric_density: float = 0.0
    comparison_words: int = 0
    process_words: int = 0
    space_words: int = 0
    evidence_words: int = 0
    has_table: bool = False
    is_top: bool = False
    trigger_scores: dict | None = None


_COMP_KW = re.compile(r'대비|비교|구분|vs\.?|이상|이하|차이|증감|감소|증가', re.I)
_PROC_KW = re.compile(r'단계|추진|절차|방향|체계|로드맵|과정|프로세스|흐름|순서|계획|방안', re.I)
_SPACE_KW = re.compile(r'위치|배치|면적|조망|입지|단면|평면|지도|좌표|부지|지역|공간', re.I)
_EVID_KW = re.compile(r'연구|논문|실험|따르면|결과|조사|분석|통계|데이터|수치|측정', re.I)
_NUM_TOK = re.compile(r'\d+(?:\.\d+)?(?:\s*(?:개|건|명|원|억|만|km|m|㎡|MW|톤|%|년|월|일|회|층))?', re.I)
_TBL_LINE = re.compile(r'^\|.*\|$', re.M)


def index_section(section: Section, is_top: bool = False) -> IndexedSection:
    t = section.text
    tokens = [x for x in re.split(r'\s+', t) if x]
    total = max(len(tokens), 1)
    return IndexedSection(
        section_id=section.section_id, title=section.title,
        depth=section.depth, text=t,
        char_start=section.char_start, char_end=section.char_end,
        section_path=section.section_path,
        length=len(t),
        numeric_density=len(_NUM_TOK.findall(t)) / total,
        comparison_words=len(_COMP_KW.findall(t)),
        process_words=len(_PROC_KW.findall(t)),
        space_words=len(_SPACE_KW.findall(t)),
        evidence_words=len(_EVID_KW.findall(t)),
        has_table=bool(_TBL_LINE.search(t)),
        is_top=is_top,
    )


def index_sections(sections: list[Section]) -> list[IndexedSection]:
    return [index_section(s, is_top=(i == 0 or (s.depth == 1 and i <= 2)))
            for i, s in enumerate(sections)]
