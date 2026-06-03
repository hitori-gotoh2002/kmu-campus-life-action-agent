"""설정 페이지 — API 키 입력 + 분야별 자동수신 설정 (노션 아님, 로컬 백엔드 저장).
개인별 비서로 쓰도록 키를 웹에서 입력한다.
"""
import os
from dotenv import load_dotenv
load_dotenv()
os.environ["DEMO_MODE"] = "false"

import streamlit as st
from modules import store, classifier

store.load_keys_into_env()
st.set_page_config(page_title="설정", page_icon="⚙️", layout="centered")
st.title("⚙️ 설정")
st.caption("API 키와 추천 수신 방식을 여기서 설정합니다. (로컬에만 저장 · 노션에는 캘린더/포트폴리오만)")

# ── API 키 ─────────────────────────────────────────────────────
st.subheader("🔑 API 키")
st.caption("각자 본인 키를 넣으면 개인 비서로 동작합니다. 키는 이 PC(로컬 DB)에만 저장됩니다.")

KEYS = [
    ("OPENAI_API_KEY", "OpenAI API 키", "sk-..."),
    ("NOTION_API_KEY", "Notion 통합 키", "ntn_..."),
    ("NOTION_CALENDAR_DB_ID", "Notion 캘린더 DB ID", "32자리"),
    ("NOTION_PORTFOLIO_PAGE_ID", "Notion 포트폴리오 페이지 ID", "32자리"),
    ("TELEGRAM_BOT_TOKEN", "Telegram 봇 토큰", "123456:AA..."),
    ("TELEGRAM_CHAT_ID", "Telegram chat_id", "숫자"),
]
with st.form("keys"):
    vals = {}
    for k, label, ph in KEYS:
        cur = store.get_setting(k, os.getenv(k, ""))
        is_secret = "KEY" in k or "TOKEN" in k
        vals[k] = st.text_input(label, value=cur, placeholder=ph,
                                type="password" if is_secret else "default")
    if st.form_submit_button("💾 키 저장", use_container_width=True):
        for k, v in vals.items():
            if v.strip():
                store.set_setting(k, v.strip())
        store.load_keys_into_env()
        st.success("저장 완료! 이제 추천/졸업 기능을 사용할 수 있습니다.")

st.divider()

# ── 자동수신(분야별) ───────────────────────────────────────────
st.subheader("📬 분야별 자동수신")
st.caption("분야별로 추천을 언제 받을지 설정합니다. '텔레그램'이면 자동 발송 시 텔레그램으로 받고 "
           "버튼으로 노션 추가를 결정합니다. '웹'이면 웹에서 검토합니다.")
prefs = store.get_prefs()
CYCLES = ["매일", "매주", "수동", "끄기"]
CHANNELS = ["웹", "텔레그램"]
with st.form("prefs"):
    new = {}
    for cat in classifier.CATEGORIES:
        p = prefs.get(cat, {"주기": "수동", "채널": "웹"})
        c = st.columns([2, 1, 1])
        c[0].markdown(f"**{cat}**")
        cyc = c[1].selectbox("주기", CYCLES, index=CYCLES.index(p.get("주기", "수동")),
                             key=f"cyc_{cat}", label_visibility="collapsed")
        ch = c[2].selectbox("채널", CHANNELS, index=CHANNELS.index(p.get("채널", "웹")),
                            key=f"ch_{cat}", label_visibility="collapsed")
        new[cat] = (cyc, ch)
    if st.form_submit_button("💾 수신 설정 저장", use_container_width=True):
        for cat, (cyc, ch) in new.items():
            store.set_pref(cat, cyc, ch)
        st.success("수신 설정 저장 완료")

st.divider()
st.caption("💡 매일/매주 자동 발송은 `python scheduler.py` 실행 시 동작합니다. "
           "수동/웹은 '추천 리뷰' 페이지에서 검토하세요.")
