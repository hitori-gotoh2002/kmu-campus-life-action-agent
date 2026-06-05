"""설정 페이지 — API 키 입력 + 분야별 자동수신 설정 (노션 아님, 로컬 백엔드 저장).
개인별 비서로 쓰도록 키를 웹에서 입력한다.
"""
import os
from dotenv import load_dotenv
load_dotenv()
os.environ["DEMO_MODE"] = "false"

import streamlit as st
from modules import store, classifier, timetable

store.load_keys_into_env()
st.set_page_config(page_title="설정", page_icon="⚙️", layout="centered")
st.title("⚙️ 설정")
backend_label = "Supabase 원격 DB" if store.active_backend() == "supabase" else "로컬 SQLite"
st.caption(f"API 키와 추천 수신 방식을 여기서 설정합니다. 현재 저장소: {backend_label} · 노션에는 캘린더/포트폴리오만 저장")


def editor_rows(value) -> list[dict]:
    if hasattr(value, "to_dict"):
        return value.to_dict("records")
    return list(value or [])

# ── API 키 ─────────────────────────────────────────────────────
st.subheader("🔑 API 키")
st.caption("각자 본인 키를 넣으면 개인 비서로 동작합니다. Supabase 원격 DB 사용 시 이 설정도 원격 DB에 저장될 수 있습니다.")

KEYS = [
    ("OPENAI_API_KEY", "OpenAI API 키", "sk-..."),
    ("NOTION_API_KEY", "Notion 통합 키", "ntn_..."),
    ("NOTION_CALENDAR_DB_ID", "Notion 캘린더 DB ID", "32자리"),
    ("NOTION_PORTFOLIO_PAGE_ID", "Notion 포트폴리오 페이지 ID", "32자리"),
    ("TELEGRAM_BOT_TOKEN", "Telegram 봇 토큰", "123456:AA..."),
    ("TELEGRAM_CHAT_ID", "Telegram chat_id", "숫자"),
    ("OPENAI_GRADUATION_MODEL", "졸업진단 AI 모델(규정해설·총평·what-if, 선택)", "gpt-4o-mini"),
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
        store.load_keys_into_env(force=True)
        st.success("저장 완료! 이제 추천/졸업 기능을 사용할 수 있습니다.")

st.divider()

# ── 시간표 PDF ────────────────────────────────────────────────
st.subheader("🗓️ 수업 시간표")
st.caption("노션 캘린더에는 회의·모임·회식 같은 실제 일정만 두고, 수업 시간표는 여기서 PDF로 업로드해 추천 로직에 반영합니다.")

saved_tt = timetable.load()
rows = saved_tt.get("rows", [])
if rows:
    st.caption(f"저장된 시간표: {len(rows)}개 수업 · 주간 {timetable.busy_hours(rows):g}시간"
               + (f" · {saved_tt.get('updated_at', '')}" if saved_tt.get("updated_at") else ""))
else:
    st.caption("저장된 시간표가 없습니다. 시간표 PDF를 올리면 일정 검증에 반영됩니다.")

up = st.file_uploader("시간표 PDF", type="pdf")
if up and st.button("📄 시간표 분석", use_container_width=True):
    with st.spinner("시간표 PDF 분석 중…"):
        try:
            parsed = timetable.extract(up.read())
            st.session_state.timetable_draft = parsed
            if parsed.get("rows"):
                st.success(f"{len(parsed['rows'])}개 수업을 추출했습니다. 아래 표를 확인하고 저장하세요.")
            else:
                st.warning("시간표를 자동 추출하지 못했습니다. 표에 직접 입력해 저장할 수 있습니다.")
        except Exception as e:
            st.error(f"시간표 분석 실패: {e}")

draft = st.session_state.get("timetable_draft") or saved_tt
draft_rows = draft.get("rows", [])
editor_seed = draft_rows or [{"name": "", "day": "월", "start": 9.0, "end": 10.5, "place": ""}]
edited_rows = st.data_editor(
    editor_seed,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "name": st.column_config.TextColumn("과목/일정명"),
        "day": st.column_config.SelectboxColumn("요일", options=timetable.DAYS),
        "start": st.column_config.NumberColumn("시작", step=0.5, format="%.2f"),
        "end": st.column_config.NumberColumn("종료", step=0.5, format="%.2f"),
        "place": st.column_config.TextColumn("장소"),
    },
    key="timetable_editor",
)
c = st.columns(2)
if c[0].button("💾 시간표 저장", use_container_width=True):
    clean_rows = timetable.normalize_rows(editor_rows(edited_rows))
    data = {"updated_at": draft.get("updated_at"), "method": draft.get("method", "manual"), "rows": clean_rows}
    timetable.save(data)
    st.session_state.timetable_draft = data
    st.success(f"시간표 저장 완료: {len(clean_rows)}개 수업 · 주간 {timetable.busy_hours(clean_rows):g}시간")
if c[1].button("🗑️ 시간표 삭제", use_container_width=True):
    timetable.clear()
    st.session_state.timetable_draft = {"rows": []}
    st.success("저장된 시간표를 삭제했습니다.")

st.divider()

# ── 자동수신(분야별) ───────────────────────────────────────────
st.subheader("📬 분야별 자동수신")
st.caption("분야별로 추천을 언제 받을지 설정합니다. '텔레그램'이면 자동 발송 시 텔레그램으로 받고 "
           "버튼으로 노션 추가를 결정합니다. '웹'이면 웹에서 검토합니다.")
st.info("자동 실행은 이 주기에 맞는 분야만 새 공지를 분석해 '내 맞춤 추천함'을 업데이트합니다. "
        "'끄기'로 둔 분야는 자동 실행과 수동 새로고침 모두에서 제외됩니다. "
        "텔레그램 수신을 선택한 분야는 같은 주기에 맞춰 그 시점의 1순위 추천만 발송됩니다. "
        "장학금·학사일정 같은 정보성 분야는 여러 건이 함께 발송될 수 있습니다.")
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
st.caption("💡 서버 운영은 GitHub Actions가 매일 08시에 실행하고, 로컬 테스트는 `python scheduler.py`로 돌릴 수 있습니다. "
           "Supabase 원격 DB를 쓰면 PC가 꺼져 있어도 서버가 추천함을 갱신합니다. "
           "`수동`은 자동 업데이트에서 제외되지만 새로고침으로 직접 재분석할 수 있고, `끄기`는 새로고침에서도 제외됩니다.")
