"""modules/validator.py
일정 타당성 검증 에이전트 (Notion 일정 DB 기반).

  - 사용자가 직접 입력한 Notion '주간 일정 DB'(요일 + 시작/종료 시간)를 읽어
    이번 주 고정 일정(수업/학회/회의 등)의 총 Busy 시간을 합산
  - 순수 공강 = 주간 총 가용시간 - Busy 시간
  - 인간 생활 보호 버퍼: 공강의 70%(safe_ratio=0.7)만 업무에 사용 (30% 여유)
  - safe_free_hours >= estimated_hours_needed 이면 Pass

  * Google Calendar 연동은 제거. 일정 소스를 Notion 하나로 통합.
"""
from __future__ import annotations

import os

SAFE_RATIO = 0.7  # 30% 안전 버퍼

# Notion 일정 DB 속성 이름 (사용자가 만들 DB의 컬럼명과 일치해야 함)
PROP_TITLE = "일정명"      # title
PROP_DAY = "요일"          # select: 월/화/수/목/금/토/일
PROP_START = "시작"        # number 또는 rich_text "09:00"
PROP_END = "종료"          # number 또는 rich_text "12:00"


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


# ---------------------------------------------------------------------------
# 데모용 가짜 주간 일정 (사용자가 Notion에 입력했다고 가정한 데이터와 동일)
# 형식: (일정명, 요일, 시작시각, 종료시각)
# ---------------------------------------------------------------------------
_DEMO_SCHEDULE = [
    ("빅데이터최신기술", "월", 13, 15),
    ("자료구조", "월", 15, 16),
    ("인지밴드", "월", 18, 21),
    ("생성형AI와비즈니스", "화", 12, 15),
    ("데이터구조와알고리즘", "화", 15, 18),
    ("Xi 세션", "화", 18, 20),
    ("dna회의", "화", 22, 23),
    ("빅데이터최신기술", "수", 13, 15),
    ("자료구조", "수", 15, 16),
    ("데이터베이스입문", "목", 15, 17),
    ("사회적행동과성격심리학", "금", 10.5, 12),
    ("딥러닝", "금", 13, 15),
    ("Dna 스터디", "금", 18, 19),
]


def _hours_from_schedule(rows: list[tuple]) -> float:
    """(이름, 요일, 시작, 종료) 목록에서 총 점유 시간(h) 합산."""
    total = 0.0
    for _name, _day, start, end in rows:
        total += max(0.0, float(end) - float(start))
    return total


def _demo_busy_hours() -> float:
    total = _hours_from_schedule(_DEMO_SCHEDULE)
    print(f"   [validator] Notion 일정 DB 읽기(demo): {len(_DEMO_SCHEDULE)}건 "
          f"→ 주간 Busy {total:.0f}시간")
    return total


def _parse_time(value) -> float:
    """Notion 셀 값을 시각(float)으로 변환. 9 / 9.5 / '09:00' / '9:30' 지원."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if ":" in s:
        h, m = s.split(":")[:2]
        return int(h) + int(m) / 60.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _read_notion_property(props: dict, name: str):
    """Notion 페이지 속성에서 값 추출 (number / rich_text / select / title 대응)."""
    p = props.get(name, {})
    t = p.get("type")
    if t == "number":
        return p.get("number")
    if t == "rich_text":
        rt = p.get("rich_text", [])
        return rt[0]["plain_text"] if rt else None
    if t == "title":
        ti = p.get("title", [])
        return ti[0]["plain_text"] if ti else None
    if t == "select":
        sel = p.get("select")
        return sel["name"] if sel else None
    return None


def _real_busy_hours() -> float:
    """실제 Notion 일정 DB를 조회해 주간 Busy 시간 합산."""
    from notion_client import Client

    notion = Client(auth=os.getenv("NOTION_API_KEY"))
    # '내 캘린더'(개인 고정 일정)를 우선 사용, 없으면 기존 일정 DB로 폴백
    db_id = os.getenv("NOTION_CALENDAR_DB_ID") or os.getenv("NOTION_SCHEDULE_DB_ID")
    if not db_id:
        raise RuntimeError("NOTION_CALENDAR_DB_ID / NOTION_SCHEDULE_DB_ID 가 없습니다.")

    rows: list[tuple] = []
    cursor = None
    while True:
        kwargs = {"database_id": db_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        for page in resp.get("results", []):
            props = page.get("properties", {})
            name = _read_notion_property(props, PROP_TITLE) or "(무제)"
            day = _read_notion_property(props, PROP_DAY) or ""
            start = _parse_time(_read_notion_property(props, PROP_START))
            end = _parse_time(_read_notion_property(props, PROP_END))
            rows.append((name, day, start, end))
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break

    total = _hours_from_schedule(rows)
    print(f"   [validator] Notion 일정 DB 읽기: {len(rows)}건 → 주간 Busy {total:.1f}시간")
    return total


def validate_schedule(estimated_hours_needed: int, ctx: dict) -> dict:
    """일정 타당성 검증. 결과 dict 반환."""
    weekly_total = ctx.get("scheduling", {}).get("weekly_total_hours", 112)
    busy = _demo_busy_hours() if _is_demo() else _real_busy_hours()

    free_hours = max(0.0, weekly_total - busy)
    safe_free = round(free_hours * SAFE_RATIO, 1)
    buffer = round(free_hours * (1 - SAFE_RATIO), 1)

    passed = safe_free >= estimated_hours_needed

    print(f"   [validator] 순수 공강 {free_hours:.0f}h - 30% 버퍼({buffer:.0f}h) "
          f"= 사용가능 {safe_free:.0f}h  (필요 {estimated_hours_needed}h)")
    print(f"   [validator] 30% 스케줄 안전 버퍼 계산 완료 → "
          f"{'PASS' if passed else 'HOLD'}")

    return {
        "passed": passed,
        "free_hours": free_hours,
        "safe_free_hours": safe_free,
        "buffer_hours": buffer,
        "needed_hours": estimated_hours_needed,
    }
