/* ════════════════════════════════════════════════════════════════
   api.js — Figure Planner 백엔드 연결 래퍼 (process_6)

   - 백엔드(FastAPI, figure_planner)가 살아 있으면 AI 그림 분석/이미지
     생성을 사용하고, 없으면 호출이 조용히 실패해 프론트엔드는 기존
     클라이언트 사이드 휴리스틱 + canvas 렌더로 폴백한다.
   - API 키는 서버의 key.env.enc(DPAPI)에서만 로드되며, 프론트엔드는
     키를 절대 보관하지 않는다.
════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  // 백엔드가 직접 서빙(localhost:8000)하면 동일 출처, file://로 열면 localhost:8000으로 시도
  const API_BASE = location.protocol === "file:" ? "http://localhost:8000" : "";

  async function health(timeoutMs = 8000) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(`${API_BASE}/api/v1/health`, { signal: ctrl.signal });
      clearTimeout(timer);
      const body = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, body };
    } catch (err) {
      return { ok: false, status: 0, error: err && err.message };
    }
  }

  // 문서를 백엔드 Figure Planner 파이프라인에 보내 AI 그림 추천 JSON을 받는다.
  async function analyzeDocument(file, timeoutMs = 180000) {
    const form = new FormData();
    form.append("file", file);
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(`${API_BASE}/api/v1/documents/upload`, {
        method: "POST",
        body: form,
        signal: ctrl.signal
      });
      clearTimeout(timer);
      const body = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, body };
    } catch (err) {
      return { ok: false, status: 0, error: err && err.message };
    }
  }

  // HWPX section*.xml 파싱 → 텍스트/표 추출 JSON (hwpx 스킬 적용).
  async function extractSections(file, timeoutMs = 60000) {
    const form = new FormData();
    form.append("file", file);
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(`${API_BASE}/api/v1/documents/sections`, {
        method: "POST",
        body: form,
        signal: ctrl.signal
      });
      clearTimeout(timer);
      const body = await res.json().catch(() => ({}));
      return { ok: res.ok && Array.isArray(body.sections), status: res.status, body };
    } catch (err) {
      return { ok: false, status: 0, error: err && err.message };
    }
  }

  // 문서를 백엔드에서 LibreOffice→PDF→PNG로 렌더해 "원본과 동일한" 페이지 이미지를 받는다.
  // LibreOffice 미설치/변환 실패 시 ok:false → 프론트가 기존 HTML 미리보기를 유지한다.
  async function previewDocument(file, timeoutMs = 120000) {
    const form = new FormData();
    form.append("file", file);
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(`${API_BASE}/api/v1/documents/preview`, {
        method: "POST",
        body: form,
        signal: ctrl.signal
      });
      clearTimeout(timer);
      const body = await res.json().catch(() => ({}));
      return { ok: res.ok && Array.isArray(body.pages) && body.pages.length > 0, status: res.status, body };
    } catch (err) {
      return { ok: false, status: 0, error: err && err.message };
    }
  }

  // Gemini 이미지 생성. 실패 시 ok:false → 프론트가 canvas로 폴백.
  async function generateImage({ prompt, description, category, style, contextText }, timeoutMs = 120000) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(`${API_BASE}/api/v1/images/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: prompt || "",
          description: description || "",
          category: category || "",
          style: style || "",
          context_text: contextText || ""
        }),
        signal: ctrl.signal
      });
      const body = await res.json().catch(() => ({}));
      clearTimeout(timer);
      return { ok: res.ok && !!body.image_url, status: res.status, body };
    } catch (err) {
      return { ok: false, status: 0, error: err && err.message };
    }
  }

  // 원본 HWPX + 생성된 이미지 목록 → 이미지 삽입된 HWPX Blob 반환.
  // figures: [{ anchor_text, position_hint, image_base64, image_mime }, ...]
  async function assembleHwpx(hwpxFile, figures, timeoutMs = 120000) {
    const form = new FormData();
    form.append("file", hwpxFile);
    form.append("figures", JSON.stringify(figures));
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(`${API_BASE}/api/v1/documents/assemble`, {
        method: "POST",
        body: form,
        signal: ctrl.signal,
      });
      clearTimeout(timer);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        return { ok: false, status: res.status, error: body.detail || "조립 실패" };
      }
      const blob = await res.blob();
      const filename = (res.headers.get("Content-Disposition") || "")
        .match(/filename="?([^";\n]+)"?/)?.[1] || "assembled.hwpx";
      return { ok: true, status: res.status, blob, filename };
    } catch (err) {
      return { ok: false, status: 0, error: err && err.message };
    }
  }

  global.API = { API_BASE, health, analyzeDocument, extractSections, previewDocument, generateImage, assembleHwpx };
})(window);
