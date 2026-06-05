"""main.py
오케스트레이터 - 전체 파이프라인 제어.

흐름:
  [0] 프로필동기화 → [1] 수집 → [1.5] 분류 → 신규필터 → [2] 문서분석
   → [3] 맥락분석+Critic → [4] 일정검증 → 분야별 랭킹
   → (approval) 텔레그램 단건 승인  또는  (digest) 분야별 추천서 발송

실행 모드(.env DELIVERY_MODE):
  - approval : 최우선 1건을 텔레그램 버튼 승인 → 노션 실행 (기본)
  - digest   : 분야별 추천서를 Preferences 주기에 맞춰 텔레그램 발송 (스케줄러용)
옵션: PIPELINE_DRY_RUN=true → approval 모드에서 승인/실행 생략
"""
from __future__ import annotations

import os

# .env 로드 (python-dotenv 없으면 무시)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from modules import (scraper, document_ai, analyzer, validator, executor,
                     profile, classifier, history, preferences, digest,
                     deadline, feedback, store)

store.load_keys_into_env()   # 웹에서 저장한 API 키도 CLI 에서 사용


def banner(text: str) -> None:
    print("\n" + "=" * 56)
    print(f"  {text}")
    print("=" * 56)


def _make_candidate(n, result, validation: dict, fb: dict, warning: str = "") -> dict:
    urg = deadline.urgency_bonus(n)
    adj = fb.get(n.category, 0)
    cand = {
        "notice": n, "analysis": result, "validation": validation,
        "category": n.category, "urgency": urg, "feedback_adj": adj,
        "rank_score": result.suitability_score + urg + adj,
        "draft_link": f"https://notion.so/draft/{abs(hash(n.title)) % 100000}",
    }
    if warning:
        cand["schedule_warning"] = warning
    return cand


def _academic_info_analysis(n):
    return analyzer.AnalysisResult(
        is_relevant=True,
        suitability_score=0,
        matching_reason="국민대 공식 학사일정에서 7일 이내에 예정된 정보성 일정입니다. 순위 평가 없이 노출합니다.",
        estimated_hours_needed=0,
        domain="학사일정",
    )


def _scholarship_info_analysis(n):
    return analyzer.AnalysisResult(
        is_relevant=True,
        suitability_score=0,
        summary="",
        matching_reason="장학금 공지는 진로 적합도 순위보다 신청 가능성 확인이 중요한 정보성 추천입니다. 신청 기간, 대상, 제출서류를 확인할 수 있도록 추천함에 노출합니다.",
        estimated_hours_needed=2,
        domain="장학금",
    )


def _academic_info_validation() -> dict:
    return {
        "passed": True,
        "free_hours": 0,
        "safe_free_hours": 0,
        "safe_free_before_academic": 0,
        "buffer_hours": 0,
        "needed_hours": 0,
        "calendar_busy_hours": 0,
        "timetable_busy_hours": 0,
        "academic_pressure": {"label": "정보성 학사일정", "reason": "", "multiplier": 1.0},
    }


def _is_official_academic_notice(n) -> bool:
    return n.category == "학사일정" and getattr(n, "source", "") == "국민대 공식 학사일정"


def _is_scholarship_notice(n) -> bool:
    return n.category == "장학금"


def _schedule_warning(validation: dict) -> str:
    acad = validation.get("academic_pressure") or {}
    label = acad.get("label") or "학사일정 압박"
    reason = acad.get("reason") or "시험 일정"
    safe = validation.get("safe_free_hours", 0)
    needed = validation.get("needed_hours", 0)
    return (
        f"⚠시험기간 주의: 현재 {label}({reason}) 영향으로 "
        f"사용가능 {safe:.0f}h보다 예상 필요 {needed:.0f}h가 큽니다. "
        "시험 이후 시작하거나 작업량을 나누는 전제로 검토하세요."
    )


def _is_exam_time_hold(validation: dict, category: str) -> bool:
    if validation.get("passed"):
        return False
    if category in history.INFO_CATEGORIES or category == "기타":
        return False
    acad = validation.get("academic_pressure") or {}
    label = acad.get("label") or ""
    reason = acad.get("reason") or ""
    return "시험" in f"{label} {reason}"


def _attach_warning(result, warning: str):
    if not warning:
        return result
    reason = getattr(result, "matching_reason", "") or ""
    if not reason.startswith("⚠시험기간 주의"):
        result.matching_reason = warning + ("\n" + reason if reason else "")
    return result


def _gather_candidates(
    ctx: dict,
    force_reanalysis: bool = False,
    allowed_categories: set[str] | None = None,
) -> list:
    """수집 → 분류 → 마감/신규필터 → 분석 → Critic(규칙+LLM) → 일정검증 → 후보."""
    banner("[1] 정보수집 에이전트")
    notices = scraper.collect_notices(allowed_categories=allowed_categories)

    fb = feedback.category_adjustments()                          # 피드백 학습
    if history.enabled():
        print(f"[feedback] 분야별 학습 보정: {feedback.summary(fb)}")

    banner("[1.5~4] 분류 → 마감/신규필터 → 분석 → Critic(규칙+LLM) → 일정검증")
    candidates, skipped, expired = [], 0, 0
    warning_holds: dict[str, dict] = {}
    for i, n in enumerate(notices, 1):
        n.category = classifier.classify(n)                       # [1.5] 분류
        print(f"\n── 공지 {i}/{len(notices)} [{n.category}]: {n.title}")

        if allowed_categories is not None and n.category not in allowed_categories:
            print("   → 설정상 업데이트 대상 분야가 아니라 스킵")
            skipped += 1
            continue

        if deadline.is_expired(n):                                # 마감 가드
            print("   → 마감 지남, 제외")
            expired += 1
            history.record(n, status="만료", category=n.category)
            continue
        prior_status = history.status(n.url) if history.enabled() else None
        if force_reanalysis and prior_status in {"승인", "거절"}:
            print(f"   → 이미 {prior_status}한 공지, 스킵")
            skipped += 1
            continue
        if history.enabled() and not force_reanalysis and not history.is_new(n):  # 신규 필터
            print("   → 이미 처리한 공지, 스킵")
            skipped += 1
            continue

        if _is_official_academic_notice(n):
            result = _academic_info_analysis(n)
            v = _academic_info_validation()
            cand = _make_candidate(n, result, v, fb)
            candidates.append(cand)
            history.record(n, result, status="추천완료", category=n.category)
            print("   → 7일 이내 공식 학사일정, 정보성 공지로 추천")
            continue

        if _is_scholarship_notice(n):
            result = _scholarship_info_analysis(n)
            v = _academic_info_validation()
            cand = _make_candidate(n, result, v, fb)
            candidates.append(cand)
            history.record(n, result, status="추천완료", category=n.category)
            print("   → 장학금 정보성 공지로 추천")
            continue

        parsed = document_ai.parse_attachment(n.attachment_url)   # [2]
        result = analyzer.analyze(n, parsed, ctx)                 # [3]
        ok, _ = analyzer.critic_review(result, parsed)            # [3] 규칙 Critic

        passed = False
        if result.is_relevant and ok:
            v = validator.validate_schedule(result.estimated_hours_needed, ctx)  # [4]
            cand = _make_candidate(n, result, v, fb)
            if v["passed"]:
                passed = True
                # LLM 검증관은 기본 '비차단'(분야별 추천 다양성 우선).
                # CRITIC_LLM=strict 일 때만 프로필 부적합을 반려해 1순위 정밀도를 높임.
                if os.getenv("CRITIC_LLM", "off").lower() == "strict":
                    lok, _lr = analyzer.llm_critic(n, result, ctx)
                    if not lok:
                        passed = False
                        print("   → LLM 검증관 반려, 제외")
                if passed:
                    candidates.append(cand)
            elif _is_exam_time_hold(v, n.category):
                warning = _schedule_warning(v)
                cand["schedule_warning"] = warning
                best = warning_holds.get(n.category)
                if not best or cand["rank_score"] > best["rank_score"]:
                    warning_holds[n.category] = cand
                print("   → 시험기간 시간 초과, 분야 대표 후보로 보류")
            else:
                print("   → 일정 부족(HOLD), 제외")
        else:
            print("   → 관련성/검증 미통과, 제외")

        history.record(n, result, status="추천완료" if passed else "수집됨",
                       category=n.category)

    best_passed: dict[str, float] = {}
    for c in candidates:
        cat = c["category"]
        if cat in history.INFO_CATEGORIES:
            continue
        best_passed[cat] = max(best_passed.get(cat, float("-inf")), c["rank_score"])

    for cat, cand in warning_holds.items():
        if cand["rank_score"] < best_passed.get(cat, float("-inf")):
            continue
        warning = cand.get("schedule_warning", "")
        cand["analysis"] = _attach_warning(cand["analysis"], warning)
        candidates.append(cand)
        history.record(cand["notice"], cand["analysis"], status="추천완료", category=cat)
        print(f"   [warning] {cat} 최상위 1건을 '{warning.split(':', 1)[0]}' 추천으로 승격")

    if history.enabled():
        print(f"\n(스킵 {skipped}건 · 마감만료 {expired}건)")
    candidates.sort(key=lambda c: c["rank_score"], reverse=True)
    return candidates


def refresh_recommendations(force_reanalysis: bool = True) -> list:
    """웹 새로고침용: '끄기'를 제외한 분야를 최신 저장 데이터 기준으로 다시 분석."""
    mode = "DEMO" if os.getenv("DEMO_MODE", "true").lower() == "true" else "LIVE"
    banner(f"KMU Career Agent  [{mode} · web refresh]")
    ctx = profile.load_profile()
    print(f"대상: {ctx['name']} / {ctx['major']} / 희망: {ctx['desired_role']}")
    allowed = preferences.enabled_categories()
    if not allowed:
        print("추천 주기가 '끄기'가 아닌 분야가 없습니다. 재분석을 종료합니다.")
        return []
    print("수동 새로고침 분석 분야: " + ", ".join(sorted(allowed)))
    if force_reanalysis and history.enabled():
        reset = history.reset_pending()
        print(f"이전 검토대기 {reset}건을 재분석 전 보류 처리")
    candidates = _gather_candidates(ctx, force_reanalysis=force_reanalysis, allowed_categories=allowed)
    banner("웹 추천 새로고침 완료")
    print(f"추천 후보 {len(candidates)}건")
    return candidates


def _print_ranking(candidates: list) -> None:
    by_cat: dict[str, list] = {}
    for c in candidates:
        by_cat.setdefault(c["category"], []).append(c)
    for cat in classifier.CATEGORIES:
        if cat in by_cat:
            print(f"■ {cat}")
            for c in by_cat[cat]:
                a = c["analysis"]
                extra = []
                if c.get("urgency"):
                    extra.append(f"마감임박+{c['urgency']}")
                if c.get("feedback_adj"):
                    extra.append(f"학습{c['feedback_adj']:+d}")
                tag = f"  ({', '.join(extra)})" if extra else ""
                arrow = f"→{c['rank_score']}" if c["rank_score"] != a.suitability_score else ""
                print(f"   [{a.suitability_score}{arrow}점] {c['notice'].title} "
                      f"({a.estimated_hours_needed}h){tag}")


def run_pipeline() -> None:
    """approval 모드: 최우선 1건 텔레그램 승인 → 노션 실행."""
    mode = "DEMO" if os.getenv("DEMO_MODE", "true").lower() == "true" else "LIVE"
    banner(f"KMU Career Agent  [{mode} · approval]")
    ctx = profile.load_profile()
    print(f"대상: {ctx['name']} / {ctx['major']} / 희망: {ctx['desired_role']}")
    if history.enabled():
        print("이력 DB: 활성화 (신규 공지만 처리)")

    candidates = _gather_candidates(ctx)

    banner("후보 랭킹 (분야별)")
    if not candidates:
        print("조건을 만족하는 신규 추천 활동이 없습니다. 종료.")
        return
    _print_ranking(candidates)
    best = candidates[0]
    print(f"\n→ 최우선 추천: [{best['category']}] {best['notice'].title}")

    if os.getenv("PIPELINE_DRY_RUN", "false").lower() == "true":
        banner("DRY RUN - 승인/실행 생략 (이력만 기록됨)")
        return

    approved = executor.request_approval(best)                    # [5] HITL
    if approved:
        executor.execute_actions(best)                            # [6] 실행
        history.mark(best["notice"].url, "승인")
        banner("완료 - 노션 일정 등록 및 칸반 티켓 생성됨")
    else:
        history.mark(best["notice"].url, "거절")
        banner("사용자 거절 - 아무 액션도 실행하지 않음")


def digest_run() -> None:
    """digest 모드: Preferences 주기에 맞는 분야만 분석/저장/발송."""
    mode = "DEMO" if os.getenv("DEMO_MODE", "true").lower() == "true" else "LIVE"
    banner(f"KMU Career Agent  [{mode} · digest]")
    ctx = profile.load_profile()
    print(f"대상: {ctx['name']} / {ctx['major']} / 희망: {ctx['desired_role']}")

    due = preferences.due_categories()
    if not due:
        print("오늘 자동수신 주기에 해당하는 분야가 없습니다. 종료.")
        return
    print("오늘 자동 업데이트 분야: " + ", ".join(sorted(due)))

    prefs = preferences.load_preferences()
    telegram_due = {
        cat for cat in due
        if prefs.get(cat, {}).get("채널") == "텔레그램"
    }
    web_due = due - telegram_due
    candidates = []

    # Telegram delivery should not wait for all web-only categories to finish.
    if telegram_due:
        print("텔레그램 우선 분석 분야: " + ", ".join(sorted(telegram_due)))
        tg_candidates = _gather_candidates(ctx, allowed_categories=telegram_due)
        candidates.extend(tg_candidates)
        digest.deliver(tg_candidates, ctx)

    if web_due:
        print("웹 추천함 업데이트 분야: " + ", ".join(sorted(web_due)))
        candidates.extend(_gather_candidates(ctx, allowed_categories=web_due))

    banner("분야별 추천서 (Digest)")
    if not candidates:
        print("신규 추천이 없습니다.")
        return
    _print_ranking(candidates)


if __name__ == "__main__":
    if os.getenv("DELIVERY_MODE", "approval").lower() == "digest":
        digest_run()
    else:
        run_pipeline()
