"""
server.py — FastAPI 서버
기존 프론트엔드(api.js)의 엔드포인트와 연결하며,
HTML/CSS/JS를 Static Files로 서빙합니다.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .secret_loader import ENCRYPTED_ENV, PLAINTEXT_ENV, load_secrets

load_secrets()

from .document_renderer import render_document_pages, render_document_pages_with_diagnostics, renderer_status
from .hybrid_pipeline import run_from_bytes_hybrid
from .pipeline import run_from_bytes

app = FastAPI(
    title="Figure Planner v2.1",
    description="보고서 그림 자동 배치 AI",
    version="2.1.0",
)

# CORS 설정 (개발용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 결과 저장 디렉토리 ──
WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent.parent  # process_6/
RESULT_DIR = WORKSPACE_DIR / "result"
RESULT_DIR.mkdir(parents=True, exist_ok=True)
FONT_DIR = WORKSPACE_DIR / "font"

# ── 프론트엔드 정적 파일 경로 ──
FRONTEND_DIR = WORKSPACE_DIR


# ────────────────────────────────────────────
#  API 엔드포인트
# ────────────────────────────────────────────

class ImageGenRequest(BaseModel):
    description: str = ""
    category: str = ""
    style: str = ""
    context_text: str = ""
    prompt: str = ""


@app.post("/api/v1/documents/upload")
async def upload_document(file: UploadFile = File(...)):
    """문서 업로드 → Figure Planner 파이프라인 실행"""
    load_secrets()
    if not file.filename:
        raise HTTPException(400, "파일명이 없습니다")

    fname = file.filename.lower()
    if fname.endswith(".docx"):
        file_format = "docx"
    elif fname.endswith(".hwpx"):
        file_format = "hwpx"
    elif fname.endswith(".hwp"):
        file_format = "hwp"
    elif fname.endswith(".pdf"):
        file_format = "pdf"
    elif fname.endswith(".md"):
        file_format = "md"
    elif fname.endswith(".txt"):
        file_format = "txt"
    else:
        raise HTTPException(400, f"지원하지 않는 형식: {fname}")

    contents = await file.read()

    max_figures = int(os.getenv("MAX_FIGURES", "8"))
    sc_n = int(os.getenv("SELF_CONSISTENCY_N", "3"))
    enable_critic = os.getenv("ENABLE_CRITIC", "true").lower() == "true"

    try:
        use_hybrid = os.getenv("ENABLE_HYBRID", "true").lower() == "true"
        if use_hybrid:
            result = run_from_bytes_hybrid(
                file_bytes=contents,
                file_format=file_format,
                max_figures=max_figures,
                self_consistency_n=sc_n,
                enable_critic=enable_critic,
                max_vision_pages=int(os.getenv("MAX_VISION_PAGES", "8")),
            )
        else:
            result = run_from_bytes(
                file_bytes=contents,
                file_format=file_format,
                max_figures=max_figures,
                self_consistency_n=sc_n,
                enable_critic=enable_critic,
            )
    except Exception as e:
        raise HTTPException(500, f"파이프라인 실행 실패: {str(e)}")

    # 결과 저장
    ts = datetime.now().strftime("%y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in (file.filename or "doc"))
    result_file = RESULT_DIR / f"{ts}_{safe_name}_result.md"
    _save_result_md(result, result_file, file.filename or "")

    return JSONResponse(content=result)


@app.post("/api/v1/documents/preview")
async def preview_document(file: UploadFile = File(...)):
    """Render uploaded documents into page images for browser preview."""
    if not file.filename:
        raise HTTPException(400, "파일명이 없습니다")

    fname = file.filename.lower()
    if fname.endswith(".docx"):
        file_format = "docx"
    elif fname.endswith(".doc"):
        file_format = "doc"
    elif fname.endswith(".hwpx"):
        file_format = "hwpx"
    elif fname.endswith(".hwp"):
        file_format = "hwp"
    elif fname.endswith(".pdf"):
        file_format = "pdf"
    else:
        raise HTTPException(400, f"미리보기를 지원하지 않는 형식: {fname}")

    contents = await file.read()
    try:
        pages, render_errors = render_document_pages_with_diagnostics(
            file_bytes=contents,
            file_format=file_format,
            max_pages=int(os.getenv("MAX_PREVIEW_PAGES", "30")),
        )
    except Exception as e:
        raise HTTPException(500, f"문서 미리보기 렌더링 실패: {str(e)}")

    if not pages:
        status = renderer_status(file_format)
        raise HTTPException(
            422,
            {
                "message": "문서를 원본 페이지 이미지로 변환할 수 없습니다.",
                "format": file_format,
                "renderer_status": status,
                "render_errors": render_errors,
                "next_steps": [
                    "HWP/HWPX는 hwp_api_env에 실제 http://IP:PORT/argodocument 주소를 넣거나 HWP_DOCUMENT_API_URL 환경 변수를 설정하세요.",
                    "또는 이 PC에 한컴오피스/한글 COM 또는 LibreOffice를 설치해 PDF 변환이 가능해야 합니다.",
                    "원본 .hwpx 파일을 주시면 현재 문서 구조 기준으로 추가 보정할 수 있습니다.",
                ],
            },
        )

    return JSONResponse(content={
        "pages": [
            {
                "page_number": page.page_number,
                "image_url": f"data:image/png;base64,{page.image_base64}",
                "width": page.width,
                "height": page.height,
                "text": page.text,
                "source": page.source,
            }
            for page in pages
        ]
    })


import re as _re

_KOR_SECTION_RE = _re.compile(r'\[([^\]]+)\]\s*([^\[]+)', _re.S)

_TYPE_GUIDE_EN: dict[str, str] = {
    "조감도": (
        "3D isometric planning illustration of a specific development site "
        "(wetland restoration, urban district, solar farm, river improvement, industrial complex, or similar project area). "
        "Aerial bird's-eye panoramic view of the entire project site. "
        "Central natural feature — river, wetland, lake, or green zone — as compositional axis, "
        "surrounded by planned facilities, roads, and distinct districts. "
        "Round colored icon bubbles mark each key facility with an icon and Korean label (color-coded by category). "
        "Small inset location reference map in top-left corner; facility legend table in bottom-right corner. "
        "Color palette: vivid blue water, lush green vegetation, cream/beige building surfaces, bright accent colors on icon pins. "
        "Soft oblique isometric perspective with gentle building shadows. Pastel base tone with vivid spot colors. "
        "Public-sector urban development, environmental, or infrastructure project report style. "
        "16:9 landscape orientation, white margins."
    ),
    "공정도": (
        "Horizontal left-to-right technical process flowchart. 3-5 rectangular step boxes with arrows. "
        "Icon and data per step. Blue and teal accents. Light gray box backgrounds. Korean-labeled steps. "
        "16:9 white background."
    ),
    "지도시각화": (
        "Korean administrative-district map visualization. Pastel regional color blocks. "
        "Korean region name labels. Pins, markers, and bubbles for location and value display. "
        "Clean borders. White background. Official static government-report style."
    ),
    "다이어그램": (
        "Data visualization chart — bar, line, or composite chart as appropriate. "
        "x-axis: years or categories; y-axis: numeric values. Legend included. "
        "Blue, teal, orange color scheme. White background with grid lines. "
        "Clear Korean axis labels and numeric values. 16:9 ratio."
    ),
    "인포그래픽": (
        "Title banner plus body grid layout infographic. 4-6 category boxes each with icon and Korean text. "
        "Blue, teal, green color scheme. Hierarchical information structure with emphasis typography. "
        "Official government-report style, 16:9."
    ),
    "현수막": (
        "Wide horizontal banner, 4:1 ratio. Calligraphy-style Korean slogan as focal point. "
        "Solid blue background. Institution name included. Impactful typography with generous white space."
    ),
    "홍보물": (
        "Korean public-institution CI/BI-compliant promotional material. Institutional blue color scheme. "
        "Format: signage, information board, or promotional banner. "
        "Balanced whitespace and text. Clear institutional identity. Official design style."
    ),
    "기타이미지": (
        "Photo-realistic documentary-style image closely related to the report content. "
        "Real-world scene of a site, facility, natural environment, or infrastructure. "
        "NO people, NO human figures. Landscape, facility exterior/interior, or environmental scene. "
        "Natural lighting, sharp and vivid colors, professional photography feel. 16:9 wide angle."
    ),
}


def _build_image_prompt(req: "ImageGenRequest") -> str:
    """한국어 구조화 프롬프트를 파싱해 Leonardo 친화적인 영문 프롬프트로 변환."""
    seed = (req.prompt or "").strip()
    sections: dict[str, str] = {}
    for m in _KOR_SECTION_RE.finditer(seed):
        sections[m.group(1).strip()] = m.group(2).strip()

    type_label = sections.get("유형", "")
    purpose    = sections.get("이미지 목적", "")
    content    = sections.get("이미지 내용", "")
    doc_topic  = sections.get("문서 주제", "")
    direction  = sections.get("생성 방향", "")

    en_guide = _TYPE_GUIDE_EN.get(type_label, "")

    if sections:
        parts: list[str] = []
        if en_guide:
            parts.append(en_guide)
        elif direction:
            parts.append(direction)
        if purpose:
            parts.append(f"Visual purpose: {purpose}")
        if content:
            parts.append(f"Content to depict: {content}")
        if doc_topic:
            parts.append(f"Document context: {doc_topic}")
    else:
        # 구조화되지 않은 프롬프트 폴백
        parts = [seed] if seed else []
        if req.description:
            parts.append(req.description)
        if req.category:
            parts.append(f"Figure type: {req.category}")

    parts.append(
        "IMPORTANT: Absolutely NO people, NO human figures, NO portraits, NO faces, "
        "NO characters, NO anime, NO realistic humans. "
        "Abstract diagram, infographic, or map illustration ONLY."
    )
    parts.append(
        "16:9 landscape ratio. White background. Suitable for embedding in an official "
        "Korean public-sector report. No watermark, no extra text artifacts."
    )
    return "\n".join(parts)


def _gemini_generate_image_data_url(prompt: str) -> str:
    """Gemini 이미지 모델로 PNG 생성 → data URL 반환."""
    from google import genai

    load_secrets()
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY가 로드되지 않았습니다 (key.env.enc 확인).")

    client = genai.Client(api_key=api_key)
    model = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

    # gemini-2.5-flash-image는 기본적으로 이미지 파트를 반환한다.
    resp = client.models.generate_content(model=model, contents=prompt)
    candidates = getattr(resp, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                data = inline.data
                if isinstance(data, str):
                    b64 = data
                else:
                    b64 = base64.b64encode(data).decode("ascii")
                mime = getattr(inline, "mime_type", None) or "image/png"
                return f"data:{mime};base64,{b64}"
    raise RuntimeError("Gemini 응답에서 이미지 데이터를 찾지 못했습니다.")


def _openai_generate_image_data_url(prompt: str) -> str:
    """OpenAI DALL-E로 이미지 생성 → data URL 반환."""
    import openai

    load_secrets()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 로드되지 않았습니다 (Railway 환경변수 확인).")

    client = openai.OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_IMAGE_MODEL", "dall-e-3")

    resp = client.images.generate(
        model=model,
        prompt=prompt,
        n=1,
        size="1792x1024",
        response_format="b64_json",
    )
    b64 = resp.data[0].b64_json
    return f"data:image/png;base64,{b64}"



@app.post("/api/v1/documents/sections")
async def extract_document_sections(file: UploadFile = File(...)):
    """HWPX → Contents/section*.xml 파싱 → 텍스트/표 추출 → JSON 반환.

    hwpx-rekian-master/hwpx 스킬의 추출 방식을 백엔드에 적용한다.
    """
    if not file.filename:
        raise HTTPException(400, "파일명이 없습니다")
    if not file.filename.lower().endswith(".hwpx"):
        raise HTTPException(400, f"HWPX만 지원합니다: {file.filename}")

    from .hwpx_cli_bridge import extract_markdown_with_hwpx_cli, hwpx_cli_available
    from .section_extract import extract_sections

    contents = await file.read()
    try:
        data = extract_sections(contents)
    except Exception as e:
        raise HTTPException(500, f"섹션 추출 실패: {str(e)}")

    data["hwpx_cli_available"] = hwpx_cli_available()
    data["markdown"] = extract_markdown_with_hwpx_cli(contents) or ""
    data["source_file"] = file.filename
    return JSONResponse(content=data)


def _resolve_image_provider() -> str:
    """이미지 생성 provider 결정.

    우선순위: IMAGE_PROVIDER 명시 → OpenAI 키 존재 → Leonardo 키 존재 → Gemini 키 존재.
    """
    load_secrets()
    from .leonardo_client import leonardo_available

    explicit = os.getenv("IMAGE_PROVIDER", "").lower()
    if explicit == "openai":
        return "openai"
    if explicit in ("gemini", "google"):
        return explicit
    if explicit == "leonardo" or (not explicit and leonardo_available()):
        return "leonardo"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    return ""


@app.post("/api/v1/images/generate")
def generate_image(req: ImageGenRequest):
    """기본 이미지 생성 엔드포인트.

    IMAGE_PROVIDER=openai (또는 OPENAI_API_KEY만 있는 경우) → OpenAI DALL-E 경로 사용.
    IMAGE_PROVIDER=leonardo (또는 Leonardo 키만 있는 경우) → Leonardo 경로 사용.
    IMAGE_PROVIDER=gemini / google (또는 Gemini 키만 있는 경우) → Gemini 경로 사용.
    실패하면 502를 반환하며, 프론트엔드는 이를 받아 canvas 렌더로 폴백한다.
    """
    load_secrets()
    image_provider = _resolve_image_provider()
    prompt = _build_image_prompt(req)
    try:
        if image_provider == "openai":
            return {"image_url": _openai_generate_image_data_url(prompt), "provider": "openai"}
        if image_provider == "leonardo":
            from .leonardo_client import generate_image_data_url
            return {"image_url": generate_image_data_url(prompt), "provider": "leonardo"}
        if image_provider in ("gemini", "google"):
            return {"image_url": _gemini_generate_image_data_url(prompt), "provider": "gemini"}
        raise RuntimeError(
            "이미지 생성 키가 없습니다. "
            "key.env에 OPENAI_API_KEY, GEMINI_API_KEY, 또는 IMAGE_API_KEY(Leonardo)를 설정하세요."
        )
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e), "fallback": "canvas"})


@app.post("/api/v1/leonardo/images/generate")
def generate_leonardo_image(req: ImageGenRequest):
    """Leonardo.ai 전용 이미지 생성 엔드포인트."""
    from .leonardo_client import generate_image_data_url

    prompt = _build_image_prompt(req)
    try:
        return {"image_url": generate_image_data_url(prompt), "provider": "leonardo"}
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": str(e),
            "fallback": "canvas",
            "provider": "leonardo",
        })


@app.post("/api/v1/documents/assemble")
async def assemble_document(
    file: UploadFile = File(...),
    figures: str = Form(...),
):
    """원본 HWPX + AI 생성 이미지 목록 → 이미지 삽입된 HWPX 반환.

    figures: JSON 배열 (각 항목: anchor_text, position_hint, image_base64, image_mime)
    """
    from .hwpx_assembler import assemble_hwpx, FigureInsertion

    if not file.filename:
        raise HTTPException(400, "파일명이 없습니다")
    if not file.filename.lower().endswith(".hwpx"):
        raise HTTPException(400, f"HWPX만 지원합니다: {file.filename}")

    try:
        figures_data = json.loads(figures)
        if not isinstance(figures_data, list):
            raise ValueError("배열이어야 합니다")
        fig_list = [
            FigureInsertion(
                anchor_text=f.get("anchor_text", ""),
                position_hint=f.get("position_hint", "after"),
                image_base64=f.get("image_base64", ""),
                image_mime=f.get("image_mime", "image/png"),
            )
            for f in figures_data
        ]
    except Exception as exc:
        raise HTTPException(400, f"figures JSON 파싱 실패: {exc}")

    original = await file.read()
    try:
        assembled = assemble_hwpx(original, fig_list)
    except Exception as exc:
        raise HTTPException(500, f"HWPX 조립 실패: {exc}")

    safe_name = file.filename.rsplit(".", 1)[0] + "_assembled.hwpx"
    ascii_fallback = safe_name.encode("ascii", errors="replace").decode("ascii")
    encoded_name = urllib.parse.quote(safe_name, safe="")
    return Response(
        content=assembled,
        media_type="application/vnd.hancom.hwpx",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{encoded_name}"
            ),
            "Content-Length": str(len(assembled)),
        },
    )


@app.post("/api/v1/documents/edit")
async def edit_document(
    file: UploadFile = File(...),
    replacements: str = Form(...),
):
    """HWPX 양식 템플릿의 플레이스홀더를 치환해 반환한다.

    replacements: JSON 객체 {"{{이름}}": "홍길동", ...}
    """
    import tempfile as _tmpfile

    if not file.filename:
        raise HTTPException(400, "파일명이 없습니다")
    if not file.filename.lower().endswith(".hwpx"):
        raise HTTPException(400, f"HWPX만 지원합니다: {file.filename}")

    try:
        mapping = json.loads(replacements)
        if not isinstance(mapping, dict):
            raise ValueError("JSON 객체여야 합니다")
    except Exception as exc:
        raise HTTPException(400, f"replacements 파싱 실패: {exc}")

    from .hwpx_edit import HwpxEditor

    original = await file.read()

    with _tmpfile.TemporaryDirectory(prefix="fp_edit_") as tmp:
        src = os.path.join(tmp, "template.hwpx")
        out = os.path.join(tmp, "edited.hwpx")
        with open(src, "wb") as f:
            f.write(original)

        try:
            ed = HwpxEditor(src)
            replaced_n = ed.replace_map(mapping)
            ed.save(out)
            with open(out, "rb") as f:
                edited = f.read()
        except Exception as exc:
            raise HTTPException(500, f"HWPX 편집 실패: {exc}")

    safe_name = file.filename.rsplit(".", 1)[0] + "_edited.hwpx"
    ascii_fallback = safe_name.encode("ascii", errors="replace").decode("ascii")
    encoded_name = urllib.parse.quote(safe_name, safe="")
    return Response(
        content=edited,
        media_type="application/vnd.hancom.hwpx",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{encoded_name}"
            ),
            "Content-Length": str(len(edited)),
            "X-Replacements-Count": str(replaced_n),
        },
    )


@app.post("/api/v1/documents/bake")
async def bake_document(file: UploadFile = File(...)):
    """HWPX를 한글 COM으로 열어 레이아웃을 재계산(baking)한 후 반환한다.

    한컴오피스가 설치된 경우에만 동작. linesegarray를 채워 글자가 올바르게 보이도록 한다.
    """
    import subprocess
    import sys
    import tempfile as _tmpfile

    if not file.filename:
        raise HTTPException(400, "파일명이 없습니다")
    if not file.filename.lower().endswith(".hwpx"):
        raise HTTPException(400, f"HWPX만 지원합니다: {file.filename}")

    original = await file.read()

    bake_script = Path(__file__).resolve().parent / "hwpx_bake.py"
    if not bake_script.exists():
        raise HTTPException(503, "hwpx_bake.py를 찾을 수 없습니다")

    with _tmpfile.TemporaryDirectory(prefix="fp_bake_") as tmp:
        src = os.path.join(tmp, "input.hwpx")
        with open(src, "wb") as f:
            f.write(original)

        try:
            proc = subprocess.run(
                [sys.executable, str(bake_script), src],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Baking 시간 초과(120초)")
        except Exception as exc:
            raise HTTPException(500, f"Baking 실행 실패: {exc}")

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise HTTPException(500, f"Baking 실패 (exit {proc.returncode}): {detail}")

        with open(src, "rb") as f:
            baked = f.read()

    safe_name = file.filename.rsplit(".", 1)[0] + "_baked.hwpx"
    ascii_fallback = safe_name.encode("ascii", errors="replace").decode("ascii")
    encoded_name = urllib.parse.quote(safe_name, safe="")
    return Response(
        content=baked,
        media_type="application/vnd.hancom.hwpx",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{encoded_name}"
            ),
            "Content-Length": str(len(baked)),
        },
    )


@app.get("/api/v1/health")
async def health():
    load_secrets()
    from .leonardo_client import leonardo_available

    image_provider = _resolve_image_provider()
    return {
        "status": "ok",
        "version": "2.1.0",
        "openai_key_loaded": bool(os.getenv("OPENAI_API_KEY")),
        "anthropic_key_loaded": bool(os.getenv("ANTHROPIC_API_KEY")),
        "gemini_key_loaded": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
        "llm_provider": os.getenv("LLM_PROVIDER", "openai"),
        "openai_model": os.getenv("OPENAI_MODEL", ""),
        "gemini_model": os.getenv("GEMINI_MODEL", ""),
        "gemini_image_model": os.getenv("GEMINI_IMAGE_MODEL", ""),
        "leonardo_key_loaded": leonardo_available(),
        "leonardo_endpoint": "/api/v1/leonardo/images/generate",
        "image_provider": image_provider,
        "image_gen_available": image_provider in ("openai", "gemini", "google", "leonardo"),
        "hybrid_enabled": os.getenv("ENABLE_HYBRID", "true").lower() == "true",
        "secret_error": bool(os.getenv("FIGURE_PLANNER_SECRET_ERROR")),
        "encrypted_env_exists": ENCRYPTED_ENV.exists(),
        "encrypted_env_path": str(ENCRYPTED_ENV),
        "plaintext_env_exists": PLAINTEXT_ENV.exists(),
    }


# ────────────────────────────────────────────
#  프론트엔드 정적 파일 서빙
# ────────────────────────────────────────────

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

# 메인 HTML 서빙
@app.get("/")
async def serve_index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path), media_type="text/html", headers=_NO_CACHE)
    return {"message": "프론트엔드 파일을 찾을 수 없습니다"}


# 정적 파일 (CSS, JS)
for static_file in ["styles.css", "app.js", "api.js"]:
    fpath = FRONTEND_DIR / static_file

    def _make_handler(p=fpath):
        async def handler():
            if p.exists():
                mt = "text/css" if p.suffix == ".css" else "application/javascript"
                return FileResponse(str(p), media_type=mt, headers=_NO_CACHE)
            raise HTTPException(404)
        return handler

    app.get(f"/{static_file}")(_make_handler())

if FONT_DIR.exists():
    app.mount("/font", StaticFiles(directory=str(FONT_DIR)), name="font")


# ────────────────────────────────────────────
#  결과 저장 헬퍼
# ────────────────────────────────────────────

def _save_result_md(result: dict, path: Path, filename: str):
    """분석 결과를 Markdown 파일로 저장"""
    lines = [
        f"# Figure Planner 분석 결과",
        f"",
        f"- **파일**: {filename}",
        f"- **총 글자수**: {result.get('total_chars', 0):,}",
        f"- **기준 개수**: {result.get('baseline_count', 0)}",
        f"- **최종 그림 수**: {result.get('total_figures', 0)}",
        f"- **분석 요약**: {result.get('report_summary', '')}",
        f"",
        f"## 추천 그림 목록",
        f"",
    ]

    for fig in result.get("figures", []):
        lines.extend([
            f"### {fig.get('figure_id', '')} — {fig.get('figure_type', '')}",
            f"- **섹션**: {fig.get('section_path', '')}",
            f"- **위치**: {fig.get('position_hint', '')}",
            f"- **목적**: {fig.get('purpose', '')}",
            f"- **앵커**: {fig.get('anchor_text', '')}",
            f"- **신뢰도**: {fig.get('confidence', 0):.2f}",
            f"- **최종 점수**: {fig.get('final_score', 0):.2f}",
            f"- **투표 수**: {fig.get('vote_count', 0)}",
            f"- **구성 요소**: {', '.join(fig.get('suggested_content', []))}",
            f"- **이미지 프롬프트**: {fig.get('image_prompt_seed', '')}",
            f"",
        ])

    lines.extend([
        f"## 메타 정보",
        f"",
        f"```json",
        json.dumps(result.get("meta", {}), ensure_ascii=False, indent=2),
        f"```",
    ])

    path.write_text("\n".join(lines), encoding="utf-8")


# ────────────────────────────────────────────
#  CLI 실행
# ────────────────────────────────────────────

def main():
    """uvicorn으로 서버 실행"""
    import uvicorn
    uvicorn.run(
        "figure_planner.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()
