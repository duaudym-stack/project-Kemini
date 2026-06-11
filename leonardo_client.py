"""Leonardo.ai image generation client.

This module is intentionally separate from the default image-generation path.
Leonardo jobs are asynchronous and include polling plus CDN download, so callers
should opt into it explicitly.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from .secret_loader import load_secrets


def leonardo_available() -> bool:
    load_secrets()
    return bool(os.getenv("LEONARDO_API_KEY") or os.getenv("IMAGE_API_KEY"))


def _http_json(url: str, method: str, headers: dict, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def generate_image_data_url(prompt: str) -> str:
    """Generate a Leonardo image, download it, and return a data URL."""
    load_secrets()
    api_key = os.getenv("LEONARDO_API_KEY") or os.getenv("IMAGE_API_KEY")
    if not api_key:
        raise RuntimeError("LEONARDO_API_KEY/IMAGE_API_KEY is not loaded.")

    base = "https://cloud.leonardo.ai/api/rest/v1"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
    }
    body = {
        "prompt": prompt[:1490],
        "modelId": os.getenv("LEONARDO_MODEL_ID", "1e60896f-3c26-4296-8ecc-53e2afecc132"),
        "width": int(os.getenv("LEONARDO_WIDTH", "1024")),
        "height": int(os.getenv("LEONARDO_HEIGHT", "576")),
        "num_images": 1,
        "negative_prompt": (
            "person, people, human, man, woman, girl, boy, child, face, portrait, "
            "character, anime, manga, realistic human figure, body, hands, eyes, crowd, "
            "watermark, signature, logo overlay, text artifact"
        ),
        "guidance_scale": 7,
        "presetStyle": "NONE",
    }

    try:
        start = _http_json(f"{base}/generations", "POST", headers, body, timeout=30)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Leonardo generation request failed {exc.code}: {detail}") from exc

    gen_id = (start.get("sdGenerationJob") or {}).get("generationId")
    if not gen_id:
        raise RuntimeError(f"Leonardo did not return generationId: {start}")

    image_url = None
    tries = int(os.getenv("LEONARDO_POLL_TRIES", "25"))
    interval = float(os.getenv("LEONARDO_POLL_INTERVAL", "3"))
    for _ in range(tries):
        time.sleep(interval)
        info = _http_json(f"{base}/generations/{gen_id}", "GET", headers, None, timeout=30)
        pk = info.get("generations_by_pk") or {}
        status = pk.get("status")
        if status == "COMPLETE":
            images = pk.get("generated_images") or []
            if images:
                image_url = images[0].get("url")
            break
        if status == "FAILED":
            raise RuntimeError("Leonardo generation failed (status=FAILED).")

    if not image_url:
        raise RuntimeError("Leonardo image was not ready before timeout.")

    dl_req = urllib.request.Request(image_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(dl_req, timeout=60) as resp:
        raw = resp.read()
        content_type = resp.headers.get("Content-Type", "")

    mime = content_type if content_type.startswith("image/") else (
        "image/png" if image_url.lower().endswith(".png") else "image/jpeg"
    )
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
