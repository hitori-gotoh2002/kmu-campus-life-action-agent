"""modules/scraper.py
학사/공모전 공지 수집 에이전트.

핵심 제약:
  1) 봇 차단 회피  - 표준 브라우저 User-Agent 주입 + 요청 간 1.5~3.0초 무작위 지연
  2) 휘발성 처리   - 긁어온 HTML 원본을 디스크에 저장하지 않고 RAM에서 즉시
                     제목/본문/첨부만 추출한 뒤 파기 (잔여 파일 오염 방지)

실연 대상 (AI빅데이터융합경영학과 기준):
  - 국민대 학사공지(본부) / 국민대 전체 장학공지 / 경영대학 공지 / 경영대학 장학공지 / SW 취업공지
  - 링커리어 공모전·대외활동·채용/인턴·교육/자격증
  * 국민대 공지 사이트는 서버 렌더링이라 requests + BeautifulSoup 로 수집 가능.
  * 링커리어는 JS-SPA라 HTML 대신 GraphQL API를 사용한다.
"""
from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

# 표준 브라우저로 위장하기 위한 헤더 (방화벽의 매크로 탐지 우회)
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# AI빅데이터융합경영학과 시연용 초점 키워드.
# 소스 자체는 매일 새로 긁되, 추천 후보는 데이터/AI/경영/기획 축으로 좁혀 과도한 노이즈를 막는다.
AI_BIGDATA_FOCUS_KEYWORDS = (
    "AI", "인공지능", "빅데이터", "데이터", "데이터분석", "분석", "애널리틱스",
    "머신러닝", "딥러닝", "LLM", "생성형", "Python", "파이썬", "SQL", "통계",
    "디지털", "DX", "IT", "SW", "소프트웨어", "개발", "해커톤",
    "서비스", "기획", "마케팅", "경영", "비즈니스", "금융", "핀테크",
    "공공데이터", "창업", "프로덕트", "PM", "UX",
)
CAREER_FOCUS_KEYWORDS = AI_BIGDATA_FOCUS_KEYWORDS + (
    "인턴", "현장실습", "직무체험", "신입", "career", "recruit",
    "데이터사이언티스트", "데이터 사이언티스트", "데이터 엔지니어",
)
CERT_FOCUS_KEYWORDS = AI_BIGDATA_FOCUS_KEYWORDS + (
    "자격증", "자격", "SQLD", "ADsP", "ADP", "DAsP", "빅데이터분석기사",
    "정보처리기사", "컴퓨터활용능력", "컴활", "부트캠프", "아카데미",
    "강의", "특강", "과정",
)

# ---------------------------------------------------------------------------
# 실제 수집 대상 (확장 가능: name/url/base/parser/kind 만 추가하면 됨)
# ---------------------------------------------------------------------------
TARGET_SOURCES = [
    {
        "name": "국민대 학사공지",
        "url": "https://www.kookmin.ac.kr/user/kmuNews/notice/index.do",
        "base": "https://www.kookmin.ac.kr",
        "parser": "kmu_main",
    },
    {
        "name": "국민대 경영대학 공지",
        "url": "https://biz.kookmin.ac.kr/community/notice/",
        "base": "https://biz.kookmin.ac.kr/community/notice/",
        "parser": "kmu_biz",
    },
    {
        "name": "국민대 전체 장학공지",
        "url": "https://www.kookmin.ac.kr/user/kmuNews/notice/7/index.do",
        "base": "https://www.kookmin.ac.kr",
        "parser": "kmu_main",
        "category_hint": "장학금",
    },
    {
        "name": "국민대 경영대학 장학공지",
        "url": "https://biz.kookmin.ac.kr/community/kookmin/scholarship/",
        "base": "https://biz.kookmin.ac.kr/community/kookmin/scholarship/",
        "parser": "kmu_biz",
        "category_hint": "장학금",
    },
    {
        "name": "국민대 SW 취업공지",
        "url": "https://cs.kookmin.ac.kr/news/jobs/",
        "base": "https://cs.kookmin.ac.kr/news/jobs/",
        "parser": "kmu_biz",
        "category_hint": "채용·인턴",
        "include_keywords": CAREER_FOCUS_KEYWORDS,
    },
    # 링커리어: Next.js SPA 라 HTML 파싱 불가 → GraphQL API 직접 호출.
    # activityTypeID 1=대외활동, 3=공모전, 5=채용, 6=교육 / status:OPEN = 모집중
    {
        "name": "링커리어 공모전",
        "kind": "graphql",
        "activity_type_id": 3,
        "category_hint": "공모전·대회",
        "include_keywords": AI_BIGDATA_FOCUS_KEYWORDS,
    },
    {
        "name": "링커리어 대외활동",
        "kind": "graphql",
        "activity_type_id": 1,
        "category_hint": "대외활동·서포터즈",
        "include_keywords": AI_BIGDATA_FOCUS_KEYWORDS + ("서포터즈", "기자단", "멘토", "앰배서더"),
    },
    {
        "name": "링커리어 채용·인턴",
        "kind": "graphql",
        "activity_type_id": 5,
        "category_hint": "채용·인턴",
        "include_keywords": CAREER_FOCUS_KEYWORDS,
    },
    {
        "name": "링커리어 교육·자격증",
        "kind": "graphql",
        "activity_type_id": 6,
        "category_hint": "자격증",
        "include_keywords": CERT_FOCUS_KEYWORDS,
    },
]

# 상세페이지(본문/첨부) 진입 수집 상한 - 요청 과다 방지.
# (실 운영에서는 '신규 공지만' 골라낸 뒤 진입하도록 main.py에서 dedup 권장)
MAX_DETAIL_PER_SOURCE = int(os.getenv("MAX_DETAIL_PER_SOURCE", "8"))
BODY_MAX_CHARS = 1500  # LLM 비용/노이즈 제어용 본문 길이 상한

# 링커리어 GraphQL
LINKAREER_GQL = "https://api.linkareer.com/graphql"
LINKAREER_PAGE_SIZE = int(os.getenv("LINKAREER_PAGE_SIZE", "24"))


@dataclass
class Notice:
    """수집된 공지 1건. raw HTML 은 절대 보관하지 않는다."""
    title: str
    date: str
    body: str
    url: str
    attachment_url: str | None = None
    category: str = ""
    source: str = ""


# ---------------------------------------------------------------------------
# 데모용 가짜 공지 (네트워크/키 없이 시연)
# ---------------------------------------------------------------------------
_DEMO_NOTICES = [
    Notice(
        title="2025 국민 빅데이터 분석 경진대회 참가자 모집",
        date="2025-06-02",
        body=("AI빅데이터융합경영학과 주관 데이터 분석 경진대회입니다. "
              "실제 기업 데이터를 활용한 예측 모델링 및 인사이트 도출 과제. "
              "NLP/생성형 AI 트랙과 정형데이터 모델링 트랙으로 구성."),
        url="https://biz.kookmin.ac.kr/notice/1001",
        attachment_url="https://biz.kookmin.ac.kr/files/bigdata_2025.pdf",
    ),
    Notice(
        title="제5회 교내 AI 해커톤 (1박 2일)",
        date="2025-06-01",
        body=("주말 1박 2일 집중형 해커톤. 생성형 AI 기반 서비스 프로토타입을 "
              "팀 단위로 제작합니다. NLP, LLM 활용 환영."),
        url="https://www.kookmin.ac.kr/notice/2002",
        attachment_url="https://www.kookmin.ac.kr/files/hackathon.pdf",
    ),
    Notice(
        title="소상공인 마케팅 서비스 기획 공모전",
        date="2025-05-30",
        body=("소상공인 대상 마케팅 캠페인 및 서비스 기획안을 제출하는 공모전. "
              "시장조사, 고객 페르소나, UX 리서치 비중이 큼."),
        url="https://biz.kookmin.ac.kr/notice/1003",
        attachment_url="https://biz.kookmin.ac.kr/files/marketing_plan.pdf",
    ),
    Notice(
        title="2025-2 계절학기 전공선택 추가 개설 안내",
        date="2025-05-28",
        body=("졸업 전공학점이 부족한 학생을 위한 전공선택 계절학기 과목 개설 안내. "
              "'머신러닝개론', '비즈니스애널리틱스' 3학점 과목 포함."),
        url="https://www.kookmin.ac.kr/notice/2004",
        attachment_url=None,
    ),
]


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


def _polite_delay(short: bool = False) -> None:
    """요청 간 무작위 지연으로 매크로 탐지 회피."""
    wait = random.uniform(0.6, 1.2) if short else random.uniform(1.5, 3.0)
    print(f"   [scraper] 차단 회피용 지연 {wait:.2f}초 …")
    time.sleep(wait if not _is_demo() else min(wait, 0.4))


def _get(url: str) -> str:
    """GET 후 인코딩 보정한 HTML 텍스트 반환."""
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding  # 한글 깨짐 방지
    return resp.text


def _keyword_blob(notice: Notice) -> str:
    """초점 필터에 사용할 텍스트. 출처/카테고리명 때문에 전부 통과되는 일을 막는다."""
    return f"{notice.title} {notice.body}".casefold()


def _apply_focus_filter(source: dict, notices: list[Notice]) -> list[Notice]:
    """소스별 초점 키워드가 있으면 AI빅데이터융합경영학과 시연에 맞는 후보만 남긴다."""
    if os.getenv("SOURCE_FOCUS_FILTER", "true").lower() != "true":
        return notices
    keywords = source.get("include_keywords")
    if not keywords:
        return notices

    folded_keywords = tuple(str(k).casefold() for k in keywords)
    filtered = [n for n in notices if any(k in _keyword_blob(n) for k in folded_keywords)]
    skipped = len(notices) - len(filtered)
    print(f"   [scraper] [{source['name']}] 시연 초점 필터 {len(filtered)}건 통과 / {skipped}건 제외")
    return filtered


# ---------------------------------------------------------------------------
# 목록 파서 (사이트별)
# ---------------------------------------------------------------------------
def _parse_kmu_main(html: str, base: str) -> list[Notice]:
    soup = BeautifulSoup(html, "html.parser")
    notices: list[Notice] = []
    for li in soup.select("div.board_list li"):
        a = li.find("a", href=True)
        title_el = li.select_one("p.title")
        if not a or not title_el:
            continue
        ctg = li.select_one("span.ctg_name")
        etc = li.select("div.board_etc span")
        date = etc[0].get_text(strip=True) if etc else ""
        notices.append(Notice(
            title=title_el.get_text(strip=True),
            date=date,
            body="",
            url=urljoin(base, a["href"]),
            category=ctg.get_text(strip=True) if ctg else "",
            source="국민대 학사공지",
        ))
    return notices


def _parse_kmu_biz(html: str, base: str) -> list[Notice]:
    soup = BeautifulSoup(html, "html.parser")
    notices: list[Notice] = []
    for li in soup.select("div.list-tbody li.subject"):
        a = li.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        row = li.find_parent("ul")
        date_el = row.select_one("li.date") if row else None
        notices.append(Notice(
            title=title,
            date=date_el.get_text(strip=True) if date_el else "",
            body="",
            url=urljoin(base, a["href"]),
            source="",
        ))
    return notices


_LIST_PARSERS = {"kmu_main": _parse_kmu_main, "kmu_biz": _parse_kmu_biz}


# ---------------------------------------------------------------------------
# 상세 파서 (본문 + 첨부) - 사이트별
# ---------------------------------------------------------------------------
def _extract_pdf_attachment(links, base: str) -> str | None:
    """다운로드 링크 중 .pdf 만 채택 (.hwp 등은 별도 파서 필요 → 현재 skip)."""
    for a in links:
        href = a.get("href", "")
        if href.lower().split("?")[0].endswith(".pdf"):
            return urljoin(base, href)
    return None


def _fill_detail(notice: Notice, parser: str, base: str) -> None:
    """상세페이지를 받아 body + attachment_url 을 채운다(인메모리)."""
    try:
        html = _get(notice.url)
    except Exception as e:
        print(f"   [scraper] 상세 수집 실패({notice.url}): {e}")
        return
    soup = BeautifulSoup(html, "html.parser")

    if parser == "kmu_main":
        cont = soup.select_one("div.view_cont")
        scope = soup.select_one("div.board_view") or soup
    else:  # kmu_biz 계열(경영대/장학/SW 단과대 공지)
        cont = (
            soup.select_one("div.layout-basic-content")
            or soup.select_one("div.board-view")
            or soup.select_one("div.table-wrap.view-wrap")
        )
        scope = cont or soup

    if cont:
        notice.body = cont.get_text(separator=" ", strip=True)[:BODY_MAX_CHARS]
    # 폴백: 본문 컨테이너가 비었으면(분류별 레이아웃 차이) 상위 영역 텍스트 사용
    if not notice.body and scope is not soup:
        notice.body = scope.get_text(separator=" ", strip=True)[:BODY_MAX_CHARS]

    file_links = scope.find_all("a", href=lambda h: h and any(
        k in h.lower() for k in ["download", "filedown", "/file", "wfile", ".pdf", ".hwp"]))
    notice.attachment_url = _extract_pdf_attachment(file_links, base)

    del html, soup  # 원본 즉시 파기


# ---------------------------------------------------------------------------
# 링커리어 GraphQL 수집 (공모전/대외활동)
# ---------------------------------------------------------------------------
def _ms_to_date(ms) -> str:
    """epoch milliseconds → 'YYYY-MM-DD' (마감일). 없으면 ''."""
    if not ms:
        return ""
    import datetime as dt
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _fetch_linkareer(source: dict) -> list[Notice]:
    """링커리어 공식 GraphQL 로 모집중(OPEN) 활동을 최신순 수집."""
    type_id = source["activity_type_id"]
    query = (
        "{ activities("
        "filterBy:{activityTypeID:%d, status:OPEN}, "
        "orderBy:{direction:DESC, field:CREATED_AT}, "
        "pagination:{page:1, pageSize:%d}) "
        "{ nodes { id title organizationName recruitStartAt recruitCloseAt "
        "activityTypeID activityType { name } } totalCount } }"
        % (type_id, LINKAREER_PAGE_SIZE)
    )
    try:
        resp = requests.post(
            LINKAREER_GQL, json={"query": query},
            headers={**BROWSER_HEADERS, "Content-Type": "application/json"}, timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"   [scraper] 링커리어 GraphQL 실패({source['name']}): {e}")
        return []

    data = (payload.get("data") or {}).get("activities") or {}
    nodes = data.get("nodes", [])
    print(f"   [scraper] [{source['name']}] GraphQL 모집중 {data.get('totalCount','?')}건 중 "
          f"최신 {len(nodes)}건 수집")

    notices: list[Notice] = []
    for n in nodes:
        type_name = (n.get("activityType") or {}).get("name") or source["name"]
        close = _ms_to_date(n.get("recruitCloseAt"))
        org = n.get("organizationName") or ""
        body = f"{org} 주최 {type_name}. 모집 마감 {close or '미정'}."
        notices.append(Notice(
            title=n.get("title", "").strip(),
            date=close,                      # 마감일을 date 로 사용
            body=body,
            url=f"https://linkareer.com/activity/{n['id']}",
            attachment_url=None,             # 링커리어 첨부는 포스터 이미지라 평가기준 파싱 대상 아님
            category=source.get("category_hint") or type_name,
            source=source["name"],
        ))
    return _apply_focus_filter(source, notices)


def _fetch_source(source: dict) -> list[Notice]:
    """소스 1개: 목록 수집 → 상세(본문/첨부) 보강."""
    if source.get("kind") == "graphql":
        return _fetch_linkareer(source)

    parser = source["parser"]
    base = source["base"]
    try:
        list_html = _get(source["url"])
    except Exception as e:
        print(f"   [scraper] 목록 수집 실패({source['name']}): {e}")
        return []

    notices = _LIST_PARSERS[parser](list_html, base)
    del list_html
    for n in notices:
        n.source = source["name"]
        if source.get("category_hint"):
            n.category = source["category_hint"]
    notices = _apply_focus_filter(source, notices)
    print(f"   [scraper] [{source['name']}] 목록 {len(notices)}건 추출 → "
          f"상위 {min(len(notices), MAX_DETAIL_PER_SOURCE)}건 상세 진입")

    for n in notices[:MAX_DETAIL_PER_SOURCE]:
        _polite_delay(short=True)
        _fill_detail(n, parser, base)

    print(f"   [scraper] [{source['name']}] RAM에서 HTML 즉시 파기 완료")
    return notices[:MAX_DETAIL_PER_SOURCE]


def _fetch_academic_calendar_notices() -> list[Notice]:
    """국민대 공식 학사일정 중 7일 이내 항목을 정보성 공지로 변환."""
    from modules import academic_calendar

    try:
        events = academic_calendar.upcoming_events(days=7)
    except Exception as e:
        print(f"   [scraper] 국민대 공식 학사일정 수집 실패: {e}")
        return []

    notices: list[Notice] = []
    for event in events:
        start = event.get("start", "")
        end = event.get("end", "") or start
        title = event.get("title", "").strip()
        if not start or not title:
            continue
        period = start if start == end else f"{start} ~ {end}"
        slug = quote(re.sub(r"\s+", "-", title)[:80], safe="")
        notices.append(Notice(
            title=title,
            date=start,
            body=f"국민대학교 공식 학사일정: {period}. {title}",
            url=f"{academic_calendar.SCHEDULE_URL}#{start}_{end}_{slug}",
            attachment_url=None,
            category="학사일정",
            source="국민대 공식 학사일정",
        ))

    print(f"   [scraper] [국민대 공식 학사일정] 7일 이내 {len(notices)}건 수집")
    return notices


def _dedupe_key(notice: Notice) -> tuple[str, str]:
    """여러 게시판에 미러링되는 장학 공지는 제목 기준으로 한 번만 남긴다."""
    title = " ".join((notice.title or "").split()).casefold()
    if (notice.category == "장학금") or ("장학" in (notice.source or "")):
        return ("scholarship", title)
    return ("url", notice.url)


def collect_notices() -> list[Notice]:
    """모든 타깃 소스에서 공지를 수집해 반환."""
    print("[1] 정보수집 에이전트 시작")
    if _is_demo():
        print("   [scraper] DEMO 모드 - 가짜 공지 데이터 사용")
        for _ in TARGET_SOURCES:
            _polite_delay()
        print("   [scraper] RAM에서 HTML 즉시 파기 완료 (demo)")
        print(f"   [scraper] 총 {len(_DEMO_NOTICES)}건 수집\n")
        return list(_DEMO_NOTICES)

    collected: list[Notice] = []
    seen_urls: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    for source in TARGET_SOURCES:
        _polite_delay()
        for n in _fetch_source(source):
            if n.url in seen_urls:
                continue
            dedupe_key = _dedupe_key(n)
            if dedupe_key in seen_keys:
                print(f"   [scraper] 중복 공지 스킵: {n.title[:60]}")
                continue
            seen_urls.add(n.url)
            seen_keys.add(dedupe_key)
            collected.append(n)

    for n in _fetch_academic_calendar_notices():
        if n.url in seen_urls:
            continue
        seen_urls.add(n.url)
        seen_keys.add(_dedupe_key(n))
        collected.append(n)

    print(f"   [scraper] 총 {len(collected)}건 수집\n")
    return collected
