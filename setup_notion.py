"""setup_notion.py — 처음 사용자용 노션 셋업 (간소화).

새 구조에서 노션에는 **캘린더 + 포트폴리오만** 둡니다.
- 포트폴리오: 본인이 노션 페이지로 직접 작성 (자동생성 X)
- 캘린더: 이 스크립트가 '내 캘린더' DB를 자동 생성
- 그 외(프로필·이력·설정·졸업진단 등)는 로컬 백엔드(data/agent.db)에 자동 저장
- API 키는 웹 ⚙️설정 페이지에서 입력 (또는 .env)

사용:
  1) 노션 빈 페이지 생성 → 페이지 ··· → 연결(Connections)에 내 통합 추가
  2) 그 페이지 URL 끝 32자리 ID 복사
  3) python setup_notion.py <페이지ID>
"""
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
import os
from notion_client import Client
from notion_client.errors import APIResponseError

ENV_PATH = Path(__file__).parent / ".env"


def dashed(pid: str) -> str:
    p = pid.replace("-", "")
    return f"{p[0:8]}-{p[8:12]}-{p[12:16]}-{p[16:20]}-{p[20:32]}" if len(p) == 32 else pid


def update_env(key: str, value: str):
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, found = [], False
    for ln in lines:
        if ln.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}"); found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    if len(sys.argv) < 2:
        print("사용법: python setup_notion.py <노션페이지ID>")
        return
    page = dashed(sys.argv[1])
    notion = Client(auth=os.getenv("NOTION_API_KEY"))
    try:
        notion.pages.retrieve(page_id=page)
    except APIResponseError:
        print("✗ 페이지 접근 불가. 노션에서 그 페이지에 통합 '연결'을 추가했는지 확인하세요.")
        return

    sel = lambda opts: {"select": {"options": [{"name": o} for o in opts]}}
    print("[1] '내 캘린더' DB 생성 …")
    cal = notion.databases.create(
        parent={"type": "page_id", "page_id": page},
        title=[{"text": {"content": "내 캘린더"}}],
        properties={
            "일정명": {"title": {}}, "요일": sel(list("월화수목금토일")),
            "시작": {"number": {}}, "종료": {"number": {}}, "날짜": {"date": {}},
            "교수": {"rich_text": {}}, "장소": {"rich_text": {}},
            "유형": sel(["수업", "학회", "스터디", "회의", "개인", "활동"]),
        },
    )
    cid = cal["id"].replace("-", "")
    update_env("NOTION_CALENDAR_DB_ID", cid)
    print(f"   NOTION_CALENDAR_DB_ID={cid}  (.env 자동 기록)")
    time.sleep(0.3)

    print("\n끝! 다음 단계:")
    print("  · 노션에 '캘린더' DB를 열어 본인 시간표를 입력하고 캘린더 뷰를 추가")
    print("  · 노션 포트폴리오 페이지를 만들고 통합 연결 → 그 ID를 웹 ⚙️설정 또는 .env(NOTION_PORTFOLIO_PAGE_ID)")
    print("  · 웹 실행: streamlit run app.py → ⚙️설정에서 OpenAI/Notion/Telegram 키 입력")


if __name__ == "__main__":
    main()
