"""졸업 시나리오 상담 Agent (What-if) — Tool Calling 패턴 (예제06 매개변수 추출기 동형).

질문 입력 → ① 매개변수 추출기(LLM 1회: category+delta) → ② 조건 가드(IF/ELSE)
→ ③ 졸업사정 재실행(기존 run_audit ×2, skip_explain) → ④ 시나리오 비교(결정론 diff)
→ ⑤ 다음 행동 제안(결정론 룰). LLM은 자연어→파라미터 변환만 — 판정·비교·제안은 전부 결정론.

가드레일: delta는 strict json_schema(전 필드 required+nullable, enum 동적)로만 받고,
적용은 StudentContext.model_validate 재검증(model_copy는 validator 미실행 — 검증 라운드1).
모든 실패는 500이 아니라 status="unsupported"로 degrade(데모 중 에러 화면 금지).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from graduation_center.v2.catalog import (
    PREV_GPA_BONUS, assemble_requirement_profile, load_catalog, load_programs,
    regular_term_cap,
)
from graduation_center.v2.models_v2 import (
    AuditPipelineResponse, NodeTraceEvent, StudentContext, WhatIfDelta, WhatIfDiff,
    WhatIfResponse,
)
from graduation_center.v2.pipeline import run_audit
from graduation_center.v2.planner import _nth_regular_term

CACHE_PATH = Path(__file__).resolve().parents[1] / "data/graduation/v2/whatif_cache.json"
CATEGORIES = ["휴학", "수강학기변경", "계절학기", "학점상한", "다전공변경", "성적우수", "기타"]


def _term_ko(label: str | None) -> str:
    """학기 라벨 사람용 표기: 2027-1 → 2027-1학기, 2027-S → 2027 하계."""
    if not label:
        return "미상"
    y, _, s = label.partition("-")
    return {"1": f"{y}-1학기", "2": f"{y}-2학기", "S": f"{y} 하계", "W": f"{y} 동계"}.get(s, label)


# ---------- ① 매개변수 추출기 (LLM) ----------
def _candidates(ctx: StudentContext) -> tuple[list[str], list[str]]:
    """add/drop 후보 — add는 카탈로그가 실제 load되는 비-primary만(검증 라운드2 M3).
    이미 선언된 전공도 add 후보에 포함한다 — '다전공을 부전공으로 바꾸면?' 같은 **트랙 변경**을
    add(동일 id, 새 track)로 표현하기 위함. 제외하면 표현 수단이 없어 LLM이 drop만 내놓아
    '부전공 전환'이 조용히 '다전공 포기' 시뮬레이션으로 둔갑한다(사용자 보고 2026-06-05)."""
    progs = load_programs()
    add_ids = []
    for pid, p in progs.items():
        if p.get("track_type", "primary") == "primary":
            continue
        if pid == ctx.program_id:
            continue
        try:
            load_catalog(pid)
        except Exception:
            continue
        add_ids.append(pid)
    return sorted(add_ids), sorted(ctx.convergence_program_ids)


def whatif_delta_schema(add_ids: list[str], drop_ids: list[str]) -> dict:
    """WhatIfDelta의 strict JSON schema — 상담(_schema)과 에이전트 총평(report_summary)이 공유.
    전 필드 required, Optional=nullable union, 빈 enum 절대 금지 —
    후보 0개인 array는 enum 없이 maxItems:0으로 닫는다(검증 라운드2·3)."""
    conv_item = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "program_id": ({"type": "string", "enum": add_ids} if add_ids
                           else {"type": "string"}),
            "track": {"type": "string", "enum": ["다전공", "부전공"]},
        },
        "required": ["program_id", "track"],
    }
    add_schema = {"type": "array", "items": conv_item}
    if not add_ids:
        add_schema["maxItems"] = 0
    drop_schema = {"type": "array",
                   "items": ({"type": "string", "enum": drop_ids} if drop_ids
                             else {"type": "string"})}
    if not drop_ids:
        drop_schema["maxItems"] = 0
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "calendar_delay_terms": {"type": ["integer", "null"]},
            "remaining_semesters_change": {"type": ["integer", "null"]},
            "seasonal_semester_allowed": {"type": ["boolean", "null"]},
            "max_credits_per_term": {"type": ["number", "null"]},
            "prev_term_gpa_ge_375": {"type": ["boolean", "null"]},
            "add_convergence": add_schema,
            "drop_convergence": drop_schema,
        },
        "required": ["calendar_delay_terms", "remaining_semesters_change",
                     "seasonal_semester_allowed", "max_credits_per_term",
                     "prev_term_gpa_ge_375", "add_convergence", "drop_convergence"],
    }


def _schema(add_ids: list[str], drop_ids: list[str]) -> dict:
    """상담 추출기 전체 응답 schema — delta 부분은 whatif_delta_schema 공유."""
    delta = whatif_delta_schema(add_ids, drop_ids)
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "category": {"type": "string", "enum": CATEGORIES},
            "interpretable": {"type": "boolean"},
            "question_summary": {"type": "string", "maxLength": 80},
            "delta": delta,
        },
        "required": ["category", "interpretable", "question_summary", "delta"],
    }


def _prompt(question: str, ctx: StudentContext, conv_names: list[str]) -> str:
    conv = ", ".join(conv_names) if conv_names else "없음"
    return f"""졸업사정 시뮬레이션의 매개변수 추출기다. 학생의 자연어 질문을 변경 파라미터(delta)로 변환한다.

현재 컨텍스트(참고만 — 같은 값이어도 delta는 질문 그대로 채운다, no-op 판정은 시스템이 한다):
- 현재 학기: {ctx.current_term or "미입력"} · 잔여 수강 학기: {ctx.remaining_semesters}
- 계절학기 허용: {ctx.seasonal_semester_allowed} · 신청 융합전공: {conv}

필드 의미(혼동 금지·허용 범위 준수):
- calendar_delay_terms: 휴학 — 수강 학기 수는 그대로, 시작(졸업) 시점만 N개 정규학기 뒤로. "다음 학기 휴학"=1, "1년 휴학"=2. 허용 1~4.
- remaining_semesters_change: 실제 남은 수강 학기 수 증감. "한 학기 더 다니면"=+1, "한 학기 줄이면"=-1. 허용 -4~+4.
- seasonal_semester_allowed: 계절학기 수강 가능 여부 변경.
- max_credits_per_term: 학기당 수강 학점 상한 변경. "15학점씩만 들으면"=15. 허용 9~24의 정수 권장.
- prev_term_gpa_ge_375: 직전학기 평점 3.75 이상 여부(신청학점 +{PREV_GPA_BONUS:.0f} 보너스).
- add_convergence / drop_convergence: 융합·연계전공 추가/포기/트랙 변경.
  예: "다전공·부전공을 빼면?"·"부전공 포기하면?" → drop_convergence에 위 '신청 융합전공'의 id를 넣어라(빈 배열 금지).
  예: "다전공을 부전공으로 바꾸면?" → add_convergence에 같은 id로 {{"program_id": 그 id, "track": "부전공"}} (트랙 변경 — drop에 넣지 마라).

규칙:
- 위 필드로 표현 불가한 질문(조기졸업 요건, 전과, 성적포기, 특정 과목, "이번 학기 안에"류 절대 시점)은 interpretable=false.
- 허용 범위 밖 요청(예: 10년 휴학)도 interpretable=false.
- 변경이 없는 필드는 null(또는 빈 배열). 질문에 없는 변경을 만들지 마라.
- 특히 add/drop_convergence는 질문이 전공·다전공·부전공의 추가/포기를 명시할 때만 채운다.
  계절학기·휴학·학점 질문에 융합전공 변경을 임의로 동반하지 마라(검증 e2e에서 실제 환각 사례 발견).
- question_summary는 해석을 80자 내로 요약(한국어).

질문: {question}"""


def interpret_question(question: str, ctx: StudentContext, client,
                       model: str, add_ids: list[str], drop_ids: list[str]) -> dict:
    progs = load_programs()
    conv_names = [progs.get(p, {}).get("name_ko", p) + f"({p})"
                  for p in ctx.convergence_program_ids]
    kwargs = {
        "model": model,
        "input": [
            {"role": "system",
             "content": "You convert Korean student questions into structured graduation-simulation parameters."},
            {"role": "user", "content": _prompt(question, ctx, conv_names)},
        ],
        "text": {"format": {"type": "json_schema", "name": "whatif_delta",
                            "schema": _schema(add_ids, drop_ids), "strict": True}},
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


# ---------- ② 조건 가드 (validator, 결정론) ----------
def apply_delta(ctx: StudentContext, delta: WhatIfDelta, profile,
                ) -> tuple[StudentContext | None, list[str], list[str], str | None]:
    """반환: (new_ctx | None, applied_changes, assumptions, unsupported_reason)."""
    updates: dict = {}
    changes: list[str] = []
    assumptions: list[str] = []

    # 휴학 — current_term을 복학 정규학기 직전 계절 슬롯으로 전진시킨 ctx 전체를
    # run_audit에 흘린다(_ordered_terms·overflow가 ctx에서 읽으므로 일관 적용).
    # 휴학 중 학기(정규·계절)는 배치에서 제외됨(검증 라운드2·3 수치 확인).
    d = delta.calendar_delay_terms or 0
    if d > 0:
        if not ctx.current_term:
            return None, [], [], "현재 학기가 미입력이라 휴학 시뮬레이션이 불가합니다 — 학생 정보에서 현재 학기를 입력해 주세요."
        ret = _nth_regular_term(ctx.current_term, d + 1)   # 복학 정규학기
        if not ret:
            return None, [], [], "현재 학기 형식 오류 — 휴학 시뮬레이션이 불가합니다."
        y, _, s = ret.partition("-")
        updates["current_term"] = f"{int(y) - 1}-W" if s == "1" else f"{y}-S"
        # 복학 첫 학기 보너스 의미 불명 → 보수적으로 미적용(질문의 명시 변경보다 우선)
        updates["prev_term_gpa_ge_375"] = False
        changes.append(f"휴학 {d}학기 — 복학 후 {_term_ko(ret)}부터 수강 배치")
        assumptions.append("휴학 중 학기(계절 포함)는 수강 불가로 가정, 복학 첫 학기 신청학점 보너스(직전학기 3.75↑) 미적용 가정")

    n = delta.remaining_semesters_change or 0
    if n != 0:
        # 상한 12도 클램프 — StudentContext le=12 가드에 걸려 400으로 새는 경로 차단(검증 코드R1)
        new_rem = max(0, min(12, ctx.remaining_semesters + n))
        updates["remaining_semesters"] = new_rem
        changes.append(f"잔여 수강 학기 {ctx.remaining_semesters}→{new_rem}")

    if delta.seasonal_semester_allowed is not None:
        if delta.seasonal_semester_allowed == ctx.seasonal_semester_allowed:
            changes.append(f"계절학기 {'허용' if ctx.seasonal_semester_allowed else '불가'} — 현재 설정과 동일")
        else:
            updates["seasonal_semester_allowed"] = delta.seasonal_semester_allowed
            changes.append(f"계절학기 {'허용' if delta.seasonal_semester_allowed else '불가'}로 변경")

    if delta.max_credits_per_term is not None:
        v = delta.max_credits_per_term
        legal = regular_term_cap(profile.total_credits_min)
        if v > legal:
            return None, [], [], (f"학기당 {v:g}학점은 설정할 수 없습니다 — "
                                  f"학사규정 제32조 상한 {legal:.0f}학점"
                                  f"(직전학기 3.75 이상 시 +{PREV_GPA_BONUS:.0f}학점까지) 초과.")
        if v < 9:
            # 비현실적 저상한(0.5학점 등) → '변화 없음' headline과 blocked 로드맵이 모순되는 카드 방지
            return None, [], [], f"학기당 상한은 9~{legal:.0f}학점 범위에서 시뮬레이션됩니다."
        updates["max_credits_per_term"] = v
        changes.append(f"학기당 학점 상한 {v:g}학점으로 제한")    # :g — 17.5를 '18'로 반올림 표기 금지

    if delta.prev_term_gpa_ge_375 is not None:
        if d > 0:
            # 휴학 리셋이 우선 — 단 요청을 침묵 무시하지 않고 가정으로 명시(데이터 흐름 추적성)
            if delta.prev_term_gpa_ge_375:
                assumptions.append("요청하신 직전학기 3.75 보너스는 휴학 가정에서 미적용(복학 직전 성적 불명)")
        else:
            updates["prev_term_gpa_ge_375"] = delta.prev_term_gpa_ge_375
            changes.append("직전학기 3.75 이상 가정" if delta.prev_term_gpa_ge_375
                           else "직전학기 3.75 미만 가정")

    progs = load_programs()
    conv_ids = list(ctx.convergence_program_ids)
    conv_tracks = dict(ctx.convergence_tracks)
    conv_changes: list[str] = []
    # '부전공으로 바꾸면' = LLM이 drop+add(동일 id) 또는 add(새 track)로 표현 → 트랙 변경으로
    # 통합 해석(포기 아님). 기존엔 표현 불가라 drop만 나와 '다전공 포기'로 둔갑(2026-06-05 수정).
    track_change_ids = {ch.program_id for ch in delta.add_convergence
                        if ch.program_id in ctx.convergence_program_ids}
    for ch in delta.add_convergence:
        p = progs.get(ch.program_id)
        name = p.get("name_ko", ch.program_id) if p else ch.program_id
        if ch.program_id in conv_ids:
            cur = conv_tracks.get(ch.program_id, "다전공")
            if cur == ch.track:
                assumptions.append(f"'{name}'은 이미 {cur} 트랙입니다 — 트랙 변경 없음")
                continue
            conv_tracks[ch.program_id] = ch.track
            conv_changes.append(f"{name} 트랙 변경: {cur} → {ch.track}(시뮬레이션)")
            continue
        if p is None or p.get("track_type", "primary") == "primary" or ch.program_id == ctx.program_id:
            return None, [], [], f"'{ch.program_id}'는 추가할 수 없는 융합·연계전공입니다."
        try:
            load_catalog(ch.program_id)
        except Exception:
            return None, [], [], f"'{ch.program_id}' 교육과정 데이터가 없어 시뮬레이션이 불가합니다."
        conv_ids.append(ch.program_id)
        conv_tracks[ch.program_id] = ch.track
        conv_changes.append(f"{name}({ch.track}) 추가")
    for pid in delta.drop_convergence:
        if pid in track_change_ids:
            continue                                  # drop+add 동일 id = 트랙 변경 — 포기로 처리 금지
        if pid not in conv_ids:
            return None, [], [], f"'{pid}'는 현재 신청하지 않은 융합·연계전공입니다."
        conv_ids.remove(pid)
        conv_tracks.pop(pid, None)                    # stale track 동반 제거(검증 라운드1)
        conv_changes.append(f"{progs.get(pid, {}).get('name_ko', pid)} 포기(시뮬레이션)")
    # add X + drop X 같은 순 no-op은 적용·표기 모두 생략('추가'와 '포기'가 한 카드에 공존 방지)
    if conv_ids != list(ctx.convergence_program_ids) or conv_tracks != dict(ctx.convergence_tracks):
        updates["convergence_program_ids"] = conv_ids
        updates["convergence_tracks"] = conv_tracks
        changes += conv_changes

    if not updates:
        return None, changes, [], "요청하신 변경이 현재 설정과 동일합니다 — 변경할 내용이 없습니다."
    # model_copy는 validator를 재실행하지 않음(검증 라운드1 실측) → model_validate로 입구 가드 재통과
    try:
        new_ctx = StudentContext.model_validate({**ctx.model_dump(), **updates})
    except Exception as exc:
        return None, [], [], f"변경 결과가 입력 검증을 통과하지 못했습니다({type(exc).__name__}) — 변경 폭을 줄여 시도해 주세요."
    return new_ctx, changes, assumptions, None


# ---------- ④ 시나리오 비교 (tool, 결정론) ----------
def _graduation_term(resp: AuditPipelineResponse) -> tuple[str | None, bool]:
    """(졸업 예상 정규학기 라벨 | None, already_met). 단일 산식 — before/after 동일 기준.

    마지막 배치가 계절학기여도 정규학기 라벨을 쓴다 — 학위수여 시점 기준으로 동치
    (Y-S 이수완료=Y-1과 같은 8월 수여, Y-W=Y-2와 같은 2월 수여)라 의도된 동작(검증 코드R1)."""
    plan = resp.roadmap
    if plan.feasible and plan.terms:
        regs = [t.term for t in plan.terms if t.term.endswith(("-1", "-2"))]
        return (regs[-1], False) if regs else (None, False)   # 전부 계절 → 산출 불가(라운드3)
    if plan.feasible and not plan.terms:
        return None, True                                     # 이미 충족
    if plan.overflow and plan.overflow.projected_graduation_term:
        return plan.overflow.projected_graduation_term, False
    return None, False


def build_diff(before: AuditPipelineResponse, after: AuditPipelineResponse,
               delta: WhatIfDelta) -> WhatIfDiff:
    gt_b, met_b = _graduation_term(before)
    gt_a, met_a = _graduation_term(after)
    gaps_b = {g.area: g.gap for g in before.audit.area_gaps}
    changed = [a for a, gap in ((ag.area, ag.gap) for ag in after.audit.area_gaps)
               if abs(gaps_b.get(a, 0.0) - gap) > 0.01]
    conv_b = {c["program_id"]: c for c in before.audit.convergence_checks}
    conv_a = {c["program_id"]: c for c in after.audit.convergence_checks}
    conv_changes: list[str] = []
    for pid, c in conv_b.items():
        if pid not in conv_a:
            conv_changes.append(f"{c['name']}({c['track']}) 제외 — 시뮬레이션 반영")
    for pid, c in conv_a.items():
        if pid not in conv_b:
            conv_changes.append(f"{c['name']}({c['track']}) 추가 — 부족 {c['gap']:.0f}학점")
        elif abs(c["gap"] - conv_b[pid]["gap"]) > 0.01:
            conv_changes.append(f"{c['name']}: 부족 {conv_b[pid]['gap']:.0f}→{c['gap']:.0f}학점")
    if abs(before.audit.to_fusion_total - after.audit.to_fusion_total) > 0.01:
        conv_changes.append(f"전공→융합 배정 {before.audit.to_fusion_total:.0f}→"
                            f"{after.audit.to_fusion_total:.0f}학점")

    d = delta.calendar_delay_terms or 0
    risk_changed = before.risk.grade != after.risk.grade
    if d > 0 and met_a:
        headline = f"졸업요건은 충족 상태가 유지되지만, 휴학 {d}학기만큼 졸업 시점이 늦어집니다."
    elif d > 0 and gt_b and not gt_a:
        # 비대칭(코드R2 MUST): before는 산출됐는데 after는 산출 불가(예: 3.75 보너스 리셋으로
        # blocked) — "늦어집니다" 단정은 카드의 "X → 산출 불가" 표기와 충돌. 산출 불가를 명시.
        headline = (f"휴학 {d}학기만큼 수강 시작이 늦어지며, 변경 후 예상 졸업 학기는 "
                    f"산출되지 않았습니다 — 아래 '변경 후 로드맵'에서 배치 결과를 확인하세요.")
    elif d > 0 and gt_a and not gt_b:
        # 복합 delta(휴학+계절 등)로 '변경 전 산출 불가 → 변경 후 산출' 역방향(코드R3-①):
        # 누락 시 '변화 없음'으로 떨어져 R1 모순 재발 — 4조합 전수 커버.
        headline = (f"휴학 {d}학기 반영 — 예상 졸업이 {_term_ko(gt_a)}로 산출됩니다"
                    f"(변경 전에는 배치 불가 상태였습니다).")
    elif d > 0 and not gt_b and not gt_a:
        # 양쪽 다 산출 불가(blocked·overflow 없음) — '변화 없음'으로 떨어지면
        # applied_changes("복학 후 X부터")와 정면 모순(검증 코드R1 HIGH). 지연을 항상 명시.
        headline = (f"휴학 {d}학기만큼 수강 시작과 졸업 시점이 늦어집니다 "
                    f"(부족 요건·리스크는 휴학으로 달라지지 않습니다).")
    elif gt_b and gt_a and gt_b != gt_a:
        headline = f"예상 졸업이 {_term_ko(gt_b)} → {_term_ko(gt_a)}로 변동합니다" + \
                   (f" (리스크 {before.risk.grade}→{after.risk.grade})." if risk_changed else ".")
    elif met_a and not met_b:
        # "에도"는 '변경 전에도 충족'으로 오독됨(이 분기는 정의상 전엔 미충족) — 코드R3-③
        # 졸업인증제 경고가 after에 살아 있으면 '추가 수강 없이 충족' 단정 금지 — 다전공 제거
        # 상담이 인증제(심화 +18)를 두고 '빼도 된다'로 읽히는 모순(검증 캠페인 codex④, 2026-06-05)
        if any(getattr(r, "factor", "") == "졸업인증제" for r in (after.risk.reasons or [])):
            headline = ("총학점·영역 요건은 충족하지만, 다·부전공이 없으면 졸업인증제에 따라 "
                        "심화전공(전공최저+18학점) 충족이 추가로 필요합니다 — 아래 안내를 확인하세요.")
        else:
            headline = "변경 후에는 추가 수강 없이 졸업요건을 충족합니다."
    elif risk_changed:
        headline = f"리스크 등급이 {before.risk.grade}({before.risk.label}) → {after.risk.grade}({after.risk.label})로 변동합니다."
    elif conv_changes:
        headline = conv_changes[0]
    elif abs(before.audit.total_gap - after.audit.total_gap) > 0.01:
        headline = f"총 부족 학점이 {before.audit.total_gap:.0f} → {after.audit.total_gap:.0f}학점으로 변동합니다."
    else:
        headline = "이 변경으로는 졸업사정 결과에 유의미한 변화가 없습니다."

    return WhatIfDiff(
        risk_before=before.risk.grade, risk_after=after.risk.grade,
        risk_label_before=before.risk.label, risk_label_after=after.risk.label,
        total_gap_before=before.audit.total_gap, total_gap_after=after.audit.total_gap,
        feasible_before=before.roadmap.feasible, feasible_after=after.roadmap.feasible,
        graduation_term_before=gt_b, graduation_term_after=gt_a,
        already_met_after=met_a,
        overflow_before=before.roadmap.overflow is not None,
        overflow_after=after.roadmap.overflow is not None,
        changed_areas=changed, convergence_changes=conv_changes, headline=headline,
    )


# ---------- ⑤ 다음 행동 제안 (tool, 결정론 룰 테이블) ----------
def suggest_next_actions(diff: WhatIfDiff, delta: WhatIfDelta,
                         ctx: StudentContext) -> list[str]:
    acts: list[str] = []
    # 변경 적용 '후' 기준으로 판단 — "계절 못 듣게 되면?" 질문에 원본 seasonal을 보면 오제안(검증 코드R1)
    seasonal_after = (delta.seasonal_semester_allowed
                      if delta.seasonal_semester_allowed is not None
                      else ctx.seasonal_semester_allowed)
    overflow_resolved = diff.overflow_before and not diff.overflow_after
    if diff.overflow_after and not diff.overflow_before:
        acts.append("초과학기 위험이 생겼습니다 — " +
                    ("학기당 학점 배분을 조정한 시나리오를 추가로 확인해 보세요."
                     if seasonal_after
                     else "계절학기 허용 시나리오를 추가로 확인해 보세요."))
    if overflow_resolved:
        acts.append("이 변경으로 초과학기가 해소됩니다 — 수강신청 시 개설학기를 꼭 확인하세요.")
    if delta.drop_convergence:
        # 인증제 문구는 '변경 후 다·부전공이 하나도 안 남을 때만' — 부전공 전환(drop+add)처럼
        # 트랙이 남는 변경에 심화전공 문구가 고정 출력되던 오안내 수정(사용자 보고 2026-06-05)
        after_conv = ((set(ctx.convergence_program_ids or []) - set(delta.drop_convergence or []))
                      | {c.program_id for c in (delta.add_convergence or [])})
        if after_conv:
            acts.append("융합·연계전공 변경 시 학위 표기가 달라집니다 — 변경 절차·시점을 학과사무실에 확인하세요.")
        else:
            acts.append("융합·연계전공 포기 시 학위 표기가 달라지고, 다·부전공이 없으면 졸업인증제(제96조의2)에 "
                        "따라 심화전공(전공최저+18학점) 충족이 필요합니다 — 포기 절차와 면제 전형 여부를 "
                        "학과사무실에 확인하세요.")
    if delta.add_convergence:
        acts.append("다전공·부전공은 신청 기간과 승인 요건이 있습니다 — 모집 공지를 확인하세요.")
    if (delta.calendar_delay_terms or 0) > 0:
        acts.append("휴학은 신청 기한·복학 절차가 있습니다 — 학과사무실 또는 원스톱서비스센터에 확인하세요.")
    # 상호배타 게이트(검증 코드R1): 초과학기가 해소된 변경에 등록금 경고·'변경 전이 안전' 권유 금지
    if diff.graduation_term_before and diff.graduation_term_after \
            and diff.graduation_term_after > diff.graduation_term_before \
            and not overflow_resolved:
        acts.append("초과 등록 학기의 등록금·국가장학 신청 요건을 확인하세요.")
    if delta.seasonal_semester_allowed is True:
        acts.append("계절학기 개설 과목은 학기마다 다릅니다 — 개설 공지를 확인하세요.")
    from graduation_center.v2.risk import grade_rank
    if grade_rank(diff.risk_after) > grade_rank(diff.risk_before) and not overflow_resolved:  # 'A+' 안전 비교
        acts.append("변경 전 계획이 더 안전합니다 — 변경이 꼭 필요하면 학과 상담을 권장합니다.")
    if diff.already_met_after and not acts:
        acts.append("졸업사정 결과가 충족으로 유지됩니다 — 졸업신청 일정만 확인하세요.")
    return acts[:3]


# ---------- 캐시 (LLM 해석 결과만 — ②~⑤는 매번 결정론 재계산) ----------
CACHE_SCHEMA_VERSION = 4          # 형식 변경·프롬프트 가드 변경 시 +1 — 구 엔트리 자동 미스
                                  # (v3: 환각 융합변경 semantic guard 도입 — 코드R3)
                                  # (v4: 트랙 변경 지원 — add enum에 선언 전공 포함·프롬프트 예시 추가)


def _cache_key(model: str, question: str, ctx: StudentContext, add_ids: list[str]) -> str:
    # 융합 선언·enum 모집단·프롬프트에 실제 들어가는 컨텍스트만 포함 — 학생 간 delta 오반환·
    # schema 드리프트 차단(라운드2). max_credits는 프롬프트 미반영이라 키에서 제외(과민 키 방지, 코드R1).
    payload = {"v": CACHE_SCHEMA_VERSION,
               "m": model, "q": " ".join(question.split()), "p": ctx.program_id,
               "y": ctx.admission_year, "c": sorted(ctx.convergence_program_ids),
               "t": sorted(ctx.convergence_tracks.items()), "a": add_ids,
               "s": ctx.seasonal_semester_allowed, "r": ctx.remaining_semesters,
               "ct": ctx.current_term}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()[:24]


def _cache_get(key: str):
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8")).get(key)
    except Exception:
        return None


def _cache_evict(key: str) -> None:
    """오염·구형식 엔트리 제거 — 안 하면 ValidationError가 같은 키에서 영구 반복(검증 코드R2)."""
    try:
        if not CACHE_PATH.exists():
            return
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if key in data:
            data.pop(key)
            tmp = CACHE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def _cache_put(key: str, value: dict) -> None:
    try:
        data = {}
        if CACHE_PATH.exists():
            try:
                data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[key] = value
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def _get_client():
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None
    try:
        from openai import OpenAI
        return OpenAI(timeout=20)
    except Exception:
        return None


# ---------- 오케스트레이션 ----------
def _semantic_guard(question: str, delta: WhatIfDelta) -> tuple[WhatIfDelta, list[str]]:
    """strict schema를 '형식상' 통과한 의미 오염을 결정론으로 차단(코드R3 MUST).

    실측: 프롬프트 negative 규칙만으로는 LLM이 '휴학하면?' 질문에 drop_convergence를
    환각으로 동반하는 사례가 재발(워밍업 검수 6건). 질문에 전공·융합·연계 언급이
    없으면 융합 변경 필드를 결정론으로 제거 — 캐시에 남아도 매번 같은 가드를 타
    결과가 결정론적으로 정화된다."""
    if (delta.add_convergence or delta.drop_convergence) \
            and not any(w in question for w in ("전공", "융합", "연계")):
        return (delta.model_copy(update={"add_convergence": [], "drop_convergence": []}),
                ["질문에 없는 융합·연계전공 변경 해석은 무시했습니다(환각 가드)"])
    return delta, []


def _derive_category(delta: WhatIfDelta, raw_category) -> str:
    """채워진 delta 필드 → category 결정론 재도출(그래프 분기 라벨의 정직성 보장)."""
    if delta.calendar_delay_terms:
        return "휴학"
    if delta.remaining_semesters_change:
        return "수강학기변경"
    if delta.seasonal_semester_allowed is not None:
        return "계절학기"
    if delta.max_credits_per_term is not None:
        return "학점상한"
    if delta.add_convergence or delta.drop_convergence:
        return "다전공변경"
    if delta.prev_term_gpa_ge_375 is not None:
        return "성적우수"
    return raw_category if raw_category in CATEGORIES else "기타"


def _unsupported(question_summary: str, category, reason: str,
                 trace: list[NodeTraceEvent]) -> WhatIfResponse:
    trace = trace + [
        NodeTraceEvent(node="조건 가드", kind="validator", status="warn",
                       summary=reason[:120], branch_taken="지원 범위 밖"),
        NodeTraceEvent(node="안내 종료", kind="tool", status="skip",
                       summary="시뮬레이션 미실행 — 안내만 반환", branch_taken=reason[:40]),
    ]
    return WhatIfResponse(status="unsupported", category=category,
                          question_summary=question_summary,
                          unsupported_reason=reason, node_trace=trace)


def run_whatif(payload: dict, client=None) -> WhatIfResponse:
    ctx = StudentContext.model_validate(payload["context"])
    question = " ".join(str(payload.get("question", "")).split())
    if not question:
        raise ValueError("question이 비어 있습니다.")
    profile = assemble_requirement_profile(ctx)
    model = os.getenv("OPENAI_GRADUATION_MODEL", "gpt-4o-mini")
    add_ids, drop_ids = _candidates(ctx)

    # ① 매개변수 추출기 (LLM 1회, 캐시 우선 — 데모 일관성·오프라인 폴백)
    # interpretable=false도 캐시한다(의도): 같은 질문 = 같은 안내가 데모 일관성에 유리.
    # 검증 실패 raw는 미저장 + 기존 캐시면 evict 후 LLM 재해석 1회(영구 고착 방지 — 코드R2).
    key = _cache_key(model, question, ctx, add_ids)
    raw, delta, cached = None, None, False
    for attempt in (1, 2):
        raw = _cache_get(key) if attempt == 1 else None
        cached = raw is not None
        if raw is None:
            client = client or _get_client()
            if client is None:
                return _unsupported(question[:40], None,
                                    "LLM 미설정 — 예시 질문 버튼을 이용해 주세요.", [
                    NodeTraceEvent(node="질문 분류", kind="llm", status="skip",
                                   summary="LLM 미설정", branch_taken="해석 불가")])
            try:
                raw = interpret_question(question, ctx, client, model, add_ids, drop_ids)
            except Exception as exc:
                return _unsupported(question[:40], None,
                                    f"질문 해석 실패({type(exc).__name__}) — 잠시 후 다시 시도해 주세요.", [
                    NodeTraceEvent(node="질문 분류", kind="llm", status="fail",
                                   summary=f"LLM 호출 실패({type(exc).__name__})",
                                   branch_taken="해석 실패")])
        try:
            delta = WhatIfDelta.model_validate(raw.get("delta") or {})
        except Exception:
            _cache_evict(key)        # 오염·구형식(extra 필드) 엔트리 제거
            if cached:
                continue             # 캐시가 원인 → LLM 재해석 1회
            # LLM이 직접 낸 값이 한도 밖 — '범위 밖 질문'으로 뭉개면 "휴학을 지원한다면서
            # 왜 범위 밖이냐"는 오해 유발(코드R2). 한도를 구체 안내.
            return _unsupported(question[:40], None,
                                "해석된 변경값이 지원 한도를 벗어났습니다(휴학 최대 4학기 · 잔여 학기 ±4 "
                                "· 학점 상한 9~24) — 범위 안에서 다시 질문해 주세요.", [
                NodeTraceEvent(node="질문 분류", kind="llm",
                               summary=f"\"{question[:40]}\"", branch_taken="한도 초과"),
                NodeTraceEvent(node="매개변수 추출", kind="llm", status="warn",
                               summary="해석값이 지원 한도 초과", branch_taken="한도 초과")])
        # 자기모순 출력 가드(실가동 점검 발견): interpretable=true인데 delta가 전부 비면
        # 추출 실패다 — 캐시되면 해당 질문이 영구 '범위 밖'이 됨(S1 '다전공 빼면?' 실사례).
        # evict+재해석 1회, 그래도 비면 캐시 없이 재시도 안내. interpretable=false+빈 delta는
        # 정당한 '범위 밖' 해석이라 캐시·안내 유지.
        if raw.get("interpretable", False) and delta.is_empty():
            _cache_evict(key)
            if cached:
                continue
            return _unsupported(question[:40], None,
                                "질문에서 변경 조건을 추출하지 못했습니다 — 조금 더 구체적으로"
                                "(예: '부전공을 빼면?', '다음 학기 휴학하면?') 다시 시도해 주세요.", [
                NodeTraceEvent(node="질문 분류", kind="llm",
                               summary=f"\"{question[:40]}\"", branch_taken="추출 실패"),
                NodeTraceEvent(node="매개변수 추출", kind="llm", status="warn",
                               summary="해석은 됐으나 변경 조건이 비어 있음", branch_taken="추출 실패")])
        break

    # 의미 오염 가드(결정론) — 캐시 전·후 어느 경로든 동일 적용되어 결과 결정론 유지
    delta, guard_notes = _semantic_guard(question, delta)
    # category는 실제 채워진 delta 필드에서 결정론 재도출 — LLM 라벨 오기(휴학 질문에
    # '다전공변경' 등)가 그래프 분기 pill에 그대로 점등되는 것 방지(검증 e2e LOW)
    category = _derive_category(delta, raw.get("category"))
    qsum = str(raw.get("question_summary", ""))[:80]
    if not cached:
        _cache_put(key, raw)        # 검증 통과한 해석만 저장(캐시 포이즈닝 방지 — 코드R2)
    cache_note = " (캐시 — 동일 입력 동일 해석)" if cached else ""
    trace = [
        NodeTraceEvent(node="질문 분류", kind="llm",
                       summary=f"\"{question[:40]}\"{cache_note}", branch_taken=category),
        NodeTraceEvent(node="매개변수 추출", kind="llm",
                       summary=qsum or "해석 결과 없음",
                       branch_taken=("delta 추출" if not delta.is_empty() else "변경 없음")),
    ]

    # ② 조건 가드 (IF/ELSE)
    if not raw.get("interpretable", False) or delta.is_empty():
        return _unsupported(qsum or question[:40], category,
                            "이 질문은 시뮬레이션 범위 밖입니다 — 지원: 휴학, 잔여 학기 변경, "
                            "계절학기, 학기당 학점 상한, 다전공·부전공 추가/포기.", trace)
    new_ctx, changes, assumptions, err = apply_delta(ctx, delta, profile)
    assumptions = guard_notes + assumptions      # 환각 가드 발동을 사용자에게 명시(침묵 금지)
    if err:
        return _unsupported(qsum, category, err, trace)
    trace.append(NodeTraceEvent(node="조건 가드", kind="validator",
                                summary="; ".join(changes)[:120] or "변경값 검증 통과",
                                branch_taken="통과"))

    # ③ 졸업사정 재실행 (before/after 모두 서버 재계산 — 클라이언트 diff 불신뢰)
    try:
        before = run_audit(payload, skip_explain=True)
        after = run_audit({**payload, "context": new_ctx.model_dump()}, skip_explain=True)
    except (ValueError, KeyError) as exc:
        return _unsupported(qsum, category,
                            f"해당 변경은 시뮬레이션할 수 없습니다({exc}).", trace)

    # ④ 비교 / ⑤ 제안 (결정론)
    diff = build_diff(before, after, delta)
    acts = suggest_next_actions(diff, delta, ctx)
    trace += [
        NodeTraceEvent(node="졸업사정 재실행", kind="tool",
                       summary=f"부족 {diff.total_gap_before:.0f}→{diff.total_gap_after:.0f} · "
                               f"리스크 {diff.risk_before}→{diff.risk_after}",
                       branch_taken="before/after 2회 실행"),
        NodeTraceEvent(node="시나리오 비교", kind="tool", summary=diff.headline[:120],
                       branch_taken=f"리스크 {diff.risk_before}→{diff.risk_after}"),
        NodeTraceEvent(node="다음 행동 제안", kind="tool",
                       summary=(acts[0][:80] if acts else "제안 없음"),
                       branch_taken=f"{len(acts)}건"),
    ]
    return WhatIfResponse(status="ok", category=category, question_summary=qsum,
                          applied_changes=changes, assumptions=assumptions,
                          diff=diff, next_actions=acts, after=after, node_trace=trace)
