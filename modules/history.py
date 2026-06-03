"""modules/history.py
History Store 에이전트 (노션 '커리어 추천 이력' DB).
- is_new(notice): 이미 처리한 공지인지 URL 로 판별 (중복 추천 방지)
- record(notice, analysis, status, category): 처리 이력 저장
- mark(url, status): 상태 갱신(승인/거절 등)
"""
from __future__ import annotations

import os


def _client():
    from notion_client import Client
    return Client(auth=os.getenv("NOTION_API_KEY"))


def _db() -> str | None:
    return os.getenv("NOTION_HISTORY_DB_ID")


def enabled() -> bool:
    """이력 DB 사용 가능 여부(데모/미설정이면 False)."""
    return os.getenv("DEMO_MODE", "true").lower() != "true" and bool(_db())


def _norm_date(s: str) -> str | None:
    """'2026.06.02' / '2026-06-21' → 'YYYY-MM-DD'. 실패 시 None."""
    if not s:
        return None
    s = s.strip().replace(".", "-").rstrip("-")
    parts = s.split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        try:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except ValueError:
            return None
    return None


def is_new(notice) -> bool:
    """이력 DB에 같은 URL이 없으면 신규(True)."""
    if not enabled():
        return True
    try:
        r = _client().databases.query(
            database_id=_db(),
            filter={"property": "URL", "url": {"equals": notice.url}},
            page_size=1,
        )
        return len(r["results"]) == 0
    except Exception as e:
        print(f"   [history] 신규조회 실패({e}) → 신규로 간주")
        return True


def record(notice, analysis=None, status="수집됨", category=None) -> None:
    """처리한 공지를 이력 DB에 기록."""
    if not enabled():
        return
    props = {
        "제목": {"title": [{"text": {"content": (notice.title or "")[:200]}}]},
        "URL": {"url": notice.url},
        "상태": {"select": {"name": status}},
    }
    src = getattr(notice, "source", "")
    if src:
        props["출처"] = {"select": {"name": src}}
    cat = category or getattr(notice, "category", None)
    if cat:
        props["카테고리"] = {"select": {"name": cat}}
    if analysis is not None:
        props["적합도"] = {"number": int(analysis.suitability_score)}
        props["소요시간"] = {"number": int(analysis.estimated_hours_needed)}
        reason = getattr(analysis, "matching_reason", "") or ""
        if reason:
            props["근거"] = {"rich_text": [{"text": {"content": reason[:1900]}}]}
        dom = getattr(analysis, "domain", "") or ""
        if dom:
            props["도메인"] = {"rich_text": [{"text": {"content": dom}}]}
    dl = _norm_date(getattr(notice, "date", ""))
    if dl:
        props["마감일"] = {"date": {"start": dl}}
    try:
        _client().pages.create(parent={"database_id": _db()}, properties=props)
    except Exception as e:
        print(f"   [history] 기록 실패({e})")


def _txt(props, name):
    p = props.get(name, {})
    if p.get("type") == "rich_text":
        rt = p.get("rich_text", [])
        return rt[0]["plain_text"] if rt else ""
    if p.get("type") == "title":
        t = p.get("title", [])
        return t[0]["plain_text"] if t else ""
    return ""


def _sel(props, name):
    s = props.get(name, {}).get("select")
    return s["name"] if s else ""


def list_pending() -> list:
    """상태='추천완료'(검토 대기) 행을 적합도순으로 반환 (웹 리뷰 UI용)."""
    if not enabled():
        return []
    try:
        r = _client().databases.query(
            database_id=_db(),
            filter={"property": "상태", "select": {"equals": "추천완료"}},
            page_size=100,
        )
    except Exception as e:
        print(f"   [history] 검토목록 조회 실패({e})")
        return []
    out = []
    for p in r["results"]:
        pr = p["properties"]
        d = pr.get("마감일", {}).get("date")
        out.append({
            "page_id": p["id"],
            "title": _txt(pr, "제목"),
            "url": pr.get("URL", {}).get("url") or "",
            "category": _sel(pr, "카테고리") or "기타",
            "source": _sel(pr, "출처"),
            "score": pr.get("적합도", {}).get("number") or 0,
            "hours": pr.get("소요시간", {}).get("number") or 0,
            "deadline": d["start"] if d else "",
            "reason": _txt(pr, "근거"),
            "domain": _txt(pr, "도메인"),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def mark(url: str, status: str) -> None:
    """URL로 행을 찾아 상태 갱신(예: 승인/거절)."""
    if not enabled():
        return
    try:
        r = _client().databases.query(
            database_id=_db(),
            filter={"property": "URL", "url": {"equals": url}}, page_size=1)
        if r["results"]:
            _client().pages.update(page_id=r["results"][0]["id"],
                                   properties={"상태": {"select": {"name": status}}})
    except Exception as e:
        print(f"   [history] 상태갱신 실패({e})")
