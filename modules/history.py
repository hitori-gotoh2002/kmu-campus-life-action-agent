"""modules/history.py
추천 이력 — 로컬 백엔드(store) 기반. (노션 아님)
중복 방지(is_new), 처리 이력 기록(record), 검토대기 조회(list_pending), 상태갱신(mark).
"""
from __future__ import annotations

import os

from modules import store


def enabled() -> bool:
    """데모가 아니면 항상 사용(로컬 저장소)."""
    return os.getenv("DEMO_MODE", "true").lower() != "true"


def _norm_date(s: str) -> str:
    if not s:
        return ""
    s = s.strip().replace(".", "-").rstrip("-")
    parts = s.split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        try:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except ValueError:
            return ""
    return ""


def is_new(notice) -> bool:
    if not enabled():
        return True
    return not store.rec_exists(notice.url)


def record(notice, analysis=None, status="수집됨", category=None) -> None:
    if not enabled():
        return
    store.add_rec({
        "url": notice.url,
        "title": (notice.title or "")[:300],
        "category": category or getattr(notice, "category", "") or "기타",
        "source": getattr(notice, "source", ""),
        "score": int(analysis.suitability_score) if analysis else None,
        "hours": int(analysis.estimated_hours_needed) if analysis else None,
        "deadline": _norm_date(getattr(notice, "date", "")),
        "reason": (getattr(analysis, "matching_reason", "") if analysis else "") or "",
        "domain": (getattr(analysis, "domain", "") if analysis else "") or "",
        "status": status,
    })


def list_pending() -> list:
    """상태='추천완료'(검토 대기) 추천 목록 (웹 리뷰용)."""
    if not enabled():
        return []
    out = []
    for r in store.list_recs("추천완료"):
        out.append({
            "page_id": r["url"],  # 웹 버튼 키로 url 사용
            "title": r["title"], "url": r["url"],
            "category": r["category"] or "기타", "source": r["source"] or "",
            "score": r["score"] or 0, "hours": r["hours"] or 0,
            "deadline": r["deadline"] or "", "reason": r["reason"] or "", "domain": r["domain"] or "",
        })
    return out


def mark(url: str, status: str) -> None:
    if not enabled():
        return
    store.set_rec_status(url, status)
