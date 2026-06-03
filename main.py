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


def _gather_candidates(ctx: dict) -> list:
    """수집 → 분류 → 마감/신규필터 → 분석 → Critic(규칙+LLM) → 일정검증 → 후보."""
    banner("[1] 정보수집 에이전트")
    notices = scraper.collect_notices()

    fb = feedback.category_adjustments()                          # 피드백 학습
    if history.enabled():
        print(f"[feedback] 분야별 학습 보정: {feedback.summary(fb)}")

    banner("[1.5~4] 분류 → 마감/신규필터 → 분석 → Critic(규칙+LLM) → 일정검증")
    candidates, skipped, expired = [], 0, 0
    for i, n in enumerate(notices, 1):
        n.category = classifier.classify(n)                       # [1.5] 분류
        print(f"\n── 공지 {i}/{len(notices)} [{n.category}]: {n.title}")

        if deadline.is_expired(n):                                # 마감 가드
            print("   → 마감 지남, 제외")
            expired += 1
            history.record(n, status="만료", category=n.category)
            continue
        if history.enabled() and not history.is_new(n):           # 신규 필터
            print("   → 이미 처리한 공지, 스킵")
            skipped += 1
            continue

        parsed = document_ai.parse_attachment(n.attachment_url)   # [2]
        result = analyzer.analyze(n, parsed, ctx)                 # [3]
        ok, _ = analyzer.critic_review(result, parsed)            # [3] 규칙 Critic

        passed = False
        if result.is_relevant and ok:
            v = validator.validate_schedule(result.estimated_hours_needed, ctx)  # [4]
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
                    urg = deadline.urgency_bonus(n)
                    adj = fb.get(n.category, 0)
                    candidates.append({
                        "notice": n, "analysis": result, "validation": v,
                        "category": n.category, "urgency": urg, "feedback_adj": adj,
                        "rank_score": result.suitability_score + urg + adj,
                        "draft_link": f"https://notion.so/draft/{abs(hash(n.title)) % 100000}",
                    })
            else:
                print("   → 일정 부족(HOLD), 제외")
        else:
            print("   → 관련성/검증 미통과, 제외")

        history.record(n, result, status="추천완료" if passed else "수집됨",
                       category=n.category)

    if history.enabled():
        print(f"\n(스킵 {skipped}건 · 마감만료 {expired}건)")
    candidates.sort(key=lambda c: c["rank_score"], reverse=True)
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
    """digest 모드: 분야별 추천서를 Preferences 주기에 맞춰 텔레그램 발송."""
    mode = "DEMO" if os.getenv("DEMO_MODE", "true").lower() == "true" else "LIVE"
    banner(f"KMU Career Agent  [{mode} · digest]")
    ctx = profile.load_profile()
    print(f"대상: {ctx['name']} / {ctx['major']} / 희망: {ctx['desired_role']}")

    candidates = _gather_candidates(ctx)

    banner("분야별 추천서 (Digest)")
    if not candidates:
        print("신규 추천이 없어 추천서를 보내지 않습니다.")
        return
    _print_ranking(candidates)
    digest.deliver(candidates, ctx)


if __name__ == "__main__":
    if os.getenv("DELIVERY_MODE", "approval").lower() == "digest":
        digest_run()
    else:
        run_pipeline()
