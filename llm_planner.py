"""
모듈 ⑤ LLM Planner (Self-Consistency)
동일 입력을 N회 호출하여 N개의 결과 배열 수집
"""
from __future__ import annotations
import json
import os
import re
import time
from pathlib import Path
from .secret_loader import load_secrets

load_secrets()

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _build_master_prompt(report_text: str, max_figures: int) -> str:
    prompt = _load_prompt("master.md")
    prompt = prompt.replace("{{REPORT_TEXT}}", report_text[:12000])
    prompt = prompt.replace("{{MAX_FIGURES}}", str(max_figures))
    # Few-shot 예시 삽입
    try:
        few_shot = _load_prompt("few_shot.md")
        prompt = prompt + "\n\n" + few_shot
    except FileNotFoundError:
        pass
    return prompt


def _extract_json(text: str) -> dict:
    """LLM 응답에서 JSON 추출"""
    # ```json ... ``` 블록 추출
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if m:
        return json.loads(m.group(1))
    # 순수 JSON
    m = re.search(r'(\{[\s\S]*\})', text)
    if m:
        return json.loads(m.group(1))
    raise ValueError("LLM 응답에서 JSON을 추출할 수 없습니다")


def _call_openai(prompt: str, temperature: float = 0.7) -> dict:
    """OpenAI API 호출"""
    from openai import OpenAI
    load_secrets()
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL", "gpt-4.1")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are Figure Planner. Output JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=4096,
    )
    return _extract_json(resp.choices[0].message.content or "")


def _call_anthropic(prompt: str, temperature: float = 0.7) -> dict:
    """Anthropic API 호출"""
    from anthropic import Anthropic
    load_secrets()
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        temperature=temperature,
        system="You are Figure Planner. Output JSON only.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text if resp.content else ""
    return _extract_json(text)


def _gemini_config(temperature: float, system_instruction: str):
    """Gemini 설정. thinking을 꺼서 출력 토큰이 본문에 쓰이도록 한다(빈 응답 방지)."""
    from google.genai import types
    kwargs = dict(
        system_instruction=system_instruction,
        temperature=temperature,
        response_mime_type="application/json",
        max_output_tokens=8192,
    )
    try:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except Exception:
        pass
    return types.GenerateContentConfig(**kwargs)


def _gemini_text_or_raise(resp) -> str:
    text = (getattr(resp, "text", None) or "").strip()
    if text:
        return text
    reason = ""
    try:
        cand = (getattr(resp, "candidates", None) or [None])[0]
        reason = (
            f"finish_reason={getattr(cand, 'finish_reason', None)}, "
            f"prompt_feedback={getattr(resp, 'prompt_feedback', None)}"
        )
    except Exception:
        pass
    raise ValueError(f"Gemini가 빈 응답을 반환했습니다 ({reason})")


_TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "overloaded", "high demand", "429",
                      "RESOURCE_EXHAUSTED", "500", "INTERNAL")
_QUOTA_MARKERS = ("429", "RESOURCE_EXHAUSTED", "insufficient_quota")


def _is_transient(err: Exception) -> bool:
    msg = str(err)
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _is_quota_exhausted(err: Exception) -> bool:
    msg = str(err)
    return any(marker in msg for marker in _QUOTA_MARKERS)


def _gemini_api_keys() -> list[str]:
    """key.env에 등록된 모든 Gemini 키를 순서대로 반환.

    GEMINI_API_KEY (기본), GEMINI_API_KEY_2, GEMINI_API_KEY_3, ...
    또는 GOOGLE_API_KEY 를 폴백으로 사용.
    """
    keys: list[str] = []
    primary = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    if primary:
        keys.append(primary)
    n = 2
    while True:
        k = os.getenv(f"GEMINI_API_KEY_{n}", "")
        if not k:
            break
        if k not in keys:
            keys.append(k)
        n += 1
    return keys


def _call_gemini(prompt: str, temperature: float = 0.7) -> dict:
    """Google Gemini API 호출.

    할당량(429/RESOURCE_EXHAUSTED) 초과 시 key.env의 다음 키로 자동 전환.
    일시적 오류(503 등)는 지수 백오프로 재시도.
    """
    from google import genai
    load_secrets()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    config = _gemini_config(temperature, "You are Figure Planner. Output JSON only.")
    max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))

    keys = _gemini_api_keys()
    if not keys:
        raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다 (key.env 확인).")

    last_err: Exception | None = None
    for key_idx, api_key in enumerate(keys):
        client = genai.Client(api_key=api_key)
        for attempt in range(max_retries):
            try:
                resp = client.models.generate_content(model=model, contents=prompt, config=config)
                return _extract_json(_gemini_text_or_raise(resp))
            except Exception as e:
                last_err = e
                if _is_quota_exhausted(e):
                    print(f"[LLM Planner] 키 {key_idx + 1}/{len(keys)} 할당량 초과 → 다음 키로 전환")
                    break  # 다음 키 시도
                if _is_transient(e) and attempt < max_retries - 1:
                    time.sleep(min(8, 2 ** attempt))
                    continue
                raise

    raise last_err or RuntimeError("모든 Gemini API 키의 할당량이 초과됐습니다.")


def _call_llm(prompt: str, temperature: float = 0.7) -> dict:
    """설정된 provider로 LLM 호출"""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider in ("gemini", "google"):
        return _call_gemini(prompt, temperature)
    if provider == "anthropic":
        return _call_anthropic(prompt, temperature)
    return _call_openai(prompt, temperature)


def plan_n_times(
    report_text: str,
    candidates: list,
    n: int = 3,
    max_figures: int = 8,
) -> list[dict]:
    """
    Self-Consistency: N회 LLM 호출하여 N개의 결과 수집

    Args:
        report_text: 정규화된 보고서 텍스트
        candidates: 트리거 통과한 후보 섹션 목록
        n: 호출 횟수 (기본 3)
        max_figures: 최대 그림 수

    Returns:
        N개의 LLM 응답 딕셔너리 리스트
    """
    # 후보 섹션 컨텍스트 구성
    candidate_context = ""
    for c in candidates:
        scores = c.trigger_scores or {}
        candidate_context += (
            f"\n--- Section: {c.section_path} (depth={c.depth}) ---\n"
            f"Trigger scores: {scores}\n"
            f"Text: {c.text[:500]}\n"
        )

    prompt = _build_master_prompt(
        report_text + "\n\n[Candidate Sections]\n" + candidate_context,
        max_figures,
    )

    results: list[dict] = []
    errors: list[str] = []
    for i in range(n):
        try:
            result = _call_llm(prompt, temperature=0.7)
            results.append(result)
        except Exception as e:
            msg = f"호출 {i+1}/{n} 실패: {e}"
            errors.append(msg)
            print(f"[LLM Planner] {msg}")
            continue

    if not results:
        # 모든 호출 실패 시 빈 결과
        results.append({
            "report_summary": "",
            "total_chars": len(report_text),
            "baseline_count": min(len(report_text) // 1500, max_figures),
            "total_figures": 0,
            "figures": [],
            "_planner_errors": errors,
        })

    elif errors:
        results[0]["_planner_errors"] = errors

    return results
