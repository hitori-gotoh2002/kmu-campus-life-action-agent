"""졸업센터 v2 Pydantic 스키마 (Bounded Audit Agent).

데이터 계약: 결정론 노드가 사실(matching·audit·risk)을 채우고, LLM은 RoadmapPlan만
생성하며, 검증기가 RoadmapPlan을 재확인한다. AuditPipelineResponse가 JSON-first 응답.
"""
from __future__ import annotations

from typing import Literal

import math
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 졸업요건 영역 (집계 카테고리). 융합전공은 연계·융합전공 카탈로그 과목 표시용.
Area = Literal["전공", "기초교양", "핵심교양", "자유교양", "일반선택", "융합전공"]
GpaMinStatus = Literal["yes", "no", "unknown"]


# ---------- 입력 컨텍스트 ----------
class StudentContext(BaseModel):
    program_id: str                                  # 예: ai_bigdata, mirae_mobility
    admission_year: int | None = None
    current_term: str | None = None                  # "2026-1" 등; None이면 로드맵 시작점 미상
    remaining_semesters: int = Field(default=2, ge=0, le=12)
    seasonal_semester_allowed: bool = False
    # 학기당 제약은 '학점 상한'만 사용(과목 수 상한은 설계 전환으로 폐기 — 2026-06 결정)
    max_credits_per_term: float | None = None        # 사용자 override; None이면 학사규정 제32조로 산출
    prev_term_gpa_ge_375: bool = False               # 직전학기 평점 3.75↑ → 첫 학기 +3학점(제32조)
    preferences: list[str] = Field(default_factory=list)
    gpa_min_met: GpaMinStatus = "unknown"            # 엑셀에 성적 없음 → 사용자 선언
    convergence_program_ids: list[str] = Field(default_factory=list)  # 연계·융합전공 — 사용자 입력
    convergence_tracks: dict[str, str] = Field(default_factory=dict)  # program_id → "다전공"|"부전공"
    masked_student_id: str | None = None             # 표시용(뒷자리 마스킹)

    @field_validator("current_term", mode="before")
    @classmethod
    def _valid_term(cls, v):
        """형식 오류('26-1','2026','2026-3' 등)는 None으로 정규화 — 오타가 2자리 연도 라벨이나
        침묵 '미입력' 경로로 새지 않게(플래너가 '미입력/형식 오류'를 명시 안내)."""
        if v is None or v == "":
            return None
        return v if re.match(r"^20\d{2}-(1|2|S|W)$", str(v)) else None


# ---------- 카탈로그 ----------
class CatalogCourse(BaseModel):
    course_id: str                                   # 교과목코드 7자리
    name_ko: str
    name_norm: str = ""
    aliases: list[str] = Field(default_factory=list)
    credits: float = 0.0
    requirement_area: Area = "전공"
    is_required: bool = False                        # 요람 비고 '필수지정'
    prerequisites: list[str] = Field(default_factory=list)        # course_id
    prereq_external: list[str] = Field(default_factory=list)      # 미해소 선수(이름) → 확인 필요
    grade_level: int | None = None
    offered_terms: list[str] = Field(default_factory=lambda: ["1", "2"])
    discontinued: bool = False                       # 폐지 과목 — 기이수 인정은 유지, 로드맵 추천만 제외
    group: str | None = None                         # 연계융합전공 그룹(A그룹/B그룹) — 그룹별 최저 체크용
    source: dict = Field(default_factory=dict)


# ---------- 파싱/매칭 ----------
class RawLine(BaseModel):
    """수강내역 .xls 한 행."""
    course_code: str = ""
    course_name: str = ""
    area_raw: str = ""                               # 이수구분 원문(전공선택/기초교양...)
    credits: float = 0.0
    section: str = ""                                # 분반
    term_label: str = ""                             # 파일/헤더에서 온 학기 라벨
    professor: str = ""
    note: str = ""                                   # 비고


class CourseMatch(BaseModel):
    raw: RawLine
    matched_course_id: str | None = None
    match_by: Literal["code", "name", "none"] = "none"
    status: Literal["matched", "aggregate_only", "unresolved"] = "unresolved"
    requirement_area: Area | None = None             # 확정 영역(매칭/이수구분 유래)
    core_area: str | None = None                     # 핵심교양 세부영역(인문Ⅰ..)


# ---------- 검증 ----------
class VerifiedCourse(BaseModel):
    course_id: str | None = None
    name_ko: str
    credits: float
    requirement_area: Area
    core_area: str | None = None
    term_label: str = ""
    included: bool = True
    exclude_reason: str | None = None                # 폐강 / F / NP / 재수강중복
    aggregate_only: bool = False                     # 카탈로그 밖(교양·타과) → 집계만
    grade_suspect: bool = False                      # 비고에 F/NP/W류 표기 — 침묵 산입 방지 경고(자동 제외 아님)
    demoted_from_major: bool = False                 # 전공계 이수구분이었으나 카탈로그 미스로 일반선택 강등
                                                     # — HITL에서 사용자가 전공으로 복구 가능(표시·일괄용)

    @field_validator("credits", mode="before")
    @classmethod
    def _finite_credits(cls, v):
        """오염 입력(inf/1e308/거대 음수/bool)이 합산→inf→JSON null·플래너 폭주로 번지지
        않게 입구에서 거부(0~30 유한 숫자만) — 라우트가 ValueError→400으로 매핑.
        mode=before: pydantic이 bool을 float로 coerce(True→1.0)하기 전에 차단."""
        if isinstance(v, bool):
            raise ValueError(f"학점 값이 유효하지 않습니다: {v}")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"학점 값이 유효하지 않습니다: {v}") from None
        if not math.isfinite(f) or f < 0 or f > 30:
            raise ValueError(f"학점 값이 유효하지 않습니다: {v}")
        return f


class VerifiedTranscript(BaseModel):
    confirmed_courses: list[VerifiedCourse] = Field(default_factory=list)
    excluded: list[VerifiedCourse] = Field(default_factory=list)
    unresolved: list[CourseMatch] = Field(default_factory=list)
    possible_retakes: list[dict] = Field(default_factory=list)    # {code, name, term_labels[]}
    earned_by_area: dict[str, float] = Field(default_factory=dict)
    core_area_earned: dict[str, float] = Field(default_factory=dict)
    total_earned: float = 0.0


# ---------- 요건 프로파일 ----------
class RequirementProfile(BaseModel):
    program_id: str
    department_name_ko: str = ""
    admission_year: int | None = None
    total_credits_min: float = 0.0
    area_min: dict[str, float] = Field(default_factory=dict)      # {전공,기초교양,핵심교양,자유교양,일반선택}
    required_course_ids: list[str] = Field(default_factory=list)
    core_area_min: float = 3.0
    core_area_min_overrides: dict[str, float] = Field(default_factory=dict)  # 별표5 영역별 override(예: 소통 5)
    core_total_min: float = 15.0
    applied_yoram: str = "2025 요람"


# ---------- 진단 ----------
class AreaGap(BaseModel):
    area: str
    required: float
    earned: float
    gap: float


class AuditResult(BaseModel):
    total_required: float
    total_earned: float                              # 졸업학점 '인정' 학점(교양 50 상한 반영)
    gyo_over_cap: float = 0.0                        # 교양(기초+핵심+자유) 50학점 초과 불인정분(제7조⑧)
    total_gap: float
    area_gaps: list[AreaGap] = Field(default_factory=list)
    core_area_gaps: list[AreaGap] = Field(default_factory=list)   # 핵심교양 영역별
    missing_required_course_ids: list[str] = Field(default_factory=list)
    missing_required_names: list[str] = Field(default_factory=list)
    missing_required_display: list[str] = Field(default_factory=list)  # "개정이름(구. 옛이름)" — 표시 전용(매칭은 names)
    required_check_available: bool = True   # 요람 필수지정 데이터 구축 여부
    gen_basic_courses: list[dict] = Field(default_factory=list)   # 기초교양 필수 [{name_ko,taken}]
    to_fusion_total: float = 0.0                                  # 융합 배정으로 전공에서 차감된 학점(과목 dedup)
    convergence_checks: list[dict] = Field(default_factory=list)  # [{program_id,name,required,earned,gap,matched_courses}]
    unresolved_credits: float = 0.0


# ---------- 리스크 ----------
class RiskReason(BaseModel):
    factor: str
    detail: str
    severity: int = 0


class RiskAssessment(BaseModel):
    grade: Literal["S", "A+", "A", "B", "C", "D"]    # 졸업 여유도 사다리(2026-06-05)
    label: str
    score: int = 0
    reasons: list[RiskReason] = Field(default_factory=list)


# ---------- 로드맵 ----------
class RoadmapCourse(BaseModel):
    course_id: str = ""                              # 슬롯/이름기준 후보는 빈 값
    name_ko: str = ""
    credits: float = 0.0
    satisfies: str = ""                              # 영역/필수 (예: 필수지정, 전공 부족, 융합 A그룹)
    reason: str = ""
    assignment: str = ""                             # 융합 과목 이수구분(중복인정/제1전공/융합전공) 등
    offered_terms: list[str] = Field(default_factory=list)   # 개설학기(아는 경우; 예: ["1"],["1","2"])
    confidence: Literal["catalog_verified", "name_only", "generic_slot"] = "catalog_verified"
    manual_check: bool = False                       # 개설학기·학점 확인 필요(이름기준/슬롯)
    source_ids: list[str] = Field(default_factory=list)


class RoadmapTerm(BaseModel):
    term: str
    courses: list[RoadmapCourse] = Field(default_factory=list)
    term_credits: float = 0.0
    term_risk: Literal["low", "medium", "high"] = "low"
    notes: list[str] = Field(default_factory=list)


class OverflowScenario(BaseModel):
    """잔여 학기로 졸업이 불가능할 때의 초과학기 예상(결정론 산출)."""
    shortfall_credits: float                          # 부족 학점(총 졸업학점 대비)
    per_term_credit_cap: float                        # 학기당 이수학점 상한(제32조)
    remaining_semesters: int                          # 현재 잔여 정규학기
    total_semesters_needed: int                       # 졸업까지 필요한 총 정규학기
    extra_semesters: int                              # 초과학기 수(= 필요 - 잔여)
    projected_graduation_term: str | None = None      # 예상 졸업 학기 라벨
    note: str = ""


class RoadmapPlan(BaseModel):
    status: Literal["generated", "not_generated", "blocked"] = "not_generated"
    feasible: bool | None = None
    terms: list[RoadmapTerm] = Field(default_factory=list)
    why_this_plan: str = ""
    blocked_reason: str | None = None
    relaxation_hint: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    overflow: OverflowScenario | None = None          # 잔여 학기 초과 시 예상 시나리오
    unplaced_by_area: dict[str, float] = Field(default_factory=dict)  # blocked 시 영역별 미배치(unfillable 포함)


class ValidationError(BaseModel):
    code: str
    detail: str
    course_id: str | None = None


class ValidationReport(BaseModel):
    ok: bool = True
    errors: list[ValidationError] = Field(default_factory=list)
    repair_attempted: bool = False


# ---------- 근거 / 응답 ----------
class Source(BaseModel):
    id: str                                          # G1.. (결정론 근거) / Y1.. (요람 RAG chunk)
    doc: str = ""
    page: int | None = None
    source_type: Literal["requirement_rule", "catalog_course", "gen_ed", "yoram_rag"] = "requirement_rule"
    ref: str | None = None
    url: str | None = None                           # 원문 링크(요람 PDF·규정집·공지) — 클릭 시 새 탭                           # rule area or course_id


# ---------- 규정 근거 해설 (보고서 내장 RAG — 챗 UI 없음) ----------
class ExplainLine(BaseModel):
    text: str
    source_ids: list[str] = Field(default_factory=list)   # Y1.. (검증된 인용만)
    grounded: bool = True                                  # False면 '요람 원문에서 직접 확인되지 않음' 표시됨


class ExplainSection(BaseModel):
    key: str                                               # missing_required / conv:<pid> / area:<영역> / core_areas
    title: str
    lines: list[ExplainLine] = Field(default_factory=list)
    deterministic: bool = False                            # True면 LLM 미경유(결정론 합성) — 프론트 '결정론' 칩


class NodeTraceEvent(BaseModel):
    node: str
    status: Literal["ok", "warn", "skip", "fail"] = "ok"
    summary: str = ""
    kind: Literal["tool", "llm", "hitl", "validator", "branch"] = "tool"
    branch_taken: str | None = None      # 실행된 분기(예: 통과 / repair 1회 / blocked / 갭없음)


# ---------- 에이전트 총평 (능동 시나리오 탐색 — bounded ReAct 단일 턴) ----------
# LLM은 "무엇을 알아볼지"(후보 delta·reason)와 총평 서사만 — 값·판정은 전부 결정론.
# 탈락 후보도 사유와 함께 보존(candidates_review) — "필터가 답을 정했다" 인상 차단(교수 R3).
class ScenarioReview(BaseModel):
    label: str                                       # 사람용 요약("계절학기 6학점 허용")
    reason_code: str
    rationale: str = ""                              # LLM의 선정 이유(왜 이 갈림길·이 값인가)
    verdict: Literal["accepted", "rejected"]
    # sim_cap: 시뮬 상한(4) 밖이라 미실행 — '효과 없음'과 구분(거짓 사유 금지, codex R1)
    # accept_cap: 효과는 있었으나 채택 상한(3) 초과 — 동일 취지
    rejected_by: Literal["pre_mismatch", "invalid_delta", "no_op", "post_no_change",
                         "sim_cap", "accept_cap"] | None = None


class ScenarioOutcome(BaseModel):
    """채택 시나리오의 결정론 관찰값 — build_diff 구조화 필드만(headline 텍스트 재사용 금지)."""
    id: str                                          # S1..
    label: str
    reason_code: str
    rationale: str = ""
    applied_changes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risk_before: str = ""
    risk_after: str = ""
    total_gap_before: float = 0.0
    total_gap_after: float = 0.0
    graduation_term_before: str | None = None
    graduation_term_after: str | None = None
    feasible_after: bool | None = None
    overflow_after: bool = False
    effect_label: str = ""                           # 결정론 합성("리스크 C→B 개선" 등) — 프론트 추측 금지(codex)
    effect_kind: Literal["improve", "worsen", "neutral"] = "neutral"


class SummaryLine(BaseModel):
    text: str
    fact_ids: list[str] = Field(default_factory=list)   # F*/S* — validator가 해소·수치 대조


class AgentSummary(BaseModel):
    headline: str = ""
    lines: list[SummaryLine] = Field(default_factory=list)
    recommendation: str = ""                         # 유지 권장 / 변경 검토 / 학과 상담 권장
    scenarios: list[ScenarioOutcome] = Field(default_factory=list)
    candidates_review: list[ScenarioReview] = Field(default_factory=list)
    facts: list[dict] = Field(default_factory=list)  # F* 근거(화면 fact 배지 토글용)


class AuditPipelineResponse(BaseModel):
    context: StudentContext
    verified_transcript: VerifiedTranscript
    audit: AuditResult
    risk: RiskAssessment
    roadmap: RoadmapPlan
    explanations: list[ExplainSection] = Field(default_factory=list)  # 규정 근거 해설(보고서 내장)
    explain_fallback: str | None = None              # 해설 생성 불가 사유(결정론 본체는 무영향)
    agent_summary: AgentSummary | None = None        # 에이전트 총평 — run_summary=True 경로에서만
    summary_fallback: str | None = None              # 총평 생성 불가 사유(그래프-카드 모순 방지)
    sources: list[Source] = Field(default_factory=list)
    node_trace: list[NodeTraceEvent] = Field(default_factory=list)
    report_markdown: str = ""


# ---------- 졸업 시나리오 상담 Agent (What-if) ----------
# LLM은 자연어 질문 → WhatIfDelta 변환(매개변수 추출기)만 하고,
# 판정은 기존 결정론 파이프라인(run_audit) 재실행이 한다. 예제06 Tool Calling 패턴.
WhatIfCategory = Literal["휴학", "수강학기변경", "계절학기", "학점상한", "다전공변경", "성적우수", "기타"]


class ConvChange(BaseModel):
    model_config = ConfigDict(extra="forbid")        # 캐시 오염·구버전 raw의 미지 필드 차단(검증 코드R1)
    program_id: str
    track: Literal["다전공", "부전공"] = "다전공"


class WhatIfDelta(BaseModel):
    """LLM 출력의 유일한 통로 — 전 필드 None/빈 리스트 = 변경 없음.
    범위 밖 값은 ValidationError → 호출측이 unsupported로 degrade."""
    model_config = ConfigDict(extra="forbid")
    calendar_delay_terms: int | None = Field(default=None, ge=0, le=4)   # 휴학: 시작만 지연
    remaining_semesters_change: int | None = Field(default=None, ge=-4, le=4)
    seasonal_semester_allowed: bool | None = None
    max_credits_per_term: float | None = Field(default=None, gt=0, le=24)
    prev_term_gpa_ge_375: bool | None = None
    add_convergence: list[ConvChange] = Field(default_factory=list)
    drop_convergence: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return (self.calendar_delay_terms in (None, 0)
                and self.remaining_semesters_change in (None, 0)
                and self.seasonal_semester_allowed is None
                and self.max_credits_per_term is None
                and self.prev_term_gpa_ge_375 is None
                and not self.add_convergence and not self.drop_convergence)


class WhatIfDiff(BaseModel):
    """before/after 결정론 비교 — 전 필드 f-string 조립(LLM 미사용)."""
    risk_before: str
    risk_after: str
    risk_label_before: str = ""
    risk_label_after: str = ""
    total_gap_before: float = 0.0
    total_gap_after: float = 0.0
    feasible_before: bool | None = None
    feasible_after: bool | None = None
    graduation_term_before: str | None = None
    graduation_term_after: str | None = None
    already_met_after: bool = False                  # terms=[] + feasible=True
    overflow_before: bool = False
    overflow_after: bool = False
    changed_areas: list[str] = Field(default_factory=list)
    convergence_changes: list[str] = Field(default_factory=list)
    headline: str = ""


class WhatIfResponse(BaseModel):
    status: Literal["ok", "unsupported"]
    category: WhatIfCategory | None = None
    question_summary: str = ""
    unsupported_reason: str | None = None
    applied_changes: list[str] = Field(default_factory=list)   # "잔여학기 4→3" 등 사람용
    assumptions: list[str] = Field(default_factory=list)
    diff: WhatIfDiff | None = None
    next_actions: list[str] = Field(default_factory=list)
    after: AuditPipelineResponse | None = None       # ⚠️ after.node_trace는 그래프 합성 금지(§계약)
    node_trace: list[NodeTraceEvent] = Field(default_factory=list)
