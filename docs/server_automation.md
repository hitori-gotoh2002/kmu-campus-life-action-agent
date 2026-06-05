# 서버 자동 발송 설정

이 프로젝트는 GitHub Actions에서 매일 아침 텔레그램 추천 발송을 실행할 수 있다.
로컬 PC가 꺼져 있어도 GitHub 서버가 `main.py`를 `digest` 모드로 실행한다.

## 실행 방식

- 매일 추천 발송 워크플로: `.github/workflows/daily-digest.yml`
- 텔레그램 버튼 처리 워크플로: `.github/workflows/telegram-callbacks.yml`
- 실행 시각: 매일 08:00 KST
- GitHub cron: `0 23 * * *` (UTC 기준)
- 수동 실행: GitHub 저장소의 `Actions > Daily KMU Digest > Run workflow`
- 기본 서버 웹 갱신 분야: `장학금`, `공모전·대회`, `대외활동·서포터즈`, `학사일정`, `채용·인턴`, `자격증`, `기타`
- 기본 서버 텔레그램 발송 분야: `공모전·대회`, `학사일정`

즉 텔레그램 발송을 받지 않는 분야도 `DIGEST_CATEGORIES`에 들어 있으면 매일 수집·분석되어 웹 추천함에 누적된다.

주의: 원격 DB를 설정하지 않으면 GitHub Actions가 갱신하는 추천 이력은 GitHub Actions 캐시에 저장되는
`data/agent.db` 기준이다. 로컬 `localhost:8501` Streamlit 화면은 로컬 PC의 `data/agent.db`를 읽으므로,
로컬 화면까지 매일 자동 갱신하려면 PC에서 `python scheduler.py`를 실행해 두거나 아래 Supabase 원격 DB를 붙여야 한다.

## Supabase 원격 DB

Supabase를 붙이면 GitHub Actions, 로컬 Streamlit, 로컬 스케줄러가 모두 같은 추천 이력과 설정을 읽고 쓴다.
따라서 로컬 PC가 꺼져 있어도 GitHub Actions가 08시에 추천을 갱신하면, 나중에 로컬 웹을 열었을 때 같은 최신 추천함을 볼 수 있다.

1. Supabase 프로젝트를 만든다.
2. Supabase SQL Editor에서 `docs/supabase_schema.sql` 내용을 실행한다.
3. 로컬 `.env`에 아래 값을 넣는다.

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
```

4. GitHub 저장소 `Settings > Secrets and variables > Actions > Repository secrets`에 아래 Secrets를 추가한다.

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

5. 기존 로컬 추천 이력을 원격 DB로 옮기려면 한 번 실행한다.

```powershell
python scripts/migrate_sqlite_to_supabase.py
```

API 키까지 `settings` 테이블에 같이 옮기려면 아래 옵션을 사용할 수 있지만, 보통은 GitHub Secrets와 로컬 `.env`에 두는 편이 낫다.

```powershell
python scripts/migrate_sqlite_to_supabase.py --include-settings
```

텔레그램의 `노션에 추가` / `무시` 버튼은 `Telegram Callback Processor`가 5분마다 확인한다.
승인된 추천은 서버에서 Notion 캘린더에 추가된다.
Supabase를 설정하면 처리 이력은 Supabase에 저장되고, 설정하지 않으면 `data/agent.db` 캐시로 이어받는다.

## GitHub Secrets

저장소 `Settings > Secrets and variables > Actions > Repository secrets`에 아래 값을 등록한다.

- `OPENAI_API_KEY`
- `NOTION_API_KEY`
- `NOTION_CALENDAR_DB_ID`
- `NOTION_PORTFOLIO_PAGE_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SUPABASE_URL` (원격 DB 사용 시)
- `SUPABASE_SERVICE_ROLE_KEY` (원격 DB 사용 시)

현재 워크플로는 `LLM_PROVIDER=openai`, `OPENAI_MODEL=gpt-4o-mini`로 실행된다.

## 분야 조정

GitHub Actions 서버에서는 로컬 `data/agent.db`가 없을 수 있으므로 환경변수로 분야를 고정한다.
기본값은 workflow에 들어 있으며, GitHub 저장소 `Settings > Secrets and variables > Actions > Variables`에서
같은 이름의 Variables를 만들면 코드 수정 없이 덮어쓸 수 있다.

- `DIGEST_CATEGORIES`: 매일 분석해 웹 추천함을 갱신할 분야
- `TELEGRAM_CATEGORIES`: 텔레그램으로 발송할 분야

예시:

```yaml
DIGEST_CATEGORIES: "장학금,공모전·대회,대외활동·서포터즈,학사일정,채용·인턴,자격증"
TELEGRAM_CATEGORIES: "공모전·대회,학사일정"
```

## 주의

GitHub Actions 예약 실행은 GitHub 서버에서 동작하므로 로컬 PC 전원 상태와 무관하다.
다만 예약 워크플로는 기본 브랜치에 올라간 파일 기준으로 실행되며, GitHub 서버 상황에 따라 몇 분 늦게 시작될 수 있다.
텔레그램 버튼 처리도 최대 몇 분 지연될 수 있다.
