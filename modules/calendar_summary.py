"""modules/calendar_summary.py
노션 캘린더 설명 에이전트.

승인된 추천 활동을 캘린더에 넣을 때, 제목만 남기지 않고
사용자가 바로 이해할 수 있는 요약/추천 이유/준비 체크리스트를 만든다.
LLM 분석 결과를 재사용하므로 캘린더 등록 단계에서 추가 API 비용이 들지 않는다.
"""
from __future__ import annotations

import re
from typing import Any


def _get(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clip(text: Any, limit: int) -> str:
    s = _clean(text)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def build_event_details(candidate: dict) -> dict:
    """추천 후보를 노션 캘린더 페이지에 넣을 설명 구조로 변환한다."""
    notice = candidate["notice"]
    analysis = candidate["analysis"]

    title = _clean(_get(notice, "title"))
    body = _clean(_get(notice, "body"))
    reason = _clean(_get(analysis, "matching_reason"))
    hours = _safe_int(_get(analysis, "estimated_hours_needed"))
    score = _safe_int(_get(analysis, "suitability_score"))
    category = _clean(candidate.get("category") or _get(notice, "category") or "기타")
    domain = _clean(_get(analysis, "domain"))
    source = _clean(_get(notice, "source"))
    deadline = _clean(_get(notice, "date") or candidate.get("deadline", ""))
    url = _clean(_get(notice, "url"))

    summary_source = body or reason or f"{title} 관련 추천 활동입니다."
    summary = _clip(summary_source, 420)
    short_description = _clip(
        f"[{category}] {summary}"
        + (f" 추천 근거: {reason}" if reason else ""),
        900,
    )

    checklist = [
        "원문 공지에서 신청 자격, 제출물, 세부 마감 시간을 확인합니다.",
        "필요한 제출물과 참고 링크를 이 캘린더 페이지 안에 정리합니다.",
    ]
    if hours:
        checklist.insert(1, f"예상 준비 시간 {hours}시간을 2~4회 작업 블록으로 나누어 진행합니다.")
    if deadline:
        checklist.append(f"마감일({deadline}) 전날까지 최종 제출 상태를 확인합니다.")

    meta = []
    if source:
        meta.append(f"출처: {source}")
    if category:
        meta.append(f"분류: {category}")
    if domain:
        meta.append(f"도메인: {domain}")
    if score:
        meta.append(f"적합도: {score}/100")
    if hours:
        meta.append(f"예상 준비: {hours}시간")
    if deadline:
        meta.append(f"마감: {deadline}")

    return {
        "title": title,
        "summary": summary,
        "short_description": short_description,
        "reason": _clip(reason, 800),
        "checklist": checklist,
        "meta": meta,
        "url": url,
        "score": score,
        "domain": domain,
        "category": category,
    }


def _text(content: str, link: str | None = None) -> dict:
    text = {"content": _clip(content, 1800)}
    if link:
        text["link"] = {"url": link}
    return {"type": "text", "text": text}


def _paragraph(content: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [_text(content)]}}


def _heading(content: str) -> dict:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [_text(content)]}}


def _bullet(content: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_text(content)]},
    }


def to_notion_children(details: dict) -> list[dict]:
    """Notion pages.create(children=...)에 넣을 블록 목록을 만든다."""
    blocks: list[dict] = []
    blocks.append(_heading("활동 요약"))
    blocks.append(_paragraph(details["summary"]))

    if details.get("meta"):
        blocks.append(_paragraph(" · ".join(details["meta"])))

    if details.get("reason"):
        blocks.append(_heading("추천 이유"))
        blocks.append(_paragraph(details["reason"]))

    if details.get("checklist"):
        blocks.append(_heading("준비 체크리스트"))
        blocks.extend(_bullet(item) for item in details["checklist"])

    if details.get("url"):
        blocks.append(_heading("원문 링크"))
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [_text("공지 원문 열기", details["url"])]},
        })

    return blocks[:20]
