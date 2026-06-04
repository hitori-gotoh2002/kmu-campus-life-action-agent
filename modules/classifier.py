"""modules/classifier.py
분류(Classifier) 에이전트.
공지를 7개 분야로 분류한다. 규칙(출처+키워드) 우선, 애매하면 LLM 폴백.
"""
from __future__ import annotations

import os

CATEGORIES = ["장학금", "공모전·대회", "대외활동·서포터즈", "학사일정", "채용·인턴", "자격증", "기타"]

# 키워드 규칙 (위에서부터 우선 적용 → 충돌 시 앞 카테고리 우선)
_ORDERED_RULES = [
    ("장학금", ["장학", "학자금", "장학금"]),
    ("자격증", [
        "자격증", "자격시험", "검정시험", "어학시험", "토익", "toeic", "기사 자격",
        "sqld", "adsp", "adp", "dasp", "빅데이터분석기사", "정보처리기사", "컴활",
        "컴퓨터활용능력", "부트캠프", "아카데미",
    ]),
    ("공모전·대회", ["공모전", "경진대회", "해커톤", "경연", "어워즈", "공모", "아이디어 공모", "챌린지", "대회"]),
    ("대외활동·서포터즈", ["서포터즈", "기자단", "홍보대사", "앰배서더", "멘토", "대외활동", "체험단", "기자 모집"]),
    ("채용·인턴", [
        "채용", "계약직", "정규직", "직원 모집", "인턴", "근로", "실무전문가", "용역", "구인",
        "현장실습", "직무체험", "신입", "career", "recruit",
    ]),
    ("학사일정", ["계절학기", "수강신청", "수강", "학점", "졸업", "등록 안내", "성적", "수업평가",
                 "재입학", "휴학", "복학", "학사", "전공 인정", "전공인정", "정정"]),
]


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _rule_classify(notice) -> str | None:
    src = getattr(notice, "source", "") or ""
    existing = getattr(notice, "category", "") or ""
    if existing in CATEGORIES:
        return existing
    # 출처가 가장 강한 신호
    if "공식 학사일정" in src:
        return "학사일정"
    if "장학" in src:
        return "장학금"
    if "공모전" in src:
        return "공모전·대회"
    if "대외활동" in src:
        return "대외활동·서포터즈"
    if any(k in src for k in ["취업", "채용", "인턴"]):
        return "채용·인턴"
    if any(k in src for k in ["자격증", "교육"]):
        return "자격증"
    # 제목 + 게시판 분류(ctg_name)만 사용 — 본문은 노이즈가 커서 제외
    blob = f"{getattr(notice,'category','')} {notice.title}".casefold()
    for cat, kws in _ORDERED_RULES:
        if any(k in blob for k in kws):
            return cat
    return None


def _llm_classify(notice) -> str:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        prompt = (
            "다음 공지를 정확히 한 단어로 분류하라. 보기: "
            + " / ".join(CATEGORIES) + ".\n"
            f"제목: {notice.title}\n본문: {getattr(notice,'body','')[:300]}\n"
            "보기 중 하나만 출력(설명 금지)."
        )
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=12,
        )
        out = (resp.choices[0].message.content or "").strip()
        for c in CATEGORIES:
            if c in out:
                return c
    except Exception as e:
        print(f"   [classifier] LLM 분류 실패({e})")
    return "기타"


def classify(notice) -> str:
    """공지 1건의 분야를 반환."""
    cat = _rule_classify(notice)
    if cat:
        return cat
    # 규칙으로 못 정한 경우만 LLM (비데모 + 키 있을 때)
    if not _is_demo() and os.getenv("OPENAI_API_KEY"):
        return _llm_classify(notice)
    return "기타"
