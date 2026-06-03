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


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _build_report(candidate: dict) -> str:
    n = candidate["notice"]
    a = candidate["analysis"]
    v = candidate["validation"]
    draft_link = candidate.get("draft_link", "(초안 링크)")
    return textwrap.dedent(f"""
    📊 추천 활동 리포트
    ─────────────────────────
    제목   : {n.title}
    도메인 : {a.domain}
    적합도 : {a.suitability_score}/100
    소요시간: {a.estimated_hours_needed}시간 (역량 가중치 반영)
    일정   : 사용가능 {v['safe_free_hours']:.0f}h ≥ 필요 {v['needed_hours']}h  ✅
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

    # 노션에는 캘린더(준비 일정)만 기록. 추천 상세는 로컬 백엔드(store)에 보관.
    _create_notion_schedule_block(n.title, a.estimated_hours_needed)
    print("   [executor] 노션 캘린더에 '준비' 일정 등록 완료 (추천 상세는 웹에서 확인)")


def _create_notion_schedule_block(title: str, hours: int) -> None:
    """승인된 활동을 Notion 일정 DB(validator가 읽는 그 DB)에 추가.
    캘린더 뷰 표시를 위해 '날짜'(다음 토요일 10시~) 도 함께 기록."""
    import datetime as dt
    from notion_client import Client
    notion = Client(auth=os.getenv("NOTION_API_KEY"))
    # '내 캘린더'로 통일(가용시간 계산이 승인 활동도 반영). 없으면 옛 일정 DB.
    schedule_db = os.getenv("NOTION_CALENDAR_DB_ID") or os.getenv("NOTION_SCHEDULE_DB_ID")

    start_h, end_h = 10, 10 + min(hours, 4)
    today = dt.date.today()
    days_ahead = (5 - today.weekday()) % 7 or 7   # 다음 토요일(월=0…토=5)
    sat = today + dt.timedelta(days=days_ahead)
    start_iso = f"{sat.isoformat()}T{start_h:02d}:00:00+09:00"
    end_iso = f"{sat.isoformat()}T{end_h:02d}:00:00+09:00"

    notion.pages.create(
        parent={"database_id": schedule_db},
        properties={
            "일정명": {"title": [{"text": {"content": f"{title} 준비"}}]},
            "요일": {"select": {"name": "토"}},
            "시작": {"number": start_h},
            "종료": {"number": end_h},
            "날짜": {"date": {"start": start_iso, "end": end_iso}},
            "유형": {"select": {"name": "활동"}},
        },
    )
    print(f"   [notion-schedule] '{title} 준비' 일정 블록 등록 완료 (캘린더 {sat} 반영)")


# (칸반 관련 코드 제거 — 새 구조에서 추천 상세는 웹/백엔드 보관, 노션엔 캘린더만)
