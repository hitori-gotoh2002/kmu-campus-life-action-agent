"""modules/store.py
로컬 백엔드 저장소 (SQLite). 노션에서 옮겨온 데이터를 여기에 보관한다.
보관 대상: 프로필 · 추천이력 · 설정(수신주기) · API키 · 졸업진단/이수내역(kv).
노션에는 캘린더 + 포트폴리오만 남긴다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / "data" / "agent.db"


@contextmanager
def _conn():
    _DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(_DB_PATH)
    try:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS profile(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS preferences(category TEXT PRIMARY KEY, cycle TEXT, channel TEXT);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS recommendations(
                url TEXT PRIMARY KEY, title TEXT, category TEXT, source TEXT,
                score REAL, hours REAL, deadline TEXT, reason TEXT, domain TEXT,
                status TEXT, created_at REAL);
        """)
        cols = {row[1] for row in c.execute("PRAGMA table_info(recommendations)")}
        if "body" not in cols:
            c.execute("ALTER TABLE recommendations ADD COLUMN body TEXT")
        if "summary" not in cols:
            c.execute("ALTER TABLE recommendations ADD COLUMN summary TEXT")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# ── settings (API 키, 자동수신 플래그 등) ──────────────────────────
def get_setting(key: str, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def all_settings() -> dict:
    with _conn() as c:
        return {k: v for k, v in c.execute("SELECT key,value FROM settings")}


# 웹에서 저장한 키를 환경변수로 올림 → 기존 모듈(os.getenv)이 그대로 동작
KEY_NAMES = ["OPENAI_API_KEY", "OPENAI_MODEL", "NOTION_API_KEY",
             "NOTION_CALENDAR_DB_ID", "NOTION_PORTFOLIO_PAGE_ID",
             "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def load_keys_into_env():
    for k in KEY_NAMES:
        v = get_setting(k)
        if v:
            os.environ[k] = v


# ── profile (포트폴리오 분석 결과) ────────────────────────────────
def get_profile() -> dict:
    with _conn() as c:
        return {k: v for k, v in c.execute("SELECT key,value FROM profile")}


def set_profile(d: dict):
    with _conn() as c:
        for k, v in d.items():
            c.execute("INSERT INTO profile(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


# ── recommendations (추천 이력) ───────────────────────────────────
def rec_exists(url: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM recommendations WHERE url=?", (url,)).fetchone() is not None


def add_rec(rec: dict):
    with _conn() as c:
        c.execute("""INSERT INTO recommendations
            (url,title,category,source,score,hours,deadline,reason,domain,status,body,summary,created_at)
            VALUES(:url,:title,:category,:source,:score,:hours,:deadline,:reason,:domain,:status,:body,:summary,:created_at)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                category=excluded.category,
                source=excluded.source,
                score=excluded.score,
                hours=excluded.hours,
                deadline=excluded.deadline,
                reason=excluded.reason,
                domain=excluded.domain,
                status=excluded.status,
                body=excluded.body,
                summary=excluded.summary,
                created_at=excluded.created_at""",
            {**{k: rec.get(k) for k in
                ("url", "title", "category", "source", "score", "hours", "deadline", "reason", "domain", "status", "body", "summary")},
             "created_at": time.time()})


def list_recs(status: str | None = None) -> list:
    q = "SELECT url,title,category,source,score,hours,deadline,reason,domain,status,body,summary,created_at FROM recommendations"
    args = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY score DESC"
    with _conn() as c:
        cols = ["url", "title", "category", "source", "score", "hours", "deadline", "reason", "domain", "status", "body", "summary", "created_at"]
        return [dict(zip(cols, row)) for row in c.execute(q, args)]


def set_rec_status(url: str, status: str):
    with _conn() as c:
        c.execute("UPDATE recommendations SET status=? WHERE url=?", (status, url))


def get_rec_status(url: str) -> str | None:
    with _conn() as c:
        r = c.execute("SELECT status FROM recommendations WHERE url=?", (url,)).fetchone()
    return r[0] if r else None


def set_status_where(from_status: str, to_status: str) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE recommendations SET status=? WHERE status=?", (to_status, from_status))
        return cur.rowcount


# ── preferences (분야별 수신 설정) ────────────────────────────────
_DEFAULT_PREFS = {
    "장학금": ("매일", "웹"), "공모전·대회": ("매일", "웹"), "대외활동·서포터즈": ("매일", "웹"),
    "학사일정": ("매주", "웹"), "채용·인턴": ("끄기", "웹"), "자격증": ("매주", "웹"), "기타": ("수동", "웹"),
}


def get_prefs() -> dict:
    with _conn() as c:
        rows = {cat: {"주기": cyc, "채널": ch}
                for cat, cyc, ch in c.execute("SELECT category,cycle,channel FROM preferences")}
    if not rows:  # 최초 1회 기본값 시드
        for cat, (cyc, ch) in _DEFAULT_PREFS.items():
            set_pref(cat, cyc, ch)
        rows = {cat: {"주기": cyc, "채널": ch} for cat, (cyc, ch) in _DEFAULT_PREFS.items()}
    return rows


def set_pref(category: str, cycle: str, channel: str = "웹"):
    with _conn() as c:
        c.execute("INSERT INTO preferences(category,cycle,channel) VALUES(?,?,?) "
                  "ON CONFLICT(category) DO UPDATE SET cycle=excluded.cycle, channel=excluded.channel",
                  (category, cycle, channel))


# ── kv (졸업진단/이수내역 등 JSON 보관) ───────────────────────────
def kv_get(key: str):
    with _conn() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def kv_set(key: str, obj):
    with _conn() as c:
        c.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(obj, ensure_ascii=False)))
