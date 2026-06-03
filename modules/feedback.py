"""modules/feedback.py
Feedback 학습 — 로컬 백엔드(store) 기반. (노션 아님)
추천 이력의 승인/거절을 분야별로 집계해 다음 추천 점수를 ±MAX_ADJ 보정한다.
"""
from __future__ import annotations

import os

MAX_ADJ = 15
PER_NET = 5   # (승인-거절) 1건당 점수


def _enabled() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() != "true"


def category_adjustments() -> dict:
    """{분야: 점수보정}. 신호 없으면 빈 dict."""
    if not _enabled():
        return {}
    from modules import store
    net: dict[str, int] = {}
    for r in store.list_recs():
        if r["status"] in ("승인", "거절"):
            cat = r["category"]
            if cat:
                net[cat] = net.get(cat, 0) + (1 if r["status"] == "승인" else -1)
    return {c: max(-MAX_ADJ, min(MAX_ADJ, v * PER_NET)) for c, v in net.items() if v}


def summary(adj: dict) -> str:
    if not adj:
        return "학습 신호 없음(승인/거절 이력 부족)"
    return ", ".join(f"{c} {'+' if v > 0 else ''}{v}" for c, v in adj.items())
