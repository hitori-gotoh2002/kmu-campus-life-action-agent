"""포트폴리오 분석 실행 → '내 프로필' DB 자동 갱신.
사용: python analyze_portfolio.py
(포트폴리오를 수정했거나, 다른 사람 포트폴리오로 바꿨을 때 다시 실행)
"""
import os
from dotenv import load_dotenv
load_dotenv()
os.environ["DEMO_MODE"] = "false"   # 실제 노션/LLM 사용

from modules import portfolio

print("=" * 56)
print("  Portfolio Analyzer — 포트폴리오 분석 → 프로필 갱신")
print("=" * 56)
data = portfolio.analyze_and_sync(verbose=True)

print("\n--- 추출된 프로필 ---")
print(f"이름      : {data.get('name')}")
print(f"학과/학교 : {data.get('major')} / {data.get('school')}")
print(f"희망직무  : {data.get('desired_role')}")
print(f"강점      : {', '.join(data.get('high_proficiency', []))}")
print(f"약점(추론): {', '.join(data.get('low_proficiency', []))}")
print(f"관심사    : {', '.join(data.get('interests', []))}")
print(f"프로젝트  : {'; '.join(data.get('past_projects', []))}")
