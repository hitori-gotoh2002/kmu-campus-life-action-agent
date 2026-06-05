"""로드맵 플래너 — 결정론 greedy 통합 배치 + 결정론 검증.

남은 의무(필수·융합 그룹·전공·교양 슬롯)를 단일 후보 풀로 정규화해 우선순위·선수·
개설학기·학점상한(제32조)을 지키며 greedy 배치하고, 검증기가 미배치/상한을 재확인한다.
실패면 정직한 blocked + 초과학기 시나리오(가짜 계획 금지). LLM은 배치에 사용하지 않는다
(출력 일관성 — 같은 입력이면 같은 로드맵). 과거 LLM 계획 함수(plan_roadmap 등)는 보존만.
LLM의 자리는 요람 RAG Q&A(qa.py) — 근거 인용 답변 생성에만 쓴다.
"""
from __future__ import annotations

import json
import os

from graduation_center.v2.catalog import (
    PREV_GPA_BONUS, SEASONAL_TERM_CAP, V2_DIR, load_catalog, regular_term_cap,
)
from graduation_center.v2.models_v2 import (
    AuditResult, RequirementProfile, RoadmapCourse, RoadmapPlan, RoadmapTerm, Source,
    StudentContext, ValidationError, ValidationReport, VerifiedTranscript,
)
from graduation_center.v2.text_norm import normalize_name

MAJOR_AREAS = {"전공"}  # 카탈로그(코드 보유)로 후보 가능한 영역


def _term_sem(term: str) -> str | None:
    # "2026-2" → "2"
    if term and "-" in term:
        return term.split("-")[-1]
    return None


def _term_key(term: str):
    try:
        y, s = term.split("-")
        order = {"1": 1, "S": 2, "2": 3, "W": 4}.get(s, 9)
        return (int(y), order)
    except Exception:
        return (9999, 9)


def _nth_regular_term(current_term: str | None, n: int) -> str | None:
    """current_term 다음의 n번째 정규학기(1/2학기) 라벨. n<=0이면 None."""
    if not current_term or n <= 0:
        return None
    try:
        y, s = current_term.split("-")
        y = int(y); so = {"1": 1, "S": 2, "2": 3, "W": 4}[s]
    except Exception:
        return None
    label = {1: "1", 3: "2"}
    count, steps = 0, 0
    while steps < 60:
        steps += 1
        so += 1
        if so > 4:
            so = 1; y += 1
        if so in (1, 3):                      # 정규학기만 카운트
            count += 1
            if count == n:
                return f"{y}-{label[so]}"
    return None


def project_overflow(audit: AuditResult, profile: RequirementProfile, context: StudentContext):
    """잔여 정규학기로 부족 학점을 못 채우면 초과학기 예상 시나리오 산출(결정론).

    capacity = 잔여학기 × 학기당 상한(+직전 3.75↑ 보너스 1회). shortfall(총 졸업학점 부족)이
    capacity를 넘으면, 필요한 총 정규학기 수와 초과학기 수·예상 졸업학기를 계산한다.
    """
    from graduation_center.v2.models_v2 import OverflowScenario
    # 더 채워야 하는 학점 = max(졸업최저 부족, 영역별 부족 합). 총학점은 충분해도 특정 영역
    # (예: 전공)이 부족하면 그만큼 추가 이수가 필요하므로 영역 갭 합도 본다.
    area_shortfall = round(sum(g.gap for g in audit.area_gaps if g.gap > 0), 1)
    shortfall = max(float(audit.total_gap), area_shortfall)
    if shortfall <= 0:
        return None
    cap = float(context.max_credits_per_term or regular_term_cap(profile.total_credits_min))
    if cap <= 0:
        return None
    bonus = (PREV_GPA_BONUS if (context.prev_term_gpa_ge_375
                                and context.max_credits_per_term is None) else 0.0)
    remaining = int(context.remaining_semesters)
    if shortfall <= remaining * cap + bonus:
        return None                            # 잔여 학기로 충분 → 시나리오 불필요
    # 필요한 최소 정규학기 수(첫 학기에만 보너스 1회)
    k = 1
    while k * cap + bonus < shortfall and k < 60:
        k += 1
    return OverflowScenario(
        shortfall_credits=round(shortfall, 1),
        per_term_credit_cap=cap,
        remaining_semesters=remaining,
        total_semesters_needed=k,
        extra_semesters=max(0, k - remaining),
        projected_graduation_term=_nth_regular_term(context.current_term, k),
        note=f"잔여 {remaining}학기·학기당 최대 {cap:.0f}학점으로는 부족 {shortfall:.0f}학점을 채울 수 없습니다. "
             f"최소 {k}학기(초과학기 {max(0, k - remaining)}학기)가 필요합니다.",
    )


def build_planning_context(
    audit: AuditResult, profile: RequirementProfile, context: StudentContext,
    verified: VerifiedTranscript,
) -> dict:
    cat = load_catalog(context.program_id)
    confirmed_ids = {c.course_id for c in verified.confirmed_courses if c.course_id}
    unresolved_norms = {normalize_name(m.raw.course_name) for m in verified.unresolved}

    gap_areas = {g.area for g in audit.area_gaps if g.gap > 0}
    candidates, sources = [], []
    src_idx = {}

    def src_for(course):
        if course.course_id not in src_idx:
            sid = f"G{len(sources) + 1}"
            src_idx[course.course_id] = sid
            sources.append(Source(id=sid,
                                  doc=course.source.get("doc", "2025-2 교육과정 교과목코드 현황"),
                                  page=course.source.get("page"),
                                  source_type="catalog_course", ref=course.course_id))
        return src_idx[course.course_id]

    want_major = bool(gap_areas & MAJOR_AREAS) or bool(audit.missing_required_course_ids)
    for c in cat["courses"]:
        if c.course_id in confirmed_ids or normalize_name(c.name_ko) in unresolved_norms:
            continue
        if getattr(c, "discontinued", False):          # 폐지 과목(2026 개정) — 추천 후보 제외
            continue
        is_missing_required = c.course_id in audit.missing_required_course_ids
        if (want_major and c.requirement_area in MAJOR_AREAS) or is_missing_required:
            candidates.append({
                "course_id": c.course_id, "name_ko": c.name_ko, "credits": c.credits,
                "requirement_area": c.requirement_area, "is_required": c.is_required,
                "prerequisites": c.prerequisites, "offered_terms": c.offered_terms,
                "source_id": src_for(c),
            })
    # 비-major(교양) 갭은 후보 카탈로그가 없어 자동계획 불가 → 별도 표기
    non_major_gap_areas = sorted(gap_areas - MAJOR_AREAS)
    # 학사규정 제32조: 정규학기 상한(사용자 override 우선), 계절 6학점, 직전 3.75↑ → 첫 학기 +3
    term_cap = float(context.max_credits_per_term or regular_term_cap(profile.total_credits_min))
    caps = {
        "regular_term_credits": term_cap,
        "seasonal_term_credits": SEASONAL_TERM_CAP,
        "first_term_bonus": (PREV_GPA_BONUS if (context.prev_term_gpa_ge_375
                                                 and context.max_credits_per_term is None) else 0.0),
        "prev_term_gpa_ge_375": context.prev_term_gpa_ge_375,
    }
    return {
        "student_context": {
            "current_term": context.current_term, "remaining_semesters": context.remaining_semesters,
            "seasonal_semester_allowed": context.seasonal_semester_allowed,
            "max_credits_per_term": term_cap,
            "seasonal_credit_cap": SEASONAL_TERM_CAP,
            "first_regular_term_extra_credits": caps["first_term_bonus"],
            "preferences": context.preferences,
        },
        "caps": caps,
        "audit_result": {
            "total_gap": audit.total_gap,
            "gaps": [{"area": g.area, "gap": g.gap} for g in audit.area_gaps if g.gap > 0],
            "missing_required_course_ids": audit.missing_required_course_ids,
            "missing_required_names": audit.missing_required_names,
        },
        "completed_ids": sorted(confirmed_ids),
        "candidate_courses": candidates,
        "non_major_gap_areas": non_major_gap_areas,
        "sources": [s.model_dump() for s in sources],
    }


_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "feasible": {"type": "boolean"},
        "terms": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "term": {"type": "string"},
                "courses": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "course_id": {"type": "string"}, "name_ko": {"type": "string"},
                        "credits": {"type": "number"}, "satisfies": {"type": "string"},
                        "reason": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                    }, "required": ["course_id", "name_ko", "credits", "satisfies", "reason", "source_ids"]}},
                "term_credits": {"type": "number"},
                "term_risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "notes": {"type": "array", "items": {"type": "string"}},
            }, "required": ["term", "courses", "term_credits", "term_risk", "notes"]}},
        "why_this_plan": {"type": "string"},
        "blocked_reason": {"type": ["string", "null"]},
        "relaxation_hint": {"type": ["string", "null"]},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["feasible", "terms", "why_this_plan", "blocked_reason", "relaxation_hint", "assumptions"],
}

_SYS = ("너는 졸업 로드맵 플래너다. 제공된 candidate_courses와 사실만 사용해 남은 학기에 들을 "
        "과목을 배치한다. 새 과목·학점·요건을 지어내지 마라. 선수과목 순서·개설학기를 지키고, "
        "학기당 이수학점 상한(student_context.max_credits_per_term, 계절학기는 seasonal_credit_cap, "
        "첫 정규학기는 first_regular_term_extra_credits만큼 추가 허용)을 넘기지 마라. "
        "학생 선호를 반영한다. 출력은 스키마 JSON만.")


def _get_client():
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None
    try:
        from openai import OpenAI
        return OpenAI()
    except Exception:
        return None


def plan_roadmap(planning_context: dict, client=None) -> RoadmapPlan:
    client = client or _get_client()
    if client is None:
        return RoadmapPlan(status="not_generated",
                           why_this_plan="LLM 미설정(OPENAI_API_KEY 없음) — 결정론 진단/리스크만 제공.")
    model = os.getenv("OPENAI_GRADUATION_MODEL", "gpt-5-mini")
    kwargs = {
        "model": model,
        "input": [{"role": "system", "content": _SYS},
                  {"role": "user", "content": json.dumps(planning_context, ensure_ascii=False)}],
        "text": {"format": {"type": "json_schema", "name": "roadmap_plan",
                            "schema": _SCHEMA, "strict": True}},
    }
    if any(model.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")):
        kwargs["reasoning"] = {"effort": "minimal"}
    else:
        kwargs["temperature"] = 0.1
    resp = client.responses.create(**kwargs)
    text = getattr(resp, "output_text", "") or ""
    data = json.loads(text)
    return RoadmapPlan(
        status="generated",
        feasible=data.get("feasible"),
        terms=data.get("terms", []),
        why_this_plan=data.get("why_this_plan", ""),
        blocked_reason=data.get("blocked_reason"),
        relaxation_hint=data.get("relaxation_hint"),
        assumptions=data.get("assumptions", []),
    )


def _allowed_terms(context: StudentContext) -> set[str] | None:
    """current_term·잔여학기·계절학기로 허용 가능한 학기 라벨 집합 생성."""
    if not context.current_term:
        return None
    try:
        y, s = context.current_term.split("-")
        y = int(y); so = {"1": 1, "S": 2, "2": 3, "W": 4}[s]
    except Exception:
        return None
    label = {1: "1", 2: "S", 3: "2", 4: "W"}
    allowed, reg, steps = set(), 0, 0
    while reg < context.remaining_semesters and steps < 40:
        steps += 1
        so += 1
        if so > 4:
            so = 1; y += 1
        lab = f"{y}-{label[so]}"
        if so in (1, 3):           # 정규학기
            allowed.add(lab); reg += 1
        elif context.seasonal_semester_allowed:  # 계절학기(허용 시)
            allowed.add(lab)
    return allowed


def validate_roadmap(plan: RoadmapPlan, ctx: dict, context: StudentContext) -> ValidationReport:
    errors: list[ValidationError] = []
    cand = {c["course_id"]: c for c in ctx["candidate_courses"]}
    src_ids = {s["id"] for s in ctx["sources"]}
    completed = set(ctx.get("completed_ids", []))
    allowed = _allowed_terms(context)
    planned: set[str] = set()          # 전체 계획된 course_id
    prior_planned: set[str] = set()    # 앞 학기까지 계획된 course_id (선수 검사용)
    seen_terms: set[str] = set()
    planned_by_area: dict[str, float] = {}

    caps = ctx.get("caps", {})
    reg_cap = float(caps.get("regular_term_credits", 19.0))
    seasonal_cap = float(caps.get("seasonal_term_credits", SEASONAL_TERM_CAP))
    first_bonus = float(caps.get("first_term_bonus", 0.0))
    # 직전학기 3.75↑ 보너스는 첫 '정규학기'에만 — 계획상 가장 이른 정규학기 식별
    first_regular = None
    for t in sorted(plan.terms, key=lambda x: _term_key(x.term)):
        if _term_sem(t.term) in ("1", "2"):
            first_regular = t.term
            break

    # 잔여학기 상한은 '정규학기' 수 기준(계절학기는 _allowed_terms에서 별도 허용·6학점 cap)
    regular_count = sum(1 for t in plan.terms if _term_sem(t.term) in ("1", "2"))
    if regular_count > context.remaining_semesters:
        errors.append(ValidationError(code="too_many_terms",
                      detail=f"정규학기 수 {regular_count} > 잔여 {context.remaining_semesters}"))

    for t in plan.terms:
        sem = _term_sem(t.term)
        if t.term in seen_terms:
            errors.append(ValidationError(code="dup_term", detail=f"학기 라벨 중복: {t.term}"))
        seen_terms.add(t.term)
        if allowed is not None and t.term not in allowed:
            errors.append(ValidationError(code="term_out_of_range",
                          detail=f"{t.term}은 허용 학기({sorted(allowed)}) 밖"))
        # 학사규정 제32조 학기당 이수학점 상한(정규/계절 + 첫 정규학기 보너스)
        is_seasonal = sem in ("S", "W")
        term_cap = seasonal_cap if is_seasonal else (reg_cap + (first_bonus if t.term == first_regular else 0.0))
        term_credit_total = sum(float(c.credits) for c in t.courses)
        if term_credit_total > term_cap + 0.01:
            errors.append(ValidationError(code="over_credit_cap",
                          detail=f"{t.term} 이수학점 {term_credit_total:.0f} > 상한 {term_cap:.0f}"))
        if sem in ("S", "W") and not context.seasonal_semester_allowed:
            errors.append(ValidationError(code="seasonal_not_allowed", detail=f"{t.term} 계절학기 불가"))
        cur_term_ids = []
        catalog_credit_sum = 0.0
        for c in t.courses:
            if c.course_id not in cand:
                errors.append(ValidationError(code="unknown_course", detail=c.name_ko, course_id=c.course_id))
                continue
            cc = cand[c.course_id]
            if c.course_id in planned:
                errors.append(ValidationError(code="dup_course", detail=c.name_ko, course_id=c.course_id))
            planned.add(c.course_id)
            cur_term_ids.append(c.course_id)
            # 학점은 카탈로그 값이 진실 — LLM 값 위조 방지
            if abs(float(c.credits) - float(cc["credits"])) > 0.01:
                errors.append(ValidationError(code="credit_mismatch",
                              detail=f"{c.name_ko} 학점 {c.credits}≠카탈로그 {cc['credits']}", course_id=c.course_id))
            catalog_credit_sum += cc["credits"]
            # satisfies는 카탈로그 영역과 일치해야
            if cc["requirement_area"] not in (c.satisfies or "") and (c.satisfies or "") not in ("필수", "전공필수"):
                errors.append(ValidationError(code="bad_satisfies",
                              detail=f"{c.name_ko} satisfies={c.satisfies}≠{cc['requirement_area']}", course_id=c.course_id))
            if sem and sem not in cc["offered_terms"]:
                errors.append(ValidationError(code="not_offered", detail=f"{c.name_ko} {t.term} 미개설", course_id=c.course_id))
            # 선수과목: 이미 이수 or 앞 학기에 계획돼야 (같은 학기/뒤 학기 불가)
            for pre in cc.get("prerequisites", []):
                if pre not in completed and pre not in prior_planned:
                    errors.append(ValidationError(code="prereq_unmet",
                                  detail=f"{c.name_ko} 선수과목 미충족", course_id=c.course_id))
            if any(sid not in src_ids for sid in c.source_ids):
                errors.append(ValidationError(code="bad_source", detail=c.name_ko, course_id=c.course_id))
            planned_by_area[cc["requirement_area"]] = planned_by_area.get(cc["requirement_area"], 0.0) + cc["credits"]
        # term_credits는 카탈로그 학점 합과 일치해야
        if abs(round(catalog_credit_sum, 1) - float(t.term_credits)) > 0.01:
            errors.append(ValidationError(code="term_credits_mismatch", detail=f"{t.term} 학점합 불일치"))
        prior_planned |= set(cur_term_ids)

    for mid in ctx["audit_result"]["missing_required_course_ids"]:
        if mid not in planned:
            errors.append(ValidationError(code="required_not_planned", detail="필수지정 과목 미포함", course_id=mid))
    for g in ctx["audit_result"]["gaps"]:
        if g["area"] in MAJOR_AREAS and planned_by_area.get(g["area"], 0.0) < g["gap"]:
            errors.append(ValidationError(code="gap_not_closed",
                          detail=f"{g['area']} 계획 {planned_by_area.get(g['area'],0)}<부족 {g['gap']}"))
    return ValidationReport(ok=not errors, errors=errors)


# ============ 결정론 통합 플래너 (codex 설계: 후보 정규화 → greedy 배치 → 검증) ============
def _ordered_terms(context: StudentContext, reg_cap: float) -> list[list]:
    """허용 학기를 시간순 [[label, cap]]로. 첫 정규학기 +직전3.75 보너스, 계절=6."""
    if not context.current_term:
        return []
    try:
        y, s = context.current_term.split("-"); y = int(y); so = {"1": 1, "S": 2, "2": 3, "W": 4}[s]
    except Exception:
        return []
    label = {1: "1", 2: "S", 3: "2", 4: "W"}
    out, reg, steps, first = [], 0, 0, True
    while reg < context.remaining_semesters and steps < 40:
        steps += 1; so += 1
        if so > 4:
            so = 1; y += 1
        lab = f"{y}-{label[so]}"
        if so in (1, 3):
            # 보너스(+3)는 '신청 가능 상한 확대'이지 의무가 아님 — 사용자가 상한을 명시하면
            # 그 의사가 우선(예: "12학점 이내로" 했는데 첫 학기 15 배치되던 버그, 2026-06-05)
            bonus_ok = first and context.prev_term_gpa_ge_375 and context.max_credits_per_term is None
            cap = reg_cap + (PREV_GPA_BONUS if bonus_ok else 0.0)
            out.append([lab, cap]); reg += 1; first = False
        elif context.seasonal_semester_allowed:
            out.append([lab, SEASONAL_TERM_CAP])
    return out


def _required_meta(program_id: str, year: int | None) -> dict:
    """학번 요람 필수 과목의 학점·개설학기 메타 {정규화이름: {credits, terms}}."""
    import json
    p = V2_DIR / "required_names_by_year.json"
    by_year = (json.loads(p.read_text(encoding="utf-8")).get("programs", {}) if p.exists() else {}).get(program_id)
    if not by_year:
        return {}
    avail = sorted(int(y) for y in by_year)
    pick = year if (year and str(year) in by_year) else (
        [y for y in avail if not year or y <= year][-1:] or [avail[-1]])[0]
    meta = {}
    for it in by_year.get(str(pick), []):
        if isinstance(it, dict):
            meta[normalize_name(it["name"])] = {"credits": float(it.get("credits", 3.0)),
                                                "terms": list(it.get("terms") or [])}
    return meta


def _overflow_from_credits(unplaced_credits: float, context: StudentContext, profile: RequirementProfile,
                           reg_cap: float):
    """미배치 학점(잔여 학기에 다 못 넣은 분)으로 초과학기 시나리오 산출 — 단일 용량 모델."""
    from graduation_center.v2.models_v2 import OverflowScenario
    import math
    if unplaced_credits <= 0:
        return None
    cap = float(reg_cap)
    extra = int(math.ceil(unplaced_credits / cap)) if cap > 0 else 0
    total_needed = context.remaining_semesters + extra
    return OverflowScenario(
        shortfall_credits=round(unplaced_credits, 1), per_term_credit_cap=cap,
        remaining_semesters=context.remaining_semesters, total_semesters_needed=total_needed,
        extra_semesters=extra, projected_graduation_term=_nth_regular_term(context.current_term, total_needed),
        note=f"잔여 {context.remaining_semesters}학기에 다 배치하지 못한 {unplaced_credits:.0f}학점 → "
             f"초과학기 약 {extra}학기 추가 필요(총 {total_needed}학기).")


def _slot_chunks(name: str, total: float, satisfies: str, size: float = 3.0) -> list[dict]:
    """교양 부족분을 학기당 배치 가능한 3학점 단위 슬롯으로 분할(단일 큰 슬롯 배치불가 방지)."""
    out, rem, i = [], round(float(total), 1), 0
    while rem > 0.01 and i < 60:    # 슬롯 상한 — 비정상 거대 갭(오염 입력)의 무한루프/OOM 가드
        c = min(size, rem); i += 1
        out.append({"name_ko": (name if total <= size else f"{name} #{i}"), "credits": round(c, 1),
                    "satisfies": satisfies, "confidence": "generic_slot", "manual": True})
        rem = round(rem - c, 1)
    return out


def build_unified_candidates(audit: AuditResult, profile: RequirementProfile,
                             verified: VerifiedTranscript) -> tuple[list[dict], list[dict]]:
    """남은 졸업 의무를 단일 후보 풀로 정규화(전공·필수·융합·교양). 반환 (선택후보, 요건요약)."""
    cat = load_catalog(profile.program_id)
    confirmed_norm = {normalize_name(c.name_ko) for c in verified.confirmed_courses}
    confirmed_pref = {c.course_id[:5] for c in verified.confirmed_courses if c.course_id}
    reqs: list[dict] = []

    # 1) 미이수 필수(이름) — 전부 이수 필요. 학점·개설학기·코드·선수를 요람메타→카탈로그 순으로 보강.
    # 카탈로그 매칭은 alias(명칭 드리프트 동치)까지 시도 — 옛 이름으로만 계획하고 신명을 전공 풀에서
    # 또 선택하는 '같은 물리 과목 이중 계획'을 방지(라운드5 검증).
    if audit.missing_required_names:
        from graduation_center.v2.audit_v2 import _group_label, _required_aliases, _required_groups_for_year
        rmeta = _required_meta(profile.program_id, profile.admission_year)
        alias_groups = _required_aliases(profile.program_id)
        # choose-1 그룹 라벨 → 배치 후보는 멤버 중 1개(카탈로그 있는 첫 과목)로 해소
        # (라벨 합성은 audit과 _group_label 공유 — 합성 규칙이 갈리면 그룹 미해소)
        grp_by_label = {_group_label(g): g for g in _required_groups_for_year(profile.program_id, profile.admission_year)}
        items = []
        for n in audit.missing_required_names:
            grp = grp_by_label.get(n)
            if grp:
                cat_norms = cat.get("by_norm") or {}
                member = next((it for it in grp["items"] if normalize_name(it["name"]) in cat_norms),
                              grp["items"][0])
                items.append({"name_ko": member["name"], "course_id": "",
                              "credits": float(member.get("credits", 3.0)),
                              "satisfies": "필수지정(택1)",
                              "offered_terms": list(member.get("terms") or ["1", "2"]),
                              "prerequisites": [],
                              "confidence": "catalog_verified" if member.get("terms") else "name_only",
                              "manual": not member.get("terms"), "_alias_norms": []})
                continue
            nn = normalize_name(n)
            m = rmeta.get(nn, {})
            terms = list(m.get("terms") or [])
            credits = m.get("credits")
            cid, prereqs = "", []
            # 본명 → alias 동치명 순으로 카탈로그 매칭
            hit = None
            for key in [nn, *alias_groups.get(nn, [])]:
                hit = (cat.get("by_norm") or {}).get(key)
                if hit:
                    break
            if hit:
                cc = cat["by_code"].get(hit[0])
                if cc:
                    cid = cc.course_id
                    if credits is None:
                        credits = cc.credits
                    if not terms:
                        terms = list(cc.offered_terms or [])
                    prereqs = list(cc.prerequisites or [])
            known = bool(terms)
            items.append({"name_ko": n, "course_id": cid, "credits": (credits if credits is not None else 3.0),
                          "satisfies": "필수지정", "offered_terms": terms or ["1", "2"], "prerequisites": prereqs,
                          "confidence": "catalog_verified" if known else "name_only", "manual": not known,
                          "_alias_norms": alias_groups.get(nn, [])})
        reqs.append({"label": "필수지정 미이수", "area": "전공", "priority": 1, "need": None, "items": items})
    # 2) 연계융합 부족 — 총 또는 '그룹별 최저' 미충족 시. 그룹별 quota를 먼저 보장하도록
    #    풀을 [부족그룹별 gap만큼 → 나머지] 순으로 구성(총 need만 채우다 한 그룹만 채움 방지).
    for cc in audit.convergence_checks:
        group_gaps = {g["group"]: g["gap"] for g in cc.get("group_checks", []) if g["gap"] > 0}
        need = max(cc.get("gap", 0.0), round(sum(group_gaps.values()), 1))
        if need <= 0:
            continue
        pool_all = [c for c in cc.get("courses", []) if not c["taken"] and not c.get("discontinued")]
        untaken, used_ids = [], set()
        for g, ggap in sorted(group_gaps.items()):
            acc_g = 0.0
            for c in sorted([x for x in pool_all if x.get("group") == g], key=lambda x: -x.get("credits", 0)):
                if acc_g >= ggap:
                    break
                untaken.append(c); used_ids.add(id(c)); acc_g += c.get("credits", 0)
        untaken += sorted([c for c in pool_all if id(c) not in used_ids], key=lambda c: -c.get("credits", 0))
        reqs.append({"label": f"{cc['name']} 부족", "area": "융합전공", "priority": 2, "need": need,
                     "pool": [{"name_ko": c["name_ko"], "course_id": c.get("course_id", ""),
                               "credits": c["credits"], "assignment": "융합전공",
                               "satisfies": f"{cc['name']} {c.get('group', '')}".strip(),
                               "offered_terms": c.get("offered_terms") or ["1", "2"],
                               "prerequisites": c.get("prerequisites") or [],
                               "confidence": "catalog_verified" if c.get("offered_terms") else "name_only",
                               "manual": not c.get("offered_terms")} for c in untaken]})
    # 3) 전공 부족 — 제1전공 카탈로그 미이수. 미이수 '필수(전공)' 학점은 갭에서 제외(중복선택 방지)
    major_gap = next((g.gap for g in audit.area_gaps if g.area == "전공"), 0.0)
    req_major_credits = sum(it["credits"] for r in reqs if r.get("label") == "필수지정 미이수"
                            for it in r.get("items", []) if r.get("area") == "전공")
    major_gap_eff = max(0.0, round(major_gap - req_major_credits, 1))
    if major_gap_eff > 0:
        confirmed_full = {c.course_id for c in verified.confirmed_courses if c.course_id}
        # choose-1 그룹 멤버는 전공 일반 풀에서 제외 — 필수지정(택1)로 1개가 이미 계획되는데
        # 미선택 sibling이 '전공 부족'으로 또 배치되는 이중 계획 방지(연도 라운드 검증, S3 재현)
        from graduation_center.v2.audit_v2 import _required_groups_for_year as _grps
        group_member_norms = {normalize_name(it["name"])
                              for g in _grps(profile.program_id, profile.admission_year)
                              for it in g.get("items", [])}
        pool = [{"name_ko": c.name_ko, "course_id": c.course_id, "credits": c.credits, "satisfies": "전공 부족",
                 "offered_terms": c.offered_terms, "prerequisites": c.prerequisites, "confidence": "catalog_verified"}
                for c in cat["courses"]
                # 7자리 전체 또는 이름으로 이수 제외(5자리 절단 충돌 — 예: 0365007/0365008 — 방지)
                if not ((c.course_id and c.course_id in confirmed_full) or normalize_name(c.name_ko) in confirmed_norm
                        or normalize_name(c.name_ko) in group_member_norms
                        or getattr(c, "discontinued", False))]
        reqs.append({"label": "전공 부족", "area": "전공", "priority": 3, "need": major_gap_eff, "pool": pool})
    # 4) 기초교양 — 영역 학점이 '부족할 때만' 계획(영역 총량 충족이면 이름 미매칭은 확인 항목일 뿐,
    #    phantom 12학점 추가 금지 — 라운드4 검증). 필수명이 있으면 그것으로, 없으면 슬롯으로.
    missing_basic = [g for g in (audit.gen_basic_courses or []) if not g["taken"]]
    basic_gap = next((g.gap for g in audit.area_gaps if g.area == "기초교양"), 0.0)
    if basic_gap > 0:
        if missing_basic:
            reqs.append({"label": "기초교양 필수", "area": "기초교양", "priority": 4, "need": basic_gap,
                         "pool": [{"name_ko": g["name_ko"], "credits": 3.0, "satisfies": "기초교양 필수",
                                   "confidence": "name_only", "manual": True} for g in missing_basic]})
        else:
            reqs.append({"label": "기초교양", "area": "기초교양", "priority": 4, "need": basic_gap,
                         "pool": _slot_chunks("기초교양 선택", basic_gap, "기초교양")})
    # 5) 핵심교양 영역별 부족 → 영역 슬롯(학기 분할). 단 핵심교양 '총량'이 이미 충족이면
    #    세부영역 부족은 과목명 미매핑(attribution 미확정)일 가능성이 높아 phantom 계획 금지.
    core_total_gap = next((g.gap for g in audit.area_gaps if g.area == "핵심교양"), 0.0)
    if core_total_gap > 0:
        for g in audit.core_area_gaps:
            if g.gap > 0:
                reqs.append({"label": f"핵심교양 {g.area}", "area": "핵심교양", "priority": 4, "need": g.gap,
                             "pool": _slot_chunks(f"핵심교양 {g.area} 선택", g.gap, f"핵심교양-{g.area}")})
    # 6) 자유교양 부족 → 슬롯(학기 분할)
    free_gap = next((g.gap for g in audit.area_gaps if g.area == "자유교양"), 0.0)
    if free_gap > 0:
        reqs.append({"label": "자유교양", "area": "자유교양", "priority": 4, "need": free_gap,
                     "pool": _slot_chunks("자유교양 선택", free_gap, "자유교양")})
    # 요건 → 후보 선택(quota 충족까지). 이름 중복 제거(필수지정이 전공부족 후보와 겹침 방지).
    # 풀이 quota를 못 채우면 unfillable로 기록(후보 고갈 → 거짓 '충족' 방지).
    selected: list[dict] = []
    seen: set[str] = set()
    unfillable: list[dict] = []
    for r in reqs:
        if r.get("items") is not None:
            for it in r["items"]:
                key = normalize_name(it["name_ko"])
                if key in seen:
                    continue
                seen.add(key)
                for ak in it.get("_alias_norms", []):   # 동치 신/구명도 다른 풀에서 재선택 금지
                    seen.add(ak)
                selected.append({**it, "area": r["area"], "priority": r["priority"]})
        else:
            acc = 0.0
            for it in r["pool"]:
                if acc >= r["need"]:
                    break
                key = normalize_name(it["name_ko"])
                if key in seen:
                    # 다른 요건이 이미 선택한 과목 — 이 요건 quota에 '가산하지 않는다'.
                    # (가산하면 융합 과목이 전공 quota를 거짓 충당해 중복인정 한도를 우회하고,
                    #  필수지정은 major_gap_eff에서 이미 차감돼 이중 차감이 됨 — 라운드4 검증)
                    continue
                seen.add(key); selected.append({**it, "area": r["area"], "priority": r["priority"]}); acc += it["credits"]
            if acc + 0.01 < r["need"]:
                unfillable.append({"label": r["label"], "area": r["area"], "shortfall": round(r["need"] - acc, 1)})

    # 총학점(일반선택) 잔여 — 선택된 후보 학점 + unfillable shortfall(그 부족분도 총학점을
    # 메우는 몫)을 차감한 뒤 산출. (안 빼면 같은 부족분 이중 계획 — 라운드5 검증)
    sel_cr = round(sum(it["credits"] for it in selected), 1)
    uf_cr = round(sum(u["shortfall"] for u in unfillable), 1)
    general_need = round(max(0.0, audit.total_gap - sel_cr - uf_cr), 1)
    if general_need > 0:
        # 졸업인증제(제96조의2): 다·부전공/융합 미신청자는 심화전공(전공 최저+18)이 사실상 필수
        # (2026-06-05 사용자 지적) — 일반선택 익명 슬롯 대신 **전공선택 실과목**으로 잔여를 우선
        # 충당해 추천. 목표·feasibility는 불변(인증제는 면제 전형이 많아 hard 요건화하지 않음 —
        # 기존 철학), 추천 구성만 바뀜. 전공 후보 소진 시 잔여는 일반선택 슬롯 유지.
        if not audit.convergence_checks:
            g_major = next((g for g in audit.area_gaps if g.area == "전공"), None)
            planned_major = round(sum(it["credits"] for it in selected if it.get("area") == "전공"), 1)
            from graduation_center.v2.catalog import deep_major_extra
            extra = deep_major_extra(profile.admission_year)
            deep_need = (round((g_major.required + extra) - g_major.earned - planned_major, 1)
                         if g_major is not None else 0.0)
            taken_keys = {(it.get("course_id") or it["name_ko"]) for it in selected}
            confirmed_full2 = {c.course_id for c in verified.confirmed_courses if c.course_id}
            deep_pool = [{"name_ko": c.name_ko, "course_id": c.course_id, "credits": c.credits,
                          "satisfies": f"심화전공 권장(+{extra:g})", "offered_terms": c.offered_terms,
                          "prerequisites": c.prerequisites, "confidence": "catalog_verified"}
                         for c in cat["courses"]
                         if not getattr(c, "discontinued", False)
                         and c.course_id not in confirmed_full2
                         and normalize_name(c.name_ko) not in confirmed_norm
                         and (c.course_id or c.name_ko) not in taken_keys]
            acc_d = 0.0
            for it in deep_pool:
                if acc_d >= min(deep_need, general_need) - 0.01:
                    break
                selected.append({**it, "area": "전공", "priority": 5})
                acc_d = round(acc_d + it["credits"], 1)
            general_need = round(max(0.0, general_need - acc_d), 1)
    if general_need > 0:
        gen_req = {"label": "총학점(일반선택)", "area": "일반선택", "priority": 5, "need": general_need,
                   "pool": _slot_chunks("일반선택 과목", general_need, "일반선택")}
        reqs.append(gen_req)
        for it in gen_req["pool"]:
            selected.append({**it, "area": "일반선택", "priority": 5})
    return selected, reqs, unfillable


def plan_greedy(selected: list[dict], terms: list[list],
                completed: set | None = None) -> tuple[list[RoadmapTerm], list[str], list[dict]]:
    """선택 후보를 학기에 배치. 우선순위·개설학기(아는 경우)·학점상한 + 선수과목 순서(위상).
    선수과목은 이미 이수(completed prefix)거나 '더 이른' 학기에 배치돼야 같은/이후 학기에 둔다.
    반환 (terms, assumptions, unplaced)."""
    completed = set(completed or set())                 # 이수 과목 코드 앞5자리
    items = sorted(selected, key=lambda c: (c["priority"], -c.get("credits", 0)))
    used = {lab: 0.0 for lab, _ in terms}
    bucket: dict[str, list] = {lab: [] for lab, _ in terms}
    placed_at: dict[str, int] = {}                      # course_id(앞5자리) → term index
    unplaced: list[dict] = []

    prereq_warn: list[str] = []
    placed_norm: dict[str, int] = {}                    # 과목명(정규화) → 배치 term index
    item_norms = {normalize_name(c["name_ko"]) for c in items}

    def _seq_prev(it):
        """Ⅰ/Ⅱ류 번호 시퀀스: 끝자리 숫자 n≥2면 같은 베이스의 n-1 정규화명 반환.

        generic_slot('일반선택 과목 #2' 등)은 제외 — 슬롯 번호는 단순 분할 인덱스라
        시퀀스로 오인하면 후속 슬롯이 전부 defer→미배치되어 초과학기를 과대 산정한다."""
        if it.get("confidence") == "generic_slot":
            return None
        nn = normalize_name(it["name_ko"])
        if nn and nn[-1].isdigit() and int(nn[-1]) >= 2:
            return nn[:-1] + str(int(nn[-1]) - 1)
        return None

    def _try_place(it):
        pres = [p[:5] for p in (it.get("prerequisites") or []) if p]
        # 선수 중 selected에 있는데 아직 미배치면 보류(나중 패스)
        sel_pref = {c.get("course_id", "")[:5] for c in items if c.get("course_id")}
        if any(p not in completed and p not in placed_at and p in sel_pref for p in pres):
            return None                                 # defer
        # 번호 시퀀스(캡스톤Ⅰ→Ⅱ 등): 같은 계획 안의 이전 번호보다 이른 학기 금지
        seq_min = 0
        prev = _seq_prev(it)
        if prev and prev in item_norms:
            if prev in placed_norm:
                seq_min = placed_norm[prev] + 1
            else:
                return None                             # 이전 번호 미배치 → defer
        # 선수가 이수도·후보도 아님 → 배치는 하되 '선수 확인 필요' 플래그(연도 코드 드리프트로
        # 실제로는 이수했을 수 있어 하드 차단하지 않음 — 라운드4 절충)
        if any(p not in completed and p not in placed_at and p not in sel_pref for p in pres):
            it["manual"] = True
            prereq_warn.append(it["name_ko"])
        min_idx = max(0, seq_min)                       # 배치된 선수·시퀀스 이전 번호보다 뒤 학기
        for p in pres:
            if p in placed_at:
                min_idx = max(min_idx, placed_at[p] + 1)
        off = it.get("offered_terms")
        known = it.get("confidence") == "catalog_verified"
        for allow_seasonal in (False, True):
            for i, (lab, cap) in enumerate(terms):
                if i < min_idx:
                    continue
                sem = _term_sem(lab)
                if known and off and sem not in off:
                    continue
                if not known and sem in ("S", "W") and not allow_seasonal:
                    continue
                if used[lab] + it["credits"] <= cap + 0.01:
                    bucket[lab].append(it); used[lab] += it["credits"]
                    if it.get("course_id"):
                        placed_at[it["course_id"][:5]] = i
                    placed_norm[normalize_name(it["name_ko"])] = i
                    return True
        return False                                    # 배치 실패(용량/개설학기)

    pending = list(items)
    progress = True
    while pending and progress:
        progress = False
        still = []
        for it in pending:
            r = _try_place(it)
            if r is True:
                progress = True
            elif r is None:
                still.append(it)                        # 선수 미배치 → 다음 패스
            else:
                unplaced.append(it)
        pending = still
    unplaced.extend(pending)                            # 교착(순환/충족불가) → 미배치
    out = []
    for lab, cap in terms:
        if not bucket[lab]:
            continue
        courses = [RoadmapCourse(course_id=it.get("course_id", "") or "", name_ko=it["name_ko"],
                                 credits=it["credits"], satisfies=it.get("satisfies", ""),
                                 assignment=it.get("assignment", ""),
                                 offered_terms=(it.get("offered_terms") or []) if it.get("confidence") == "catalog_verified" else [],
                                 confidence=it.get("confidence", "catalog_verified"),
                                 manual_check=it.get("manual", False)) for it in bucket[lab]]
        out.append(RoadmapTerm(term=lab, courses=courses, term_credits=round(used[lab], 1),
                               # high: 상한 만재(float 안전 임계 — codex R2) — 표시용 산출(배치 로직 아님)
                               term_risk=("high" if used[lab] >= cap - 0.01
                                          else "medium" if used[lab] > cap - 3 else "low")))
    assumptions = []
    if any(str(it.get("satisfies", "")).startswith("심화전공 권장") for it in selected):
        assumptions.append("다·부전공 미신청 → 졸업인증제 충족을 위해 심화전공(전공 최저+추가학점) 기준으로 "
                           "전공 과목을 추천했습니다 — 면제 전형·교직·공학인증 해당 시 학과 확인.")
    if any(it.get("manual") for it in selected):
        assumptions.append("이름기준·교양 슬롯 과목은 개설학기·학점을 수강신청 전 확인하세요.")
    if prereq_warn:
        assumptions.append(f"선수과목 이수 여부 확인 필요: {', '.join(sorted(set(prereq_warn))[:6])}")
    return out, assumptions, unplaced


def run_planner(
    audit: AuditResult, profile: RequirementProfile, context: StudentContext,
    verified: VerifiedTranscript, client=None,
) -> tuple[RoadmapPlan, ValidationReport, dict]:
    """결정론 통합 플래너: 남은 의무 → 단일 후보 풀 → greedy 학기배치 → 검증.
    (LLM 배치 미사용 — codex 권장. plan_roadmap 등 LLM 함수는 보존만.)"""
    selected, reqs, unfillable = build_unified_candidates(audit, profile, verified)
    summary = [{"label": r["label"], "area": r["area"],
                "need": (r.get("need") if r.get("need") is not None else sum(i["credits"] for i in r.get("items", [])))}
               for r in reqs]
    ctx = {"sources": [], "requirements_summary": summary}
    # 학기당 상한: 사용자 override는 법정 상한(제32조) 이내로 클램프. 0/음수/None → 법정값.
    legal_cap = regular_term_cap(profile.total_credits_min)
    user_cap = context.max_credits_per_term
    reg_cap = min(float(user_cap), legal_cap) if (user_cap and user_cap > 0) else legal_cap

    if reqs and not selected:
        # 요건은 남았는데 채울 후보가 전혀 없음(풀 고갈) → 거짓 '충족' 금지
        labs = ", ".join(r["label"] for r in reqs)
        return (RoadmapPlan(status="blocked", feasible=False, terms=[],
                            blocked_reason=f"미충족 요건({labs})을 채울 수강 후보가 없습니다 — 학과 확인 필요."),
                ValidationReport(ok=False, errors=[ValidationError(code="no_candidates", detail=labs)]), ctx)
    if not selected:
        return (RoadmapPlan(status="generated", feasible=True, terms=[],
                            why_this_plan="졸업요건을 모두 충족했습니다. 추가 수강 계획이 필요 없습니다."),
                ValidationReport(ok=True), ctx)

    sel_credits = round(sum(it["credits"] for it in selected), 1)
    terms = _ordered_terms(context, reg_cap)
    if not terms:
        names = ", ".join(f"{it['name_ko']}({it['credits']:.0f})" for it in selected[:12])
        if context.current_term and context.remaining_semesters <= 0:
            # 현재 학기는 있으나 잔여 정규학기 0 → 배치 불가 = 정직한 blocked + 초과학기
            ov = _overflow_from_credits(sel_credits, context, profile, reg_cap)
            return (RoadmapPlan(status="blocked", feasible=False, terms=[], overflow=ov,
                                blocked_reason=f"잔여 학기 0 — 남은 의무 {sel_credits:.0f}학점 이수 불가.",
                                relaxation_hint=(ov.note if ov else "초과학기가 필요합니다.")),
                    ValidationReport(ok=False, errors=[ValidationError(code="no_terms", detail="잔여 학기 0")]), ctx)
        # 현재 학기 미입력/형식 오류 → 학기 배치 보류(미상). 권장 과목만 안내.
        return (RoadmapPlan(status="generated", feasible=None, terms=[],
                            why_this_plan=f"현재 학기 미입력 또는 형식 오류(예: 2026-1) — 학기 배치 생략. 추가 이수 권장: {names}",
                            assumptions=["현재 학기를 올바른 형식(연도-학기)으로 입력하면 학기별 배치를 제공합니다."]),
                ValidationReport(ok=True), ctx)

    completed = {c.course_id[:5] for c in verified.confirmed_courses if c.course_id}
    placed, assumptions, unplaced = plan_greedy(selected, terms, completed)

    # 결정론 검증: 학점상한 + 미배치(선수/개설/용량으로 못 넣음). 미배치 있으면 ok=False·blocked.
    errors = []
    for t in placed:
        cap = next((c for lab, c in terms if lab == t.term), reg_cap)
        if t.term_credits > cap + 0.01:
            errors.append(ValidationError(code="over_credit_cap", detail=f"{t.term} {t.term_credits:.0f}>{cap:.0f}"))
    for it in unplaced:
        errors.append(ValidationError(code="unplaced", detail=it["name_ko"], course_id=it.get("course_id") or None))
    for uf in unfillable:
        errors.append(ValidationError(code="pool_exhausted", detail=f"{uf['label']} {uf['shortfall']:.0f}학점 후보 부족"))
    # 영역(이수구분) 커버리지 검증 — 배치된 후보가 전공 floor 갭을 실제로 닫는지(안전망)
    unplaced_ids = {id(it) for it in unplaced}
    placed_by_area: dict = {}
    for it in selected:
        if id(it) in unplaced_ids:
            continue
        placed_by_area[it["area"]] = placed_by_area.get(it["area"], 0.0) + it["credits"]
    for g in audit.area_gaps:
        # 같은 area의 unfillable(후보 고갈)로 이미 보고된 경우만 억제(이름 substring 매칭 금지)
        if g.area in MAJOR_AREAS and g.gap > 0 and placed_by_area.get(g.area, 0.0) + 0.01 < g.gap \
                and not any(u.get("area") == g.area for u in unfillable):
            errors.append(ValidationError(code="gap_not_closed",
                          detail=f"{g.area} 계획 {placed_by_area.get(g.area, 0):.0f} < 부족 {g.gap:.0f}"))
    report = ValidationReport(ok=not errors, errors=errors)
    gap_errors = [e for e in errors if e.code == "gap_not_closed"]

    parts = [s["label"] for s in summary]
    why = "남은 요건(" + ", ".join(parts) + ")을 잔여 학기에 개설학기·선수·학점상한을 지켜 배치했습니다." if parts else ""
    if unplaced or unfillable or gap_errors:
        # blocked. 초과학기는 '용량/개설/선수로 잔여 학기에 못 넣은' unplaced 학점만 환산 —
        # unfillable(후보 고갈)은 학기를 늘려도 해결 불가이므로 별도 메시지(라운드4 검증).
        un = ", ".join(f"{it['name_ko']}({it['credits']:.0f})" for it in unplaced)
        uf = ", ".join(f"{u['label']}(-{u['shortfall']:.0f})" for u in unfillable)
        unplaced_cr = round(sum(it["credits"] for it in unplaced), 1)
        ov = _overflow_from_credits(unplaced_cr, context, profile, reg_cap)
        if ov and any(it.get("confidence") == "catalog_verified" and it.get("offered_terms") not in (None, ["1", "2"])
                      for it in unplaced):
            ov.note += " (개설학기 제약으로 실제 필요 학기는 더 늘 수 있음)"
        reason = "잔여 학기에 다 배치하지 못한 과목: " + un if un else ""
        if uf:
            reason += (" · " if reason else "") + f"수강 후보 없음(초과학기로 해결 불가, 학과 확인 필요): {uf}"
        if gap_errors:
            reason += (" · " if reason else "") + "영역 부족 미해소: " + ", ".join(e.detail for e in gap_errors)
        if ov:
            hint = ov.note
        elif unfillable and not unplaced:
            # '학기 늘리세요'는 후보 고갈엔 자기모순 — 메시지 일관화(라운드5 검증)
            hint = "수강 후보가 없는 요건은 초과학기로 해결되지 않습니다 — 학과/교육과정 확인이 필요합니다."
        else:
            hint = "잔여 학기를 늘리거나 계절학기를 활용하세요."
        # 미배치 영역별 집계 — unplaced(용량·개설학기) + unfillable(후보 고갈) 합산(적대 R1 §10).
        # blocked_reason 문자열은 이 집계와 같은 원천(unplaced·unfillable)에서 조립 — 이중 진실 없음.
        upa: dict[str, float] = {}
        for it in unplaced:
            upa[it.get("area") or "기타"] = round(upa.get(it.get("area") or "기타", 0) + it["credits"], 1)
        for u in unfillable:
            upa[u.get("area") or "기타"] = round(upa.get(u.get("area") or "기타", 0) + u["shortfall"], 1)
        # 계절 허용인데 카탈로그 과목이 미배치로 남은 경우 — 본 모델은 전공·지정 과목의
        # 계절 개설을 보장하지 않아 정규 학기에만 배치(보수). 실제 계절 개설되면 초과 없이
        # 가능할 수 있으므로 그 가능성을 정직하게 안내(2026-06-05 사용자 지적: '계절로 되는데 왜 초과?')
        if context.seasonal_semester_allowed and any(
                it.get("confidence") == "catalog_verified" for it in unplaced):
            names_un = ", ".join(it["name_ko"] for it in unplaced
                                 if it.get("confidence") == "catalog_verified")
            assumptions.append(f"전공·지정 과목({names_un})은 계절학기 개설이 보장되지 않아 정규 학기에만 "
                               "배치했습니다 — 해당 과목이 계절학기에 개설되면 초과학기 없이 졸업 가능할 수 "
                               "있으니 개설 공지를 확인하세요.")
        plan = RoadmapPlan(status="blocked", feasible=False, terms=placed, why_this_plan=why,
                           assumptions=assumptions, overflow=ov, unplaced_by_area=upa,
                           blocked_reason=reason, relaxation_hint=hint)
    else:
        # 전부 배치됨(feasible) → 초과학기 카드는 모순이므로 표시하지 않음(단일 용량 모델)
        plan = RoadmapPlan(status="generated", feasible=True, terms=placed,
                           why_this_plan=why, assumptions=assumptions, overflow=None)
    return plan, report, ctx
