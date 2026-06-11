# Visual Flow + Figure Planner v2.1 (Gemini) — process_6

`process_5`의 Visual Flow 웹앱(클라이언트 사이드 HWPX 분석 + canvas 미리보기)에
`figure_planner_v2.1_spec.md`의 **AI 그림 자동 배치 파이프라인**을 보강하고,
**Google Gemini** API로 ① 그림 위치/유형 분석과 ② 실제 이미지 생성을 연결한 자체포함
웹앱입니다. 기존 UX/UI는 그대로 두고, AI 기능은 **보강(augment)** 방식으로 더해집니다.

## 동작 방식 (요약)

- HWPX 업로드 → 기존 휴리스틱으로 **즉시** 이미지 필요 지점(slot)·미리보기 표시(UX 불변).
- 백엔드가 살아 있으면, 비동기로 Figure Planner 파이프라인(10단계, Self-Consistency +
  Critic)을 돌려 결과(`figure_type` 11종, `purpose`, `anchor_text`, `confidence`,
  `suggested_content`, `image_prompt_seed`, `trigger_scores`)로 slot을 **보강**하고,
  새 위치를 추가합니다. 분석 상태는 좌측 점검 패널에 한 줄로 표시됩니다.
- "다시 생성 / 모두 생성 / PNG 저장" 버튼은 **실제 Gemini 이미지**를 생성합니다.
- **"섹션 JSON" 버튼**: `hwpx-rekian-master/hwpx` 스킬 방식으로 업로드된 HWPX의
  `Contents/section*.xml`을 파싱해 **텍스트·표를 구조화한 JSON**을 모달로 보여주고
  내려받을 수 있습니다(백엔드 `/api/v1/documents/sections`). 백엔드 미연결 시에는
  클라이언트 파싱 결과로 동일 구조의 JSON을 구성합니다.
- 백엔드 미연결·키 오류·LLM 호출 실패 시에는 **기존 휴리스틱 + canvas로 자연 폴백**하며
  상태 줄에 사유가 표시됩니다. Gemini 과부하(503)는 자동 재시도합니다
  (`GEMINI_MAX_RETRIES`, 기본 4회 지수 백오프).

## 폴더 구조

```
process_6/
├── index.html / app.js / api.js / styles.css   # 프론트엔드 (Visual Flow + AI 보강)
├── hwpx_skill                                   # HWPX 스킬 문서
├── key.env                                      # API 키(여기에 Gemini 키 입력) ※ git 제외
├── key.env.enc                                  # DPAPI 암호화본(아래 절차로 생성)
├── start_server.bat                             # 설치+암호화+실행 일괄 스크립트
└── backend/                                     # figure_planner 백엔드(FastAPI)
    ├── pyproject.toml
    └── src/figure_planner/...
```

## 설치 & 실행

사전 요구: Windows, Python 3.10+, [Google AI Studio](https://aistudio.google.com/apikey) Gemini API 키.

**선택 설치 — HWP/HWPX 원본 페이지 미리보기 기능 사용 시:**
- [LibreOffice](https://www.libreoffice.org/download/libreoffice-fresh/) 설치 (HWP→PDF 변환에 사용)
- 한컴오피스 2018 이상 (한글 COM API 기반 고품질 변환, 라이선스 별도 필요)

1. **키 파일 준비**: `key.env.example`을 복사해 `key.env`로 이름을 바꾸고, `GEMINI_API_KEY=` 값에 실제 키를 입력합니다.
   (`key.env`는 `.gitignore` 됩니다. 절대 커밋하지 마세요.)
   ```powershell
   copy key.env.example key.env
   # 이후 key.env를 텍스트 편집기로 열어 키 입력
   ```
2. **일괄 실행**: `start_server.bat` 더블클릭. (의존성 설치 → `key.env` 암호화 → 서버 실행)

   또는 수동으로:
   ```powershell
   cd backend
   pip install -e .
   python -m figure_planner.encrypt_key_env   # key.env → ..\key.env.enc (DPAPI)
   python -m figure_planner.run_server         # http://localhost:8000
   ```
3. 브라우저에서 **http://localhost:8000** 접속 →
   업로드 시 AI 보강이 동작합니다.

> `index.html`을 파일로 직접 열어도(서버 없이) 기존 클라이언트 분석은 그대로 동작하며,
> 점검 패널에 "AI 분석 미사용(백엔드 미연결)"이 표시됩니다.

### 키가 잘 로드됐는지 확인
`http://localhost:8000/api/v1/health` 접속 → `llm_provider:"gemini"`,
`gemini_key_loaded:true`, `image_gen_available:true`, `encrypted_env_exists:true`,
`secret_error:false` 이면 정상입니다.

## 환경 변수

`key.env`(암호화되면 `key.env.enc`)에서 읽습니다. 주요 값:

| 변수 | 기본값 | 설명 |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` / `openai` / `anthropic` |
| `GEMINI_API_KEY` | — | Google AI Studio 키 |
| `GEMINI_MODEL` | `gemini-2.5-flash` | 텍스트/분석 모델 |
| `GEMINI_VISION_MODEL` | `gemini-2.5-flash` | 페이지 이미지(비전) 분석 모델 |
| `GEMINI_IMAGE_MODEL` | `gemini-2.5-flash-image` | 이미지 생성 모델 |
| `SELF_CONSISTENCY_N` | `3` | LLM 다수결 호출 횟수 |
| `ENABLE_CRITIC` | `true` | Critic Pass 활성화 |
| `MAX_FIGURES` | `8` | 최대 그림 수 |
| `ENABLE_HYBRID` | `true` | 페이지 이미지+텍스트 하이브리드 |

> 키 값은 강제로 덮어쓰지 않습니다. 다른 provider를 쓰려면 `key.env`의 `LLM_PROVIDER`와
> 해당 키만 바꾸면 됩니다.

## HWPX 렌더링 (원본 보기 / 페이지 이미지)

LibreOffice 26.2에는 **HWPX(OWPML) import 필터가 없습니다**(레거시 바이너리 `.hwp`만 지원).
그래서 웹앱은 HWPX를 **본문 텍스트·표로 추출 → HTML 재구성 → LibreOffice로 HTML→PDF →
PyMuPDF로 페이지 PNG** 경로로 렌더링합니다(결과 `source: hwpx->html->libreoffice-pdf`).

- LibreOffice 실행 파일은 `../process_7`(포터블, 관리자 설치 불필요)에 동봉되어 **자동 탐지**됩니다.
  필요하면 환경변수 `LIBREOFFICE_PATH`로 직접 지정할 수 있습니다. 자세한 내용은
  [`../process_7/README.md`](../process_7/README.md) 참고.
- 한컴오피스 COM·Hancom DocsConverter·argodocument API가 설정돼 있으면 그쪽(픽셀 동일 렌더)이
  우선 사용되고, 모두 없을 때 위 HTML 경로가 동작합니다. 전부 실패하면 텍스트 기반 분석으로 폴백합니다.

## 보안

- API 키는 **서버에서만** `key.env.enc`(Windows DPAPI)로 복호화되어 메모리에 로드됩니다.
  프론트엔드는 키를 보관하지 않습니다.
- DPAPI 암호화는 같은 Windows 사용자/PC에서만 복호화됩니다. 다른 PC로 옮기면 `key.env`를
  다시 입력하고 재암호화해야 합니다.
