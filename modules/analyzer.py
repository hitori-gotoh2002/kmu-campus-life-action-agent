"""modules/analyzer.py
맥락 분석 에이전트 (시스템의 심장부).

  - user_context 와 공지/평가기준을 비교해 적합도를 산출
  - 출력은 고정 스키마로 강제: is_relevant / suitability_score / matching_reason
                               / estimated_hours_needed
  - 역량 가중치: high_proficiency 도메인이면 시간 0.7배(단축),
                생소한 도메인이면 1.5배(공부시간 가중)
  - Critic 검증: 산출 결과의 타당성을 2차로 점검(환각/과대평가 차단)
"""
from __future__ import annotations

import json
import os
import re

try:
    from pydantic import BaseModel, Field

    class AnalysisResult(BaseModel):
        is_relevant: bool = Field(description="학생 진로와 관련 있는 활동인지")
        suitability_score: int = Field(ge=0, le=100, description="적합도 0~100")
        summary: str = Field(default="", description="줄바꿈이 있는 내용 요약 — 왜 적합한지는 제외")
        matching_reason: str = Field(description="적합/부적합 판단 근거")
        estimated_hours_needed: int = Field(ge=0, description="가중치 적용 후 예상 소요 시간")
        domain: str = Field(default="", description="분류된 도메인")

        def to_dict(self):
            return self.model_dump()

    _SCHEMA = "pydantic"
except ImportError:
    from dataclasses import dataclass, asdict

    @dataclass
    class AnalysisResult:  # type: ignore
        is_relevant: bool
        suitability_score: int
        matching_reason: str
        estimated_hours_needed: int
        domain: str = ""
        summary: str = ""

        def to_dict(self):
            return asdict(self)

    _SCHEMA = "dataclass(fallback)"


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


_DOMAIN_KEYWORDS = {
    "AI/데이터 모델링": ["모델", "예측", "데이터 분석", "머신러닝", "nlp", "생성형", "ai", "분석"],
    "서비스 기획/마케팅": ["기획", "마케팅", "페르소나", "ux", "리서치", "시장"],
    "학사/수강": ["계절학기", "전공", "학점", "수강", "개설"],
}
_BASE_HOURS = {"AI/데이터 모델링": 28, "서비스 기획/마케팅": 30, "학사/수강": 45, "기타": 20}

_CAREER_DEMO_POSITIVE = (
    "인턴", "intern", "채용형", "체험형", "현장실습", "직무체험", "트레이니",
    "trainee", "커리어세션", "career session", "교육생", "양성", "채용연계",
    "ai", "데이터", "data", "분석가", "analyst", "pm", "서비스 기획",
    "개발자", "developer", "engineer", "신입", "주니어", "campus",
)
_CAREER_DEMO_STRONG = (
    "ai", "데이터", "data", "분석가", "analyst", "머신러닝", "ml",
    "pm", "서비스 기획", "개발자", "developer", "engineer",
)
_CAREER_DEMO_NEGATIVE = (
    "간호", "조리원", "수의사", "지게차", "소방", "생산관리", "계약직원",
    "교학팀", "생활관", "법무", "인사팀", "고객응대", "수납", "학술팀",
    "영업 담당자", "고객 기술 지원",
)
_URL_RE = re.compile(r"https?://[^\s)>\]]+")


def _body_excerpt(text: str, limit: int = 1200) -> str:
    text = " ".join((text or "").split()).strip()
    if len(text) <= limit:
        return text
    chunk = text[:limit].rstrip()
    candidates = [chunk.rfind(mark) for mark in (".", "다.", "요.", "함.", "됨.")]
    cut = max(candidates)
    if cut >= int(limit * 0.6):
        return chunk[: cut + 1].strip()
    return chunk.rstrip(" ,.;:·") + "…"


def _summary_is_thin(summary: str) -> bool:
    text = (summary or "").strip()
    if not text:
        return True
    if len(text) < 260 or text.count("\n") < 3:
        return True
    thin_phrases = (
        "자세한 사항은 홈페이지 참고",
        "자세한 내용은 홈페이지 참고",
        "원문에서 모집 대상·일정·혜택을 확인",
        "게시물 내용",
    )
    return any(phrase in text for phrase in thin_phrases)


def _clean_career_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"작성일\s*\d{2}\.\d{2}\.\d{2}", " ", text)
    text = re.sub(r"구분\s*취업", " ", text)
    text = re.sub(r"작성자\s*\S+", " ", text)
    text = re.sub(r"조회수\s*\d+", " ", text)
    text = text.replace("게시물 내용", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_labeled_value(text: str, labels: tuple[str, ...], limit: int = 160) -> str:
    for label in labels:
        pattern = rf"(?:\[{re.escape(label)}\]|{re.escape(label)})\s*[:：]?\s*([^\n\[]{{2,{limit}}})"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = " ".join(match.group(1).split()).strip(" -:：|")
            if value:
                return _body_excerpt(value, limit)
    return ""


def _extract_external_detail(text: str, limit: int = 320) -> str:
    match = re.search(r"외부\s+상세\s+요약\s*[:：]\s*(.+)", text)
    if not match:
        return ""
    return _body_excerpt(match.group(1).strip(), limit)


def _extract_org_and_role(title: str) -> tuple[str, str]:
    title = " ".join((title or "").replace("…", "").split()).strip()
    org = ""
    match = re.match(r"\[([^\]]+)\]\s*(.+)", title)
    if match:
        org = match.group(1).strip()
        title = match.group(2).strip()
    else:
        org = title.split()[0].strip("[]") if title else "모집 기관"
    return org or "모집 기관", title or "채용·인턴 프로그램"


def _infer_career_type(text: str) -> str:
    low = text.casefold()
    if any(k in low for k in ("kdt", "campus", "교육생", "양성", "채용연계")):
        return "채용연계 교육/부트캠프"
    if any(k in low for k in ("인턴", "intern", "trainee", "traineeship", "트레이니")):
        return "인턴/트레이니"
    if any(k in low for k in ("career session", "커리어세션")):
        return "커리어 세션"
    if any(k in low for k in ("pm", "개발자", "분석가", "data", "ai")):
        return "직무 채용"
    return "채용 정보"


def _build_career_summary(notice, result: AnalysisResult) -> str:
    title = getattr(notice, "title", "") or ""
    body = getattr(notice, "body", "") or ""
    source_url = getattr(notice, "url", "") or ""
    text = _clean_career_text(f"{title}\n{body}")
    org, role = _extract_org_and_role(title)
    career_type = _infer_career_type(text)

    deadline = (
        _extract_labeled_value(text, ("모집기간", "모집 기간", "접수기간", "기간", "마감"), 120)
        or _extract_labeled_value(title, ("~",), 80)
    )
    if not deadline:
        match = re.search(r"\(~\s*([^)]+)\)", title)
        deadline = f"~ {match.group(1).strip()}" if match else ""

    field = _extract_labeled_value(text, ("모집분야", "모집 분야", "분야", "직무", "포지션"), 180)
    target = _extract_labeled_value(text, ("모집대상", "대상", "지원자격", "자격요건"), 180)
    apply = _extract_labeled_value(text, ("신청 방법", "지원 방법", "접수방법", "신청/제출", "지원/접수"), 180)
    region = _extract_labeled_value(text, ("지역", "근무지", "장소"), 120)
    external_detail = _extract_external_detail(text)
    homepage = ""
    urls = _URL_RE.findall(body)
    if urls:
        homepage = urls[0].rstrip(".,")

    lines = [
        f"{org}의 {role} 관련 {career_type} 공고입니다.",
        f"모집/직무: {field or role}",
    ]
    if external_detail:
        lines.append(f"외부 상세: {external_detail}")
    lines.extend([
        f"기간/마감: {deadline or '공고 원문에서 접수 마감일을 확인해야 합니다.'}",
        f"지원/접수: {apply or '지원 방식, 제출서류, 전형 절차는 공고 링크에서 확인해야 합니다.'}",
    ])
    if target:
        lines.append(f"대상/자격: {target}")
    if region:
        lines.append(f"근무/활동 지역: {region}")
    lines.append(
        "확인 포인트: 담당 업무나 교육 커리큘럼, 지원 자격, 전형 단계, 근무/교육 방식, 보상이나 수료 혜택을 원문에서 확인하세요."
    )
    if result.domain:
        lines.append(f"분류 힌트: {result.domain} 계열로 분석됐고 예상 준비 시간은 약 {result.estimated_hours_needed}시간입니다.")
    lines.append(f"원문: {homepage or source_url or '공고 링크를 앱에서 열어 확인하세요.'}")
    return "\n".join(lines)


def _classify_domain(text: str) -> str:
    low = text.lower()
    best, score = "기타", 0
    for domain, kws in _DOMAIN_KEYWORDS.items():
        hit = sum(1 for k in kws if k in low)
        if hit > score:
            best, score = domain, hit
    return best


def _proficiency_weight(domain: str, text: str, ctx: dict) -> tuple[float, str]:
    blob = (domain + " " + text).lower()
    high = [s for s in ctx.get("high_proficiency", []) if s.lower() in blob]
    low = [s for s in ctx.get("low_proficiency", []) if s.lower() in blob]

    if domain == "AI/데이터 모델링":
        high = high or ["Data Modeling"]
    if domain == "서비스 기획/마케팅":
        low = low or ["Service Planning"]

    if high:
        return 0.7, f"숙련 분야({', '.join(high)}) → 0.7배 단축"
    if low:
        return 1.5, f"생소 분야({', '.join(low)}) → 1.5배 가중"
    return 1.0, "기준 분야 → 1.0배"


def _heuristic_estimate(notice, parsed_doc, ctx: dict) -> tuple[str, int, float, str, int]:
    full_text = f"{notice.title} {notice.body}"
    if parsed_doc:
        full_text += " " + parsed_doc.text

    domain = _classify_domain(full_text)
    base_hours = _BASE_HOURS.get(domain, _BASE_HOURS["기타"])
    weight, weight_reason = _proficiency_weight(domain, full_text, ctx)
    est_hours = round(base_hours * weight)
    return domain, base_hours, weight, weight_reason, est_hours


def _heuristic_analyze(notice, parsed_doc, ctx: dict) -> AnalysisResult:
    full_text = f"{notice.title} {notice.body}"
    if parsed_doc:
        full_text += " " + parsed_doc.text

    domain, base_hours, weight, weight_reason, est_hours = _heuristic_estimate(notice, parsed_doc, ctx)

    score = 55
    if "0.7배" in weight_reason:
        score += 30
    elif "1.5배" in weight_reason:
        score -= 15
    if any(p.lower() in full_text.lower() for p in ["ai", "데이터", "분석", "nlp", "생성형"]):
        score += 10
    if "학점" in full_text and "부족" in ctx.get("unmet_graduation_requirement", ""):
        score += 20
        domain = "학사/수강"
    score = max(0, min(100, score))

    reason = (f"[{domain}] 기본 {base_hours}h × {weight}배 = {est_hours}h. {weight_reason}. "
              f"희망직무 '{ctx.get('desired_role','')}' 관점에서 적합도 {score}점.")

    body_txt = " ".join((notice.body or "").split()).strip()
    summary = (_body_excerpt(body_txt) if body_txt
               else f"'{notice.title}' 공고입니다. 원문에서 모집 대상·일정·혜택을 확인하세요.")

    print(f"   [analyzer] 개인 역량 가중치 {weight}배 적용 완료  → {est_hours}시간")
    return AnalysisResult(
        is_relevant=score >= 50,
        suitability_score=score,
        summary=summary,
        matching_reason=reason,
        estimated_hours_needed=est_hours,
        domain=domain,
    )


def _career_demo_relaxation(notice, result: AnalysisResult, strict_gate: bool = False) -> AnalysisResult:
    """Demo mode: keep career/internship recommendations visible for KMU AI/data students."""
    if getattr(notice, "category", "") != "채용·인턴":
        return result

    text = f"{getattr(notice, 'title', '')} {getattr(notice, 'body', '')}".casefold()
    has_negative = any(keyword.casefold() in text for keyword in _CAREER_DEMO_NEGATIVE)
    has_positive = any(keyword.casefold() in text for keyword in _CAREER_DEMO_POSITIVE)
    if strict_gate and has_negative:
        result.is_relevant = False
        result.suitability_score = min(int(result.suitability_score or 0), 20)
        result.estimated_hours_needed = 0
        result.matching_reason = "시연 기준에서도 전공/희망 직무와 거리가 먼 채용 공고라 추천에서 제외합니다."
        return result
    if strict_gate and not has_positive:
        result.is_relevant = False
        result.suitability_score = min(int(result.suitability_score or 0), 25)
        result.estimated_hours_needed = 0
        result.matching_reason = "시연 기준상 AI·데이터·PM·인턴·채용연계 교육 등과 연결되는 단서가 부족해 제외합니다."
        return result
    if has_negative or not has_positive:
        return result

    strong = any(keyword.casefold() in text for keyword in _CAREER_DEMO_STRONG)
    target_score = 65 if strong else 55
    result.is_relevant = True
    result.suitability_score = max(int(result.suitability_score or 0), target_score)
    if result.estimated_hours_needed <= 0:
        intensive = any(keyword in text for keyword in ("인턴", "intern", "채용형", "현장실습", "신입", "주니어"))
        result.estimated_hours_needed = 40 if intensive else 20
    if result.estimated_hours_needed > 30:
        result.estimated_hours_needed = 30
    if not result.domain or result.domain == "기타":
        result.domain = "AI/데이터 채용" if strong else "커리어 탐색"

    weak_reason = ("무관" in (result.matching_reason or "")) or ("관련 없음" in (result.matching_reason or ""))
    if weak_reason or not result.matching_reason:
        if strong:
            result.matching_reason = (
                "시연 기준에서는 AI·데이터·PM·개발 직무와 연결되는 채용/인턴/교육 공고를 "
                "커리어 탐색 후보로 넓게 인정합니다. 학생의 희망 직무와 직접 맞닿아 있어 추천함에 노출합니다."
            )
        else:
            result.matching_reason = (
                "시연 기준에서는 인턴, 채용형 프로그램, 커리어 세션처럼 학생이 진로 판단에 활용할 수 있는 "
                "채용 정보를 추천 후보로 완화해 노출합니다."
            )
    if _summary_is_thin(result.summary):
        result.summary = _build_career_summary(notice, result)
    print("   [analyzer] 채용·인턴 시연 완화 기준 적용")
    return result


def _llm_analyze(notice, parsed_doc, ctx: dict) -> AnalysisResult:
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    sys_prompt = (
        "너는 국민대 AI빅데이터융합경영학과 학생의 커리어 매니저다. "
        "공지와 학생 프로필을 비교해 아래 JSON 스키마로만 답하라. "
        "키: is_relevant(bool), suitability_score(int 0~100), summary(str), matching_reason(str), "
        "estimated_hours_needed(int), domain(str). JSON 외 텍스트 금지.\n"
        "[관련성 is_relevant 판단] — 분야별 추천이므로 폭넓게 인정한다\n"
        "- true: 학생이 직접 참여하는 모든 활동(공모전·대회·해커톤·대외활동·서포터즈·기자단·"
        "장학금·자격증·전공 계절학기·채용형/체험형 인턴·현장실습·직무체험 등). "
        "채용·인턴 분야에서는 시연 기준상 AI/데이터/IT/PM/서비스기획 관련 인턴, 채용형 인턴, "
        "트레이니, 채용연계 교육, 커리어 세션, 신입·주니어 채용도 학생의 진로 탐색 후보이면 true. "
        "데이터/AI와 직접 관련이 적어도 '활동'이면 true 로 두고, "
        "적합도(suitability_score)로만 차등한다.\n"
        "- false: 학생 진로 탐색과 거리가 먼 정규직·계약직·직원·연구원 채용 공고, "
        "간호·조리·지게차·생산관리·법무·인사·행정직처럼 전공/희망 직무와 명확히 무관한 공고, "
        "행정 안내(수강신청·등록·성적정정·수업평가·"
        "기숙사 등 학생이 '참여 주체'가 아닌 공지)만 false.\n"
        "[적합도 suitability_score]\n"
        "- 학생의 희망직무('AI 데이터 사이언티스트')와 강점(NLP·생성형AI·데이터모델링)에 "
        "가까울수록 높게. AI/데이터/분석/모델링 활동 80~95, AI/데이터/PM 채용·인턴·교육 60~85, "
        "일반 기획/마케팅/문학/건축 등 30~55, "
        "무관/비참여 0~20.\n"
        "[소요시간 estimated_hours_needed] (관련 활동이면 절대 0 금지, 5~120 범위)\n"
        "- 기본: 공모전·경진대회 30, 해커톤 20, 대외활동·서포터즈(기간형) 35, "
        "인턴/현장실습 40, 자격증 50, 계절학기 45.\n"
        "- 보정: 학생 강점(high_proficiency) 분야면 ×0.7, 생소(low_proficiency) 분야면 ×1.5.\n"
        "- is_relevant=false 이면 estimated_hours_needed 는 0 으로 둔다.\n"
        "[summary] — 사용자가 '할지 말지' 판단할 수 있는 상세 내용 요약(추천 이유 아님)\n"
        "- 반드시 줄바꿈이 포함된 6~10줄, 500~900자 안팎의 브리핑으로 작성한다. "
        "원문 정보가 충분한데 3줄 이하로 끝내면 안 된다.\n"
        "- 첫 문장은 이 공지가 무엇이고 어떤 결과물을 요구하는지 1~2문장으로 설명한다.\n"
        "- 그 다음 줄부터는 원문에 있는 정보만 골라 `대상: ...`, `활동/과제: ...`, `기간/마감: ...`, "
        "`신청/제출: ...`, `혜택/보상: ...`, `선발/평가: ...`, `유의사항: ...`, "
        "`판단 포인트: ...`처럼 라벨이 있는 줄로 정리한다.\n"
        "- 사용자가 판단해야 하는 정보(자격 제한, 실제 해야 할 일, 제출물, 일정 부담, 선발 방식, 혜택, 비용/장소, 문의)를 "
        "최소 5개 이상 포함한다. 정보가 없는 라벨은 생략하되, 제목과 마감만으로 끝내지 않는다.\n"
        "- 공모전·대회 / 대외활동·서포터즈 / 채용·인턴은 주최/주관, 대상, 활동·직무·과제, 제출물, 일정, 혜택, 선발/평가 방식을 최대한 포함한다.\n"
        "- 채용·인턴은 회사/기관명, 모집 직무나 교육명, 고용/프로그램 유형(인턴·트레이니·채용연계 교육·커리어세션 등), "
        "지원 기간/마감, 지원 자격, 담당 업무나 교육 커리큘럼, 전형 절차, 근무/교육 방식, 지역, 지원 링크를 우선 정리한다. "
        "원문 정보가 부족하면 부족한 항목을 '원문에서 확인 필요'라고 표시하되 한 줄 요약으로 끝내지 않는다.\n"
        "- 장학금은 신청 대상, 신청 기간, 신청 방법, 선발/지급 조건, 제출서류, 유의사항을 우선 포함한다.\n"
        "- 자격증·교육·학사일정은 일정, 대상, 해야 할 일, 비용/장소, 준비 부담, 주의사항 중심으로 정리한다.\n"
        "- 원문(body)에 있는 사실만 쓰고, 작성자·조회수·이전글 같은 군더더기는 제외한다.\n"
        "- '학생에게 왜 적합한지'·적합도·강점 연결은 절대 쓰지 않는다(그건 matching_reason).\n"
        "- 원문 정보가 정말 부족하면 '원문에 공개된 정보가 제한적입니다'라고 밝히고, 확인해야 할 항목을 판단 포인트에 적는다.\n"
        "- 예) \"방위사업청과 국방과학연구소가 주최하는 국방기술 활용 창업 아이디어 공모전입니다. 국방기술거래장터의 기술을 민간 사업화 아이디어로 연결하는 제안을 요구합니다.\\n"
        "대상: 대학생·대학원생·휴학생은 학생부로, 예비창업자와 창업 3년 이내 기업은 일반부로 지원할 수 있습니다.\\n"
        "활동/과제: 기계·소재, 전기·전자, 정보·통신, 바이오·의료 등 등록 기술을 활용해 사업화 아이디어와 실행 가능성을 제시해야 합니다.\\n"
        "기간/마감: 6월 1일부터 6월 30일 오후 2시까지 접수합니다.\\n"
        "신청/제출: 공모전 접수 페이지에서 참가 부문을 선택하고 제안서와 요구 서류를 제출해야 합니다.\\n"
        "혜택/보상: 우수작에는 방위사업청장상, 멘토링, MVP 제작비, 기술이전·사업화 연계가 제공됩니다.\\n"
        "판단 포인트: 단순 아이디어보다 기술 이해와 사업화 설계가 중요하므로, 팀 구성·제안서 작성 시간을 확보할 수 있는지 확인해야 합니다.\"\n"
        "[matching_reason] — '왜 이 학생에게 추천하는지'만 (활동 내용 재설명 금지)\n"
        "- 활동이 무엇인지 다시 설명하지 말고, summary 와 내용이 겹치지 않게 쓴다.\n"
        "- 1~2문장: 학생의 희망직무·강점·부족한 역량과 어떻게 연결되는지 설명한다.\n"
        "- 1문장: 준비시간, 마감, 시험기간 부담처럼 실제 판단에 필요한 주의점을 적는다."
    )
    user_prompt = json.dumps({
        "notice": {"title": notice.title,
                   "category": getattr(notice, "category", "") or "",
                   "body": notice.body},
        "eval_criteria": parsed_doc.eval_criteria if parsed_doc else {},
        "student": ctx,
    }, ensure_ascii=False)

    raw = ""
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": sys_prompt},
                      {"role": "user", "content": user_prompt}],
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content
    else:
        import google.generativeai as genai
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
        resp = model.generate_content(
            sys_prompt + "\n\n" + user_prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        raw = resp.text

    data = json.loads(re.sub(r"```json|```", "", raw).strip())
    if data.get("estimated_hours_needed") in (None, ""):
        if data.get("is_relevant") is False:
            data["estimated_hours_needed"] = 0
        else:
            domain_guess, _base, _weight, _why, est_hours = _heuristic_estimate(notice, parsed_doc, ctx)
            data["estimated_hours_needed"] = est_hours
            data.setdefault("domain", domain_guess)
            print(f"   [analyzer] LLM 시간 누락 → 휴리스틱으로 {est_hours}시간만 보정")
    if not data.get("domain"):
        domain_guess, _base, _weight, _why, _est_hours = _heuristic_estimate(notice, parsed_doc, ctx)
        data["domain"] = domain_guess
    data.setdefault("summary", "")
    data.setdefault("matching_reason", "")
    print(f"   [analyzer] LLM 구조화 출력 수신 → {data.get('estimated_hours_needed')}시간")
    return AnalysisResult(**data)


def analyze(notice, parsed_doc, ctx: dict) -> AnalysisResult:
    if _is_demo():
        return _career_demo_relaxation(notice, _heuristic_analyze(notice, parsed_doc, ctx), strict_gate=True)
    try:
        return _career_demo_relaxation(notice, _llm_analyze(notice, parsed_doc, ctx), strict_gate=False)
    except Exception as e:
        print(f"   [analyzer] LLM 실패({e}) → 휴리스틱 폴백")
        return _career_demo_relaxation(notice, _heuristic_analyze(notice, parsed_doc, ctx), strict_gate=True)


def critic_review(result: AnalysisResult, parsed_doc) -> tuple[bool, str]:
    """결과 타당성 2차 점검.
    - 소프트 보정: 의심스러운 값은 '제외'하지 않고 값만 교정해 좋은 후보를 살린다.
    - 하드 실패: 명백히 못 쓰는 결과(소요시간 0 이하)만 제외.
    """
    notes = []

    # 소프트 보정 ① 평가기준(rubric) 근거 없이 고득점 → 과신 방지로 점수 하향(유지)
    if result.suitability_score >= 80 and (not parsed_doc or not parsed_doc.eval_criteria):
        before = result.suitability_score
        result.suitability_score = max(50, before - 15)
        notes.append(f"평가기준 미첨부 → 점수 {before}→{result.suitability_score} 하향")

    # 소프트 보정 ② 소요시간 과대 → 현실값으로 클램프(유지)
    if result.estimated_hours_needed > 200:
        result.estimated_hours_needed = 120
        notes.append("소요시간 과대(>200h) → 120h 보정")

    # 하드 실패: 관련 활동인데 소요시간이 0 이하면 추정 실패로 간주
    if result.estimated_hours_needed <= 0:
        print("   [critic] 소요시간 0 이하 → 비정상(제외)")
        return False, "소요시간 0 이하"

    msg = "검증 통과" + (f" ({'; '.join(notes)})" if notes else "")
    print(f"   [critic] {msg}")
    return True, msg


def llm_critic(notice, result, ctx: dict) -> tuple[bool, str]:
    """LLM 검증관(2차). 분석 결과가 학생 프로필에 비추어 타당한지 재검토.
    후보(검증 통과분)에만 호출해 비용을 아낀다. 실패 시 통과로 폴백.
    """
    if _is_demo() or not os.getenv("OPENAI_API_KEY"):
        return True, "LLM critic 생략(demo)"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        prompt = (
            "너는 추천 검증관이다. 아래 학생에게 이 활동 추천이 타당한지 판단해 JSON으로만 답하라. "
            "키: ok(bool, 학생 진로/강점과 실제 부합하고 점수가 과대평가가 아니면 true), "
            "reason(str, 1문장).\n"
            f"학생: 희망직무={ctx.get('desired_role')}, 강점={ctx.get('high_proficiency')}, "
            f"관심={ctx.get('interests')}\n"
            f"활동: {notice.title}\n"
            f"분석: 적합도 {result.suitability_score}/100, 도메인 {getattr(result,'domain','')}, "
            f"근거 {result.matching_reason[:200]}"
        )
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, max_tokens=120,
        )
        import json as _json
        data = _json.loads(resp.choices[0].message.content)
        ok = bool(data.get("ok", True))
        reason = data.get("reason", "")
        print(f"   [critic-LLM] {'승인' if ok else '반려'}: {reason[:60]}")
        return ok, reason
    except Exception as e:
        print(f"   [critic-LLM] 실패({e}) → 통과 처리")
        return True, "LLM critic 오류"
