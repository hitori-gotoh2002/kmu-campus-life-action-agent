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
    r"붙임\d*|☎|조회\s*\d+)"
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

_CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩"
_INLINE_MARKER_RE = re.compile(rf"\s+([{_CIRCLED_NUMBERS}])\s*")
_NUMBERED_MARKER_RE = re.compile(r"(?<!\d)\s+([1-9])\.\s*(?=[가-힣A-Za-zA-Z])")
_NOTICE_MARKER_RE = re.compile(r"\s+(※)\s*")
_SYMBOL_BULLET_RE = re.compile(r"\s+(◎|○)\s*")
_RAW_NOTICE_MARKER_RE = re.compile(rf"(\s[1-9]\.\s|[{_CIRCLED_NUMBERS}※◎○])")
_LABEL_RE = re.compile(r"^\s*-?\s*([^:：]{2,24})\s*[:：]\s*(.+)$")
_KNOWN_LABELS = (
    "대상", "지원 대상", "신청 대상", "참가 대상", "자격", "지원 자격", "신청 자격",
    "기간", "신청 기간", "접수 기간", "활동 기간", "일정", "마감",
    "방법", "신청 방법", "접수 방법", "제출 방법",
    "혜택", "지원 내용", "선발", "주의사항", "유의사항", "문의",
)
_GOOD_SUMMARY_ENDINGS = (".", "!", "?", "…", "다", "요", "함", "됨", "가능", "제외", "참조", "확인")

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


def _normalize_summary_text(text: str) -> str:
    """공지 원문식 한 줄 나열을 읽기 좋은 문단/항목 형태로 되살린다."""
    text = (text or "").strip()
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = _NUMBERED_MARKER_RE.sub("\n- ", text)
    text = _INLINE_MARKER_RE.sub("\n- ", text)
    text = _SYMBOL_BULLET_RE.sub("\n- ", text)
    text = _NOTICE_MARKER_RE.sub(r"\n\n\1 ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_label_line(line: str, markdown: bool = False) -> str:
    """'신청 기간: ...' 같은 핵심 항목을 한눈에 들어오게 정리한다."""
    bullet = line.strip().startswith("-")
    raw = line.strip()[1:].strip() if bullet else line.strip()
    m = _LABEL_RE.match(raw)
    if not m:
        if bullet and len(raw) <= 18 and any(key in raw for key in _KNOWN_LABELS):
            return f"- **{raw}**" if markdown else f"- {raw}"
        return f"- {raw}" if bullet else raw
    label, value = m.group(1).strip(), m.group(2).strip()
    if not any(key in label for key in _KNOWN_LABELS):
        return f"- {raw}" if bullet else raw
    if markdown:
        return f"- **{label}:** {value}"
    return f"- {label}: {value}"


def _looks_truncated(text: str) -> bool:
    s = _clean(text)
    if len(s) < 360:
        return False
    return not s.endswith(_GOOD_SUMMARY_ENDINGS)


def _looks_like_raw_notice_excerpt(text: str) -> bool:
    return len(_clean(text)) < 700 and bool(_RAW_NOTICE_MARKER_RE.search(text or ""))


def _choose_content(summary: str, body: str) -> str:
    """저장된 요약이 중간에서 끊긴 경우 본문 기반 요약으로 복구한다."""
    summary = str(summary or "").strip()
    body_summary = _clean_body(body)
    should_recover = (
        _looks_truncated(summary)
        or _looks_like_raw_notice_excerpt(summary)
    ) and len(body_summary) > len(_clean(summary)) + 80
    if summary and not should_recover:
        return summary
    return body_summary or summary


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"((?:다|요)\.|[.!?])\s+", _clean(text))
    sentences = []
    for idx in range(0, len(parts), 2):
        sentence = parts[idx].strip()
        if not sentence:
            continue
        ending = parts[idx + 1] if idx + 1 < len(parts) else ""
        sentences.append((sentence + ending).strip())
    return sentences


def _summary_paragraphs(text: str) -> list[str]:
    """상세 설명을 노션 문단 블록용으로 자른다. 줄바꿈이 있으면 문단으로,
    한 덩어리로 길면 문장 단위로 묶어 읽기 좋게 나눈다."""
    text = _normalize_summary_text(text)
    if not text:
        return ["원문에서 모집 대상·일정·혜택을 확인하세요."]
    paras = [p.strip() for p in text.replace("\r", "\n").split("\n") if p.strip()]
    if len(paras) > 1:
        return [_clip(_format_label_line(p), 1800) for p in paras]
    if len(text) <= 450:
        return [text]
    # 긴 산문은 문장 2~3개씩 묶어 문단으로
    sents = _split_sentences(text)
    chunks, cur = [], ""
    for s in sents:
        cur = (cur + " " + s).strip() if cur else s
        if len(cur) >= 200:
            chunks.append(cur)
            cur = ""
    if cur:
        chunks.append(cur)
    return chunks or [text]


def summary_to_markdown(text: str) -> str:
    """웹 화면용 내용 요약. 기존 한 줄 요약도 문단/불릿으로 재배치한다."""
    blocks = _summary_paragraphs(text)
    lines = []
    for block in blocks:
        if block.startswith("- "):
            lines.append(_format_label_line(block, markdown=True))
        else:
            lines.append(block)
    return "\n\n".join(lines)


def summary_to_plain_text(text: str) -> str:
    """텔레그램 같은 일반 텍스트 채널용 내용 요약."""
    return "\n".join(_summary_paragraphs(text))


def build_event_details(candidate: dict) -> dict:
    """추천 후보를 사용자에게 도움이 되는 설명 구조로 변환한다."""
    notice = candidate["notice"]
    analysis = candidate["analysis"]

    title = _clean(_get(notice, "title"))
    # 활동 '내용 요약'(산문). LLM summary(자세한 설명)를 우선, 없으면 본문 정리본.
    # 문단 구분(줄바꿈)을 살려야 하므로 _clean(공백 평탄화)을 적용하지 않는다.
    content = _choose_content(_get(analysis, "summary"), _get(notice, "body"))
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

    # '내용 요약' = 활동을 자세히 풀어 쓴 산문(웹/노션 공통 단일 소스).
    summary = content or f"'{title}' 공고입니다. 원문에서 모집 대상·일정·혜택을 확인하세요."

    # 노션 '설명' 속성에 들어갈 한 줄(간결). 추천 이유는 본문 블록으로 분리하므로 제외.
    short_description = _clip(" ".join(summary.split()), 400)

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


def to_notion_children(details: dict) -> list[dict]:
    """Notion pages.create(children=...)에 넣을 블록 목록을 만든다."""
    blocks: list[dict] = []

    # 1) 내용 요약 — 활동을 자세히 풀어 쓴 산문(추천 이유와 분리).
    blocks.append(_heading("📝 내용 요약"))
    for para in _summary_paragraphs(details.get("summary", "")):
        if para.startswith("- "):
            blocks.append(_bullet(para[2:].strip()))
        else:
            blocks.append(_paragraph(para))

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
