"""modules/digest.py
Digest 에이전트 — 채널 인식.
- 채널=웹  : 추천을 백엔드에 남겨두고 웹 '추천 리뷰'에서 검토 (별도 발송 없음)
- 채널=텔레그램 : 추천을 버튼([✅노션추가]/[❌무시])과 함께 발송 → 승인 시 노션 캘린더 등록
전달 대상 분야는 Preferences 주기(매일/매주)로 결정.
"""
from __future__ import annotations

import os
import time

from modules import preferences, classifier

MAX_TELEGRAM = 8          # 한 번에 보낼 텔레그램 추천 상한
POLL_SECONDS = 300        # 버튼 응답 대기(초)


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _tg(token: str, method: str, payload: dict):
    import requests
    return requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)


def _send_card(token: str, chat_id: str, c: dict, idx: int):
    a, n = c["analysis"], c["notice"]
    dl = f"~{n.date} " if getattr(n, "date", "") else ""
    text = (f"📌 [{c['category']}] {n.title}\n"
            f"적합도 {a.suitability_score}/100 · {dl}{a.estimated_hours_needed}h\n"
            f"{(a.matching_reason or '')[:180]}\n{n.url}")
    kb = {"inline_keyboard": [[
        {"text": "✅ 노션에 추가", "callback_data": f"ap:{idx}"},
        {"text": "❌ 무시", "callback_data": f"rj:{idx}"}]]}
    _tg(token, "sendMessage", {"chat_id": chat_id, "text": text,
                               "disable_web_page_preview": True, "reply_markup": kb})


def _group_due(candidates: list, due: set, prefs: dict, channel: str) -> list:
    out = [c for c in candidates
           if c["category"] in due and prefs.get(c["category"], {}).get("채널") == channel]
    out.sort(key=lambda c: c.get("rank_score", c["analysis"].suitability_score), reverse=True)
    return out


def deliver(candidates: list, ctx: dict) -> None:
    """오늘 전달 대상 추천을 채널별로 처리."""
    from modules import executor, history
    due = preferences.due_categories()
    prefs = preferences.load_preferences()
    print(f"   [digest] 오늘 전달 분야: {sorted(due) or '없음'}")

    web = _group_due(candidates, due, prefs, "웹")
    tg = _group_due(candidates, due, prefs, "텔레그램")[:MAX_TELEGRAM]
    print(f"   [digest] 웹 검토 {len(web)}건 · 텔레그램 발송 {len(tg)}건")

    if not tg or _is_demo():
        if tg:
            print("   [digest] (demo) 텔레그램 발송 생략")
        return

    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("   [digest] 텔레그램 미설정 → 발송 생략")
        return

    # 과거 업데이트 비우기
    last = _tg(token, "getUpdates", {"timeout": 0}).json().get("result", [])
    offset = (last[-1]["update_id"] + 1) if last else 0

    for i, c in enumerate(tg):
        _send_card(token, chat, c, i)
    print(f"   [digest] {len(tg)}건 발송 — 버튼 응답 대기(최대 {POLL_SECONDS // 60}분)")

    deadline = time.time() + POLL_SECONDS
    done = set()
    while time.time() < deadline and len(done) < len(tg):
        try:
            r = _tg(token, "getUpdates", {"offset": offset, "timeout": 25,
                                          "allowed_updates": ["callback_query"]}).json()
        except Exception:
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if not cb:
                continue
            _tg(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
            data = cb.get("data", "")
            if ":" not in data:
                continue
            act, idx = data.split(":", 1)
            idx = int(idx) if idx.isdigit() else -1
            if idx < 0 or idx >= len(tg) or idx in done:
                continue
            c = tg[idx]
            url = c["notice"].url
            if act == "ap":
                executor.execute_actions(c)          # 노션 캘린더 등록
                history.mark(url, "승인")
                print(f"   [digest] 승인→노션 캘린더: {c['notice'].title[:30]}")
            elif act == "rj":
                history.mark(url, "거절")
                print(f"   [digest] 무시: {c['notice'].title[:30]}")
            done.add(idx)
    print(f"   [digest] 처리 완료 {len(done)}/{len(tg)}건")
