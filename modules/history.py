"""modules/history.py
추천 이력 — 로컬 백엔드(store) 기반. (노션 아님)
중복 방지(is_new), 처리 이력 기록(record), 검토대기 조회(list_pending), 상태갱신(mark).
"""
from __future__ import annotations

import os

from modules import store


PROTECTED_STATUSES = {"승인", "거절"}


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


def index_by_url() -> dict[str, dict]:
    """현재 추천 이력을 URL 기준으로 한 번에 읽어 증분 분석에 사용."""
    if not enabled():
        return {}
    return {row["url"]: row for row in store.list_recs() if row.get("url")}


def _norm_text(value: str | None, limit: int | None = None) -> str:
    text = " ".join((value or "").split())
    return text[:limit] if limit else text


def needs_analysis(notice, existing: dict | None) -> tuple[bool, str]:
    """신규/변경분만 분석하기 위한 비교.

    같은 URL이어도 제목, 본문, 마감일, 분야가 바뀌었거나 과거 분석 결과가 비어 있으면
    다시 분석한다. 승인/거절한 항목은 사용자의 명시적 피드백이므로 다시 살리지 않는다.
    """
    if not enabled() or not existing:
        return True, "신규 공지"

    status = existing.get("status")
    if status in PROTECTED_STATUSES:
        return False, f"이미 {status}한 공지"

    title_changed = _norm_text(existing.get("title"), 300) != _norm_text(getattr(notice, "title", ""), 300)
    body_changed = _norm_text(existing.get("body"), 5000) != _norm_text(getattr(notice, "body", ""), 5000)
    deadline_changed = _norm_date(existing.get("deadline", "")) != _norm_date(getattr(notice, "date", ""))
    category_changed = (existing.get("category") or "기타") != (getattr(notice, "category", "") or "기타")
    missing_analysis = not any(existing.get(k) for k in ("summary", "reason", "score", "hours", "domain"))

    changes = []
    if title_changed:
        changes.append("제목")
    if body_changed:
        changes.append("본문")
    if deadline_changed:
        changes.append("마감일")
    if category_changed:
        changes.append("분야")
    if missing_analysis:
        changes.append("분석결과 없음")

    if changes:
        return True, "변경 감지: " + ", ".join(changes)
    return False, "기존 분석 유지"


def status(url: str) -> str | None:
    if not enabled():
        return None
    return store.get_rec_status(url)


def record(notice, analysis=None, status="수집됨", category=None) -> None:
    if not enabled():
        return
    store.add_rec({
        "url": notice.url,
        "title": (notice.title or "")[:300],
        "category": category or getattr(notice, "category", "") or "기타",
        "source": getattr(notice, "source", ""),
        "body": (getattr(notice, "body", "") or "")[:5000],
        "score": int(analysis.suitability_score) if analysis else None,
        "hours": int(analysis.estimated_hours_needed) if analysis else None,
        "deadline": _norm_date(getattr(notice, "date", "")),
        "reason": (getattr(analysis, "matching_reason", "") if analysis else "") or "",
        "summary": (getattr(analysis, "summary", "") if analysis else "") or "",
        "domain": (getattr(analysis, "domain", "") if analysis else "") or "",
        "status": status,
    })


# 정보성 분야: 점수와 무관하게 '추천할 내용이 있으면' 전부 노출
INFO_CATEGORIES = {"장학금", "학사일정"}


def _to_web(r: dict) -> dict:
    return {
        "page_id": r["url"],  # 웹 버튼 키로 url 사용
        "title": r["title"], "url": r["url"],
        "category": r["category"] or "기타", "source": r["source"] or "",
        "score": r["score"] or 0, "hours": r["hours"] or 0,
        "deadline": r["deadline"] or "", "reason": r["reason"] or "", "domain": r["domain"] or "",
        "body": r.get("body") or "", "summary": r.get("summary") or "",
    }


def list_pending() -> list:
    """검토 대기 추천 — 분야별 1순위(점수 최고)만. 단:
       - 장학금/학사일정(정보성): 있는 만큼 전부
       - 주기='끄기' 분야: 제외
    무시(거절)/승인된 항목은 store 상태가 바뀌어 자동 제외되므로,
    1순위를 무시하면 새로고침 시 다음 순위가 올라온다.
    """
    if not enabled():
        return []
    prefs = store.get_prefs()
    off = {c for c, p in prefs.items() if p.get("주기") == "끄기"}

    by_cat: dict[str, list] = {}
    for r in store.list_recs("추천완료"):   # 점수 내림차순
        cat = r["category"] or "기타"
        if cat in off:
            continue
        by_cat.setdefault(cat, []).append(r)

    out = []
    for cat, items in by_cat.items():
        chosen = items if cat in INFO_CATEGORIES else items[:1]
        out.extend(_to_web(r) for r in chosen)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def mark(url: str, status: str) -> None:
    if not enabled():
        return
    store.set_rec_status(url, status)


def reset_pending() -> int:
    """재분석 전 기존 검토대기 항목을 보류로 내린다. 승인/거절 이력은 보존."""
    if not enabled():
        return 0
    return store.set_status_where("추천완료", "수집됨")
