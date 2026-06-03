"""modules/portfolio.py
Portfolio Analyzer 에이전트 (LLM 기반).

노션 포트폴리오(자유 서술 + Featured Projects DB)를 읽어 LLM으로 분석하고,
분석/추천에 쓰는 구조화 프로필(강점·관심사·희망직무·과거프로젝트 등)을 추출한다.
추출 결과는 '내 프로필' DB에 동기화 → 기존 파이프라인이 그대로 사용.

→ 포트폴리오만 바꿔 끼우면 누구에게나 적용되는 범용 프로필 소스.
"""
from __future__ import annotations

import json
import os
import re


def _client():
    from notion_client import Client
    return Client(auth=os.getenv("NOTION_API_KEY"))


def _block_text(b: dict) -> str:
    data = b.get(b["type"], {})
    if "rich_text" in data:
        return "".join(x["plain_text"] for x in data["rich_text"])
    return ""


def read_portfolio_text(page_id: str | None = None) -> str:
    """포트폴리오 페이지 블록 + Featured Projects DB 를 텍스트로 직렬화."""
    notion = _client()
    pid = page_id or os.getenv("NOTION_PORTFOLIO_PAGE_ID")
    lines: list[str] = []
    cursor = None
    while True:
        kw = {"block_id": pid, "page_size": 100}
        if cursor:
            kw["start_cursor"] = cursor
        r = notion.blocks.children.list(**kw)
        for b in r["results"]:
            txt = _block_text(b)
            if txt:
                lines.append(txt)
            if b["type"] == "child_database":            # Featured Projects
                for row in notion.databases.query(database_id=b["id"], page_size=25)["results"]:
                    cells = []
                    for name, pr in row["properties"].items():
                        ty = pr["type"]
                        v = ""
                        if ty == "title":
                            v = "".join(x["plain_text"] for x in pr["title"])
                        elif ty == "rich_text":
                            v = "".join(x["plain_text"] for x in pr["rich_text"])
                        elif ty == "select" and pr["select"]:
                            v = pr["select"]["name"]
                        elif ty == "multi_select":
                            v = ",".join(o["name"] for o in pr["multi_select"])
                        if v:
                            cells.append(f"{name}:{v}")
                    if cells:
                        lines.append("· " + " | ".join(cells))
        if r.get("has_more"):
            cursor = r["next_cursor"]
        else:
            break
    return "\n".join(lines)[:6000]


def extract_profile(text: str) -> dict:
    """LLM 으로 포트폴리오에서 구조화 프로필 추출."""
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    sys = (
        "너는 커리어 코치다. 아래 학생 포트폴리오를 읽고 JSON 으로만 프로필을 추출하라. "
        "키: name, school, major, desired_role(한 문장), "
        "high_proficiency(잘하는 핵심 역량 키워드 배열, 5~8개), "
        "low_proficiency(포트폴리오에서 상대적으로 약하거나 비중이 적은 영역 추론, 3~5개), "
        "interests(관심 분야 배열), past_projects(대표 프로젝트 제목 배열). "
        "역량은 한국어/영어 키워드로 간결히. JSON 외 텍스트 금지."
    )
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "system", "content": sys},
                  {"role": "user", "content": text}],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content
    data = json.loads(re.sub(r"```json|```", "", raw).strip())
    # 리스트 보정
    for k in ("high_proficiency", "low_proficiency", "interests", "past_projects"):
        v = data.get(k)
        if isinstance(v, str):
            data[k] = [x.strip() for x in re.split(r"[,;]", v) if x.strip()]
        elif not isinstance(v, list):
            data[k] = []
    return data


# 추출 프로필 → '내 프로필' DB 항목 매핑 (포트폴리오로 결정되는 항목만 갱신)
_SYNC_MAP = {
    "이름": lambda d: d.get("name", ""),
    "학교": lambda d: d.get("school", ""),
    "학과": lambda d: d.get("major", ""),
    "희망직무": lambda d: d.get("desired_role", ""),
    "과거프로젝트": lambda d: "; ".join(d.get("past_projects", [])),
    "강점": lambda d: ", ".join(d.get("high_proficiency", [])),
    "약점": lambda d: ", ".join(d.get("low_proficiency", [])),
    "관심사": lambda d: ", ".join(d.get("interests", [])),
}


def sync_to_profile_db(data: dict) -> int:
    """추출 프로필을 로컬 백엔드(store.profile)에 저장. 갱신 건수 반환.
    (미충족졸업요건/주간가용시간 등 포트폴리오로 알 수 없는 항목은 건드리지 않음)"""
    from modules import store
    vals = {k: fn(data) for k, fn in _SYNC_MAP.items()}
    vals = {k: v for k, v in vals.items() if v}
    store.set_profile(vals)
    return len(vals)


def analyze_and_sync(verbose: bool = True) -> dict:
    """포트폴리오 읽기 → LLM 분석 → 프로필 DB 동기화. 추출 dict 반환."""
    text = read_portfolio_text()
    if verbose:
        print(f"   [portfolio] 포트폴리오 텍스트 {len(text)}자 로드")
    data = extract_profile(text)
    if verbose:
        print(f"   [portfolio] 추출: {data.get('name')} / 강점 {len(data.get('high_proficiency',[]))}개 "
              f"/ 관심 {len(data.get('interests',[]))}개")
    n = sync_to_profile_db(data)
    if verbose:
        print(f"   [portfolio] '내 프로필' DB {n}개 항목 갱신 완료")
    return data
