"""에이전트 총평 — 능동 시나리오 탐색 (bounded ReAct, 단일 턴).

LLM의 결정권은 두 곳뿐: ① 갈림길 선정(facts를 보고 what-if 후보 ≤5개와 delta 파라미터 값을
스스로 제안 — Thought) ② 관찰값 기반 비교 총평 서사(Answer). 시뮬레이션 실행(Action)·diff
관찰(Observation)·pre/post 관련성 판정은 전부 결정론 — 기존 whatif 도구(apply_delta·run_audit
재실행·build_diff)를 그대로 재사용한다. 값(학점·판정·날짜)은 LLM이 한 글자도 만들지 않는다.

탈락 후보는 소거하지 않고 사유와 함께 candidates_review로 보존 — "필터가 답을 정해놓고
LLM이 받아 적는다" 인상 차단(교수 페르소나 R3 비타협 사항).

순환 import 차단: pipeline.run_audit는 함수 주입(run_audit_fn)으로 받는다 —
pipeline → (lazy) report_summary → whatif → pipeline 의 top-level 순환을 끊는 설계.
계획·검증 대장: docs/plans/agentic_scenario_summary_plan.md (4라운드 양 렌즈 수렴).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from graduation_center.v2.models_v2 import (
    AgentSummary, NodeTraceEvent, ScenarioOutcome, ScenarioReview, StudentContext,
    SummaryLine, WhatIfDelta,
)
from graduation_center.v2.whatif import (
    _candidates, apply_delta, build_diff, whatif_delta_schema,
)

CACHE_PATH = Path(__file__).resolve().parents[1] / "data/graduation/v2/summary_cache.json"
SUMMARY_SCHEMA_VERSION = 7        # 프롬프트·schema·필터 규칙 변경 시 +1 — 구 엔트리 자동 미스
MAX_CANDIDATES = 5                # LLM 제안 상한(Thought의 폭)
MAX_SIMULATIONS = 4               # pre 통과 후보 시뮬레이션 상한(비용 가드)
MAX_ACCEPTED = 3                  # 최종 채택 상한
RECOMMENDATIONS = ["유지 권장", "변경 검토", "학과 상담 권장"]
REASON_CODES = ["graduate_faster", "overflow_relief", "conv_tradeoff",
                "load_adjust", "timeline_extend"]
_REASON_KO = {
    "graduate_faster": "더 빨리 졸업", "overflow_relief": "초과학기 해소",
    "conv_tradeoff": "다전공 유지/포기 갈림길", "load_adjust": "수강 부담 조정",
    "timeline_extend": "기간 연장(휴학·학기 추가)",
}
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_SID_RE = re.compile(r"20\d{6}")  # 학번 원본 노출 가드(컨텍스트엔 입학연도 4자리만 있으나 방어)


# ---------- facts (LLM 입력·캐시 키 원천 — 결정론) ----------
def _build_facts(audit, risk, plan, ctx: StudentContext) -> list[dict]:
    """본 보고서의 결정론 산출만으로 F* fact 목록 구성 — 표시 계층(markdown·trace·해설·총평)
    미포함(자기참조·캐시 오염 방지, 적대 H3)."""
    facts: list[dict] = []

    def add(text: str) -> None:
        facts.append({"id": f"F{len(facts) + 1}", "text": text})

    # :g — .5 단위 보존(서로 다른 학생이 :.0f 라운딩으로 같은 캐시 키가 되는 충돌 방지, 적대① R1)
    add(f"총 이수 {audit.total_earned:g}/{audit.total_required:g}학점 · 총 부족 {audit.total_gap:g}학점")
    gaps = [f"{g.area} {g.earned:g}/{g.required:g}(부족 {g.gap:g})"
            for g in audit.area_gaps if g.gap > 0]
    add("영역 부족: " + (", ".join(gaps) if gaps else "없음"))
    add("미이수 필수: " + (", ".join(audit.missing_required_display or audit.missing_required_names)
                          if audit.missing_required_names else "없음"))
    # 점수(score)는 구 트리거 보조 지표 — 여유도 사다리와 따로 놀아 총평에서 제외(사용자 혼란)
    add(f"졸업 여유도 {risk.grade}({risk.label})")
    o = plan.overflow
    # 사용자 언어로 — blocked/feasible 같은 내부 용어가 총평 문장에 그대로 새던 문제(사용자 피드백)
    if plan.feasible:
        rm = f"로드맵: 잔여 학기 안에 배치 가능({len(plan.terms)}개 학기 계획)"
    else:
        rm = f"로드맵: 현 조건으로는 잔여 학기 안에 전부 배치 불가({len(plan.terms)}개 학기만 부분 배치)"
    add(rm + (f" · 초과 {o.extra_semesters}학기 필요(예상 졸업 {o.projected_graduation_term or '미상'})" if o else ""))
    for cc in audit.convergence_checks:
        add(f"{cc['name']}({cc['track']}): {cc['earned']:g}/{cc['required']:g}"
            f"(부족 {cc['gap']:g}) · 겹침 {cc['overlap_credits']:g} 중 중복인정 "
            f"{cc['double_recognizable']:g}/{cc['double_cap']:g}")
    add(f"잔여 수강 학기 {ctx.remaining_semesters} · 계절학기 {'허용' if ctx.seasonal_semester_allowed else '불가'}"
        + (f" · 학기당 상한 {ctx.max_credits_per_term:g}학점" if ctx.max_credits_per_term else ""))
    return facts


def _canonical_facts(facts: list[dict]) -> str:
    """캐시 키용 canonical 직렬화(codex SHOULD) — 순서·표기 고정."""
    return json.dumps(facts, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


# ---------- 갈림길 선정 (LLM ① — Thought) ----------
def _selector_schema(add_ids: list[str], drop_ids: list[str]) -> dict:
    cand = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "delta": whatif_delta_schema(add_ids, drop_ids),
            "reason_code": {"type": "string", "enum": REASON_CODES},
            "rationale": {"type": "string", "maxLength": 80},
        },
        "required": ["delta", "reason_code", "rationale"],
    }
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"candidates": {"type": "array", "items": cand,
                                      "maxItems": MAX_CANDIDATES}},
        "required": ["candidates"],
    }


def _selector_prompt(facts: list[dict], conv_names: list[str]) -> str:
    facts_txt = "\n".join(f"- [{f['id']}] {f['text']}" for f in facts)
    conv = ", ".join(conv_names) if conv_names else "없음"
    return f"""졸업사정 보고서를 받은 학생에게 의미 있는 '갈림길'(what-if 시나리오)을 고르는 선정기다.
아래 결정론 진단 facts를 보고, 이 학생이 실제로 고민할 시나리오 후보를 최대 {MAX_CANDIDATES}개 제안하라.
각 후보의 효과는 네가 판단하지 않는다 — 시스템이 결정론 시뮬레이터로 실제 재계산해 검증한다.

진단 facts:
{facts_txt}

신청 융합·연계전공: {conv}

delta 필드 의미(허용 범위 준수):
- calendar_delay_terms(휴학 1~4) · remaining_semesters_change(잔여 학기 ±4)
- seasonal_semester_allowed(계절학기) · max_credits_per_term(학기당 상한 9~24)
- prev_term_gpa_ge_375(성적우수 +3) · add_convergence/drop_convergence(융합전공 추가/포기)

reason_code: graduate_faster(더 빨리 졸업) / overflow_relief(초과학기 해소) /
conv_tradeoff(다전공 유지·포기 갈림길) / load_adjust(수강 부담 조정) / timeline_extend(기간 연장 영향)

규칙:
- **파라미터 값까지 네가 고른다** — 같은 reason이라도 값(계절 허용 vs 상한 18, 잔여 +1 vs +2,
  상한 12 vs 15)을 이 학생의 facts에 맞게 선택하고, rationale에 왜 그 값인지 한 줄로 써라.
- 학생 상태와 무관한 시나리오(예: 초과학기가 없는데 overflow_relief, 융합 미선언인데 conv_tradeoff)는 내지 마라.
- 서로 다른 갈림길을 다양하게 — 같은 delta의 사소한 변형 반복 금지.
- **휴학(calendar_delay_terms)과 잔여 학기 증감(remaining_semesters_change)을 한 후보에 동시에
  넣지 마라** — 휴학은 수강 학기 수를 바꾸지 않는다(시점만 지연). 둘은 별개 갈림길로 분리하라.
- 변경 없는 필드는 null(또는 빈 배열)."""


# ---------- 관련성 필터 (결정론 — pre/post 2단, 탈락은 기록) ----------
def _delta_label(delta: WhatIfDelta, prog_names: dict) -> str:
    parts = []
    if delta.calendar_delay_terms:
        parts.append(f"휴학 {delta.calendar_delay_terms}학기")
    if delta.remaining_semesters_change:
        parts.append(f"잔여 학기 {delta.remaining_semesters_change:+d}")
    if delta.seasonal_semester_allowed is not None:
        parts.append("계절학기 " + ("허용" if delta.seasonal_semester_allowed else "불가"))
    if delta.max_credits_per_term is not None:
        parts.append(f"학기당 {delta.max_credits_per_term:g}학점")
    if delta.prev_term_gpa_ge_375 is not None:
        parts.append("직전 3.75" + ("↑" if delta.prev_term_gpa_ge_375 else "↓"))
    for ch in delta.add_convergence:
        parts.append(f"{prog_names.get(ch.program_id, ch.program_id)} 추가({ch.track})")
    for pid in delta.drop_convergence:
        parts.append(f"{prog_names.get(pid, pid)} 포기")
    return " · ".join(parts) or "변경 없음"


def _pre_check(reason_code: str, delta: WhatIfDelta, audit, plan, ctx) -> bool:
    """선정 노드의 pre 판정(시뮬 전) — 계획 §3.3 규칙표."""
    if reason_code == "overflow_relief":
        return plan.overflow is not None
    if reason_code == "conv_tradeoff":
        return bool(ctx.convergence_program_ids) and any(
            cc.get("gap", 0) > 0 or cc.get("overlap_credits", 0) > cc.get("double_cap", 0)
            for cc in audit.convergence_checks)
    if reason_code == "graduate_faster":
        return (audit.total_gap > 0 or any(g.gap > 0 for g in audit.area_gaps)
                or bool(audit.missing_required_names))
    if reason_code == "load_adjust":
        return (delta.max_credits_per_term is not None
                or delta.seasonal_semester_allowed is not None
                or delta.prev_term_gpa_ge_375 is not None)
    if reason_code == "timeline_extend":
        return ((delta.calendar_delay_terms or 0) > 0
                or (delta.remaining_semesters_change or 0) > 0)
    return False


def _post_check(reason_code: str, diff) -> bool:
    """시뮬 노드의 post 판정(diff 산출 후) — WhatIfDiff 실재 필드만 사용(적대 R3)."""
    gt_b, gt_a = diff.graduation_term_before, diff.graduation_term_after
    changed = (diff.risk_before != diff.risk_after
               or diff.total_gap_before != diff.total_gap_after
               or diff.feasible_before != diff.feasible_after
               or gt_b != gt_a or diff.overflow_before != diff.overflow_after
               or bool(diff.changed_areas) or bool(diff.convergence_changes))
    if not changed:
        return False
    if reason_code == "overflow_relief":
        return ((diff.overflow_before and not diff.overflow_after)
                or (gt_b and gt_a and gt_a < gt_b))
    if reason_code == "conv_tradeoff":
        return bool(diff.convergence_changes) or diff.risk_before != diff.risk_after
    if reason_code == "graduate_faster":
        return ((gt_b and gt_a and gt_a < gt_b)
                or (diff.feasible_before is not True and diff.feasible_after is True))
    if reason_code == "load_adjust":
        return (diff.feasible_before != diff.feasible_after
                or diff.risk_before != diff.risk_after or gt_b != gt_a)
    if reason_code == "timeline_extend":
        # 지연 표시는 양쪽 모두 산출일 때만 — None(배치불가)→산출은 '개선'이라 별항(적대① R1)
        return ((gt_b is not None and gt_a is not None and gt_a > gt_b)
                or (diff.overflow_before and not diff.overflow_after)
                or (diff.feasible_before is not True and diff.feasible_after is True))
    return False


# ---------- 총평 생성·검증 (LLM ② + validator) ----------
def _summary_schema(fact_ids: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "headline": {"type": "string", "maxLength": 120},
            "lines": {"type": "array", "maxItems": 5, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "maxLength": 200},
                    "fact_ids": {"type": "array", "maxItems": 4,
                                 "items": {"type": "string", "enum": fact_ids}},
                },
                "required": ["text", "fact_ids"],
            }},
            "recommendation": {"type": "string", "enum": RECOMMENDATIONS},
        },
        "required": ["headline", "lines", "recommendation"],
    }


_GRADE_ORDER = {"S": -2, "A+": -1, "A": 0, "B": 1, "C": 2, "D": 3}   # 여유도 사다리와 동기


def _effect(diff) -> tuple[str, str]:
    """시나리오 효과 라벨·종류 — 결정론 합성(프론트 추측 금지, codex R1). build_diff 값만 사용.

    kind는 라벨 완성 후 단일 판정(적대 R1 — 누적식은 '졸업 지연'이 초록으로 뜨는 표면 모순):
    악화 신호가 하나라도 있으면 worsen — 학생 의사결정은 다운사이드 우선이 정직."""
    parts = []
    rb, ra = diff.risk_before, diff.risk_after
    if rb != ra and rb in _GRADE_ORDER and ra in _GRADE_ORDER:
        parts.append(f"리스크 {rb}→{ra} {'개선' if _GRADE_ORDER[ra] < _GRADE_ORDER[rb] else '악화'}")
    gb, ga = diff.graduation_term_before, diff.graduation_term_after
    if gb and ga and gb != ga:
        parts.append(f"예상 졸업 {'단축' if ga < gb else '지연'}")
    elif gb and not ga:                              # 산출 가능→불가 — 악화(적대 R1 비대칭)
        parts.append("예상 졸업 산출 불가 전환")
    if not diff.overflow_before and diff.overflow_after:
        parts.append("초과학기 발생")
    elif diff.overflow_before and not diff.overflow_after:
        parts.append("초과학기 해소")
    if diff.feasible_before is not True and diff.feasible_after is True:
        parts.append("배치 가능 전환")
    elif diff.feasible_before is True and diff.feasible_after is False:
        parts.append("배치 불가 전환")
    label = " · ".join(parts) or "변화 없음"
    worsen = any(w in label for w in ("악화", "지연", "초과학기 발생", "불가 전환"))
    improve = any(w in label for w in ("개선", "단축", "해소", "가능 전환"))
    kind = "worsen" if worsen else "improve" if improve else "neutral"
    return (label, kind)


def _scenario_facts(outcomes: list[ScenarioOutcome]) -> list[dict]:
    out = []
    for sc in outcomes:
        gt = (f"{sc.graduation_term_before or '미상'}→{sc.graduation_term_after or '미상'}"
              if sc.graduation_term_before != sc.graduation_term_after else "졸업시점 동일")
        out.append({"id": sc.id, "text": f"[{sc.label}] 리스크 {sc.risk_before}→{sc.risk_after} · "
                    f"총 부족 {sc.total_gap_before:g}→{sc.total_gap_after:g} · {gt}"
                    + (" · 초과학기 발생" if sc.overflow_after else "")})
    return out


def _summary_prompt(facts: list[dict], scen_facts: list[dict],
                    review: list[ScenarioReview]) -> str:
    f_txt = "\n".join(f"- [{f['id']}] {f['text']}" for f in facts)
    s_txt = "\n".join(f"- [{f['id']}] {f['text']}" for f in scen_facts) or "- (채택 시나리오 없음)"
    rej = "\n".join(f"- {r.label}: {r.rejected_by}" for r in review if r.verdict == "rejected") or "- 없음"
    return f"""졸업사정 컨설팅 보고서의 '에이전트 총평' 작성기다. 아래는 전부 결정론 시뮬레이터가
계산한 검증된 값이다. 너는 **이 값들만으로** 학생에게 갈림길 비교 조언을 쓴다.

진단 facts:
{f_txt}

시뮬레이션 관찰값(채택 시나리오):
{s_txt}

검토했지만 제외된 시나리오(참고 — post_no_change=효과 없음, pre_mismatch=전제 불일치,
sim_cap/accept_cap=상한 초과로 미채택일 뿐 효과 없음이 아님 — '개선 없음'으로 단정 금지):
{rej}

규칙:
- 모든 문장의 fact_ids에 근거 id(F*/S*)를 달아라. 위 텍스트에 없는 숫자·과목명·등급을 만들지 마라.
- 졸업 가능/불가를 새로 판정하지 마라 — 리스크 등급·feasible은 facts 그대로 인용만.
- 채택 시나리오가 없으면: "검토 결과 현 계획 유지가 최적"을 headline으로, recommendation은 '유지 권장'.
- headline은 이 학생의 핵심 갈림길 한 문장(예: "다전공 유지 시 +1학기 vs 포기 시 적시 졸업 — 이게 핵심 선택").
- **학생에게 말하듯 쉬운 한국어로** — blocked·feasible 같은 시스템 용어 금지, F1/S1 같은 근거 id를
  본문에 쓰지 마라(근거는 fact_ids 필드로만 — 화면이 배지로 따로 단다).
- **권고 변별**: 채택 시나리오 중 리스크·졸업시점이 *개선*된 것이 있으면 recommendation과
  headline에 그 방향을 반영하라. 개선이 없고 로드맵이 배치 가능 상태면 '유지 권장'.
  (판정은 여전히 관찰값 인용일 뿐 — 새 판정을 만들지 마라.)"""


# 한글 인접("A등급"·"D입니다")도 잡는 등급 패턴 — 영문 단어 내부(AI·CLASS)는 제외(codex R1)
# 'A+' 포함 — A 단독과 구분 위해 A\+ 우선 매칭, 한글 인접 규칙 유지
_GRADE_RE = re.compile(r"(?<![A-Za-z])(A\+|[SABCD])(?![A-Za-z+])")


_UNIT_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?=학점|학기|과목|점)")  # 단위 동반 수치(1자리 포함)


def _norm_num(n: str) -> str:
    """수치 토큰 정규화 — '12.0'≡'12', '08'≡'8' 양변 대칭(적대① lstrip 비대칭 수정)."""
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n.lstrip("0") or "0"


def _text_violations(text: str, allowed_nums: set, allowed_grades: str) -> str | None:
    """한 문장의 위반 사유(없으면 None) — 본문·headline 공용.
    검사 대상: 단위(학점·학기·과목·점) 동반 수치는 1자리도 검사(적대① — '8학기' 위조),
    그 외 bare 수치는 2자리 이상. '졸업 가능/불가' 단정은 LLM 금지 영역(판정은 결정론)."""
    if _SID_RE.search(text):
        return "학번 패턴"
    allowed = {_norm_num(a) for a in allowed_nums}
    nums = set(_UNIT_NUM_RE.findall(text))
    nums |= {n for n in _NUM_RE.findall(text) if len(n.replace(".", "")) >= 2}
    if any(_norm_num(n) not in allowed for n in nums):
        return "근거 밖 수치"
    if any(g not in allowed_grades for g in _GRADE_RE.findall(text)):
        return "등급 모순"
    # 단정만 거부 — '졸업 가능 시점/시기/여부/성' 같은 fact 기반 표현은 허용(적대 R2 M1 과폐기 방지)
    if re.search(r"졸업\s*(불가|가능)(?!\s*(시점|시기|여부|성))", text):
        return "판정 단정"
    # 내부 용어·fact id 본문 노출 차단 — 사용자가 못 알아듣는 문장 방지(2026-06-05 사용자 피드백)
    if re.search(r"(?<![A-Za-z])[FS]\d(?![0-9.])", text) or re.search(r"\b(blocked|feasible)\b", text, re.IGNORECASE):
        return "내부 용어 노출"
    return None


def _validate_summary(raw: dict, facts: list[dict], scen_facts: list[dict],
                      risk_grade: str) -> tuple[AgentSummary | None, list[str]]:
    """문장별 fact 해소·수치 대조·판정 모순·마스킹 — 위반 줄 폐기(설명 표시 아닌 폐기가 정직).
    headline도 동일 검증(전체 fact 수치 기준 — codex R1: 환각이 headline으로 새는 경로 차단)."""
    by_id = {f["id"]: f["text"] for f in facts + scen_facts}
    all_nums: set = set()
    for t in by_id.values():
        all_nums |= set(_NUM_RE.findall(t))
    all_grades = " ".join(by_id.values()) + f" {risk_grade}"
    issues: list[str] = []
    lines: list[SummaryLine] = []
    for ln in raw.get("lines", []):
        text, ids = (ln.get("text") or "").strip(), ln.get("fact_ids") or []
        if not text:
            continue
        if not ids or any(i not in by_id for i in ids):
            issues.append(f"근거 미해소: {text[:30]}…")
            continue
        allowed = set()
        ref_txt = f"{risk_grade} "
        for i in ids:
            allowed |= set(_NUM_RE.findall(by_id[i]))
            ref_txt += by_id[i] + " "
        bad = _text_violations(text, allowed, ref_txt)
        if bad:
            issues.append(f"{bad}: {text[:30]}…")
            continue
        lines.append(SummaryLine(text=text, fact_ids=ids))
    if not lines:
        return None, issues or ["검증 통과 문장 0"]
    rec = raw.get("recommendation") or ""
    if rec not in RECOMMENDATIONS:
        rec = "학과 상담 권장"
    headline = (raw.get("headline") or "").strip()[:120]
    if headline and _text_violations(headline, all_nums, all_grades):
        issues.append("headline 위반 — 비표시")
        headline = ""
    return AgentSummary(headline=headline, lines=lines, recommendation=rec), issues


# ---------- 캐시 (검증 통과분만 — candidates_review 포함, R4) ----------
def _cache_key(model: str, facts: list[dict], ctx: StudentContext) -> str:
    """facts(수치)만으론 동수치 타학생·타전공 충돌 가능(codex R1) — 학생 정체성(전공·학번·
    학기·융합 선언)을 키에 포함."""
    ident = json.dumps({
        "pid": ctx.program_id, "yr": ctx.admission_year, "term": ctx.current_term,
        "conv": sorted(ctx.convergence_program_ids),
        "tracks": dict(sorted(ctx.convergence_tracks.items())),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{model}|v{SUMMARY_SCHEMA_VERSION}|{ident}|{_canonical_facts(facts)}".encode()).hexdigest()


def _cache_get(key: str):
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8")).get(key)
    except Exception:
        return None


def _cache_evict(key: str) -> None:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if key in data:
            del data[key]
            CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _cache_put(key: str, value: dict) -> None:
    try:
        import os
        data = {}
        if CACHE_PATH.exists():
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        data[key] = value
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")          # 원자적 쓰기 — explain과 동일(적대③ 동시성)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass                                          # 캐시 실패는 본체에 영향 없음


def _get_client():
    import os
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=key, timeout=20.0)
    except Exception:
        return None


def _model() -> str:
    import os
    return (os.getenv("GRADUATION_SUMMARY_MODEL") or os.getenv("OPENAI_GRADUATION_MODEL")
            or os.getenv("GRADUATION_EXPLAIN_MODEL") or "gpt-4o-mini")


def _llm_json(client, model: str, prompt: str, schema: dict, name: str) -> dict:
    kwargs = {
        "model": model,
        "input": [{"role": "system",
                   "content": "You analyze deterministic graduation-audit facts for a Korean student report."},
                  {"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
    }
    if model.startswith(("gpt-5", "o")):
        kwargs["reasoning"] = {"effort": "minimal"}
    try:
        resp = client.responses.create(**kwargs, temperature=0.1)
    except Exception as exc:
        if "temperature" in str(exc).lower():
            resp = client.responses.create(**kwargs)
        else:
            raise
    return json.loads(getattr(resp, "output_text", "") or "")


def _skip_trace(reason: str) -> list[NodeTraceEvent]:
    """생략·실패 시에도 4노드 placeholder 방출 — 점등 순서 안정(codex SHOULD)·그래프-카드
    모순은 summary_fallback 안내가 메움(적대 M4)."""
    return [NodeTraceEvent(node=n, kind=k, status="skip", summary=reason, branch_taken="총평 생략")
            for n, k in (("갈림길 선정", "llm"), ("갈림길 시뮬레이션", "tool"),
                         ("총평 생성", "llm"), ("총평 검증", "validator"))]


# ---------- 오케스트레이션 (단일 턴) ----------
def run_report_summary(payload: dict, audit, risk, plan, ctx: StudentContext,
                       run_audit_fn, client=None,
                       ) -> tuple[AgentSummary | None, str | None, list[NodeTraceEvent]]:
    """반환: (agent_summary | None, summary_fallback | None, trace 4노드)."""
    facts = _build_facts(audit, risk, plan, ctx)
    model = _model()
    key = _cache_key(model, facts, ctx)
    cached = _cache_get(key)
    if cached:
        try:
            summary = AgentSummary.model_validate(cached)
        except Exception:                            # corrupt cache → 미스 취급 + 제거(codex R1)
            _cache_evict(key)
            cached = None
    if cached:
        n_acc = sum(1 for r in summary.candidates_review if r.verdict == "accepted")
        return summary, None, [
            NodeTraceEvent(node="갈림길 선정", kind="llm", summary="캐시 적중(동일 진단)",
                           branch_taken=f"후보 {len(summary.candidates_review)} → 채택 {n_acc}"),
            NodeTraceEvent(node="갈림길 시뮬레이션", kind="tool",
                           summary=f"시나리오 {len(summary.scenarios)}건 관찰값(캐시)", branch_taken="캐시"),
            NodeTraceEvent(node="총평 생성", kind="llm", summary="캐시 적중",
                           branch_taken=summary.recommendation),   # 미스 경로와 라벨 정합(적대① R1)
            NodeTraceEvent(node="총평 검증", kind="validator", summary="검증 통과분 캐시", branch_taken="통과"),
        ]

    cli = client or _get_client()
    if cli is None:
        return None, "LLM 미설정 — 에이전트 총평 생략(결정론 진단·로드맵은 유효)", _skip_trace("LLM 미설정")

    from graduation_center.v2.catalog import load_programs
    progs = load_programs()
    prog_names = {pid: p.get("name_ko", pid) for pid, p in progs.items()}
    add_ids, drop_ids = _candidates(ctx)
    conv_names = [f"{prog_names.get(p, p)}({p})" for p in ctx.convergence_program_ids]

    # ① 갈림길 선정 (LLM — Thought) + pre 판정
    try:
        raw_sel = _llm_json(cli, model, _selector_prompt(facts, conv_names),
                            _selector_schema(add_ids, drop_ids), "scenario_selector")
    except Exception as exc:
        return None, f"갈림길 선정 실패({type(exc).__name__}) — 총평 생략", _skip_trace("선정 LLM 실패")

    from graduation_center.v2.catalog import assemble_requirement_profile
    profile = assemble_requirement_profile(ctx)
    review: list[ScenarioReview] = []
    pre_ok: list[tuple[WhatIfDelta, str, str, str]] = []     # (delta, reason, rationale, label)
    for c in (raw_sel.get("candidates") or [])[:MAX_CANDIDATES]:
        reason = c.get("reason_code") or ""
        rationale = (c.get("rationale") or "")[:80]
        try:
            delta = WhatIfDelta.model_validate(c.get("delta") or {})
        except Exception:
            review.append(ScenarioReview(label="(해석 불가 delta)", reason_code=reason,
                                         rationale=rationale, verdict="rejected",
                                         rejected_by="invalid_delta"))
            continue
        label = _delta_label(delta, prog_names)
        if delta.is_empty():
            review.append(ScenarioReview(label=label, reason_code=reason, rationale=rationale,
                                         verdict="rejected", rejected_by="no_op"))
            continue
        # 휴학+잔여학기 동시 변경 조합 차단(2026-06-05 사용자 지적: 라벨이 '휴학하면 잔여 +1'
        # 인과처럼 읽힘 — 휴학은 수강 학기 수 불변, 별개 갈림길이어야 함). 프롬프트 1차 + 여기 2차.
        if (delta.calendar_delay_terms or 0) > 0 and (delta.remaining_semesters_change or 0) != 0:
            review.append(ScenarioReview(label=label, reason_code=reason, rationale=rationale,
                                         verdict="rejected", rejected_by="invalid_delta"))
            continue
        if not _pre_check(reason, delta, audit, plan, ctx):
            review.append(ScenarioReview(label=label, reason_code=reason, rationale=rationale,
                                         verdict="rejected", rejected_by="pre_mismatch"))
            continue
        pre_ok.append((delta, reason, rationale, label))

    # ② 시뮬레이션 (결정론 — Action) + post 판정 (Observation)
    # before는 본 보고서 객체 재사용 금지 — skip_explain=True로 깨끗하게 1회 재계산(적대 H3)
    try:
        before = run_audit_fn(payload, skip_explain=True)
    except Exception as exc:
        return (None, f"시뮬레이션 기준 계산 실패({type(exc).__name__}) — 총평 생략",
                _skip_trace("before 재계산 실패"))
    outcomes: list[ScenarioOutcome] = []
    sim_capped: list[ScenarioReview] = []
    # cap 밖 후보도 정직하게 기록 — 미시뮬은 '효과 없음'이 아니라 sim_cap(거짓 사유 금지, codex R1).
    # 표시 순서는 LLM 제안 순서 보존을 위해 시뮬 결과 뒤에 합류(적대 R2 M2).
    for delta, reason, rationale, label in pre_ok[MAX_SIMULATIONS:]:
        sim_capped.append(ScenarioReview(label=label, reason_code=reason, rationale=rationale,
                                         verdict="rejected", rejected_by="sim_cap"))
    for delta, reason, rationale, label in pre_ok[:MAX_SIMULATIONS]:
        new_ctx, changes, assumptions, unsupported = apply_delta(ctx, delta, profile)
        if new_ctx is None:
            review.append(ScenarioReview(label=label, reason_code=reason, rationale=rationale,
                                         verdict="rejected", rejected_by="no_op"))
            continue
        try:
            after = run_audit_fn({**payload, "context": new_ctx.model_dump()}, skip_explain=True)
        except Exception:
            review.append(ScenarioReview(label=label, reason_code=reason, rationale=rationale,
                                         verdict="rejected", rejected_by="no_op"))
            continue
        diff = build_diff(before, after, delta)
        effective = _post_check(reason, diff)
        if not effective or len(outcomes) >= MAX_ACCEPTED:
            review.append(ScenarioReview(
                label=label, reason_code=reason, rationale=rationale, verdict="rejected",
                # 효과가 있었는데 채택 상한에 걸린 것은 accept_cap — post_no_change와 구분
                rejected_by=("accept_cap" if effective else "post_no_change")))
            continue
        review.append(ScenarioReview(label=label, reason_code=reason, rationale=rationale,
                                     verdict="accepted"))
        outcomes.append(ScenarioOutcome(
            id=f"S{len(outcomes) + 1}", label=label, reason_code=reason, rationale=rationale,
            applied_changes=changes, assumptions=assumptions,
            risk_before=diff.risk_before, risk_after=diff.risk_after,
            total_gap_before=diff.total_gap_before, total_gap_after=diff.total_gap_after,
            graduation_term_before=diff.graduation_term_before,
            graduation_term_after=diff.graduation_term_after,
            feasible_after=diff.feasible_after, overflow_after=diff.overflow_after,
            effect_label=_effect(diff)[0], effect_kind=_effect(diff)[1]))

    review += sim_capped
    n_cand, n_acc = len(review), len(outcomes)
    scen_facts = _scenario_facts(outcomes)

    # ③ 총평 생성 (LLM — Answer) + ④ 검증 — 실패 시 evict 의미 없음(미캐시), 재시도 1회
    summary = None
    issues: list[str] = []
    fact_ids = [f["id"] for f in facts] + [f["id"] for f in scen_facts]
    for _ in range(2):
        try:
            raw_sum = _llm_json(cli, model, _summary_prompt(facts, scen_facts, review),
                                _summary_schema(fact_ids), "agent_summary")
        except Exception as exc:
            return None, f"총평 생성 실패({type(exc).__name__}) — 총평 생략", _skip_trace("총평 LLM 실패")
        summary, issues = _validate_summary(raw_sum, facts, scen_facts, risk.grade)
        if summary is not None:
            break
    if summary is None:
        return (None, "총평 검증 실패(근거 미해소) — 총평 생략(결정론 진단은 유효)",
                _skip_trace(f"검증 전량 폐기: {'; '.join(issues[:2])}"))

    summary = summary.model_copy(update={
        "scenarios": outcomes, "candidates_review": review, "facts": facts})
    _cache_put(key, summary.model_dump())            # 검증 통과분만 — 탐색 과정 포함(R4)

    trace = [
        NodeTraceEvent(node="갈림길 선정", kind="llm",
                       summary=f"학생 갈림길 후보 {n_cand}건 제안(delta 값 포함)",
                       branch_taken=f"후보 {n_cand} → pre 통과 {len(pre_ok)}"),
        NodeTraceEvent(node="갈림길 시뮬레이션", kind="tool",
                       summary=f"결정론 재실행 {min(len(pre_ok), MAX_SIMULATIONS)}회 → 채택 {n_acc}·제외 {n_cand - n_acc}",
                       branch_taken=(f"채택 {n_acc}" if n_acc else "전부 효과 없음 → 유지 권장")),
        NodeTraceEvent(node="총평 생성", kind="llm",
                       summary=f"비교 총평 {len(summary.lines)}문장({summary.recommendation})",
                       branch_taken=summary.recommendation),
        NodeTraceEvent(node="총평 검증", kind="validator",
                       status="ok" if not issues else "warn",
                       summary=("전 문장 근거 해소" if not issues else f"{len(issues)}건 폐기 후 통과"),
                       branch_taken="통과"),
    ]
    return summary, None, trace
