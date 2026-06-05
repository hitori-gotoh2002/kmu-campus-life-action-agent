"""내 맞춤 추천함 — 분야별 추천을 모아 보고 노션 캘린더에 추가/무시."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv()
os.environ["DEMO_MODE"] = "false"

import streamlit as st

from modules import calendar_summary, classifier, executor, history, preferences, store

store.load_keys_into_env()

st.set_page_config(page_title="내 맞춤 추천함", page_icon="📌", layout="wide")


def row_to_web(row: dict) -> dict:
    return {
        "page_id": row["url"],
        "title": row["title"] or "(제목 없음)",
        "url": row["url"] or "",
        "category": row["category"] or "기타",
        "source": row["source"] or "",
        "score": int(row["score"] or 0),
        "hours": int(row["hours"] or 0),
        "deadline": row["deadline"] or "",
        "reason": row["reason"] or "",
        "domain": row["domain"] or "",
        "body": row.get("body") or "",
        "summary": row.get("summary") or "",
        "created_at": row.get("created_at"),
        "status": row.get("status") or "",
    }


def load_recommendations() -> list[dict]:
    enabled = preferences.enabled_categories()
    rows = [row_to_web(r) for r in store.list_recs("추천완료")]
    rows = [r for r in rows if r["category"] in enabled]
    rows.sort(key=lambda r: (r.get("created_at") or 0, r["score"]), reverse=True)
    return rows


def candidate_from_row(row: dict) -> dict:
    notice = SimpleNamespace(
        title=row["title"],
        url=row["url"],
        date=row.get("deadline", ""),
        source=row.get("source", ""),
        category=row["category"],
        body=row.get("body", ""),
    )
    analysis = SimpleNamespace(
        suitability_score=int(row.get("score") or 0),
        estimated_hours_needed=int(row.get("hours") or 0),
        matching_reason=row.get("reason", ""),
        summary=row.get("summary", ""),
        domain=row.get("domain", ""),
    )
    return {"notice": notice, "analysis": analysis, "category": row["category"]}


def add_to_notion(row: dict) -> None:
    executor.execute_actions(candidate_from_row(row))
    history.mark(row["url"], "승인")


def remove_from_view(url: str) -> None:
    st.session_state.recommendations = [
        row for row in st.session_state.recommendations if row["url"] != url
    ]


def run_reanalysis() -> int:
    """최신 웹 소스에서 신규/변경 공지만 추천 분석."""
    from main import refresh_recommendations

    log = io.StringIO()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        candidates = refresh_recommendations(force_reanalysis=False)
    st.session_state.last_refresh_log = log.getvalue()
    st.session_state.recommendations = load_recommendations()
    return len(candidates)


def fmt_time(ts) -> str:
    if not ts:
        return "추천 시각 없음"
    return dt.datetime.fromtimestamp(float(ts)).strftime("%Y.%m.%d %H:%M")


def split_schedule_warning(reason: str) -> tuple[str, str]:
    reason = reason or ""
    if not reason.startswith("⚠시험기간 주의"):
        return "", reason
    if "\n" in reason:
        warning, rest = reason.split("\n", 1)
        return warning.strip(), rest.strip()
    return reason.strip(), ""


def by_category(rows: list[dict]) -> dict[str, list[dict]]:
    grouped = {cat: [] for cat in classifier.CATEGORIES}
    for row in rows:
        grouped.setdefault(row["category"], []).append(row)
    for items in grouped.values():
        items.sort(key=lambda r: (r.get("created_at") or 0, r["score"]), reverse=True)
    return grouped


def status_counts(rows: list[dict]) -> dict:
    counts = {}
    for row in rows:
        status = row.get("status") or "(없음)"
        counts[status] = counts.get(status, 0) + 1
    return counts


def category_status_counts(rows: list[dict]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        cat = row.get("category") or "기타"
        status = row.get("status") or "(없음)"
        counts.setdefault(cat, {})
        counts[cat][status] = counts[cat].get(status, 0) + 1
    return counts


def short_body(row: dict, limit: int = 260) -> str:
    text = " ".join((row.get("body") or "").split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _clean_sentence(text: str) -> str:
    text = " ".join((text or "").split()).strip()
    return text.rstrip(".。")


def _period_from_body(row: dict) -> str:
    body = row.get("body") or ""
    prefix = "국민대학교 공식 학사일정:"
    if prefix not in body:
        return ""
    rest = body.split(prefix, 1)[1].strip()
    if "." not in rest:
        return ""
    return rest.split(".", 1)[0].strip()


def content_summary_text(row: dict) -> str:
    """추천 활동의 '내용'을 자세히 풀어 쓴 산문 설명.
    (추천 이유·적합도 판단은 '왜 추천됐나요'로 분리, 노션과 동일한 단일 소스 사용)"""
    cat = row["category"]
    title = row["title"]

    if cat == "학사일정":
        period = _period_from_body(row)
        parts = [f"국민대 공식 학사일정의 ‘{title}’ 일정입니다."]
        if period:
            parts.append(f"일정 기간은 {period}입니다.")
        elif row.get("deadline"):
            parts.append(f"일정 날짜는 {row['deadline']}입니다.")
        if "시험" in title:
            parts.append("시험 준비가 필요한 기간이라 공모전·대외활동 추천의 가용 시간을 줄여 계산합니다.")
        elif "성적" in title:
            parts.append("성적 입력·확인과 관련된 행정 일정이라 일정 확인용 정보로 보여줍니다.")
        else:
            parts.append("수강·등록·학적 관련 행정 일정이라 놓치지 않도록 정보성으로 보여줍니다.")
        return " ".join(parts)

    # 비학사: 노션 캘린더와 동일한 상세 설명을 단일 소스에서 생성
    return calendar_summary.build_event_details(candidate_from_row(row))["summary"]


def next_steps(row: dict, warning: str) -> list[str]:
    cat = row["category"]
    deadline = row.get("deadline") or ""
    hours = int(row.get("hours") or 0)

    if cat == "학사일정":
        steps = ["내 시간표와 캘린더에서 겹치는 일정이 있는지 확인합니다."]
        if "시험" in row["title"]:
            steps.append("시험 준비 시간을 우선 배정하고, 큰 활동은 시험 이후로 미룹니다.")
        return steps

    if cat == "장학금":
        steps = ["지원 자격, 성적/소득 요건, 제출서류를 먼저 확인합니다."]
    elif cat == "공모전·대회":
        steps = ["주제, 제출물, 팀 구성 필요 여부를 확인하고 포트폴리오 산출물을 정합니다."]
    elif cat == "대외활동·서포터즈":
        steps = ["활동 기간, 정기모임, 필수 참석 조건을 캘린더와 비교합니다."]
    elif cat == "채용·인턴":
        steps = ["지원 자격과 직무 키워드를 보고 이력서/포트폴리오에 연결할 경험을 고릅니다."]
    elif cat == "자격증":
        steps = ["시험일/접수일과 학습 범위를 확인하고 주간 학습 시간을 잡습니다."]
    else:
        steps = ["원문에서 신청 조건과 실제 소요 시간을 확인합니다."]

    if hours:
        steps.append(f"예상 준비 시간 {hours}시간을 2~4개의 작업 블록으로 나눕니다.")
    if deadline:
        steps.append(f"마감일 {deadline} 전날까지 제출/신청을 끝내는 일정으로 잡습니다.")
    if warning:
        steps.append("시험기간에는 바로 시작하지 말고, 시험 이후 착수 또는 최소 작업만 남기는 방식으로 조정합니다.")
    return steps


def render_category_cards(grouped: dict[str, list[dict]], all_rows: list[dict]) -> None:
    st.markdown("#### 분야별 추천함")
    stats_by_cat = category_status_counts(all_rows)
    enabled = preferences.enabled_categories()
    cards = st.columns(4)
    visible_categories = [cat for cat in classifier.CATEGORIES if cat in enabled]
    for idx, cat in enumerate(visible_categories):
        items = grouped.get(cat, [])
        latest = fmt_time(items[0].get("created_at")) if items else "추천 없음"
        stats = stats_by_cat.get(cat, {})
        selected = st.session_state.selected_category == cat
        with cards[idx % 4].container(border=True):
            st.markdown(f"**{cat}**")
            st.caption(f"추천 대기 {len(items)}건")
            st.caption(f"노션 추가됨 {stats.get('승인', 0)}건 · 무시 {stats.get('거절', 0)}건")
            st.caption(f"최근 추천 {latest}")
            label = "열려 있음" if selected else "이 분야 보기"
            if st.button(label, key=f"cat_{cat}", use_container_width=True, disabled=selected):
                st.session_state.selected_category = cat
                st.rerun()


def render_recommendation_card(row: dict, index: int) -> None:
    warning, reason = split_schedule_warning(row.get("reason", ""))

    with st.container(border=True):
        top = st.columns([6, 2])
        label = "정보" if row["category"] == "학사일정" else f"{row['score']}점"
        top[0].markdown(f"**[{label}] {row['title']}**")
        top[1].caption(f"추천됨 {fmt_time(row.get('created_at'))}")

        meta = []
        if row.get("source"):
            meta.append(row["source"])
        if row.get("deadline"):
            meta.append(f"마감 {row['deadline']}")
        if row.get("domain"):
            meta.append(row["domain"])
        if row.get("hours"):
            meta.append(f"예상 {row['hours']}시간")
        if meta:
            st.caption(" · ".join(meta))

        if warning:
            st.warning(warning)

        st.markdown("**내용 요약**")
        st.markdown(calendar_summary.summary_to_markdown(content_summary_text(row)))

        if reason:
            st.markdown("**왜 추천됐나요**")
            st.write(reason)

        st.markdown("**다음에 확인할 것**")
        for step in next_steps(row, warning):
            st.markdown(f"- {step}")

        if row["url"]:
            st.markdown(f"[원문 보기]({row['url']})")

        buttons = st.columns([1.2, 1.2, 5])
        add_label = "노션에 추가"
        if buttons[0].button(add_label, key=f"add_{index}_{row['page_id']}", use_container_width=True):
            with st.spinner("노션 캘린더에 추가 중입니다."):
                add_to_notion(row)
            remove_from_view(row["url"])
            st.toast(f"노션에 추가됨: {row['title'][:24]}…")
            st.rerun()
        if buttons[1].button("무시", key=f"ign_{index}_{row['page_id']}", use_container_width=True):
            history.mark(row["url"], "거절")
            remove_from_view(row["url"])
            st.rerun()


def show_update_rules(rows: list[dict]) -> None:
    counts = status_counts(rows)
    cols = st.columns(4)
    cols[0].metric("추천 대기", counts.get("추천완료", 0))
    cols[1].metric("노션 추가됨", counts.get("승인", 0))
    cols[2].metric("보류/제외", counts.get("수집됨", 0) + counts.get("만료", 0))
    cols[3].metric("무시", counts.get("거절", 0))

    with st.expander("추천이 갱신되는 방식", expanded=False):
        st.markdown(
            "- 이 화면은 현재 상태가 `추천완료`인 항목을 분야별로 모아 보여줍니다.\n"
            "- 설정에서 `끄기`로 둔 분야는 추천함 표시와 수동 새로고침 재분석에서 제외됩니다.\n"
            "- 매일 자동 실행과 `새로고침`은 새 공지나 내용이 바뀐 공지만 분석합니다.\n"
            "- 같은 URL의 제목·본문·마감일·분야가 그대로면 기존 분석 결과를 유지해 시간을 줄입니다.\n"
            "- 시간표·캘린더·프로필·졸업진단 기준이 크게 바뀌어 전체 재분석이 필요하면 코드에서 `force_reanalysis=True`로 실행합니다.\n"
            "- 시험기간/시험 직전에는 분야별 최상위 1건이 시간 초과여도 `시험기간 주의`로 남습니다.\n"
            "- 국민대 공식 학사일정은 7일 이내 일정이면 순위 없이 정보성 추천으로 보여줍니다."
        )


if "recommendations" not in st.session_state:
    st.session_state.recommendations = load_recommendations()
if "selected_category" not in st.session_state:
    rows_by_recent = st.session_state.recommendations
    st.session_state.selected_category = (
        rows_by_recent[0]["category"] if rows_by_recent else classifier.CATEGORIES[0]
    )

st.title("내 맞춤 추천함")
top = st.columns([4, 1])
top[0].caption("분야를 고르면 지금까지 추천된 항목을 모아 보고, 필요한 것만 노션 캘린더에 추가할 수 있습니다.")
if top[1].button("새로고침", use_container_width=True):
    with st.spinner("최신 정보를 확인하고 신규/변경 공지만 분석 중입니다."):
        try:
            count = run_reanalysis()
            st.toast(f"증분 분석 완료: 신규/변경 후보 {count}건")
        except Exception as e:
            st.error(f"추천 재분석 실패: {e}")
            st.stop()
    st.rerun()

if st.session_state.get("last_refresh_log"):
    with st.expander("마지막 새로고침 분석 로그", expanded=False):
        st.code(st.session_state.last_refresh_log[-6000:])

all_rows = store.list_recs()
show_update_rules(all_rows)

rows = st.session_state.recommendations
if not rows:
    st.info("아직 추천 대기 항목이 없습니다. 새로고침을 누르면 최신 공지 중 신규/변경 항목을 분석합니다.")
    st.stop()

enabled_categories = preferences.enabled_categories()
if st.session_state.selected_category not in enabled_categories:
    st.session_state.selected_category = rows[0]["category"]

grouped = by_category(rows)
render_category_cards(grouped, all_rows)

selected = st.session_state.selected_category
items = grouped.get(selected, [])
st.divider()
st.subheader(selected)

if not items:
    st.info("이 분야에는 현재 추천된 항목이 없습니다.")
else:
    st.caption(f"{selected} 분야에 누적된 추천 {len(items)}건")
    for idx, row in enumerate(items):
        render_recommendation_card(row, idx)
