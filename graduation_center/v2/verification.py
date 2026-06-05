"""사용자 검증(HITL) — 신뢰성 다리 (결정론).

수강내역엔 성적이 없으므로 F/재수강을 자동 판정하지 않는다. 동일 코드가 여러 학기
나오면(=재수강 가능) 첫 이수만 기본 포함, 나머지는 기본 제외(확인 필요)로 두고 사용자가
조정한다. 카탈로그 밖 과목은 aggregate_only로 집계영역에만 반영.
"""
from __future__ import annotations

import re

from graduation_center.v2.catalog import area_from_isugubun, match_course
from graduation_center.v2.models_v2 import (
    CourseMatch, RawLine, VerifiedCourse, VerifiedTranscript,
)


def _term_order(label: str) -> tuple[int, int]:
    """학기 라벨 → 정렬 키 (1학기 < 하계 < 2학기 < 동계, 클수록 최신).

    실파일 라벨 변형 견고화: '여름/겨울 계절학기', 계절구분 미표기('계절학기'만 — 하계로 간주,
    학년도 기준 여름이 먼저), 2자리 연도('23학년도'). 미인식 라벨은 (0,0)으로 맨 앞 고정."""
    label = label or ""
    m = re.search(r"(\d{4})", label)
    if m:
        year = int(m.group(1))
    else:
        m2 = re.search(r"(\d{2})\s*학년도", label)
        year = 2000 + int(m2.group(1)) if m2 else 0
    if "1학기" in label:
        sem = 1
    elif "동계" in label or "겨울" in label:
        sem = 4
    elif "2학기" in label:
        sem = 3
    elif "하계" in label or "여름" in label or "계절" in label:
        sem = 2          # 계절구분 미표기는 하계 간주(학년도 내 첫 계절학기)
    else:
        sem = 0
    return (year, sem)


def build_verification_table(
    lines: list[RawLine], program_id: str, retakes: list[dict],
) -> tuple[list[VerifiedCourse], list[CourseMatch], list[dict]]:
    """매칭 → 편집 가능한 검증 테이블 + unresolved + possible_retakes."""
    retake_codes = {r["course_code"] for r in retakes}
    # 재수강 코드의 '최신 이수' 학기 (가장 마지막 이수만 기본 포함)
    latest: dict[str, tuple[int, int]] = {}
    for ln in lines:
        if ln.course_code in retake_codes:
            o = _term_order(ln.term_label)
            if ln.course_code not in latest or o > latest[ln.course_code]:
                latest[ln.course_code] = o
    table: list[VerifiedCourse] = []
    unresolved: list[CourseMatch] = []
    used_latest: set[str] = set()
    for ln in lines:
        m = match_course(ln, program_id)
        if m.status == "unresolved":
            unresolved.append(m)
            continue
        area = m.requirement_area or area_from_isugubun(ln.area_raw)
        # 강등 복원(2026-06-04 재결정): '전공계 이수구분 + 카탈로그 미스'는 보수적으로 일반선택.
        # 자동 신뢰는 전과 등 이수구분 자체가 틀린 케이스에 오신뢰 위험 — 대신 HITL 테이블에서
        # 사용자가 이수구분을 직접 수정 가능(demoted_from_major 플래그로 강등 행 표시·일괄 복구).
        demoted = False
        if m.status == "aggregate_only" and area == "전공":
            area, demoted = "일반선택", True
        included, reason, grade_suspect = True, None, False
        code = ln.course_code
        # 폐강 자동 제외
        if "폐강" in (ln.note or "") or "폐강" in (ln.area_raw or ""):
            included, reason = False, "폐강"
        # 비고에 F/NP류 표기가 있으면 침묵 산입 금지(합성 검증 γ HIGH) — 단 자동 제외는 안 함
        # (비고 의미가 불확실 — 성적표가 아니므로). HITL 경고 표기로 사용자 판단 위임.
        elif re.search(r"(?<![A-Za-z])(F|NP|W)(?![A-Za-z])|미취득|낙제", ln.note or ""):
            grade_suspect = True
        elif code and code in retake_codes:
            # 최신 이수만 포함, 이전 이수는 제외(확인 필요)
            if _term_order(ln.term_label) == latest[code] and code not in used_latest:
                used_latest.add(code)
            else:
                included, reason = False, "재수강(이전 이수)"
        table.append(VerifiedCourse(
            # 실제 엑셀 교과목코드를 항상 보존(제1전공 카탈로그 밖 과목도 융합전공 코드매칭 가능하도록).
            course_id=ln.course_code or m.matched_course_id,
            name_ko=ln.course_name,
            credits=ln.credits,
            requirement_area=area,
            core_area=m.core_area,
            term_label=ln.term_label,
            included=included,
            exclude_reason=reason,
            grade_suspect=grade_suspect,
            aggregate_only=(m.status == "aggregate_only"),
            demoted_from_major=demoted,
        ))
    # 학기 오름차순(과거→최신) 정렬 — 화면 표시·검토 순서
    table.sort(key=lambda vc: _term_order(vc.term_label))
    return table, unresolved, retakes


def finalize_transcript(
    table: list[VerifiedCourse], unresolved: list[CourseMatch], retakes: list[dict],
) -> VerifiedTranscript:
    """확정 테이블(사용자 편집 반영) → 집계."""
    earned_by_area: dict[str, float] = {}
    core_area_earned: dict[str, float] = {}
    total = 0.0
    confirmed, excluded = [], []
    for vc in table:
        if vc.included:
            confirmed.append(vc)
            earned_by_area[vc.requirement_area] = earned_by_area.get(vc.requirement_area, 0.0) + vc.credits
            total += vc.credits
            if vc.requirement_area == "핵심교양" and vc.core_area:
                core_area_earned[vc.core_area] = core_area_earned.get(vc.core_area, 0.0) + vc.credits
        else:
            excluded.append(vc)
    return VerifiedTranscript(
        confirmed_courses=confirmed,
        excluded=excluded,
        unresolved=unresolved,
        possible_retakes=retakes,
        earned_by_area=earned_by_area,
        core_area_earned=core_area_earned,
        total_earned=round(total, 1),
    )
