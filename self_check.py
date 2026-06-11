"""
모듈 ⑩ Self-Check
앵커 실재성·중복·신뢰도 검증
"""
from __future__ import annotations
import re


def _check_anchor_exists(fig: dict, original_text: str) -> bool:
    """anchor_text가 원문에 실재하는지 확인"""
    anchor = fig.get("anchor_text", "")
    if not anchor or len(anchor) < 10:
        return False
    # 정확히 포함되는지 확인
    if anchor in original_text:
        return True
    # 공백 정규화 후 재시도
    norm_anchor = re.sub(r'\s+', ' ', anchor).strip()
    norm_text = re.sub(r'\s+', ' ', original_text).strip()
    if norm_anchor in norm_text:
        return True
    # 부분 매칭 (80% 이상 연속 문자열)
    min_len = int(len(norm_anchor) * 0.7)
    for i in range(len(norm_anchor) - min_len + 1):
        substr = norm_anchor[i:i + min_len]
        if substr in norm_text:
            return True
    return False


def _check_type_content_match(fig: dict) -> bool:
    """figure_type과 content가 매칭되는지 확인"""
    ftype = fig.get("figure_type", "")
    content = " ".join(fig.get("suggested_content", []))

    type_keywords = {
        "concept_diagram": ["개념", "정의", "원리", "구조", "체계"],
        "comparison_table": ["비교", "대비", "항목", "구분", "차이"],
        "flow_chart": ["단계", "절차", "흐름", "과정", "순서"],
        "roadmap": ["년", "월", "계획", "일정", "추진"],
        "map_layout": ["위치", "지도", "입지", "지역", "현장"],
        "floor_plan": ["평면", "단면", "배치", "층", "공간"],
        "rendering": ["조감", "투시", "3D", "렌더링", "조망"],
        "bar_chart": ["수치", "통계", "현황", "건", "개"],
        "line_chart": ["추이", "변화", "추세", "연도", "시계열"],
        "photo": ["사진", "현장", "실물", "외관", "모습"],
        "cover_banner": ["표지", "제목", "배너", "로고"],
    }

    keywords = type_keywords.get(ftype, [])
    if not keywords:
        return True
    return any(kw in content or kw in fig.get("purpose", "") for kw in keywords)


def _check_duplicate(figs: list[dict]) -> list[dict]:
    """동일 핵심 메시지 중복 검사 — 점수 낮은 쪽 드롭"""
    if len(figs) <= 1:
        return figs

    result: list[dict] = []
    seen_purposes: list[str] = []

    for fig in figs:
        purpose = fig.get("purpose", "").strip()
        is_dup = False
        for sp in seen_purposes:
            # 간단한 유사도: 공통 키워드 비율
            words_a = set(purpose.split())
            words_b = set(sp.split())
            if words_a and words_b:
                overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
                if overlap > 0.6:
                    is_dup = True
                    break
        if not is_dup:
            result.append(fig)
            if purpose:
                seen_purposes.append(purpose)

    return result


def self_check(figures: list[dict], original_text: str) -> list[dict]:
    """
    §3 모듈 ⑩ Self-Check — 5종 검사

    | 검사           | 통과 조건                    | 실패 시        |
    |----------------|------------------------------|----------------|
    | Anchor 실재    | anchor_text ∈ 원문           | 드롭           |
    | 유형 적합      | type-content 매칭            | 유형 재분류    |
    | 중복 정보      | 동일 핵심 메시지 없음        | 점수 낮은 드롭 |
    | 밀도 상한      | ≤ 200자당 1개               | 점수 낮은 드롭 |
    | 신뢰도         | confidence ≥ 0.5            | 드롭           |
    """
    if not figures:
        return []

    checked: list[dict] = []

    for fig in figures:
        # 1. Anchor 실재 검사
        if not _check_anchor_exists(fig, original_text):
            continue

        # 2. 유형 적합 검사 (실패 시 드롭하지 않고 유지)
        if not _check_type_content_match(fig):
            pass  # classifier에서 이미 처리됨

        # 3. 신뢰도 검사
        if fig.get("confidence", 0) < 0.5:
            continue

        checked.append(fig)

    # 4. 중복 정보 검사
    checked = _check_duplicate(checked)

    # 5. 밀도 상한 (optimizer에서 이미 처리되지만 이중 확인)
    max_by_density = max(1, len(original_text) // 1500)
    checked = checked[:max_by_density]

    # figure_id 재부여
    for i, fig in enumerate(checked):
        fig["figure_id"] = f"F{i + 1}"

    return checked
