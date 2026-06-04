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


# 크롤링 본문에서 흔한 노이즈(작성자/조회수/이전글 등) 제거
_BODY_NOISE = re.compile(
    r"(작성자|작성일|조회수|첨부파일|이전\s*글|다음\s*글|목록|등록일|담당자|담당부서|"
    r"붙임\d*|☎|조회\s*\d+|\d{2,4}[.\-]\d{1,2}[.\-]\d{1,2})"
)

# 분야별 맞춤 준비 체크리스트
_CHECKLISTS = {
    "공모전·대회": ["모집요강에서 주제·제출물·심사기준 확인", "팀 구성 또는 개인 참가 결정",
                "아이디어·분석 초안 작성", "제출물 점검 후 마감 전 제출"],
    "대외활동·서포터즈": ["모집요강·활동기간·혜택 확인", "지원서/자기소개 항목 작성",
                  "활동 가능 일정 점검", "마감 전 지원서 제출"],
    "장학금": ["신청 자격요건(성적·소득 등) 충족 여부 확인", "필요 서류 준비",
            "신청서 작성", "마감 전 제출"],
    "자격증": ["시험 일정·접수기간 확인", "교재/강의 학습 계획 수립",
            "기출·모의고사 풀이", "접수 및 응시"],
    "채용·인턴": ["모집부문·자격요건 확인", "이력서/자기소개서 작성",
              "포트폴리오 정리", "마감 전 지원"],
    "학사일정": ["신청/처리 기한 확인", "필요 절차·서류 준비", "기한 내 처리"],
}
_DEFAULT_CHECK = ["공지 원문에서 자격·제출물·마감 확인", "필요 서류/준비물 정리", "마감 전 신청·제출"]

_KIND = {
    "공모전·대회": "공모전/대회", "대외활동·서포터즈": "대외활동·서포터즈", "장학금": "장학금",
    "자격증": "자격증·교육", "채용·인턴": "인턴/채용형", "학사일정": "학사 일정", "기타": "추천 활동",
}


def _clean_body(body: str) -> str:
    """크롤링 본문에서 노이즈 토큰을 걷어내고 의미 있는 앞부분만 남긴다."""
    s = _clean(body)
    if not s:
        return ""
    # 노이즈가 처음 나오는 지점 이전까지만 사용
    m = _BODY_NOISE.search(s)
    if m and m.start() > 25:
        s = s[: m.start()]
    s = _BODY_NOISE.sub(" ", s)
    return _clean(s)


def _split_lines(text: str) -> list[str]:
    """줄바꿈으로 구분된 키워드 항목을 글머리표 목록으로 분해한다."""
    out = []
    for ln in str(text or "").replace("\r", "\n").split("\n"):
        ln = re.sub(r"[ \t]+", " ", ln).strip().lstrip("-•*‣◦·").strip()
        if ln:
            out.append(ln)
    return out


def _split_sentences(text: str, limit: int = 4) -> list[str]:
    """줄바꿈이 없는 산문 요약을 읽기 좋은 문장 단위로 끊는다."""
    s = _clean(text)
    if not s:
        return []
    raw = re.split(r"(?<=다)\.\s+|(?<=요)\.\s+|(?<=음)\.\s+|[.!?]\s+", s)
    return [p.strip().strip("·").strip() for p in raw if p.strip()][:limit]


def _keyword_points(content: str, *, category: str, deadline: str,
                    hours: int, source: str, domain: str, title: str) -> list[str]:
    """활동 내용을 키워드 중심 3줄 이상으로 정리한다.
    - LLM summary(줄바꿈 키워드)면 그대로 글머리표로.
    - 산문이면 문장 단위로 분해.
    - 그래도 3줄이 안 되면 분야/마감/시간/출처/도메인 같은 사실로 보강."""
    points = _split_lines(content)
    if len(points) <= 1:  # 줄바꿈이 없으면(과거 산문 요약/본문) 문장으로 분해
        sent = _split_sentences(content)
        if len(sent) > len(points):
            points = sent

    if len(points) < 3:
        facts = [f"분야: {category}"]
        if deadline:
            facts.append(f"신청 마감: {deadline}")
        if hours:
            facts.append(f"예상 준비: {hours}시간")
        if source:
            facts.append(f"출처: {source}")
        if domain:
            facts.append(f"관련 도메인: {domain}")
        for f in facts:
            if f not in points:
                points.append(f)

    seen, uniq = set(), []
    for p in points:
        p = _clip(p, 200)
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq[:12] or [f"'{title}' 공고 — 원문에서 모집 대상·일정·혜택을 확인하세요."]


def build_event_details(candidate: dict) -> dict:
    """추천 후보를 사용자에게 도움이 되는 설명 구조로 변환한다."""
    notice = candidate["notice"]
    analysis = candidate["analysis"]

    title = _clean(_get(notice, "title"))
    # 활동 '내용 요약': LLM이 만든 summary(키워드 줄바꿈)를 우선 사용 — 줄바꿈을 보존해야
    # 키워드별 글머리표로 나눌 수 있으므로 _clean(공백 평탄화)을 적용하지 않는다.
    content = str(_get(analysis, "summary") or "").strip() or _clean_body(_get(notice, "body"))
    reason = _clean(_get(analysis, "matching_reason"))
    hours = _safe_int(_get(analysis, "estimated_hours_needed"))
    score = _safe_int(_get(analysis, "suitability_score"))
    category = _clean(candidate.get("category") or _get(notice, "category") or "기타")
    domain = _clean(_get(analysis, "domain"))
    source = _clean(_get(notice, "source"))
    deadline = _clean(_get(notice, "date") or candidate.get("deadline", ""))
    url = _clean(_get(notice, "url"))
    kind = _KIND.get(category, "추천 활동")

    # 핵심 정보(마감/시간/적합도) — 본문 요약과 별개로 한 줄에 모은다.
    facts = []
    if deadline:
        facts.append(f"마감 {deadline}")
    if hours:
        facts.append(f"예상 준비 {hours}시간")
    if score:
        facts.append(f"내 진로 적합도 {score}/100")

    # 키워드 중심 3줄 이상의 '내용 요약' 글머리표(웹/노션 공통 단일 소스).
    summary_points = _keyword_points(
        content, category=category, deadline=deadline, hours=hours,
        source=source, domain=domain, title=title,
    )
    summary = "\n".join(summary_points)

    # 노션 '설명' 속성에 들어갈 한 줄(간결). 추천 이유는 본문 블록으로 분리하므로 제외.
    short_description = _clip(" · ".join(summary_points), 300)

    checklist = list(_CHECKLISTS.get(category, _DEFAULT_CHECK))
    if deadline:
        checklist.append(f"마감일({deadline}) 전날까지 최종 점검·제출 상태 확인")

    meta = list(facts)
    if domain:
        meta.append(f"도메인 {domain}")
    if source:
        meta.append(f"출처 {source}")

    return {
        "title": title,
        "content": content,
        "summary": summary,
        "summary_points": summary_points,
        "short_description": short_description,
        "reason": _clip(reason, 800),
        "checklist": checklist,
        "facts": facts,
        "meta": meta,
        "url": url,
        "score": score,
        "domain": domain,
        "category": category,
        "hours": hours,
        "deadline": deadline,
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


def split_kv(point: str) -> tuple[str, str]:
    """'항목: 내용' 한 줄을 (항목, 내용)으로 분리. 콜론이 없으면 항목은 빈 문자열."""
    s = str(point or "").strip()
    if ":" in s:
        k, v = s.split(":", 1)
        return k.strip(), v.strip()
    return "", s


def _table_cell(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": _clip(text or "", 1900)}}]


def _summary_table(points: list[str]) -> dict:
    """키워드 요약을 노션 표(항목 | 내용) 블록으로 만든다."""
    rows = [{
        "object": "block", "type": "table_row",
        "table_row": {"cells": [_table_cell("항목"), _table_cell("내용")]},
    }]
    for p in points:
        k, v = split_kv(p)
        rows.append({
            "object": "block", "type": "table_row",
            "table_row": {"cells": [_table_cell(k or "내용"), _table_cell(v)]},
        })
    return {
        "object": "block", "type": "table",
        "table": {"table_width": 2, "has_column_header": True,
                  "has_row_header": False, "children": rows},
    }


def to_notion_children(details: dict) -> list[dict]:
    """Notion pages.create(children=...)에 넣을 블록 목록을 만든다."""
    blocks: list[dict] = []

    # 1) 내용 요약 — 활동이 무엇인지(추천 이유와 분리).
    #    '항목: 내용' 줄이 2개 이상이면 정보표로, 아니면 글머리표로 보여준다.
    blocks.append(_heading("📝 내용 요약"))
    pts = details.get("summary_points") or _split_lines(details.get("summary", "")) \
        or ["원문에서 모집 대상·일정·혜택을 확인하세요."]
    keyed = [p for p in pts if split_kv(p)[0]]
    if len(keyed) >= 2:
        blocks.append(_summary_table(pts))
    else:
        blocks.extend(_bullet(p) for p in pts)

    # 2) 핵심 정보 — 마감/예상시간/적합도/도메인/출처를 글머리표로
    if details.get("meta"):
        blocks.append(_heading("📌 핵심 정보"))
        blocks.extend(_bullet(item) for item in details["meta"])

    # 3) 추천 이유 — 왜 나에게 맞는지(내용 요약과 중복 X)
    if details.get("reason"):
        blocks.append(_heading("🧭 추천 이유"))
        blocks.append(_paragraph(details["reason"]))

    # 4) 준비 체크리스트
    if details.get("checklist"):
        blocks.append(_heading("✅ 준비 체크리스트"))
        blocks.extend(_bullet(item) for item in details["checklist"])

    # 5) 원문 링크
    if details.get("url"):
        blocks.append(_heading("🔗 원문 링크"))
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [_text("공지 원문 열기", details["url"])]},
        })

    return blocks[:30]
