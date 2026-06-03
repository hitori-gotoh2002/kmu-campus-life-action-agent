"""modules/deadline.py
Deadline Guard 에이전트.
- 마감이 지난 공고는 추천에서 제외
- 마감 임박(D-N) 항목은 우선순위 가점

주의: 국민대 학사공지의 notice.date 는 '게시일'이라 마감으로 쓰면 안 됨.
  → 제목의 '~M/D' 마감 표기, 또는 링커리어(마감일=close date) 만 마감으로 인정.
"""
from __future__ import annotations

import datetime as dt
import re

_YEAR = 2026


def deadline_date(notice) -> dt.date | None:
    """공고의 실제 마감일(date) 추정. 알 수 없으면 None."""
    title = getattr(notice, "title", "") or ""
    m = re.search(r"~\s*(\d{1,2})\s*/\s*(\d{1,2})", title)
    if m:
        try:
            return dt.date(_YEAR, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    src = getattr(notice, "source", "") or ""
    if "링커리어" in src:
        s = (getattr(notice, "date", "") or "").replace(".", "-")
        try:
            y, mo, d = (int(x) for x in s.split("-")[:3])
            return dt.date(y, mo, d)
        except (ValueError, IndexError):
            return None
    return None  # 마감 불명 → 만료 처리하지 않음


def days_left(notice, today: dt.date | None = None) -> int | None:
    d = deadline_date(notice)
    if not d:
        return None
    return (d - (today or dt.date.today())).days


def is_expired(notice, today: dt.date | None = None) -> bool:
    dl = days_left(notice, today)
    return dl is not None and dl < 0


def urgency_bonus(notice, today: dt.date | None = None) -> int:
    """마감 임박 가점: D-3 이내 +10, D-7 이내 +5."""
    dl = days_left(notice, today)
    if dl is None:
        return 0
    if dl <= 3:
        return 10
    if dl <= 7:
        return 5
    return 0
