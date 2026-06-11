"""
pipeline.py — 전체 오케스트레이션
§10 스켈레톤에 따라 10단계 파이프라인 실행
"""
from __future__ import annotations
import time
from .parser import parse
from .splitter import split_sections
from .indexer import index_sections
from .triggers import score_sections
from .llm_planner import plan_n_times
from .voter import vote
from .critic import critic_pass
from .classifier import classify_types
from .optimizer import optimize
from .self_check import self_check


def _attach_document_positions(figures: list[dict], indexed, report_text: str) -> list[dict]:
    """Add char_start/char_end so optimized figures can be shown in report order."""
    section_positions = {s.section_path: (s.char_start, s.char_end) for s in indexed}
    title_positions = {s.title: (s.char_start, s.char_end) for s in indexed}

    for fig in figures:
        anchor = fig.get("anchor_text") or ""
        idx = report_text.find(anchor) if anchor else -1
        if idx >= 0:
            fig["char_start"] = idx
            fig["char_end"] = idx + len(anchor)
            continue

        section = fig.get("section_path") or ""
        start_end = section_positions.get(section)
        if not start_end:
            start_end = next((v for title, v in title_positions.items() if title and title in section), None)
        if start_end:
            fig["char_start"], fig["char_end"] = start_end

    return figures


def run(
    report_text: str,
    max_figures: int = 8,
    self_consistency_n: int = 3,
    enable_critic: bool = True,
    file_format: str | None = None,
) -> dict:
    """
    Figure Planner v2.1 메인 파이프라인

    Args:
        report_text: 보고서 텍스트 또는 파일 경로
        max_figures: 최대 그림 수 (기본 8)
        self_consistency_n: Self-Consistency 호출 횟수 (기본 3)
        enable_critic: Critic Pass 활성화 여부 (기본 True)
        file_format: 파일 포맷 명시 (None이면 자동 감지)

    Returns:
        출력 JSON 딕셔너리 (§2.2 스키마)
    """
    start_ms = time.time()

    # ① Parser
    md = parse(report_text, file_format=file_format)

    # ② Section Splitter
    sections = split_sections(md)

    # ③ Structure Indexer
    indexed = index_sections(sections)

    # ④ Trigger Scanner
    scored = score_sections(indexed)

    # 1차 컷: max(trigger_scores) >= 0.4 인 섹션만 LLM에 전달
    candidates = [
        s for s in scored
        if s.trigger_scores and max(s.trigger_scores.values()) >= 0.4
    ]

    # ⑤ LLM Planner (Self-Consistency)
    runs = plan_n_times(md, candidates, n=self_consistency_n, max_figures=max_figures)

    # ⑥ Voter (다수결 병합)
    voted = vote(runs)

    # ⑦ Critic Pass (자기검토)
    if enable_critic:
        voted = critic_pass(md, voted)

    # ⑧ Type Classifier
    typed = classify_types(voted, indexed)
    typed = _attach_document_positions(typed, indexed, md)

    # ⑨ Count Optimizer
    optimized = optimize(typed, total_chars=len(md), max_figures=max_figures)

    # ⑩ Self-Check
    final = self_check(optimized, md)

    elapsed_ms = int((time.time() - start_ms) * 1000)

    return {
        "report_summary": runs[0].get("report_summary", "") if runs else "",
        "total_chars": len(md),
        "baseline_count": min(len(md) // 1500, max_figures),
        "total_figures": len(final),
        "figures": final,
        "meta": {
            "model": "",
            "self_consistency_n": self_consistency_n,
            "critic_used": enable_critic,
            "elapsed_ms": elapsed_ms,
            "planner_errors": runs[0].get("_planner_errors", []) if runs else [],
        },
    }


def run_from_bytes(
    file_bytes: bytes,
    file_format: str = "docx",
    max_figures: int = 8,
    self_consistency_n: int = 3,
    enable_critic: bool = True,
) -> dict:
    """바이트 데이터로부터 파이프라인 실행 (파일 업로드 시)"""
    start_ms = time.time()

    md = parse(file_bytes, file_format=file_format)
    sections = split_sections(md)
    indexed = index_sections(sections)
    scored = score_sections(indexed)
    candidates = [
        s for s in scored
        if s.trigger_scores and max(s.trigger_scores.values()) >= 0.4
    ]

    runs = plan_n_times(md, candidates, n=self_consistency_n, max_figures=max_figures)
    voted = vote(runs)

    if enable_critic:
        voted = critic_pass(md, voted)

    typed = classify_types(voted, indexed)
    typed = _attach_document_positions(typed, indexed, md)
    optimized = optimize(typed, total_chars=len(md), max_figures=max_figures)
    final = self_check(optimized, md)

    elapsed_ms = int((time.time() - start_ms) * 1000)

    return {
        "report_summary": runs[0].get("report_summary", "") if runs else "",
        "total_chars": len(md),
        "baseline_count": min(len(md) // 1500, max_figures),
        "total_figures": len(final),
        "figures": final,
        "meta": {
            "model": "",
            "self_consistency_n": self_consistency_n,
            "critic_used": enable_critic,
            "elapsed_ms": elapsed_ms,
            "planner_errors": runs[0].get("_planner_errors", []) if runs else [],
        },
    }
