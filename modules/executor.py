"""modules/executor.py
Human-in-the-Loop 의사결정 + 액션 에이전트.

  - 검증을 통과한 최적 후보를 텔레그램으로 발송 (인라인 버튼 [승인]/[거절])
  - 사용자가 [승인] 터치 시 콜백:
       1) Notion 일정 DB에 준비 일정 블록 등록
       2) Notion 칸반 보드에 작업 티켓 + 서류 초안 뼈대 생성
  - 에이전트가 독단적으로 실행하지 못하도록 항상 승인을 선행시킨다.
"""
from __future__ import annotations

import os
import textwrap
import time

from modules import calendar_summary


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _build_report(candidate: dict) -> str:
    n = candidate["notice"]
    a = candidate["analysis"]
    v = candidate["validation"]
    draft_link = candidate.get("draft_link", "(초안 링크)")
    schedule_line = (
        f"사용가능 {v['safe_free_hours']:.0f}h ≥ 필요 {v['needed_hours']}h  ✅"
        if v.get("passed")
        else f"⚠시험기간 주의: 사용가능 {v['safe_free_hours']:.0f}h < 필요 {v['needed_hours']}h"
    )
    return textwrap.dedent(f"""
    📊 추천 활동 리포트
    ─────────────────────────
    제목   : {n.title}
    도메인 : {a.domain}
    적합도 : {a.suitability_score}/100
    소요시간: {a.estimated_hours_needed}시간 (역량 가중치 반영)
    일정   : {schedule_line}
    근거   : {a.matching_reason}
    초안   : {draft_link}
    ─────────────────────────
    [승인] 하시면 노션 일정 등록 + 칸반 티켓을 생성합니다.
    """).strip()


# ---------------------------------------------------------------------------
# Telegram long-polling: 인라인 버튼 콜백 응답을 동기적으로 기다린다
# ---------------------------------------------------------------------------
def _poll_callback(token: str, timeout_sec: int = 300) -> bool:
    """
    getUpdates long-polling 으로 callback_query 를 기다린다.
    - approve:* → True
    - reject:*  → False
    - timeout_sec 초 경과 → False (자동 거절)
    """
    import requests

    # 현재 update_id 기준점 확보 (과거 처리된 업데이트 건너뜀)
    r = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"timeout": 0, "allowed_updates": ["callback_query"]},
        timeout=10,
    )
    updates = r.json().get("result", [])
    offset = (updates[-1]["update_id"] + 1) if updates else 0

    deadline = time.time() + timeout_sec
    print(f"   [executor] 사용자 응답 대기 중 (최대 {timeout_sec // 60}분)…")

    while time.time() < deadline:
        wait = min(30, int(deadline - time.time()))
        if wait <= 0:
            break
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": wait,
                    "allowed_updates": ["callback_query"],
                },
                timeout=wait + 5,
            )
        except requests.exceptions.Timeout:
            continue

        for upd in r.json().get("result", []):
            offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            if not cb:
                continue
            # 텔레그램 UI 로딩 스피너 제거
            requests.post(
                f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                json={"callback_query_id": cb["id"]},
                timeout=5,
            )
            data = cb.get("data", "")  # Telegram 콜백 필드명은 'data'
            if data.startswith("approve"):
                print("   [executor] 사용자 승인 ✅")
                return True
            if data.startswith("reject"):
                print("   [executor] 사용자 거절 ❌")
                return False

    print("   [executor] 응답 시간 초과 → 자동 거절 처리")
    return False


# ---------------------------------------------------------------------------
# 텔레그램 발송 + 승인 대기 (Human-in-the-Loop)
# ---------------------------------------------------------------------------
def request_approval(candidate: dict) -> bool:
    """후보를 사용자에게 보내고 승인 여부를 반환."""
    report = _build_report(candidate)

    if _is_demo():
        print("\n[5] 텔레그램 발송 (Human-in-the-Loop)")
        print("───── Telegram 메시지 미리보기 ─────")
        print(report)
        print("   [ 승인 ✅ ]   [ 거절 ❌ ]")
        print("────────────────────────────────────")
        decision = os.getenv("DEMO_APPROVE", "yes").lower() == "yes"
        print(f"   [executor] (시뮬레이션) 사용자 선택 → {'승인' if decision else '거절'}\n")
        return decision

    # --- 실제 텔레그램 인라인 버튼 발송 + long-polling 승인 대기 ---
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    payload = {
        "chat_id": chat_id,
        "text": report,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ 승인", "callback_data": "approve"},
                {"text": "❌ 거절", "callback_data": "reject"},
            ]]
        },
    }
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
    print("   [executor] 텔레그램 발송 완료 - 사용자 응답 대기 중…")
    return _poll_callback(token)


# ---------------------------------------------------------------------------
# 승인 후 액션: 노션 일정 등록 + 칸반 티켓 생성
# ---------------------------------------------------------------------------
def execute_actions(candidate: dict) -> None:
    """승인 콜백에서 호출되는 최종 자율 실행."""
    n = candidate["notice"]
    a = candidate["analysis"]

    if _is_demo():
        print("[6] 승인 - 캘린더 등록 (demo)")
        print(f"   [calendar] '{n.title} 준비' {a.estimated_hours_needed}h 캘린더 등록(demo)")
        print("   [info] 추천 상세(근거·체크리스트·초안)는 웹/백엔드에서 열람\n")
        return

    details = calendar_summary.build_event_details(candidate)
    created = _create_notion_schedule_block(n, a.estimated_hours_needed, details)
    if created:
        print("   [executor] 노션 캘린더에 일정 + 요약 설명 등록 완료")
    else:
        print("   [executor] 이미 등록된 일정으로 판단해 중복 생성 생략")


def _ensure_detail_properties(notion, database_id: str) -> set[str]:
    """캘린더 카드에서 바로 볼 수 있는 설명용 속성을 보강한다. 실패해도 본문 블록은 계속 쓴다."""
    optional = {
        "설명": {"rich_text": {}},
        "원문": {"url": {}},
        "적합도": {"number": {}},
        "도메인": {"rich_text": {}},
    }
    try:
        db = notion.databases.retrieve(database_id=database_id)
        existing = set((db.get("properties") or {}).keys())
        missing = {k: v for k, v in optional.items() if k not in existing}
        if missing:
            notion.databases.update(database_id=database_id, properties=missing)
            existing.update(missing.keys())
        return existing
    except Exception as e:
        print(f"   [notion-schedule] 설명 속성 확인 생략({e})")
        return set()


_WD = ["월", "화", "수", "목", "금", "토", "일"]


def _calendar_occupied(notion, db_id: str) -> dict:
    """캘린더의 기존 일정 점유 시간 {날짜: [(시작h,종료h)]}. 슬롯 충돌 방지용."""
    import datetime as dt
    occ: dict[str, list] = {}
    try:
        cursor = None
        while True:
            kw = {"database_id": db_id, "page_size": 100}
            if cursor:
                kw["start_cursor"] = cursor
            r = notion.databases.query(**kw)
            for p in r["results"]:
                d = p["properties"].get("날짜", {}).get("date")
                if not d or not d.get("start") or "T" not in d["start"]:
                    continue
                try:
                    sdt = dt.datetime.fromisoformat(d["start"].replace("Z", "+00:00")).replace(tzinfo=None)
                    e = d.get("end") or d["start"]
                    edt = dt.datetime.fromisoformat(e.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    continue
                occ.setdefault(sdt.date().isoformat(), []).append(
                    (sdt.hour + sdt.minute / 60, edt.hour + edt.minute / 60))
            if r.get("has_more"):
                cursor = r["next_cursor"]
            else:
                break
    except Exception as e:
        print(f"   [notion-schedule] 기존 일정 조회 생략({e})")
    return occ


def _schedule_exists(notion, db_id: str, title: str, details: dict, available_props: set[str]) -> bool:
    """같은 원문/제목으로 이미 등록된 캘린더 항목이 있으면 중복 생성을 막는다."""
    filters = []
    if "원문" in available_props and details.get("url"):
        filters.append({"property": "원문", "url": {"equals": details["url"]}})
    filters.append({"property": "일정명", "title": {"equals": f"{title} 준비"}})

    for flt in filters:
        try:
            resp = notion.databases.query(database_id=db_id, filter=flt, page_size=1)
            if resp.get("results"):
                return True
        except Exception as e:
            print(f"   [notion-schedule] 중복 확인 생략({e})")
            return False
    return False


def _find_slot(notice, hours: int, notion, db_id: str):
    """마감일 기준 날짜 + 시간표/기존일정과 안 겹치는 빈 슬롯을 찾아 (날짜, 시작h, 종료h) 반환."""
    import datetime as dt
    from modules import deadline as dlmod, timetable
    today = dt.date.today()
    dl = dlmod.deadline_date(notice)
    if dl and dl > today:
        target = dl - dt.timedelta(days=2)
        if target <= today:
            target = today + dt.timedelta(days=1)
    else:
        target = today + dt.timedelta(days=2)
    block = 2 if (hours or 0) <= 25 else 3

    tt_rows = timetable.load().get("rows", [])
    occ = _calendar_occupied(notion, db_id)

    def overlaps(s, e, busy):
        return any(s < be and bs < e for bs, be in busy)

    for off in range(0, 21):
        day = target + dt.timedelta(days=off)
        wd = _WD[day.weekday()]
        busy = [(float(r.get("start", 0)), float(r.get("end", 0)))
                for r in tt_rows if r.get("day") == wd]
        busy += occ.get(day.isoformat(), [])
        for start in (19, 18, 20, 21, 14, 16, 13, 15, 10):
            if start + block <= 23 and not overlaps(start, start + block, busy):
                return day, start, start + block
    return target, 19, 19 + block


def _create_notion_schedule_block(notice, hours: int, details: dict | None = None) -> bool:
    """승인된 활동을 Notion 캘린더에 추가.
    마감일 기준 + 빈 슬롯 탐색으로 날짜·시간을 분산 배치(한 날짜에 몰리지 않게)."""
    from notion_client import Client
    notion = Client(auth=os.getenv("NOTION_API_KEY"))
    # '내 캘린더'로 통일(가용시간 계산이 승인 활동도 반영). 없으면 옛 일정 DB.
    schedule_db = os.getenv("NOTION_CALENDAR_DB_ID") or os.getenv("NOTION_SCHEDULE_DB_ID")

    title = getattr(notice, "title", "활동")
    day, start_h, end_h = _find_slot(notice, hours, notion, schedule_db)
    start_iso = f"{day.isoformat()}T{start_h:02d}:00:00+09:00"
    end_iso = f"{day.isoformat()}T{end_h:02d}:00:00+09:00"
    print(f"   [notion-schedule] 배치: {day}({_WD[day.weekday()]}) {start_h}~{end_h}시")

    details = details or {}
    available_props = _ensure_detail_properties(notion, schedule_db)
    if _schedule_exists(notion, schedule_db, title, details, available_props):
        print(f"   [notion-schedule] '{title} 준비'가 이미 캘린더에 있어 중복 생성 생략")
        return False
    properties = {
        "일정명": {"title": [{"text": {"content": f"{title} 준비"}}]},
        "요일": {"select": {"name": _WD[day.weekday()]}},
        "시작": {"number": start_h},
        "종료": {"number": end_h},
        "날짜": {"date": {"start": start_iso, "end": end_iso}},
        "유형": {"select": {"name": "활동"}},
    }
    if "설명" in available_props and details.get("short_description"):
        properties["설명"] = {"rich_text": [{"text": {"content": details["short_description"][:1900]}}]}
    if "원문" in available_props and details.get("url"):
        properties["원문"] = {"url": details["url"]}
    if "적합도" in available_props and details.get("score"):
        properties["적합도"] = {"number": details["score"]}
    if "도메인" in available_props and details.get("domain"):
        properties["도메인"] = {"rich_text": [{"text": {"content": details["domain"][:1900]}}]}

    children = calendar_summary.to_notion_children(details) if details else []

    notion.pages.create(
        parent={"database_id": schedule_db},
        properties=properties,
        children=children,
    )
    print(f"   [notion-schedule] '{title} 준비' 일정 블록+설명 등록 완료 (캘린더 {day} 반영)")
    return True


# (칸반 관련 코드 제거 — 새 구조에서 추천 상세는 웹/백엔드 보관, 노션엔 캘린더만)
