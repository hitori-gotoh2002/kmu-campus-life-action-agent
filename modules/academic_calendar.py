"""modules/academic_calendar.py
국민대학교 공식 학사일정 기반 가용시간 보정.

추천 일정 검증 시 시험기간/시험 직전에는 실제 가용시간을 크게 낮춘다.
소스: 국민대학교 학사일정 페이지
https://www.kookmin.ac.kr/user/scGuid/scSchedule/index.do
"""
from __future__ import annotations

import datetime as dt
import re

import requests
from bs4 import BeautifulSoup

from modules import store

SCHEDULE_URL = "https://www.kookmin.ac.kr/user/scGuid/scSchedule/index.do"
CACHE_KEY_PREFIX = "kmu_academic_calendar"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def _today() -> dt.date:
    return dt.date.today()


def _academic_year_from_html(html: str) -> int:
    m = re.search(r"(\d{4})\s*학년도", html)
    return int(m.group(1)) if m else _today().year


def _event_type(title: str) -> str:
    if "시험" in title:
        return "exam"
    if "방학" in title:
        return "break"
    if "수강신청" in title:
        return "registration"
    if "성적" in title:
        return "grade"
    return "academic"


def _parse_date_range(text: str, base_year: int) -> tuple[str, str] | None:
    # 예: 06.09 (화) ~ 06.15 (월), 2027년 01.04 (월) ~ 01.22 (금)
    clean = re.sub(r"\s+", " ", text or "")
    m = re.search(
        r"(?:(?P<sy>\d{4})년\s*)?(?P<sm>\d{1,2})\.(?P<sd>\d{1,2})"
        r"(?:\s*\([^)]*\))?"
        r"(?:\s*~\s*(?:(?P<ey>\d{4})년\s*)?(?P<em>\d{1,2})\.(?P<ed>\d{1,2}))?",
        clean,
    )
    if not m:
        return None

    sy = int(m.group("sy") or base_year)
    sm, sd = int(m.group("sm")), int(m.group("sd"))
    em = int(m.group("em") or sm)
    ed = int(m.group("ed") or sd)
    ey = int(m.group("ey") or sy)
    if not m.group("ey") and em < sm:
        ey = sy + 1
    try:
        start = dt.date(sy, sm, sd)
        end = dt.date(ey, em, ed)
    except ValueError:
        return None
    return start.isoformat(), end.isoformat()


def fetch_events(year: int | None = None, force: bool = False) -> list[dict]:
    """국민대 공식 학사일정을 가져와 [{start,end,title,type}] 형태로 반환."""
    year = year or _today().year
    cache_key = f"{CACHE_KEY_PREFIX}_{year}"
    cached = store.kv_get(cache_key)
    if cached and not force:
        if isinstance(cached, dict):
            fetched_at = cached.get("fetched_at")
            try:
                fetched = dt.datetime.fromisoformat(fetched_at).date() if fetched_at else None
            except ValueError:
                fetched = None
            if fetched and (_today() - fetched).days < 1:
                return cached.get("events", [])
        elif isinstance(cached, list):
            return cached

    resp = requests.get(SCHEDULE_URL, headers=BROWSER_HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    html = resp.text
    base_year = _academic_year_from_html(html)
    soup = BeautifulSoup(html, "html.parser")

    events: list[dict] = []
    for tr in soup.select("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        date_text, title = cells[-2], cells[-1]
        parsed = _parse_date_range(date_text, base_year)
        if not parsed or not title:
            continue
        start, end = parsed
        if int(start[:4]) < year - 1 or int(start[:4]) > year + 1:
            continue
        events.append({
            "start": start,
            "end": end,
            "title": title,
            "type": _event_type(title),
            "source": SCHEDULE_URL,
        })

    store.kv_set(cache_key, {
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": SCHEDULE_URL,
        "events": events,
    })
    return events


def _overlaps(a_start: dt.date, a_end: dt.date, b_start: dt.date, b_end: dt.date) -> bool:
    return a_start <= b_end and b_start <= a_end


def pressure(today: dt.date | None = None) -> dict:
    """현재 주 기준 학사일정 압박도.

    multiplier는 추천에 쓸 수 있는 가용시간에 곱한다.
    시험기간: 0.35, 시험 7일 전: 0.55, 보강/성적 기간: 0.8, 일반: 1.0
    """
    today = today or _today()
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=6)

    try:
        events = fetch_events(today.year)
    except Exception as e:
        return {
            "multiplier": 1.0,
            "label": "학사일정 확인 실패",
            "reason": str(e),
            "events": [],
        }

    active = []
    upcoming_exam = []
    grade_or_makeup = []
    for event in events:
        start = dt.date.fromisoformat(event["start"])
        end = dt.date.fromisoformat(event["end"])
        if _overlaps(start, end, week_start, week_end):
            active.append(event)
            if event["type"] == "grade" or "보강" in event["title"]:
                grade_or_makeup.append(event)
        if event["type"] == "exam":
            if _overlaps(start, end, week_start, week_end):
                return {
                    "multiplier": 0.35,
                    "label": "시험기간",
                    "reason": event["title"],
                    "events": active,
                }
            if today <= start <= today + dt.timedelta(days=7):
                upcoming_exam.append(event)

    if upcoming_exam:
        return {
            "multiplier": 0.55,
            "label": "시험 직전",
            "reason": upcoming_exam[0]["title"],
            "events": upcoming_exam,
        }
    if grade_or_makeup:
        return {
            "multiplier": 0.8,
            "label": "보강/성적 기간",
            "reason": grade_or_makeup[0]["title"],
            "events": grade_or_makeup,
        }
    return {
        "multiplier": 1.0,
        "label": "일반 학사기간",
        "reason": "",
        "events": active,
    }
