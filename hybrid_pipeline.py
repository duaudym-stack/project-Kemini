"""Hybrid visual + text Figure Planner pipeline."""
from __future__ import annotations

import time

from .classifier import classify_types
from .document_renderer import render_document_pages
from .indexer import index_sections
from .optimizer import optimize
from .parser import parse
from .pipeline import _attach_document_positions, run_from_bytes
from .self_check import self_check
from .splitter import split_sections
from .vision_planner import plan_with_vision


def _normalize_vision_figures(figures: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for idx, fig in enumerate(figures):
        item = dict(fig)
        item.setdefault("figure_id", f"F{idx + 1}")
        item.setdefault("position_hint", "after")
        item.setdefault("figure_type", "concept_diagram")
        item.setdefault("trigger_scores", {"T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "T6": 0})
        item.setdefault("cognitive_load", 0.7)
        item.setdefault("final_score", item.get("confidence", 0.7))
        item.setdefault("confidence", 0.7)
        item.setdefault("suggested_content", [])
        item.setdefault("image_prompt_seed", "")
        item.setdefault("vote_count", 1)
        item.setdefault("critic_approved", True)
        normalized.append(item)
    return normalized


def run_from_bytes_hybrid(
    file_bytes: bytes,
    file_format: str = "docx",
    max_figures: int = 8,
    self_consistency_n: int = 3,
    enable_critic: bool = True,
    max_vision_pages: int = 8,
) -> dict:
    """Run visual-page analysis first, with text-only planner fallback."""
    start_ms = time.time()
    md = parse(file_bytes, file_format=file_format)
    render_errors: list[str] = []
    vision_errors: list[str] = []

    try:
        pages = render_document_pages(file_bytes, file_format=file_format, max_pages=max_vision_pages)
    except Exception as exc:
        pages = []
        render_errors.append(str(exc))

    if not pages:
        result = run_from_bytes(
            file_bytes=file_bytes,
            file_format=file_format,
            max_figures=max_figures,
            self_consistency_n=self_consistency_n,
            enable_critic=enable_critic,
        )
        fallback_reason = (
            "HWPX는 텍스트/XML 파서로 읽었습니다. 페이지 이미지 기반 위치 분석은 PDF 또는 LibreOffice 변환이 가능한 문서에서 사용됩니다."
            if file_format.lower() == "hwpx"
            else "문서 페이지 이미지를 만들 수 없어 텍스트 기반 분석으로 전환했습니다."
        )
        result.setdefault("meta", {})
        result["meta"].update({
            "mode": "text_fallback",
            "rendered_pages": 0,
            "fallback_reason": fallback_reason,
            "render_errors": render_errors,
            "vision_errors": vision_errors,
        })
        return result

    try:
        raw = plan_with_vision(md, pages, max_figures=max_figures)
    except Exception as exc:
        vision_errors.append(str(exc))
        result = run_from_bytes(
            file_bytes=file_bytes,
            file_format=file_format,
            max_figures=max_figures,
            self_consistency_n=self_consistency_n,
            enable_critic=enable_critic,
        )
        result.setdefault("meta", {})
        result["meta"].update({
            "mode": "vision_failed_text_fallback",
            "rendered_pages": len(pages),
            "render_source": pages[0].source if pages else "",
            "render_errors": render_errors,
            "vision_errors": vision_errors,
        })
        return result

    sections = split_sections(md)
    indexed = index_sections(sections)
    figures = _normalize_vision_figures(raw.get("figures", []))
    figures = classify_types(figures, indexed)
    figures = _attach_document_positions(figures, indexed, md)
    optimized = optimize(figures, total_chars=len(md), max_figures=max_figures)
    final = self_check(optimized, md)

    elapsed_ms = int((time.time() - start_ms) * 1000)
    return {
        "report_summary": raw.get("report_summary", ""),
        "total_chars": len(md),
        "baseline_count": min(max(1, len(md) // 1500), max_figures) if md else 0,
        "total_figures": len(final),
        "figures": final,
        "meta": {
            "mode": "hybrid_vision_text",
            "model": "",
            "self_consistency_n": self_consistency_n,
            "critic_used": False,
            "elapsed_ms": elapsed_ms,
            "rendered_pages": len(pages),
            "render_source": pages[0].source if pages else "",
            "render_errors": render_errors,
            "vision_errors": vision_errors,
        },
    }
