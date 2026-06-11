"""
모듈 ⑦ Critic Pass (자기검토)
Voter 결과를 다른 프롬프트로 재호출하여 검토
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from .secret_loader import load_secrets

load_secrets()
PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_critic_prompt(report_text: str, voted_figures: list[dict]) -> str:
    prompt = (PROMPTS_DIR / "critic.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{{REPORT_TEXT}}", report_text[:12000])
    prompt = prompt.replace("{{VOTED_FIGURES_JSON}}", json.dumps(voted_figures, ensure_ascii=False, indent=2))
    return prompt


def _extract_json(text: str) -> dict:
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r'(\{[\s\S]*\})', text)
    if m:
        return json.loads(m.group(1))
    raise ValueError("JSON 추출 실패")


def _call_llm_critic(prompt: str) -> dict:
    """temperature=0 으로 호출"""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider == "anthropic":
        from anthropic import Anthropic
        load_secrets()
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        resp = client.messages.create(
            model=model, max_tokens=4096, temperature=0.0,
            system="You are Figure Critic. Output JSON only.",
            messages=[{"role": "user", "content": prompt}],
        )
        return _extract_json(resp.content[0].text if resp.content else "")
    if provider in ("gemini", "google"):
        from google import genai
        from google.genai import types
        load_secrets()
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            system_instruction="You are Figure Critic. Output JSON only.",
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=4096,
        )
        resp = client.models.generate_content(model=model, contents=prompt, config=config)
        text = (getattr(resp, "text", None) or "").strip()
        return _extract_json(text)
    # openai (default)
    from openai import OpenAI
    load_secrets()
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL", "gpt-4.1")
    resp = client.chat.completions.create(
        model=model, temperature=0.0, max_tokens=4096,
        messages=[
            {"role": "system", "content": "You are Figure Critic. Output JSON only."},
            {"role": "user", "content": prompt},
        ],
    )
    return _extract_json(resp.choices[0].message.content or "")


def critic_pass(report_text: str, voted_figures: list[dict]) -> list[dict]:
    """
    Critic Pass: 투표 결과를 검토하여 승인/거부/신규 제안

    Args:
        report_text: 원문
        voted_figures: Voter 통과 figure 리스트

    Returns:
        critic_approved 필드가 추가된 figure 리스트
    """
    if not voted_figures:
        return []

    prompt = _load_critic_prompt(report_text, voted_figures)

    try:
        result = _call_llm_critic(prompt)
    except Exception as e:
        print(f"[Critic Pass] 호출 실패: {e}, 모든 figure 승인 처리")
        for fig in voted_figures:
            fig["critic_approved"] = True
        return voted_figures

    # 검토 결과 적용
    reviewed = {r["figure_id"]: r for r in result.get("reviewed", [])}
    approved_figs: list[dict] = []
    for fig in voted_figures:
        fid = fig.get("figure_id", "")
        review = reviewed.get(fid, {})
        approved = review.get("approved", True)
        fig["critic_approved"] = approved
        # 유형 대안 제안 시 반영
        alt_type = review.get("suggest_alternative_type")
        if alt_type and alt_type != "null" and alt_type != fig.get("figure_type"):
            fig["figure_type"] = alt_type
        if approved:
            approved_figs.append(fig)

    # 누락 항목 추가
    add_missing = result.get("add_missing", [])
    fig_counter = len(voted_figures) + 1
    for missing in add_missing:
        new_fig = {
            "figure_id": f"F{fig_counter}",
            "section_path": missing.get("section_path", ""),
            "anchor_text": missing.get("anchor_text", ""),
            "figure_type": missing.get("figure_type", "concept_diagram"),
            "purpose": missing.get("purpose", ""),
            "confidence": missing.get("confidence", 0.5),
            "trigger_scores": {"T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "T6": 0},
            "cognitive_load": 0.6,
            "final_score": 0.6,
            "vote_count": 1,
            "critic_approved": True,
            "position_hint": "after",
            "suggested_content": [],
            "image_prompt_seed": "",
        }
        approved_figs.append(new_fig)
        fig_counter += 1

    return approved_figs
