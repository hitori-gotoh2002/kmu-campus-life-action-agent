# 서버 자동 발송 설정

이 프로젝트는 GitHub Actions에서 매일 아침 텔레그램 추천 발송을 실행할 수 있다.
로컬 PC가 꺼져 있어도 GitHub 서버가 `main.py`를 `digest` 모드로 실행한다.

## 실행 방식

- 매일 추천 발송 워크플로: `.github/workflows/daily-digest.yml`
- 텔레그램 버튼 처리 워크플로: `.github/workflows/telegram-callbacks.yml`
- 실행 시각: 매일 08:00 KST
- GitHub cron: `0 23 * * *` (UTC 기준)
- 수동 실행: GitHub 저장소의 `Actions > Daily KMU Digest > Run workflow`
- 기본 서버 추천/발송 분야: `공모전·대회`, `학사일정`

텔레그램의 `노션에 추가` / `무시` 버튼은 `Telegram Callback Processor`가 5분마다 확인한다.
승인된 추천은 서버에서 Notion 캘린더에 추가되고, 처리 이력은 `data/agent.db` 캐시로 이어받는다.

## GitHub Secrets

저장소 `Settings > Secrets and variables > Actions > Repository secrets`에 아래 값을 등록한다.

- `OPENAI_API_KEY`
- `NOTION_API_KEY`
- `NOTION_CALENDAR_DB_ID`
- `NOTION_PORTFOLIO_PAGE_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

현재 워크플로는 `LLM_PROVIDER=openai`, `OPENAI_MODEL=gpt-4o-mini`로 실행된다.

## 분야 조정

GitHub Actions 서버에서는 로컬 `data/agent.db`가 없을 수 있으므로 환경변수로 분야를 고정한다.

- `DIGEST_CATEGORIES`: 매일 분석할 분야
- `TELEGRAM_CATEGORIES`: 텔레그램으로 발송할 분야

예시:

```yaml
DIGEST_CATEGORIES: "장학금,공모전·대회,학사일정"
TELEGRAM_CATEGORIES: "공모전·대회,학사일정"
```

## 주의

GitHub Actions 예약 실행은 GitHub 서버에서 동작하므로 로컬 PC 전원 상태와 무관하다.
다만 예약 워크플로는 기본 브랜치에 올라간 파일 기준으로 실행되며, GitHub 서버 상황에 따라 몇 분 늦게 시작될 수 있다.
텔레그램 버튼 처리도 최대 몇 분 지연될 수 있다.
