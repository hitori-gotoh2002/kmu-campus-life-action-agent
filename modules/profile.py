"""modules/profile.py
Profile Sync — 로컬 백엔드(store) 기반. (노션 아님)
포트폴리오 분석기가 store.profile 에 써둔 값을 읽어 분석용 ctx 를 만든다.
store 가 비어있거나 데모면 config/user_context.json 으로 폴백.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from modules import store

_FALLBACK = Path(__file__).parent.parent / "config" / "user_context.json"


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _load_json() -> dict:
    with open(_FALLBACK, encoding="utf-8") as f:
        return json.load(f)


def _split(s: str, sep: str = ",") -> list[str]:
    return [x.strip() for x in (s or "").split(sep) if x.strip()]


def load_profile() -> dict:
    if _is_demo():
        return _load_json()
    kv = store.get_profile()
    if not kv:
        return _load_json()
    ctx = {
        "name": kv.get("이름", ""),
        "school": kv.get("학교", ""),
        "major": kv.get("학과", ""),
        "desired_role": kv.get("희망직무", ""),
        "past_projects": _split(kv.get("과거프로젝트", ""), ";"),
        "high_proficiency": _split(kv.get("강점", "")),
        "low_proficiency": _split(kv.get("약점", "")),
        "interests": _split(kv.get("관심사", "")),
        "unmet_graduation_requirement": kv.get("미충족졸업요건", ""),
        "scheduling": {
            "weekly_total_hours": int(float(kv.get("주간가용시간") or 112)),
            "safe_buffer_ratio": float(kv.get("안전버퍼비율") or 0.3),
        },
    }
    return ctx
