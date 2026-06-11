#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 생성된 raw HWPX를 한글로 열어 다시 저장(레이아웃 baking)하고, 검증용 PDF(_verify_*.pdf)를 만든다.
# 사용법: python hwpx_bake.py 파일1.hwpx 파일2.hwpx ...
import os, sys, winreg, importlib.util, win32com.client
# pyhwpx 보안 DLL 자동 탐색 (모듈을 import하지 않고 위치만 찾음 → gencache 충돌 회피)
_spec = importlib.util.find_spec("pyhwpx")
if _spec is None:
    sys.exit("[오류] pyhwpx가 없습니다. 'pip install pyhwpx' 후 다시 실행하세요.")
DLL = os.path.join(os.path.dirname(_spec.origin), "FilePathCheckerModule.dll")
for p in (r"Software\HNC\HwpAutomation\Modules", r"Software\Hnc\HwpUserAction\Modules"):
    k=winreg.CreateKey(winreg.HKEY_CURRENT_USER,p); winreg.SetValueEx(k,"FilePathCheckerModule",0,winreg.REG_SZ,DLL); winreg.CloseKey(k)
hwp=win32com.client.dynamic.Dispatch("HWPFrame.HwpObject")
hwp.RegisterModule("FilePathCheckDLL","FilePathCheckerModule"); hwp.SetMessageBoxMode(0xFFFFFF)
for f in sys.argv[1:]:
    ap=os.path.abspath(f)
    hwp.Open(ap,"HWPX","")
    hwp.SaveAs(ap,"HWPX","")  # baking: lineseg 재계산 + 행높이 자동
    pdf=os.path.join(os.path.dirname(ap), "_verify_"+os.path.splitext(os.path.basename(ap))[0]+".pdf")
    hwp.SaveAs(pdf,"PDF","")
    print("[OK]", f, "-> baked + pdf", flush=True)
    hwp.Clear(1)
hwp.Quit(); print("done")
