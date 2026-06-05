# KMU 캠퍼스 라이프 에이전트

국민대학교 AI빅데이터융합경영학과 학생을 기준으로 만든 맞춤형 캠퍼스·커리어 추천 웹앱입니다.  
공지, 공모전, 대외활동, 장학금, 채용·인턴, 자격증, 학사일정을 매일 수집해 사용자의 포트폴리오, 시간표, 실제 일정, 수강내역 기반 졸업 상태를 바탕으로 추천합니다.

현재 운영 구조는 **Supabase 원격 DB + Streamlit 웹 + GitHub Actions 자동화 + Notion 캘린더 + Telegram 승인**입니다.

## 현재 상태

- 로컬 웹: `streamlit run app.py` → <http://localhost:8501>
- 저장소: 기본 SQLite 지원, 운영은 `STORE_BACKEND=supabase`
- 원격 DB: Supabase `settings`, `profile`, `preferences`, `kv`, `recommendations`
- 서버 자동화: GitHub Actions가 매일 08:00 KST 추천 갱신
- 텔레그램 승인: GitHub Actions가 5분마다 승인 버튼 확인
- Notion 사용 범위: 실제 일정 캘린더, 포트폴리오 페이지
- 시간표: Notion에 넣지 않고 웹 설정 페이지에서 PDF/표로 저장
- 졸업 진단: ON국민 수강내역 엑셀을 교과목코드로 매칭해 졸업요건을 진단하고 이수 완료 과목을 추천 프로필에 반영

## 빠른 실행

```powershell
pip install -r requirements.txt
copy .env.example .env
streamlit run app.py
```

웹을 열면 다음 순서로 설정합니다.

1. `설정`에서 OpenAI, Notion, Telegram 키 입력
2. 시간표 PDF 업로드 또는 표 직접 입력
3. 분야별 자동수신 주기 선택
4. `내 맞춤 추천함`에서 새로고침으로 추천 분석
5. 필요한 추천은 `노션에 추가`, 필요 없는 추천은 `무시`
6. `졸업 진단`에서 ON국민 수강내역 엑셀 업로드 후 결과 확인

## 운영 자동화

GitHub Actions는 로컬 PC가 꺼져 있어도 실행됩니다.

| 자동화 | 파일 | 주기 | 역할 |
|---|---|---:|---|
| Daily KMU Digest | `.github/workflows/daily-digest.yml` | 매일 08:00 KST | 정보 수집, 추천 분석, Supabase 업데이트, 텔레그램 발송 |
| Telegram Callback Processor | `.github/workflows/telegram-callbacks.yml` | 5분마다 | 텔레그램 승인/무시 버튼 확인, Notion 캘린더 반영 |

필수 GitHub Secrets:

- `OPENAI_API_KEY`
- `NOTION_API_KEY`
- `NOTION_CALENDAR_DB_ID`
- `NOTION_PORTFOLIO_PAGE_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

서버에서 매일 분석할 분야와 텔레그램으로 보낼 분야는 기본적으로 웹 `설정` 화면에 저장된 Supabase 설정을 따릅니다.
GitHub Variables는 서버에서 임시로 강제하고 싶을 때만 사용합니다.

- `DIGEST_CATEGORIES`: 값이 있으면 웹 설정 대신 이 분야만 자동 분석
- `TELEGRAM_CATEGORIES`: 값이 있으면 해당 분야를 텔레그램 채널로 강제

## 데이터 흐름

```text
웹 공지/링커리어/국민대 공지
→ scraper.py 수집
→ classifier.py 분야 분류
→ analyzer.py LLM 분석/요약/점수화
→ validator.py 시간표·실제일정·학사일정 기반 가용시간 검증
→ store.py Supabase recommendations 저장
→ Streamlit 추천함 표시
→ Telegram 1순위 발송
→ 승인 시 executor.py가 Notion 캘린더에 등록
```

## 주요 화면

| 화면 | 파일 | 사용자가 하는 일 |
|---|---|---|
| 홈 | `app.py` | 프로필 요약, 추천함/졸업 진단 진입 |
| 설정 | `pages/0_설정.py` | API 키, 시간표, 분야별 자동수신 설정 |
| 내 맞춤 추천함 | `pages/1_내_맞춤_추천함.py` | 추천 누적 확인, 새로고침, 노션 추가, 무시 |
| 졸업 진단 | `pages/2_졸업_진단.py` | 수강내역 엑셀 검증, 졸업요건 진단, What-if 상담 |

## 핵심 모듈

| 모듈 | 역할 |
|---|---|
| `main.py` | 추천 파이프라인 오케스트레이션 |
| `scheduler.py` | 로컬 PC에서 자동 갱신/텔레그램 발송을 돌릴 때 사용 |
| `modules/store.py` | SQLite/Supabase 공통 저장소 API |
| `modules/scraper.py` | 국민대/링커리어 등 정보 수집 |
| `modules/analyzer.py` | LLM 기반 적합도 분석, 요약 생성 |
| `modules/validator.py` | 실제 일정, 시간표, 시험기간 기반 가용시간 검증 |
| `modules/digest.py` | 분야별 주기 설정에 맞춰 추천 갱신/발송 |
| `modules/telegram_callbacks.py` | 텔레그램 승인 버튼 처리 |
| `modules/executor.py` | Notion 캘린더 일정 생성 |
| `modules/timetable.py` | 시간표 PDF/표 저장 및 주간 수업시간 계산 |
| `modules/academic_calendar.py` | 국민대 학사일정/시험기간 반영 |
| `modules/graduation_link.py` | 졸업진단 결과를 추천 프로필로 연동 |
| `graduation_center/v2/` | 수강내역 엑셀 파싱, 졸업사정, 로드맵, 리스크, RAG 해설 |

## 수집 소스

| 분야 | 주요 소스 |
|---|---|
| 장학금 | 국민대 전체 장학공지, 경영대학 장학공지 |
| 공모전·대회 | 링커리어 공모전, 국민대/경영대학 공지 |
| 대외활동·서포터즈 | 링커리어 대외활동 |
| 학사일정 | 국민대 학사공지, 학사일정성 공지 |
| 채용·인턴 | 링커리어 채용·인턴, 국민대 SW 취업공지 |
| 자격증 | 링커리어 교육·자격증 |
| 기타 | 위 분야로 분류되지 않은 보조 공지 |

시연 기본값은 `SOURCE_FOCUS_FILTER=true`이며, AI/데이터/경영/인턴/분석 키워드 중심으로 후보를 좁힙니다.

## 추천 기준

- 포트폴리오 기반 관심 직무와 강점
- 시간표 PDF에서 추출한 고정 수업 시간
- Notion 캘린더의 실제 일정
- 국민대 학사일정과 시험기간
- 수강내역 엑셀 기반 졸업 미충족 요건과 이수 완료 과목
- 마감일, 예상 필요 시간, 활동 분야 적합도
- 사용자가 과거에 승인/무시한 피드백

시험기간에는 가용시간을 크게 줄여 무리한 추천을 보류합니다. 단, 분야별 최상위 후보는 `시험기간 주의`로 표시해 시연 화면이 비지 않도록 했습니다. 학사일정은 점수 순위보다 일정성 공지 자체를 보여주는 방식입니다.

## 추천 상태

| 상태 | 의미 |
|---|---|
| `추천완료` | 웹 추천함에 표시되는 후보 |
| `승인` | 사용자가 노션에 추가한 후보 |
| `거절` | 사용자가 무시한 후보 |
| `보류/제외` | 시간, 마감, 관련도 조건을 통과하지 못한 후보 |

새로고침은 기존 추천을 초기화하지 않고 새 후보를 누적 저장합니다. 같은 URL의 제목·본문·마감일·분야가 그대로면 기존 분석을 유지하고, 신규/변경 공지만 다시 LLM 분석합니다.

## 처음 보는 사람을 위한 자료

- 실행 가이드: [`사용설명서.md`](사용설명서.md)
- 서버 자동화: [`docs/server_automation.md`](docs/server_automation.md)
- Supabase 스키마: [`docs/supabase_schema.sql`](docs/supabase_schema.sql)
- 웹 사용 설계도 PDF: [`웹_사용_설계도.pdf`](웹_사용_설계도.pdf)

## 클로드에게 이어서 맡길 때

현재 구현의 기준 파일은 아래입니다.

- 추천 화면 개선: `pages/1_내_맞춤_추천함.py`
- 추천 분석 품질: `modules/analyzer.py`, `modules/scraper.py`
- 자동 갱신/발송: `modules/digest.py`, `main.py`, `.github/workflows/daily-digest.yml`
- 텔레그램 승인 처리: `modules/telegram_callbacks.py`, `.github/workflows/telegram-callbacks.yml`
- 원격 DB: `modules/store.py`, `docs/supabase_schema.sql`
- 시간표/가용시간: `modules/timetable.py`, `modules/validator.py`, `modules/academic_calendar.py`
- 졸업진단 v2: `pages/2_졸업_진단.py`, `modules/graduation_link.py`, `graduation_center/v2/`, `graduation_center/data/graduation/`

주의할 점:

- `.env`와 `data/`는 커밋하지 않습니다.
- 운영 자동화는 Supabase를 기준으로 동작하므로 GitHub Actions에서 `STORE_BACKEND=supabase`가 필요합니다.
- GitHub Variables `DIGEST_CATEGORIES`를 비워두면 웹 설정의 `매일/매주/수동/끄기`가 서버 자동화에도 그대로 적용됩니다.
- Notion은 캘린더와 포트폴리오만 사용합니다. 추천 이력/설정/졸업 데이터는 Supabase 또는 SQLite에 있습니다.
- 텔레그램 발송은 설정된 분야의 1순위 추천 중심입니다. 정보성 분야는 여러 건이 같이 갈 수 있습니다.
