#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HWPX 생성 엔진 (범용).
- 기준 템플릿(seed) HWPX의 header.xml/section0.xml을 역공학한 결과를 바탕으로,
  파이썬 자료구조 → section0.xml 본문(단락/표/셀병합)을 생성하고,
  header.xml에는 필요한 borderFill(셀 배경색)·charPr(글자색)만 '추가'한 뒤,
  zipfile 모듈로 원본 순서를 유지하며 재패키징한다.

기술문서(HWPX_기술문서) 핵심 원칙 반영:
  * section0.xml만 새로 쓰고 header.xml은 '추가'만 한다(기존 항목 수정 금지).
  * 표는 hp:p>hp:run 안에 treatAsChar="1"(인라인)로 감싼다. 표 뒤 <hp:t/> 필수.
  * 모든 hp:linesegarray는 빈 태그(한글이 열 때 자동 재계산).
  * hp:tbl rowCnt/colCnt 정확히, 열너비 합 == 표너비.
  * 셀 cellAddr=실제(col,row), 병합은 cellSpan(colSpan/rowSpan) + 가려진 칸 생략.
  * 표 id/zOrder는 원본 최댓값+순번으로 고유 부여.
"""
import re, html, zipfile, os, shutil, struct as _struct

HWPU_PER_MM = 283.465
def mm(v): return int(round(v*HWPU_PER_MM))
def esc(s): return html.escape(str(s), quote=False)

def _img_pixels(path):
    """외부 라이브러리 없이 png/jpg/gif/bmp의 (가로,세로) 픽셀 반환. 실패 시 None."""
    with open(path, 'rb') as f:
        head = f.read(32)
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return _struct.unpack('>II', head[16:24])
    if head[:2] == b'\xff\xd8':  # JPEG: SOF 마커 탐색
        with open(path, 'rb') as f:
            f.read(2)
            while True:
                b = f.read(1)
                while b and b != b'\xff':
                    b = f.read(1)
                marker = f.read(1)
                if not marker:
                    break
                if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                                 0xC7, 0xC9, 0xCA, 0xCB):
                    f.read(3)
                    h, w = _struct.unpack('>HH', f.read(4))
                    return w, h
                ln = _struct.unpack('>H', f.read(2))[0]
                f.seek(ln - 2, 1)
        return None
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return _struct.unpack('<HH', head[6:10])
    if head[:2] == b'BM':
        return _struct.unpack('<ii', head[18:26])
    return None

# 시드에서 확보한 스타일 ID (header.xml)
CH_TITLE='7'; CH_H1='8'; CH_H2='9'; CH_NORMAL='10'; CH_SMALL='11'; CH_WHITE='12'; CH_CELL='13'
PA_CENTER='20'; PA_LEFT='21'
BF_DATA='3'  # 테두리 있는 흰 셀(데이터)

class HwpxDoc:
    def __init__(self, template='_seed.hwpx'):
        self.template = template
        with zipfile.ZipFile(template) as z:
            self.header = z.read('Contents/header.xml').decode('utf-8')
            self.section = z.read('Contents/section0.xml').decode('utf-8')
        # 시드 생성(CharShape 자동화)이 남긴 깨진 charPr 교정:
        # ratio/relSz=0(글자크기 0%), fontRef=0 → 정상값(100/100, font 2)
        zero='hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"'
        full='hangul="100" latin="100" hanja="100" japanese="100" other="100" symbol="100" user="100"'
        self.header = self.header.replace(f'<hh:ratio {zero}/>', f'<hh:ratio {full}/>')
        self.header = self.header.replace(f'<hh:relSz {zero}/>', f'<hh:relSz {full}/>')
        self.header = self.header.replace(f'<hh:fontRef {zero}/>',
                                          '<hh:fontRef hangul="2" latin="2" hanja="2" japanese="2" other="2" symbol="2" user="2"/>')
        self._next_bf = self._max_borderfill_id()+1
        self._next_ch = self._max_charpr_id()+1
        self._tbl_id = self._max_attr_id('id')+1000
        self._z = self._max_tbl_zorder()+200
        self._added_bf = []   # (xml,)
        self._added_ch = []
        self._color_bf = {}   # hex -> bf id
        self._color_ch = {}   # (hex,bold,height) -> ch id
        self._bindata = []    # (arcname, bytes, media_type, item_id) — 삽입 이미지
        self._sec_ctrls = []  # 섹션 컨트롤 XML(머리말/꼬리말/쪽번호)
        self._next_img = 1
        self._inst = 1000000

    # ---- header 분석 ----
    def _max_borderfill_id(self):
        return max(int(x) for x in re.findall(r'<hh:borderFill id="(\d+)"', self.header))
    def _max_charpr_id(self):
        return max(int(x) for x in re.findall(r'<hh:charPr id="(\d+)"', self.header))
    def _max_attr_id(self, attr):
        ids=[int(x) for x in re.findall(r'<hp:tbl[^>]*\bid="(\d+)"', self.section)]
        return max(ids) if ids else 0
    def _max_tbl_zorder(self):
        zs=[int(x) for x in re.findall(r'<hp:tbl[^>]*zOrder="(\d+)"', self.section)]
        return max(zs) if zs else 0

    # ---- 색 borderFill 추가 (id 반환) ----
    def fill(self, hexcolor):
        hexcolor=hexcolor.upper()
        if not hexcolor.startswith('#'): hexcolor='#'+hexcolor
        if hexcolor in self._color_bf: return self._color_bf[hexcolor]
        bid=self._next_bf; self._next_bf+=1
        xml=(f'<hh:borderFill id="{bid}" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
             '<hh:slash type="NONE" Crooked="0" isCounter="0"/><hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
             '<hh:leftBorder type="SOLID" width="0.12 mm" color="#000000"/>'
             '<hh:rightBorder type="SOLID" width="0.12 mm" color="#000000"/>'
             '<hh:topBorder type="SOLID" width="0.12 mm" color="#000000"/>'
             '<hh:bottomBorder type="SOLID" width="0.12 mm" color="#000000"/>'
             '<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>'
             f'<hc:fillBrush><hc:winBrush faceColor="{hexcolor}" hatchColor="#000000" alpha="0"/></hc:fillBrush>'
             '</hh:borderFill>')
        self._added_bf.append(xml); self._color_bf[hexcolor]=str(bid)
        return str(bid)

    # ---- 색 charPr 추가 (기존 charPr 복사 후 색/굵기만 변경) ----
    def char(self, base_id, hexcolor, bold=None):
        hexcolor=hexcolor.upper()
        if not hexcolor.startswith('#'): hexcolor='#'+hexcolor
        key=(base_id,hexcolor,bold)
        if key in self._color_ch: return self._color_ch[key]
        base=re.search(r'<hh:charPr id="'+str(base_id)+r'".*?</hh:charPr>', self.header, re.DOTALL).group()
        cid=self._next_ch; self._next_ch+=1
        new=re.sub(r'id="'+str(base_id)+r'"', f'id="{cid}"', base, count=1)
        new=re.sub(r'textColor="[^"]*"', f'textColor="{hexcolor}"', new, count=1)
        if bold is True and '<hh:bold' not in new:
            new=new.replace('</hh:charPr>','<hh:bold/></hh:charPr>')
        if bold is False:
            new=new.replace('<hh:bold/>','')
        self._added_ch.append(new); self._color_ch[key]=str(cid)
        return str(cid)

    # ---- 임의 크기 charPr 추가 (기존 복사 후 height/색/굵기 변경) ----
    def char_sz(self, base_id, height_pt, hexcolor, bold=None):
        hexcolor=hexcolor.upper()
        if not hexcolor.startswith('#'): hexcolor='#'+hexcolor
        h=int(round(height_pt*100))
        key=('SZ',base_id,h,hexcolor,bold)
        if key in self._color_ch: return self._color_ch[key]
        base=re.search(r'<hh:charPr id="'+str(base_id)+r'".*?</hh:charPr>', self.header, re.DOTALL).group()
        cid=self._next_ch; self._next_ch+=1
        new=re.sub(r'id="'+str(base_id)+r'"', f'id="{cid}"', base, count=1)
        new=re.sub(r'height="\d+"', f'height="{h}"', new, count=1)
        new=re.sub(r'textColor="[^"]*"', f'textColor="{hexcolor}"', new, count=1)
        if bold is True and '<hh:bold' not in new: new=new.replace('</hh:charPr>','<hh:bold/></hh:charPr>')
        if bold is False: new=new.replace('<hh:bold/>','')
        self._added_ch.append(new); self._color_ch[key]=str(cid)
        return str(cid)

    # ---- 페이지 설정 ----
    def page(self, landscape=False, margin_lr_mm=20, margin_tb_mm=15):
        # 한글: 용지 width/height는 항상 세로 기준(210x297). 방향은 landscape 속성만으로 결정.
        #   WIDELY=세로, NARROWLY=가로. 가로일 때 가용 가로폭은 297mm(=height) 기준.
        W=mm(210); H=mm(297)
        lr=mm(margin_lr_mm); tb=mm(margin_tb_mm); hf=mm(10)
        paper_w = H if landscape else W
        self.content_w = paper_w - 2*lr
        attr = "NARROWLY" if landscape else "WIDELY"
        pp=(f'<hp:pagePr landscape="{attr}" width="{W}" height="{H}" gutterType="LEFT_ONLY">'
            f'<hp:margin header="{hf}" footer="{hf}" gutter="0" left="{lr}" right="{lr}" top="{tb}" bottom="{tb}"/></hp:pagePr>')
        self._pagepr=pp
        return self

    # ---- 본문 빌딩 ----
    def _para(self, runs, paraPr=PA_LEFT):
        # runs: list of (text, charPr)
        rxml=''.join(f'<hp:run charPrIDRef="{cp}"><hp:t>{esc(t)}</hp:t></hp:run>' for t,cp in runs)
        if not rxml: rxml=f'<hp:run charPrIDRef="{CH_NORMAL}"><hp:t></hp:t></hp:run>'
        return (f'<hp:p id="0" paraPrIDRef="{paraPr}" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
                f'{rxml}<hp:linesegarray/></hp:p>')

    def cell(self, lines, col, row, width, height=900, bf=BF_DATA, paraPr=PA_CENTER, colspan=1, rowspan=1):
        # lines: str 또는 (text,charPr) 리스트; 여러 줄이면 hp:p 여러 개
        if isinstance(lines,str): lines=[(lines,CH_CELL)]
        elif lines and isinstance(lines[0],str): lines=[(lines[0],CH_CELL)] if len(lines)==1 else [(x,CH_CELL) for x in lines]
        inner=''.join(self._cell_para(t,cp,paraPr) for t,cp in lines) if lines else self._cell_para('',CH_CELL,paraPr)
        return (f'<hp:tc name="" header="0" hasMargin="0" protect="0" editable="0" dirty="0" borderFillIDRef="{bf}">'
                f'<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="CENTER" '
                f'linkListIDRef="0" linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
                f'{inner}</hp:subList>'
                f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
                f'<hp:cellSpan colSpan="{colspan}" rowSpan="{rowspan}"/>'
                f'<hp:cellSz width="{width}" height="{height}"/>'
                f'<hp:cellMargin left="340" right="340" top="100" bottom="100"/></hp:tc>')

    def _cell_para(self, text, charPr, paraPr):
        return (f'<hp:p id="0" paraPrIDRef="{paraPr}" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
                f'<hp:run charPrIDRef="{charPr}"><hp:t>{esc(text)}</hp:t></hp:run><hp:linesegarray/></hp:p>')

    def table(self, rows, col_widths):
        # rows: list of list[cell-xml]; col_widths 합 = 표너비
        rc=len(rows); cc=len(col_widths); tw=sum(col_widths)
        tid=self._tbl_id; self._tbl_id+=1
        z=self._z; self._z+=1
        trs=''.join('<hp:tr>'+''.join(r)+'</hp:tr>' for r in rows)
        tbl=(f'<hp:tbl id="{tid}" zOrder="{z}" numberingType="TABLE" textWrap="TOP_AND_BOTTOM" '
             f'textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" pageBreak="NONE" repeatHeader="1" '
             f'rowCnt="{rc}" colCnt="{cc}" cellSpacing="0" borderFillIDRef="{BF_DATA}" noAdjust="0">'
             f'<hp:sz width="{tw}" widthRelTo="ABSOLUTE" height="1282" heightRelTo="ABSOLUTE" protect="0"/>'
             f'<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" '
             f'vertRelTo="PARA" horzRelTo="COLUMN" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
             f'<hp:outMargin left="0" right="0" top="0" bottom="0"/>'
             f'<hp:inMargin left="340" right="340" top="100" bottom="100"/>'
             f'{trs}</hp:tbl>')
        return (f'<hp:p id="0" paraPrIDRef="{PA_LEFT}" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
                f'<hp:run charPrIDRef="0">{tbl}<hp:t/></hp:run><hp:linesegarray/></hp:p>')

    # ---- 이미지 삽입 (인라인, 글자처럼 취급) ----
    def image(self, path, width_mm=None, height_mm=None, paraPr=PA_CENTER):
        """이미지 파일을 BinData에 임베드하고 인라인 그림 문단 XML을 반환.
        크기 미지정 시 픽셀(96dpi) 기준 자동, 한쪽만 주면 비율 유지."""
        ext = os.path.splitext(path)[1].lower().lstrip('.')
        if ext == 'jpeg': ext = 'jpg'
        media = {'png':'image/png','jpg':'image/jpg','bmp':'image/bmp',
                 'gif':'image/gif','tif':'image/tiff','tiff':'image/tiff'}.get(ext, 'image/'+ext)
        with open(path, 'rb') as f:
            raw = f.read()
        item_id = 'image%d' % self._next_img; self._next_img += 1
        arc = 'BinData/%s.%s' % (item_id, ext)
        self._bindata.append((arc, raw, media, item_id))
        px = _img_pixels(path)
        if width_mm and height_mm:
            W, H = mm(width_mm), mm(height_mm)
        elif width_mm:
            W = mm(width_mm); H = int(W*(px[1]/px[0])) if px else W
        elif height_mm:
            H = mm(height_mm); W = int(H*(px[0]/px[1])) if px else H
        elif px:
            W = int(px[0]/96*25.4*HWPU_PER_MM); H = int(px[1]/96*25.4*HWPU_PER_MM)
        else:
            W, H = mm(40), mm(30)
        inst = self._inst; self._inst += 1
        z = self._z; self._z += 1
        pid = self._tbl_id; self._tbl_id += 1
        pic = (f'<hp:pic id="{pid}" zOrder="{z}" numberingType="PICTURE" textWrap="TOP_AND_BOTTOM" '
               f'textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" href="" groupLevel="0" instid="{inst}" reverse="0">'
               '<hp:offset x="0" y="0"/>'
               f'<hp:orgSz width="{W}" height="{H}"/><hp:curSz width="0" height="0"/>'
               '<hp:flip horizontal="0" vertical="0"/>'
               '<hp:rotationInfo angle="0" centerX="0" centerY="0" rotateimage="0"/>'
               '<hp:renderingInfo><hc:transMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
               '<hc:scaMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
               '<hc:rotMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/></hp:renderingInfo>'
               f'<hc:img binaryItemIDRef="{item_id}" bright="0" contrast="0" effect="REAL_PIC" alpha="0"/>'
               f'<hp:imgRect><hc:pt0 x="0" y="0"/><hc:pt1 x="{W}" y="0"/>'
               f'<hc:pt2 x="{W}" y="{H}"/><hc:pt3 x="0" y="{H}"/></hp:imgRect>'
               f'<hp:imgClip left="0" right="{W}" top="0" bottom="{H}"/>'
               '<hp:inMargin left="0" right="0" top="0" bottom="0"/>'
               f'<hp:imgDim dimwidth="{W}" dimheight="{H}"/><hp:effects/>'
               f'<hp:sz width="{W}" widthRelTo="ABSOLUTE" height="{H}" heightRelTo="ABSOLUTE" protect="0"/>'
               '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" '
               'vertRelTo="PARA" horzRelTo="PARA" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
               '<hp:outMargin left="0" right="0" top="0" bottom="0"/></hp:pic>')
        return (f'<hp:p id="0" paraPrIDRef="{paraPr}" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
                f'<hp:run charPrIDRef="{CH_NORMAL}">{pic}<hp:t/></hp:run><hp:linesegarray/></hp:p>')

    # ---- 머리말 / 꼬리말 / 쪽번호 ----
    def _hdrftr(self, kind, text, charPr, paraPr, page_number=False):
        runs = ''
        if text:
            runs += f'<hp:run charPrIDRef="{charPr}"><hp:t>{esc(text)}</hp:t></hp:run>'
        if page_number:
            runs += (f'<hp:run charPrIDRef="{charPr}"><hp:ctrl><hp:autoNum num="1" numType="PAGE">'
                     '<hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar="" supscript="0"/>'
                     '</hp:autoNum></hp:ctrl></hp:run>')
        if not runs:
            runs = f'<hp:run charPrIDRef="{charPr}"><hp:t></hp:t></hp:run>'
        sid = 1 if kind == 'header' else 20
        valign = 'TOP' if kind == 'header' else 'BOTTOM'
        return (f'<hp:{kind} id="{sid}" applyPageType="BOTH"><hp:subList id="" textDirection="HORIZONTAL" '
                f'lineWrap="BREAK" vertAlign="{valign}" linkListIDRef="0" linkListNextIDRef="0" '
                'textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
                f'<hp:p id="0" paraPrIDRef="{paraPr}" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
                f'{runs}<hp:linesegarray/></hp:p></hp:subList></hp:{kind}>')

    def set_header(self, text, charPr=CH_NORMAL, paraPr=PA_CENTER):
        # 주의: self.header(헤더 XML 문자열)와 충돌 피하려고 set_ 접두사 사용
        self._sec_ctrls.append(self._hdrftr('header', text, charPr, paraPr)); return self

    def set_footer(self, text='', charPr=CH_NORMAL, paraPr=PA_CENTER, page_number=False):
        self._sec_ctrls.append(self._hdrftr('footer', text, charPr, paraPr, page_number)); return self

    def page_number(self, pos='BOTTOM_CENTER', side_char='-'):
        """간단 쪽번호(꼬리말 없이도 표시). pos: BOTTOM_CENTER/BOTTOM_RIGHT 등."""
        self._sec_ctrls.append(f'<hp:pageNum pos="{pos}" formatType="DIGIT" sideChar="{esc(side_char)}"/>')
        return self

    # ---- 최종 저장 ----
    def save(self, out_path, title, title_charPr=CH_TITLE, body=''):
        # 헤더 갱신: borderFills/charPrs 추가
        header=self.header
        if self._added_bf:
            m=re.search(r'<hh:borderFills itemCnt="(\d+)">', header)
            cnt=int(m.group(1))+len(self._added_bf)
            header=header[:m.start()]+f'<hh:borderFills itemCnt="{cnt}">'+header[m.end():]
            header=header.replace('</hh:borderFills>', ''.join(self._added_bf)+'</hh:borderFills>',1)
        if self._added_ch:
            m=re.search(r'<hh:charProperties itemCnt="(\d+)">', header)
            cnt=int(m.group(1))+len(self._added_ch)
            header=header[:m.start()]+f'<hh:charProperties itemCnt="{cnt}">'+header[m.end():]
            header=header.replace('</hh:charProperties>', ''.join(self._added_ch)+'</hh:charProperties>',1)
        # 섹션 머리: secPr 포함 첫 문단까지 + pagePr 교체
        head=self.section[: self.section.find('</hp:ctrl></hp:run>')+len('</hp:ctrl></hp:run>')]
        head=re.sub(r'<hp:pagePr\b.*?</hp:pagePr>', self._pagepr, head, count=1, flags=re.DOTALL)
        # 섹션 컨트롤(머리말/꼬리말/쪽번호)을 secPr 문단 안에 run으로 주입
        ctrl_runs=''.join(f'<hp:run charPrIDRef="0"><hp:ctrl>{c}</hp:ctrl></hp:run>'
                          for c in self._sec_ctrls)
        # 첫 문단을 제목으로 채우고 닫기 (paraPrIDRef=20 가운데정렬 유지)
        title_run=f'<hp:run charPrIDRef="{title_charPr}"><hp:t>{esc(title)}</hp:t></hp:run><hp:linesegarray/></hp:p>'
        section=head+ctrl_runs+title_run+body+'</hs:sec>'
        # content.hpf 매니페스트에 임베드 이미지 항목 추가
        def patch_hpf(hpf):
            if not self._bindata: return hpf
            items=''.join(f'<opf:item id="{iid}" href="{arc}" media-type="{mt}" isEmbeded="1"/>'
                          for arc,_raw,mt,iid in self._bindata)
            return hpf.replace('</opf:manifest>', items+'</opf:manifest>', 1)
        # 재패키징 (원본 infolist 순서 유지 + BinData 추가)
        with zipfile.ZipFile(self.template) as zin, zipfile.ZipFile(out_path,'w',zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename=='Contents/header.xml':
                    zout.writestr(item, header.encode('utf-8'))
                elif item.filename=='Contents/section0.xml':
                    zout.writestr(item, section.encode('utf-8'))
                elif item.filename=='Contents/content.hpf':
                    zout.writestr(item, patch_hpf(zin.read(item.filename).decode('utf-8')).encode('utf-8'))
                elif item.filename=='mimetype':
                    # mimetype은 무압축이어야 안전
                    zout.writestr(item, zin.read(item.filename))
                else:
                    zout.writestr(item, zin.read(item.filename))
            # 임베드 이미지 파일 추가
            for arc,raw,_mt,_iid in self._bindata:
                zout.writestr(arc, raw)
        return out_path
