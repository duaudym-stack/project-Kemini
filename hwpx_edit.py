#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HWPX 편집 엔진 — 기존 .hwpx를 열어 부분 수정 후 재패키징.

생성(hwpxgen)·읽기(hwpx_read)에 이어 '편집' 방향. 제안서 양식처럼 골격은
그대로 두고 내용만 채워넣는 작업에 쓴다. 한컴 없이 순수 파이썬으로 동작하지만,
저장 후에는 hwpx_bake.py로 baking(레이아웃 재계산)하는 것을 권장한다.

핵심 함정: hwpx 본문의 한 단락 텍스트는 여러 <hp:t> run으로 쪼개져 있다.
플레이스홀더가 run 경계를 넘나들면 단순 치환이 실패하므로, '단락 단위로
run 텍스트를 이어붙여 치환'하는 2-pass 전략을 쓴다.
  1) run 하나 안에 전부 들어있으면 그 run만 치환(글자모양 보존).
  2) run 경계에 걸치면 단락의 첫 run에 합쳐 넣고 나머지 run은 비움.

주요 API
  ed = HwpxEditor("양식.hwpx")
  ed.replace("{{과제명}}", "생성형 AI 인재양성")     # 단일 치환
  ed.replace_map({"{{이름}}":"홍길동", "{{금액}}":"5,000,000"})
  n = ed.count("{{과제명}}")                          # 남은 개수 확인
  ed.append_table_row(table_index=0, cells=["항목","값",...])  # 표 끝에 행 추가
  ed.save("작성본.hwpx")

CLI
  python hwpx_edit.py 양식.hwpx -o 결과.hwpx --set "{{이름}}=홍길동" --set "{{금액}}=500"
  python hwpx_edit.py 양식.hwpx --map 치환표.json -o 결과.hwpx
"""
import sys, os, re, json, zipfile, io
from lxml import etree

_HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'

class HwpxEditor:
    def __init__(self, path):
        self.path = path
        self._order = []            # zip 내 파일 순서 보존
        self._raw = {}              # name -> bytes (수정 안 하는 파일)
        self._sections = {}         # name -> lxml root (수정 대상)
        with zipfile.ZipFile(path) as z:
            for item in z.infolist():
                data = z.read(item.filename)
                self._order.append(item.filename)
                if re.match(r'Contents/section\d+\.xml$', item.filename):
                    self._sections[item.filename] = etree.fromstring(data)
                else:
                    self._raw[item.filename] = data
        self._sec_names = sorted(self._sections,
                                 key=lambda n: int(re.search(r'(\d+)', n).group(1)))

    # ── 조회 ──
    def _iter_paras(self):
        for name in self._sec_names:
            for p in self._sections[name].iter(_HP + 'p'):
                yield p

    @staticmethod
    def _own_ts(p):
        """p에 '직접' 속한 hp:t만 반환. 중첩 표 셀 안쪽 run은 제외
        (그렇지 않으면 표를 감싼 단락이 모든 셀 텍스트까지 들고 와 중복/붕괴 발생)."""
        return [t for t in p.iter(_HP + 't')
                if next(t.iterancestors(_HP + 'p'), None) is p]

    def count(self, needle):
        c = 0
        for p in self._iter_paras():
            joined = ''.join((t.text or '') for t in self._own_ts(p))
            c += joined.count(needle)
        return c

    def text(self):
        """현재 상태의 평문(검증용)."""
        out = []
        for p in self._iter_paras():
            out.append(''.join((t.text or '') for t in self._own_ts(p)))
        return '\n'.join(x for x in out if x.strip())

    # ── 치환 ──
    def replace(self, old, new):
        """문서 전체에서 old→new. 치환 횟수 반환."""
        return self._replace_many({old: new})

    def replace_map(self, mapping):
        return self._replace_many(dict(mapping))

    def _replace_many(self, mapping):
        total = 0
        for p in self._iter_paras():
            ts = self._own_ts(p)   # 직접 run만 (셀 내부는 해당 셀 단락에서 별도 처리)
            if not ts:
                continue
            # pass 1: run 단위(글자모양 보존)
            for t in ts:
                if not t.text:
                    continue
                for old, new in mapping.items():
                    if old and old in t.text:
                        cnt = t.text.count(old)
                        if cnt:
                            t.text = t.text.replace(old, new)
                            total += cnt
            # pass 2: run 경계에 걸친 경우 단락 합치기
            joined = ''.join((t.text or '') for t in ts)
            need = {o: n for o, n in mapping.items() if o and o in joined}
            if need:
                for old, new in need.items():
                    cnt = joined.count(old)
                    if cnt:
                        joined = joined.replace(old, new)
                        total += cnt
                ts[0].text = joined
                for t in ts[1:]:
                    t.text = ''
        return total

    # ── 셀 내용 교체 ──
    def _tables(self):
        tbls = []
        for name in self._sec_names:
            tbls.extend(self._sections[name].iter(_HP + 'tbl'))
        return tbls

    def set_cell(self, table_index, row, col, text):
        """table_index번째 표에서 cellAddr=(col,row)인 셀의 텍스트를 교체.
        lxml 트리 조작이라 정규식 오프셋 밀림 문제가 없다(v5 사례⑤ 대안)."""
        tbls = self._tables()
        if table_index >= len(tbls):
            raise IndexError('표 인덱스 범위 초과: %d (총 %d개)' % (table_index, len(tbls)))
        tbl = tbls[table_index]
        target = None
        for tr in tbl.findall(_HP + 'tr'):           # 직접 행/셀만(중첩표 제외)
            for tc in tr.findall(_HP + 'tc'):
                ca = tc.find(_HP + 'cellAddr')
                if ca is not None and ca.get('colAddr') == str(col) \
                        and ca.get('rowAddr') == str(row):
                    target = tc; break
            if target is not None:
                break
        if target is None:
            raise ValueError('셀을 찾지 못함: 표 %d (col=%d, row=%d)' % (table_index, col, row))
        sub = target.find(_HP + 'subList')
        ps = sub.findall(_HP + 'p') if sub is not None else []
        if not ps:
            raise ValueError('셀에 단락이 없습니다')
        # 첫 단락 첫 run에 text, 나머지 run/단락은 비움(셀을 단일 값으로)
        first_ts = self._own_ts(ps[0])
        if first_ts:
            first_ts[0].text = text
            for extra in first_ts[1:]:
                extra.text = ''
        for p in ps[1:]:
            for t in self._own_ts(p):
                t.text = ''
        return True

    # ── 표 행 추가 ──
    def append_table_row(self, table_index, cells):
        """table_index번째 표의 마지막 행을 복제해 cells 텍스트로 채워 추가."""
        tbls = self._tables()
        if table_index >= len(tbls):
            raise IndexError('표 인덱스 범위 초과: %d (총 %d개)' % (table_index, len(tbls)))
        import copy
        tbl = tbls[table_index]
        trs = tbl.findall(_HP + 'tr')
        if not trs:
            raise ValueError('표에 행이 없습니다')
        new_tr = copy.deepcopy(trs[-1])
        tcs = new_tr.findall(_HP + 'tc')
        for i, tc in enumerate(tcs):
            val = cells[i] if i < len(cells) else ''
            t_nodes = list(tc.iter(_HP + 't'))
            if t_nodes:
                t_nodes[0].text = val
                for extra in t_nodes[1:]:
                    extra.text = ''
            # cellAddr rowAddr 증가
            addr = tc.find(_HP + 'cellAddr')
            if addr is not None and addr.get('rowAddr') is not None:
                addr.set('rowAddr', str(int(addr.get('rowAddr')) + 1))
        tbl.append(new_tr)
        # rowCnt 갱신
        if tbl.get('rowCnt') is not None:
            tbl.set('rowCnt', str(int(tbl.get('rowCnt')) + 1))
        return True

    # ── 저장 ──
    def save(self, out_path):
        rendered = {name: etree.tostring(root, xml_declaration=True,
                                         encoding='UTF-8', standalone=True)
                    for name, root in self._sections.items()}
        with zipfile.ZipFile(self.path) as zin, \
             zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                name = item.filename
                if name in rendered:
                    zout.writestr(item, rendered[name])
                else:
                    zout.writestr(item, zin.read(name))
        return out_path


# ── CLI ──
def _main(argv):
    if not argv or argv[0].startswith('-'):
        print(__doc__); return 1
    src = argv[0]
    out = None
    mapping = {}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == '-o':
            out = argv[i + 1]; i += 2
        elif a == '--set':
            k, _, v = argv[i + 1].partition('=')
            mapping[k] = v; i += 2
        elif a == '--map':
            with io.open(argv[i + 1], encoding='utf-8') as fp:
                mapping.update(json.load(fp))
            i += 2
        else:
            i += 1
    if not out:
        out = os.path.splitext(src)[0] + '_edited.hwpx'
    ed = HwpxEditor(src)
    n = ed.replace_map(mapping) if mapping else 0
    ed.save(out)
    print('[OK] %d건 치환 → %s  (baking: python hwpx_bake.py "%s")' % (n, out, out))
    return 0

if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
