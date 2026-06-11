"""
모듈 ⑧ Type Classifier
결정 트리 기반 figure_type 확정 (§3 모듈 ⑧)
"""
from __future__ import annotations
import re
from .indexer import IndexedSection


def _dominant_trigger(scores: dict[str, float]) -> str:
    """가장 높은 트리거 코드 반환"""
    if not scores:
        return "T1"
    return max(scores, key=lambda k: scores[k])


def classify_type(fig: dict, indexed_sections: list[IndexedSection]) -> str:
    """
    §3 결정 트리에 따라 figure_type 확정

    IF T6 우세 → cover_banner
    ELIF T3 우세
       IF "지도/입지" keyword → map_layout
       ELIF "단면/평면/배치" → floor_plan
       ELIF "조망/투시/조감" → rendering
    ELIF T4 우세
       IF 연도/월 토큰 ≥3 → roadmap
       ELSE → flow_chart
    ELIF T2 우세
       IF 항목≥3 × 속성≥3 → comparison_table
       ELIF 수치 ≥5 → bar_chart
    ELIF T1 우세 → concept_diagram
    ELIF T5 우세
       IF 시계열 수치 → line_chart
       ELSE → photo
    ELSE → concept_diagram
    """
    scores = fig.get("trigger_scores", {})
    text = fig.get("anchor_text", "") + " " + " ".join(fig.get("suggested_content", []))

    # 섹션 텍스트도 참조
    section_path = fig.get("section_path", "")
    section_text = ""
    for idx in indexed_sections:
        if idx.section_path == section_path or idx.title in section_path:
            section_text = idx.text
            break
    full_text = text + " " + section_text

    dom = _dominant_trigger(scores)

    if dom == "T6":
        return "cover_banner"
    elif dom == "T3":
        if re.search(r'지도|입지|위치|좌표', full_text):
            return "map_layout"
        elif re.search(r'단면|평면|배치|층', full_text):
            return "floor_plan"
        elif re.search(r'조망|투시|조감|렌더링|3D', full_text):
            return "rendering"
        return "map_layout"
    elif dom == "T4":
        year_month = re.findall(r'\d{4}년|\d{1,2}월|\d{4}', full_text)
        if len(year_month) >= 3:
            return "roadmap"
        return "flow_chart"
    elif dom == "T2":
        nums = re.findall(r'\d+(?:\.\d+)?', full_text)
        if len(nums) >= 5:
            return "bar_chart"
        return "comparison_table"
    elif dom == "T1":
        return "concept_diagram"
    elif dom == "T5":
        if re.search(r'추이|추세|변화|연도별|월별|시계열', full_text):
            return "line_chart"
        return "photo"
    return "concept_diagram"


def classify_types(figures: list[dict], indexed: list[IndexedSection]) -> list[dict]:
    """모든 figure의 유형을 결정 트리로 재분류"""
    for fig in figures:
        determined = classify_type(fig, indexed)
        # LLM이 이미 적절한 유형을 제안한 경우 유지, 그렇지 않으면 재분류
        if not fig.get("figure_type"):
            fig["figure_type"] = determined
    return figures
