"""추천 리뷰 페이지 — 이력 DB의 '추천완료' 항목을 분야별로 보고 노션에 추가/무시."""
import os
from types import SimpleNamespace

from dotenv import load_dotenv
load_dotenv()
os.environ["DEMO_MODE"] = "false"

import streamlit as st
from modules import history, executor, classifier

st.set_page_config(page_title="추천 리뷰", page_icon="📋", layout="centered")


def add_to_notion(row: dict) -> None:
    notice = SimpleNamespace(title=row["title"], url=row["url"], date=row["deadline"],
                             source=row["source"], category=row["category"])
    analysis = SimpleNamespace(suitability_score=int(row["score"]),
                               estimated_hours_needed=int(row["hours"]),
                               matching_reason=row["reason"], domain=row["domain"])
    executor.execute_actions({"notice": notice, "analysis": analysis})
    history.mark(row["url"], "승인")


def drop(page_id: str) -> None:
    st.session_state.pending = [r for r in st.session_state.pending if r["page_id"] != page_id]


if "pending" not in st.session_state:
    st.session_state.pending = history.list_pending()

st.title("📋 커리어 추천 리뷰")
top = st.columns([3, 1])
top[0].caption("이력 DB의 '추천완료' 항목 · [노션에 추가]하면 캘린더+칸반에 기록됩니다")
if top[1].button("🔄 새로고침", use_container_width=True):
    st.session_state.pending = history.list_pending()
    st.rerun()

pending = st.session_state.pending
if not pending:
    st.info("검토할 신규 추천이 없습니다. (파이프라인 실행 후 새로고침하세요)")
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
    st.subheader(f"■ {cat}  ({len(items)})")
    for row in items:
        with st.container(border=True):
            st.markdown(f"**[{int(row['score'])}점] {row['title']}**")
            meta = []
            if row["deadline"]:
                meta.append(f"🗓 마감 {row['deadline']}")
            meta.append(f"⏱ {int(row['hours'])}h")
            if row["domain"]:
                meta.append(f"🧠 {row['domain']}")
            if row["source"]:
                meta.append(f"📍 {row['source']}")
            st.caption("  ·  ".join(meta))
            if row["reason"]:
                st.write(row["reason"])
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
