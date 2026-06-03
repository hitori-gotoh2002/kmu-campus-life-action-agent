"""scheduler.py
매일 정해진 시각에 digest(분야별 추천서)를 자동 실행한다.
- 실행: python scheduler.py   (창을 켜둔 동안 매일 발송)
- 설정(.env): DIGEST_HOUR(기본 8), DIGEST_MINUTE(기본 0), DIGEST_WEEKLY_DAY
- 즉시 1회 테스트: RUN_NOW=true python scheduler.py

[대안] 창을 안 켜두려면 Windows 작업 스케줄러 권장:
  - 프로그램: python  / 인수: main.py  / 시작위치: 이 폴더
  - 환경변수 DELIVERY_MODE=digest 로 매일 트리거
"""
import os

from dotenv import load_dotenv
load_dotenv()
os.environ["DELIVERY_MODE"] = "digest"   # 스케줄러는 항상 digest 모드

from apscheduler.schedulers.blocking import BlockingScheduler

from main import digest_run

HOUR = int(os.getenv("DIGEST_HOUR", "8"))
MINUTE = int(os.getenv("DIGEST_MINUTE", "0"))


def job():
    print("[scheduler] digest 실행 시작")
    try:
        digest_run()
    except Exception as e:
        print(f"[scheduler] digest 실패: {e}")


if __name__ == "__main__":
    if os.getenv("RUN_NOW", "false").lower() == "true":
        job()

    sched = BlockingScheduler(timezone="Asia/Seoul")
    sched.add_job(job, "cron", hour=HOUR, minute=MINUTE)
    print(f"[scheduler] 매일 {HOUR:02d}:{MINUTE:02d} (KST) 분야별 추천서 발송 대기. Ctrl+C 종료.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n[scheduler] 종료")
