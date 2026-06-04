"""추천 리뷰 페이지 — 이력 DB의 '추천완료' 항목을 분야별로 보고 노션에 추가/무시."""
import datetime as dt
import os
from types import SimpleNamespace

from dotenv import load_dotenv
load_dotenv()
os.environ["DEMO_MODE"] = "false"

import streamlit as st
from modules import history, executor, classifier, store, calendar_summary

store.load_keys_into_env()

st.set_page_config(page_title="추천 리뷰", page_icon="📋", layout="centered")


def add_to_notion(row: dict) -> None:
    notice = SimpleNamespace(title=row["title"], url=row["url"], date=row["deadline"],
                             source=row["source"], category=row["category"], body=row.get("body", ""))
    analysis = SimpleNamespace(suitability_score=int(row["score"]),
                               estimated_hours_needed=int(row["hours"]),
                               matching_reason=row["reason"], domain=row["domain"])
    executor.execute_actions({"notice": notice, "analysis": analysis})
    history.mark(row["url"], "승인")


def drop(page_id: str) -> None:
    st.session_state.pending = [r for r in st.session_state.pending if r["page_id"] != page_id]


def status_counts(rows: list[dict]) -> dict:
    counts = {}
    for row in rows:
        status = row.get("status") or "(없음)"
        counts[status] = counts.get(status, 0) + 1
    return counts


def fmt_time(ts) -> str:
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M")


def show_update_rules(rows: list[dict]) -> None:
    counts = status_counts(rows)
    cols = st.columns(4)
    cols[0].metric("검토 대기", counts.get("추천완료", 0))
    cols[1].metric("노션 추가됨", counts.get("승인", 0))
    cols[2].metric("제외/보류", counts.get("수집됨", 0) + counts.get("만료", 0))
    cols[3].metric("무시", counts.get("거절", 0))

    with st.expander("추천이 업데이트되는 기준", expanded=True):
        st.markdown(
            "- 이 화면은 로컬 이력 DB에서 **상태가 `추천완료`인 항목만** 보여줍니다.\n"
            "- `노션에 추가`를 누르면 상태가 `승인`으로 바뀌면서 이 화면에서 사라집니다.\n"
            "- `무시`를 누르면 상태가 `거절`로 바뀌면서 이 화면에서 사라집니다.\n"
            "- 새 추천은 `python main.py` 또는 `python scheduler.py`가 실행될 때 갱신됩니다. "
            "Streamlit의 `새로고침` 버튼은 새 공지를 수집하지 않고 DB만 다시 읽습니다.\n"
            "- 파이프라인은 마감이 지나지 않았고, URL이 이전에 처리된 적 없고, LLM 관련성/검증과 일정 여유 검사를 통과한 항목만 `추천완료`로 둡니다.\n"
            "- 한 번 `수집됨`, `승인`, `거절`로 기록된 URL은 중복 추천 방지를 위해 다음 실행에서 다시 분석하지 않습니다."
        )

    if rows:
        st.subheader("최근 처리 이력")
        recent = sorted(rows, key=lambda r: r.get("created_at") or 0, reverse=True)[:8]
        for row in recent:
            with st.container(border=True):
                score = int(row["score"] or 0)
                title = row["title"] or "(제목 없음)"
                st.markdown(f"**{row['status']} · {score}점 · {title}**")
                meta = []
                if row.get("category"):
                    meta.append(row["category"])
                if row.get("deadline"):
                    meta.append(f"마감 {row['deadline']}")
                if row.get("created_at"):
                    meta.append(f"처리 {fmt_time(row['created_at'])}")
                st.caption(" · ".join(meta))
                if row.get("reason"):
                    st.write(row["reason"])


if "pending" not in st.session_state:
    st.session_state.pending = history.list_pending()

st.title("📋 커리어 추천 리뷰")
top = st.columns([3, 1])
top[0].caption("이력 DB의 '추천완료' 항목 · [노션에 추가]하면 캘린더에 기록됩니다")
if top[1].button("🔄 새로고침", use_container_width=True):
    st.session_state.pending = history.list_pending()
    st.rerun()

pending = st.session_state.pending
if not pending:
    all_rows = store.list_recs()
    st.info("검토할 신규 추천이 없습니다. 현재 `추천완료` 상태인 항목이 0건입니다.")
    show_update_rules(all_rows)
    st.stop()

counts = {}
for r in pending:
    counts[r["category"]] = counts.get(r["category"], 0) + 1
st.write("**검토 대기:** " + " · ".join(f"{c} {n}건" for c, n in counts.items()))
st.divider()

by_cat = {}
for r in pending:
    by_cat.setdefault(r["category"], []).append(r)

for cat in classifier.CATEGORIES:
    items = by_cat.get(cat, [])
    if not items:
        continue
    st.subheader(f"■ {cat}  (1순위)")
    for row in items:
        cand = {
            "notice": SimpleNamespace(title=row["title"], url=row["url"], date=row["deadline"],
                                      source=row["source"], category=row["category"], body=row.get("body", "")),
            "analysis": SimpleNamespace(suitability_score=int(row["score"]),
                                        estimated_hours_needed=int(row["hours"]),
                                        matching_reason=row["reason"], domain=row["domain"]),
            "category": row["category"],
        }
        d = calendar_summary.build_event_details(cand)
        with st.container(border=True):
            st.markdown(f"**[{int(row['score'])}점] {row['title']}**")
            if d["meta"]:
                st.caption("  ·  ".join(d["meta"]))
            if d["summary"]:
                st.write(d["summary"])
            if d["reason"]:
                st.markdown(f"**🧭 추천 이유**  {d['reason']}")
            if d["checklist"]:
                st.markdown("**✅ 준비 체크리스트**")
                for it in d["checklist"]:
                    st.markdown(f"- {it}")
            if row["url"]:
                st.markdown(f"[🔗 원문 보기]({row['url']})")
            b = st.columns([1, 1, 4])
            if b[0].button("✅ 노션에 추가", key="add_" + row["page_id"], use_container_width=True):
                with st.spinner("노션에 등록 중…"):
                    add_to_notion(row)
                drop(row["page_id"])
                st.toast(f"노션에 추가됨: {row['title'][:20]}…")
                st.rerun()
            if b[1].button("🗑️ 무시", key="ign_" + row["page_id"], use_container_width=True):
                history.mark(row["url"], "거절")
                drop(row["page_id"])
                st.rerun()
