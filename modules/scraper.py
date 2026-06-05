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

import json
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
BODY_MAX_CHARS = 3000  # LLM 비용/노이즈 제어용 본문 길이 상한

# 링커리어 GraphQL
LINKAREER_GQL = "https://api.linkareer.com/graphql"
LINKAREER_PAGE_SIZE = int(os.getenv("LINKAREER_PAGE_SIZE", "24"))
LINKAREER_DETAIL_SECTIONS = (
    "공모명", "공모내용", "공모자격", "공모기간", "응모방법", "접수방법", "지원방법",
    "제출서류", "심사방법", "심사기준", "시상내역", "활동내용", "활동기간",
    "모집대상", "모집기간", "지원자격", "혜택", "결과발표", "문의",
)


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


def _career_body_is_sparse(body: str) -> bool:
    text = re.sub(r"\s+", " ", body or "").strip()
    if len(text) < 260:
        return True
    sparse_markers = ("자세한 사항은 홈페이지 참고", "자세한 내용은 홈페이지 참고")
    return any(marker in text for marker in sparse_markers)


def _external_urls_from_text(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s)>\]]+", text or "")
    out: list[str] = []
    for url in urls:
        clean = url.rstrip(".,")
        low = clean.lower().split("?")[0]
        if "kookmin.ac.kr" in low:
            continue
        if low.endswith((".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".zip", ".png", ".jpg", ".jpeg")):
            continue
        out.append(clean)
    return list(dict.fromkeys(out))


def _fetch_external_page_brief(url: str) -> str:
    """채용 공고 외부 링크가 있을 때 HTML의 공개 요약 신호만 짧게 보강한다."""
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"   [scraper] 외부 채용 상세 보강 실패({url}): {e}")
        return ""
    content_type = resp.headers.get("Content-Type", "").lower()
    if content_type and "html" not in content_type:
        return ""
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    parts: list[str] = []
    title = soup.find("title")
    if title:
        parts.append(title.get_text(" ", strip=True))
    for selector in (
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"},
    ):
        meta = soup.find("meta", attrs=selector)
        if meta and meta.get("content"):
            parts.append(meta["content"])
    for tag in soup.select("h1, h2")[:4]:
        text = tag.get_text(" ", strip=True)
        if text:
            parts.append(text)
    brief = " / ".join(dict.fromkeys(_clean_text(p) for p in parts if _clean_text(p)))
    del resp, soup
    return brief[:900]


def _enrich_sparse_career_body(notice: Notice) -> None:
    if notice.category != "채용·인턴" or not _career_body_is_sparse(notice.body):
        return
    urls = _external_urls_from_text(notice.body)
    if not urls:
        return
    brief = _fetch_external_page_brief(urls[0])
    if brief:
        notice.body = (notice.body.rstrip() + f"\n외부 상세 요약: {brief}")[:BODY_MAX_CHARS]


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
    _enrich_sparse_career_body(notice)

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


def _names(items) -> str:
    if not items:
        return ""
    names = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = str(item)
        if name:
            names.append(str(name).strip())
    return ", ".join(dict.fromkeys(n for n in names if n))


def _append_line(lines: list[str], label: str, value) -> None:
    text = _clean_text(value)
    if text:
        lines.append(f"{label}: {text}")


def _clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
    return _clean_text(text)


def _linkareer_detail_lines(text: str) -> list[str]:
    text = _clean_text(text).replace("문 의", "문의")
    if not text:
        return []
    for section in LINKAREER_DETAIL_SECTIONS:
        text = re.sub(rf"^({re.escape(section)})\s+", r"\1: ", text)
        text = re.sub(rf"\s+({re.escape(section)})\s+", r"\n\1: ", text)
    lines = [line.strip(" -") for line in text.split("\n") if line.strip(" -")]
    return lines


def _activity_field(activity: dict, node: dict, key: str):
    value = activity.get(key)
    if value in (None, "", [], {}):
        return node.get(key)
    return value


def _linkareer_reward_text(activity: dict, node: dict) -> str:
    parts: list[str] = []
    benefits = _names(_activity_field(activity, node, "benefits"))
    if benefits:
        parts.append(benefits)
    additional = _activity_field(activity, node, "additionalBenefit")
    if additional:
        parts.append(str(additional))
    reward = _activity_field(activity, node, "tenThousandUnitOfReward")
    if reward:
        parts.append(f"시상 규모 {reward}만원")
    for item in _activity_field(activity, node, "integers") or []:
        typ = item.get("type") or {}
        name = typ.get("name")
        integer = item.get("integer")
        unit = typ.get("unit") or ""
        if name and integer:
            parts.append(f"{name} {integer}{unit}")
    return ", ".join(dict.fromkeys(_clean_text(p) for p in parts if _clean_text(p)))


def _linkareer_period(activity: dict, node: dict, start_key: str, end_key: str) -> str:
    start = _ms_to_date(_activity_field(activity, node, start_key))
    end = _ms_to_date(_activity_field(activity, node, end_key))
    if start and end:
        return f"{start} ~ {end}"
    return start or end


def _fetch_linkareer_detail(activity_id: str) -> dict:
    """링커리어 상세 페이지의 Next.js 데이터에서 본문성 정보를 추출한다."""
    url = f"https://linkareer.com/activity/{activity_id}"
    try:
        html = _get(url)
    except Exception as e:
        print(f"   [scraper] 링커리어 상세 수집 실패({url}): {e}")
        return {}

    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "description"})
    description = meta.get("content", "") if meta else ""
    script = soup.find("script", id="__NEXT_DATA__")
    activity: dict = {}
    seo_activity: dict = {}
    detail_texts: list[str] = []
    if script and script.string:
        try:
            data = json.loads(script.string)
            page_data = (((data.get("props") or {}).get("pageProps") or {}).get("data") or {})
            activity_data = page_data.get("activityData") or {}
            activity = activity_data.get("activity") or {}
            seo_activity = activity_data.get("activitySeo") or {}
            for obj in (activity, seo_activity):
                detail = (obj.get("detailText") or {}).get("text")
                if detail:
                    detail_texts.append(detail)
                for text_obj in obj.get("texts") or []:
                    text = text_obj.get("text")
                    if text:
                        detail_texts.append(text)
        except (TypeError, ValueError):
            pass
    cleaned_details = [_html_to_text(text) for text in detail_texts]
    detail_text = "\n".join(dict.fromkeys(text for text in cleaned_details if text))
    del html, soup
    return {
        "description": _clean_text(description),
        "detail_text": detail_text[:BODY_MAX_CHARS],
        "activity": activity or seo_activity,
    }


def _build_linkareer_body(node: dict, source: dict, detail: dict | None = None) -> str:
    detail = detail or {}
    activity = detail.get("activity") or {}
    type_name = ((activity.get("activityType") or node.get("activityType") or {}).get("name")
                 or source["name"])
    org = _activity_field(activity, node, "organizationName") or ""
    lines: list[str] = []
    intro = f"{org}에서 모집하는 {type_name}입니다." if org else f"{type_name} 모집 공고입니다."
    lines.append(intro)
    _append_line(lines, "요약", detail.get("description"))
    lines.extend(_linkareer_detail_lines(detail.get("detail_text"))[:14])
    _append_line(lines, "모집 대상", _names(_activity_field(activity, node, "targets")))
    _append_line(lines, "모집 기간", _linkareer_period(activity, node, "recruitStartAt", "recruitCloseAt"))
    _append_line(lines, "활동 기간", _linkareer_period(activity, node, "activityStartAt", "activityEndAt"))
    _append_line(lines, "분야", _names(_activity_field(activity, node, "categories")))
    _append_line(lines, "관심 키워드", _names(_activity_field(activity, node, "interests")))
    apply_types = _names(_activity_field(activity, node, "applyTypes"))
    apply_detail = _activity_field(activity, node, "applyDetail")
    _append_line(lines, "신청 방법", " / ".join(p for p in [apply_types, _clean_text(apply_detail)] if p))
    _append_line(lines, "혜택", _linkareer_reward_text(activity, node))
    _append_line(lines, "지역", _names(_activity_field(activity, node, "regions")))
    _append_line(lines, "참가 비용", _activity_field(activity, node, "cost"))
    scale = _activity_field(activity, node, "recruitScale")
    if str(scale or "").strip() not in ("", "0", "0.0"):
        _append_line(lines, "모집 인원", scale)
    _append_line(lines, "외부 링크", _activity_field(activity, node, "homepageURL"))
    manager_phone = _clean_text(_activity_field(activity, node, "managerPhoneNumber"))
    manager_email = _clean_text(_activity_field(activity, node, "managerEmail"))
    manager_name = _clean_text(_activity_field(activity, node, "managerName"))
    if manager_phone or manager_email:
        manager_bits = [manager_name, manager_phone, manager_email]
        _append_line(lines, "문의", " / ".join(_clean_text(x) for x in manager_bits if _clean_text(x)))
    return "\n".join(lines)[:BODY_MAX_CHARS]


def _fetch_linkareer(source: dict) -> list[Notice]:
    """링커리어 공식 GraphQL 로 모집중(OPEN) 활동을 최신순 수집."""
    type_id = source["activity_type_id"]
    query = (
        "{ activities("
        "filterBy:{activityTypeID:%d, status:OPEN}, "
        "orderBy:{direction:DESC, field:CREATED_AT}, "
        "pagination:{page:1, pageSize:%d}) "
        "{ nodes { id title organizationName recruitStartAt recruitCloseAt "
        "activityStartAt activityEndAt facetimePeriod recruitType homepageURL "
        "activityTypeID activityType { name } benefits { name } targets { name } "
        "regions { name } categories { name } interests { name } applyTypes { name } "
        "applyDetail additionalBenefit tenThousandUnitOfReward cost recruitScale "
        "integers { integer type { name unit } } } totalCount } }"
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
    node_by_url: dict[str, dict] = {}
    for n in nodes:
        type_name = (n.get("activityType") or {}).get("name") or source["name"]
        close = _ms_to_date(n.get("recruitCloseAt"))
        activity_id = str(n["id"])
        url = f"https://linkareer.com/activity/{activity_id}"
        body = _build_linkareer_body(n, source)
        notices.append(Notice(
            title=n.get("title", "").strip(),
            date=close,                      # 마감일을 date 로 사용
            body=body,
            url=url,
            attachment_url=None,             # 링커리어 첨부는 포스터 이미지라 평가기준 파싱 대상 아님
            category=source.get("category_hint") or type_name,
            source=source["name"],
        ))
        node_by_url[url] = n

    notices = _apply_focus_filter(source, notices)
    detail_targets = notices[:MAX_DETAIL_PER_SOURCE]
    if detail_targets:
        print(f"   [scraper] [{source['name']}] 상세 본문 보강 {len(detail_targets)}건")
    for notice in detail_targets:
        node = node_by_url.get(notice.url) or {}
        activity_id = notice.url.rstrip("/").split("/")[-1]
        _polite_delay(short=True)
        detail = _fetch_linkareer_detail(activity_id)
        if detail:
            notice.body = _build_linkareer_body(node, source, detail)
    return notices


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


def _source_allowed(source: dict, allowed_categories: set[str] | None) -> bool:
    """분야가 정해진 실행에서는 명백히 무관한 소스를 수집 전부터 건너뛴다."""
    if not allowed_categories:
        return True

    hint = source.get("category_hint")
    if hint:
        return hint in allowed_categories

    # 범용 게시판은 여러 분야가 섞이므로 필요한 분야와 겹칠 때만 유지한다.
    source_groups = {
        "국민대 학사공지": {"학사일정", "장학금", "기타"},
        "국민대 경영대학 공지": {
            "공모전·대회", "대외활동·서포터즈", "학사일정",
            "채용·인턴", "자격증", "기타",
        },
    }
    groups = source_groups.get(source.get("name"))
    return bool(groups is None or groups & allowed_categories)


def collect_notices(allowed_categories: set[str] | None = None) -> list[Notice]:
    """타깃 소스에서 공지를 수집해 반환.

    allowed_categories가 있으면 명백히 관련 없는 소스는 요청하지 않아 자동 갱신 시간을 줄인다.
    """
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
        if not _source_allowed(source, allowed_categories):
            print(f"   [scraper] [{source['name']}] 업데이트 대상 분야가 아니라 수집 생략")
            continue
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

    if not allowed_categories or "학사일정" in allowed_categories:
        for n in _fetch_academic_calendar_notices():
            if n.url in seen_urls:
                continue
            seen_urls.add(n.url)
            seen_keys.add(_dedupe_key(n))
            collected.append(n)

    print(f"   [scraper] 총 {len(collected)}건 수집\n")
    return collected
