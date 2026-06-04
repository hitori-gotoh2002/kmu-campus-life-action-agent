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
        summary: str = Field(default="", description="키워드 중심 3~5줄(줄바꿈 구분) 내용 요약 — 왜 적합한지는 제외")
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


def _heuristic_analyze(notice, parsed_doc, ctx: dict) -> AnalysisResult:
    full_text = f"{notice.title} {notice.body}"
    if parsed_doc:
        full_text += " " + parsed_doc.text

    domain = _classify_domain(full_text)
    base_hours = _BASE_HOURS.get(domain, _BASE_HOURS["기타"])
    weight, weight_reason = _proficiency_weight(domain, full_text, ctx)
    est_hours = round(base_hours * weight)

    score = 55
    if weight == 0.7:
        score += 30
    elif weight == 1.5:
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
    _kw = [f"분야: {domain}"]
    if body_txt:
        _kw.append(f"내용: {body_txt[:70]}")
    else:
        _kw.append(f"활동: '{notice.title}' (원문에서 모집 요강 확인 필요)")
    _kw.append(f"예상 준비: {est_hours}시간")
    summary = "\n".join(_kw)

    print(f"   [analyzer] 개인 역량 가중치 {weight}배 적용 완료  → {est_hours}시간")
    return AnalysisResult(
        is_relevant=score >= 50,
        suitability_score=score,
        summary=summary,
        matching_reason=reason,
        estimated_hours_needed=est_hours,
        domain=domain,
    )


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
        "데이터/AI와 직접 관련이 적어도 '활동'이면 true 로 두고, "
        "적합도(suitability_score)로만 차등한다.\n"
        "- false: 학생 대상 인턴/현장실습이 아닌 정규직·계약직·직원·연구원 채용 공고, "
        "행정 안내(수강신청·등록·성적정정·수업평가·"
        "기숙사 등 학생이 '참여 주체'가 아닌 공지)만 false.\n"
        "[적합도 suitability_score]\n"
        "- 학생의 희망직무('AI 데이터 사이언티스트')와 강점(NLP·생성형AI·데이터모델링)에 "
        "가까울수록 높게. AI/데이터/분석/모델링 활동 80~95, 일반 기획/마케팅/문학/건축 등 30~55, "
        "무관/비참여 0~20.\n"
        "[소요시간 estimated_hours_needed] (관련 활동이면 절대 0 금지, 5~120 범위)\n"
        "- 기본: 공모전·경진대회 30, 해커톤 20, 대외활동·서포터즈(기간형) 35, "
        "인턴/현장실습 40, 자격증 50, 계절학기 45.\n"
        "- 보정: 학생 강점(high_proficiency) 분야면 ×0.7, 생소(low_proficiency) 분야면 ×1.5.\n"
        "- is_relevant=false 이면 estimated_hours_needed 는 0 으로 둔다.\n"
        "[summary] — 활동 내용을 '정보표'처럼 항목별로 정리한 요약(추천 이유 아님)\n"
        "- 반드시 줄바꿈(\\n)으로 구분하고, 각 줄은 '항목: 내용' 형식으로 쓴다(콜론 1개로 항목과 내용을 구분).\n"
        "- 아래 '공모전·대회 / 대외활동·서포터즈 / 채용·인턴' 세 분야는 원문에 있는 정보를 빠짐없이, 6~10줄로 자세히 정리한다:\n"
        "  · 공모전·대회: 주최/관련 기관, 참가 대상, 접수 기간, 공모 주제, 신청 분야, "
        "지원 방식(개인/팀), 시상 규모, 주요 혜택, 제출물, 문의처.\n"
        "  · 대외활동·서포터즈: 주최/주관, 활동 내용·미션, 모집 대상·인원, 활동 기간, "
        "활동 방식(온/오프라인·주 N회), 주요 혜택, 우대 사항, 지원 방식, 접수 기간.\n"
        "  · 채용·인턴: 회사/기관, 모집 직무·부문, 자격 요건, 근무 형태(인턴 기간·근무지·급여), "
        "우대 사항, 전형 절차, 접수 기간, 문의처.\n"
        "- 한 항목에 하위 구분이 있으면(예: 학생부/일반부, 부문별) 줄을 나눠 각각 '항목: 내용'으로 적는다.\n"
        "- 값이 여러 개면 쉼표로 나열한다(예: '주요 혜택: 장관상, 멘토링, 제작비, 사업화 연계').\n"
        "- 그 외 분야(장학금·자격증·학사일정 등)는 3~5줄로 핵심만 쓴다.\n"
        "- 원문(body)에서 확인되는 사실만 쓰고, 작성자·조회수·이전글 같은 군더더기는 제외한다.\n"
        "- '학생에게 왜 적합한지'·적합도·강점 연결은 절대 쓰지 않는다(그건 matching_reason).\n"
        "- 원문 정보가 부족하면 제목·분야에서 알 수 있는 항목으로라도 최소 3줄을 채운다.\n"
        "- 예(공모전) \"주최/관련 기관: 방위사업청, 국방과학연구소\\n참가 대상: 제한 없음(학생부·일반부 구분)\\n"
        "학생부: 대학생, 대학원생, 휴학생\\n일반부: 일반인, 예비창업자, 창업 3년 이내 스타트업\\n"
        "접수 기간: 2026.06.01~06.30 14시\\n공모 주제: 국방기술 민간 사업화 아이디어\\n"
        "신청 분야: 기계·소재, 전기·전자, 정보·통신, 바이오·의료 등\\n지원 방식: 개인 또는 팀\\n"
        "주요 혜택: 방위사업청장상, 멘토링, MVP 제작비, 사업화 연계\"\n"
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
    print(f"   [analyzer] LLM 구조화 출력 수신 → {data.get('estimated_hours_needed')}시간")
    return AnalysisResult(**data)


def analyze(notice, parsed_doc, ctx: dict) -> AnalysisResult:
    if _is_demo():
        return _heuristic_analyze(notice, parsed_doc, ctx)
    try:
        return _llm_analyze(notice, parsed_doc, ctx)
    except Exception as e:
        print(f"   [analyzer] LLM 실패({e}) → 휴리스틱 폴백")
        return _heuristic_analyze(notice, parsed_doc, ctx)


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
