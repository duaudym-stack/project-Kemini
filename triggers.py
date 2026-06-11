"""
모듈 ④ Trigger Scanner
6개 트리거 규칙 기반 스코어링 (T1~T6)
"""
from __future__ import annotations
import re
from .indexer import IndexedSection


def score_section(idx: IndexedSection) -> dict[str, float]:
    """§3 모듈 ④ 의 score_section 구현"""
    s = {
        'T1': 0.3 if re.search(r'\(정의\)|란\?|개요|개념|원리|정의', idx.text) else 0,
        'T2': min(1.0, idx.numeric_density * 0.4 + idx.comparison_words * 0.2),
        'T3': min(1.0, idx.space_words * 0.5),
        'T4': min(1.0, idx.process_words * 0.5),
        'T5': 0.3 if idx.evidence_words and idx.numeric_density > 0 else 0,
        'T6': 0.2 if idx.depth == 1 and idx.is_top else 0,
    }
    if idx.has_table:
        s['T2'] *= 0.3
    return s


def score_sections(indexed: list[IndexedSection]) -> list[IndexedSection]:
    """모든 섹션에 트리거 스코어 부여 후 반환"""
    for idx in indexed:
        idx.trigger_scores = score_section(idx)
    return indexed
