"""modules/preferences.py
Preferences 에이전트.
노션 '추천 설정' DB(분야/주기/채널)를 읽어, 오늘 전달할 분야를 결정한다.
- 주기: 매일 / 매주(DIGEST_WEEKLY_DAY 요일에만) / 수동 / 끄기
"""
from __future__ import annotations

import datetime as dt
import os

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

# 노션 DB 미설정/실패 시 기본값
_DEFAULTS = {
    "장학금": {"주기": "매일", "채널": "텔레그램"},
    "공모전·대회": {"주기": "매일", "채널": "텔레그램"},
    "대외활동·서포터즈": {"주기": "매일", "채널": "텔레그램"},
    "학사일정": {"주기": "매주", "채널": "텔레그램"},
    "채용·인턴": {"주기": "끄기", "채널": "텔레그램"},
    "자격증": {"주기": "매주", "채널": "텔레그램"},
    "기타": {"주기": "수동", "채널": "웹"},
}


def load_preferences() -> dict:
    """{분야: {주기, 채널}} 반환."""
    db = os.getenv("NOTION_PREFS_DB_ID")
    if not db:
        return dict(_DEFAULTS)
    try:
        from notion_client import Client
        notion = Client(auth=os.getenv("NOTION_API_KEY"))
        prefs = {}
        r = notion.databases.query(database_id=db, page_size=100)
        for p in r["results"]:
            props = p["properties"]
            t = props.get("분야", {}).get("title", [])
            cat = t[0]["plain_text"] if t else ""
            cyc = (props.get("주기", {}).get("select") or {}).get("name", "수동")
            ch = (props.get("채널", {}).get("select") or {}).get("name", "텔레그램")
            if cat:
                prefs[cat] = {"주기": cyc, "채널": ch}
        return prefs or dict(_DEFAULTS)
    except Exception as e:
        print(f"   [prefs] 설정 로드 실패({e}) → 기본값")
        return dict(_DEFAULTS)


def due_categories(today: dt.date | None = None) -> set[str]:
    """오늘 자동 전달 대상 분야 집합."""
    today = today or dt.date.today()
    wd = WEEKDAYS[today.weekday()]
    weekly_day = os.getenv("DIGEST_WEEKLY_DAY", "월")
    prefs = load_preferences()
    due = set()
    for cat, p in prefs.items():
        cyc = p.get("주기")
        if cyc == "매일":
            due.add(cat)
        elif cyc == "매주" and wd == weekly_day:
            due.add(cat)
    return due
