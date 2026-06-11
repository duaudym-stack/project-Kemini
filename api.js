/* ════════════════════════════════════════════════════════════════
   api.js — Backend abstraction + placeholder fallback.

   The real AI image generation (Gemini / Imagen) lives on a backend
   that the frontend never touches the API key for. While the backend
   is unavailable, generateImage() falls back to picsum.photos so the
   UI is fully demoable.

   This module ALSO exports the category constraint table used by the
   prompt builder in app.js — kept here so the same map is reachable
   from a future server-side prompt sanitizer.
════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  const API_BASE = (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
    ? 'http://localhost:8000'
    : '';

  /* Flat seeds — one seed per category. Style dimension was removed
     because the UI no longer exposes style; "아이소메트릭" is now a
     first-class category. */
  const SEEDS = {
    '조감도':       'ae-aerial',
    '다이어그램':   'dg-diagram',
    '인포그래픽':   'if-infographic',
    '배너 디자인':  'bn-banner',
    '카드뉴스':     'cn-cardnews',
    '현수막':       'hs-banner-out',
    '아이소메트릭': 'iso-3d',
    '사진':         'photo-doc'
  };

  /* Category constraints — used by the prompt builder. */
  const CATEGORY_CONSTRAINTS = {
    '인포그래픽': {
      allowed:   ['process flow', 'icon-based composition', 'data visualization', 'numbered steps', 'clean vector layout'],
      forbidden: ['photography', 'landscape', 'concept art', 'general illustration', 'fantasy', 'clip art', 'stock photo feel']
    },
    '다이어그램': {
      allowed:   ['structure diagram', 'system architecture', 'flowchart', 'technical diagram', 'connected nodes', 'labeled arrows'],
      forbidden: ['photography', 'landscape', 'decorative illustration', 'characters', 'fantasy']
    },
    '조감도': {
      allowed:   ["aerial bird's-eye view", 'top-down satellite-style render', 'site overview', 'architectural visualization from above'],
      forbidden: ['infographic', 'diagram', 'close-up viewpoint', 'eye-level photography', 'portrait']
    },
    '배너 디자인': {
      allowed:   ['horizontal banner', 'editorial layout', 'bold typography composition', 'wide aspect marketing visual'],
      forbidden: ['random photo', 'cartoon', 'clip art', 'fantasy']
    },
    '카드뉴스': {
      allowed:   ['vertical card layout', 'social-media news format', 'editorial title card', 'clear hierarchy'],
      forbidden: ['busy photography', 'cartoon', 'fantasy']
    },
    '현수막': {
      allowed:   ['outdoor banner', 'large-format printable layout', 'high-contrast headline', 'wide aspect'],
      forbidden: ['busy collage', 'fantasy', 'clip art']
    },
    '아이소메트릭': {
      allowed:   ['isometric projection', '30-degree axonometric view', 'clean 3D vector blocks', 'unified light direction', 'flat shading'],
      forbidden: ['photography', 'perspective view', 'fish-eye lens', 'fantasy', 'painterly textures']
    },
    /* 사진 — documentary photography for 복원 전/후 / 현장 기록 / 사례.
       Intentionally allows photography; bans illustration vocabulary
       so a request for a photo never becomes a vector. */
    '사진': {
      allowed:   ['documentary photography', 'photojournalism', 'natural daylight', 'on-site capture', 'public-sector report photography', '300dpi print quality'],
      forbidden: ['cartoon', 'clip art', 'fantasy', 'painterly illustration', 'vector flat design', 'icon-based composition', 'AI-style stylization']
    }
  };

  function placeholderUrl(category, extra) {
    const base = SEEDS[category] || 'default';
    const s = extra != null ? `${base}-${extra}` : base;
    return `https://picsum.photos/seed/${encodeURIComponent(s)}/1024/640`;
  }

  async function uploadDocument(file, onProgress) {
    const meta = { name: file.name, size: file.size, type: file.type, ext: extOf(file.name) };
    if (!API_BASE) {
      Logger.info('api.uploadDocument', 'backend offline → skipping upload notify', meta);
      return { ok: false, status: 0, error: 'backend-offline' };
    }
    Logger.info('api.uploadDocument', 'POST /api/v1/documents/upload', meta);

    const form = new FormData();
    form.append('file', file);

    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API_BASE}/api/v1/documents/upload`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && typeof onProgress === 'function') onProgress(e.loaded / e.total);
      };
      const t0 = performance.now();
      xhr.onload = () => {
        const dt = Math.round(performance.now() - t0);
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch (_) {}
        const out = { ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, body };
        Logger.info('api.uploadDocument', `← ${xhr.status} in ${dt}ms`, { ok: out.ok });
        resolve(out);
      };
      xhr.onerror   = () => { Logger.warn('api.uploadDocument', 'network error'); resolve({ ok: false, status: 0, error: 'network' }); };
      xhr.ontimeout = () => { Logger.warn('api.uploadDocument', 'timeout');       resolve({ ok: false, status: 0, error: 'timeout' }); };
      xhr.timeout = 60_000;
      xhr.send(form);
    });
  }

  async function generateImage(payload) {
    const safe = redact(payload);
    Logger.info('api.generateImage', 'POST /api/v1/images/generate', safe);

    if (!API_BASE) {
      const url = placeholderUrl(payload.category, Math.floor(Math.random() * 9999));
      Logger.info('api.generateImage', '→ placeholder (backend offline)', { url });
      return { ok: true, body: { image_url: url, fallback: true } };
    }
    try {
      const t0 = performance.now();
      const res = await fetch(`${API_BASE}/api/v1/images/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const body = await res.json().catch(() => ({}));
      const dt = Math.round(performance.now() - t0);
      Logger.info('api.generateImage', `← ${res.status} in ${dt}ms`, { ok: res.ok });
      return { ok: res.ok, status: res.status, body };
    } catch (err) {
      Logger.warn('api.generateImage', 'transport error', { msg: err.message });
      return { ok: false, status: 0, error: err.message };
    }
  }

  function extOf(name) {
    const m = String(name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
    return m ? m[1] : '';
  }

  /* Strip anything that looks like a secret before logging. */
  function redact(obj) {
    if (!obj || typeof obj !== 'object') return obj;
    const out = {};
    for (const k of Object.keys(obj)) {
      if (/(key|token|secret|password|auth|bearer)/i.test(k)) out[k] = '[REDACTED]';
      else if (typeof obj[k] === 'string' && obj[k].length > 240) out[k] = obj[k].slice(0, 240) + `…(+${obj[k].length - 240})`;
      else if (obj[k] && typeof obj[k] === 'object') out[k] = redact(obj[k]);
      else out[k] = obj[k];
    }
    return out;
  }

  const Logger = {
    info(component, op, payload)  { console.log('[INFO]',  ts(), component, '·', op, payload || ''); },
    warn(component, op, payload)  { console.warn('[WARN]', ts(), component, '·', op, payload || ''); },
    error(component, op, payload) { console.error('[ERR]', ts(), component, '·', op, payload || ''); },
    debug(component, op, payload) { console.debug('[DBG]', ts(), component, '·', op, payload || ''); }
  };
  function ts() { return new Date().toISOString(); }

  global.API = {
    placeholderUrl,
    uploadDocument,
    generateImage,
    SEEDS,
    CATEGORY_CONSTRAINTS,
    Logger
  };
})(window);
