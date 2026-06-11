"""
hwpx_assembler.py — 원본 HWPX 서식을 보존하며 AI 생성 이미지를 삽입.

레퍼런스 보존형 재조립 워크플로우
  1. 원본 HWPX 언팩 (ZIP → 메모리)
  2. 앵커 텍스트로 삽입 위치 탐색 (Contents/section0.xml)
  3. hp:pic 이미지 문단 삽입
  4. Contents/content.hpf 매니페스트에 이미지 항목 추가
  5. BinData/ 디렉터리에 이미지 바이너리 추가
  6. linesegarray 제거 (한글이 줄배열 재계산하도록)
  7. 재팩킹 (mimetype 첫 번째, ZIP_STORED)

stdlib 전용 — lxml 등 추가 의존성 없음.
"""

from __future__ import annotations

import base64
import io
import struct
from dataclasses import dataclass, field
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
import xml.etree.ElementTree as ET

# ── 네임스페이스 접두사 등록 ──────────────────────────────────────────────────
# ET.register_namespace()는 전역 레지스트리 → 직렬화 시 원본 접두사 사용
_NS_MAP = {
    "hp":  "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hs":  "http://www.hancom.co.kr/hwpml/2011/section",
    "hc":  "http://www.hancom.co.kr/hwpml/2011/core",
    "hh":  "http://www.hancom.co.kr/hwpml/2011/head",
    "ha":  "http://www.hancom.co.kr/hwpml/2011/app",
    "hhs": "http://www.hancom.co.kr/hwpml/2011/history",
    "hm":  "http://www.hancom.co.kr/hwpml/2011/master-page",
    "hpf": "http://www.hancom.co.kr/schema/2011/hpf",
    "dc":  "http://purl.org/dc/elements/1.1/",
    "opf": "http://www.idpf.org/2007/opf/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
for _pfx, _uri in _NS_MAP.items():
    ET.register_namespace(_pfx, _uri)

# Clark 표기 상수
_HP  = "http://www.hancom.co.kr/hwpml/2011/paragraph"
_HC  = "http://www.hancom.co.kr/hwpml/2011/core"
_HS  = "http://www.hancom.co.kr/hwpml/2011/section"
_OPF = "http://www.idpf.org/2007/opf/"

_P_TAG   = f"{{{_HP}}}p"
_T_TAG   = f"{{{_HP}}}t"
_PIC_TAG = f"{{{_HP}}}pic"
_LSA_TAG = f"{{{_HP}}}linesegarray"
_SEC_TAG = f"{{{_HS}}}sec"

# A4 본문폭 HWPUNIT (210mm − 좌우 30mm 여백 각각 = 150mm)
# 1mm = 2834.6 HWPUNIT → 150mm ≈ 42520 HU
_TEXT_W_HU: int = 42520
_PX_TO_HU: int = 75  # 96 dpi 기준: 1inch/7200HU / 96px = 75 HU/px


# ── 공개 데이터 클래스 ────────────────────────────────────────────────────────

@dataclass
class FigureInsertion:
    """그림 삽입 요청 하나."""
    anchor_text: str
    position_hint: str = "after"       # "before" | "after"
    image_bytes: bytes = field(default_factory=bytes)
    image_mime: str = "image/png"
    image_base64: str = ""             # data URL 또는 순수 base64

    def resolved_bytes(self) -> bytes:
        if self.image_bytes:
            return self.image_bytes
        if self.image_base64:
            b64 = self.image_base64
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            return base64.b64decode(b64)
        return b""


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _ext(mime: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
            "image/gif": "gif", "image/bmp": "bmp", "image/webp": "webp"
            }.get(mime.lower().strip(), "png")


def _mime(ext: str) -> str:
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp"
            }.get(ext.lower(), "image/png")


def _png_wh(data: bytes) -> tuple[int, int]:
    """PNG IHDR에서 픽셀 크기 읽기. 실패 시 (0, 0)."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return 0, 0


def _dims(img: bytes, mime: str, target_w: int = _TEXT_W_HU) -> tuple[int, int, int, int]:
    """(org_w, org_h, disp_w, disp_h) — 단위: HWPUNIT."""
    w_px, h_px = _png_wh(img) if "png" in mime else (0, 0)
    if w_px > 0 and h_px > 0:
        org_w, org_h = w_px * _PX_TO_HU, h_px * _PX_TO_HU
    else:
        org_w, org_h = 1920 * _PX_TO_HU, 1080 * _PX_TO_HU  # 기본 16:9
    disp_w = target_w
    disp_h = int(disp_w * org_h / org_w)
    return org_w, org_h, disp_w, disp_h


def _max_id(root: ET.Element) -> int:
    best = 10_000_000
    for el in root.iter():
        try:
            v = int(el.get("id", "0"))
            if v > best:
                best = v
        except (ValueError, TypeError):
            pass
    return best


def _para_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(_T_TAG))


def _sec_and_paras(root: ET.Element) -> tuple[ET.Element, list[ET.Element]]:
    """(섹션 부모 요소, 최상위 hp:p 목록)."""
    for sec in root.iter(_SEC_TAG):
        paras = [c for c in sec if c.tag == _P_TAG]
        if paras:
            return sec, paras
    # 섹션 래퍼 없이 루트에 직접 있는 경우
    return root, [c for c in root if c.tag == _P_TAG]


def _build_pic_para(
    para_id: int, pic_id: int, instid: int,
    bin_ref: str,
    org_w: int, org_h: int,
    disp_w: int, disp_h: int,
) -> ET.Element:
    """hp:pic 을 담은 hp:p 요소 생성 (실제 HWPX 파일 구조 기준)."""

    def hp(tag: str) -> str:
        return f"{{{_HP}}}{tag}"

    def hc(tag: str) -> str:
        return f"{{{_HC}}}{tag}"

    p = ET.Element(hp("p"), {
        "id": str(para_id), "paraPrIDRef": "0", "styleIDRef": "0",
        "pageBreak": "0", "columnBreak": "0", "merged": "0",
    })
    run = ET.SubElement(p, hp("run"), {"charPrIDRef": "0"})
    pic = ET.SubElement(run, hp("pic"), {
        "id": str(pic_id), "zOrder": "0",
        "numberingType": "PICTURE",
        "textWrap": "TOP_AND_BOTTOM", "textFlow": "BOTH_SIDES",
        "lock": "0", "dropcapstyle": "None",
        "href": "", "groupLevel": "0",
        "instid": str(instid), "reverse": "0",
    })

    ET.SubElement(pic, hp("offset"), {"x": "0", "y": "0"})
    ET.SubElement(pic, hp("orgSz"),  {"width": str(org_w),  "height": str(org_h)})
    ET.SubElement(pic, hp("curSz"),  {"width": str(disp_w), "height": str(disp_h)})
    ET.SubElement(pic, hp("flip"),   {"horizontal": "0", "vertical": "0"})
    ET.SubElement(pic, hp("rotationInfo"), {
        "angle": "0",
        "centerX": str(disp_w // 2),
        "centerY": str(disp_h // 2),
        "rotateimage": "1",
    })

    ri = ET.SubElement(pic, hp("renderingInfo"))
    sx = f"{disp_w / org_w:.6f}"
    sy = f"{disp_h / org_h:.6f}"
    ET.SubElement(ri, hc("transMatrix"), {"e1":"1","e2":"0","e3":"0","e4":"0","e5":"1","e6":"0"})
    ET.SubElement(ri, hc("scaMatrix"),   {"e1":sx, "e2":"0","e3":"0","e4":"0","e5":sy, "e6":"0"})
    ET.SubElement(ri, hc("rotMatrix"),   {"e1":"1","e2":"0","e3":"0","e4":"0","e5":"1","e6":"0"})

    ET.SubElement(pic, hc("img"), {
        "binaryItemIDRef": bin_ref,
        "bright": "0", "contrast": "0",
        "effect": "REAL_PIC", "alpha": "0",
    })

    rect = ET.SubElement(pic, hp("imgRect"))
    ET.SubElement(rect, hc("pt0"), {"x": "0",        "y": "0"})
    ET.SubElement(rect, hc("pt1"), {"x": str(org_w), "y": "0"})
    ET.SubElement(rect, hc("pt2"), {"x": str(org_w), "y": str(org_h)})
    ET.SubElement(rect, hc("pt3"), {"x": "0",        "y": str(org_h)})

    ET.SubElement(pic, hp("imgClip"),
                  {"left":"0","right":str(org_w),"top":"0","bottom":str(org_h)})
    ET.SubElement(pic, hp("inMargin"),  {"left":"0","right":"0","top":"0","bottom":"0"})
    ET.SubElement(pic, hp("imgDim"),    {"dimwidth":str(org_w),"dimheight":str(org_h)})
    ET.SubElement(pic, hp("effects"))
    ET.SubElement(pic, hp("sz"), {
        "width": str(disp_w), "widthRelTo": "ABSOLUTE",
        "height": str(disp_h), "heightRelTo": "ABSOLUTE",
        "protect": "0",
    })
    ET.SubElement(pic, hp("pos"), {
        "treatAsChar": "1", "affectLSpacing": "0",
        "flowWithText": "1", "allowOverlap": "0",
        "holdAnchorAndSO": "0",
        "vertRelTo": "PARA", "horzRelTo": "COLUMN",
        "vertAlign": "TOP", "horzAlign": "LEFT",
        "vertOffset": "0", "horzOffset": "0",
    })
    ET.SubElement(pic, hp("outMargin"), {"left":"0","right":"0","top":"0","bottom":"0"})

    # 빈 trailing run — Hancom 관례
    ET.SubElement(
        ET.SubElement(p, hp("run"), {"charPrIDRef": "0"}),
        hp("t"),
    )
    return p


def _remove_lsa(root: ET.Element) -> None:
    """linesegarray 전체 제거 — 한글이 줄배열을 재계산하도록."""
    for parent in root.iter():
        for c in [ch for ch in parent if ch.tag == _LSA_TAG]:
            parent.remove(c)


def _to_xml(root: ET.Element) -> bytes:
    body = ET.tostring(root, encoding="unicode")
    return f"<?xml version='1.0' encoding='UTF-8'?>\n{body}".encode("utf-8")


def _pack(files: dict[str, bytes]) -> bytes:
    """dict → HWPX bytes. mimetype 항목은 반드시 첫 번째, ZIP_STORED."""
    buf = io.BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        if "mimetype" in files:
            zf.writestr("mimetype", files["mimetype"], compress_type=ZIP_STORED)
        for name, data in files.items():
            if name != "mimetype":
                zf.writestr(name, data)
    return buf.getvalue()


# ── 공개 API ─────────────────────────────────────────────────────────────────

def assemble_hwpx(
    original_bytes: bytes,
    figures: list[FigureInsertion],
) -> bytes:
    """
    원본 HWPX bytes + 삽입할 그림 목록 → 새 HWPX bytes 반환.

    원본의 header.xml(스타일 정의 전체)은 그대로 보존하고,
    section0.xml에만 hp:pic 문단을 추가한다.
    앵커 텍스트를 찾지 못하면 문서 끝에 추가한다.
    """
    if not figures:
        return original_bytes

    try:
        zin = ZipFile(io.BytesIO(original_bytes), "r")
    except Exception as exc:
        raise ValueError(f"유효하지 않은 HWPX ZIP: {exc}") from exc

    files: dict[str, bytes] = {}
    with zin:
        for name in zin.namelist():
            files[name] = zin.read(name)

    # section0.xml 위치 탐색 (대소문자 무관)
    sec_key = next(
        (k for k in files if k.lower() == "contents/section0.xml"),
        "Contents/section0.xml",
    )
    if sec_key not in files:
        raise ValueError("Contents/section0.xml 없음")

    # content.hpf 위치 탐색
    hpf_key = next(
        (k for k in files if k.lower() == "contents/content.hpf"),
        "Contents/content.hpf",
    )
    if hpf_key not in files:
        raise ValueError("Contents/content.hpf 없음")

    sec_root = ET.fromstring(files[sec_key].decode("utf-8"))
    hpf_root = ET.fromstring(files[hpf_key].decode("utf-8"))

    # manifest 요소
    manifest_el = hpf_root.find(f"{{{_OPF}}}manifest")
    if manifest_el is None:
        manifest_el = ET.SubElement(hpf_root, f"{{{_OPF}}}manifest")

    # 기존 imageN 번호 수집 → 다음 번호 결정
    existing_nums: set[int] = set()
    for item in manifest_el:
        iid = item.get("id", "")
        if iid.startswith("image"):
            try:
                existing_nums.add(int(iid[5:]))
            except ValueError:
                pass
    next_num = max(existing_nums, default=0) + 1

    id_cur = _max_id(sec_root) + 1000
    sec_parent, top_paras = _sec_and_paras(sec_root)

    for fig in figures:
        img_data = fig.resolved_bytes()
        if not img_data:
            continue

        # 앵커 텍스트 탐색
        anchor = fig.anchor_text.strip()
        anchor_idx: int | None = None
        for i, para in enumerate(top_paras):
            if anchor and anchor in _para_text(para):
                anchor_idx = i
                break

        # 삽입 위치 결정 (sec_parent 기준 child index)
        all_children = list(sec_parent)
        if anchor_idx is None:
            child_idx = len(all_children)
        elif fig.position_hint == "before":
            child_idx = all_children.index(top_paras[anchor_idx])
        else:
            child_idx = all_children.index(top_paras[anchor_idx]) + 1

        org_w, org_h, disp_w, disp_h = _dims(img_data, fig.image_mime)

        para_id = id_cur; id_cur += 1
        pic_id  = id_cur; id_cur += 1
        instid  = id_cur; id_cur += 1
        img_num = next_num; next_num += 1

        ext_str  = _ext(fig.image_mime)
        bin_ref  = f"image{img_num}"
        bin_path = f"BinData/{bin_ref}.{ext_str}"

        pic_p = _build_pic_para(
            para_id, pic_id, instid, bin_ref,
            org_w, org_h, disp_w, disp_h,
        )
        sec_parent.insert(child_idx, pic_p)
        top_paras = [c for c in sec_parent if c.tag == _P_TAG]

        files[bin_path] = img_data

        # manifest에 이미지 항목 추가 (section0 item 앞에 삽입)
        new_item = ET.Element(f"{{{_OPF}}}item", {
            "id": bin_ref,
            "href": bin_path,
            "media-type": _mime(ext_str),
            "isEmbeded": "1",
        })
        sec_items = [c for c in manifest_el if "section" in c.get("id", "")]
        if sec_items:
            manifest_el.insert(list(manifest_el).index(sec_items[0]), new_item)
        else:
            manifest_el.append(new_item)

    _remove_lsa(sec_root)

    files[sec_key] = _to_xml(sec_root)
    files[hpf_key] = _to_xml(hpf_root)

    return _pack(files)
