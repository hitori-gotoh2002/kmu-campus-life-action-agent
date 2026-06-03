"""app.py — KMU 캠퍼스 라이프 에이전트 (통합 홈)
두 시스템: ① 커리어 추천   ② 졸업 진단  — 사용자 정보를 공유해 더 정확한 추천.

실행:  streamlit run app.py
"""
import os
from dotenv import load_dotenv
load_dotenv()
os.environ["DEMO_MODE"] = "false"

import streamlit as st
from modules import profile

st.set_page_config(page_title="KMU 캠퍼스 라이프 에이전트", page_icon="🎓", layout="centered")

st.title("🎓 KMU 캠퍼스 라이프 에이전트")
st.caption("포트폴리오·시간표·성적을 한데 모아 — 커리어 추천 + 졸업까지 도와줍니다.")

st.divider()
c = st.columns(2)
with c[0]:
    st.subheader("📋 커리어 추천")
    st.write("학사·공모전·대외활동 공지를 분야별로 분석해, 내 강점·일정에 맞는 활동을 추천하고 "
             "노션 캘린더·칸반에 등록합니다.")
    st.page_link("pages/1_추천_리뷰.py", label="추천 리뷰 열기 →")
with c[1]:
    st.subheader("🎓 졸업 진단")
    st.write("성적증명서(PDF)를 올리면 이수학점을 분석해 졸업까지 부족한 학점을 진단하고, "
             "그 결과를 추천에 반영합니다.")
    st.page_link("pages/2_졸업_진단.py", label="졸업 진단 열기 →")

st.divider()
# 현재 프로필 요약
try:
    ctx = profile.load_profile()
    st.subheader("👤 현재 프로필 (포트폴리오 기반)")
    a = st.columns(2)
    a[0].write(f"**이름** {ctx.get('name')}")
    a[0].write(f"**희망직무** {ctx.get('desired_role')}")
    a[1].write(f"**강점** {', '.join(ctx.get('high_proficiency', [])[:5])}")
    unmet = ctx.get("unmet_graduation_requirement", "")
    if unmet:
        st.info(f"🚩 미충족 졸업요건(졸업 진단 반영): {unmet}")
except Exception as e:
    st.caption(f"프로필 로드 보류: {e}")

st.divider()
st.caption("데이터: 노션(프로필·이력·설정·캘린더·졸업요건) · OpenAI · 텔레그램")
