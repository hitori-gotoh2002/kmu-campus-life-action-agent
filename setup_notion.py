"""setup_notion.py — 처음 사용자를 위한 노션 DB 자동 생성기.

노션 페이지 하나에 통합 '생비' 연결을 추가한 뒤 실행하면, 필요한 DB 7개를 만들고
.env 에 ID 를 자동 기록한다.

사용:
  1) 노션에서 빈 페이지를 만들고, 페이지 ··· → 연결 → 내 통합 추가
  2) 그 페이지 URL 끝의 32자리 ID 복사
  3) python setup_notion.py <페이지ID> [입학년도(기본 2026)]
"""
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
from notion_client import Client
from notion_client.errors import APIResponseError

THROTTLE = 0.34
ENV_PATH = Path(__file__).parent / ".env"
REQ_PATH = Path(__file__).parent / "config" / "requirements.json"


def dashed(pid: str) -> str:
    p = pid.replace("-", "")
    return f"{p[0:8]}-{p[8:12]}-{p[12:16]}-{p[16:20]}-{p[20:32]}" if len(p) == 32 else pid


def title(name):
    return [{"text": {"content": name}}]


def update_env(ids: dict):
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    keys = dict(ids)
    out = []
    for ln in lines:
        k = ln.split("=", 1)[0].strip() if "=" in ln else ""
        if k in keys:
            out.append(f"{k}={keys.pop(k)}")
        else:
            out.append(ln)
    for k, v in keys.items():
        out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    if len(sys.argv) < 2:
        print("사용법: python setup_notion.py <노션페이지ID> [입학년도]")
        return
    page = dashed(sys.argv[1])
    year = sys.argv[2] if len(sys.argv) > 2 else "2026"
    notion = Client(auth=os.getenv("NOTION_API_KEY"))

    try:
        notion.pages.retrieve(page_id=page)
    except APIResponseError:
        print("✗ 페이지 접근 불가. 노션에서 그 페이지에 통합 '연결'을 추가했는지 확인하세요.")
        return

    CAT = ["장학금", "공모전·대회", "대외활동·서포터즈", "학사일정", "채용·인턴", "자격증", "기타"]
    SRC = ["국민대 학사공지", "국민대 경영대학 공지", "링커리어 공모전", "링커리어 대외활동"]

    def db(name, props):
        d = notion.databases.create(parent={"type": "page_id", "page_id": page},
                                    title=title(name), properties=props)
        print(f"   + {name}")
        time.sleep(THROTTLE)
        return d["id"]

    print("[1] DB 생성 …")
    sel = lambda opts: {"select": {"options": [{"name": o} for o in opts]}}
    ids = {}
    ids["NOTION_PROFILE_DB_ID"] = db("내 프로필", {
        "항목": {"title": {}}, "값": {"rich_text": {}},
        "분류": sel(["기본", "강점", "약점", "관심사", "졸업요건", "일정"])})
    ids["NOTION_HISTORY_DB_ID"] = db("커리어 추천 이력", {
        "제목": {"title": {}}, "카테고리": sel(CAT), "출처": sel(SRC),
        "상태": sel(["신규", "수집됨", "추천완료", "승인", "거절", "만료"]),
        "적합도": {"number": {}}, "소요시간": {"number": {}}, "마감일": {"date": {}},
        "URL": {"url": {}}, "근거": {"rich_text": {}}, "도메인": {"rich_text": {}}})
    ids["NOTION_PREFS_DB_ID"] = db("추천 설정", {
        "분야": {"title": {}}, "주기": sel(["매일", "매주", "수동", "끄기"]),
        "채널": sel(["텔레그램", "웹"])})
    ids["NOTION_CALENDAR_DB_ID"] = db("내 캘린더", {
        "일정명": {"title": {}}, "요일": sel(list("월화수목금토일")),
        "시작": {"number": {}}, "종료": {"number": {}}, "날짜": {"date": {}},
        "교수": {"rich_text": {}}, "장소": {"rich_text": {}},
        "유형": sel(["수업", "학회", "스터디", "회의", "개인", "활동"])})
    ids["NOTION_KANBAN_DB_ID"] = db("칸반", {
        "Name": {"title": {}}, "Status": sel(["To Do", "In Progress", "Done"])})
    ids["NOTION_GRAD_REQ_DB_ID"] = db("졸업요건", {
        "구분": {"title": {}}, "기준학점": {"number": {}}, "비고": {"rich_text": {}}})
    ids["NOTION_GRAD_HISTORY_DB_ID"] = db("이수내역", {
        "과목명": {"title": {}}, "학점": {"number": {}},
        "이수구분": sel(["전공선택", "기초교양", "핵심교양", "자유교양", "일반선택", "기타"]),
        "성적": {"rich_text": {}}, "인정": sel(["인정", "제외(F/재수강)"])})

    print("[2] 기본값 시드 …")
    # 프로필 템플릿
    PROF = [("이름", "", "기본"), ("학교", "국민대학교", "기본"),
            ("학과", "AI빅데이터융합경영학과", "기본"), ("희망직무", "", "기본"),
            ("과거프로젝트", "", "기본"), ("강점", "", "강점"), ("약점", "", "약점"),
            ("관심사", "", "관심사"), ("미충족졸업요건", "", "졸업요건"),
            ("주간가용시간", "112", "일정"), ("안전버퍼비율", "0.3", "일정")]
    for it, v, c in PROF:
        notion.pages.create(parent={"database_id": ids["NOTION_PROFILE_DB_ID"]}, properties={
            "항목": {"title": title(it)}, "값": {"rich_text": title(v) if v else []},
            "분류": {"select": {"name": c}}}); time.sleep(THROTTLE)
    # 추천 설정 기본값
    PREF = {"장학금": "매일", "공모전·대회": "매일", "대외활동·서포터즈": "매일",
            "학사일정": "매주", "채용·인턴": "끄기", "자격증": "매주", "기타": "수동"}
    for cat, cyc in PREF.items():
        notion.pages.create(parent={"database_id": ids["NOTION_PREFS_DB_ID"]}, properties={
            "분야": {"title": title(cat)}, "주기": {"select": {"name": cyc}},
            "채널": {"select": {"name": "텔레그램"}}}); time.sleep(THROTTLE)
    # 졸업요건 (입학년도)
    req = json.loads(REQ_PATH.read_text(encoding="utf-8")).get(str(year), {})
    for k, v in req.items():
        if isinstance(v, (int, float)) and k != "심화전공_초과":
            notion.pages.create(parent={"database_id": ids["NOTION_GRAD_REQ_DB_ID"]}, properties={
                "구분": {"title": title(k)}, "기준학점": {"number": v},
                "비고": {"rich_text": title(f"{year} 기준")}}); time.sleep(THROTTLE)

    update_env(ids)
    print("\n[3] .env 자동 기록 완료:")
    for k, v in ids.items():
        print(f"   {k}={v.replace('-', '')}")
    print("\n끝! 이제 포트폴리오 페이지 ID(NOTION_PORTFOLIO_PAGE_ID)만 .env 에 넣으면 됩니다.")


if __name__ == "__main__":
    main()
