"""modules/profile.py
Profile Sync 에이전트.
노션 '내 프로필' DB(항목/값)를 읽어 분석용 ctx(dict)를 생성한다.
- DEMO 또는 DB 미설정/실패 시 config/user_context.json 으로 폴백.
- 사용자가 노션에서 값을 고치면 다음 실행부터 자동 반영.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_FALLBACK = Path(__file__).parent.parent / "config" / "user_context.json"


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _load_json() -> dict:
    with open(_FALLBACK, encoding="utf-8") as f:
        return json.load(f)


def _split(s: str, sep: str = ",") -> list[str]:
    return [x.strip() for x in (s or "").split(sep) if x.strip()]


def _read_kv() -> dict:
    """프로필 DB 의 (항목→값) 딕셔너리."""
    from notion_client import Client
    notion = Client(auth=os.getenv("NOTION_API_KEY"))
    db = os.getenv("NOTION_PROFILE_DB_ID")
    kv, cursor = {}, None
    while True:
        kw = {"database_id": db, "page_size": 100}
        if cursor:
            kw["start_cursor"] = cursor
        r = notion.databases.query(**kw)
        for p in r["results"]:
            props = p["properties"]
            t = props.get("항목", {}).get("title", [])
            item = t[0]["plain_text"] if t else ""
            rt = props.get("값", {}).get("rich_text", [])
            val = rt[0]["plain_text"] if rt else ""
            if item:
                kv[item.strip()] = val.strip()
        if r.get("has_more"):
            cursor = r["next_cursor"]
        else:
            break
    return kv


def load_profile() -> dict:
    """분석에 쓸 사용자 프로필(dict) 반환."""
    if _is_demo() or not os.getenv("NOTION_PROFILE_DB_ID"):
        return _load_json()
    try:
        kv = _read_kv()
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
        print(f"   [profile] 노션 프로필 DB 로드 ({len(kv)}개 항목) → {ctx['name']}")
        return ctx
    except Exception as e:
        print(f"   [profile] 노션 프로필 로드 실패({e}) → json 폴백")
        return _load_json()
