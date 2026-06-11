"""Vision-based figure planning from rendered document pages."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from .document_renderer import RenderedPage
from .secret_loader import load_secrets

load_secrets()


VISION_SYSTEM = (
    "You are Figure Planner Vision, an expert Korean report editor. "
    "You inspect rendered document pages and nearby text to decide where images, "
    "figures, diagrams, maps, charts, or renderings should be inserted. "
    "Return JSON only."
)


def _extract_json(text: str) -> dict[str, Any]:
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if match:
        return json.loads(match.group(1))
    match = re.search(r"(\{[\s\S]*\})", text)
    if match:
        return json.loads(match.group(1))
    raise ValueError("Vision Planner 응답에서 JSON을 추출할 수 없습니다")


def _page_text_block(pages: list[RenderedPage]) -> str:
    blocks: list[str] = []
    for page in pages:
        text = re.sub(r"\s+", " ", page.text).strip()
        if text:
            blocks.append(f"[Page {page.page_number} text]\n{text[:1800]}")
    return "\n\n".join(blocks)


def _build_prompt(report_text: str, pages: list[RenderedPage], max_figures: int) -> str:
    page_text = _page_text_block(pages)
    return f"""
Analyze the attached rendered report pages and the extracted Korean text.

Goal:
1. Find visual insertion locations by looking at page layout, empty boxes, image placeholders, captions, table cells, large blank visual spaces, and sections that visually need a figure.
2. Read nearby text to decide what image/diagram/chart should go there.
3. Return the same Figure Planner JSON schema used by the backend.

Rules:
- Prefer page-layout evidence when a rendered page shows an explicit empty image area, missing figure, caption-only area, blank image cell, or visual placeholder.
- Also recommend implicit figures when text around the location is hard to understand without a visual.
- anchor_text must be copied from the extracted text or report_text exactly when possible.
- If a rendered page clearly shows a visual placeholder but text extraction is sparse, use the closest readable text on that page as anchor_text.
- Include page_number and bbox fields for each figure. bbox is normalized [x, y, width, height] from 0 to 1, approximate if needed.
- Keep total_figures <= {max_figures}.
- Use Korean for purpose and suggested_content, English for image_prompt_seed.
- JSON only. No markdown.

Figure types:
concept_diagram, comparison_table, flow_chart, roadmap, map_layout, floor_plan, rendering, bar_chart, line_chart, photo, cover_banner

Output schema:
{{
  "report_summary": "100자 이내 요약",
  "total_chars": 0,
  "baseline_count": 0,
  "total_figures": 0,
  "figures": [
    {{
      "figure_id": "F1",
      "page_number": 1,
      "bbox": [0.0, 0.0, 0.0, 0.0],
      "visual_evidence": "빈 이미지 박스/캡션/큰 공백/레이아웃상 후보 등",
      "section_path": "섹션 경로",
      "anchor_text": "원문에 실재하는 주변 텍스트 30~120자",
      "position_hint": "before|after|inline",
      "figure_type": "concept_diagram",
      "trigger_scores": {{"T1":0,"T2":0,"T3":0,"T4":0,"T5":0,"T6":0}},
      "cognitive_load": 0,
      "final_score": 0,
      "purpose": "이 그림이 전달할 핵심 메시지 1문장",
      "suggested_content": ["요소1","요소2","요소3","요소4","요소5"],
      "image_prompt_seed": "English prompt for image generation",
      "confidence": 0
    }}
  ]
}}

[Extracted page text]
{page_text}

[Full extracted report text, truncated]
{report_text[:9000]}
""".strip()


def _plan_with_vision_openai(report_text: str, pages: list[RenderedPage], max_figures: int) -> dict[str, Any]:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_VISION_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4.1")

    content: list[dict[str, Any]] = [{"type": "text", "text": _build_prompt(report_text, pages, max_figures)}]
    for page in pages:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{page.image_base64}",
                "detail": "high",
            },
        })

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": VISION_SYSTEM},
            {"role": "user", "content": content},
        ],
        temperature=0.2,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )
    return _extract_json(resp.choices[0].message.content or "")


def _plan_with_vision_gemini(report_text: str, pages: list[RenderedPage], max_figures: int) -> dict[str, Any]:
    import base64

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    model = os.getenv("GEMINI_VISION_MODEL") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    parts: list[Any] = [types.Part.from_text(text=_build_prompt(report_text, pages, max_figures))]
    for page in pages:
        parts.append(types.Part.from_bytes(data=base64.b64decode(page.image_base64), mime_type="image/png"))

    cfg_kwargs = dict(
        system_instruction=VISION_SYSTEM,
        temperature=0.2,
        response_mime_type="application/json",
        max_output_tokens=8192,
    )
    try:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except Exception:
        pass

    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        raise ValueError("Gemini Vision이 빈 응답을 반환했습니다")
    return _extract_json(text)


def plan_with_vision(
    report_text: str,
    pages: list[RenderedPage],
    max_figures: int = 8,
) -> dict[str, Any]:
    """Call the configured vision model with rendered pages and return figure-plan JSON."""
    if not pages:
        raise ValueError("렌더링된 페이지 이미지가 없습니다")

    load_secrets()
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider in ("gemini", "google"):
        result = _plan_with_vision_gemini(report_text, pages, max_figures)
    else:
        result = _plan_with_vision_openai(report_text, pages, max_figures)

    result.setdefault("figures", [])
    result["total_chars"] = len(report_text)
    result["baseline_count"] = min(max(1, len(report_text) // 1500), max_figures) if report_text else 0
    result["total_figures"] = len(result.get("figures", []))
    return result
