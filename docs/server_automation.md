# 서버 자동화 설정

이 프로젝트는 GitHub Actions와 Supabase를 사용해 로컬 PC가 꺼져 있어도 추천 갱신과 텔레그램 발송을 수행합니다.

## 현재 운영 구조

```text
GitHub Actions
├─ Daily KMU Digest: 매일 08:00 KST
│  ├─ 웹 공지/링커리어/국민대 공지 수집
│  ├─ LLM 추천 분석
│  ├─ Supabase recommendations 업데이트
│  └─ 텔레그램 수신 분야 발송
└─ Telegram Callback Processor: 5분마다
   ├─ 텔레그램 버튼 클릭 여부 확인
   ├─ 승인/무시 상태를 Supabase에 반영
   └─ 승인 시 Notion 캘린더에 일정 생성
```

## 워크플로

| 워크플로 | 파일 | 실행 조건 |
|---|---|---|
| Daily KMU Digest | `.github/workflows/daily-digest.yml` | `0 23 * * *` UTC = 매일 08:00 KST |
| Telegram Callback Processor | `.github/workflows/telegram-callbacks.yml` | `*/5 * * * *` = 5분마다 |

두 워크플로 모두 `STORE_BACKEND=supabase`를 명시합니다.  
따라서 Supabase Secrets가 없으면 조용히 로컬 SQLite로 떨어지지 않고 실패하도록 되어 있습니다.

## 필요한 GitHub Secrets

GitHub 저장소에서 `Settings > Secrets and variables > Actions > Repository secrets`에 등록합니다.

| Secret | 용도 |
|---|---|
| `OPENAI_API_KEY` | 추천 분석과 요약 생성 |
| `NOTION_API_KEY` | Notion 캘린더 일정 등록 |
| `NOTION_CALENDAR_DB_ID` | 실제 일정/추천 일정이 들어가는 Notion DB |
| `NOTION_PORTFOLIO_PAGE_ID` | 포트폴리오 페이지 분석 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 메시지 발송/버튼 확인 |
| `TELEGRAM_CHAT_ID` | 메시지를 받을 채팅 |
| `SUPABASE_URL` | 원격 DB URL |
| `SUPABASE_SERVICE_ROLE_KEY` | 원격 DB 서버 권한 키 |

## Supabase 초기 설정

1. Supabase 프로젝트 생성
2. SQL Editor에서 `docs/supabase_schema.sql` 실행
3. 로컬 `.env`에 아래 값 추가

```env
STORE_BACKEND=supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
```

4. GitHub Secrets에도 같은 Supabase 값 등록
5. 기존 로컬 데이터를 원격으로 옮길 때 한 번 실행

```powershell
python scripts\migrate_sqlite_to_supabase.py
```

기본 마이그레이션은 API 키가 들어 있는 `settings` 테이블을 옮기지 않습니다.  
키까지 원격 DB에 옮기려면 아래 옵션을 쓰지만, 보통은 로컬 `.env`와 GitHub Secrets에 분리해 두는 편이 안전합니다.

```powershell
python scripts\migrate_sqlite_to_supabase.py --include-settings
```

## 분야 설정

GitHub Actions의 기본값:

- 웹 추천함 갱신: `장학금,공모전·대회,대외활동·서포터즈,학사일정,채용·인턴,자격증,기타`
- 텔레그램 발송: `공모전·대회,학사일정`

GitHub Variables로 수정할 수 있습니다.

| Variable | 의미 |
|---|---|
| `DIGEST_CATEGORIES` | 매일 수집·분석해 Supabase 추천함에 저장할 분야 |
| `TELEGRAM_CATEGORIES` | 텔레그램으로 발송할 분야 |

예시:

```text
DIGEST_CATEGORIES=장학금,공모전·대회,대외활동·서포터즈,학사일정,채용·인턴,자격증
TELEGRAM_CATEGORIES=공모전·대회,채용·인턴
```

## 로컬 PC가 꺼져 있을 때

가능한 것:

- 매일 08시 추천 수집/분석
- Supabase 원격 DB 업데이트
- 텔레그램 발송
- 텔레그램 승인 버튼 확인
- 승인된 추천의 Notion 캘린더 등록

불가능한 것:

- `localhost:8501` 접속

로컬 웹은 PC가 켜져 있어야 열립니다. 다만 웹을 다시 열면 Supabase에 이미 저장된 최신 추천을 읽습니다.

## 로컬 스케줄러와의 차이

`python scheduler.py`는 로컬 PC가 켜져 있을 때만 동작합니다.  
GitHub Actions 자동화가 켜져 있으면 운영 자동화는 GitHub Actions를 기준으로 보면 됩니다.

로컬 스케줄러가 필요한 경우:

- 개발 중 즉시 테스트
- GitHub Actions를 쓰지 않는 개인 로컬 운영
- 로컬 콘솔에서 로그를 직접 보고 싶을 때

## 점검 방법

로컬에서 Supabase 연결 확인:

```powershell
python -c "from dotenv import load_dotenv; load_dotenv(); from modules import store; print(store.active_backend()); print(len(store.list_recs()))"
```

정상 예:

```text
supabase
118
```

GitHub Actions 상태 확인:

1. GitHub 저장소 `Actions` 탭 열기
2. `Daily KMU Digest` 최근 실행이 성공인지 확인
3. `Telegram Callback Processor` 최근 실행이 성공인지 확인

## 보안 주의

`SUPABASE_SERVICE_ROLE_KEY`는 서버 권한 키입니다. 노출되면 Supabase Dashboard에서 rotate한 뒤 아래 두 곳을 갱신하세요.

- 로컬 `.env`
- GitHub Repository Secrets
