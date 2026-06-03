"""modules/graduation.py
Graduation Center 에이전트 (결정론적 진단).

성적증명서의 '이수구분별 취득학점'과 졸업요건(config/requirements.json, 입학년도별)을
비교해 구분별 부족 학점·위험도를 산출. 미충족 요건은 추천 시스템(프로필)으로 전달.

졸업요건 구분(실제): 전공선택 / 기초교양 / 핵심교양 / 자유교양 (+ 총 이수학점).
일반선택은 잔여(overflow 흡수) 성격이라 '부족'으로 다루지 않고 정보성으로만 표시.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_REQ_PATH = Path(__file__).parent.parent / "config" / "requirements.json"

# 최저이수 구분(부족 판정 대상)
MIN_CATEGORIES = ["전공선택", "기초교양", "핵심교양", "자유교양"]

# 성적증명서 이수구분 → 졸업요건 구분 매핑
_CAT_MAP = {
    "전공필수": "전공선택", "전공선택": "전공선택",
    "전공기초(MSC)": "전공선택", "학부기초": "전공선택", "전공기초교양": "전공선택",
    "기초교양": "기초교양", "교양필수": "기초교양",
    "핵심교양": "핵심교양", "교양선택": "핵심교양", "계열교양": "핵심교양",
    "자유교양": "자유교양",
    "일반선택": "일반선택",
}


def _client():
    from notion_client import Client
    return Client(auth=os.getenv("NOTION_API_KEY"))


def load_requirements(admission_year) -> dict:
    """입학년도별 졸업요건. JSON 우선, 없으면 빈 dict."""
    y = str(admission_year).strip()[:4]
    try:
        with open(_REQ_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get(y, {})
    except Exception as e:
        print(f"   [graduation] 졸업요건 로드 실패({e})")
        return {}


def diagnose(transcript_data: dict) -> dict:
    """성적증명서 추출 dict → 졸업 진단."""
    student = transcript_data.get("student", {}) or {}
    year = student.get("admission_year") or ""
    req = load_requirements(year)
    by_cat = transcript_data.get("by_category", {}) or {}
    summary = transcript_data.get("summary", {}) or {}
    total_earned = summary.get("total_credits") or sum(by_cat.values())

    # 성적 이수구분 → 졸업요건 구분 집계
    earned = {}
    for tcat, cr in by_cat.items():
        rcat = _CAT_MAP.get(tcat, tcat)
        earned[rcat] = earned.get(rcat, 0) + cr

    rows = []
    for cat in MIN_CATEGORIES:
        required = req.get(cat, 0)
        e = earned.get(cat, 0)
        gap = max(0, required - e)
        pct = min(100, round(e / required * 100)) if required else 100
        rows.append({"구분": cat, "기준": required, "이수": e, "부족": gap, "달성률": pct})

    total_req = req.get("총 이수학점", 130)
    total_gap = max(0, total_req - total_earned)
    free_earned = earned.get("일반선택", 0)   # 정보성

    unmet = [f"{r['구분']} {r['부족']}학점 부족" for r in rows if r["부족"] > 0]
    if total_gap > 0:
        unmet.append(f"총 {total_gap}학점 부족")

    # 위험도
    major_gap = next((r["부족"] for r in rows if r["구분"] == "전공선택"), 0)
    if total_gap <= 0 and not any(r["부족"] for r in rows):
        risk = ("졸업 가능", "safe")
    elif total_gap >= 50 or major_gap >= 20:
        risk = ("주의", "caution")
    else:
        risk = ("안정권", "safe")

    return {
        "year": str(year)[:4],
        "major": req.get("major", student.get("major", "")),
        "rows": rows,
        "free_earned": free_earned,
        "total_earned": total_earned,
        "total_required": total_req,
        "total_gap": total_gap,
        "gpa": summary.get("gpa"),
        "risk_label": risk[0],
        "risk_level": risk[1],
        "unmet": unmet,
        "requirements_set": bool(req),
        "cert_note": "졸업인증제: 심화전공 또는 다·부전공 1개 이상 이수 필요"
                     + (f" (전공선택 +{req.get('심화전공_초과')}학점 초과 시 심화전공 인정)"
                        if req.get("심화전공_초과") else ""),
    }


def sync_unmet_to_profile(unmet: list) -> None:
    """미충족 요건 요약을 '내 프로필' DB 의 미충족졸업요건에 기록 → 추천 반영."""
    db = os.getenv("NOTION_PROFILE_DB_ID")
    if not db or not unmet:
        return
    summary = "; ".join(unmet)
    try:
        for p in _client().databases.query(database_id=db, page_size=100)["results"]:
            t = p["properties"].get("항목", {}).get("title", [])
            if t and t[0]["plain_text"].strip() == "미충족졸업요건":
                _client().pages.update(page_id=p["id"], properties={
                    "값": {"rich_text": [{"text": {"content": summary[:1900]}}]}})
                print(f"   [graduation] 미충족졸업요건 → 프로필: {summary[:60]}")
                return
    except Exception as e:
        print(f"   [graduation] 프로필 반영 실패({e})")
