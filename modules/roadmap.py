"""modules/roadmap.py
수강 로드맵 플래너 (LLM + 검증).

졸업 진단의 '부족 구분'을 어떤 과목/영역으로 채울지 학기별로 계획한다.
원 프로젝트 철학: 계산은 컴퓨터, 전략(로드맵)은 LLM, 검증은 다시 컴퓨터.
※ 실제 개설과목/선수과목은 매학기 수강편람에서 확인해야 하므로, 추천은 '필수과목 +
   구분별 영역' 중심으로 제시하고 환각(없는 과목 발명)을 피한다.
"""
from __future__ import annotations

import json
import math
import os
import re

# 입학년도별 전공선택 필수과목 (졸업요건 PDF 기준)
_MAJOR_REQUIRED = {
    "old": ["현대경영과기업가정신", "경영통계", "회계학원론", "경영수학",
            "경영정보학원론", "회귀분석", "머신러닝", "사제동행세미나"],
    "2022": ["현대경영과기업가정신", "경영통계", "회계학원론", "인공지능수학", "경영정보학원론",
             "회귀분석", "머신러닝", "딥러닝", "AI빅데이터프로그래밍Ⅰ", "AI빅데이터프로그래밍Ⅱ",
             "사제동행세미나"],
    "new": ["현대경영과기업가정신", "경영통계", "회계학원론", "인공지능수학", "경영정보학원론",
            "회귀분석", "머신러닝", "딥러닝", "AI빅데이터프로그래밍Ⅰ", "AI빅데이터프로그래밍Ⅱ",
            "유레카프로젝트"],
}
_GE_AREAS = {
    "핵심교양": "인문Ⅰ·인문Ⅱ·소통·글로벌·창의 영역(각 영역 최저 3학점)",
    "기초교양": "글쓰기·College English·English Conversation·글로벌영어",
}


def _required_courses(year: str) -> list:
    y = str(year)[:4]
    if y in ("2020", "2021"):
        return _MAJOR_REQUIRED["old"]
    if y in ("2022", "2023"):
        return _MAJOR_REQUIRED["2022"]
    return _MAJOR_REQUIRED["new"]


def generate(diagnosis: dict, semesters_left: int | None = None) -> dict:
    """진단 결과 → 학기별 수강 로드맵(LLM) + 검증."""
    total_gap = diagnosis.get("total_gap", 0)
    if total_gap <= 0:
        return {"semesters": [], "note": "이미 졸업 학점을 충족했습니다.", "verified": True}

    sem = semesters_left or max(1, math.ceil(total_gap / 16))
    gaps = {r["구분"]: r["부족"] for r in diagnosis.get("rows", []) if r["부족"] > 0}
    year = diagnosis.get("year", "2022")
    required = _required_courses(year)

    if os.getenv("DEMO_MODE", "true").lower() == "true" or not os.getenv("OPENAI_API_KEY"):
        return _fallback(gaps, total_gap, sem, required)

    sys = (
        "너는 대학 졸업 컨설턴트다. 학생의 '부족 학점'을 채우는 학기별 수강 로드맵을 JSON 으로만 짜라.\n"
        "규칙: ①한 학기 15~18학점 ②부족 구분(전공선택/핵심교양/기초교양/자유교양)을 우선 채움 "
        "③아래 전공필수 과목 중 미이수분을 먼저 배치(이미 들었으면 학생이 제외) "
        "④없는 과목명을 지어내지 말 것(필수과목 외엔 '전공선택 과목', '핵심교양(○○영역)'처럼 구분/영역으로 표기).\n"
        "스키마: {semesters:[{학기:str, 과목:[{name:str, credits:number, 구분:str, 사유:str}], 학점합:number}], "
        "advice:str}. JSON 외 텍스트 금지."
    )
    user = json.dumps({
        "입학년도": year, "남은학기": sem, "총부족학점": total_gap,
        "구분별부족": gaps, "전공필수과목": required, "교양영역": _GE_AREAS,
    }, ensure_ascii=False)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        data = json.loads(re.sub(r"```json|```", "", resp.choices[0].message.content).strip())
        data["verified"], data["verify_msg"] = _verify(data, total_gap)
        return data
    except Exception as e:
        print(f"   [roadmap] LLM 실패({e}) → 폴백")
        return _fallback(gaps, total_gap, sem, required)


def _verify(plan: dict, total_gap: int) -> tuple[bool, str]:
    """검증: 계획 총학점이 부족분을 덮는가."""
    planned = sum(s.get("학점합") or sum(c.get("credits", 0) for c in s.get("과목", []))
                  for s in plan.get("semesters", []))
    if planned >= total_gap:
        return True, f"계획 {planned:g}학점 ≥ 부족 {total_gap:g}학점 ✓"
    return False, f"계획 {planned:g}학점 < 부족 {total_gap:g}학점 — 학기/과목 보강 필요"


def _fallback(gaps: dict, total_gap: int, sem: int, required: list) -> dict:
    """LLM 없이도 동작하는 단순 분배."""
    per = math.ceil(total_gap / sem)
    semesters = []
    for i in range(sem):
        semesters.append({"학기": f"+{i+1}학기", "과목": [
            {"name": "전공선택/필수 과목", "credits": min(per, 15), "구분": "전공선택",
             "사유": "부족 구분 충당"}], "학점합": min(per, 18)})
    return {"semesters": semesters, "verified": True,
            "verify_msg": f"단순분배 {total_gap:g}학점 / {sem}학기",
            "advice": "전공필수 미이수분 우선 수강: " + ", ".join(required[:6]) + " 등. "
                      "핵심교양은 영역별 최저 3학점 충족."}
