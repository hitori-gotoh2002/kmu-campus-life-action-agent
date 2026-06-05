"""modules/preferences.py
Preferences — 로컬 백엔드(store) 기반. (노션 아님)
분야별 수신주기(매일/매주/수동/끄기)를 웹에서 설정 → store 에 저장.
오늘 자동 업데이트/전달할 분야를 결정한다.
"""
from __future__ import annotations

import datetime as dt
import os

from modules import store

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
ENV_CATEGORIES = "DIGEST_CATEGORIES"
ENV_TELEGRAM_CATEGORIES = "TELEGRAM_CATEGORIES"
ENV_ENABLED_CATEGORIES = "ENABLED_CATEGORIES"


def _split_categories(value: str | None) -> set[str]:
    if not value:
        return set()
    return {x.strip() for x in value.split(",") if x.strip()}


def load_preferences() -> dict:
    """{분야: {주기, 채널}}."""
    prefs = {cat: dict(pref) for cat, pref in store.get_prefs().items()}
    digest_cats = _split_categories(os.getenv(ENV_CATEGORIES))
    telegram_cats = _split_categories(os.getenv(ENV_TELEGRAM_CATEGORIES))

    for cat in digest_cats:
        prefs.setdefault(cat, {"주기": "매일", "채널": "웹"})
        prefs[cat]["주기"] = "매일"

    for cat in telegram_cats:
        prefs.setdefault(cat, {"주기": "매일", "채널": "텔레그램"})
        prefs[cat]["채널"] = "텔레그램"

    return prefs


def set_preference(category: str, cycle: str, channel: str = "웹") -> None:
    store.set_pref(category, cycle, channel)


def due_categories(today: dt.date | None = None) -> set[str]:
    """오늘 자동 업데이트/전달 대상 분야. 수동/끄기는 자동 실행에서 제외."""
    env_due = _split_categories(os.getenv(ENV_CATEGORIES))
    if env_due:
        return env_due

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


def enabled_categories() -> set[str]:
    """추천 대상 분야. '끄기'는 자동/수동 업데이트와 추천함 표시에서 제외."""
    env_enabled = _split_categories(os.getenv(ENV_ENABLED_CATEGORIES)) or _split_categories(os.getenv(ENV_CATEGORIES))
    if env_enabled:
        return env_enabled

    return {
        cat
        for cat, p in load_preferences().items()
        if p.get("주기") != "끄기"
    }
