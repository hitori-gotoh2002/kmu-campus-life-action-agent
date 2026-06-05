"""졸업 진단 — 수강신청내역(엑셀) 업로드 → 사용자 검증(HITL) → 결정론 졸업사정.

성적증명서(비전) 대신 ON국민 '수강내역(수강신청확인서)' 엑셀을 학기별로 올린다.
graduation_center.v2 결정론 파이프라인(과목코드 매칭·갭감사·로드맵·리스크)을 사용한다.
성적·GPA는 엑셀에 없으므로 졸업평점 충족 여부 등은 사용자가 직접 선언한다.
"""
import os

from dotenv import load_dotenv

load_dotenv()
os.environ["DEMO_MODE"] = "false"

import streamlit as st

from modules import graduation_link, store
from graduation_center.v2.pipeline import run_audit, run_verify

store.load_keys_into_env()

st.set_page_config(page_title="졸업 진단", page_icon="🎓", layout="wide")
st.title("🎓 졸업 진단")
st.caption("ON국민 **수강내역(수강신청확인서) 엑셀**을 학기별로 올리면, 교과목코드 매칭으로 졸업요건 충족도를 진단합니다. "
           "성적·GPA는 엑셀에 없어 일부 항목은 직접 선택합니다.")

PROGRAMS = graduation_link.load_programs()
PRIMARY = {k: v for k, v in PROGRAMS.items() if v.get("track_type") == "primary"}
CONV = {k: v for k, v in PROGRAMS.items() if v.get("track_type") == "convergence"}
AREAS = ["전공", "기초교양", "핵심교양", "자유교양", "일반선택"]

# 새로고침해도 유지 — 백엔드(store)에서 복원
for key in ("grad_verify", "grad_audit"):
    if key not in st.session_state and store.kv_get(key):
        st.session_state[key] = store.kv_get(key)


def warn_text(vc: dict) -> str:
    bits = []
    if not vc.get("included") and vc.get("exclude_reason"):
        bits.append(f"제외: {vc['exclude_reason']}")
    if vc.get("grade_suspect"):
        bits.append("⚠ 비고 F/NP/W 의심")
    if vc.get("demoted_from_major"):
        bits.append("전공→일반선택 강등(확인)")
    if vc.get("aggregate_only"):
        bits.append("카탈로그 밖(집계만)")
    return " · ".join(bits)


# ── ① 내 정보 ──────────────────────────────────────────────────
st.subheader("① 내 정보")
prog_names = {v["name_ko"]: k for k, v in PRIMARY.items()} or {"AI빅데이터융합경영학과": "ai_bigdata"}
default_prog = "AI빅데이터융합경영학과"
c = st.columns(3)
prog_label = c[0].selectbox("학과(제1전공)", list(prog_names),
                            index=list(prog_names).index(default_prog) if default_prog in prog_names else 0)
program_id = prog_names[prog_label]
YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]
admission_year = c[1].selectbox("입학년도", YEARS, index=YEARS.index(2022))
current_term = c[2].text_input("현재 학기 (예: 2026-1)", value="2026-1")

c2 = st.columns(3)
remaining = c2[0].number_input("남은 학기 수", min_value=0, max_value=12, value=4, step=1)
seasonal = c2[1].checkbox("계절학기 활용 가능", value=False)
prev_gpa = c2[2].checkbox("직전학기 평점 3.75↑ (첫 학기 +3학점)", value=False)
gpa_min = st.radio("졸업 평점 요건 충족 여부 (엑셀엔 성적이 없어 직접 선택)",
                   ["unknown", "yes", "no"], horizontal=True,
                   format_func=lambda x: {"unknown": "모름", "yes": "충족", "no": "미충족"}[x])

conv_ids, conv_tracks = [], {}
if CONV:
    with st.expander("연계·융합전공(다전공/부전공) 추가 — 선택"):
        for cid, cv in CONV.items():
            cc = st.columns([3, 2])
            on = cc[0].checkbox(cv["name_ko"], key=f"conv_{cid}")
            tr = cc[1].selectbox("이수형태", ["다전공", "부전공"], key=f"track_{cid}",
                                 label_visibility="collapsed")
            if on:
                conv_ids.append(cid)
                conv_tracks[cid] = tr

ctx = {
    "program_id": program_id,
    "admission_year": int(admission_year),
    "current_term": current_term.strip() or None,
    "remaining_semesters": int(remaining),
    "seasonal_semester_allowed": bool(seasonal),
    "prev_term_gpa_ge_375": bool(prev_gpa),
    "gpa_min_met": gpa_min,
    "convergence_program_ids": conv_ids,
    "convergence_tracks": conv_tracks,
}

st.divider()

# ── ② 수강내역 업로드 ──────────────────────────────────────────
st.subheader("② 수강내역 엑셀 업로드")
st.caption("ON국민 → 수강신청 → 수강내역(수강신청확인서)에서 **학기별로 내려받은 엑셀**(.xls/.xlsx)을 모두 올립니다.")
files = st.file_uploader("수강내역 엑셀 (여러 학기 파일 선택 가능)", type=["xls", "xlsx"],
                         accept_multiple_files=True)
if files and st.button("📄 검증표 만들기", use_container_width=True):
    try:
        file_tuples = [(f.read(), f.name) for f in files]
        with st.spinner("엑셀 파싱·교과목코드 매칭 중…"):
            st.session_state.grad_verify = run_verify(file_tuples, ctx)
        st.session_state.pop("grad_audit", None)
        store.kv_set("grad_verify", st.session_state.grad_verify)
        store.kv_set("grad_ctx", ctx)
        n = len(st.session_state.grad_verify["verification_table"])
        st.success(f"{len(file_tuples)}개 파일 · {n}개 수강행을 인식했습니다. 아래에서 확인·수정하세요.")
    except Exception as e:
        st.error(f"엑셀 분석 실패: {e}")

# ── ③ 사용자 검증(HITL) ───────────────────────────────────────
if "grad_verify" in st.session_state:
    v = st.session_state.grad_verify
    table = v["verification_table"]
    st.divider()
    st.subheader("③ 수강 내역 확인·수정")
    st.caption("성적이 없는 자료라 F/재수강은 자동 판정하지 않습니다. **포함 여부**와 **이수구분**을 확인·수정하세요. "
               "(재수강 이전 이수분·폐강은 기본 제외)")
    if v.get("possible_retakes"):
        codes = ", ".join(r.get("course_code", "") for r in v["possible_retakes"])
        st.warning(f"재수강 의심(동일 코드 복수 학기): {codes} — 이전 이수분은 기본 제외했습니다. 필요 시 포함 조정하세요.")
    if v.get("unresolved"):
        st.info(f"카탈로그 미매칭 {len(v['unresolved'])}건 — 이수구분 원문으로 집계영역에만 반영됩니다.")

    rows = [{
        "포함": vc.get("included", True),
        "과목명": vc.get("name_ko", ""),
        "학점": vc.get("credits", 0),
        "이수구분": vc.get("requirement_area", "일반선택"),
        "학기": vc.get("term_label", ""),
        "비고": warn_text(vc),
    } for vc in table]
    edited = st.data_editor(
        rows, use_container_width=True, hide_index=True, num_rows="fixed",
        column_config={
            "포함": st.column_config.CheckboxColumn("포함", help="졸업학점 집계에 포함"),
            "과목명": st.column_config.TextColumn("과목명", disabled=True),
            "학점": st.column_config.NumberColumn("학점", disabled=True, format="%g"),
            "이수구분": st.column_config.SelectboxColumn("이수구분", options=AREAS),
            "학기": st.column_config.TextColumn("학기", disabled=True),
            "비고": st.column_config.TextColumn("비고", disabled=True),
        },
        key="grad_editor",
    )

    if st.button("✅ 졸업 진단 실행", type="primary", use_container_width=True):
        for i, r in enumerate(edited):
            if i < len(table):
                table[i]["included"] = bool(r["포함"])
                table[i]["requirement_area"] = r["이수구분"]
        payload = {**v, "verification_table": table}
        try:
            with st.spinner("결정론 졸업사정(갭 계산·로드맵·리스크) 중…"):
                resp = run_audit(payload, client=None, skip_explain=True, run_summary=False)
            st.session_state.grad_audit = resp.model_dump()
            store.kv_set("grad_audit", st.session_state.grad_audit)
            store.kv_set("grad_verify", payload)  # 사용자 수정 반영분 저장
            info = graduation_link.sync_to_recommender(st.session_state.grad_audit)
            st.toast(f"진단 완료 · 이수 {info['completed_count']}과목을 추천 역량에 반영")
        except Exception as e:
            st.error(f"졸업 진단 실패: {e}")

# ── ④ 진단 결과 ────────────────────────────────────────────────
if "grad_audit" in st.session_state:
    d = st.session_state.grad_audit
    a = d.get("audit", {})
    risk = d.get("risk", {})
    st.divider()
    st.subheader("④ 졸업 진단 결과")
    m = st.columns(3)
    m[0].metric("총 이수 / 필요", f"{a.get('total_earned', 0):g} / {a.get('total_required', 0):g}")
    m[1].metric("총 부족 학점", f"{a.get('total_gap', 0):g}")
    m[2].metric("졸업 리스크", f"{risk.get('grade', '')} {risk.get('label', '')}")

    if a.get("missing_required_names"):
        st.error("🚩 미이수 필수지정 과목: " + " · ".join(
            a.get("missing_required_display") or a.get("missing_required_names")))

    st.markdown(d.get("report_markdown", "_리포트를 생성하지 못했습니다._"))
    st.caption("※ 이수 완료 과목은 추천 시스템에 '보유 역량'으로 전달됩니다. "
               "미이수 필수과목은 추천에 반영하지 않습니다(졸업진단에서 해결).")
