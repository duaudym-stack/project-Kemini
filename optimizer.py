"""
모듈 ⑨ Count Optimizer
그림 개수·밀도 최적화
"""
from __future__ import annotations


def _merge_adjacent_in_same_section(figs: list[dict]) -> list[dict]:
    """동일 섹션 내 인접 figure 병합"""
    if len(figs) <= 1:
        return figs

    merged: list[dict] = [figs[0]]
    for fig in figs[1:]:
        prev = merged[-1]
        if (prev.get("section_path") == fig.get("section_path")
                and prev.get("figure_type") == fig.get("figure_type")):
            # 동일 섹션·유형이면 높은 점수 유지, content 합산
            if fig.get("final_score", 0) > prev.get("final_score", 0):
                merged[-1] = fig
            prev_content = set(prev.get("suggested_content", []))
            for item in fig.get("suggested_content", []):
                if item not in prev_content:
                    merged[-1].setdefault("suggested_content", []).append(item)
        else:
            merged.append(fig)
    return merged


def _enforce_density_cap(figs: list[dict], total_chars: int, cap_per_chars: int = 1500) -> list[dict]:
    """밀도 상한: total_chars / cap_per_chars 개 이하"""
    max_by_density = max(1, total_chars // cap_per_chars)
    return figs[:max_by_density]


def _sort_by_document_order(figs: list[dict]) -> list[dict]:
    """Keep final recommendations in report order instead of score order."""
    return sorted(figs, key=lambda x: x.get("char_start", 10**12))


def optimize(figs: list[dict], total_chars: int, max_figures: int = 8) -> list[dict]:
    """
    §3 모듈 ⑨ Count Optimizer

    Args:
        figs: figure 리스트
        total_chars: 전체 글자 수
        max_figures: 최대 그림 수

    Returns:
        최적화된 figure 리스트
    """
    if not figs:
        return []

    baseline = min(total_chars // 1500, max_figures)
    baseline = max(baseline, 1)  # 최소 1개

    # final_score 기준 정렬
    figs.sort(key=lambda x: x.get("final_score", 0), reverse=True)

    # 상위 baseline개 선택
    selected = figs[:baseline]

    # 동일 섹션 병합
    selected = _merge_adjacent_in_same_section(selected)

    # 밀도 상한 적용
    selected = _enforce_density_cap(selected, total_chars, cap_per_chars=1500)
    selected = _sort_by_document_order(selected)

    # figure_id 재부여
    for i, fig in enumerate(selected):
        fig["figure_id"] = f"F{i + 1}"

    return selected
