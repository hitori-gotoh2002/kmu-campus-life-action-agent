"""modules/feedback.py
Feedback 학습 에이전트.
이력 DB의 승인/거절 기록을 분야별로 집계해, 다음 추천 점수를 개인화 보정한다.
- 많이 '승인'한 분야 → 가점 / 많이 '거절'한 분야 → 감점
- 보정폭은 ±MAX_ADJ 로 제한 (과보정 방지)
"""
from __future__ import annotations

import os

MAX_ADJ = 15
PER_NET = 5   # (승인-거절) 1건당 점수


def _client():
    from notion_client import Client
    return Client(auth=os.getenv("NOTION_API_KEY"))


def _enabled() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() != "true" and bool(os.getenv("NOTION_HISTORY_DB_ID"))


def category_adjustments() -> dict:
    """{분야: 점수보정} 반환. 신호 없으면 빈 dict."""
    if not _enabled():
        return {}
    db = os.getenv("NOTION_HISTORY_DB_ID")
    net: dict[str, int] = {}
    try:
        for status, sign in (("승인", +1), ("거절", -1)):
            r = _client().databases.query(
                database_id=db,
                filter={"property": "상태", "select": {"equals": status}},
                page_size=100)
            for p in r["results"]:
                sel = p["properties"].get("카테고리", {}).get("select")
                cat = sel["name"] if sel else None
                if cat:
                    net[cat] = net.get(cat, 0) + sign
    except Exception as e:
        print(f"   [feedback] 이력 집계 실패({e})")
        return {}
    return {c: max(-MAX_ADJ, min(MAX_ADJ, v * PER_NET)) for c, v in net.items() if v}


def summary(adj: dict) -> str:
    if not adj:
        return "학습 신호 없음(승인/거절 이력 부족)"
    return ", ".join(f"{c} {'+' if v > 0 else ''}{v}" for c, v in adj.items())
