(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const utf8Encoder = new TextEncoder();
  const utf8Decoder = new TextDecoder("utf-8");

  const state = {
    file: null,
    entries: new Map(),
    sections: [],
    blocks: [],
    audit: [],
    packageInfo: null,
    zip: null,
    slots: [],
    selected: null,
    aiStatus: null,
    aiImageAvailable: false,
    backendConnected: false,
    previewPages: null,
    previewMode: false,
    deferAiAugment: false,
    hwpxDisplayHtml: null   // parseHwpxForDisplay()가 생성하는 시각적 HTML (이미지 포함)
  };

  const ui = {
    uploadView: $("uploadView"),
    workspace: $("workspace"),
    dropzone: $("dropzone"),
    fileInput: $("fileInput"),
    progress: $("progress"),
    fileName: $("fileName"),
    slotCount: $("slotCount"),
    sectionCount: $("sectionCount"),
    auditStatus: $("auditStatus"),
    auditList: $("auditList"),
    slotList: $("slotList"),
    documentView: $("documentView"),
    previewBtn: $("previewBtn"),
    previewPages: $("previewPages"),
    emptyPanel: $("emptyPanel"),
    editor: $("editor"),
    canvas: $("previewCanvas"),
    descInput: $("descInput"),
    typeInput: $("typeInput"),
    styleInput: null,
    promptInput: $("promptInput"),
    toast: $("toast"),
    convertModal: $("convertModal"),
    convBody: $("convBody"),
    convCloseBtn: $("convCloseBtn"),
    convPlainBtn: $("convPlainBtn"),
    convMdBtn: $("convMdBtn"),
    convCopyBtn: $("convCopyBtn"),
    convSaveBtn: $("convSaveBtn")
  };

  function toast(message, isError = false) {
    ui.toast.textContent = message;
    ui.toast.classList.toggle("is-error", isError);
    ui.toast.classList.add("is-on");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => ui.toast.classList.remove("is-on"), 2600);
  }

  // ── HWPX → 일반 텍스트 / 마크다운 변환기 ──────────────────────
  // 이미 파싱된 state.blocks(문단 run→t 텍스트 + 표)를 재사용한다.
  function blocksToPlainText() {
    const lines = [];
    state.blocks.forEach((b) => {
      if (b.kind === "text") {
        const line = b.text.trim();
        if (line) lines.push(line);
      } else {
        b.rows.forEach((row) => {
          const cells = row.map((cell) => (cell.text || "").replace(/\s+/g, " ").trim());
          if (cells.some(Boolean)) lines.push(cells.join("\t"));
        });
      }
    });
    return lines.join("\n");
  }

  function tableToMarkdown(table) {
    if (!table.rows.length) return "";
    const esc = (s) => ((typeof s === "object" && s !== null ? s.text : s) || "").replace(/\s+/g, " ").replace(/\|/g, "\\|").trim();
    const cols = Math.max(...table.rows.map((r) => r.length));
    const pad = (r) => { const a = r.map(esc); while (a.length < cols) a.push(""); return a; };
    const head = pad(table.rows[0]);
    const out = [`| ${head.join(" | ")} |`, `| ${head.map(() => "---").join(" | ")} |`];
    table.rows.slice(1).forEach((r) => out.push(`| ${pad(r).join(" | ")} |`));
    return out.join("\n");
  }

  function blocksToMarkdown() {
    const out = [];
    let titleDone = false;
    state.blocks.forEach((b) => {
      if (b.kind === "table") { out.push(tableToMarkdown(b)); return; }
      const t = b.text.trim();
      if (!t) return;
      if (!titleDone && t.length > 6) { out.push(`# ${t}`); titleDone = true; }
      else if (/^(\d+\.\s|[가-힣]\.\s|붙임|【|<)/.test(t)) out.push(`## ${t}`);
      else if (/^[□◈▪▶●◦❍∘·\-*]\s?/.test(t)) out.push(`- ${t.replace(/^[□◈▪▶●◦❍∘·\-*]\s?/, "")}`);
      else out.push(t);
    });
    return out.join("\n\n");
  }

  function renderConvert(format) {
    state.convFormat = format;
    ui.convBody.textContent = format === "md" ? blocksToMarkdown() : blocksToPlainText();
    ui.convPlainBtn.classList.toggle("is-active", format !== "md");
    ui.convMdBtn.classList.toggle("is-active", format === "md");
  }

  function openConvertModal() {
    if (!state.blocks || !state.blocks.length) {
      toast("먼저 HWPX 파일을 업로드하세요.", true);
      return;
    }
    renderConvert(state.convFormat || "plain");
    ui.convertModal.hidden = false;
    document.body.style.overflow = "hidden";
  }

  function closeConvertModal() {
    ui.convertModal.hidden = true;
    document.body.style.overflow = "";
  }

  function setProgress(value) {
    ui.progress.classList.toggle("is-on", value > 0 && value < 1);
    ui.progress.querySelector("i").style.width = `${Math.round(value * 100)}%`;
  }

  function sanitizeName(value) {
    return String(value || "image").replace(/[\\/:*?"<>|]+/g, "_").replace(/\s+/g, "_").slice(0, 90);
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[ch]);
  }

  function readU16(view, offset) { return view.getUint16(offset, true); }
  function readU32(view, offset) { return view.getUint32(offset, true); }

  function normalizeEncoding(value) {
    const encName = String(value || "").trim().toLowerCase().replace(/_/g, "-");
    if (!encName) return "utf-8";
    if (encName === "ks-c-5601" || encName === "ks-c-5601-1987" || encName === "cp949") return "euc-kr";
    if (encName === "utf16") return "utf-16le";
    return encName;
  }

  function sniffXmlEncoding(bytes) {
    if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) return "utf-8";
    if (bytes[0] === 0xff && bytes[1] === 0xfe) return "utf-16le";
    if (bytes[0] === 0xfe && bytes[1] === 0xff) return "utf-16be";

    const sample = bytes.slice(0, Math.min(bytes.length, 240));
    const evenNulls = sample.filter((_, i) => i % 2 === 0 && sample[i] === 0).length;
    const oddNulls = sample.filter((_, i) => i % 2 === 1 && sample[i] === 0).length;
    if (oddNulls > sample.length / 5) return "utf-16le";
    if (evenNulls > sample.length / 5) return "utf-16be";

    const ascii = Array.from(sample, (byte) => byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : " ").join("");
    const match = ascii.match(/encoding\s*=\s*["']([^"']+)["']/i);
    return normalizeEncoding(match?.[1]);
  }

  function decodeXml(bytes) {
    const encoding = sniffXmlEncoding(bytes);
    try {
      return new TextDecoder(encoding).decode(bytes);
    } catch (_) {
      return utf8Decoder.decode(bytes);
    }
  }

  async function inflateRaw(bytes) {
    if (!("DecompressionStream" in window)) {
      throw new Error("이 브라우저는 HWPX 압축 해제를 지원하지 않습니다. 최신 Chrome 또는 Edge에서 열어주세요.");
    }
    const stream = new Blob([bytes]).stream();
    let ds;
    try {
      ds = stream.pipeThrough(new DecompressionStream("deflate-raw"));
    } catch (_) {
      ds = stream.pipeThrough(new DecompressionStream("deflate"));
    }
    return new Uint8Array(await new Response(ds).arrayBuffer());
  }

  async function readZipEntries(file) {
    const buf = await file.arrayBuffer();
    const bytes = new Uint8Array(buf);
    const view = new DataView(buf);
    let eocd = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 66000); i--) {
      if (readU32(view, i) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("HWPX ZIP 디렉터리를 찾지 못했습니다.");

    const total = readU16(view, eocd + 10);
    const cdOffset = readU32(view, eocd + 16);
    const entries = new Map();
    let ptr = cdOffset;

    for (let i = 0; i < total; i++) {
      if (readU32(view, ptr) !== 0x02014b50) break;
      const method = readU16(view, ptr + 10);
      const compressedSize = readU32(view, ptr + 20);
      const uncompressedSize = readU32(view, ptr + 24);
      const nameLen = readU16(view, ptr + 28);
      const extraLen = readU16(view, ptr + 30);
      const commentLen = readU16(view, ptr + 32);
      const localOffset = readU32(view, ptr + 42);
      const name = utf8Decoder.decode(bytes.slice(ptr + 46, ptr + 46 + nameLen));
      entries.set(name, { name, method, compressedSize, uncompressedSize, localOffset });
      ptr += 46 + nameLen + extraLen + commentLen;
    }

    async function getBytes(name) {
      const entry = entries.get(name);
      if (!entry) throw new Error(`${name} 항목이 없습니다.`);
      const local = entry.localOffset;
      if (readU32(view, local) !== 0x04034b50) throw new Error(`${name} 로컬 헤더를 읽지 못했습니다.`);
      const nameLen = readU16(view, local + 26);
      const extraLen = readU16(view, local + 28);
      const start = local + 30 + nameLen + extraLen;
      const compressed = bytes.slice(start, start + entry.compressedSize);
      if (entry.method === 0) return compressed;
      if (entry.method === 8) return inflateRaw(compressed);
      throw new Error(`지원하지 않는 ZIP 압축 방식입니다: ${entry.method}`);
    }

    async function getText(name) {
      return decodeXml(await getBytes(name));
    }

    return { entries, getBytes, getText };
  }

  function makeAuditItem(level, title, detail) {
    return { level, title, detail };
  }

  function validatePackage(zip) {
    const names = Array.from(zip.entries.keys());
    const audit = [];
    const firstName = names[0] || "";
    const mimetype = zip.entries.get("mimetype");
    const required = [
      "mimetype",
      "META-INF/container.xml",
      "META-INF/manifest.xml",
      "version.xml",
      "settings.xml",
      "Contents/header.xml",
      "Contents/content.hpf"
    ];

    required.forEach((name) => {
      audit.push(zip.entries.has(name)
        ? makeAuditItem("ok", name, "필수 항목 확인")
        : makeAuditItem("warn", name, "HWPX 기본 구조에서 누락됨"));
    });

    if (mimetype) {
      audit.push(firstName === "mimetype" && mimetype.method === 0
        ? makeAuditItem("ok", "mimetype 패키징", "첫 번째 ZIP 엔트리이며 무압축 상태")
        : makeAuditItem("warn", "mimetype 패키징", "스킬 권장값은 첫 번째 ZIP 엔트리 + ZIP_STORED"));
    }

    const sectionNames = names
      .filter((name) => /^Contents\/section\d+\.xml$/i.test(name))
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    audit.push(sectionNames.length
      ? makeAuditItem("ok", "본문 section XML", `${sectionNames.length}개 섹션 감지`)
      : makeAuditItem("warn", "본문 section XML", "Contents/section*.xml을 찾지 못함"));

    return { audit, sectionNames };
  }

  function localChildren(node, name) {
    return Array.from(node.children || []).filter((child) => child.localName === name);
  }

  function hasAncestor(node, names) {
    const set = new Set(Array.isArray(names) ? names : [names]);
    for (let cur = node.parentElement; cur; cur = cur.parentElement) {
      if (set.has(cur.localName)) return true;
    }
    return false;
  }

  function plainText(node) {
    let out = "";
    function walk(cur) {
      if (cur.nodeType === Node.TEXT_NODE) { out += cur.nodeValue || ""; return; }
      if (cur.nodeType !== Node.ELEMENT_NODE) return;
      if (cur.localName === "t") { out += cur.textContent || ""; return; }
      if (cur.localName === "lineBreak" || cur.localName === "br") { out += "\n"; return; }
      if (cur.localName === "tab") { out += "\t"; return; }
      Array.from(cur.childNodes || []).forEach(walk);
      if (cur.localName === "p" && out && !out.endsWith("\n")) out += "\n";
    }
    walk(node);
    return out.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  }

  function nearestAncestor(node, name) {
    for (let cur = node.parentElement; cur; cur = cur.parentElement) {
      if (cur.localName === name) return cur;
    }
    return null;
  }

  function directParagraphText(paragraph) {
    const parts = Array.from(paragraph.getElementsByTagName("*"))
      .filter((node) => node.localName === "t" && nearestAncestor(node, "p") === paragraph)
      .map((node) => node.textContent || "");
    return parts.join("").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  }

  function parseHwpxStyles(headerDoc) {
    const charPr = new Map();
    const paraPr = new Map();
    const borderFill = new Map();
    if (!headerDoc) return { charPr, paraPr, borderFill };

    Array.from(headerDoc.getElementsByTagName("*")).forEach((node) => {
      if (node.localName === "borderFill") {
        const id = node.getAttribute("id");
        if (id == null) return;
        const winBrush = Array.from(node.getElementsByTagName("*")).find((child) => child.localName === "winBrush");
        const borders = ["leftBorder", "rightBorder", "topBorder", "bottomBorder"]
          .map((name) => Array.from(node.children || []).find((child) => child.localName === name))
          .filter(Boolean);
        const visible = borders.some((border) => (border.getAttribute("type") || "NONE") !== "NONE");
        const color = borders.find((border) => border.getAttribute("color"))?.getAttribute("color") || "#8bbfff";
        const faceColor = winBrush?.getAttribute("faceColor") || "";
        borderFill.set(id, {
          background: faceColor && faceColor !== "none" ? faceColor : "",
          border: visible ? `1px solid ${color}` : ""
        });
      } else if (node.localName === "charPr") {
        const id = node.getAttribute("id");
        if (id == null) return;
        const height = Number(node.getAttribute("height") || 0);
        const underline = Array.from(node.children || []).find((child) => child.localName === "underline");
        const spacing = Array.from(node.children || []).find((child) => child.localName === "spacing");
        const color = node.getAttribute("textColor");
        const shade = node.getAttribute("shadeColor");
        const bf = borderFill.get(node.getAttribute("borderFillIDRef") || "") || {};
        charPr.set(id, {
          fontSizePt: height > 0 ? Math.max(7, Math.min(28, height / 100)) : null,
          bold: Array.from(node.children || []).some((child) => child.localName === "bold"),
          italic: Array.from(node.children || []).some((child) => child.localName === "italic"),
          underline: underline && (underline.getAttribute("type") || "NONE") !== "NONE",
          color: color && color !== "none" ? color : "",
          background: shade && shade !== "none" ? shade : (bf.background || ""),
          border: bf.border || "",
          letterSpacingEm: Number(spacing?.getAttribute("hangul") || spacing?.getAttribute("latin") || 0) / 100
        });
      } else if (node.localName === "paraPr") {
        const id = node.getAttribute("id");
        if (id == null) return;
        const align = Array.from(node.children || []).find((child) => child.localName === "align");
        const margins = Array.from(node.getElementsByTagName("*")).filter((child) => child.localName === "margin");
        const margin = margins[margins.length - 1];
        const lineSpacing = Array.from(node.getElementsByTagName("*"))
          .filter((child) => child.localName === "lineSpacing")
          .slice(-1)[0];
        const border = Array.from(node.children || []).find((child) => child.localName === "border");
        const valueOf = (name) => {
          const direct = margin?.getAttribute(name);
          if (direct != null) return Number(direct);
          const child = Array.from(margin?.children || []).find((item) => item.localName === name);
          return Number(child?.getAttribute("value") || 0);
        };
        const bf = borderFill.get(border?.getAttribute("borderFillIDRef") || "") || {};
        paraPr.set(id, {
          textAlign: (align?.getAttribute("horizontal") || "").toLowerCase(),
          marginTopPt: valueOf("prev") / 100,
          marginBottomPt: valueOf("next") / 100,
          textIndentPt: valueOf("indent") || valueOf("intent") ? (valueOf("indent") || valueOf("intent")) / 100 : 0,
          marginLeftPt: valueOf("left") / 100,
          lineHeight: Number(lineSpacing?.getAttribute("value") || 0) / 100,
          background: bf.background || "",
          border: bf.border || ""
        });
      }
    });
    return { charPr, paraPr, borderFill };
  }

  function paragraphStyle(paragraph, styleInfo) {
    const paraId = paragraph.getAttribute("paraPrIDRef") || "0";
    const runs = Array.from(paragraph.getElementsByTagName("*"))
      .filter((node) => node.localName === "run" && nearestAncestor(node, "p") === paragraph);
    const runIds = runs.map((run) => run.getAttribute("charPrIDRef")).filter(Boolean);
    const charId = runIds[0] || "0";
    return {
      charPrIDRef: charId,
      paraPrIDRef: paraId,
      char: styleInfo.charPr.get(charId) || styleInfo.charPr.get("0") || {},
      para: styleInfo.paraPr.get(paraId) || styleInfo.paraPr.get("0") || {}
    };
  }

  function directRunText(run) {
    let out = "";
    function walk(node) {
      if (node.nodeType === Node.TEXT_NODE) { out += node.nodeValue || ""; return; }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      if (node !== run && (node.localName === "p" || node.localName === "tbl")) return;
      if (node.localName === "t") { out += node.textContent || ""; return; }
      if (node.localName === "lineBreak" || node.localName === "br") { out += "\n"; return; }
      if (node.localName === "tab") { out += "\t"; return; }
      Array.from(node.childNodes || []).forEach(walk);
    }
    Array.from(run.childNodes || []).forEach(walk);
    return out;
  }

  function paragraphRuns(paragraph, styleInfo) {
    const directRuns = Array.from(paragraph.getElementsByTagName("*"))
      .filter((node) => node.localName === "run" && nearestAncestor(node, "p") === paragraph);
    return directRuns.map((run) => {
      const charId = run.getAttribute("charPrIDRef") || "0";
      return {
        text: directRunText(run),
        charPrIDRef: charId,
        char: styleInfo.charPr.get(charId) || styleInfo.charPr.get("0") || {}
      };
    }).filter((run) => run.text);
  }

  // 배경색이 어두울 경우 텍스트를 흰색으로 전환 (가시성 보장)
  function isDarkHex(hex) {
    if (!hex || !/^#[0-9a-f]{6}$/i.test(hex)) return false;
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) < 100;
  }

  function hwpStyleToCss(style) {
    const css = [];
    const ch = style?.char || {};
    const pa = style?.para || {};
    if (ch.fontSizePt) css.push(`font-size:${Math.max(7.5, Math.min(ch.fontSizePt * 0.9, 30))}pt`);
    if (ch.bold) css.push("font-weight:700");
    if (ch.italic) css.push("font-style:italic");
    if (ch.underline) css.push("text-decoration:underline");
    if (ch.color && /^#[0-9a-f]{6}$/i.test(ch.color)) css.push(`color:${ch.color}`);
    if (pa.textAlign && pa.textAlign !== "justify") css.push(`text-align:${pa.textAlign}`);
    if (pa.marginTopPt > 0) css.push(`margin-top:${Math.min(pa.marginTopPt * 0.55, 22)}pt`);
    if (pa.marginBottomPt > 0) css.push(`margin-bottom:${Math.min(pa.marginBottomPt * 0.55, 22)}pt`);
    if (pa.marginLeftPt) css.push(`padding-left:${Math.max(0, Math.min(pa.marginLeftPt * 0.5, 40))}pt`);
    if (pa.textIndentPt) css.push(`text-indent:${Math.max(-16, Math.min(pa.textIndentPt * 0.5, 32))}pt`);
    return css.join(";");
  }

  function hwpParaStyleToCss(style) {
    const css = [];
    const pa = style?.para || {};
    if (pa.textAlign && pa.textAlign !== "justify") css.push(`text-align:${pa.textAlign}`);
    if (pa.marginTopPt > 0) css.push(`margin-top:${Math.min(pa.marginTopPt * 0.55, 22)}pt`);
    if (pa.marginBottomPt > 0) css.push(`margin-bottom:${Math.min(pa.marginBottomPt * 0.55, 22)}pt`);
    if (pa.marginLeftPt) css.push(`padding-left:${Math.max(0, Math.min(pa.marginLeftPt * 0.5, 40))}pt`);
    if (pa.textIndentPt) css.push(`text-indent:${Math.max(-16, Math.min(pa.textIndentPt * 0.5, 32))}pt`);
    if (pa.lineHeight) css.push(`line-height:${Math.max(1.2, Math.min(pa.lineHeight, 2.8))}`);
    if (pa.background && /^#[0-9a-f]{6}$/i.test(pa.background)) {
      css.push(`background:${pa.background}`);
      if (isDarkHex(pa.background)) css.push("color:#f0f0f0");
    }
    if (pa.border) css.push(`border:${pa.border};padding:4pt 8pt`);
    return css.join(";");
  }

  function hwpCharStyleToCss(char) {
    const css = [];
    if (char?.fontSizePt) css.push(`font-size:${Math.max(7.5, Math.min(char.fontSizePt * 0.9, 30))}pt`);
    if (char?.bold) css.push("font-weight:700");
    if (char?.italic) css.push("font-style:italic");
    if (char?.underline) css.push("text-decoration:underline");
    if (char?.color && /^#[0-9a-f]{6}$/i.test(char.color)) css.push(`color:${char.color}`);
    if (char?.background && /^#[0-9a-f]{6}$/i.test(char.background)) {
      css.push(`background:${char.background}`);
      if (isDarkHex(char.background)) css.push("color:#f0f0f0");
    }
    if (char?.border) css.push(`border:${char.border};padding:1pt 2pt`);
    if (char?.letterSpacingEm) css.push(`letter-spacing:${Math.max(-0.12, Math.min(char.letterSpacingEm * 0.7, 0.12))}em`);
    return css.join(";");
  }

  function renderHwpText(block, index) {
    const paraStyle = hwpParaStyleToCss(block.hwpStyle);
    const runs = Array.isArray(block.runs) && block.runs.length
      ? block.runs
      : [{ text: block.text, char: block.hwpStyle?.char || {} }];
    let paragraphStyle = paraStyle;
    if (runs.length === 1 && !paragraphStyle) {
      const ch = runs[0].char || {};
      const box = [];
      if (ch.background && /^#[0-9a-f]{6}$/i.test(ch.background)) box.push(`background:${ch.background}`);
      if (ch.border) box.push(`border:${ch.border};padding:4pt 8pt`);
      paragraphStyle = box.join(";");
    }
    const html = runs.map((run) => {
      const style = hwpCharStyleToCss(run.char || {});
      return `<span${style ? ` style="${escapeHtml(style)}"` : ""}>${escapeHtml(run.text)}</span>`;
    }).join("");
    const idxAttr = Number.isFinite(index) ? ` data-block="${index}"` : "";
    return `<p class="doc-line hwp-line" contenteditable="true"${idxAttr}${paragraphStyle ? ` style="${escapeHtml(paragraphStyle)}"` : ""}>${html}</p>`;
  }

  function isContainerParagraph(paragraph) {
    return Array.from(paragraph.getElementsByTagName("*")).some((node) => {
      if (node === paragraph) return false;
      return node.localName === "tbl" || node.localName === "p";
    });
  }

  function tableToBlock(tbl, sectionIndex, styleInfo) {
    const sz = localChildren(tbl, "sz")[0];
    const width = Number(sz?.getAttribute("width") || 0);
    const height = Number(sz?.getAttribute("height") || 0);
    const bf = styleInfo ? styleInfo.borderFill : new Map();

    // Pass 1: collect all cells with grid addresses and spans
    const allCells = [];
    let trIdx = 0;
    for (const tr of localChildren(tbl, "tr")) {
      for (const tc of localChildren(tr, "tc")) {
        const cellAddr = localChildren(tc, "cellAddr")[0];
        const cellSpan = localChildren(tc, "cellSpan")[0];
        const cellSz = localChildren(tc, "cellSz")[0];
        const bfId = tc.getAttribute("borderFillIDRef") || "";
        const bfStyle = bf.get(bfId) || {};
        const rowAddr = Number(cellAddr?.getAttribute("rowAddr") ?? trIdx);
        const colAddr = Number(cellAddr?.getAttribute("colAddr") ?? 0);
        const colspan = Math.max(1, Number(cellSpan?.getAttribute("colSpan") || 1));
        const rowspan = Math.max(1, Number(cellSpan?.getAttribute("rowSpan") || 1));
        allCells.push({
          rowAddr, colAddr, colspan, rowspan,
          text: plainText(tc),
          width: Number(cellSz?.getAttribute("width") || 0),
          height: Number(cellSz?.getAttribute("height") || 0),
          background: bfStyle.background || "",
          border: bfStyle.border || ""
        });
      }
      trIdx++;
    }

    // Pass 2: mark positions covered by spanning cells (phantom cells)
    const covered = new Set();
    for (const cell of allCells) {
      for (let dr = 0; dr < cell.rowspan; dr++) {
        for (let dc = 0; dc < cell.colspan; dc++) {
          if (dr > 0 || dc > 0) covered.add(`${cell.rowAddr + dr},${cell.colAddr + dc}`);
        }
      }
    }

    // Pass 3: group visible cells by row
    const rowMap = new Map();
    for (const cell of allCells) {
      if (covered.has(`${cell.rowAddr},${cell.colAddr}`)) continue;
      if (!rowMap.has(cell.rowAddr)) rowMap.set(cell.rowAddr, []);
      rowMap.get(cell.rowAddr).push({
        text: cell.text,
        width: cell.width,
        height: cell.height,
        background: cell.background,
        border: cell.border,
        colspan: cell.colspan > 1 ? cell.colspan : undefined,
        rowspan: cell.rowspan > 1 ? cell.rowspan : undefined
      });
    }
    const rows = Array.from(rowMap.keys()).sort((a, b) => a - b).map((k) => rowMap.get(k));
    return { kind: "table", sectionIndex, width, height, rows };
  }

  async function parseHwpx(file) {
    const zip = await readZipEntries(file);
    const packageInfo = validatePackage(zip);
    const sectionNames = packageInfo.sectionNames;
    if (!sectionNames.length) throw new Error("본문 section XML을 찾지 못했습니다.");

    const parser = new DOMParser();
    const blocks = [];
    const sections = [];
    const audit = [...packageInfo.audit];
    let styleInfo = { charPr: new Map(), paraPr: new Map() };

    if (zip.entries.has("Contents/header.xml")) {
      const headerXml = await zip.getText("Contents/header.xml");
      const headerDoc = parser.parseFromString(headerXml, "application/xml");
      if (!headerDoc.querySelector("parsererror")) {
        styleInfo = parseHwpxStyles(headerDoc);
        audit.push(...analyzeHeaderDoc(headerDoc));
      }
    }

    if (zip.entries.has("mimetype")) {
      const mimetype = (await zip.getText("mimetype")).trim();
      audit.push(mimetype === "application/hwp+zip"
        ? makeAuditItem("ok", "mimetype 내용", "application/hwp+zip 확인")
        : makeAuditItem("warn", "mimetype 내용", `예상값 application/hwp+zip, 현재 ${mimetype || "빈 값"}`));
    }

    for (let s = 0; s < sectionNames.length; s++) {
      const xml = await zip.getText(sectionNames[s]);
      const doc = parser.parseFromString(xml, "application/xml");
      const parseError = doc.querySelector("parsererror");
      if (parseError) throw new Error(`${sectionNames[s]} XML 파싱에 실패했습니다.`);
      sections.push(sectionNames[s]);
      audit.push(...analyzeSectionDoc(doc, sectionNames[s]));

      const paragraphs = Array.from(doc.getElementsByTagName("*"))
        .filter((node) => node.localName === "p" && !hasAncestor(node, ["p", "tc"]));

      paragraphs.forEach((p) => {
        const container = isContainerParagraph(p);
        const text = directParagraphText(p);
        const style = paragraphStyle(p, styleInfo);
        const runs = paragraphRuns(p, styleInfo);
        const tables = Array.from(p.getElementsByTagName("*"))
          .filter((node) => node.localName === "tbl" && !hasAncestor(node, "tbl"));
        if (text) {
          const lineText = text.replace(/\s+$/g, "");
          if (lineText.trim()) {
            blocks.push({ kind: "text", sectionIndex: s, text: lineText, runs, redraftable: true, hwpStyle: style });
          }
        }
        tables.forEach((tbl) => blocks.push(tableToBlock(tbl, s, styleInfo)));
      });
    }
    return { zip, sections, blocks, audit, packageInfo };
  }

  async function parseDocx(file) {
    const zip = await readZipEntries(file);
    if (!zip.entries.has("word/document.xml")) {
      throw new Error("DOCX document.xml을 찾지 못했습니다.");
    }
    const parser = new DOMParser();
    const doc = parser.parseFromString(await zip.getText("word/document.xml"), "application/xml");
    const parseError = doc.querySelector("parsererror");
    if (parseError) throw new Error("DOCX XML 파싱에 실패했습니다.");

    const body = Array.from(doc.getElementsByTagName("*")).find((node) => node.localName === "body") || doc.documentElement;
    const blocks = [];
    const children = Array.from(body.children);

    function textOf(node) {
      return Array.from(node.getElementsByTagName("*"))
        .filter((child) => child.localName === "t")
        .map((child) => child.textContent || "")
        .join("")
        .replace(/\s+\n/g, "\n")
        .trim();
    }

    function tableRows(tbl) {
      return Array.from(tbl.children)
        .filter((tr) => tr.localName === "tr")
        .map((tr) => Array.from(tr.children)
          .filter((tc) => tc.localName === "tc")
          .map((tc) => ({ text: textOf(tc), width: 0, height: 0 })));
    }

    children.forEach((node) => {
      if (node.localName === "p") {
        const text = textOf(node);
        if (text) blocks.push({ kind: "text", sectionIndex: 0, text, redraftable: true });
      } else if (node.localName === "tbl") {
        blocks.push({ kind: "table", sectionIndex: 0, width: 0, height: 0, rows: tableRows(node) });
      }
    });

    return {
      zip: null,
      sections: ["word/document.xml"],
      blocks,
      audit: [
        makeAuditItem("ok", "DOCX 문서", "word/document.xml에서 문단과 표를 추출했습니다."),
        makeAuditItem("info", "원본 보기", "서버 렌더링으로 업로드 문서를 페이지 이미지로 표시합니다.")
      ],
      packageInfo: { sectionNames: ["word/document.xml"], audit: [] }
    };
  }

  function documentExtension(file) {
    return ((file && file.name ? file.name : "").split(".").pop() || "").toLowerCase();
  }

  function contextAround(blocks, index, span = 8) {
    return blocks.slice(Math.max(0, index - span), Math.min(blocks.length, index + span + 1))
      .map((block) => block.kind === "text" ? block.text : tableText(block))
      .join("\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function tableText(table) {
    return table.rows.flat().map((cell) => cell.text).filter(Boolean).join(" ");
  }

  function analyzeSectionDoc(doc, sectionName) {
    const nodes = Array.from(doc.getElementsByTagName("*"));
    const paragraphs = nodes.filter((node) => node.localName === "p");
    const tables = nodes.filter((node) => node.localName === "tbl").length;
    const controls = nodes.filter((node) => node.localName === "ctrl").length;
    const lineSegArrays = nodes.filter((node) => node.localName === "linesegarray").length;
    const textBoxes = nodes.filter((node) => /textbox|textBox|drawText/i.test(node.localName)).length;
    const runs = nodes.filter((node) => node.localName === "run").length;
    const textNodes = nodes.filter((node) => node.localName === "t").length;
    const containers = paragraphs.filter(isContainerParagraph).length;
    const fillableBullets = paragraphs.filter((p) => /^[◦❍-]\s*$/.test(directParagraphText(p))).length;
    const audit = [];

    if (containers) {
      audit.push(makeAuditItem("ok", `${sectionName} leaf 문단`, `${containers}개 컨테이너 문단은 일반 텍스트 추출에서 제외`));
    }
    if (tables) {
      audit.push(makeAuditItem("info", `${sectionName} 표 구조`, `${tables}개 표 감지: 셀 내부 텍스트는 원본 구조 유지 권장`));
    }
    if (controls || textBoxes) {
      audit.push(makeAuditItem("warn", `${sectionName} 고정 개체`, `${controls + textBoxes}개 제어/텍스트 박스 감지: COM 전환 후보`));
    }
    if (lineSegArrays) {
      audit.push(makeAuditItem("info", `${sectionName} linesegarray`, `${lineSegArrays}개 감지: 재조립 시 제거 후 한글 재계산 권장`));
    }
    if (fillableBullets) {
      audit.push(makeAuditItem("info", `${sectionName} 빈 불릿`, `${fillableBullets}개 감지: template_filler 방식으로 제자리 채우기 가능`));
    }
    if (runs > 0 && textNodes / runs > 1.8) {
      audit.push(makeAuditItem("info", `${sectionName} run 분해`, "한 줄이 여러 텍스트 노드로 나뉜 구간이 있어 노드 단위 보존 권장"));
    }
    return audit;
  }

  function analyzeHeaderDoc(doc) {
    const nodes = Array.from(doc.getElementsByTagName("*"));
    const count = (name) => nodes.filter((node) => node.localName === name).length;
    const charPr = count("charPr");
    const paraPr = count("paraPr");
    const borderFill = count("borderFill");
    const audit = [
      makeAuditItem("ok", "header.xml 스타일", `charPr ${charPr}개, paraPr ${paraPr}개, borderFill ${borderFill}개 분석`),
      makeAuditItem("info", "ID 참조 보존", "section의 charPrIDRef/paraPrIDRef는 header.xml 정의와 함께 보존")
    ];
    [
      ["charProperties", "charPr"],
      ["paraProperties", "paraPr"],
      ["borderFills", "borderFill"]
    ].forEach(([groupName, childName]) => {
      const group = nodes.find((node) => node.localName === groupName);
      if (!group) return;
      const declared = Number(group.getAttribute("itemCnt"));
      const actual = localChildren(group, childName).length;
      if (Number.isFinite(declared)) {
        audit.push(declared === actual
          ? makeAuditItem("ok", `${groupName} itemCnt`, `${declared}개 일치`)
          : makeAuditItem("warn", `${groupName} itemCnt`, `선언 ${declared}개, 실제 ${actual}개`));
      }
    });
    return audit;
  }

  function nearestTitle(blocks, index) {
    for (let i = index; i >= 0; i--) {
      const block = blocks[i];
      const text = block.kind === "text" ? block.text : tableText(block);
      if (/붙임|전경도|개요|지원|홍보|행사|기후|음수대|부스/.test(text)) {
        return text.replace(/\s+/g, " ").slice(0, 80);
      }
    }
    return "보고서 이미지";
  }

  function classifySlot(text) {
    const t = text.toLowerCase();
    if (/전경도|행사장|잠원|한강|배치|지도|location|map/.test(t)) return "venue";
    if (/qr|홈페이지|홍보|캠페인|실천|대국민/.test(t)) return "campaign";
    if (/부스|운영|설치/.test(t)) return "booth";
    if (/음수대|수돗물|물탱크|탈플라스틱|water/.test(t)) return "water";
    return "diagram";
  }

  function slotDescription(type, title) {
    const label = {
      venue: "행사장 전경도",
      campaign: "기후행동 홍보 이미지",
      booth: "홍보부스 운영 이미지",
      water: "스마트 음수대 안내 이미지",
      diagram: "보고서 설명 다이어그램"
    }[type];
    return title && !title.includes(label) ? `${label} - ${title}` : label;
  }

  // 구 영문 type → 한국어 uiType 매핑
  const HEURISTIC_TYPE_TO_UI = {
    venue: "지도시각화", campaign: "홍보물", booth: "인포그래픽",
    water: "인포그래픽", diagram: "다이어그램", other: "기타이미지"
  };

  function _heuristicSlot(id, blockIndex, sectionIndex, type, title, context) {
    const uiType = HEURISTIC_TYPE_TO_UI[type] || "다이어그램";
    const desc = slotDescription(type, title).slice(0, 46);
    const ai = { figureType: null, purpose: desc, suggestedContent: [], anchorText: title, sectionPath: title };
    return {
      id,
      blockIndex,
      sectionIndex,
      type,
      uiType,
      style: type === "venue" ? "field" : "official",
      desc,
      title,
      context,
      prompt: buildAiPrompt(ai, context, uiType),
      image: ""
    };
  }

  function findSlots(blocks) {
    const slots = [];
    const used = new Set();

    blocks.forEach((block, index) => {
      const text = block.kind === "text" ? block.text : tableText(block);
      const context = contextAround(blocks, index);
      const hasMarker = /#\s*그림|그림\s*삽입|이미지\s*삽입|사진\s*삽입|\[?\s*image\s*\]?/i.test(text);
      const largeTable = block.kind === "table" && (block.width > 50000 || block.height > 20000);
      const emptyLargeCell = block.kind === "table" && block.rows.flat().some((cell) => !cell.text.trim() && (cell.width > 25000 || cell.height > 12000));
      const visualTitle = /전경도|배치도|조감도|홍보물|안내도|포스터|카드뉴스/.test(text);
      if (!hasMarker && !largeTable && !emptyLargeCell) return;

      const title = visualTitle ? text : nearestTitle(blocks, index);
      const type = classifySlot(`${title}\n${context}`);
      const key = `${index}:${type}:${title}`;
      if (used.has(key)) return;
      used.add(key);
      slots.push(_heuristicSlot(slots.length, index, block.sectionIndex, type, title, context));
    });

    if (!slots.length) {
      const context = contextAround(blocks, Math.min(8, blocks.length - 1), 10);
      const type = classifySlot(context);
      slots.push(_heuristicSlot(0, 0, 0, type, nearestTitle(blocks, 0), context));
    }
    return slots.slice(0, 12);
  }

  function buildPrompt(type, desc, context) {
    const typeGuide = {
      venue: "잠원한강공원 야외 기념식 행사장 전경도. 무대, 관람석, K-water 홍보부스, 스마트 음수대, QR 홍보존, 동선 화살표를 포함한 보고서용 평면 안내도.",
      campaign: "기후 행동으로 실현하는 녹색 대한민국을 주제로 한 공공기관 보고서용 홍보 카드. QR 참여 유도, 시민 참여, 녹색 대한민국 메시지.",
      booth: "K-water 홍보부스 설치 및 운영 계획을 설명하는 보고서용 인포그래픽. 부스, 상담 테이블, 안내 배너, 운영 인력 배치.",
      water: "이동형 수돗물 스마트 음수대와 탈플라스틱 기후행동을 설명하는 보고서용 안내 이미지. 물탱크 방식, 컵 사용 절감, 안전한 수돗물.",
      diagram: "보고서 문맥을 설명하는 깔끔한 공공기관형 개념 다이어그램."
    }[type];
    return [
      typeGuide,
      "스타일: 깨끗한 공공기관 보고서 삽입용 PNG, 흰 배경, 명확한 라벨, 과도한 장식 없음.",
      "문서 문맥:",
      context.slice(0, 900)
    ].join("\n");
  }

  // ── Figure Planner v2.1 AI 보강 ─────────────────────────────
  function enhancePrompt(base, ai) {
    const extra = [];
    if (ai.purpose) extra.push(`목적: ${ai.purpose}`);
    if (ai.suggestedContent && ai.suggestedContent.length) extra.push(`핵심 요소: ${ai.suggestedContent.join(", ")}`);
    if (ai.imagePromptSeed) extra.push(`Image seed: ${ai.imagePromptSeed}`);
    if (!extra.length) return base;
    return `${base}\n\n[AI 보강]\n${extra.join("\n")}`;
  }

  function blockTextAt(index) {
    const block = state.blocks[index];
    if (!block) return "";
    return block.kind === "text" ? block.text : tableText(block);
  }

  function findBlockForAnchor(anchor) {
    if (!anchor) return -1;
    const needle = anchor.slice(0, 20);
    return state.blocks.findIndex((block) => {
      const text = block.kind === "text" ? block.text : tableText(block);
      return text.includes(needle);
    });
  }

  // 백엔드 figures[]를 기존 slot에 매핑·보강하고, 미매칭은 신규 slot으로 추가.
  // 영문 figure_type → 한국어 라벨
  const FIGURE_TYPE_LABEL = {
    concept_diagram: "다이어그램", comparison_table: "인포그래픽", flow_chart: "공정도",
    roadmap: "공정도", map_layout: "지도시각화", floor_plan: "지도시각화", rendering: "조감도",
    bar_chart: "다이어그램", line_chart: "다이어그램", photo: "조감도", cover_banner: "홍보물",
    other: "기타이미지"
  };
  // figure_type → typeInput select value (유형 드롭다운)
  const FIGURE_TYPE_TO_KIND = {
    concept_diagram: "다이어그램", comparison_table: "인포그래픽", flow_chart: "공정도",
    roadmap: "공정도", map_layout: "지도시각화", floor_plan: "지도시각화", rendering: "조감도",
    bar_chart: "다이어그램", line_chart: "다이어그램", photo: "조감도", cover_banner: "홍보물",
    other: "기타이미지"
  };

  // 유형별 이미지 생성 방향 (샘플 이미지 분석 기반)
  const TYPE_GENERATION_GUIDE = {
    "조감도": "특정 사업지(습지·도시·태양광 단지·하천 복원·산업단지 등)를 3D 아이소메트릭으로 표현한 계획 조감도. 사업지 전체를 항공 시점으로 내려다보는 파노라마 구도. 수면·녹지를 중심 축으로 시설·도로·구역이 배치된 구성. 주요 시설을 원형 아이콘 버블(카테고리별 색상 구분, 아이콘+한글 시설명)로 표기. 좌상단에 소형 위치 참조 지도, 우하단에 시설 범례표 삽입. 파란 수면·풍부한 녹지·크림베이지 시설 배색. 부드러운 사선 투시, 건물 그림자, 파스텔 기조에 선명한 포인트 색상. 공공기관 도시개발·환경·인프라 보고서용, 가로 16:9, 흰 여백.",
    "공정도": "좌→우 수평 흐름의 기술 공정도. 3~5단계 직사각형 박스와 화살표 연결. 단계별 아이콘·데이터 포함 가능. 파란색·청록색 포인트, 연회색 배경 박스, 명확한 한글 단계명. 16:9 흰 배경.",
    "지도시각화": "한국 행정구역 지도 기반 시각화. 지역별 파스텔 색상 구분, 한글 지역명 레이블. 핀·마커·버블로 위치·수치 표기. 깔끔한 경계선, 흰 배경, 공공보고서용 정적 스타일.",
    "다이어그램": "차트 형식의 데이터 시각화. 막대·꺾은선·복합 차트 중 적합한 형태 선택. x축 연도·항목, y축 수치, 범례 포함. 파란색·청록색·주황색 계열 배색. 흰 배경, 격자선, 한글 축 레이블과 수치 명확히 표기. 16:9 비율.",
    "인포그래픽": "제목 배너 + 본문 그리드 레이아웃의 정보 인포그래픽. 4~6개 카테고리 박스에 아이콘+한글 텍스트 구성. 파란색·청록색·초록색 계열 배색. 계층적 정보 구조, 핵심 내용 강조 타이포그래피. 공공기관 보고서용, 16:9.",
    "현수막": "가로 긴 현수막 형식(4:1 비율). 붓글씨·캘리그래피 스타일 한글 슬로건 중심. 파란색 계열 단색 배경. 공공기관 명칭 포함. 여백을 살린 임팩트 있는 타이포그래피.",
    "홍보물": "공공기관 CI/BI 가이드라인 준수 홍보물. 기관 대표 파란색 계열 색상 통일. 사인보드·안내판·홍보배너·서식 중 문서 내용에 적합한 형태. 여백과 텍스트의 균형, 기관 아이덴티티 명확히 표현. 흰 배경 또는 기관 색상 배경, 공식 디자인 스타일.",
    "기타이미지": "보고서 내용과 연관된 현장·시설·자연환경의 실사 사진 스타일 이미지. 사람 없이 장소·시설·환경 중심. 자연스럽고 현실적인 장면, 전문 다큐멘터리 사진 느낌. 16:9 가로 구도, 밝고 선명한 색감."
  };

  // AI 분석 결과 기반 한국어 이미지 생성 프롬프트
  function buildAiPrompt(ai, context, typeOverride) {
    const typeLabel = typeOverride || FIGURE_TYPE_TO_KIND[ai.figureType] || FIGURE_TYPE_LABEL[ai.figureType] || "다이어그램";
    const isEnglishOnly = (s) => !!(s && /[a-zA-Z]{3,}/.test(s) && !/[가-힣]/.test(s));

    // 목적: LLM이 합성한 purpose 우선. 영어 전용이면 sectionPath로 대체
    const purpose = (!isEnglishOnly(ai.purpose) && ai.purpose)
      ? ai.purpose
      : (ai.sectionPath || "보고서 내용 시각화");

    // 시각화 요소: suggestedContent에서 한글 항목만 (원문 문장이 아닌 명사·수치·라벨)
    const korItems = (ai.suggestedContent || [])
      .filter((s) => !isEnglishOnly(s))
      // 문장형(동사로 끝나거나 30자 초과)은 제외하고 명사구/수치만 사용
      .filter((s) => s.length <= 25 && !/[다했다이다니다습니다]$/.test(s));

    const guide = TYPE_GENERATION_GUIDE[typeLabel] ||
      "공공기관 보고서용, 흰 배경, 명확한 한글 라벨, 16:9 비율, 간결하고 전문적인 스타일";

    const parts = [`[이미지 목적] ${purpose}`];
    if (korItems.length) parts.push(`[시각화 요소] ${korItems.join(" / ")}`);
    parts.push(`[유형] ${typeLabel}`);
    parts.push(`[생성 방향] ${guide}`);
    return parts.join("\n");
  }

  // AI figures[] → 슬롯 목록 (보고서 내용 기반). 휴리스틱/K-water 분류를 대체한다.
  function buildAiSlots(figures) {
    const filtered = figures;
    // 동일 anchor+type+section 중복 제거 (anchor가 비어 있으면 purpose로 대체)
    const _seen = new Set();
    const deduped = filtered.filter((fig) => {
      const anchor = (fig.anchor_text || "").trim().toLowerCase();
      const dedupeAnchor = anchor || (fig.purpose || "").slice(0, 30).toLowerCase();
      const k = `${dedupeAnchor}|${fig.figure_type}|${fig.section_path || ""}`;
      if (_seen.has(k)) return false;
      _seen.add(k);
      return true;
    });
    return deduped.map((fig, i) => {
      const anchor = (fig.anchor_text || "").trim();
      const ai = {
        figureType: fig.figure_type || "concept_diagram",
        purpose: fig.purpose || "",
        suggestedContent: Array.isArray(fig.suggested_content) ? fig.suggested_content : [],
        imagePromptSeed: fig.image_prompt_seed || "",
        confidence: typeof fig.confidence === "number" ? fig.confidence : null,
        triggerScores: fig.trigger_scores || null,
        anchorText: anchor,
        positionHint: fig.position_hint || "",
        sectionPath: fig.section_path || ""
      };
      const blockIndex = findBlockForAnchor(anchor);
      const idx = blockIndex >= 0 ? blockIndex : 0;
      const block = state.blocks[idx];
      const context = contextAround(state.blocks, idx);
      const label = FIGURE_TYPE_LABEL[ai.figureType] || ai.figureType;
      const desc = ai.purpose ? ai.purpose.slice(0, 46) : (ai.sectionPath || `${label} 후보`);
      const uiType = FIGURE_TYPE_TO_KIND[ai.figureType] || "다이어그램";
      const slot = {
        id: i,
        blockIndex: idx,
        sectionIndex: block ? block.sectionIndex : 0,
        type: "diagram",
        style: "official",
        uiType,
        desc,
        title: ai.sectionPath || ai.purpose || "AI 추천 위치",
        context,
        prompt: buildAiPrompt(ai, context),
        image: "",
        ai,
        figureType: ai.figureType,
        confidence: ai.confidence,
        suggestedContent: ai.suggestedContent,
        imagePromptSeed: ai.imagePromptSeed,
        fromAi: true
      };
      const canvas = document.createElement("canvas");
      canvas.width = 1200;
      canvas.height = 760;
      drawImage(canvas, slot, false);
      slot.image = canvas.toDataURL("image/png");
      return slot;
    });
  }

  async function runAiAugment(file) {
    if (!window.API || typeof window.API.analyzeDocument !== "function") return;
    if (state.deferAiAugment) return;
    state.aiAnalyzing = true;
    state.selected = null;
    ui.editor.hidden = true;
    ui.emptyPanel.hidden = false;
    renderSlotList();
    renderDocument();
    setAiStatus("AI 분석 중…", "info");

    // 분석 중 메시지 순환 타이머 — 3초마다 다음 단계 메시지로 교체
    const _ANALYSIS_MSGS = [
      "AI가 문서 구조를 파악하고 있습니다…",
      "이미지가 필요한 위치를 탐색하고 있습니다…",
      "각 위치의 시각적 요소를 분석하고 있습니다…",
      "이미지 생성 프롬프트를 구성하고 있습니다…",
      "분석 결과를 정리하고 있습니다…",
    ];
    let _msgIdx = 0;
    const _msgTimer = setInterval(() => {
      if (!state.aiAnalyzing) { clearInterval(_msgTimer); return; }
      _msgIdx = (_msgIdx + 1) % _ANALYSIS_MSGS.length;
      const el = document.querySelector(".doc-loading-msg");
      if (el) {
        el.style.animation = "none";
        el.offsetHeight; // reflow
        el.style.animation = "";
        el.textContent = _ANALYSIS_MSGS[_msgIdx];
      }
    }, 3000);

    const health = await window.API.health();
    if (!health.ok) {
      state.aiAnalyzing = false;
      renderSlotList();
      renderDocument();
      setAiStatus(state.backendConnected ? "AI 분석 응답 지연" : "백엔드 미연결", "warn");
      state.aiImageAvailable = false;
      return;
    }
    state.backendConnected = true;
    const hb = health.body || {};
    state.aiImageAvailable = !!hb.image_gen_available;
    const provider = hb.llm_provider || "";
    if (hb.secret_error) setAiStatus("키 오류 (key.env.enc)", "warn");

    const res = await window.API.analyzeDocument(file);
    if (!res.ok || !res.body) {
      state.aiAnalyzing = false;
      renderSlotList();
      renderDocument();
      setAiStatus("분석 실패 (백엔드)", "warn");
      return;
    }
    const body = res.body;
    state.aiMeta = body.meta || {};
    const figures = Array.isArray(body.figures) ? body.figures : [];
    const errs = (body.meta && body.meta.planner_errors) || [];
    if (body.report_summary) state.reportSummary = body.report_summary;

    state.aiAnalyzing = false;
    if (figures.length) {
      // AI 분석이 슬롯을 전부 주도: 휴리스틱(K-water) 슬롯을 보고서 내용 기반으로 교체.
      state.slots = buildAiSlots(figures);
      state.selected = null;
      renderSlotList();
      renderDocument();
      if (state.slots.length) selectSlot(0);
      ui.slotCount.textContent = state.slots.length;
      setAiStatus(`완료 · 그림 ${figures.length}개`, "ok");
      toast(`AI 그림 분석 완료: ${figures.length}개`);
      // 이미지 생성이 가능하면 필요한 위치에 실제 이미지를 자동 생성·삽입한다.
      if (state.aiImageAvailable) {
        setAiStatus(`이미지 생성 중… (그림 ${figures.length}개)`, "info");
        try {
          const made = await generateAllSlots(false);
          setAiStatus(`완료 · 그림 ${figures.length}개 (이미지 ${made}개)`, "ok");
        } catch (_) {
          setAiStatus(`완료 · 그림 ${figures.length}개 (이미지 생성 일부 실패)`, "warn");
        }
      }
    } else if (errs.length) {
      renderSlotList();
      renderDocument();
      setAiStatus("LLM 호출 실패", "warn");  // 기존 휴리스틱 슬롯 유지
    } else {
      renderSlotList();
      renderDocument();
      setAiStatus("추천 0개", "ok");
    }
  }

  function renderDocument() {
    if (state.aiAnalyzing) {
      ui.documentView.innerHTML = `
        <div class="doc-loading">
          <span class="spinner doc-spinner"></span>
          <div class="loading-dots"><span></span><span></span><span></span></div>
          <div class="loading-bar"><div class="loading-bar-fill"></div></div>
          <p class="doc-loading-msg">AI가 문서를 분석하고 있습니다…</p>
        </div>`;
      return;
    }

    // HWPX 시각적 HTML이 있으면 이미지 포함 문서를 표시하고 슬롯을 하단에 추가
    if (state.hwpxDisplayHtml) {
      const slotSections = state.slots.map(slot => renderSlot(slot)).join('');
      const slotBlock = slotSections
        ? `<div style="margin-top:28px;padding-top:20px;border-top:1px solid var(--hairline)">${slotSections}</div>`
        : '';
      ui.documentView.innerHTML = state.hwpxDisplayHtml + slotBlock;
      ui.documentView.querySelectorAll("[data-select-slot]").forEach((btn) => {
        btn.addEventListener("click", () => selectSlot(Number(btn.dataset.selectSlot)));
      });
      markActiveSlot();
      return;
    }

    const chunks = [];
    const slotByBlock = new Map(state.slots.map((slot) => [slot.blockIndex, slot]));
    let titleDone = false;

    state.blocks.forEach((block, index) => {
      const slot = slotByBlock.get(index);
      if (block.kind === "text") {
        const text = block.text;
        const cls = [];
        const inlineStyle = block.hwpStyle ? hwpStyleToCss(block.hwpStyle) : "";
        if (block.hwpStyle) {
          chunks.push(renderHwpText(block, index));
          if (slot) chunks.push(renderSlot(slot));
          return;
        }
        if (!titleDone && text.length > 8 && !/^[\d가-힣]\s*[\.\)]/.test(text)) {
          cls.push("doc-title"); titleDone = true;
        } else if (/^\d+\.\s/.test(text)) {
          cls.push("doc-line", "heading", "h-num");          // 1. 개요
        } else if (/^[가나다라마바사아자차]\.\s/.test(text)) {
          cls.push("doc-line", "heading", "h-kor");          // 가. 나.
        } else if (/^□/.test(text)) {
          cls.push("doc-line", "bullet", "lv1");             // □ 대제목 불릿
        } else if (/^[○◯◦]/.test(text)) {
          cls.push("doc-line", "bullet", "lv2");             // ○ 중간 불릿
        } else if (/^[❍■▪◈‣·]/.test(text)) {
          cls.push("doc-line", "bullet", "lv3");
        } else if (/^[-–]\s/.test(text)) {
          cls.push("doc-line", "bullet", "lv3", "dash");     // - 세부
        } else if (/^※/.test(text)) {
          cls.push("doc-line", "note");                      // ※ 주석
        } else if (/^(붙임|첨부)/.test(text)) {
          cls.push("doc-line", "heading", "h-attach");
        } else {
          cls.push("doc-line");
        }
        chunks.push(`<p class="${cls.join(" ")}" contenteditable="true" data-block="${index}">${escapeHtml(text)}</p>`);
      } else {
        chunks.push(renderTable(block, index));
      }
      if (slot) chunks.push(renderSlot(slot));
    });
    ui.documentView.innerHTML = chunks.join("");
    ui.documentView.querySelectorAll("[data-select-slot]").forEach((btn) => {
      btn.addEventListener("click", () => selectSlot(Number(btn.dataset.selectSlot)));
    });
    bindEditableSync();
    markActiveSlot();
  }

  // contenteditable 문단·표 셀의 편집 내용을 state.blocks에 반영한다.
  // (원본을 페이지 이미지가 아니라 편집 가능한 형태로 보여주기 위함)
  function bindEditableSync() {
    ui.documentView.oninput = (event) => {
      const cell = event.target.closest("[data-cell]");
      if (cell) {
        const block = state.blocks[Number(cell.dataset.block)];
        const r = Number(cell.dataset.row);
        const c = Number(cell.dataset.col);
        if (block && block.rows && block.rows[r] && block.rows[r][c]) {
          block.rows[r][c].text = cell.innerText.replace(/ /g, " ");
        }
        return;
      }
      const para = event.target.closest("[data-block]");
      if (para) {
        const block = state.blocks[Number(para.dataset.block)];
        if (block && block.kind === "text") {
          block.text = para.innerText.replace(/ /g, " ");
          block.runs = null;   // 편집 후에는 단일 텍스트로 취급(다시 그릴 때 block.text 사용)
        }
      }
    };
  }

  // ── "원본 보기": 백엔드 LibreOffice→PDF→PNG 렌더로 원본과 동일한 페이지 이미지 표시 ──
  function renderPreviewPages(pages) {
    ui.previewPages.innerHTML = pages.map((page) => `<figure class="preview-page">
      <img class="doc-page-img" src="${page.image_url}" alt="원본 ${page.page_number} 페이지" loading="lazy">
      <figcaption class="preview-page-cap">${page.page_number} / ${pages.length}${page.source ? ` · ${escapeHtml(page.source)}` : ""}</figcaption>
    </figure>`).join("");
  }

  function renderPreviewLoading() {
    ui.previewPages.innerHTML = `<div class="preview-error preview-loading">
      <strong>원본 문서를 페이지 이미지로 렌더링하는 중입니다.</strong>
      <p>HWPX/HWP/PDF/DOCX 원본 형태를 우선 표시하고, 추출 텍스트는 AI 분석용으로만 사용합니다.</p>
    </div>`;
  }

  function previewErrorMessage(res) {
    const detail = res && res.body && res.body.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      const status = detail.renderer_status || {};
      const parts = [detail.message || "원본 렌더링에 실패했습니다."];
      const errors = Array.isArray(detail.render_errors) ? detail.render_errors.filter(Boolean) : [];
      if (!status.external_api_configured) parts.push("argodocument API 주소가 설정되지 않았습니다.");
      if (!status.libreoffice_path) parts.push("LibreOffice를 찾지 못했습니다.");
      if (status.hancom_com_available) parts.push("한컴 COM 변환도 실패했습니다.");
      if (errors.length) parts.push(`상세: ${errors.join(" / ")}`);
      return parts.join(" ");
    }
    return (res && res.error) || "원본 렌더링에 실패했습니다.";
  }

  function renderPreviewError(message) {
    ui.previewPages.innerHTML = `<div class="preview-error">
      <strong>원본 모양으로 읽을 수 없습니다.</strong>
      <p>${escapeHtml(message)}</p>
      <p>1번처럼 보려면 HWP/HWPX를 페이지 이미지로 변환할 수 있는 argodocument API, 한컴오피스 COM, 또는 LibreOffice 변환 경로가 필요합니다.</p>
    </div>`;
  }

  function setPreviewMode(on) {
    state.previewMode = on;
    ui.documentView.hidden = on;
    ui.previewPages.hidden = !on;
  }

  async function loadOriginalPreview() {
    if (!state.file) return;

    setPreviewMode(true);
    if (state.previewPages) {
      renderPreviewPages(state.previewPages);
      return;
    }

    ui.previewBtn.disabled = true;
    const prevLabel = ui.previewBtn.textContent;
    ui.previewBtn.textContent = "렌더링…";
    renderPreviewLoading();
    toast("원본 렌더링 중…");
    try {
      const res = await API.previewDocument(state.file);
      if (res.ok) {
        state.backendConnected = true;
        state.previewPages = res.body.pages;
        renderPreviewPages(state.previewPages);
        setAiStatus("원본 렌더 완료", "ok");
        toast(`원본 미리보기 ${state.previewPages.length}페이지`);
      } else {
        state.backendConnected = true;
        const message = previewErrorMessage(res);
        console.warn("Original preview failed:", message);
        setPreviewMode(false);
        toast("원본 미리보기를 만들 수 없어 텍스트 보기로 전환했습니다.", true);
      }
    } catch (_) {
      state.backendConnected = false;
      const message = "원본 렌더링에 실패했습니다.";
      setPreviewMode(false);
      toast("원본 미리보기를 만들 수 없어 텍스트 보기로 전환했습니다.", true);
    } finally {
      ui.previewBtn.disabled = false;
      ui.previewBtn.textContent = prevLabel;
    }
  }

  async function togglePreview() {
    if (!state.file) return;
    if (state.previewMode) {
      setPreviewMode(false);
      return;
    }
    await loadOriginalPreview();
  }

  function renderTable(table, index) {
    const idxAttr = Number.isFinite(index) ? ` data-block="${index}"` : "";

    // HWPX cell widths → 비례 colgroup
    const firstRow = table.rows[0] || [];
    const totalW = firstRow.reduce((s, c) => s + (c.width || 0), 0);
    let colgroup = "";
    if (totalW > 0) {
      colgroup = `<colgroup>${firstRow.map((c) => `<col style="width:${((c.width / totalW) * 100).toFixed(2)}%">`).join("")}</colgroup>`;
    }

    const rows = table.rows.map((row, r) => {
      const tag = r === 0 ? "th" : "td";
      const cells = row.map((cell, c) => {
        const bg = cell.background && /^#[0-9a-f]{6}$/i.test(cell.background) ? cell.background : "";
        const cellStyle = bg
          ? ` style="background:${bg}${isDarkHex(bg) ? ";color:#f0f0f0" : ""}"` : "";
        const colspanAttr = cell.colspan ? ` colspan="${cell.colspan}"` : "";
        const rowspanAttr = cell.rowspan ? ` rowspan="${cell.rowspan}"` : "";
        return `<${tag}${colspanAttr}${rowspanAttr} contenteditable="true" data-cell${idxAttr} data-row="${r}" data-col="${c}"${cellStyle}>${escapeHtml(cell.text).replace(/\n/g, "<br>") || "&nbsp;"}</${tag}>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    return `<table class="doc-table">${colgroup}${rows}</table>`;
  }

  function aiBadge(slot) {
    if (!slot.figureType) return "";
    const conf = slot.confidence != null ? ` · ${Math.round(slot.confidence * 100)}%` : "";
    return `<span class="ai-badge">${escapeHtml(slot.figureType)}${conf}</span>`;
  }

  function renderSlot(slot) {
    const src = slot.image || makeBlankDataUrl(slot);
    return `<section class="image-slot" id="slot-${slot.id}">
      <div class="slot-toolbar">
        <strong>${escapeHtml(slot.desc)}${aiBadge(slot)}</strong>
        <button type="button" data-select-slot="${slot.id}">옵션 열기</button>
      </div>
      <img class="slot-img" draggable="false" src="${src}" alt="${escapeHtml(slot.desc)}">
    </section>`;
  }

  function renderSlotList() {
    if (state.aiAnalyzing) {
      ui.slotList.innerHTML = `<div class="slot-loading"><span class="spinner"></span><div class="loading-dots"><span></span><span></span><span></span></div><span>AI 분석 중…</span></div>`;
      return;
    }
    ui.slotList.innerHTML = state.slots.map((slot, i) => {
      const typeLabel = slot.uiType || (FIGURE_TYPE_LABEL[slot.figureType] || slot.figureType || "이미지");
      const posHint = slot.ai && slot.ai.positionHint ? (slot.ai.positionHint === "before" ? "앞" : "뒤") : "";
      const posText = `섹션 ${slot.sectionIndex + 1}${posHint ? " · 앵커 " + posHint : ""}`;
      return `<button class="slot-item" draggable="true" type="button" data-id="${slot.id}" data-idx="${i}">
        <span class="drag-handle" aria-hidden="true">⠿</span>
        <b>${slot.id + 1}. ${escapeHtml(slot.desc)}</b>
        <span>${posText} · ${escapeHtml(typeLabel)}</span>
      </button>`;
    }).join("");
    ui.slotList.querySelectorAll(".slot-item").forEach((btn) => {
      btn.addEventListener("click", () => selectSlot(Number(btn.dataset.id)));
      btn.addEventListener("dragstart", (e) => {
        btn.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
      });
      btn.addEventListener("dragend", () => {
        btn.classList.remove("dragging");
        const draggedOrder = [...ui.slotList.querySelectorAll(".slot-item")].map((b) => Number(b.dataset.idx));
        // 문서 내 위치(blockIndex 오름차순)는 유지하면서 content만 스왑 → 이미지가 실제로 이동
        const byDocPos = [...state.slots].sort((a, b) => (a.blockIndex || 0) - (b.blockIndex || 0));
        const CONTENT_KEYS = ["image", "desc", "uiType", "type", "prompt", "title", "context",
                               "ai", "figureType", "fromAi", "imagePromptSeed", "suggestedContent", "confidence"];
        // 얕은 복사가 아닌 딥 스냅샷: 스왑 중 덮어쓰기로 인한 데이터 오염 방지
        const snapshots = state.slots.map((slot) => {
          const snap = {};
          CONTENT_KEYS.forEach((k) => { snap[k] = slot[k]; });
          return snap;
        });
        draggedOrder.forEach((oldIdx, newPos) => {
          const target = byDocPos[newPos];
          const snap = snapshots[oldIdx];
          CONTENT_KEYS.forEach((k) => { target[k] = snap[k]; });
        });
        byDocPos.forEach((s, j) => { s.id = j; });
        state.slots = byDocPos;
        renderSlotList();
        renderDocument();
      });
      btn.addEventListener("dragover", (e) => {
        e.preventDefault();
        const dragging = ui.slotList.querySelector(".dragging");
        if (!dragging || dragging === btn) return;
        const mid = btn.getBoundingClientRect().top + btn.getBoundingClientRect().height / 2;
        if (e.clientY < mid) btn.before(dragging); else btn.after(dragging);
      });
    });
    markActiveSlot();
  }

  // 컴팩트 점검: 진행 중인 절차(AI 상태) 또는 최종 점검 요약만 우측 상단에 작게 표시.
  function renderAudit() {
    const warnings = state.audit.filter((item) => item.level === "warn").length;
    let text;
    let warn;
    if (state.aiStatus) {
      text = state.aiStatus.text;
      warn = state.aiStatus.level === "warn";
    } else if (!state.audit.length) {
      text = "대기";
      warn = false;
    } else {
      text = warnings ? `${warnings}개 확인 필요` : "정상";
      warn = warnings > 0;
    }
    ui.auditStatus.textContent = text;
    ui.auditStatus.classList.toggle("has-warning", warn);
    if (ui.auditList) ui.auditList.innerHTML = "";  // 상세 목록은 컴팩트 모드에서 숨김
  }

  function setAiStatus(text, level = "info") {
    state.aiStatus = { text, level };
    renderAudit();
  }

  function markActiveSlot() {
    document.querySelectorAll(".slot-item, .image-slot").forEach((node) => node.classList.remove("is-active"));
    if (!state.selected) return;
    document.querySelector(`.slot-item[data-id="${state.selected.id}"]`)?.classList.add("is-active");
    document.getElementById(`slot-${state.selected.id}`)?.classList.add("is-active");
  }

  function makeBlankDataUrl(slot) {
    const canvas = document.createElement("canvas");
    canvas.width = 1200;
    canvas.height = 760;
    drawImage(canvas, slot, true);
    return canvas.toDataURL("image/png");
  }

  function selectSlot(id) {
    const slot = state.slots.find((item) => item.id === id);
    if (!slot) return;
    state.selected = slot;
    ui.emptyPanel.hidden = true;
    ui.editor.hidden = false;
    ui.descInput.value = slot.desc;
    ui.typeInput.value = slot.uiType || slot.type;
    ui.promptInput.value = slot.prompt;
    if (slot.image) {
      paintDataUrlToCanvas(ui.canvas, slot.image);
    } else {
      drawImage(ui.canvas, slot, true);
    }
    markActiveSlot();
    document.getElementById(`slot-${slot.id}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function updateSelectedFromEditor() {
    const slot = state.selected;
    if (!slot) return null;
    slot.desc = ui.descInput.value.trim() || slot.desc;
    slot.uiType = ui.typeInput.value;
    slot.prompt = ui.promptInput.value.trim() || buildAiPrompt(slot.ai || {}, slot.context || "");
    return slot;
  }

  function paintDataUrlToCanvas(canvas, dataUrl) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(true);
      };
      img.onerror = () => resolve(false);
      img.src = dataUrl;
    });
  }

  function drawCanvasSlot(slot) {
    drawImage(ui.canvas, slot, false);
    slot.image = ui.canvas.toDataURL("image/png");
  }

  // 백엔드(Gemini) 이미지 생성 시도 → 실패/미연결 시 canvas 렌더로 폴백.
  async function generateSlot(slot) {
    let usedAi = false;
    if (state.aiImageAvailable && window.API && typeof window.API.generateImage === "function") {
      const res = await window.API.generateImage({
        prompt: slot.prompt || slot.imagePromptSeed || "",
        description: slot.desc || "",
        category: slot.figureType || slot.type || "",
        style: slot.style || "",
        contextText: slot.context || ""
      });
      if (res && res.ok && res.body && res.body.image_url) {
        slot.image = res.body.image_url;
        usedAi = true;
        if (state.selected && state.selected.id === slot.id) {
          await paintDataUrlToCanvas(ui.canvas, slot.image);
        }
      }
    }
    if (!usedAi) {
      drawCanvasSlot(slot);
      if (state.selected && state.selected.id === slot.id) drawImage(ui.canvas, slot, false);
    }
    const img = document.querySelector(`#slot-${slot.id} .slot-img`);
    if (img) img.src = slot.image;
    return usedAi;
  }

  // 모든 슬롯의 이미지를 생성(가능하면 AI, 아니면 canvas)하고 문서에 다시 삽입한다.
  // 중복 프롬프트 슬롯은 이미 생성된 이미지를 재사용하여 불필요한 API 호출을 막는다.
  async function generateAllSlots(showToast = true) {
    let aiCount = 0;
    const promptToImage = new Map(); // prompt → 생성된 이미지 URL
    for (const slot of state.slots) {
      const key = slot.prompt || "";
      if (key && promptToImage.has(key)) {
        slot.image = promptToImage.get(key);
        const img = document.querySelector(`#slot-${slot.id} .slot-img`);
        if (img) img.src = slot.image;
        continue;
      }
      const usedAi = await generateSlot(slot);
      if (usedAi) aiCount++;
      if (key && slot.image) promptToImage.set(key, slot.image);
    }
    // 생성 후 동일 이미지 슬롯 중복 제거 (같은 데이터 URL = 사실상 동일 이미지)
    {
      const _seenImgs = new Set();
      const _prevLen = state.slots.length;
      state.slots = state.slots.filter((s) => {
        if (!s.image) return true;
        if (_seenImgs.has(s.image)) return false;
        _seenImgs.add(s.image);
        return true;
      });
      if (state.slots.length < _prevLen) toast(`중복 이미지 ${_prevLen - state.slots.length}개 제거됨`);
    }
    renderDocument();
    renderSlotList();
    if (state.selected) selectSlot(state.selected.id);
    if (showToast) {
      toast(aiCount ? `${state.slots.length}개 생성 (AI ${aiCount}개)` : `${state.slots.length}개 이미지를 생성했습니다.`);
    }
    return aiCount;
  }

  function downloadDataUrl(dataUrl, filename) {
    const a = document.createElement("a");
    a.href = dataUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function downloadText(text, filename, type = "application/json") {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    URL.revokeObjectURL(a.href);
    a.remove();
  }

  const crcTable = (() => {
    const table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[i] = c >>> 0;
    }
    return table;
  })();

  function crc32(bytes) {
    let crc = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) crc = crcTable[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    return (crc ^ 0xffffffff) >>> 0;
  }

  function concatBytes(chunks) {
    const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    chunks.forEach((chunk) => {
      out.set(chunk, offset);
      offset += chunk.length;
    });
    return out;
  }

  function writeU16(bytes, offset, value) {
    bytes[offset] = value & 0xff;
    bytes[offset + 1] = value >>> 8 & 0xff;
  }

  function writeU32(bytes, offset, value) {
    bytes[offset] = value & 0xff;
    bytes[offset + 1] = value >>> 8 & 0xff;
    bytes[offset + 2] = value >>> 16 & 0xff;
    bytes[offset + 3] = value >>> 24 & 0xff;
  }

  function makeZipStore(entries) {
    const localChunks = [];
    const centralChunks = [];
    let offset = 0;

    entries.forEach((entry) => {
      const nameBytes = utf8Encoder.encode(entry.name);
      const data = entry.bytes;
      const crc = crc32(data);
      const local = new Uint8Array(30 + nameBytes.length);
      writeU32(local, 0, 0x04034b50);
      writeU16(local, 4, 20);
      writeU16(local, 6, 0x0800);
      writeU16(local, 8, 0);
      writeU16(local, 10, 0);
      writeU16(local, 12, 0);
      writeU32(local, 14, crc);
      writeU32(local, 18, data.length);
      writeU32(local, 22, data.length);
      writeU16(local, 26, nameBytes.length);
      writeU16(local, 28, 0);
      local.set(nameBytes, 30);
      localChunks.push(local, data);

      const central = new Uint8Array(46 + nameBytes.length);
      writeU32(central, 0, 0x02014b50);
      writeU16(central, 4, 20);
      writeU16(central, 6, 20);
      writeU16(central, 8, 0x0800);
      writeU16(central, 10, 0);
      writeU16(central, 12, 0);
      writeU16(central, 14, 0);
      writeU32(central, 16, crc);
      writeU32(central, 20, data.length);
      writeU32(central, 24, data.length);
      writeU16(central, 28, nameBytes.length);
      writeU16(central, 30, 0);
      writeU16(central, 32, 0);
      writeU16(central, 34, 0);
      writeU16(central, 36, 0);
      writeU32(central, 38, 0);
      writeU32(central, 42, offset);
      central.set(nameBytes, 46);
      centralChunks.push(central);

      offset += local.length + data.length;
    });

    const centralDir = concatBytes(centralChunks);
    const end = new Uint8Array(22);
    writeU32(end, 0, 0x06054b50);
    writeU16(end, 8, entries.length);
    writeU16(end, 10, entries.length);
    writeU32(end, 12, centralDir.length);
    writeU32(end, 16, offset);
    writeU16(end, 20, 0);
    return concatBytes([...localChunks, centralDir, end]);
  }

  function dataUrlToBytes(dataUrl) {
    const base64 = String(dataUrl).split(",")[1] || "";
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function wrapText(ctx, text, x, y, maxWidth, lineHeight, maxLines = 4) {
    const words = String(text).replace(/\s+/g, " ").split(" ");
    let line = "";
    let lines = 0;
    for (const word of words) {
      const next = line ? `${line} ${word}` : word;
      if (ctx.measureText(next).width > maxWidth && line) {
        ctx.fillText(line, x, y);
        y += lineHeight;
        lines++;
        line = word;
        if (lines >= maxLines) return y;
      } else {
        line = next;
      }
    }
    if (line && lines < maxLines) ctx.fillText(line, x, y);
    return y + lineHeight;
  }

  function drawImage(canvas, slot, placeholder = false) {
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    const palette = slot.style === "field"
      ? ["#eef7f2", "#1d7d62", "#2f80ed", "#f3b23c", "#172033"]
      : slot.style === "clean"
        ? ["#f7fafc", "#0b6bcb", "#00a58d", "#f4c542", "#172033"]
        : ["#ffffff", "#0b6bcb", "#14866d", "#e8eef6", "#172033"];
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = palette[0];
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = "rgba(11,107,203,.06)";
    ctx.fillRect(0, 0, w, 96);
    ctx.fillStyle = palette[4];
    ctx.font = "700 46px Arial, sans-serif";
    wrapText(ctx, placeholder ? "이미지 생성 후보" : slot.desc, 54, 64, 1000, 52, 2);
    ctx.font = "500 24px Arial, sans-serif";
    ctx.fillStyle = "#5d6878";
    if (slot.figureType) {
      const label = FIGURE_TYPE_LABEL[slot.figureType] || slot.figureType;
      const sub = (state.docTitle ? state.docTitle + " · " : "") + label + " · 보고서 삽입 이미지";
      ctx.fillText(sub.slice(0, 46), 56, 132);
    } else {
      ctx.fillText("K-water · 2026 환경의 날 · 기후행동 출범식", 56, 132);
    }

    if (slot.figureType) {
      drawFigurePlaceholder(ctx, slot, palette);
    } else if (slot.type === "venue") drawVenue(ctx, palette);
    else if (slot.type === "campaign") drawCampaign(ctx, palette);
    else if (slot.type === "booth") drawBooth(ctx, palette);
    else if (slot.type === "water") drawWater(ctx, palette);
    else drawDiagram(ctx, palette);

    ctx.fillStyle = "rgba(23,32,51,.7)";
    ctx.font = "500 20px Arial, sans-serif";
    let footer;
    if (slot.figureType) {
      footer = (slot.suggestedContent && slot.suggestedContent.length)
        ? slot.suggestedContent.join(" · ")
        : (slot.ai && slot.ai.purpose) || slot.title || "";
    } else {
      footer = slot.context.split(/\n/).find((line) => /주제|목적|주요내용|홍보|행사장|음수대/.test(line)) || slot.title || "";
    }
    wrapText(ctx, String(footer).replace(/[<>#]/g, ""), 56, 704, 1050, 26, 2);
  }

  // figure_type별 범용 도안(보고서 내용 기반 placeholder). K-water 무관.
  function drawFigurePlaceholder(ctx, slot, p) {
    const t = slot.figureType;
    const cx = 130;
    const top = 200;
    if (t === "bar_chart") {
      const vals = [0.55, 0.8, 0.4, 0.95, 0.65, 0.5];
      const base = 540;
      vals.forEach((v, i) => {
        const bh = v * 300;
        ctx.fillStyle = i % 2 ? p[2] : p[1];
        roundRect(ctx, cx + i * 160, base - bh, 96, bh, 8); ctx.fill();
      });
      ctx.strokeStyle = p[4]; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(cx - 20, base); ctx.lineTo(1090, base); ctx.stroke();
    } else if (t === "line_chart") {
      const pts = [0.7, 0.5, 0.6, 0.35, 0.45, 0.25, 0.15];
      const base = 540, x0 = cx, dx = 150;
      ctx.strokeStyle = p[4]; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(x0 - 20, base); ctx.lineTo(1090, base); ctx.stroke();
      ctx.strokeStyle = p[1]; ctx.lineWidth = 5; ctx.beginPath();
      pts.forEach((v, i) => { const x = x0 + i * dx, y = base - (1 - v) * 320; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
      ctx.fillStyle = p[2];
      pts.forEach((v, i) => { ctx.beginPath(); ctx.arc(x0 + i * dx, base - (1 - v) * 320, 7, 0, Math.PI * 2); ctx.fill(); });
    } else if (t === "comparison_table") {
      const cols = 4, rows = 4, x = cx, y = top, cw = 230, rh = 78;
      for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
        ctx.fillStyle = r === 0 ? p[1] : (c === 0 ? p[3] : "#fff");
        ctx.fillRect(x + c * cw, y + r * rh, cw, rh);
        ctx.strokeStyle = "#cdd6e4"; ctx.lineWidth = 2;
        ctx.strokeRect(x + c * cw, y + r * rh, cw, rh);
      }
    } else if (t === "flow_chart" || t === "concept_diagram") {
      const items = (slot.suggestedContent && slot.suggestedContent.length ? slot.suggestedContent : ["A", "B", "C", "D"]).slice(0, 4);
      items.forEach((it, i) => {
        const x = cx + i * 245;
        ctx.fillStyle = i % 2 ? p[2] : p[1];
        roundRect(ctx, x, 330, 180, 120, 18); ctx.fill();
        ctx.fillStyle = "#fff"; ctx.font = "700 22px Arial, sans-serif";
        wrapText(ctx, String(it), x + 16, 378, 150, 24, 2);
        if (i < items.length - 1) { ctx.strokeStyle = p[4]; ctx.lineWidth = 4; ctx.beginPath(); ctx.moveTo(x + 188, 390); ctx.lineTo(x + 240, 390); ctx.stroke(); }
      });
    } else if (t === "roadmap") {
      const base = 400; ctx.strokeStyle = p[4]; ctx.lineWidth = 4;
      ctx.beginPath(); ctx.moveTo(cx, base); ctx.lineTo(1080, base); ctx.stroke();
      [0, 1, 2, 3].forEach((i) => {
        const x = cx + 60 + i * 300; ctx.fillStyle = p[1];
        ctx.beginPath(); ctx.arc(x, base, 14, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = p[3]; roundRect(ctx, x - 70, base - 110, 150, 70, 10); ctx.fill();
      });
    } else if (t === "map_layout" || t === "floor_plan" || t === "rendering") {
      ctx.fillStyle = "#eef2f8"; roundRect(ctx, cx, top, 950, 360, 18); ctx.fill();
      ctx.strokeStyle = p[1]; ctx.lineWidth = 4; ctx.strokeRect(cx + 40, top + 40, 360, 270);
      ctx.strokeRect(cx + 460, top + 40, 440, 130); ctx.strokeRect(cx + 460, top + 200, 440, 110);
    } else { // photo, cover_banner, default
      ctx.fillStyle = "#e8eef6"; roundRect(ctx, cx, top, 950, 360, 18); ctx.fill();
      ctx.strokeStyle = p[1]; ctx.lineWidth = 4; ctx.beginPath();
      ctx.moveTo(cx + 60, top + 300); ctx.lineTo(cx + 360, top + 120); ctx.lineTo(cx + 560, top + 250);
      ctx.lineTo(cx + 760, top + 90); ctx.lineTo(cx + 890, top + 300); ctx.stroke();
      ctx.fillStyle = p[3]; ctx.beginPath(); ctx.arc(cx + 760, top + 90, 36, 0, Math.PI * 2); ctx.fill();
    }
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function label(ctx, text, x, y, color = "#172033") {
    ctx.fillStyle = color;
    ctx.font = "700 23px Arial, sans-serif";
    ctx.fillText(text, x, y);
  }

  function drawVenue(ctx, p) {
    ctx.strokeStyle = "rgba(29,125,98,.28)";
    ctx.lineWidth = 18;
    ctx.beginPath();
    ctx.moveTo(82, 600);
    ctx.bezierCurveTo(220, 460, 270, 235, 475, 224);
    ctx.bezierCurveTo(700, 212, 805, 344, 1088, 276);
    ctx.stroke();
    ctx.fillStyle = "#dcefe6";
    roundRect(ctx, 90, 190, 1020, 430, 26);
    ctx.fill();
    ctx.strokeStyle = "#b8d9ca";
    ctx.lineWidth = 3;
    ctx.stroke();
    ctx.fillStyle = p[1];
    roundRect(ctx, 475, 230, 250, 70, 12);
    ctx.fill();
    label(ctx, "메인 무대", 545, 275, "#fff");
    ctx.fillStyle = "#ffffff";
    for (let r = 0; r < 5; r++) {
      for (let c = 0; c < 8; c++) {
        roundRect(ctx, 335 + c * 66, 340 + r * 38, 42, 22, 6);
        ctx.fill();
      }
    }
    label(ctx, "관람 구역", 540, 548);
    ctx.fillStyle = p[2];
    roundRect(ctx, 152, 278, 170, 110, 12);
    ctx.fill();
    label(ctx, "K-water\n홍보부스", 178, 326, "#fff");
    ctx.fillStyle = "#fff";
    roundRect(ctx, 865, 312, 150, 100, 12);
    ctx.fill();
    ctx.strokeStyle = p[2];
    ctx.lineWidth = 4;
    ctx.stroke();
    label(ctx, "QR 참여존", 888, 368, p[2]);
    ctx.fillStyle = p[3];
    roundRect(ctx, 840, 470, 190, 80, 12);
    ctx.fill();
    label(ctx, "스마트 음수대", 862, 518, "#3d2c00");
    ctx.strokeStyle = p[4];
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.moveTo(250, 570); ctx.lineTo(870, 570); ctx.lineTo(930, 420);
    ctx.stroke();
    label(ctx, "참여 동선", 668, 596);
  }

  function drawCampaign(ctx, p) {
    ctx.fillStyle = p[1];
    roundRect(ctx, 86, 190, 410, 410, 20);
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.font = "800 54px Arial, sans-serif";
    ctx.fillText("기후 행동", 138, 318);
    ctx.font = "700 34px Arial, sans-serif";
    ctx.fillText("녹색 대한민국", 138, 370);
    ctx.fillStyle = "rgba(255,255,255,.2)";
    for (let i = 0; i < 5; i++) ctx.fillRect(142 + i * 54, 438, 34, 34);
    ctx.fillStyle = "#fff";
    ctx.fillRect(690, 242, 290, 290);
    ctx.fillStyle = p[4];
    for (let y = 0; y < 7; y++) for (let x = 0; x < 7; x++) {
      if ((x + y) % 2 === 0 || x < 2 && y < 2 || x > 4 && y < 2 || x < 2 && y > 4) {
        ctx.fillRect(714 + x * 34, 266 + y * 34, 24, 24);
      }
    }
    label(ctx, "QR로 실천 참여", 736, 580, p[1]);
  }

  function drawBooth(ctx, p) {
    ctx.fillStyle = "#e9f1fb";
    roundRect(ctx, 120, 250, 960, 290, 18);
    ctx.fill();
    ctx.fillStyle = p[1];
    roundRect(ctx, 188, 220, 260, 235, 16);
    ctx.fill();
    label(ctx, "K-water\n홍보부스", 235, 322, "#fff");
    ctx.fillStyle = "#fff";
    roundRect(ctx, 520, 300, 235, 95, 12);
    ctx.fill();
    label(ctx, "상담 테이블", 574, 357);
    ctx.fillStyle = p[2];
    roundRect(ctx, 828, 245, 170, 250, 12);
    ctx.fill();
    label(ctx, "기후행동\n배너", 862, 346, "#fff");
    ctx.strokeStyle = p[4];
    ctx.lineWidth = 3;
    ctx.strokeRect(130, 565, 940, 1);
    label(ctx, "설치 · 안내 · 참여 유도 · 운영관리", 372, 620);
  }

  function drawWater(ctx, p) {
    ctx.fillStyle = "#eaf7ff";
    roundRect(ctx, 130, 220, 330, 330, 22);
    ctx.fill();
    ctx.fillStyle = p[2];
    roundRect(ctx, 222, 260, 150, 230, 20);
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.beginPath();
    ctx.arc(297, 360, 48, 0, Math.PI * 2);
    ctx.fill();
    label(ctx, "수돗물", 258, 368, p[2]);
    ctx.strokeStyle = p[1];
    ctx.lineWidth = 5;
    ctx.beginPath();
    ctx.moveTo(510, 370); ctx.lineTo(680, 370); ctx.lineTo(680, 290);
    ctx.stroke();
    ctx.fillStyle = p[1];
    roundRect(ctx, 710, 245, 310, 250, 18);
    ctx.fill();
    label(ctx, "탈플라스틱\n기후행동", 770, 350, "#fff");
    ctx.font = "600 25px Arial, sans-serif";
    ctx.fillStyle = p[4];
    ctx.fillText("이동형 스마트 음수대 운영", 430, 598);
  }

  function drawDiagram(ctx, p) {
    const items = ["목적", "참여", "지원", "성과"];
    items.forEach((item, i) => {
      const x = 130 + i * 255;
      ctx.fillStyle = i % 2 ? p[2] : p[1];
      roundRect(ctx, x, 300, 170, 120, 18);
      ctx.fill();
      label(ctx, item, x + 62, 370, "#fff");
      if (i < items.length - 1) {
        ctx.strokeStyle = p[4];
        ctx.lineWidth = 4;
        ctx.beginPath();
        ctx.moveTo(x + 180, 360); ctx.lineTo(x + 240, 360);
        ctx.stroke();
      }
    });
    label(ctx, "보고서 문맥 기반 시각화", 430, 545);
  }

  // ═══════════════════════════════════════════════════════════════
  // HWPX 시각적 렌더링 (프로토타입 방식: JSZip + 섹션 XML 워커)
  // 임베디드 이미지를 포함한 문서를 HTML로 렌더링한다.
  // ═══════════════════════════════════════════════════════════════

  // ── PDF.js 클라이언트 렌더링 ────────────────────────────────────
  let _pdfjsPromise = null;
  function loadPdfJs() {
    if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
    if (_pdfjsPromise) return _pdfjsPromise;
    _pdfjsPromise = new Promise((resolve, reject) => {
      let n = 0;
      const t = setInterval(() => {
        if (window.pdfjsLib) { clearInterval(t); resolve(window.pdfjsLib); }
        else if (n++ > 80) { clearInterval(t); reject(new Error('PDF.js를 불러올 수 없습니다')); }
      }, 100);
    });
    return _pdfjsPromise;
  }

  async function parsePdfForDisplay(file) {
    const pdfjsLib = await loadPdfJs();
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';
    const buf = await file.arrayBuffer();
    const pdf = await pdfjsLib.getDocument({ data: buf }).promise;
    const pages = [];
    const scale = window.devicePixelRatio >= 2 ? 1.5 : 2;
    for (let i = 1; i <= pdf.numPages; i++) {
      const page = await pdf.getPage(i);
      const vp = page.getViewport({ scale });
      const canvas = document.createElement('canvas');
      canvas.width = vp.width;
      canvas.height = vp.height;
      await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise;
      pages.push({
        page_number: i,
        image_url: canvas.toDataURL('image/jpeg', 0.92),
        width: vp.width,
        height: vp.height,
        source: 'pdf.js'
      });
    }
    return pages;
  }

  let _jszipPromise = null;
  function loadJSZipLib() {
    if (window.JSZip) return Promise.resolve(window.JSZip);
    if (_jszipPromise) return _jszipPromise;
    _jszipPromise = new Promise((resolve, reject) => {
      let n = 0;
      const t = setInterval(() => {
        if (window.JSZip) { clearInterval(t); resolve(window.JSZip); }
        else if (n++ > 60) { clearInterval(t); reject(new Error('JSZip을 불러올 수 없습니다')); }
      }, 100);
    });
    return _jszipPromise;
  }

  async function parseHwpxForDisplay(buffer) {
    const JSZip = await loadJSZipLib();
    const zip = await JSZip.loadAsync(buffer);

    // 1) content.hpf 매니페스트에서 이미지 ID → href/mediaType 맵 구성
    const manifest = {};
    const hpfFile = zip.file('Contents/content.hpf');
    if (hpfFile) {
      const text = await hpfFile.async('string');
      const itemRe = /<opf:item\b([^/>]*)\/?>/g;
      let m;
      while ((m = itemRe.exec(text)) !== null) {
        const attrs = {};
        const aRe = /(\w[\w-]*)\s*=\s*"([^"]*)"/g;
        let am;
        while ((am = aRe.exec(m[1])) !== null) attrs[am[1]] = am[2];
        if (attrs.id) manifest[attrs.id] = { href: attrs.href, mediaType: attrs['media-type'] };
      }
    }

    // 2) 모든 이미지를 data-URL로 사전 로드
    const imageDataUrls = {};
    for (const id of Object.keys(manifest)) {
      const meta = manifest[id];
      if (!/^image\//i.test(meta.mediaType || '')) continue;
      const imgFile = zip.file(meta.href);
      if (!imgFile) continue;
      const b64 = await imgFile.async('base64');
      imageDataUrls[id] = `data:${meta.mediaType};base64,${b64}`;
    }

    // 3) 섹션 XML을 순서대로 렌더링
    const sectionNames = Object.keys(zip.files)
      .filter(n => /Contents\/section\d+\.xml$/i.test(n))
      .sort();
    if (!sectionNames.length) throw new Error('HWPX 본문 섹션이 없습니다');

    const out = [];
    for (const name of sectionNames) {
      const xml = await zip.file(name).async('string');
      out.push(_renderHwpxSection(xml, imageDataUrls));
    }

    // 한국어 문서 스타일 (인라인 scoped CSS)
    const scoped = `<style>
      .hwpx-doc {
        font-family: 'HCR Dotum','Apple SD Gothic Neo','Pretendard Variable','Pretendard','Malgun Gothic','맑은 고딕',sans-serif;
        font-size: 14.5px; line-height: 1.78; color: #111;
      }
      .hwpx-doc h2 {
        font-family: 'HCR Dotum','Apple SD Gothic Neo','Pretendard',sans-serif;
        font-size: 17px; font-weight: 700; color: #111;
        margin: 22px 0 10px; letter-spacing: -0.01em;
      }
      .hwpx-doc p { margin: 0 0 6px; white-space: pre-wrap; word-break: keep-all; }
      .hwpx-doc .hwpx-callout {
        border: 1px solid #999; padding: 10px 14px;
        margin: 10px 0 18px; font-weight: 600; background: transparent;
      }
      .hwpx-doc .hwpx-note { font-size: 12.5px; color: #333; padding-left: 20px; margin: 2px 0 6px; }
      .hwpx-doc .hwpx-table {
        width: 100%; border-collapse: collapse; margin: 10px 0;
        table-layout: auto; border: 1px solid #888;
      }
      .hwpx-doc .hwpx-table td {
        border: 1px solid #888; padding: 6px 10px;
        vertical-align: top; font-size: 13.5px; line-height: 1.65;
      }
      .hwpx-doc .hwpx-table.hwpx-title { border: 1.5px solid #2c4d8c; margin: 6px 0 14px; }
      .hwpx-doc .hwpx-table.hwpx-title td {
        border: 1.5px solid #2c4d8c; color: #2c4d8c;
        font-weight: 700; font-size: 16px; text-align: center;
        padding: 10px 14px; letter-spacing: -0.005em;
      }
      .hwpx-doc .hwpx-table.hwpx-restoration { border: 1.5px dashed #c0392b; margin: 12px 0; }
      .hwpx-doc .hwpx-table.hwpx-restoration td {
        border: 1.5px dashed #c0392b; padding: 0;
        text-align: center; vertical-align: middle;
      }
      .hwpx-doc .hwpx-table.hwpx-restoration tr:first-child td {
        background: #b80000; color: #fff; font-weight: 700; padding: 6px 10px;
      }
      .hwpx-doc .hwpx-embedded { display: block; margin: 0 auto; max-width: 100%; height: auto; }
      .hwpx-doc .hwpx-img-missing {
        display: block; padding: 18px; background: #fbe3e3;
        color: #b80000; text-align: center; font-size: 12px;
      }
    </style>`;
    return scoped + `<div class="hwpx-doc">${out.join('\n')}</div>`;
  }

  function _renderHwpxSection(xml, imageDataUrls) {
    let doc;
    try { doc = new DOMParser().parseFromString(xml, 'application/xml'); }
    catch (e) { return ''; }
    if (doc.getElementsByTagName('parsererror').length) {
      return _regexExtractHwpx(xml);
    }
    const emitted = new WeakSet();
    const buf = [];
    _walkHwpx(doc.documentElement, buf, emitted, imageDataUrls);
    return buf.join('\n');
  }

  function _walkHwpx(node, buf, emitted, images) {
    if (!node || emitted.has(node) || node.nodeType !== 1) return;
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType !== 1 || emitted.has(child)) continue;
      const tag = child.localName;
      if (tag === 'p') {
        const html = _renderHwpxP(child, emitted, images);
        if (html) buf.push(html);
      } else if (tag === 'tbl') {
        buf.push(_renderHwpxTbl(child, emitted, images));
      } else if (tag === 'pic') {
        const img = _renderHwpxPic(child, images);
        if (img) buf.push(img);
        _markEmitted(child, emitted);
      } else {
        _walkHwpx(child, buf, emitted, images);
      }
    }
  }

  function _markEmitted(node, set) {
    set.add(node);
    for (const c of Array.from(node.childNodes)) {
      if (c.nodeType === 1) _markEmitted(c, set);
    }
  }

  function _renderHwpxP(p, emitted, images) {
    if (emitted.has(p)) return '';
    emitted.add(p);
    const pieces = [];
    let textBuf = '';

    function flushText() {
      const t = textBuf.replace(/ /g, ' ').trim();
      textBuf = '';
      if (!t) return;
      const isHead    = t.length <= 50 && /^\s*[0-9]+\s*\.\s*[가-힣A-Za-z]/.test(t);
      const isCallout = /^\s*[◈◆]/.test(t);
      const isNote    = /^\s*\*\s/.test(t);
      if (isHead)         pieces.push(`<h2>${escapeHtml(t)}</h2>`);
      else if (isCallout) pieces.push(`<p class="hwpx-callout">${escapeHtml(t)}</p>`);
      else if (isNote)    pieces.push(`<p class="hwpx-note">${escapeHtml(t)}</p>`);
      else                pieces.push(`<p>${escapeHtml(t)}</p>`);
    }

    function visit(n) {
      if (!n || emitted.has(n)) return;
      if (n.nodeType === 1) {
        const tag = n.localName;
        if (tag === 't') { textBuf += n.textContent || ''; emitted.add(n); return; }
        if (tag === 'p') {
          flushText();
          const html = _renderHwpxP(n, emitted, images);
          if (html) pieces.push(html);
          return;
        }
        if (tag === 'tbl') { flushText(); pieces.push(_renderHwpxTbl(n, emitted, images)); return; }
        if (tag === 'pic') {
          flushText();
          const img = _renderHwpxPic(n, images);
          if (img) pieces.push(img);
          _markEmitted(n, emitted);
          return;
        }
        for (const c of Array.from(n.childNodes)) visit(c);
      }
    }
    for (const c of Array.from(p.childNodes)) visit(c);
    flushText();
    return pieces.join('\n');
  }

  function _renderHwpxTbl(tbl, emitted, images) {
    if (emitted.has(tbl)) return '';
    emitted.add(tbl);

    const rows = [];
    let rowCount = 0, cellCount = 0;
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
        const cellPieces = [];
        for (const sl of Array.from(tc.childNodes)) {
          if (sl.nodeType !== 1 || sl.localName !== 'subList') continue;
          emitted.add(sl);
          for (const innerP of Array.from(sl.childNodes)) {
            if (innerP.nodeType !== 1 || innerP.localName !== 'p') continue;
            const html = _renderHwpxP(innerP, emitted, images);
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

    let extraClass = '';
    const firstRow = allCellTexts[0] || [];
    const firstRowJoined = firstRow.join(' ');
    if (rowCount === 1 && cellCount === 1 && firstRow[0] && firstRow[0].length <= 80) {
      extraClass = ' hwpx-title';
    } else if (/복원\s*[전후]/.test(firstRowJoined)) {
      extraClass = ' hwpx-restoration';
    }
    return `<table class="hwpx-table${extraClass}">${rows.join('')}</table>`;
  }

  function _renderHwpxPic(pic, images) {
    const imgs = pic.getElementsByTagNameNS('*', 'img');
    if (!imgs.length) return '';
    const ref = imgs[0].getAttribute('binaryItemIDRef');
    if (!ref) return '';
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
    if (!dataUrl) return `<div class="hwpx-img-missing">[그림 누락: ${escapeHtml(ref)}]</div>`;
    return `<img class="hwpx-embedded" src="${dataUrl}" alt="HWPX 그림 ${escapeHtml(ref)}" style="${widthCss}">`;
  }

  function _regexExtractHwpx(xml) {
    const out = [];
    const re = /<(?:hp:)?p\b[^>]*>([\s\S]*?)<\/(?:hp:)?p>/gi;
    const tRe = /<(?:hp:)?t\b[^>]*>([\s\S]*?)<\/(?:hp:)?t>/gi;
    let m;
    while ((m = re.exec(xml)) !== null) {
      let line = '';
      let tm;
      tRe.lastIndex = 0;
      while ((tm = tRe.exec(m[1])) !== null) line += tm[1];
      line = line.replace(/<[^>]+>/g, ' ').replace(/&[a-z]+;/g, ' ').replace(/\s+/g, ' ').trim();
      if (!line) continue;
      out.push(line.length <= 40 ? `<h2>${escapeHtml(line)}</h2>` : `<p>${escapeHtml(line)}</p>`);
    }
    return out.join('\n');
  }

  async function handleFile(file) {
    const ext = documentExtension(file);
    if (!file || !["hwpx", "hwp", "pdf", "docx"].includes(ext)) {
      toast("HWPX, HWP, PDF 또는 DOCX 파일만 업로드할 수 있습니다.", true);
      return;
    }
    try {
      state.file = file;
      state.aiStatus = null;
      state.aiImageAvailable = false;
      state.backendConnected = false;
      state.hwpxDisplayHtml = null;
      setProgress(.15);
      const parsed = ext === "hwp" || ext === "pdf"
        ? {
            zip: null,
            sections: [`server-rendered-${ext}`],
            blocks: [{ kind: "text", sectionIndex: 0, text: file.name.replace(/\.(hwp|pdf)$/i, "") || `${ext.toUpperCase()} document`, redraftable: false }],
            audit: [makeAuditItem("info", `${ext.toUpperCase()} document`, "원본 페이지 미리보기와 AI 분석은 백엔드 렌더러/파서가 처리합니다.")],
            packageInfo: { sectionNames: [`server-rendered-${ext}`], audit: [] }
          }
        : (ext === "docx" ? await parseDocx(file) : await parseHwpx(file));
      setProgress(.58);

      // HWPX: 임베디드 이미지 포함 시각적 HTML 생성 (비동기, 블로킹 없음)
      if (ext === "hwpx") {
        file.arrayBuffer().then(buf => parseHwpxForDisplay(buf))
          .then(html => { state.hwpxDisplayHtml = html; renderDocument(); })
          .catch(() => { /* 폴백: 기존 블록 렌더링 유지 */ });
      }
      state.zip = parsed.zip || null;
      state.entries = parsed.zip ? parsed.zip.entries : new Map();
      state.sections = parsed.sections;
      state.blocks = parsed.blocks;
      state.audit = parsed.audit;
      state.packageInfo = parsed.packageInfo;
      state.slots = findSlots(parsed.blocks);
      state.selected = null;
      state.slots.forEach((slot) => {
        const canvas = document.createElement("canvas");
        canvas.width = 1200;
        canvas.height = 760;
        drawImage(canvas, slot, false);
        slot.image = canvas.toDataURL("image/png");
      });
      setProgress(.86);
      showWorkspace();
      setProgress(1);
      setTimeout(() => setProgress(0), 400);
      toast(`분석 완료: 이미지 필요 지점 ${state.slots.length}개`);
      if (ext === "hwp") {
        loadOriginalPreview().catch(() => {});
      } else if (ext === "pdf") {
        // PDF.js로 클라이언트에서 바로 렌더링, 실패 시 서버 렌더링으로 폴백
        parsePdfForDisplay(file)
          .then(pages => {
            state.previewPages = pages;
            setPreviewMode(true);
            renderPreviewPages(pages);
          })
          .catch(() => loadOriginalPreview().catch(() => {}));
      }
      // 편집 가능한 문서 뷰를 유지한 채 AI 보강(그림 위치 분석 + 이미지 생성)만 비차단으로 실행
      runAiAugment(file).catch(() => setAiStatus("분석 오류", "warn"));
    } catch (err) {
      setProgress(0);
      toast(err.message || "파일 분석에 실패했습니다.", true);
    }
  }

  function showWorkspace() {
    ui.uploadView.hidden = true;
    ui.workspace.hidden = false;
    state.previewPages = null;
    setPreviewMode(false);   // 기본을 편집 가능한 문서 뷰로(페이지 이미지는 "원본 보기"에서 on-demand)
    const firstText = state.blocks.find((b) => b.kind === "text" && b.text.trim().length > 6);
    state.docTitle = firstText ? firstText.text.trim().slice(0, 60) : (state.file ? state.file.name.replace(/\.(hwpx|hwp|pdf|docx)$/i, "") : "");
    ui.fileName.textContent = state.file.name;
    ui.slotCount.textContent = state.slots.length;
    ui.sectionCount.textContent = state.sections.length;
    renderAudit();
    renderSlotList();
    renderDocument();
    if (state.slots.length) selectSlot(0);
  }

  function exportPlan() {
    const plan = {
      source_file: state.file?.name || "",
      generated_at: new Date().toISOString(),
      goal: "그림이 제거된 HWPX 보고서에서 이미지 삽입 필요 지점을 감지하고 문맥 기반 이미지를 생성",
      applied_skill: {
        source: "test/hwpx-rekian-master",
        workflow: "HWPX XML-first analysis with COM escalation guardrails",
        guardrails: [
          "mimetype은 첫 번째 ZIP 엔트리이며 무압축이어야 함",
          "mimetype 내용은 application/hwp+zip이어야 함",
          "표 또는 중첩 문단을 품은 컨테이너 문단은 일반 문단처럼 평탄화하지 않음",
          "header.xml의 charPr/paraPr/borderFill ID 참조를 보존",
          "header.xml의 itemCnt와 실제 스타일 자식 수를 일치시킴",
          "표 셀, 고정 개체, 텍스트 박스는 원본 구조를 유지하고 필요 시 COM 흐름으로 전환",
          "본문 교체 후 linesegarray는 한글에서 재계산되도록 제거",
          "빈 불릿은 template_filler 방식으로 기존 t 노드를 제자리 치환"
        ]
      },
      report_summary: state.reportSummary || "",
      ai_meta: state.aiMeta || null,
      audit: state.audit,
      slots: state.slots.map((slot) => ({
        id: slot.id + 1,
        section: slot.sectionIndex + 1,
        description: slot.desc,
        type: slot.type,
        style: slot.style,
        title: slot.title,
        prompt: slot.prompt,
        context: slot.context,
        ...aiPlanFields(slot)
      }))
    };
    downloadText(JSON.stringify(plan, null, 2), `${sanitizeName(state.file?.name)}_image_plan.json`);
  }

  function aiPlanFields(slot) {
    if (!slot.ai && !slot.figureType) return {};
    const ai = slot.ai || {};
    return {
      from_ai: !!slot.fromAi,
      figure_type: slot.figureType || ai.figureType || null,
      purpose: ai.purpose || null,
      anchor_text: ai.anchorText || null,
      position_hint: ai.positionHint || null,
      confidence: slot.confidence != null ? slot.confidence : (ai.confidence != null ? ai.confidence : null),
      trigger_scores: ai.triggerScores || null,
      suggested_content: ai.suggestedContent || null,
      image_prompt_seed: slot.imagePromptSeed || ai.imagePromptSeed || null
    };
  }

  function buildPlanObject() {
    return {
      source_file: state.file?.name || "",
      generated_at: new Date().toISOString(),
      goal: "HWPX 원본 구조를 보존하면서 생성 PNG와 삽입 계획을 패키지 내부에 포함",
      report_summary: state.reportSummary || "",
      ai_meta: state.aiMeta || null,
      slots: state.slots.map((slot) => ({
        id: slot.id + 1,
        section: slot.sectionIndex + 1,
        block_index: slot.blockIndex,
        image_href: `BinData/visual_flow_${String(slot.id + 1).padStart(2, "0")}.png`,
        description: slot.desc,
        type: slot.type,
        style: slot.style,
        title: slot.title,
        prompt: slot.prompt,
        context: slot.context,
        ...aiPlanFields(slot)
      })),
      audit: state.audit
    };
  }

  function appendContentItems(xml, items) {
    if (!xml || !items.length || !/<[^>]*:manifest\b/.test(xml)) return xml;
    const missing = items.filter((item) => !xml.includes(`href="${item.href}"`));
    if (!missing.length) return xml;
    const insert = missing.map((item) => `    <opf:item id="${item.id}" href="${item.href}" media-type="${item.type}"/>`).join("\n");
    return xml.replace(/(\s*<\/[^>]*:manifest>)/, `\n${insert}$1`);
  }

  function appendOdfManifestItems(xml, items) {
    if (!xml || !items.length || !/<[^>]*:manifest\b/.test(xml)) return xml;
    const missing = items.filter((item) => !xml.includes(`full-path="${item.href}"`));
    if (!missing.length) return xml;
    const insert = missing.map((item) => `  <odf:file-entry odf:media-type="${item.type}" odf:full-path="${item.href}"/>`).join("\n");
    if (/<[^>]*:manifest\b[^>]*\/>/.test(xml)) {
      return xml.replace(/<([^>\s]+:manifest)(\b[^>]*)\/>/, `<$1$2>\n${insert}\n</$1>`);
    }
    return xml.replace(/(\s*<\/[^>]*:manifest>)/, `\n${insert}$1`);
  }

  async function exportWorkingHwpx() {
    if (!state.zip || !state.file) {
      toast("작업본 HWPX 저장은 HWPX 파일에서만 사용할 수 있습니다.", true);
      return;
    }
    state.slots.forEach((slot) => {
      if (!slot.image) {
        const canvas = document.createElement("canvas");
        canvas.width = 1200;
        canvas.height = 760;
        drawImage(canvas, slot, false);
        slot.image = canvas.toDataURL("image/png");
      }
    });

    const planBytes = utf8Encoder.encode(JSON.stringify(buildPlanObject(), null, 2));
    const additions = new Map();
    const contentItems = [{
      id: "visual_flow_image_plan",
      href: "Contents/visual_flow_image_plan.json",
      type: "application/json"
    }];
    const odfItems = [{
      href: "Contents/visual_flow_image_plan.json",
      type: "application/json"
    }];

    additions.set("Contents/visual_flow_image_plan.json", planBytes);
    state.slots.forEach((slot) => {
      const name = `BinData/visual_flow_${String(slot.id + 1).padStart(2, "0")}.png`;
      additions.set(name, dataUrlToBytes(slot.image));
      contentItems.push({ id: `visual_flow_${slot.id + 1}`, href: name, type: "image/png" });
      odfItems.push({ href: name, type: "image/png" });
    });

    const entries = [];
    const originalNames = Array.from(state.zip.entries.keys());
    const orderedNames = originalNames.includes("mimetype")
      ? ["mimetype", ...originalNames.filter((name) => name !== "mimetype")]
      : ["mimetype", ...originalNames];

    for (const name of orderedNames) {
      let bytes;
      if (name === "mimetype" && !state.zip.entries.has(name)) {
        bytes = utf8Encoder.encode("application/hwp+zip");
      } else if (name === "Contents/content.hpf" && state.zip.entries.has(name)) {
        const xml = appendContentItems(await state.zip.getText(name), contentItems);
        bytes = utf8Encoder.encode(xml);
      } else if (name === "META-INF/manifest.xml" && state.zip.entries.has(name)) {
        const xml = appendOdfManifestItems(await state.zip.getText(name), odfItems);
        bytes = utf8Encoder.encode(xml);
      } else if (state.zip.entries.has(name)) {
        bytes = await state.zip.getBytes(name);
      } else {
        continue;
      }
      entries.push({ name, bytes });
    }

    if (!state.zip.entries.has("Contents/content.hpf")) {
      additions.set("Contents/content.hpf", utf8Encoder.encode(`<?xml version='1.0' encoding='UTF-8'?>\n<opf:package xmlns:opf="http://www.idpf.org/2007/opf/">\n  <opf:manifest>\n${contentItems.map((item) => `    <opf:item id="${item.id}" href="${item.href}" media-type="${item.type}"/>`).join("\n")}\n  </opf:manifest>\n</opf:package>`));
    }

    additions.forEach((bytes, name) => {
      if (!entries.some((entry) => entry.name === name)) entries.push({ name, bytes });
    });

    const hwpxBytes = makeZipStore(entries);
    const href = URL.createObjectURL(new Blob([hwpxBytes], { type: "application/hwp+zip" }));
    const a = document.createElement("a");
    a.href = href;
    a.download = `${sanitizeName(state.file.name).replace(/\.hwpx$/i, "")}_visual_flow.hwpx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
    toast("작업본 HWPX 저장을 시작했습니다.");
  }

  // 백엔드 /api/v1/documents/assemble 를 호출해 원본 서식 보존 HWPX 다운로드.
  // hwpx_assembler.py (Python)가 section0.xml에 hp:pic 을 실제로 삽입한다.
  async function assembleWithBackend() {
    if (!state.file) {
      toast("먼저 HWPX 파일을 업로드하세요.", true);
      return;
    }
    if (!state.file.name.toLowerCase().endsWith(".hwpx")) {
      toast("서버 조립은 HWPX 파일에서만 사용할 수 있습니다.", true);
      return;
    }
    if (!window.API || !state.backendConnected) {
      toast("백엔드가 연결되어 있지 않습니다 (서버를 먼저 실행하세요).", true);
      return;
    }

    // 이미지 없는 슬롯은 canvas로 먼저 생성
    state.slots.forEach((slot) => {
      if (!slot.image) {
        const cv = document.createElement("canvas");
        cv.width = 1200; cv.height = 760;
        drawImage(cv, slot, false);
        slot.image = cv.toDataURL("image/png");
      }
    });

    // URL 이미지(Leonardo 등)를 data URL로 변환
    async function toDataUrl(src) {
      if (!src) return "";
      if (src.startsWith("data:")) return src;
      try {
        const res = await fetch(src);
        const blob = await res.blob();
        return await new Promise((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.readAsDataURL(blob);
        });
      } catch (_) {
        return src;
      }
    }

    // 드래그로 바뀐 시각적 순서를 HWPX 삽입 순서에 반영:
    // 슬롯을 원래 문서 위치(blockIndex) 순으로 정렬한 뒤,
    // 현재 state.slots 순서(드래그 후)에서 같은 인덱스의 이미지를 매핑한다.
    const slotsWithImage = state.slots.filter((s) => !!s.image);
    const sortedByPos = [...slotsWithImage].sort((a, b) => (a.blockIndex || 0) - (b.blockIndex || 0));
    const imagesByVisualOrder = slotsWithImage.map((s) => s.image);

    const resolvedImages = await Promise.all(imagesByVisualOrder.map(toDataUrl));

    const figures = sortedByPos.map((s, i) => ({
      anchor_text: (s.ai && s.ai.anchorText) || s.title || "",
      position_hint: (s.ai && s.ai.positionHint) || "after",
      image_base64: resolvedImages[i] || "",
      image_mime: "image/png",
    }));

    if (!figures.length) {
      toast("삽입할 이미지가 없습니다.", true);
      return;
    }

    toast(`서버에서 HWPX 조립 중… (그림 ${figures.length}개)`);
    setAiStatus("HWPX 조립 중…", "info");

    try {
      const result = await window.API.assembleHwpx(state.file, figures);
      if (!result.ok) {
        toast(`조립 실패: ${result.error || "알 수 없는 오류"}`, true);
        setAiStatus("HWPX 조립 실패", "warn");
        return;
      }
      const url = URL.createObjectURL(result.blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = result.filename || "assembled.hwpx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(`서버 조립 완료 — ${result.filename}`);
      setAiStatus(`조립 완료 · 그림 ${figures.length}개 삽입`, "ok");
    } catch (err) {
      toast(`조립 오류: ${err.message}`, true);
      setAiStatus("HWPX 조립 오류", "warn");
    }
  }

  function bindEvents() {
    $("themeBtn").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("vf.hwpx.theme", next);
    });
    document.documentElement.dataset.theme = localStorage.getItem("vf.hwpx.theme") || "light";

    ui.dropzone.addEventListener("click", () => ui.fileInput.click());
    ui.dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); ui.fileInput.click(); }
    });
    ui.fileInput.addEventListener("change", () => handleFile(ui.fileInput.files[0]));
    ["dragenter", "dragover"].forEach((name) => ui.dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      ui.dropzone.classList.add("is-drag");
    }));
    ["dragleave", "drop"].forEach((name) => ui.dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      ui.dropzone.classList.remove("is-drag");
    }));
    ui.dropzone.addEventListener("drop", (event) => handleFile(event.dataTransfer.files[0]));
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !ui.convertModal.hidden) closeConvertModal();
    });


    $("newBtn").addEventListener("click", () => {
      ui.workspace.hidden = true;
      ui.uploadView.hidden = false;
      ui.fileInput.value = "";
      state.previewPages = null;
      state.previewMode = false;
      state.hwpxDisplayHtml = null;
    });
    $("generateOneBtn").addEventListener("click", async () => {
      const slot = updateSelectedFromEditor();
      if (!slot) return;
      toast(state.aiImageAvailable ? "AI 이미지 생성 중…" : "이미지 생성 중…");
      const usedAi = await generateSlot(slot);
      renderSlotList();
      renderDocument();
      if (state.selected) selectSlot(state.selected.id);
      toast(usedAi ? "AI 이미지가 생성되었습니다." : "이미지가 다시 생성되었습니다.");
    });
    $("generateAllBtn").addEventListener("click", () => generateAllSlots(true));
    $("downloadOneBtn").addEventListener("click", async () => {
      const slot = updateSelectedFromEditor();
      if (!slot) return;
      if (!slot.image) await generateSlot(slot);
      downloadDataUrl(slot.image, `${String(slot.id + 1).padStart(2, "0")}_${sanitizeName(slot.desc)}.png`);
    });
    $("assembleHwpxBtn").addEventListener("click", async () => {
      try {
        await assembleWithBackend();
      } catch (err) {
        toast(err.message || "다운로드에 실패했습니다.", true);
      }
    });
    $("copyPromptBtn").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(ui.promptInput.value);
        toast("프롬프트를 복사했습니다.");
      } catch (_) {
        toast("브라우저가 클립보드 복사를 막았습니다.", true);
      }
    });
    [ui.descInput, ui.promptInput].forEach((input) => {
      if (!input) return;
      input.addEventListener("change", () => {
        const slot = updateSelectedFromEditor();
        if (slot) drawImage(ui.canvas, slot, false);
      });
    });
    if (ui.typeInput) {
      ui.typeInput.addEventListener("change", () => {
        const slot = state.selected;
        if (!slot) return;
        slot.uiType = ui.typeInput.value;
        slot.prompt = buildAiPrompt(slot.ai || {}, slot.context || "", slot.uiType);
        ui.promptInput.value = slot.prompt;
        drawImage(ui.canvas, slot, false);
      });
    }
  }

  bindEvents();
})();
