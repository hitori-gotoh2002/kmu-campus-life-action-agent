"""modules/preferences.py
Preferences — 로컬 백엔드(store) 기반. (노션 아님)
분야별 수신주기(매일/매주/수동/끄기)를 웹에서 설정 → store 에 저장.
오늘 자동 전달할 분야를 결정한다.
"""
from __future__ import annotations

import datetime as dt
import os

from modules import store

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def load_preferences() -> dict:
    """{분야: {주기, 채널}}."""
    return store.get_prefs()


def set_preference(category: str, cycle: str, channel: str = "웹") -> None:
    store.set_pref(category, cycle, channel)


def due_categories(today: dt.date | None = None) -> set[str]:
    """오늘 자동 전달 대상 분야."""
    today = today or dt.date.today()
    wd = WEEKDAYS[today.weekday()]
    weekly_day = os.getenv("DIGEST_WEEKLY_DAY", "월")
    due = set()
    for cat, p in load_preferences().items():
        cyc = p.get("주기")
        if cyc == "매일":
            due.add(cat)
        elif cyc == "매주" and wd == weekly_day:
            due.add(cat)
    return due
