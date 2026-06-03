"""modules/digest.py
Digest 에이전트.
후보를 분야별로 묶어 '오늘의 추천서'를 구성하고, 자동 전달 대상(Preferences)만
텔레그램으로 발송한다. (per-item 노션 반영은 Phase 3 웹 UI 담당)
"""
from __future__ import annotations

import datetime as dt
import os

from modules import preferences, classifier

MAX_PER_CATEGORY = 3   # 분야별 상위 N건


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _group(candidates: list) -> dict:
    by_cat: dict[str, list] = {}
    for c in candidates:
        by_cat.setdefault(c["category"], []).append(c)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda c: c["analysis"].suitability_score, reverse=True)
    return by_cat


def build_digest(candidates: list, ctx: dict, due: set[str]) -> str | None:
    """due 분야 중 후보가 있는 것만 추천서 텍스트로. 없으면 None."""
    by_cat = _group(candidates)
    sections = []
    for cat in classifier.CATEGORIES:
        if cat not in due or cat not in by_cat:
            continue
        items = by_cat[cat][:MAX_PER_CATEGORY]
        lines = [f"■ {cat} ({len(by_cat[cat])}건)"]
        for i, c in enumerate(items, 1):
            a, n = c["analysis"], c["notice"]
            dl = f"~{n.date} " if n.date else ""
            lines.append(f" {i}. [{a.suitability_score}점] {n.title}")
            lines.append(f"    {dl}{a.estimated_hours_needed}h · {n.url}")
        sections.append("\n".join(lines))

    if not sections:
        return None

    today = dt.date.today().isoformat()
    header = (f"📬 오늘의 맞춤 추천서 ({today})\n"
              f"대상: {ctx.get('name','')} · {ctx.get('desired_role','')}\n"
              f"전달 분야: {', '.join(sorted(due))}\n"
              + "─" * 22)
    footer = "─" * 22 + "\n노션 반영은 웹 리뷰에서, 또는 개별 승인으로 진행하세요."
    return header + "\n\n" + "\n\n".join(sections) + "\n\n" + footer


def _send_telegram(text: str) -> bool:
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("   [digest] 텔레그램 미설정 → 발송 생략")
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text,
                            "disable_web_page_preview": True}, timeout=10)
    ok = r.ok and r.json().get("ok")
    print("   [digest] 텔레그램 추천서 발송 " + ("완료" if ok else f"실패: {r.text[:120]}"))
    return bool(ok)


def deliver(candidates: list, ctx: dict) -> None:
    """오늘 전달 대상 분야의 추천서를 구성·발송."""
    due = preferences.due_categories()
    print(f"   [digest] 오늘 자동 전달 분야: {sorted(due) or '없음'}")
    text = build_digest(candidates, ctx, due)
    if not text:
        print("   [digest] 오늘 전달할 신규 추천이 없습니다.")
        return
    print("\n----- 추천서 미리보기 -----")
    print(text)
    print("---------------------------\n")
    if not _is_demo():
        _send_telegram(text)
