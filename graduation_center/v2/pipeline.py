"""v2 오케스트레이션 — verify / audit 두 단계 + JSON-first 응답 조립.

run_verify: 엑셀(여러 학기) → 매칭 → 편집 가능한 검증 테이블 반환(HITL).
run_audit : 사용자 확정 테이블 → 진단 → 로드맵(결정론 배치+검증) → 리스크 → AuditPipelineResponse.
"""
from __future__ import annotations

from graduation_center.v2.audit_v2 import compute_audit
from graduation_center.v2.catalog import (
    PREV_GPA_BONUS, SEASONAL_TERM_CAP, assemble_requirement_profile, load_programs,
    regular_term_cap,
)
from graduation_center.v2.excel_parser import parse_many
from graduation_center.v2.explain import run_explain, skip_explain_trace
from graduation_center.v2.models_v2 import (
    AuditPipelineResponse, CourseMatch, NodeTraceEvent, Source, StudentContext,
    VerifiedCourse,
)
from graduation_center.v2.planner import run_planner
from graduation_center.v2.risk import compute_risk
from graduation_center.v2.verification import build_verification_table, finalize_transcript


def run_verify(files: list[tuple[bytes, str]], context: dict) -> dict:
    ctx = StudentContext.model_validate(context)
    lines, retakes = parse_many(files)
    table, unresolved, retakes = build_verification_table(lines, ctx.program_id, retakes)
    matched_n = sum(1 for t in table if t.course_id and not t.aggregate_only)
    agg_n = sum(1 for t in table if t.aggregate_only)
    trace = [
        NodeTraceEvent(node="요람 로딩", kind="tool", summary=f"{ctx.program_id} 카탈로그·요건 로드"),
        NodeTraceEvent(node="데이터 수집", kind="tool",
                       summary=f"{len(files)}개 학기 파일 · {len(lines)}개 수강행", branch_taken=f"{len(files)}개 학기 병합"),
        NodeTraceEvent(node="코드 매칭", kind="tool",
                       summary=f"매칭 {matched_n} · 집계 {agg_n} · 미해소 {len(unresolved)}",
                       branch_taken=("미해소 있음" if unresolved else "전부 분류")),
    ]
    return {
        "context": ctx.model_dump(),
        "verification_table": [t.model_dump() for t in table],
        "unresolved": [u.model_dump() for u in unresolved],
        "possible_retakes": retakes,
        "node_trace": [e.model_dump() for e in trace],
    }


def run_audit(payload: dict, client=None, *, skip_explain: bool = False,
              run_summary: bool = False) -> AuditPipelineResponse:
    """run_summary: 에이전트 총평(능동 시나리오 탐색) — **독립 게이트, 기본 끔**.
    허용 경로는 /graduation/v2/audit 라우트와 워밍업 스크립트 opt-in뿐(계획 §1) —
    테스트·what-if 상담·총평 내부 시뮬레이션은 기본 False라 LLM 미진입(재귀도 구조적 차단)."""
    ctx = StudentContext.model_validate(payload["context"])
    table = [VerifiedCourse.model_validate(x) for x in payload.get("verification_table", [])]
    unresolved = [CourseMatch.model_validate(x) for x in payload.get("unresolved", [])]
    retakes = payload.get("possible_retakes", [])

    from graduation_center.v2.risk import already_met as _already_met
    verified = finalize_transcript(table, unresolved, retakes)
    profile = assemble_requirement_profile(ctx)
    audit = compute_audit(verified, profile, convergence_program_ids=ctx.convergence_program_ids,
                          convergence_tracks=ctx.convergence_tracks)
    plan, vrep, pctx = run_planner(audit, profile, ctx, verified, client=client)
    feasible = plan.feasible if plan.status != "not_generated" else None
    # 초과학기 1학기 완화(risk D→C)는 '실제 경로 검증' 후에만 — overflow의 extra는 학점 환산
    # 추정치라 개설학기·선수 제약으로 더 늘 수 있음(라운드5 codex). 잔여+1로 재배치가
    # 실제 feasible일 때만 overflow를 risk에 전달해 완화를 허용한다(결정론 재실행 1회).
    # 단 overflow 자체는 항상 risk에 전달 — 빼버리면 reason이 '계획 없음'으로 떨어져
    # 화면의 초과학기 카드와 무화해 병치가 재발(라운드6). 검증 결과는 별도 플래그로 게이트.
    overflow_verified = None
    if plan.status == "blocked" and plan.overflow and plan.overflow.extra_semesters <= 1:
        ctx_plus = ctx.model_copy(update={"remaining_semesters": ctx.remaining_semesters + 1})
        plan_plus, _, _ = run_planner(audit, profile, ctx_plus, verified, client=client)
        overflow_verified = plan_plus.feasible is True
    # 졸업 여유도 사다리 시나리오 런(결정론 — 사용자 ctx와 독립: 보너스·계절 고정).
    # 가지치기: S(이미 충족)·15캡 성공 시 다음 런 생략. current_term 없으면 배치 불가 → None 폴백.
    ladder = None
    if ctx.current_term:
        ladder = {"feasible_15": None, "feasible_legal": None, "feasible_seasonal": None}
        if not _already_met(audit, ctx):
            base = {"prev_term_gpa_ge_375": False, "seasonal_semester_allowed": False}
            c15 = ctx.model_copy(update={**base, "max_credits_per_term": 15.0})
            p15, _, _ = run_planner(audit, profile, c15, verified, client=None)
            ladder["feasible_15"] = p15.feasible is True
            if not ladder["feasible_15"]:
                cl = ctx.model_copy(update={**base, "max_credits_per_term": None})
                pl_, _, _ = run_planner(audit, profile, cl, verified, client=None)
                ladder["feasible_legal"] = pl_.feasible is True
                if not ladder["feasible_legal"]:
                    cs = ctx.model_copy(update={**base, "max_credits_per_term": None,
                                                "seasonal_semester_allowed": True})
                    ps_, _, _ = run_planner(audit, profile, cs, verified, client=None)
                    ladder["feasible_seasonal"] = ps_.feasible is True
    risk = compute_risk(audit, ctx, roadmap_feasible=feasible, overflow=plan.overflow,
                        overflow_verified=overflow_verified, ladder=ladder)
    # 근거(G1..) — 결정론 구성: 적용 요람·학사규정 제32/77조·융합 요람·교양과정.
    # (과거 LLM 플래너의 pctx["sources"]는 빈 배열이라 citation contract가 죽어 있었음)
    sources, marks = _build_sources(profile, ctx, audit)
    # 규정 근거 해설(보고서 내장 RAG) — LLM은 요람 chunk 해설만, 실패해도 본체 무영향.
    # skip_explain: what-if 재실행 경로용 — 결정론 판정만 필요해 해설(LLM·검색) 생략.
    if skip_explain:
        explanations, y_sources, explain_fallback = [], [], None
        explain_trace = skip_explain_trace("해설 생략(what-if 재실행)", "해설 생략")
    else:
        explanations, y_sources, explain_trace, explain_fallback = run_explain(
            audit, profile, ctx, client=client)
        # 미이수 필수는 결정론 섹션으로 합성 — run_explain 밖(trace·캐시·ungrounded 집계는
        # LLM 섹션만 대상, 캐시-라이브 모순 회피). 근거는 G1(적용 요람 졸업요건) — "필수=결정론,
        # 정책 해설=RAG" 역할 분리(3파트 develop §A, 미확인 줄의 61%가 이 항목의 LLM 인용 누락이었음).
        if audit.missing_required_names:
            from graduation_center.v2.models_v2 import ExplainLine, ExplainSection
            names = ", ".join(audit.missing_required_display or audit.missing_required_names)
            # 둘째 줄은 배치 결과 조건부 — 미배치 필수가 있는데 "배치되어 있습니다" 단정은
            # 미배치 칩과 정면 모순(적대 R1 HIGH — S3 실재 사례)
            placed_names = {c.name_ko for t in plan.terms for c in t.courses}
            unplaced_req = [n for n in audit.missing_required_names if n not in placed_names]
            line2 = ("개설 학기를 반영해 아래 추천 로드맵에 배치되어 있습니다." if not unplaced_req
                     else f"이 중 {', '.join(unplaced_req)}은(는) 잔여 학기에 배치되지 못했습니다 — "
                          "아래 미배치·초과학기 시나리오를 확인하세요.")
            det = ExplainSection(
                key="missing_required", deterministic=True,
                title=f"미이수 필수지정 과목 ({len(audit.missing_required_names)}과목) — 결정론 판정",
                lines=[
                    ExplainLine(text=f"{names} — 학과 교과과정표상 '필수' 지정 과목입니다"
                                f"({profile.applied_yoram} 졸업요건 기준).",
                                source_ids=["G1"], grounded=True),
                    ExplainLine(text=line2, source_ids=["G1"], grounded=True),
                ])
            explanations = [det] + explanations
        # S(요건 충족) 학생 + 조기졸업 RAG 섹션이 없을 때(LLM 무키·실패) — 공식 공지 curated
        # 텍스트로 결정론 fallback(계획 R2 MUST: Chroma/키 실패에도 안내 보장)
        if _already_met(audit, ctx) and not any(
                getattr(x, "key", "") == "early_graduation" for x in explanations):
            try:
                import json as _json
                pol = _json.loads((__import__("pathlib").Path(__file__).resolve().parents[1]
                                   / "data/graduation/policies.json").read_text(encoding="utf-8"))
                eg = (pol.get("early_graduation") or [{}])[0]
                if eg.get("text"):
                    from graduation_center.v2.models_v2 import ExplainLine, ExplainSection
                    sid_eg = f"G{len(sources) + 1}"
                    sources.append(Source(id=sid_eg, doc=f"조기졸업 승인 안내 — {eg.get('title', '공식 공지')}",
                                          page=None, source_type="requirement_rule", ref="조기졸업",
                                          url=eg.get("url")))
                    first = eg["text"].split(". ")
                    explanations.append(ExplainSection(
                        key="early_graduation", deterministic=True,
                        title="조기졸업 요건 안내 (요건 충족 — 공식 공지 기준)",
                        lines=[ExplainLine(text=". ".join(first[:3]) + ".",
                                           source_ids=[sid_eg], grounded=True),
                               ExplainLine(text="평점·등록학기 등 본인 해당 여부는 ON국민 졸업정보에서 확인하세요.",
                                           source_ids=[sid_eg], grounded=True)]))
            except Exception:
                pass                                  # 안내 fallback 실패는 본체 무영향
    sources += y_sources

    conv_n = len(audit.convergence_checks)
    n_terms = len(plan.terms)
    n_courses = sum(len(t.courses) for t in plan.terms)
    if plan.status == "blocked":
        plan_branch = "실현불가(초과학기 필요)"
    elif not plan.terms:
        plan_branch = "갭 없음(이미 충족)" if plan.feasible else "학기 배치 보류"
    else:
        plan_branch = f"{n_terms}학기 {n_courses}과목 배치"
    # 결정론 통합 플래너(LLM 미사용) — 노드는 'tool', 검증은 미배치/학점상한 결정론 체크
    trace = [
        NodeTraceEvent(node="데이터 검증", kind="hitl",
                       summary=f"확정 {len(verified.confirmed_courses)} · 제외 {len(verified.excluded)} · {verified.total_earned}학점",
                       branch_taken="사용자 확정"),
        NodeTraceEvent(node="갭 계산", kind="tool",
                       summary=f"총 부족 {audit.total_gap} · 필수누락 {len(audit.missing_required_names)} · 연계융합 {conv_n}건",
                       # '충족'은 총학점·영역·필수·융합(그룹 포함) 전부 충족일 때만 — 리포트 ⚠️와 모순 방지
                       branch_taken=(("부족 있음" if (
                           audit.total_gap > 0 or any(g.gap > 0 for g in audit.area_gaps)
                           or bool(audit.missing_required_names)
                           or any(cc.get("gap", 0) > 0 or any(gc["gap"] > 0 for gc in cc.get("group_checks", []))
                                  for cc in audit.convergence_checks)) else "충족")
                           + (f" · 융합 {conv_n}건" if conv_n else ""))),
        NodeTraceEvent(node="로드맵 배치", kind="tool",
                       status="ok" if plan.status != "blocked" else "warn",
                       summary=plan.why_this_plan or plan.blocked_reason or "", branch_taken=plan_branch),
        NodeTraceEvent(node="로드맵 검증", kind="validator",
                       status="ok" if vrep.ok else "fail",
                       summary=("통과(선수·개설학기·학점상한)" if vrep.ok else f"{len(vrep.errors)}건 미충족"),
                       branch_taken=("통과" if vrep.ok else "미배치 → 초과학기")),
        # 분기 갈래(그래프 토폴로지): 미배치 시에만 실행되는 사이드 노드 — feasible이면 미점등
        *([NodeTraceEvent(node="초과학기 시나리오", kind="tool",
                          summary=plan.overflow.note,
                          branch_taken=f"+{plan.overflow.extra_semesters}학기 → {plan.overflow.projected_graduation_term or '미상'}")]
          if plan.overflow else []),
        NodeTraceEvent(node="리스크 산정", kind="tool",
                       summary=f"{risk.grade} {risk.label} ({risk.score})",
                       # 완화(D→C 등) 발동 여부를 분기 라벨에 노출 — 학생별로 다른 경로가 보이게
                       branch_taken=f"{risk.grade} {risk.label}"
                       + (" · 완화 발동" if any("등급 완화" in r.detail for r in risk.reasons) else "")),
        *explain_trace,
    ]
    # 에이전트 총평(bounded ReAct 단일 턴) — lazy import + 함수 주입으로 순환 차단
    # (pipeline→report_summary→whatif→pipeline). run_summary=False 경로는 로직 자체 미진입.
    agent_summary, summary_fallback, summary_trace = None, None, []
    if run_summary:
        try:
            # import도 try 안 — 총평 모듈 import-time 오류까지 degrade(codex R2 CRITICAL)
            from graduation_center.v2.report_summary import _skip_trace, run_report_summary
            agent_summary, summary_fallback, summary_trace = run_report_summary(
                payload, audit, risk, plan, ctx, run_audit_fn=run_audit, client=client)
        except Exception as exc:                     # 총평 실패는 절대 /audit 500으로 안 샘(codex R1)
            agent_summary = None
            summary_fallback = f"총평 생성 오류({type(exc).__name__}) — 결정론 진단·로드맵은 유효"
            summary_trace = [NodeTraceEvent(node=n, kind=k, status="skip",
                                            summary="총평 내부 오류", branch_taken="총평 생략")
                             for n, k in (("갈림길 선정", "llm"), ("갈림길 시뮬레이션", "tool"),
                                          ("총평 생성", "llm"), ("총평 검증", "validator"))]
        trace += summary_trace
    md = _markdown(ctx, profile, audit, risk, plan, marks, explanations=explanations)
    if agent_summary:                                # markdown은 총평 경로에서만 append(적대 L1)
        md += "\n\n" + _summary_markdown(agent_summary)
    return AuditPipelineResponse(
        context=ctx, verified_transcript=verified, audit=audit, risk=risk,
        roadmap=plan, explanations=explanations, explain_fallback=explain_fallback,
        agent_summary=agent_summary, summary_fallback=summary_fallback,
        sources=sources, node_trace=trace, report_markdown=md,
    )


def _summary_markdown(s) -> str:
    L = ["## 에이전트 총평 (갈림길 시뮬레이션 기반)", f"**{s.headline}**" if s.headline else ""]
    for sc in s.scenarios:
        gt = (f" · 예상 졸업 {sc.graduation_term_before or '미상'}→{sc.graduation_term_after or '미상'}"
              if sc.graduation_term_before != sc.graduation_term_after else "")
        L.append(f"- [{sc.id}] {sc.label}: 리스크 {sc.risk_before}→{sc.risk_after}"
                 f" · 부족 {sc.total_gap_before:g}→{sc.total_gap_after:g}{gt}")   # 카드와 표기 일치(적대②)
    for ln in s.lines:
        L.append(f"- {ln.text} " + "".join(f"[{i}]" for i in ln.fact_ids))
    rejected = [r for r in s.candidates_review if r.verdict == "rejected"]
    if rejected:
        L.append("- 검토 후 제외: " + "; ".join(f"{r.label}({r.rejected_by})" for r in rejected))
    L.append(f"- **권고: {s.recommendation}**")
    return "\n".join(x for x in L if x)


def _source_links() -> dict:
    """원문 링크(요람 PDF·규정집) — 클릭 시 원문 이동(2026-06-05 사용자 제안).
    PDF 직링크는 재업로드 시 변동 가능 → 미보유 연도·깨짐 대비 index 폴백."""
    import json as _json
    from pathlib import Path as _P
    try:
        return _json.loads((_P(__file__).resolve().parents[1]
                            / "data/graduation/v2/source_links.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def yoram_url(year, page=None) -> str | None:
    L = _source_links()
    base = (L.get("yoram_pdf") or {}).get(str(year)) or L.get("yoram_index")
    if base and page and "thumbnail2.do" in base:
        return f"{base}#page={int(page)}"             # 브라우저 PDF 뷰어 페이지 점프
    return base


def _build_sources(profile, ctx, audit) -> tuple[list[Source], dict]:
    """결정론 근거 목록(G1..) + markdown 마커 매핑. 요람 페이지는 programs.json 기준."""
    progs = load_programs()
    pages = {pid: p.get("yoram_page") for pid, p in progs.items()}
    links = _source_links()
    sources: list[Source] = []
    marks: dict[str, str] = {}

    def add(key: str, doc: str, page=None, source_type="requirement_rule", ref=None, url=None):
        sid = f"G{len(sources) + 1}"
        sources.append(Source(id=sid, doc=doc, page=page, source_type=source_type, ref=ref, url=url))
        marks[key] = sid

    # yoram_page는 2025 요람 기준 — 다른 연도 요람 적용 시 페이지 비표시(틀린 페이지 인용 방지)
    yp = pages.get(ctx.program_id) if "2025" in (profile.applied_yoram or "") else None
    add("yoram", f"{profile.applied_yoram} — {profile.department_name_ko} 졸업요건(영역별 최저·필수지정)",
        page=yp, ref="졸업요건", url=yoram_url(profile.admission_year or 2025, yp))
    cap = regular_term_cap(profile.total_credits_min)
    add("cap", "학사규정 제32조(학기당 이수학점)", ref=f"정규 {cap:.0f}학점 · 계절 {SEASONAL_TERM_CAP:.0f}학점"
        f" · 직전학기 3.75 이상 시 +{PREV_GPA_BONUS:.0f}학점", url=links.get("rules"))
    if audit.convergence_checks:
        add("dup", "학사규정 제77조(학점 중복인정)", ref="다전공 12학점 / 부전공 6학점 한도", url=links.get("rules"))
        for cc in audit.convergence_checks:
            add(f"conv:{cc['program_id']}", f"2025 요람 — {cc['name']} 교육과정(그룹·요구학점)",
                page=pages.get(cc["program_id"]), source_type="catalog_course", ref=cc["program_id"],
                url=yoram_url(2025, pages.get(cc["program_id"])))
    add("gen", "2025 교양교육과정(핵심교양 영역별 최저)", page=4, source_type="gen_ed", ref="핵심교양",
        url=yoram_url(2025, 4))
    return sources, marks


def _markdown(ctx, profile, audit, risk, plan, marks: dict | None = None,
              explanations: list | None = None) -> str:
    m = marks or {}

    def mk(key):  # 근거 마커 — 섹션 헤더 수준에만 최소 부착(텍스트 덤프化 방지)
        return f" [{m[key]}]" if key in m else ""

    L = [f"# 졸업사정 컨설팅 리포트 — {profile.department_name_ko}",
         f"**종합 판정: {risk.grade} {risk.label}**  ·  총 {audit.total_earned:.0f}/{audit.total_required:.0f}학점"
         f"  ·  적용 요람 {profile.applied_yoram}{mk('yoram')}", "", f"## 영역별 현황{mk('yoram')}"]
    for g in audit.area_gaps:
        mark = "✅" if g.gap <= 0 else f"⚠️ {g.gap:.0f} 부족"
        # 전공 '학점'은 충족이어도 필수지정 미이수가 있으면 ✅만 띄우지 않음(모순 방지)
        if g.area == "전공" and g.gap <= 0 and audit.missing_required_names:
            mark = f"⚠️ 학점 충족 · 필수지정 {len(audit.missing_required_names)}과목 미이수"
        L.append(f"- {g.area}: {g.earned:.0f}/{g.required:.0f} {mark}")
    if audit.gyo_over_cap > 0:
        L.append(f"- ⚠️ 교양(기초+핵심+자유) 50학점 초과 {audit.gyo_over_cap:.0f}학점은 졸업학점 불인정(학사규정 제7조⑧){mk('cap')}")
    if audit.convergence_checks:
        L += ["", f"## 연계·융합전공 (학점 중복인정 반영){mk('dup')}"]
        for cc in audit.convergence_checks:
            mark = "✅" if cc["gap"] <= 0 else f"⚠️ {cc['gap']:.0f} 부족"
            L.append(f"- {cc['name']}({cc['track']}·{cc['conv_type']}): {cc['earned']:.0f}/{cc['required']:.0f} {mark}"
                     f"  [제1전공과 겹침 {cc['overlap_credits']:.0f} 중 중복인정 가능 {cc['double_recognizable']:.0f}/{cc['double_cap']:.0f}]")
            for gc in cc.get("group_checks", []):
                gm = "✅" if gc["gap"] <= 0 else f"⚠️ {gc['gap']:.0f} 부족"
                L.append(f"    · {gc['group']}: {gc['earned']:.0f}/{gc['required']:.0f} {gm}")
            if cc["recommend_double_count"]:
                L.append(f"  · 중복인정 신청 권장: {', '.join(cc['recommend_double_count'])}")
            if cc.get("note"):
                L.append(f"  · {cc['note']}")
    if not profile.required_course_ids:
        L += ["", f"※ {profile.department_name_ko} 요람 필수지정 과목 데이터 미구축 — 필수과목 체크 제외(확인 필요)"]
    if audit.missing_required_names:
        L += ["", f"## 미이수 필수지정{mk('yoram')}"] + [
            f"- {n}" for n in (audit.missing_required_display or audit.missing_required_names)]
    core_short = [g for g in audit.core_area_gaps if g.gap > 0]
    if core_short:
        L += ["", f"## 핵심교양 영역 부족{mk('gen')}"] + [f"- {g.area}: {g.earned:.0f}/{g.required:.0f}" for g in core_short]
    L += ["", f"## 추천 로드맵{mk('cap')}"]
    if plan.status == "not_generated":
        L.append("- (LLM 미설정 — 결정론 진단만 제공)")
    elif plan.status == "blocked":
        # 부분 배치가 있으면 숨기지 않고 보여준다(JSON 로드맵과 markdown 표면 일치).
        for t in plan.terms:
            courses = ", ".join(f"{c.name_ko}({c.credits:.0f})" for c in t.courses)
            L.append(f"- {t.term}: {courses}")
        L.append(("- ⚠️ 일부만 배치 가능: " if plan.terms else "- 실현 가능한 계획 없음: ")
                 + (plan.blocked_reason or ""))
        # overflow 섹션이 같은 사유를 다시 출력하므로 hint는 overflow 없을 때만(중복 방지)
        if plan.relaxation_hint and not plan.overflow:
            L.append(f"  - {plan.relaxation_hint}")
    elif not plan.terms:
        L.append(f"- {plan.why_this_plan}")
    else:
        for t in plan.terms:
            courses = ", ".join(f"{c.name_ko}({c.credits:.0f})" for c in t.courses)
            L.append(f"- {t.term}: {courses}")
        if plan.why_this_plan:
            L.append(f"  - 왜 이 계획: {plan.why_this_plan}")
    if plan.overflow:
        o = plan.overflow
        # note에 사유(용량/개설학기 제약)가 담겨 있으므로 그대로 사용 — '12<19인데 왜 못 채움'
        # 같은 단독 문장 어색함 방지(개설학기 제약 캐비엣 포함)
        L += ["", f"## ⚠️ 초과학기 예상 시나리오{mk('cap')}",
              f"- {o.note}",
              f"- 졸업까지 최소 **{o.total_semesters_needed}학기**(초과학기 **{o.extra_semesters}학기**) 필요"
              + (f" · 예상 졸업: **{o.projected_graduation_term}**" if o.projected_graduation_term else "")]
    if explanations:
        L += ["", "## 규정 근거 해설 (요람 원문 기반)"]
        for sec in explanations:
            L.append(f"### {sec.title}")
            for ln in sec.lines:
                cite = "".join(f"[{s}]" for s in ln.source_ids)
                L.append(f"- {ln.text} {cite}".rstrip())
    L += ["", "※ 본 진단 범위 외(해당 시 학과·교무팀 확인): 졸업논문·종합시험, 등록학기(8학기) 요건, "
              "외국인 한국어능력 인증(TOPIK), 편입·학석사연계 등 특수전형 학점인정."]
    return "\n".join(L)
