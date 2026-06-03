# KMU 캠퍼스 라이프 에이전트 (커리어 추천 + 졸업 진단)

> 🔰 **처음이세요?** → [`사용설명서.md`](사용설명서.md) 한 장으로 설치·키 발급·실행까지.
> 셋업: `python setup_notion.py <노션페이지ID>` (캘린더 DB 자동 생성) → 웹 ⚙️설정에서 키 입력.

## 현재 아키텍처 (노션 최소화 · v2)
```
노션      = 내 캘린더(시간표) + 포트폴리오   ← 이 둘만
로컬 백엔드 = 프로필·추천이력·설정·졸업진단·API키  (modules/store.py, data/agent.db 자동생성)
웹(Streamlit) = ⚙️설정(키·자동수신) · 📋추천 리뷰 · 🎓졸업 진단(영속)
텔레그램  = 자동수신 분야 → 버튼 발송 → [승인] 시 노션 캘린더 등록
```
- API 키는 **웹 ⚙️설정**에서 입력(개인별 비서) → 로컬에만 저장
- 추천 상세(근거·체크리스트·로드맵)는 **웹/백엔드**에 있고, 노션엔 **캘린더 일정만** 기록
- 아래 Phase 기록은 개발 변천사(일부는 v2에서 백엔드로 이전됨)

> 전체 구조 다이어그램: `통합_설계도.pdf`

국민대 AI빅데이터융합경영학과 학우 맞춤형 **Human-in-the-Loop 멀티 에이전트 커리어 비서**.

학사·공모전 공지를 자동 수집·분석해 내 전공/실력/일정에 맞는 활동만 골라
텔레그램으로 추천하고, **내가 승인하면** Notion에 일정 블록 + 칸반 티켓을 자동 생성합니다.

> 일정 소스를 **Notion 하나로 통합**했습니다. (구글 캘린더 연동 없음 → OAuth 세팅 불필요)
> 수업/고정 일정은 사용자가 Notion 일정 DB에 직접 입력합니다.

## 아키텍처 (역할별 에이전트)

```
목표 → [오케스트레이터 main.py]
        │
        ├─ [1] scraper      공지 수집 (봇 차단 회피 + 인메모리 즉시 파기)
        ├─ [2] document_ai  첨부 PDF 평가기준/표 파싱 (BytesIO + pdfplumber)
        ├─ [3] analyzer      LLM 맥락 분석 + 역량 가중치(0.7/1.5) + Critic 검증
        ├─ [4] validator     Notion 일정 DB 읽기 + 30% 안전 버퍼 검증
        └─ [5] executor      텔레그램 승인(HITL) → [6] Notion 일정+칸반 실행
```

## 핵심 설계 포인트

| 기능 | 위치 | 의미 |
|---|---|---|
| 인메모리 파기 | scraper | HTML 디스크 미저장, RAM에서 추출 후 즉시 폐기 |
| 역량 가중치 | analyzer | 숙련 분야 ×0.7, 생소 분야 ×1.5 동적 보정 |
| Critic 검증 | analyzer | 점수 과대·환각 2차 차단 |
| 30% 안전 버퍼 | validator | 공강의 70%만 사용, 일정 과부하 방지 |
| Human-in-the-Loop | executor | 승인 없이는 어떤 액션도 실행하지 않음 |

## 필요한 Notion DB 2개

1. **일정 DB** (`NOTION_SCHEDULE_DB_ID`) — validator가 읽어 공강 계산
   - 속성: `일정명`(title), `요일`(select: 월~일), `시작`(number 또는 "09:00"), `종료`(number 또는 "12:00")
2. **칸반 DB** (`NOTION_KANBAN_DB_ID`) — executor가 승인 후 티켓/초안 생성
   - 속성: `Name`(title), `Status`(select: To Do / In Progress / Done)

## 빠른 실행 (데모 - 키 불필요)

```bash
python main.py                   # 가짜 데이터로 전체 파이프라인 시연
DEMO_APPROVE=no python main.py   # 거절 시나리오
```

## 실제 운영 전환

```bash
pip install -r requirements.txt
cp .env.example .env             # 키 입력 후 DEMO_MODE=false 로 변경
python main.py
```

필요한 키: LLM(Gemini 또는 OpenAI), Notion API + DB 2개, Telegram Bot Token.

## 수집 소스 현황 (scraper.py)

| 소스 | URL | 방식 | 상태 |
|---|---|---|---|
| 국민대 학사공지 | `www.kookmin.ac.kr/user/kmuNews/notice/index.do` | 서버렌더 (requests+bs4) | ✅ 연결됨 (목록+본문+PDF첨부) |
| 국민대 경영대학 공지 | `biz.kookmin.ac.kr/community/notice/` | 서버렌더 (requests+bs4) | ✅ 연결됨 (목록+본문+PDF첨부) |
| 링커리어 공모전 | `api.linkareer.com/graphql` (activityTypeID 3) | 공식 GraphQL API | ✅ 연결됨 (제목+기관+마감일) |
| 링커리어 대외활동 | `api.linkareer.com/graphql` (activityTypeID 1) | 공식 GraphQL API | ✅ 연결됨 (제목+기관+마감일) |
| 학과 인스타그램 | `instagram.com/kmuabm_official` | 로그인벽 + 안티봇 | ❌ 스크래핑 비권장 (대안 필요) |

> 링커리어는 `status:OPEN`(모집중)을 최신순으로 조회. `LINKAREER_PAGE_SIZE`(기본 10)로 소스당 건수 조절.

- `TARGET_SOURCES` 에 `{name,url,base,parser}` 만 추가하면 소스 확장 가능.
- `MAX_DETAIL_PER_SOURCE`(기본 8) 로 상세페이지 진입 수 제한.
- 첨부는 현재 **.pdf 만** 파싱. 국내 공지에 흔한 **.hwp 는 별도 파서 필요**(미구현).

## Phase 1 (구현 완료) — 분류 · 신규필터 · 프로필 동기화

| 에이전트 | 파일 | 역할 |
|---|---|---|
| Classifier | `modules/classifier.py` | 7종 분류(장학금·공모전·대회·대외활동·서포터즈·학사일정·채용·인턴·자격증·기타). 제목+게시판분류 규칙 우선, 애매하면 LLM |
| History Store | `modules/history.py` | 노션 '커리어 추천 이력' DB. URL로 신규 판별 → **중복 추천 방지**, 처리 이력·승인/거절 기록 |
| Profile Sync | `modules/profile.py` | 노션 '내 프로필' DB(항목/값)를 읽어 분석용 프로필 생성. 노션에서 고치면 다음 실행부터 반영 |

추가 `.env` 키: `NOTION_HISTORY_DB_ID`, `NOTION_PROFILE_DB_ID`
실행 옵션: `PIPELINE_DRY_RUN=true` (승인/실행 생략, 분류·랭킹·이력 기록까지만 — 테스트/스케줄 점검용)

## Phase 2 (구현 완료) — 수신주기 · 추천서 · 스케줄러

| 에이전트 | 파일 | 역할 |
|---|---|---|
| Preferences | `modules/preferences.py` | 노션 '추천 설정' DB(분야/주기/채널). 분야별 매일·매주·수동·끄기 → 오늘 전달할 분야 결정 |
| Digest | `modules/digest.py` | 후보를 분야별로 묶어 '추천서' 구성, 자동 전달 대상만 텔레그램 발송 |
| Scheduler | `scheduler.py` | 매일 정해진 시각에 digest 자동 실행 (APScheduler). Windows 작업 스케줄러로도 가능 |

실행 모드(`.env DELIVERY_MODE`): `approval`(단건 승인·기본) / `digest`(분야별 추천서)
추가 `.env` 키: `NOTION_PREFS_DB_ID`, `DIGEST_WEEKLY_DAY`(매주 발송 요일), `DIGEST_HOUR`/`DIGEST_MINUTE`
자동 발송: `python scheduler.py` (즉시 1회 테스트: `RUN_NOW=true python scheduler.py`)

## Phase 3 (구현 완료) — 웹 리뷰 UI

| 구성 | 파일 | 역할 |
|---|---|---|
| 웹 리뷰 UI | `app.py` (Streamlit) | 이력 DB의 '추천완료' 항목을 분야별로 표시 → 항목별 [노션에 추가]/[무시] |
| 검토목록 조회 | `modules/history.py` `list_pending()` | 상태=추천완료 행을 적합도순으로 반환 |

실행: `streamlit run app.py` → http://localhost:8501
- [✅ 노션에 추가] → executor가 캘린더(날짜) + 칸반 상세티켓 생성, 이력 상태 '승인'
- [🗑️ 무시] → 이력 상태 '거절'
- 데이터 공급: `python main.py`(approval) 또는 `scheduler.py`(digest)가 '추천완료' 행을 쌓음

## 프로필 소스 전환 — 포트폴리오 분석 (LLM)

수동 입력 대신 **노션 포트폴리오를 LLM으로 분석**해 프로필을 자동 생성한다.

| 구성 | 파일 | 역할 |
|---|---|---|
| Portfolio Analyzer | `modules/portfolio.py` | 포트폴리오 페이지+Featured Projects DB 읽기 → LLM으로 강점·관심사·희망직무·프로젝트 추출 → '내 프로필' DB 동기화 |
| 실행 | `analyze_portfolio.py` | `python analyze_portfolio.py` (포트폴리오 수정/교체 시 재실행) |

- `.env`: `NOTION_PORTFOLIO_PAGE_ID`
- 포트폴리오로 알 수 없는 항목(미충족졸업요건·주간가용시간)은 건드리지 않음 → 그대로 직접 관리
- **범용성**: 포트폴리오만 바꿔 끼우고 재실행하면 다른 사람에게도 그대로 적용
- 다운스트림은 변경 없음(기존 `profile.load_profile()`이 갱신된 DB를 읽음)

## Phase 4 (구현 완료) — 마감관리 · LLM 검증관 · 피드백 학습

| 에이전트 | 파일 | 역할 |
|---|---|---|
| Deadline Guard | `modules/deadline.py` | 마감 지난 공고 제외, 임박(D-3 +10 / D-7 +5) 우선순위 가점. 제목 `~M/D`·링커리어 마감일만 인정(게시일과 구분) |
| Critic (LLM) | `modules/analyzer.py` `llm_critic()` | 후보에만 LLM 2차 검증 — 프로필 부합/과대평가 여부 판단해 반려 가능 (비용 절약 위해 검증 통과분만) |
| Feedback | `modules/feedback.py` | 이력 DB 승인/거절을 분야별 집계 → 다음 추천 점수 ±15 보정(개인화 루프) |

랭킹 점수 = 적합도 + 마감임박 가점 + 피드백 보정. `_print_ranking`에 `70→80(학습+10)` 형태로 표시.

## 전체 에이전트 (Phase 0~4 완료)

수집(Scraper) · 분류(Classifier) · 이력/중복(History) · 프로필(Profile←Portfolio Analyzer) ·
문서분석(Document AI) · 맥락분석(Analyzer) · 검증(Critic 규칙+LLM) · 마감(Deadline) ·
일정검증(Validator) · 피드백(Feedback) · 설정(Preferences) · 추천서(Digest) ·
전달(Telegram/웹 Streamlit) · 실행(Executor) · 스케줄러

## 졸업도우미 통합 (추천 + 졸업)

성적증명서 기반 졸업 진단을 붙이고, 두 시스템을 하나의 웹으로 통합.

| 구성 | 파일 | 역할 |
|---|---|---|
| 성적증명서 추출 | `modules/transcript.py` | 이미지형 PDF → pypdfium2 렌더 → OpenAI 비전. 총학점·GPA·이수구분별 추출 (촘촘한 표는 비전이 불안정 → 웹에서 사람 검증) |
| 졸업 진단 | `modules/graduation.py` | 이수구분별(성적) vs 기준학점(졸업요건 DB) → 구분별 부족·위험도. 미충족요건을 '내 프로필' 미충족졸업요건에 반영 → 추천이 계절학기·전공 우선 |
| 통합 웹 | `app.py` + `pages/` | Streamlit 멀티페이지: 홈 / ①추천 리뷰 / ②졸업 진단(성적증명서 업로드) |

추가 노션 DB: 내 캘린더(시간표→가용시간), 졸업요건(기준학점), 이수내역
추가 `.env`: `NOTION_CALENDAR_DB_ID`, `NOTION_GRAD_REQ_DB_ID`, `NOTION_GRAD_HISTORY_DB_ID`
실행: `streamlit run app.py`

> ⚠️ 졸업요건 기준학점은 **예시값** — 노션 '졸업요건' DB에 실제 핸드북 값을 넣어야 정확.
> 성적증명서의 이수구분별 학점은 **업로드 후 화면에서 확인·수정**(사람 검증) 단계를 거침.

## 다음 후보 (선택)

- 졸업 로드맵(LLM 학기별 수강 추천) · 마감 임박 리마인더 · 멀티 사용자(포트폴리오별)
