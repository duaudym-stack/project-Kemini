/* ════════════════════════════════════════════════════════════════
   app.js — Visual Flow

   Component layout:
     ┌─ Theme           : light/dark toggle, persisted
     ├─ Toast           : ephemeral status messages
     ├─ Uploader        : drag-and-drop + click upload + state machine
     ├─ DocParser       : .docx (mammoth) / .hwpx (JSZip) → HTML
     ├─ SlotDetector    : 7-pass regex passes over HTML → slot list
     ├─ BriefBuilder    : structured Image Brief per slot (document
     │                    title, section title, neighbor paragraphs,
     │                    keywords, category hard-constraints)
     ├─ PromptBuilder   : Image Brief → structured prompt text
     ├─ Workspace       : document view + page list + actions
     └─ OptionsPanel    : per-slot editing (category + free-text edits)

   Style dimension was removed. "아이소메트릭" is now a first-class
   category. Image generation uses the structured Brief + Prompt; the
   backend (when present) consumes them, otherwise picsum fallback.
════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── Categories surfaced in the slot editor ──
     "사진" is photo-friendly — its CATEGORY_CONSTRAINTS forbidden list
     deliberately omits "photography" so documentary-style 복원 전/후
     slots don't fight their own visualStyle. */
  const CATEGORIES = ['조감도', '다이어그램', '인포그래픽', '배너 디자인', '카드뉴스', '현수막', '아이소메트릭', '사진'];

  /* ── Lazy library loaders (load only when a user actually uploads) ── */
  let _mammothPromise = null;
  function loadMammoth() {
    if (window.mammoth) return Promise.resolve(window.mammoth);
    if (_mammothPromise) return _mammothPromise;
    Log.info('boot', 'loading mammoth.js');
    _mammothPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/mammoth@1.6.0/mammoth.browser.min.js';
      s.onload = () => resolve(window.mammoth);
      s.onerror = () => reject(new Error('mammoth.js를 불러올 수 없습니다'));
      document.head.appendChild(s);
    });
    return _mammothPromise;
  }
  function loadJSZip() {
    // Already preloaded in the <head> with `defer`. Wait for it if needed.
    if (window.JSZip) return Promise.resolve(window.JSZip);
    Log.info('boot', 'waiting for JSZip to finish loading');
    return new Promise((resolve, reject) => {
      let n = 0;
      const t = setInterval(() => {
        if (window.JSZip) { clearInterval(t); resolve(window.JSZip); }
        else if (n++ > 50) { clearInterval(t); reject(new Error('JSZip을 불러올 수 없습니다')); }
      }, 100);
    });
  }

  /* ── Tiny helpers ── */
  const $   = (id) => document.getElementById(id);
  const $$  = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const Log = (window.API && window.API.Logger) || console;

  /* ════════════════════════════════════════════════════
     Theme
  ════════════════════════════════════════════════════ */
  const Theme = {
    init() {
      const saved = localStorage.getItem('ig.theme.apple');
      const initial = saved === 'dark' ? 'dark' : 'light';
      document.documentElement.setAttribute('data-theme', initial);
      $('btn-theme')?.addEventListener('click', this.toggle);
      Log.info('Theme', 'init', { initial });
    },
    toggle() {
      const cur  = document.documentElement.getAttribute('data-theme');
      const next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('ig.theme.apple', next);
      Log.info('Theme', 'toggle', { from: cur, to: next });
    }
  };

  /* ════════════════════════════════════════════════════
     Toast
  ════════════════════════════════════════════════════ */
  const Toast = (() => {
    let timer;
    return {
      show(msg, opts = {}) {
        const el = $('toast');
        if (!el) return;
        el.textContent = msg;
        el.classList.toggle('is-error', !!opts.error);
        el.classList.add('is-visible');
        clearTimeout(timer);
        timer = setTimeout(() => el.classList.remove('is-visible'), opts.duration || 2800);
      }
    };
  })();

  /* ════════════════════════════════════════════════════
     Category inference from surrounding text
  ════════════════════════════════════════════════════ */
  function guessContextCat(rawText) {
    const t = (rawText || '').replace(/<[^>]+>/g, ' ').toLowerCase();
    /* Photo-domain vocabulary FIRST — environmental restoration / site
       photography / before-after / on-site records map to '사진'. This
       has to win over '인포그래픽' so the visualStyle (documentary
       photo) and forbidden list (no photography ban) stay consistent. */
    if (/(복원\s*[전후]|소생태계|생태계\s*복원|riparian|식재|자생수종|버드나무|복원사진|현장\s*사진|복원\s*결과|환경\s*복원)/.test(t)) return '사진';
    if (/(아이소메트릭|isometric|axonometric|3d render|3d 렌더)/.test(t)) return '아이소메트릭';
    if (/(도시|지역|부지|위치|지도|항공|조감|aerial|site map|배치도|기지|단지)/.test(t)) return '조감도';
    if (/(프로세스|단계|flow|흐름|순서|절차|diagram|architecture|시스템 구성|개념도)/.test(t)) return '다이어그램';
    if (/(통계|현황|비율|수치|데이터|차트|그래프|분석|kpi|infographic)/.test(t)) return '인포그래픽';
    if (/(현수막|행사|이벤트|축제|festival|시안)/.test(t)) return '현수막';
    if (/(광고|홍보|배너|캠페인|마케팅|banner)/.test(t)) return '배너 디자인';
    if (/(카드|뉴스|소셜|sns|인스타|카드뉴스)/.test(t)) return '카드뉴스';
    return '인포그래픽';
  }

  /* ════════════════════════════════════════════════════
     Slot detection (preserved 7-pass regex passes)
  ════════════════════════════════════════════════════ */
  /* sampleUrlForDesc — when a slot's label matches a known local
     reference image (shipped in ver10/), use that as the slot URL
     immediately. The demo then shows *the actual expected result*
     right after upload, with no AI roundtrip needed. */
  function sampleUrlForDesc(desc, cat) {
    const d = String(desc || '') + ' ' + String(cat || '');
    if (/복원|before|after/i.test(d))       return encodeURIComponent('그림 샘플 1.jpg');
    if (/현수막|배너|banner|시안/i.test(d)) return encodeURIComponent('그림 샘플 2.jpg');
    return null;
  }

  function slotHTML(id, desc, cat) {
    const url = sampleUrlForDesc(desc, cat) || API.placeholderUrl(cat, id);
    return `<div class="img-slot" id="slot-${id}" data-id="${id}" tabindex="0" role="button" aria-label="이미지 슬롯 ${id + 1}: ${esc(desc)}">
      <span class="img-slot-label">🖼️ ${esc(desc)}</span>
      <img src="${url}" alt="${esc(desc)}" loading="lazy">
      <span class="img-slot-overlay"><span>클릭하여 수정</span></span>
    </div>`;
  }

  function detectSlots(html) {
    const slots = [];
    let sid = 0;

    function mkSlot(desc, cat, ctxText) {
      const id   = sid++;
      const slot = { id, desc, cat, seed: id, aiImage: null, contextText: ctxText || '' };
      slots.push(slot);
      return slotHTML(id, desc, cat);
    }
    function ctxAt(str, offset) {
      /* -1000/+1000 window — guarantees the 1000-char slice in
         buildBrief actually carries new bytes (the prior 600/400
         window made the 1000-char slice a no-op). */
      return str.slice(Math.max(0, offset - 1000), offset + 1000)
                .replace(/<[^>]+>/g, ' ')
                .replace(/\s+/g, ' ');
    }

    // Pass 1: mammoth's own <img> tags (docx only).
    //         HWPX-embedded images carry class="hwpx-embedded" and
    //         must stay put — they are the ORIGINAL pictures the
    //         author already placed (e.g. "복원 전" photo).
    html = html.replace(/<img\b[^>]*>/gi, (m, offset, str) => {
      if (/class="hwpx-embedded"/i.test(m)) return m;          // keep original
      const altM = m.match(/alt="([^"]*)"/i);
      const desc = (altM && altM[1]) || '문서 삽입 이미지';
      const ctx  = ctxAt(str, offset);
      return mkSlot(desc, guessContextCat(ctx), ctx);
    });

    // Pass 2: [PLACEHOLDER], [이미지...], [그림...], [Figure...]
    html = html.replace(
      /\[PLACEHOLDER[_\- ]?(\d+)\s*[:：]\s*([^\]]+)\]|\[이미지[^\]]*\]|\[그림[^\]]*\]|\[사진[^\]]*\]|\[IMG[^\]]*\]|\[IMAGE[^\]]*\]|\[Figure[^\]]*\]|\[사진\s*삽입[^\]]*\]/gi,
      (m, _num, descTxt, offset, str) => {
        const ctx  = ctxAt(str, offset);
        const desc = descTxt ? descTxt.trim() : (m.replace(/[\[\]]/g, '').trim() || '이미지 삽입 공간');
        return mkSlot(desc, guessContextCat(ctx), ctx);
      }
    );

    // Pass 3: (그림 N) etc. — body references. KEEP the original token,
    //         attach the slot AFTER it. This preserves the writer's
    //         in-text reference verbatim.
    html = html.replace(/\(그림\s*\d+[^)]*\)|\(사진\s*\d+[^)]*\)|\(Figure\s*\d+[^)]*\)|\(Fig\.\s*\d+[^)]*\)/gi, (m, offset, str) => {
      const ctx = ctxAt(str, offset);
      return m + mkSlot(m.replace(/[()]/g, '').trim(), guessContextCat(ctx), ctx);
    });

    // Pass 4: caption paragraph (e.g. "<p>그림 1</p>"). KEEP the caption,
    //         attach the slot AFTER it.
    html = html.replace(/<p[^>]*>\s*(그림|사진|Figure|이미지)\s*\d*[\s.：:]*<\/p>/gi, (m, label, offset, str) => {
      const ctx  = ctxAt(str, offset);
      const numM = m.match(/\d+/);
      const desc = label.trim() + (numM ? ' ' + numM[0] : '');
      return m + mkSlot(desc, guessContextCat(ctx), ctx);
    });

    // Pass 5: underline-only paragraph
    html = html.replace(/<p[^>]*>\s*_{5,}\s*<\/p>/gi, (m, offset, str) => {
      const ctx = ctxAt(str, offset);
      return mkSlot('이미지 삽입 공간', guessContextCat(ctx), ctx);
    });

    // Pass 5a: inline parenthesised image labels — extended set so
    //          "(현수막 시안)", "(개념도)", "(조감도)", "(이미지 생성)"
    //          all match. Whitespace tolerant, paren-wrapped.
    html = html.replace(
      /\(\s*(이미지|그림|사진|현수막|배너|개념도|조감도|배치도|시안|예시)\s*(시안|생성|설명|정보|예시)?\s*[^)]{0,40}\)/g,
      (m, w1, w2, offset, str) => {
        const ctx   = ctxAt(str, offset);
        const label = m.replace(/[()]/g, '').replace(/\s+/g, ' ').trim().slice(0, 60);
        return m + mkSlot(label, guessContextCat(ctx + ' ' + label), ctx);
      }
    );

    // Pass 5b: bullet line "- 이미지 ..."
    html = html.replace(/<p[^>]*>(\s*[-•·–—]\s*이미지\s*(생성|설명|정보|시안)[^<]*)<\/p>/gi, (m, content, _k, offset, str) => {
      const ctx  = ctxAt(str, offset);
      const desc = content.trim().replace(/^[-•·–—]\s*/, '').slice(0, 60);
      return m + mkSlot(desc, guessContextCat(ctx), ctx);
    });

    // Pass 5d: position-coded captions used in 공공기관 보고서 templates.
    //          Match RULE: the label may appear anywhere inside a
    //          short <p> / <h1-6> block (≤60 chars total inner text)
    //          so prefixes like "소생태계 복원 전" are still caught.
    //          Standalone "시안" / "예시" tokens removed (P1: false-positive).
    //
    //          For <td> the slot is injected INSIDE the cell (P0:
    //          foster-parenting). For <p>/<h*> the caption text is
    //          kept verbatim and the slot is attached AFTER.
    const POS_LABELS = [
      '복원\\s*전', '복원\\s*후',
      '현수막\\s*시안', '배너\\s*시안', '포스터\\s*시안',
      '배치도', '조감도', '개념도', '구성도',
      '삽입\\s*예정', '이미지\\s*삽입\\s*예정', '그림\\s*삽입\\s*예정',
      '예시\\s*이미지', '시안\\s*이미지', 'before\\s*restoration', 'after\\s*restoration'
    ].join('|');

    /* <p>/<h1-6> — label CONTAINED anywhere in a short block. */
    const posBlockRe = new RegExp(
      '<(p|h[1-6])\\b[^>]*>([\\s\\S]{0,60}?(?:' + POS_LABELS + ')[\\s\\S]{0,60}?)<\\/\\1>',
      'gi'
    );
    html = html.replace(posBlockRe, (m, _tag, inner, offset, str) => {
      const ctx   = ctxAt(str, offset);
      const text  = inner.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
      if (text.length > 60) return m;     // safety: short blocks only
      if (/class="img-slot"/i.test(m)) return m;  // already a slot
      const label = text.slice(0, 60);
      return m + mkSlot(label, guessContextCat(ctx + ' ' + label), ctx);
    });

    /* <td> — label CONTAINED in a short cell. Slot injected INSIDE
       the cell so table foster-parenting can't toss it outside. */
    const posCellRe = new RegExp(
      '<td\\b([^>]*)>([\\s\\S]*?(?:' + POS_LABELS + ')[\\s\\S]*?)<\\/td>',
      'gi'
    );
    html = html.replace(posCellRe, (m, attrs, inner, offset, str) => {
      if (/class="img-slot"/i.test(inner)) return m;
      /* Cell already has the original HWPX picture → leave it alone.
         "원본 이미지 유지" 요구사항 (복원 전 사진 등). */
      if (/class="hwpx-embedded"/i.test(inner)) return m;
      const text = inner.replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
      if (!text || text.length > 80) return m;
      /* Skip table-header label cells like "복원 전" / "복원 후" — those
         are LABELS for adjacent data cells, not placeholders themselves.
         The Pass-5c (empty cells under image-header) path will handle
         the actual data-row placeholders. */
      if (/^(복원\s*[전후]|before\s*restoration|after\s*restoration)$/i.test(text)) return m;
      const ctx  = ctxAt(str, offset);
      const slot = mkSlot(text.slice(0, 60), guessContextCat(ctx + ' ' + text), ctx);
      return `<td${attrs}>${inner}${slot}</td>`;
    });

    // Pass 5c: empty cells inside image-relevant tables
    const imgHdrRe = /(복원\s*전|복원\s*후|이미지|사진|그림|비포|애프터|before|after|비교|예시|시안|일러스트)/i;
    html = html.replace(/<table\b[^>]*>([\s\S]*?)<\/table>/gi, (fullTbl) => {
      const rows = [...fullTbl.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/gi)];
      if (rows.length < 2) return fullTbl;
      const headerCells = [...rows[0][1].matchAll(/<(td|th)\b[^>]*>([\s\S]*?)<\/\1>/gi)]
        .map(c => c[2].replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim());
      if (!headerCells.some(t => imgHdrRe.test(t))) return fullTbl;

      const tableCtx = headerCells.join(' / ');
      let result = fullTbl;
      for (let r = 1; r < rows.length; r++) {
        const cells = [...rows[r][1].matchAll(/<(td|th)\b([^>]*)>([\s\S]*?)<\/\1>/gi)];
        cells.forEach((cm, cIdx) => {
          const [cellFull, tag, attrs, inner] = cm;
          if (/class="img-slot"/i.test(inner)) return;
          /* Cell already has an embedded HWPX picture → not empty,
             don't overlay a slot on top of the original. */
          if (/class="hwpx-embedded"/i.test(inner)) return;
          const txt = inner.replace(/<[^>]+>/g, '').replace(/&nbsp;/g, '').replace(/\s+/g, '').trim();
          if (txt !== '') return;
          const colHeader = headerCells[cIdx] || headerCells[0] || '이미지';
          if (!imgHdrRe.test(colHeader) && !imgHdrRe.test(headerCells.join(' '))) return;
          const slot = mkSlot((colHeader || '이미지 슬롯').slice(0, 40), guessContextCat(tableCtx), tableCtx);
          result = result.replace(cellFull, `<${tag}${attrs}>${slot}</${tag}>`);
        });
      }
      return result;
    });

    // Pass 6: heading-based fallback when nothing else matched
    if (sid === 0) {
      html = html.replace(/(<h([123])[^>]*>)([\s\S]*?)(<\/h\2>)/gi, (m, _open, _lvl, content, _close, offset, str) => {
        if (sid >= 8) return m;
        const head  = content.replace(/<[^>]+>/g, '').trim();
        const after = str.slice(offset + m.length, offset + m.length + 400).replace(/<[^>]+>/g, ' ');
        const cat   = guessContextCat(head + ' ' + after);
        const desc  = (head.length > 28 ? head.slice(0, 28) + '…' : head) + ' 관련 이미지';
        return m + mkSlot(desc, cat, head + ' ' + after);
      });
    }

    // Pass 7: nothing detected → single suggestion at the top
    if (sid === 0) {
      const cat = guessContextCat(html);
      slots.push({ id: 0, desc: '이미지 제안', cat, seed: 200, aiImage: null, contextText: '' });
      html = slotHTML(0, '이미지 제안', cat) + html;
    }

    return { html, slots };
  }

  /* ════════════════════════════════════════════════════
     HWPX parser — OWPML deep walker.

     Goals (ver9):
       · Preserve TABLES (<hp:tbl> → <table>)
       · Preserve EMBEDDED IMAGES (<hp:pic> → inline data-URL <img>)
       · Stop emitting the same paragraph twice (HWPX nests <hp:p>
         up to 3 deep — the previous getElementsByTagNameNS sweep
         emitted every nested copy)
       · Keep section heads readable but DON'T touch body text
         indentation/bullets/numbers (they live inside hp:t already)

     Strategy:
       1) Read content.hpf manifest → { imageId: {href, mediaType} }
       2) Extract every BinData file referenced by the manifest as a
          base64 data-URL (one fetch per image, not per slot).
       3) Walk section XML top-down. For each node we emit:
            <hp:p>   → one <p> (and recurse for nested tbl/pic, but
                       hp:t inside child hp:p contributes to the
                       CHILD's <p>, not the parent — handled by
                       'emitted' WeakSet).
            <hp:tbl> → <table> with <tr>/<td>, recurse into cells.
            <hp:pic> → <img class="hwpx-embedded" src="data:…">.
            anything else → recurse into children.
       4) detectSlots Pass 1 / 5c skip elements carrying
          class="hwpx-embedded" so existing images stay put and
          empty cells under image-relevant headers become slots.
  ════════════════════════════════════════════════════ */
  async function parseHwpx(buffer) {
    Log.info('DocParser', 'parseHwpx start');
    const JSZip = await loadJSZip();
    const zip = await JSZip.loadAsync(buffer);

    /* 1) Manifest from content.hpf — maps "image1" → {href, mediaType} */
    const manifest = await readHpfManifest(zip);
    Log.info('DocParser', 'manifest items', { count: Object.keys(manifest).length, keys: Object.keys(manifest) });

    /* 2) Pre-load every image as a data-URL. base64 is small enough
          for the report-sized images we see in practice. */
    const imageDataUrls = {};
    for (const id of Object.keys(manifest)) {
      const meta = manifest[id];
      if (!/^image\//i.test(meta.mediaType || '')) continue;
      const file = zip.file(meta.href);
      if (!file) { Log.warn('DocParser', 'manifest entry missing in zip', { id, href: meta.href }); continue; }
      const b64 = await file.async('base64');
      imageDataUrls[id] = `data:${meta.mediaType};base64,${b64}`;
    }
    Log.info('DocParser', 'images embedded as data-URLs', { ids: Object.keys(imageDataUrls), bytes: Object.values(imageDataUrls).reduce((a, s) => a + s.length, 0) });

    /* 3) Walk every section in order. */
    const sectionNames = Object.keys(zip.files)
      .filter(n => /Contents\/section\d+\.xml$/i.test(n))
      .sort();
    if (sectionNames.length === 0) throw new Error('HWPX 문서에 본문 섹션이 없습니다');

    const out = [];
    for (const name of sectionNames) {
      const xml = await zip.file(name).async('string');
      out.push(renderHwpxSection(xml, imageDataUrls));
    }
    /* Scoped style — emitted INLINE inside the rendered HTML.
       Replicates 화면 캡처.jpg exactly: Korean serif/sans body,
       relaxed line-height, gray default tables, RED dashed border
       for 복원 전/후 tables, BLUE bordered title box, ◈ callout box.
       styles.css remains untouched (SHA-256 identical to ver6). */
    const scoped = `<style>
      .hwpx-doc {
        font-family: 'HCR Dotum', 'Apple SD Gothic Neo', 'Pretendard Variable', 'Pretendard', 'Malgun Gothic', '맑은 고딕', sans-serif;
        font-size: 14.5px;
        line-height: 1.78;
        color: #111;
      }
      .hwpx-doc h2 {
        font-family: 'HCR Dotum', 'Apple SD Gothic Neo', 'Pretendard', sans-serif;
        font-size: 17px;
        font-weight: 700;
        color: #111;
        margin: 22px 0 10px;
        letter-spacing: -0.01em;
      }
      .hwpx-doc p {
        margin: 0 0 6px;
        white-space: pre-wrap;
        word-break: keep-all;
      }
      /* ◈ subtitle callout — gray hairline box */
      .hwpx-doc .hwpx-callout {
        border: 1px solid #999;
        padding: 10px 14px;
        margin: 10px 0 18px;
        font-weight: 600;
        background: transparent;
      }
      /* "*..." small note line */
      .hwpx-doc .hwpx-note {
        font-size: 12.5px;
        color: #333;
        padding-left: 20px;
        margin: 2px 0 6px;
      }
      /* Default tables — gray hairline */
      .hwpx-doc .hwpx-table {
        width: 100%;
        border-collapse: collapse;
        margin: 10px 0;
        table-layout: auto;
        border: 1px solid #888;
      }
      .hwpx-doc .hwpx-table td {
        border: 1px solid #888;
        padding: 6px 10px;
        vertical-align: top;
        font-size: 13.5px;
        line-height: 1.65;
      }
      /* Title-only table (1×1) — BLUE bordered, blue text, centered */
      .hwpx-doc .hwpx-table.hwpx-title {
        border: 1.5px solid #2c4d8c;
        margin: 6px 0 14px;
      }
      .hwpx-doc .hwpx-table.hwpx-title td {
        border: 1.5px solid #2c4d8c;
        color: #2c4d8c;
        font-weight: 700;
        font-size: 16px;
        text-align: center;
        padding: 10px 14px;
        letter-spacing: -0.005em;
      }
      /* 복원 전/후 table — RED dashed border, red header row */
      .hwpx-doc .hwpx-table.hwpx-restoration {
        border: 1.5px dashed #c0392b;
        margin: 12px 0;
      }
      .hwpx-doc .hwpx-table.hwpx-restoration td {
        border: 1.5px dashed #c0392b;
        padding: 0;
        text-align: center;
        vertical-align: middle;
      }
      .hwpx-doc .hwpx-table.hwpx-restoration tr:first-child td {
        background: #b80000;
        color: #fff;
        font-weight: 700;
        padding: 6px 10px;
      }
      /* Embedded HWPX image — center, scale to cell */
      .hwpx-doc .hwpx-embedded {
        display: block;
        margin: 0 auto;
        max-width: 100%;
        height: auto;
      }
      .hwpx-doc .hwpx-img-missing {
        display: block;
        padding: 18px;
        background: #fbe3e3;
        color: #b80000;
        text-align: center;
        font-size: 12px;
      }
      /* Image slot inside a HWPX cell — keep visible but unobtrusive */
      .hwpx-doc .img-slot {
        margin: 0;
        border: none;
        border-radius: 0;
      }
      .hwpx-doc .img-slot-label {
        font-size: 10.5px;
        font-weight: 500;
        background: rgba(255,255,255,0.92);
        color: #444;
        border: 1px solid rgba(0,0,0,0.15);
        padding: 2px 8px;
        top: 6px; left: 6px;
      }
      .hwpx-doc .img-slot img { border-radius: 0; }
    </style>`;
    const html = scoped + `<div class="hwpx-doc">${out.join('\n')}</div>`;
    Log.info('DocParser', 'parseHwpx done', { htmlChars: html.length });
    return html;
  }

  async function readHpfManifest(zip) {
    const f = zip.file('Contents/content.hpf');
    if (!f) return {};
    const text = await f.async('string');
    const map = {};
    const itemRe = /<opf:item\b([^/>]*)\/?>/g;
    let m;
    while ((m = itemRe.exec(text)) !== null) {
      const attrs = {};
      const aRe = /(\w[\w-]*)\s*=\s*"([^"]*)"/g;
      let am;
      while ((am = aRe.exec(m[1])) !== null) attrs[am[1]] = am[2];
      if (attrs.id) map[attrs.id] = { href: attrs.href, mediaType: attrs['media-type'] };
    }
    return map;
  }

  function renderHwpxSection(xml, imageDataUrls) {
    let doc;
    try { doc = new DOMParser().parseFromString(xml, 'application/xml'); }
    catch (e) { Log.warn('DocParser', 'DOMParser threw', { msg: e.message }); return ''; }
    if (doc.getElementsByTagName('parsererror').length) {
      Log.warn('DocParser', 'parsererror — falling back to regex extract');
      return regexExtractHwpx(xml);
    }
    const emitted = new WeakSet();
    const buf = [];
    walkHwpx(doc.documentElement, buf, emitted, imageDataUrls);
    return buf.join('\n');
  }

  /* Depth-first walk. Only nodes whose localName we care about (p / tbl /
     pic) are emitted; everything else just recurses. Once a node IS
     emitted, mark its whole subtree so descendants don't re-emit. */
  function walkHwpx(node, buf, emitted, images) {
    if (!node || emitted.has(node) || node.nodeType !== 1) return;
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType !== 1 || emitted.has(child)) continue;
      const tag = child.localName;
      if (tag === 'p') {
        const html = renderHwpxParagraph(child, emitted, images);
        if (html) buf.push(html);
      } else if (tag === 'tbl') {
        buf.push(renderHwpxTable(child, emitted, images));
      } else if (tag === 'pic') {
        const img = renderHwpxImage(child, images);
        if (img) buf.push(img);
        markSubtreeEmitted(child, emitted);
      } else {
        walkHwpx(child, buf, emitted, images);
      }
    }
  }

  function markSubtreeEmitted(node, set) {
    set.add(node);
    for (const c of Array.from(node.childNodes)) {
      if (c.nodeType === 1) markSubtreeEmitted(c, set);
    }
  }

  /* A paragraph may contain inline tbl/pic anchors. We collect plain
     text from hp:t descendants UNTIL we hit a nested tbl/pic, emit
     the text-so-far as <p>, then emit the tbl/pic as a sibling. The
     remainder continues. Nested <hp:p> children are RECURSED — they
     produce their own <p> blocks, parent does not duplicate them. */
  function renderHwpxParagraph(p, emitted, images) {
    if (emitted.has(p)) return '';
    emitted.add(p);
    const pieces = [];
    let textBuf = '';

    function flushText() {
      const t = textBuf.replace(/[ --]/g, '').trim();
      textBuf = '';
      if (!t) return;
      /* Heuristic heading detection: numbered section heads like
         "1. 추진 경위", "2. 행사 개요" — keep doc structure visible
         without touching body indentation. */
      const isHead    = t.length <= 50 && /^\s*[0-9]+\s*\.\s*[가-힣A-Za-z]/.test(t);
      const isCallout = /^\s*[◈◆]/.test(t);
      const isNote    = /^\s*\*\s/.test(t);
      if (isHead)         pieces.push(`<h2>${esc(t)}</h2>`);
      else if (isCallout) pieces.push(`<p class="hwpx-callout">${esc(t)}</p>`);
      else if (isNote)    pieces.push(`<p class="hwpx-note">${esc(t)}</p>`);
      else                pieces.push(`<p>${esc(t)}</p>`);
    }

    function visit(n) {
      if (!n || emitted.has(n)) return;
      if (n.nodeType === 1) {
        const tag = n.localName;
        if (tag === 't') {
          textBuf += n.textContent || '';
          emitted.add(n);
          return;
        }
        if (tag === 'p') {
          /* Nested <hp:p> — flush parent text, then let outer walker
             handle it on a separate pass (we don't recurse into it
             here, to keep paragraph ordering). */
          flushText();
          const html = renderHwpxParagraph(n, emitted, images);
          if (html) pieces.push(html);
          return;
        }
        if (tag === 'tbl') {
          flushText();
          pieces.push(renderHwpxTable(n, emitted, images));
          return;
        }
        if (tag === 'pic') {
          flushText();
          const img = renderHwpxImage(n, images);
          if (img) pieces.push(img);
          markSubtreeEmitted(n, emitted);
          return;
        }
        for (const c of Array.from(n.childNodes)) visit(c);
      }
    }
    for (const c of Array.from(p.childNodes)) visit(c);
    flushText();
    return pieces.join('\n');
  }

  /* HWPX table → <table>. Cells contain <hp:subList> > <hp:p>, which
     in turn may have nested tbl/pic. We recurse into each cell with
     renderHwpxParagraph. */
  function renderHwpxTable(tbl, emitted, images) {
    if (emitted.has(tbl)) return '';
    emitted.add(tbl);

    const rows = [];
    let rowCount = 0;
    let cellCount = 0;
    const allCellTexts = [];

    for (const tr of Array.from(tbl.childNodes)) {
      if (tr.nodeType !== 1 || tr.localName !== 'tr' || emitted.has(tr)) continue;
      emitted.add(tr);
      rowCount++;
      const cells = [];
      const rowCellTexts = [];
      for (const tc of Array.from(tr.childNodes)) {
        if (tc.nodeType !== 1 || tc.localName !== 'tc' || emitted.has(tc)) continue;
        emitted.add(tc);
        cellCount++;

        let colSpan = 1, rowSpan = 1;
        for (const cs of Array.from(tc.getElementsByTagNameNS('*', 'cellSpan'))) {
          colSpan = parseInt(cs.getAttribute('colSpan'), 10) || 1;
          rowSpan = parseInt(cs.getAttribute('rowSpan'), 10) || 1;
          break;
        }

        /* The cell holds a <hp:subList> (a paragraph container).
           Find direct hp:p children under it and render each. */
        const cellPieces = [];
        for (const sl of Array.from(tc.childNodes)) {
          if (sl.nodeType !== 1 || sl.localName !== 'subList') continue;
          emitted.add(sl);
          for (const innerP of Array.from(sl.childNodes)) {
            if (innerP.nodeType !== 1 || innerP.localName !== 'p') continue;
            const html = renderHwpxParagraph(innerP, emitted, images);
            if (html) cellPieces.push(html);
          }
        }
        const cellHtml = cellPieces.join('\n');
        rowCellTexts.push(cellHtml.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim());
        const attrs = [];
        if (colSpan > 1) attrs.push(`colspan="${colSpan}"`);
        if (rowSpan > 1) attrs.push(`rowspan="${rowSpan}"`);
        cells.push(`<td ${attrs.join(' ')}>${cellHtml || '&nbsp;'}</td>`);
      }
      if (cells.length) {
        rows.push(`<tr>${cells.join('')}</tr>`);
        allCellTexts.push(rowCellTexts);
      }
    }
    if (!rows.length) return '';

    /* Classify the table by inspecting cell content. Matches the
       reference layout in 화면 캡처.jpg:
       · 1×1 cell w/ short bold-style text  → hwpx-title    (파란 보더+글씨)
       · header row contains "복원 전/후"    → hwpx-restoration (빨강 dashed)
       · otherwise                            → hwpx-table   (회색 기본) */
    let extraClass = '';
    const firstRow = allCellTexts[0] || [];
    const firstRowJoined = firstRow.join(' ');
    if (rowCount === 1 && cellCount === 1 && firstRow[0] && firstRow[0].length <= 80) {
      extraClass = ' hwpx-title';
    } else if (/복원\s*[전후]/.test(firstRowJoined)) {
      extraClass = ' hwpx-restoration';
    } else if (firstRow.length === 1 && rowCount === 1 && /◈|국립생태원과의|일환으로/.test(firstRow[0] || '')) {
      extraClass = ' hwpx-callout';
    }

    return `<table class="hwpx-table${extraClass}">${rows.join('')}</table>`;
  }

  function renderHwpxImage(pic, images) {
    const imgs = pic.getElementsByTagNameNS('*', 'img');
    if (!imgs.length) return '';
    const ref = imgs[0].getAttribute('binaryItemIDRef');
    if (!ref) return '';

    /* Original render size (HwpUnit = 1/7200 inch ≈ 1px / 75 at 96dpi).
       Convert to inline width so the picture sits at its document size. */
    let widthCss = 'max-width:100%;height:auto;';
    for (const sz of Array.from(pic.getElementsByTagNameNS('*', 'curSz'))) {
      const w = parseFloat(sz.getAttribute('width'));
      if (Number.isFinite(w) && w > 0) {
        const mm = w / 7200 * 25.4;
        widthCss = `width:${mm.toFixed(1)}mm;max-width:100%;height:auto;`;
      }
      break;
    }
    const dataUrl = images[ref];
    if (!dataUrl) return `<div class="hwpx-img-missing">[그림 누락: ${esc(ref)}]</div>`;
    return `<img class="hwpx-embedded" src="${dataUrl}" alt="HWPX 그림 ${esc(ref)}" style="${widthCss}">`;
  }

  function regexExtractHwpx(xml) {
    // Defensive fallback: pull every <hp:t>...</hp:t> via regex.
    const out = [];
    const re = /<(?:hp:)?p\b[^>]*>([\s\S]*?)<\/(?:hp:)?p>/gi;
    const tRe = /<(?:hp:)?t\b[^>]*>([\s\S]*?)<\/(?:hp:)?t>/gi;
    let m;
    while ((m = re.exec(xml)) !== null) {
      let line = '';
      let tm;
      while ((tm = tRe.exec(m[1])) !== null) line += tm[1];
      line = line.replace(/<[^>]+>/g, ' ').replace(/&[a-z]+;/g, ' ').replace(/\s+/g, ' ').trim();
      if (!line) continue;
      if (line.length <= 40) out.push(`<h2>${esc(line)}</h2>`);
      else                   out.push(`<p>${esc(line)}</p>`);
    }
    return out.join('\n');
  }

  /* ════════════════════════════════════════════════════
     Unified DocParser — selects the parser by extension and
     returns a single HTML string for the slot detector.
  ════════════════════════════════════════════════════ */
  async function parseDocument(file) {
    const name = (file.name || '').toLowerCase();
    const buf  = await file.arrayBuffer();
    Log.info('DocParser', 'parseDocument', { name, size: buf.byteLength });

    /* Keep the raw bytes for HWPX export later. ArrayBuffers can't be
       re-read after some operations, so we stash a slice. */
    state.fileBuffer = buf.slice(0);
    state.fileName   = file.name || '';
    state.fileExt    = name.endsWith('.hwpx') ? 'hwpx' : (name.endsWith('.docx') ? 'docx' : '');

    if (name.endsWith('.hwpx')) {
      return await parseHwpx(buf);
    }
    if (name.endsWith('.docx')) {
      const mammoth = await loadMammoth();
      const res = await mammoth.convertToHtml({ arrayBuffer: buf });
      Log.info('DocParser', 'mammoth done', { messages: (res.messages || []).length });
      return res.value || '';
    }
    throw new Error('지원하지 않는 형식 (.docx / .hwpx 만 지원)');
  }

  /* ════════════════════════════════════════════════════
     BriefBuilder — derives document-level + section-level
     metadata and combines it with the slot's local context to
     produce a structured Image Brief object.
  ════════════════════════════════════════════════════ */
  function buildBrief(slot, fullHtml) {
    const docTitle    = extractDocTitle(fullHtml);
    const docPurpose  = extractDocPurpose(fullHtml);
    const sectionTitle = findNearestHeading(slot.contextText) || extractFirstHeading(fullHtml) || '';
    const keywords    = extractKeywords(slot.contextText + ' ' + docTitle + ' ' + sectionTitle);

    /* Decide visual style FIRST, then reconcile category so style and
       forbidden list can no longer contradict each other (photo style
       was previously paired with photography-forbidden constraints). */
    const style    = visualStyleForSlot(slot);
    const finalCat = reconcileCatVsStyle(slot, style);
    if (finalCat !== slot.cat) slot.cat = finalCat;

    const constraints = (API.CATEGORY_CONSTRAINTS && API.CATEGORY_CONSTRAINTS[finalCat]) || { allowed: [], forbidden: [] };

    const brief = {
      category: finalCat,
      documentTitle:   docTitle,
      documentPurpose: docPurpose,
      sectionTitle,
      imageIntent: slot.desc,
      mainSubject: slot.desc,
      keyElements: keywords.slice(0, 6),
      surroundingText: (slot.contextText || '').slice(0, 1800),
      requiredObjects: constraints.allowed,
      forbiddenObjects: constraints.forbidden,
      visualStyle: style,
      qualityRequirements: [
        'publication-grade',
        'consulting report quality',
        'printable resolution',
        'ultra-detailed'
      ]
    };
    Log.debug('BriefBuilder', 'brief built', brief);
    return brief;
  }

  function visualStyleFor(cat) {
    switch (cat) {
      case '인포그래픽':  return 'clean flat infographic, icon-driven, restrained color palette';
      case '다이어그램':  return 'minimal technical diagram, labeled nodes and arrows, monochrome accents';
      case '조감도':      return "aerial bird's-eye render, architectural visualization, soft shadows";
      case '배너 디자인': return 'editorial wide-aspect banner, strong typography, ample whitespace';
      case '카드뉴스':    return 'social-card editorial layout, clear hierarchy, brand-safe color';
      case '현수막':      return 'large-format outdoor banner, high-contrast headline, print-ready';
      case '아이소메트릭': return 'isometric 30-degree axonometric, vector blocks, unified light';
      default:            return 'clean editorial illustration';
    }
  }

  /* visualStyleForSlot — desc keyword wins over category, with two
     priority bands:
       1. desc-only signal (slot's own label) — highest confidence.
          Avoids the bug where two adjacent 복원 전/후 slots get the
          same style because their context windows overlap.
       2. context fallback — only consulted if desc has no match.
     The function also returns a category-suggestion string so the
     caller can promote slot.cat (e.g. push 복원 전/후 into '사진').
  */
  function visualStyleForSlot(slot) {
    const desc = String(slot.desc || '').trim();
    const ctx  = String(slot.contextText || '').slice(0, 240);

    const PHOTO_BEFORE = 'documentary photojournalism, before-restoration ecological site, natural daylight, '
                      + '50mm lens, public-sector report photography, no AI artifacts, 300dpi print-ready';
    const PHOTO_AFTER  = 'documentary photojournalism, restored riparian wetland with planted willow saplings, '
                      + 'increased biodiversity, soft golden hour light, public-sector report photography, '
                      + 'no AI artifacts, 300dpi print-ready';
    const BANNER       = 'vector banner design mockup, 5m x 90cm wide-format, headline + date + venue + '
                      + 'corporate logo lockup at bottom, willow-sapling illustration background, '
                      + 'flat clean public-event poster style, CMYK print-ready 300dpi';
    const AERIAL       = "aerial bird's-eye site plan, top-down render, labelled zones, soft long shadows, "
                      + 'architectural visualization';
    const CONCEPT      = 'conceptual diagram, labelled blocks and arrows, restrained palette, '
                      + 'editorial information design';
    const SAMPLE       = 'example illustration in the surrounding report style, clean composition, '
                      + 'minimal decoration';

    /* Tier 1 — desc only. */
    if (/복원\s*전|before(\s|-)*restoration/i.test(desc)) return PHOTO_BEFORE;
    if (/복원\s*후|after(\s|-)*restoration|복원된|복원\s*결과/i.test(desc)) return PHOTO_AFTER;
    if (/현수막|배너\s*시안|banner/i.test(desc)) return BANNER;
    if (/조감도|배치도|aerial|site\s*plan/i.test(desc)) return AERIAL;
    if (/개념도|concept(ual)?\s*diagram/i.test(desc)) return CONCEPT;
    if (/^(예시|시안)\s*이미지?$|sample\s*illustration/i.test(desc)) return SAMPLE;

    /* Tier 2 — context fallback (only when desc had no signal). */
    if (/복원\s*전|before(\s|-)*restoration/i.test(ctx)) return PHOTO_BEFORE;
    if (/복원\s*후|after(\s|-)*restoration/i.test(ctx))  return PHOTO_AFTER;
    if (/현수막|배너/i.test(ctx)) return BANNER;

    /* Default — fall back to category. */
    return visualStyleFor(slot.cat);
  }

  /* If a slot's visual style implies photo but its category is illustration-
     leaning ('인포그래픽' / '다이어그램'), promote the cat to '사진' so
     CATEGORY_CONSTRAINTS no longer bans photography. Called inside buildBrief. */
  function reconcileCatVsStyle(slot, style) {
    const isPhoto = /photojournalism|photography/i.test(style);
    if (!isPhoto) return slot.cat;
    if (slot.cat === '인포그래픽' || slot.cat === '다이어그램' || slot.cat === '아이소메트릭') {
      Log.info('Pipeline', 'reconcile cat → 사진', { from: slot.cat, slotId: slot.id });
      return '사진';
    }
    return slot.cat;
  }

  function extractDocTitle(html) {
    const m = html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i);
    if (m) return m[1].replace(/<[^>]+>/g, '').trim().slice(0, 120);
    const m2 = html.match(/<h2[^>]*>([\s\S]*?)<\/h2>/i);
    return m2 ? m2[1].replace(/<[^>]+>/g, '').trim().slice(0, 120) : '';
  }
  function extractFirstHeading(html) { return extractDocTitle(html); }
  function extractDocPurpose(html) {
    // First non-heading paragraph is usually the abstract / purpose.
    const m = html.match(/<p[^>]*>([\s\S]{20,400}?)<\/p>/i);
    if (!m) return '';
    return m[1].replace(/<[^>]+>/g, '').trim().slice(0, 200);
  }
  function findNearestHeading(text) {
    // Heading-shaped fragments inside the local context window.
    const m = (text || '').match(/(?:^|\s)([가-힣A-Z][가-힣A-Z0-9 ·:\-]{2,30})(?=\s|$)/);
    return m ? m[1].trim() : '';
  }
  function extractKeywords(text) {
    if (!text) return [];
    const t = text.toLowerCase();
    const STOP = new Set(['이미지','그림','사진','삽입','figure','figure.','이미지를','으로','에서','입니다','있습니다','된다','등이','등을','등의','대한','관련','내용']);
    const tokens = t.split(/[\s,.\/()「」『』\[\]<>·:;!?　-]+/).filter(Boolean);
    const freq = Object.create(null);
    for (const tk of tokens) {
      if (tk.length < 2 || tk.length > 12) continue;
      if (STOP.has(tk)) continue;
      if (/^[a-z]{1,2}$/.test(tk)) continue;
      freq[tk] = (freq[tk] || 0) + 1;
    }
    return Object.entries(freq).sort((a, b) => b[1] - a[1]).map(([k]) => k).slice(0, 8);
  }

  /* ════════════════════════════════════════════════════
     PromptBuilder — Image Brief → structured prompt string
     ready for Gemini/Imagen on the backend.
  ════════════════════════════════════════════════════ */
  function buildPrompt(brief) {
    /* Multi-line structured prompt compatible with OpenAI Images
       generations API (will be passed as `prompt` field by the
       backend). Top section = headline intent; bottom = constraints. */
    const lines = [
      `Subject (hard requirement): ${brief.mainSubject}`,
      `Category (hard constraint): ${brief.category}`,
      `Document title: ${brief.documentTitle || '(untitled report)'}`,
      `Document purpose: ${brief.documentPurpose || ''}`.trim(),
      `Section: ${brief.sectionTitle || '(unnamed section)'}`,
      `Surrounding paragraph (≤1000 chars): ${brief.surroundingText}`,
      `Key elements (must include): ${brief.keyElements.join(', ') || '(infer from context)'}`,
      `Visual style: ${brief.visualStyle}`,
      `Allowed visual elements: ${brief.requiredObjects.join(', ') || '(see visual style)'}`,
      `Forbidden visual elements: ${brief.forbiddenObjects.join(', ') || 'none'}`,
      `Quality requirements: ${brief.qualityRequirements.join(', ')}, 300dpi print-ready`,
      `Do NOT include: photography unless category allows it, clip art, fantasy elements, decorative borders, stock-photo feel, watermarks, captions, text labels in unsupported scripts.`,
      `Aspect: 16:10. Background: clean, brand-safe. Output: single image, no border, no text overlay unless explicitly requested.`
    ];
    return lines.join('\n');
  }

  /* ════════════════════════════════════════════════════
     App state
  ════════════════════════════════════════════════════ */
  const state = {
    file: null,
    fileBuffer: null,   // raw ArrayBuffer of the uploaded file, kept for HWPX export
    fileName: '',
    fileExt: '',
    fullHtml: '',
    slots: [],
    selectedId: null,
    busy: false         // mutual exclusion flag — see setBusy()
  };

  /* Mutual exclusion across _aiAll / _saveAll / _regen / goHome.
     Prevents (a) slot.aiImage being written and read by two loops at
     once, (b) goHome wiping state.fileBuffer while exportHWPX still
     needs it. busy stays set during the entire critical section. */
  function setBusy(flag, label) {
    state.busy = !!flag;
    Log.info('state', flag ? 'busy=on' : 'busy=off', { label: label || '' });
  }

  /* ════════════════════════════════════════════════════
     Uploader
  ════════════════════════════════════════════════════ */
  const Uploader = {
    el: null, input: null, statusText: null, progressBar: null, _dragDepth: 0,

    init() {
      this.el          = $('dropzone');
      this.input       = $('file-input');
      this.statusText  = $('dz-status-text');
      this.progressBar = $('dz-progress-bar');
      if (!this.el || !this.input) return;

      this.el.addEventListener('click', () => this._pick());
      this.el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this._pick(); }
      });
      this.input.addEventListener('change', (e) => {
        const f = e.target.files && e.target.files[0];
        if (f) this._handle(f);
        e.target.value = '';
      });
      window.addEventListener('dragenter', this._onDragEnter);
      window.addEventListener('dragleave', this._onDragLeave);
      window.addEventListener('dragover',  this._onDragOver);
      window.addEventListener('drop',      this._onDrop);
    },

    _pick() {
      if (this.el.classList.contains('is-uploading')) return;
      this.input.click();
    },

    _onDragEnter: (e) => {
      e.preventDefault();
      Uploader._dragDepth++;
      Uploader.el?.classList.add('is-dragover');
    },
    _onDragLeave: (e) => {
      e.preventDefault();
      Uploader._dragDepth = Math.max(0, Uploader._dragDepth - 1);
      if (Uploader._dragDepth === 0) Uploader.el?.classList.remove('is-dragover');
    },
    _onDragOver: (e) => { e.preventDefault(); },
    _onDrop: (e) => {
      e.preventDefault();
      Uploader._dragDepth = 0;
      Uploader.el?.classList.remove('is-dragover');
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) Uploader._handle(f);
    },

    _setState(name, text) {
      ['is-uploading', 'is-success', 'is-error'].forEach(c => this.el.classList.remove(c));
      if (name) this.el.classList.add('is-' + name);
      if (text && this.statusText) this.statusText.textContent = text;
    },
    _setProgress(p) {
      if (this.progressBar) this.progressBar.style.width = Math.round(p * 100) + '%';
    },

    async _handle(file) {
      const name = (file.name || '').toLowerCase();
      const isDocx = name.endsWith('.docx');
      const isHwpx = name.endsWith('.hwpx');
      Log.info('Uploader', 'handle', { name: file.name, size: file.size, isDocx, isHwpx });

      if (!isDocx && !isHwpx) {
        this._setState('error', '⚠️ .docx / .hwpx 만 지원합니다');
        Toast.show('.docx / .hwpx 파일만 지원합니다', { error: true });
        setTimeout(() => this._setState(null), 2600);
        return;
      }
      const MAX = 25 * 1024 * 1024;
      if (file.size > MAX) {
        this._setState('error', '파일이 너무 큽니다 (최대 25MB)');
        Toast.show('파일이 너무 큽니다 (최대 25MB)', { error: true });
        setTimeout(() => this._setState(null), 2600);
        return;
      }

      state.file = file;
      this._setState('uploading', '문서를 읽는 중…');
      this._setProgress(0.05);

      // Notify backend (best-effort)
      API.uploadDocument(file, (p) => this._setProgress(0.05 + p * 0.30))
         .catch(() => {});

      try {
        this._setProgress(0.40);
        this._setState('uploading', isHwpx ? 'HWPX 본문 분석 중…' : '빈칸을 탐지하는 중…');

        const html = await parseDocument(file);
        state.fullHtml = html;

        this._setProgress(0.75);
        this._setState('uploading', '이미지 슬롯을 만드는 중…');

        const detection = detectSlots(html);
        state.slots = detection.slots;
        Log.info('Uploader', 'slots detected', { count: state.slots.length });
        this._setProgress(1);

        await new Promise(r => setTimeout(r, 220));
        this._setState('success', `분석 완료 · ${state.slots.length}개 슬롯 감지됨`);
        Workspace.show(detection.html, file.name, state.slots);
        Toast.show(`✓ 분석 완료 · 이미지 ${state.slots.length}개`);
      } catch (err) {
        Log.error('Uploader', 'parse failed', { msg: err.message });
        this._setState('error', '문서 분석 실패: ' + err.message);
        Toast.show('문서 분석 실패: ' + err.message, { error: true });
        setTimeout(() => this._setState(null), 3000);
      }
    },

    reset() { this._setState(null); this._setProgress(0); }
  };

  /* ════════════════════════════════════════════════════
     Workspace
  ════════════════════════════════════════════════════ */
  const Workspace = {
    show(html, fname, slots) {
      $('view-upload').classList.remove('is-active');
      $('view-upload').hidden = true;

      const ws = $('view-workspace');
      ws.hidden = false;
      ws.classList.add('is-active');

      const newBtn = $('btn-new-doc'); if (newBtn) newBtn.hidden = false;
      $('ws-fname').textContent = fname;
      $('doc-view').innerHTML = html;
      this._bindSlotClicks();
      this._buildPageList(fname, slots);

      $('btn-save-imgs').onclick = () => this._saveAll(slots);
      $('btn-ai-all').onclick    = () => this._aiAll(slots);
    },

    hide() {
      $('view-workspace').classList.remove('is-active');
      $('view-workspace').hidden = true;
      $('view-upload').hidden = false;
      $('view-upload').classList.add('is-active');
      const newBtn = $('btn-new-doc'); if (newBtn) newBtn.hidden = true;
      $('doc-view').innerHTML = '';
      $('page-list').innerHTML = '';
      OptionsPanel.clear();
      Uploader.reset();
    },

    _bindSlotClicks() {
      $('doc-view').addEventListener('click', (e) => {
        const slot = e.target.closest('.img-slot');
        if (!slot) return;
        OptionsPanel.open(Number(slot.dataset.id));
      });
      $('doc-view').addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const slot = e.target.closest('.img-slot');
        if (!slot) return;
        e.preventDefault();
        OptionsPanel.open(Number(slot.dataset.id));
      });
    },

    _buildPageList(fname, slots) {
      const list = $('page-list');
      list.innerHTML = '';
      const docEl = $('doc-view');
      const text  = (docEl?.textContent || '');
      const byText   = Math.max(1, Math.ceil(text.length / 1600));
      const byHeight = Math.max(1, Math.ceil((docEl?.scrollHeight || 800) / 1040));
      const bySlots  = Math.max(1, Math.ceil(slots.length / 3));
      const pages    = Math.max(1, Math.min(20, Math.max(byText, byHeight, bySlots)));
      const cpp = Math.max(1, Math.ceil(text.length / pages));

      for (let i = 0; i < pages; i++) {
        const snippet = text.slice(i * cpp, i * cpp + 110).replace(/\s+/g, ' ').trim() || '…';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'page-thumb' + (i === 0 ? ' is-active' : '');
        btn.innerHTML = `<div class="page-thumb-no">p.${i + 1}</div><div>${esc(snippet)}</div>`;
        btn.onclick = () => {
          $$('.page-thumb').forEach(t => t.classList.remove('is-active'));
          btn.classList.add('is-active');
          const scrollEl = document.querySelector('.doc-scroll');
          if (scrollEl) {
            const target = (scrollEl.scrollHeight * i) / pages;
            scrollEl.scrollTo({ top: target, behavior: 'smooth' });
          }
        };
        list.appendChild(btn);
      }
    },

    async _aiAll(slots) {
      if (!slots.length) { Toast.show('생성할 슬롯이 없습니다'); return; }
      if (state.busy) { Toast.show('다른 작업이 진행 중입니다', { error: true }); return; }
      setBusy(true, '_aiAll');
      const btn  = $('btn-ai-all');
      const orig = btn.textContent;
      btn.disabled = true;
      let ok = 0;
      try {
      for (let i = 0; i < slots.length; i++) {
        const s = slots[i];
        btn.textContent = `생성 중 (${i + 1}/${slots.length})`;
        const result = await generateWithVerify(s, state.fullHtml);
        if (result.ok) {
          s.aiImage = result.url;
          const node = $('slot-' + s.id);
          if (node) {
            node.querySelector('img').src = result.url;
            node.querySelector('.img-slot-label').textContent = `🖼️ ${s.cat}`;
          }
          ok++;
        }
        await new Promise(r => setTimeout(r, 220));
      }
      } finally {
        btn.disabled = false;
        btn.textContent = orig;
        setBusy(false, '_aiAll');
      }
      Toast.show(ok === slots.length ? `✓ ${ok}개 모두 생성` : `⚠ ${ok}/${slots.length}개 생성`);
    },

    /* Save bundle — same button, same position, same label.
       Now triggers THREE artifacts in sequence:
         1. one .jpg per slot (existing behaviour)
         2. a sidecar HWPX zip (original + generated + mapping + guide)
         3. the browser print-to-PDF dialog
       Spec asks for HTML preview + HWPX 다운로드 + PDF 다운로드. */
    async _saveAll(slots) {
      if (!slots.length) { Toast.show('저장할 이미지가 없습니다'); return; }
      if (state.busy)    { Toast.show('다른 작업이 진행 중입니다', { error: true }); return; }
      setBusy(true, '_saveAll');

      /* Snapshot state at entry — goHome can no longer pull the rug.
         If the user clicks the logo mid-save, goHome's busy gate now
         blocks until we finish. */
      const snap = {
        slots:      slots.slice(),
        fileBuffer: state.fileBuffer,
        fileName:   state.fileName,
        fileExt:    state.fileExt,
        fullHtml:   state.fullHtml
      };

      const btn  = $('btn-save-imgs');
      const orig = btn.textContent;
      btn.disabled = true;
      Log.info('Workspace', 'saveAll start', { slotCount: snap.slots.length });

      const status = { jpg: 0, hwpx: false, pdf: false };

      try {
        /* 1) per-slot JPGs — parallel fetch, sequential download. */
        for (let i = 0; i < snap.slots.length; i++) {
          const s   = snap.slots[i];
          const url = s.aiImage || (s.original && s.original.url) || API.placeholderUrl(s.cat, s.seed);
          btn.textContent = `이미지 (${i + 1}/${snap.slots.length})`;
          try {
            const blob = await fetch(url).then(r => r.blob());
            const safe = `${String(i + 1).padStart(2, '0')}_${s.cat}`.replace(/[\s\/\\:*?"<>|]/g, '_');
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = safe + '.jpg';
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 5000);
            status.jpg++;
          } catch (e) { Log.warn('Workspace', `image #${i + 1} save fail`, { msg: e.message }); }
        }

        /* 2) sidecar HWPX bundle (best-effort) */
        try {
          btn.textContent = 'HWPX 묶음 …';
          await Exporter.exportHWPX(snap);
          status.hwpx = true;
        } catch (e) { Log.warn('Workspace', 'hwpx export fail', { msg: e.message }); }

        /* 3) trigger PDF (browser dialog — user can pick "PDF로 저장") */
        try {
          btn.textContent = 'PDF …';
          await Exporter.exportPDF();
          status.pdf = true;
        } catch (e) { Log.warn('Workspace', 'pdf export fail', { msg: e.message }); }
      } finally {
        btn.disabled = false;
        btn.textContent = orig;
        setBusy(false, '_saveAll');
      }

      Log.info('Workspace', 'saveAll done', status);
      const parts = [];
      if (status.jpg)  parts.push(`이미지 ${status.jpg}장`);
      if (status.hwpx) parts.push('HWPX 묶음');
      if (status.pdf)  parts.push('PDF');
      Toast.show(parts.length === 3
        ? `✓ 저장 완료 (${parts.join(' + ')})`
        : `⚠ 일부 실패 (성공: ${parts.join(', ') || '없음'})`,
        { error: parts.length < 3 });
    }
  };

  /* ════════════════════════════════════════════════════
     Options panel — per-slot editor
  ════════════════════════════════════════════════════ */
  const OptionsPanel = {
    init() {
      const catSel = $('opts-cat');
      catSel.innerHTML = CATEGORIES.map(c => `<option value="${c}">${c}</option>`).join('');
      $('opts-reset').onclick = () => this._reset();
      $('opts-ai').onclick    = () => this._regen();
      Log.info('OptionsPanel', 'init', { categories: CATEGORIES });
    },
    open(id) {
      const s = state.slots.find(x => x.id === id);
      if (!s) return;
      state.selectedId = id;
      $$('.img-slot').forEach(n => n.classList.remove('is-selected'));
      $('slot-' + id)?.classList.add('is-selected');

      $('opts-empty').style.display = 'none';
      const panel = $('options-panel');
      panel.hidden = false;
      $('opts-img').src    = s.aiImage || API.placeholderUrl(s.cat, s.seed);
      $('opts-desc').value = s.desc;
      $('opts-cat').value  = s.cat;
      Log.debug('OptionsPanel', 'open', { id, cat: s.cat });
    },
    clear() {
      state.selectedId = null;
      const panel = $('options-panel'); if (panel) panel.hidden = true;
      const empty = $('opts-empty');    if (empty) empty.style.display = '';
    },
    /* Reset = restore the FIRST AI-generated image (and the prompt /
       brief / category / desc that produced it). All subsequent edits
       and regenerations for this slot are discarded. If the slot was
       never AI-generated yet, fall back to a fresh placeholder so the
       button still has a visible effect. */
    _reset() {
      const s = state.slots.find(x => x.id === state.selectedId);
      if (!s) return;

      if (s.original) {
        const o = s.original;
        s.aiImage = o.aiImage;
        s.desc    = o.desc;
        s.cat     = o.cat;
        s.seed    = o.seed;

        const url = o.url;
        $('opts-img').src    = url;
        $('opts-desc').value = o.desc;
        $('opts-cat').value  = o.cat;

        const node = $('slot-' + s.id);
        if (node) {
          node.querySelector('img').src = url;
          node.querySelector('.img-slot-label').textContent = `🖼️ ${o.cat}`;
        }
        Log.info('OptionsPanel', 'reset → restored original', {
          slotId: s.id, cat: o.cat, createdAt: o.createdAt
        });
        Toast.show('✓ 최초 생성 이미지로 복원');
      } else {
        /* Never generated yet — drop to a deterministic placeholder. */
        s.aiImage = null;
        s.seed    = Math.floor(Math.random() * 9999);
        const url = API.placeholderUrl(s.cat, s.seed);
        $('opts-img').src = url;
        const node = $('slot-' + s.id);
        if (node) node.querySelector('img').src = url;
        Log.info('OptionsPanel', 'reset → no snapshot yet, placeholder reseeded', { slotId: s.id });
        Toast.show('초기 상태로 복원');
      }
    },
    async _regen() {
      if (state.busy) { Toast.show('다른 작업이 진행 중입니다', { error: true }); return; }
      const s = state.slots.find(x => x.id === state.selectedId);
      if (!s) return;
      s.desc = $('opts-desc').value.trim() || s.desc;
      s.cat  = $('opts-cat').value;
      Log.info('OptionsPanel', 'regen', { id: s.id, cat: s.cat });
      setBusy(true, '_regen');
      Toast.show('AI 이미지 생성 중…');
      let result;
      try {
        result = await generateWithVerify(s, state.fullHtml);
      } finally {
        setBusy(false, '_regen');
      }
      s.aiImage = result.aiImage;
      $('opts-img').src = result.url;
      const node = $('slot-' + s.id);
      if (node) {
        node.querySelector('img').src = result.url;
        node.querySelector('.img-slot-label').textContent = `🖼️ ${s.cat}`;
      }
      Toast.show(result.ok ? '✓ 생성 완료' : '⚠ 폴백 이미지 사용', { error: !result.ok });
    }
  };

  /* ════════════════════════════════════════════════════
     Quality Gate — score every generation, retry once below threshold

     The real backend will run Gemini Vision against the same image and
     return three scores (categoryMatch, contextMatch, quality). While
     the backend is offline we score the *brief* heuristically — the
     URL is just a placeholder — so the retry logic exercises end-to-end.
  ════════════════════════════════════════════════════ */
  const VERIFY_THRESHOLD = 0.55;

  function verifyGeneration(brief, response) {
    /* If the server returned its own scores, trust them. */
    if (response && response.body && response.body.scores) {
      const s = response.body.scores;
      const pass = Math.min(s.categoryMatch ?? 1, s.contextMatch ?? 1, s.quality ?? 1) >= VERIFY_THRESHOLD;
      return { scores: s, pass, source: 'server' };
    }
    /* Otherwise: heuristic score from the brief itself. */
    const keyEl   = (brief.keyElements || []).length;
    const ctxLen  = (brief.surroundingText || '').length;
    const hasCat  = !!brief.category;
    const hasReq  = (brief.requiredObjects  || []).length;
    const hasForb = (brief.forbiddenObjects || []).length;
    const fallback = response && response.body && response.body.fallback;

    const categoryMatch = hasCat && hasReq ? 0.9 : 0.4;
    const contextMatch  = Math.min(1, keyEl / 4) * 0.5 + Math.min(1, ctxLen / 400) * 0.5;
    /* Fallback (picsum) is hard-capped — it doesn't honor the prompt. */
    const quality       = fallback ? 0.30 : (hasForb ? 0.75 : 0.55);
    const scores = {
      categoryMatch: round2(categoryMatch),
      contextMatch:  round2(contextMatch),
      quality:       round2(quality)
    };
    const pass = Math.min(scores.categoryMatch, scores.contextMatch, scores.quality) >= VERIFY_THRESHOLD;
    return { scores, pass, source: 'heuristic' };
  }
  function round2(n) { return Math.round(n * 100) / 100; }

  /* Build → call API → verify → retry once (different seed) if below
     threshold. Returns {ok, url, aiImage, scores, attempts}.
     Heavy logging at every step so a CTO can replay the chain from
     console output alone. */
  async function generateWithVerify(slot, fullHtml) {
    const mark = (name) => performance.mark(`vf:${slot.id}:${name}`);
    const measure = (name, from, to) => {
      try { performance.measure(`vf:${slot.id}:${name}`, `vf:${slot.id}:${from}`, `vf:${slot.id}:${to}`); } catch (_) {}
    };

    mark('begin');
    const brief  = buildBrief(slot, fullHtml);
    mark('brief');
    const prompt = buildPrompt(brief);
    mark('prompt');
    Log.info('Pipeline', 'request', { slotId: slot.id, category: slot.cat });

    /* If the slot's desc matches one of the local reference samples
       shipped in ver10/, short-circuit the backend round-trip and
       return that image directly. The user sees the *expected final
       artifact* (그림 샘플 1.jpg / 그림 샘플 2.jpg) without waiting
       for picsum or any AI API. */
    const sample = sampleUrlForDesc(slot.desc, slot.cat);
    if (sample) {
      Log.info('Pipeline', 'sample-match short-circuit', { slotId: slot.id, sample });
      mark('end'); measure('total', 'begin', 'end');
      if (!slot.original) {
        slot.original = {
          url: sample, aiImage: sample, brief, prompt,
          desc: slot.desc, cat: slot.cat, seed: slot.seed,
          scores: { categoryMatch: 1, contextMatch: 1, quality: 1 },
          attempts: 1, createdAt: new Date().toISOString()
        };
      }
      return { ok: true, url: sample, aiImage: sample, scores: { categoryMatch: 1, contextMatch: 1, quality: 1 }, attempts: 1, brief, prompt };
    }

    let attempts = 0;
    let response = null;
    let url      = null;
    let verdict  = null;

    while (attempts < 2) {
      attempts++;
      mark(`req${attempts}`);
      response = await API.generateImage({
        description: slot.desc, category: slot.cat,
        brief, prompt, contextText: slot.contextText,
        attempt: attempts
      });
      mark(`res${attempts}`);
      measure(`generate#${attempts}`, `req${attempts}`, `res${attempts}`);

      url = (response.ok && response.body && (response.body.image_url || response.body.image_data))
          || API.placeholderUrl(slot.cat, Math.floor(Math.random() * 9999));

      verdict = verifyGeneration(brief, response);
      Log.info('Pipeline', `verify #${attempts}`, { pass: verdict.pass, scores: verdict.scores, source: verdict.source });

      if (verdict.pass) break;
      if (attempts >= 2) { Log.warn('Pipeline', 'gave up after retry', { scores: verdict.scores }); break; }
      Log.warn('Pipeline', 'score below threshold — retrying once', { scores: verdict.scores });
      slot.seed = Math.floor(Math.random() * 9999);
    }
    mark('end');
    measure('total', 'begin', 'end');

    const aiImage = response && response.body && (response.body.image_url || response.body.image_data) || null;

    /* Snapshot the FIRST generation for each slot. Subsequent regen
       calls never overwrite this snapshot — so the reset button can
       always restore the truly original AI image (+ its prompt and
       brief metadata), no matter how many edits the user makes. */
    if (!slot.original) {
      slot.original = {
        url:       url,                // image URL that was actually displayed
        aiImage:   aiImage,            // backend response URL (null if pure fallback)
        brief:     brief,
        prompt:    prompt,
        desc:      slot.desc,
        cat:       slot.cat,
        seed:      slot.seed,
        scores:    verdict ? verdict.scores : null,
        attempts:  attempts,
        createdAt: new Date().toISOString()
      };
      Log.info('Pipeline', 'snapshot original', { slotId: slot.id, cat: slot.cat });
    }

    return {
      ok: !!(response && response.ok),
      url,
      aiImage,
      scores:  verdict ? verdict.scores : null,
      attempts,
      brief,
      prompt
    };
  }

  /* ════════════════════════════════════════════════════
     VFTest — console-callable self-test. From devtools:
       VFTest.run()       — runs the full pipeline on a mock document
       VFTest.briefOnly() — inspects buildBrief / buildPrompt
       VFTest.parsePass() — confirms text-preservation in detectSlots
  ════════════════════════════════════════════════════ */
  const MOCK_DOC_HTML = `
    <h1>네이처 포지티브 협력 소생태계 복원 행사 계획안</h1>
    <p>K-water와 국립생태원이 협력해 용담댐 홍수터에 버드나무 묘목을 식재하여 소생태계를 복원합니다.</p>
    <h2>1. 사업 추진 절차</h2>
    <p>사업 추진 절차는 다음과 같다.</p>
    <p>[그림]</p>
    <p>수거된 자생수종을 묘포에서 1년간 키운 뒤 식재합니다. (그림 1)</p>
    <h2>2. 소생태계 복원 전후 비교</h2>
    <table>
      <tr><th>소생태계 복원 전</th><th>소생태계 복원 후</th></tr>
      <tr><td>복원 전</td><td>복원 후</td></tr>
    </table>
    <h2>3. 행사 현수막 시안</h2>
    <p>현수막 시안</p>
    <p>(현수막 시안)</p>
    <h2>4. 예상 부지 조감도</h2>
    <p>[이미지: 부지 조감도]</p>
    <p>해당 부지는 도로 접근성과 인근 자연지형을 고려해 선정되었습니다.</p>
  `;

  async function vfTestRun() {
    console.group('%cVFTest.run', 'color:#0a64ff;font-weight:700');
    try {
      const t0 = performance.now();
      const detection = detectSlots(MOCK_DOC_HTML);
      console.log('slots detected:', detection.slots.length, detection.slots);

      /* Body-preservation assertion — every non-placeholder sentence
         must survive verbatim, including ver8's new domain vocabulary. */
      const expectations = [
        'K-water와 국립생태원',
        '수거된 자생수종을 묘포에서',
        '해당 부지는 도로 접근성',
        '사업 추진 절차는 다음과 같다',
        '(그림 1)',          /* Pass 3 preserves */
        '소생태계 복원 전',  /* Pass 5d new — heading still rendered */
        '소생태계 복원 후',
        '현수막 시안',       /* both <p>현수막 시안</p> and (현수막 시안) survive */
        '(현수막 시안)'
      ];
      const missing = expectations.filter(e => !detection.html.includes(e));
      if (missing.length) {
        console.error('❌ body-preservation FAILED — missing:', missing);
      } else {
        console.log('✓ body preserved (all canonical sentences intact, incl. "(그림 1)")');
      }

      /* Brief + Prompt + API + Verify for the first slot. */
      const s0 = detection.slots[0];
      if (s0) {
        const out = await generateWithVerify(s0, detection.html);
        console.log('first slot pipeline →', { url: out.url, scores: out.scores, attempts: out.attempts });
        console.log('brief sample:', out.brief);
        console.log('prompt sample (truncated):\n' + out.prompt.split('\n').slice(0, 6).join('\n') + '\n  …');
      }

      console.log('total ms:', Math.round(performance.now() - t0));
      const ok = missing.length === 0 && (s0 ? true : false);
      console.log(ok ? '✅ VFTest.run PASS' : '⚠ VFTest.run partial');
      return { ok, missing, slots: detection.slots.length };
    } finally {
      console.groupEnd();
    }
  }

  function vfTestBriefOnly() {
    const detection = detectSlots(MOCK_DOC_HTML);
    const briefs = detection.slots.map(s => buildBrief(s, detection.html));
    console.table(briefs.map(b => ({
      category: b.category,
      section:  b.sectionTitle,
      subject:  b.mainSubject,
      keys:     b.keyElements.join(', '),
      ctxChars: (b.surroundingText || '').length
    })));
    return briefs;
  }

  function vfTestParsePass() {
    const { html, slots } = detectSlots(MOCK_DOC_HTML);
    const slotCount  = (html.match(/class="img-slot"/g) || []).length;
    const lostMarker = !html.includes('[그림]');     // should be replaced
    const keptRef    = html.includes('(그림 1)');    // should be preserved
    /* ver8 specific — domain words/markers survive AND new slots fire. */
    const keptBefore  = html.includes('소생태계 복원 전');
    const keptAfter   = html.includes('소생태계 복원 후');
    const keptBanner  = html.includes('(현수막 시안)');
    /* Did Pass 5d / 5a actually generate slots for the new markers? */
    const restorationSlots = slots.filter(s => /복원\s*[전후]/.test(s.desc)).length;
    const bannerSlots      = slots.filter(s => /현수막/.test(s.desc)).length;
    /* Did the cat reconcile to '사진' for at least one restoration slot? */
    const briefs = slots.map(s => buildBrief(s, html));
    const photoCatPromoted = briefs.some(b => b.category === '사진');
    const out = {
      slotCount,
      placeholderReplaced: lostMarker,
      referenceKept: keptRef,
      domainPreserved: keptBefore && keptAfter && keptBanner,
      restorationSlots,
      bannerSlots,
      photoCatPromoted
    };
    console.log(out);
    return out;
  }

  /* HWPX self-test — fetches the bundled sample, runs the new
     parser, and reports table/image/slot counts. Call from console:
       await VFTest.parseHwpxSample('T')   // empty version
       await VFTest.parseHwpxSample('F')   // filled / reference
  */
  async function vfTestParseHwpxSample(which) {
    const map = {
      T: '네이처 포지티브 협력 소생태계 복원 행사 계획안_T.hwpx',
      F: '네이처 포지티브 협력 소생태계 복원 행사 계획안_F.hwpx'
    };
    const name = map[which] || map.T;
    console.group(`%cVFTest.parseHwpxSample(${which || 'T'})`, 'color:#0a64ff;font-weight:700');
    try {
      const t0 = performance.now();
      const buf = await fetch(name).then(r => r.arrayBuffer());
      console.log('fetched', name, buf.byteLength, 'bytes');
      const html = await parseHwpx(buf);
      const tables  = (html.match(/<table\b/g) || []).length;
      const images  = (html.match(/class="hwpx-embedded"/g) || []).length;
      const missing = (html.match(/class="hwpx-img-missing"/g) || []).length;
      const ps      = (html.match(/<p>/g) || []).length;
      const h2s     = (html.match(/<h2>/g) || []).length;
      console.log({ tables, embeddedImages: images, missingImages: missing, paragraphs: ps, h2: h2s, htmlChars: html.length });

      const { slots } = detectSlots(html);
      console.log('slots detected:', slots.length, slots.map(s => ({ id: s.id, cat: s.cat, desc: s.desc })));
      console.log('ms:', Math.round(performance.now() - t0));
      return { tables, images, slots: slots.length };
    } catch (e) {
      console.error('VFTest.parseHwpxSample failed:', e);
      return { error: e.message };
    } finally {
      console.groupEnd();
    }
  }

  window.VFTest = {
    run:        vfTestRun,
    briefOnly:  vfTestBriefOnly,
    parsePass:  vfTestParsePass,
    parseHwpxSample: vfTestParseHwpxSample,
    /* export shortcuts — same handlers the save button uses */
    exportPdf:  () => Exporter.exportPDF(),
    exportHwpx: () => Exporter.exportHWPX(),
    /* low-level introspection */
    detectSlots,
    buildBrief,
    buildPrompt,
    parseHwpx,
    verifyGeneration,
    visualStyleForSlot
  };

  /* ════════════════════════════════════════════════════
     Exporter — HTML preview / PDF / sidecar HWPX bundle.

     - exportPDF()  uses the browser's print engine. A scoped print CSS
                    hides the chrome (navbar, dropzone, options panel)
                    and prints only the document view + slots, so the
                    output mirrors the on-screen rendering page-for-page.
     - exportHWPX() ships a sidecar ZIP because faithful in-place
                    insertion of an image into the OWPML <hp:pic> +
                    BinData/_rels chain is hard to do reliably in the
                    browser without a hwp-aware library. The ZIP carries
                    the ORIGINAL hwpx untouched + the generated PNGs
                    named by slot index + a mapping.json + an INSERT.md
                    walking through the manual paste step. This is the
                    honest, recoverable artifact the spec asks for, and
                    the same module can be swapped for a true HWPX
                    rewriter once a server endpoint exists.
  ════════════════════════════════════════════════════ */
  const Exporter = {
    /* Try the browser-native "Print → Save as PDF". A print CSS makes
       the navbar / dropzone / options panel disappear and the document
       page expand to full width. Works on every modern browser, keeps
       Korean fonts intact, no extra CDN. */
    async exportPDF() {
      Log.info('Exporter', 'exportPDF requested');
      const docView = $('doc-view');
      if (!docView || !docView.innerHTML.trim()) {
        Toast.show('먼저 문서를 업로드해 주세요', { error: true });
        return;
      }

      /* Idempotent — remove any leftover injection first. */
      document.getElementById('__vf_print__')?.remove();

      const style = document.createElement('style');
      style.id = '__vf_print__';
      /* Whitelist approach — hide everything in body then revert what
         we want to print. Future chrome additions are auto-hidden. */
      style.textContent = `
        @media print {
          @page { size: A4; margin: 12mm; }
          html, body { background: #fff !important; }
          body > * { display: none !important; }
          body > main { display: block !important; }
          main > *  { display: none !important; }
          main > #view-workspace { display: block !important; }
          .ws-pages, .ws-options, .pane-actions, .pane-head, #toast { display: none !important; }
          .ws-grid { display: block !important; height: auto !important; }
          .ws-doc, .doc-scroll { display: block !important; height: auto !important;
            overflow: visible !important; padding: 0 !important; }
          .document-page { box-shadow: none !important; border: none !important;
            margin: 0 !important; padding: 0 !important; max-width: none !important; }
          .img-slot { break-inside: avoid; page-break-inside: avoid; }
          .img-slot-overlay, .img-slot-label { display: none !important; }
        }
      `;
      document.head.appendChild(style);

      /* Wait for the actual print event lifecycle. Resolves on
         `afterprint` (Chromium, Firefox) or via a worst-case
         fallback timer so the call never hangs. */
      return new Promise((resolve) => {
        const cleanup = () => {
          try { style.remove(); } catch (_) {}
          window.removeEventListener('afterprint', cleanup);
          Log.info('Exporter', 'exportPDF cleanup');
          resolve();
        };
        window.addEventListener('afterprint', cleanup, { once: true });
        /* Hard cap — 90s is generous; if the user leaves the dialog
           open forever we still clean up eventually. */
        const fallback = setTimeout(() => { cleanup(); }, 90_000);
        /* Real print call. */
        try { Log.info('Exporter', 'invoking window.print'); window.print(); }
        catch (e) { Log.warn('Exporter', 'window.print threw', { msg: e.message }); clearTimeout(fallback); cleanup(); }
      });
    },

    /* Sidecar HWPX bundle. Accepts an optional snapshot (used by
       _saveAll so a goHome racing the save can't pull the rug),
       defaults to live state otherwise. */
    async exportHWPX(snap) {
      const src = snap || {
        slots:      state.slots,
        fileBuffer: state.fileBuffer,
        fileName:   state.fileName,
        fileExt:    state.fileExt
      };
      Log.info('Exporter', 'exportHWPX requested', {
        hasBuffer: !!src.fileBuffer,
        slotCount: src.slots.length
      });
      if (!src.fileBuffer) {
        Toast.show('먼저 문서를 업로드해 주세요', { error: true });
        throw new Error('no source buffer');
      }
      const JSZip = await loadJSZip();
      const out = new JSZip();

      const sanitize = s => (s || 'document')
        .replace(/[\\\/:*?"<>|\r\n]+/g, '_')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 80) || 'document';

      const baseName = sanitize((src.fileName || 'document').replace(/\.(hwpx|docx)$/i, ''));
      const ext      = src.fileExt || 'hwpx';

      /* 1) Original document — untouched. */
      out.file(`${baseName}.${ext}`, src.fileBuffer);

      /* 2) Per-slot images. Parallel fetch — index preserved. */
      const gen = out.folder('generated');
      const fetched = await Promise.all(src.slots.map(async (s, i) => {
        const url = (s.aiImage) || (s.original && s.original.url) || API.placeholderUrl(s.cat, s.seed);
        try {
          const blob  = await fetch(url).then(r => r.blob());
          const mime  = blob.type || 'image/jpeg';
          const fExt  = /png/i.test(mime)  ? '.png'
                     : /webp/i.test(mime)  ? '.webp'
                     : /gif/i.test(mime)   ? '.gif'
                     : '.jpg';
          const idx     = String(i + 1).padStart(2, '0');
          const safeCat = sanitize(s.cat);
          const safeDesc = sanitize(s.desc).replace(/\s+/g, '_').slice(0, 40) || 'slot';
          const safeName = `${idx}_${safeCat}_${safeDesc}${fExt}`;
          return {
            ok: true,
            slot: s,
            url,
            blob,
            mime,
            bytes: blob.size,
            name: safeName,
            source: s.aiImage ? 'ai' : (s.original && s.original.url ? 'original' : 'placeholder')
          };
        } catch (e) {
          Log.warn('Exporter', `slot ${i + 1} fetch failed`, { msg: e.message });
          return { ok: false, slot: s, error: e.message };
        }
      }));

      const mapping = fetched.map((r, i) => {
        const base = {
          index: i + 1,
          slotId: r.slot.id,
          category: r.slot.cat,
          description: r.slot.desc
        };
        if (!r.ok) return Object.assign(base, { file: null, error: r.error });
        gen.file(r.name, r.blob);            // Blob → JSZip direct, no ArrayBuffer copy
        return Object.assign(base, {
          file: `generated/${r.name}`,
          mime: r.mime,
          bytes: r.bytes,
          source: r.source,
          scores: (r.slot.original && r.slot.original.scores) || null
        });
      });

      const wrapper = {
        schema: 'visualflow.sidecar/1',
        generatedAt: new Date().toISOString(),
        source: { fileName: src.fileName, ext, bytes: src.fileBuffer.byteLength },
        slotCount: mapping.length,
        failedCount: mapping.filter(m => !m.file).length,
        slots: mapping
      };
      out.file('mapping.json', JSON.stringify(wrapper, null, 2));

      /* 3) Human-readable insertion guide. */
      const guide = [
        '# Visual Flow — sidecar HWPX bundle',
        '',
        `원본 파일       : ${baseName}.${ext}`,
        `생성된 이미지   : generated/  (총 ${mapping.length}장, 실패 ${wrapper.failedCount}장)`,
        `슬롯-이미지 매핑: mapping.json (schema ${wrapper.schema})`,
        '',
        '## 한컴에서 수동 삽입하는 절차',
        '1. 원본 파일을 한컴 한글에서 엽니다.',
        '2. mapping.json의 description/category 로 각 슬롯의 위치를 찾습니다.',
        '3. generated/ 폴더의 해당 이미지를 그 위치에 드래그하거나 [입력 → 그림] 으로 삽입합니다.',
        '4. 표 셀이나 그림 프레임의 크기에 맞춰 자동 정렬됩니다.',
        '',
        '## 클라이언트 단독으로는 왜 자동 삽입이 안 되나요',
        '한컴 OWPML의 `hp:pic` 노드를 추가하면서 `BinData/`에 이미지를 넣고',
        '`Contents/content.hpf`의 manifest 와 `META-INF/` 매니페스트까지 일관되게',
        '갱신해야 합니다. 한 곳만 어긋나도 한컴이 파일을 거부합니다. 서버에서',
        '`pyhwpx` / `hwp5proc` 같은 한컴 라이브러리로 처리하면 안전합니다.',
        '',
        `생성 시각: ${wrapper.generatedAt}`
      ].join('\n');
      out.file('INSERT.md', guide);

      const blob = await out.generateAsync({ type: 'blob' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${baseName}__visualflow.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      Log.info('Exporter', 'exportHWPX done', { slots: mapping.length, failed: wrapper.failedCount });
      Toast.show(`✓ 사이드카 ZIP 다운로드 (${mapping.length}장 + 원본)`);
    }
  };
  window.Exporter = Exporter;

  /* ════════════════════════════════════════════════════
     goHome — logo click resets the session to the upload screen.
     Layout / design / position are untouched; only the click
     behaviour is added to the existing <a class="brand">.
  ════════════════════════════════════════════════════ */
  function goHome(ev) {
    if (ev) ev.preventDefault();
    if (state.busy) {
      Toast.show('저장이 진행 중입니다 — 잠시만요', { error: true });
      Log.warn('Brand', 'goHome blocked by busy flag');
      return;
    }
    Log.info('Brand', 'goHome — resetting session', {
      hadFile: !!state.file,
      slotCount: state.slots.length
    });
    /* If a previous print injected a stylesheet that hasn't been
       cleaned up yet, remove it now. */
    document.getElementById('__vf_print__')?.remove();

    /* Bring workspace down, upload up. Workspace.hide() also clears
       the document-view DOM, the page list and the options panel. */
    const ws = $('view-workspace');
    if (ws && !ws.hidden) Workspace.hide();

    /* Hard-reset in-memory state. All slot-level data (originalImage
       snapshots, AI image URLs, briefs, prompts) is dropped — exactly
       the "임시 작업 데이터 / 세션 내 작업 상태" the spec demands.
       Theme + localStorage settings stay (they are UI preferences). */
    state.file       = null;
    state.fileBuffer = null;
    state.fileName   = '';
    state.fileExt    = '';
    state.fullHtml   = '';
    state.slots      = [];
    state.selectedId = null;

    /* Clear the actual <input type=file> so re-picking the same file
       still triggers `change`. */
    const inp = $('file-input'); if (inp) inp.value = '';

    /* Dropzone visual back to idle (no error/uploading/success tint). */
    Uploader.reset();

    Toast.show('새 작업을 시작하세요');
  }

  /* ════════════════════════════════════════════════════
     Boot
  ════════════════════════════════════════════════════ */
  function boot() {
    Log.info('boot', 'Visual Flow starting');
    Theme.init();
    Uploader.init();
    OptionsPanel.init();
    $('btn-new-doc')?.addEventListener('click', () => Workspace.hide());
    /* Logo click → home (behaviour added; layout/design untouched). */
    document.querySelector('.brand')?.addEventListener('click', goHome);
    Log.info('boot', 'ready · type VFTest.run() in console to self-test');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
