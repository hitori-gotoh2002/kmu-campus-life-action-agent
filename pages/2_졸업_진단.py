"""졸업 진단 페이지 — 성적증명서 업로드 → (비전 초안 → 사람 검증) → 진단 대시보드."""
import os
from dotenv import load_dotenv
load_dotenv()
os.environ["DEMO_MODE"] = "false"

import streamlit as st
from modules import transcript, graduation, store

store.load_keys_into_env()

st.set_page_config(page_title="졸업 진단", page_icon="🎓", layout="centered")
st.title("🎓 졸업 진단")
st.caption("성적증명서(PDF)를 올리면 이수학점을 분석해 졸업까지 부족한 학점을 진단합니다.")

# 성적증명서 이수구분 라벨 (진단 시 졸업요건 구분으로 매핑됨)
CATS = ["전공선택", "전공필수", "기초교양", "핵심교양", "교양선택", "자유교양", "일반선택"]
YEARS = ["2020", "2021", "2022", "2023", "2024", "2025", "2026"]

# 새로고침해도 유지 — 백엔드(store)에서 복원
if "tdata" not in st.session_state and store.kv_get("transcript"):
    st.session_state.tdata = store.kv_get("transcript")
if "diag" not in st.session_state and store.kv_get("graduation"):
    st.session_state.diag = store.kv_get("graduation")

# ── 1) 업로드 + 비전 추출 ──────────────────────────────────────
up = st.file_uploader("성적증명서 PDF", type="pdf")
if up and st.button("📄 성적증명서 분석", use_container_width=True):
    with st.spinner("성적증명서 분석 중 (LLM 비전)…"):
        try:
            st.session_state.tdata = transcript.extract(up.read())
            store.kv_set("transcript", st.session_state.tdata)   # 영속
        except Exception as e:
            st.error(f"추출 실패: {e}")

# ── 2) 사람 검증 (추출값 확인·수정) ────────────────────────────
if "tdata" in st.session_state:
    d = st.session_state.tdata
    sm = d.get("summary", {})
    bc = d.get("by_category", {})
    st.divider()
    st.subheader("① 추출 결과 확인·수정")
    st.caption("⚠️ AI가 읽은 초안입니다. **이수구분별 학점은 부정확할 수 있으니 성적증명서를 보고 꼭 확인·수정**하세요.")
    stu = d.get("student", {})
    yr = str(stu.get("admission_year", "2022"))[:4]
    c = st.columns(3)
    year = c[0].selectbox("입학년도", YEARS, index=YEARS.index(yr) if yr in YEARS else 2)
    total = c[1].number_input("총 취득학점", value=float(sm.get("total_credits") or 0), step=1.0)
    gpa = c[2].number_input("GPA", value=float(sm.get("gpa") or 0), step=0.01, format="%.2f")

    st.write("**이수구분별 취득학점**")
    edited, cc = {}, st.columns(2)
    for i, cat in enumerate(CATS):
        edited[cat] = cc[i % 2].number_input(cat, value=float(bc.get(cat, 0) or 0),
                                              step=1.0, key="cat_" + cat)

    if st.button("✅ 졸업 진단 실행", type="primary", use_container_width=True):
        td = {"student": {"admission_year": year, "major": "AI빅데이터융합경영학과"},
              "summary": {"total_credits": total, "gpa": gpa},
              "by_category": {k: v for k, v in edited.items() if v}}
        st.session_state.diag = graduation.diagnose(td)
        store.kv_set("graduation", st.session_state.diag)        # 영속(새로고침 유지)
        graduation.sync_unmet_to_profile(st.session_state.diag["unmet"])
        st.toast("진단 완료 · 미충족요건을 추천 시스템에 반영했습니다")

# ── 3) 진단 대시보드 ───────────────────────────────────────────
if "diag" in st.session_state:
    d = st.session_state.diag
    st.divider()
    st.subheader("② 졸업 진단 결과")
    if not d["requirements_set"]:
        st.warning("졸업요건 기준학점이 없습니다. 노션 '졸업요건' DB에 입력하세요.")
    m = st.columns(3)
    m[0].metric("총 취득학점", f"{d['total_earned']:g} / {d['total_required']:g}")
    m[1].metric("남은 학점", f"{d['total_gap']:g}")
    m[2].metric("위험도", d["risk_label"])
    st.caption(f"{d['year']} {d['major']} · GPA {d['gpa']} · 일반선택(잔여) {d['free_earned']:g}학점")

    st.write("**구분별 이수 현황**")
    for r in d["rows"]:
        if r["구분"] == "총 이수학점":
            continue
        st.write(f"{r['구분']}  —  {r['이수']:g} / {r['기준']:g}  "
                 + (f"(**{r['부족']:g}학점 부족**)" if r["부족"] > 0 else "✅ 충족"))
        st.progress(min(1.0, r["달성률"] / 100))

    if d["unmet"]:
        st.error("🚩 미충족 요건: " + " · ".join(d["unmet"]))
        st.caption("→ 이 부족 요건이 추천 시스템에 전달되어 계절학기·전공 공지를 우선 추천합니다.")
    else:
        st.success("모든 요건을 충족했습니다! 🎉")
    st.info("🎖️ " + d["cert_note"])

    # ── 수강 로드맵 ─────────────────────────────────────────────
    if d["total_gap"] > 0:
        st.divider()
        st.subheader("③ 학기별 수강 로드맵")
        sem = st.slider("남은 학기 수", 1, 8,
                        value=max(1, -(-int(d["total_gap"]) // 16)))
        if st.button("📚 로드맵 생성", use_container_width=True):
            from modules import roadmap
            with st.spinner("LLM 수강 로드맵 작성 중…"):
                st.session_state.plan = roadmap.generate(d, sem)
        if "plan" in st.session_state:
            plan = st.session_state.plan
            for s in plan.get("semesters", []):
                with st.container(border=True):
                    st.markdown(f"**{s.get('학기','')}**  ·  {s.get('학점합', 0):g}학점")
                    for c in s.get("과목", []):
                        st.write(f"- {c.get('name')} ({c.get('credits',0):g}, {c.get('구분','')}) "
                                 f"— {c.get('사유','')}")
            if plan.get("advice"):
                st.info("💡 " + plan["advice"])
            v = "✅" if plan.get("verified") else "⚠️"
            st.caption(f"{v} 검증: {plan.get('verify_msg','')}  ·  이미 이수한 과목은 제외 · "
                       "실제 개설·선수과목은 수강편람 확인")
