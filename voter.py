"""
모듈 ⑥ Voter (다수결 병합)
N개 LLM 결과를 (section_path, figure_type) 키 기반으로 다수결 병합
"""
from __future__ import annotations
from collections import defaultdict


def _merge_figs(figs: list[dict]) -> dict:
    """동일 키 내 figure들을 병합"""
    base = dict(figs[0])
    # confidence 평균
    confs = [f.get("confidence", 0) for f in figs]
    base["confidence"] = sum(confs) / len(confs) if confs else 0
    # suggested_content 합집합
    all_content: list[str] = []
    seen: set[str] = set()
    for f in figs:
        for item in f.get("suggested_content", []):
            if item not in seen:
                seen.add(item)
                all_content.append(item)
    base["suggested_content"] = all_content[:8]
    # final_score 평균
    scores = [f.get("final_score", 0) for f in figs]
    base["final_score"] = sum(scores) / len(scores) if scores else 0
    return base


def vote(results_n: list[dict]) -> list[dict]:
    """
    N개 LLM 결과에서 다수결 병합

    Args:
        results_n: plan_n_times의 결과 리스트

    Returns:
        투표 통과한 figure 리스트
    """
    if not results_n:
        return []

    bucket: dict[tuple, list[dict]] = defaultdict(list)
    for run in results_n:
        for fig in run.get("figures", []):
            key = (fig.get("section_path", ""), fig.get("figure_type", ""))
            bucket[key].append(fig)

    threshold = len(results_n) // 2 + 1
    voted: list[dict] = []
    fig_counter = 1

    for key, figs in bucket.items():
        if len(figs) >= threshold:
            merged = _merge_figs(figs)
            merged["vote_count"] = len(figs)
            merged["figure_id"] = f"F{fig_counter}"
            fig_counter += 1
            voted.append(merged)

    # final_score 기준 정렬
    voted.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    return voted
