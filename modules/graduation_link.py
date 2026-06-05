"""modules/graduation_link.py
졸업진단(graduation_center.v2) ↔ 추천 시스템 연동 + 카탈로그 헬퍼.

설계(사용자 결정 2026-06-05):
- '미이수 필수과목'은 추천에 **반영하지 않는다**(졸업진단에서 해결할 문제).
- '이수 완료 과목'을 **보유 역량(긍정 신호)** 으로 추천 프로필에 전달 → 분석기가
  "이 활동에 필요한 역량 과목을 이미 들었는지" 판단에 활용.
- '영역별 부족(전공/교양 학점)'은 정보성으로만 전달(기존 동작 보존).
- 복잡해지면 분리 가능하도록 store.profile 키로만 느슨하게 연결한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from modules import store

_GRAD_DATA = Path(__file__).resolve().parent.parent / "graduation_center" / "data" / "graduation"


def load_programs() -> dict:
    """programs.json(학과·융합전공 메타) 로드."""
    try:
        p = _GRAD_DATA / "v2" / "programs.json"
        return json.loads(p.read_text(encoding="utf-8")).get("programs", {})
    except Exception:
        return {}


def _completed_course_names(audit_dump: dict) -> list[str]:
    verified = audit_dump.get("verified_transcript", {}) or {}
    confirmed = verified.get("confirmed_courses", []) or []
    names = {(c.get("name_ko") or "").strip() for c in confirmed}
    return sorted(n for n in names if n)


def _area_gap_summary(audit_dump: dict) -> str:
    """영역별/총 부족 학점 요약(과목명 X — 미이수 필수과목은 의도적으로 제외)."""
    a = audit_dump.get("audit", {}) or {}
    parts = [f"{g['area']} {g['gap']:g}학점 부족"
             for g in a.get("area_gaps", []) if (g.get("gap") or 0) > 0]
    total_gap = a.get("total_gap") or 0
    if total_gap > 0:
        parts.append(f"총 {total_gap:g}학점 부족")
    return "; ".join(parts)


def sync_to_recommender(audit_dump: dict) -> dict:
    """진단 결과를 추천 프로필(store.profile)에 반영.
    반환: 반영 요약(디버그/표시용)."""
    completed = _completed_course_names(audit_dump)
    gap_summary = _area_gap_summary(audit_dump)
    store.set_profile({
        # 보유 역량(긍정 신호) — profile.load()가 ctx['completed_courses']로 노출
        "이수과목": "; ".join(completed)[:1800],
        # 영역 부족(정보성) — 기존 분석기 가점 로직과 호환(과목명 미포함)
        "미충족졸업요건": gap_summary,
    })
    return {"completed_count": len(completed), "gap_summary": gap_summary}
