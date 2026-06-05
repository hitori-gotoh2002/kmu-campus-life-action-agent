"""scheduler.py
매일 정해진 시각에 digest(분야별 추천서)를 자동 실행한다.
- 실행: python scheduler.py   (창을 켜둔 동안 매일 발송)
- 설정(.env): DIGEST_HOUR(기본 8), DIGEST_MINUTE(기본 0), DIGEST_WEEKLY_DAY
- 즉시 1회 테스트: RUN_NOW=true python scheduler.py

[대안] 창을 안 켜두려면 Windows 작업 스케줄러 권장:
  - 프로그램: python  / 인수: main.py  / 시작위치: 이 폴더
  - 환경변수 DELIVERY_MODE=digest 로 매일 트리거
"""
import datetime as dt
import os
import traceback
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
os.environ["DELIVERY_MODE"] = "digest"   # 스케줄러는 항상 digest 모드

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.schedulers.blocking import BlockingScheduler

from modules import telegram_callbacks
from main import digest_run

HOUR = int(os.getenv("DIGEST_HOUR", "8"))
MINUTE = int(os.getenv("DIGEST_MINUTE", "0"))
MISFIRE_GRACE_SECONDS = int(os.getenv("DIGEST_MISFIRE_GRACE_MINUTES", "240")) * 60
LOG_PATH = Path("data/scheduler.log")


def log(message: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[scheduler] 로그 기록 실패: {e}", flush=True)


def job():
    started = dt.datetime.now()
    log("[scheduler] digest 실행 시작")
    try:
        digest_run()
        elapsed = (dt.datetime.now() - started).total_seconds()
        log(f"[scheduler] digest 실행 완료 ({elapsed:.1f}s)")
    except Exception as e:
        log(f"[scheduler] digest 실패: {e}")
        log(traceback.format_exc().rstrip())


def callback_job():
    try:
        result = telegram_callbacks.poll_once(timeout=0)
        if result.get("handled"):
            log(f"[scheduler] 텔레그램 승인 처리 {result['handled']}건")
    except Exception as e:
        log(f"[scheduler] 텔레그램 콜백 처리 실패: {e}")
        log(traceback.format_exc().rstrip())


def scheduler_listener(event):
    if event.code == EVENT_JOB_MISSED:
        log(f"[scheduler] 예약 작업 놓침: {event.job_id} scheduled={event.scheduled_run_time}")


if __name__ == "__main__":
    if os.getenv("RUN_NOW", "false").lower() == "true":
        job()

    sched = BlockingScheduler(timezone="Asia/Seoul")
    sched.add_listener(scheduler_listener, EVENT_JOB_MISSED)
    sched.add_job(
        job,
        "cron",
        hour=HOUR,
        minute=MINUTE,
        id="daily_digest",
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    sched.add_job(callback_job, "interval", seconds=20, id="telegram_callbacks", coalesce=True)
    log(
        f"[scheduler] 매일 {HOUR:02d}:{MINUTE:02d} (KST) 분야별 추천서 발송 대기. "
        f"놓친 실행 허용 {MISFIRE_GRACE_SECONDS // 60}분. Ctrl+C 종료."
    )
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log("[scheduler] 종료")
