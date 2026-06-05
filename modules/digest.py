"""modules/digest.py
Digest 에이전트 — 채널 인식 + 분야별 1순위.
- 채널=웹      : 추천을 백엔드에 남겨 웹 '내 맞춤 추천함'에서 누적 검토
- 채널=텔레그램 : 분야별 1순위 추천을 버튼과 함께 발송 → 승인 시 노션 캘린더 등록
  · 장학금/학사일정(정보성): 점수 무관, 있는 만큼 전부
  · 텔레그램 분야인데 추천이 없으면 '추천 없음' 안내 메시지
승인/거절(버튼) 처리는 비동기 리스너(telegram_callbacks.poll_once, scheduler가 20초마다)가 담당.
자동 업데이트/전달 대상 분야는 Preferences 주기(매일/매주)로 결정.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from modules import preferences, calendar_summary, telegram_callbacks, history

MAX_TELEGRAM = 8
LOG_PATH = Path("data/scheduler.log")


def _log(message: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _tg(token: str, method: str, payload: dict):
    import requests
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not response.ok or data.get("ok") is False:
        description = data.get("description") or response.text[:200]
        _log(f"[telegram] {method} 실패 status={response.status_code} desc={description}")
    return response


def _rank(c: dict):
    return c.get("rank_score", c["analysis"].suitability_score)


def _send_card(token: str, chat_id: str, c: dict):
    d = calendar_summary.build_event_details(c)
    key = telegram_callbacks.callback_key(c["notice"].url)
    lines = [f"📌 [{d['category']}] {d['title']}"]
    if d["meta"]:
        lines.append(" · ".join(d["meta"]))
    if d["summary"]:
        lines.append(calendar_summary.summary_to_plain_text(d["summary"]))
    if d["reason"]:
        lines.append(f"🧭 추천 이유: {d['reason'][:200]}")
    if d["checklist"]:
        lines.append("✅ 준비: " + " / ".join(d["checklist"][:2]))
    if d["url"]:
        lines.append(d["url"])
    text = "\n".join(lines)[:3800]
    kb = {"inline_keyboard": [[
        {"text": "✅ 노션에 추가", "callback_data": f"ap:{key}"},
        {"text": "❌ 무시", "callback_data": f"rj:{key}"}]]}
    _tg(token, "sendMessage", {"chat_id": chat_id, "text": text,
                               "disable_web_page_preview": True, "reply_markup": kb})


def _top_per_category(cands: list) -> list:
    """분야별 1순위(정보성 분야는 전부)."""
    by_cat: dict[str, list] = {}
    for c in cands:
        by_cat.setdefault(c["category"], []).append(c)
    out = []
    for cat, items in by_cat.items():
        items.sort(key=_rank, reverse=True)
        out.extend(items if cat in history.INFO_CATEGORIES else items[:1])
    out.sort(key=_rank, reverse=True)
    return out


def deliver(candidates: list, ctx: dict) -> None:
    """오늘 전달 대상 추천을 채널별로 처리(발송만; 승인은 비동기 리스너)."""
    due = preferences.due_categories()
    prefs = preferences.load_preferences()
    tg_cats = {cat for cat in due if prefs.get(cat, {}).get("채널") == "텔레그램"}

    tg_cands = [c for c in candidates if c["category"] in tg_cats]
    to_send = _top_per_category(tg_cands)[:MAX_TELEGRAM]
    have = {c["category"] for c in tg_cands}
    empty = sorted(cat for cat in tg_cats
                   if cat not in have and cat not in history.INFO_CATEGORIES)

    print(f"   [digest] 텔레그램 발송 {len(to_send)}건 · 빈 분야 {len(empty)}건")
    _log(f"[digest] 텔레그램 발송 대상 {len(to_send)}건 · 빈 분야 {len(empty)}건 · 분야={', '.join(sorted(tg_cats)) or '없음'}")

    if _is_demo():
        _log("[digest] DEMO_MODE=true - 텔레그램 발송 생략")
        return
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("   [digest] 텔레그램 미설정 → 발송 생략")
        _log("[digest] 텔레그램 미설정 - 발송 생략")
        return

    for c in to_send:
        _send_card(token, chat, c)
    if empty:
        _tg(token, "sendMessage", {"chat_id": chat,
            "text": "📭 오늘 " + ", ".join(empty) + " 분야엔 새 추천이 없습니다."})
    print("   [digest] 발송 완료 - 승인/거절은 리스너(scheduler)가 처리")
    _log("[digest] 텔레그램 발송 완료")
