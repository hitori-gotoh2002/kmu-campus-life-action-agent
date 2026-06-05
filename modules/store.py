"""modules/store.py
Shared backend storage.

Default backend is the existing local SQLite file at data/agent.db.
When SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY/SUPABASE_ANON_KEY are set,
the same public functions use Supabase PostgREST instead. This lets GitHub
Actions and the local Streamlit app read/write one shared recommendation DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests

_DB_PATH = Path(__file__).parent.parent / "data" / "agent.db"

_REC_COLUMNS = [
    "url", "title", "category", "source", "score", "hours", "deadline",
    "reason", "domain", "status", "body", "summary", "created_at",
]

_DEFAULT_PREFS = {
    "장학금": ("매일", "웹"),
    "공모전·대회": ("매일", "웹"),
    "대외활동·서포터즈": ("매일", "웹"),
    "학사일정": ("매주", "웹"),
    "채용·인턴": ("끄기", "웹"),
    "자격증": ("매주", "웹"),
    "기타": ("수동", "웹"),
}

# 웹에서 저장한 키를 환경변수로 올림 → 기존 모듈(os.getenv)이 그대로 동작
KEY_NAMES = [
    "OPENAI_API_KEY", "OPENAI_MODEL", "NOTION_API_KEY",
    "NOTION_CALENDAR_DB_ID", "NOTION_PORTFOLIO_PAGE_ID",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    # 졸업진단 RAG 규정해설·총평·what-if용 OpenAI 모델(미설정 시 코드 기본값)
    "OPENAI_GRADUATION_MODEL",
]


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


class SQLiteBackend:
    name = "sqlite"

    def get_setting(self, key: str, default=None):
        with _conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key: str, value: str):
        with _conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def all_settings(self) -> dict:
        with _conn() as c:
            return {k: v for k, v in c.execute("SELECT key,value FROM settings")}

    def get_profile(self) -> dict:
        with _conn() as c:
            return {k: v for k, v in c.execute("SELECT key,value FROM profile")}

    def set_profile(self, d: dict):
        with _conn() as c:
            for k, v in d.items():
                c.execute(
                    "INSERT INTO profile(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str(v)),
                )

    def rec_exists(self, url: str) -> bool:
        with _conn() as c:
            return c.execute("SELECT 1 FROM recommendations WHERE url=?", (url,)).fetchone() is not None

    def add_rec(self, rec: dict):
        row = {k: rec.get(k) for k in _REC_COLUMNS if k != "created_at"}
        row["created_at"] = rec.get("created_at") or time.time()
        with _conn() as c:
            c.execute(
                """INSERT INTO recommendations
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
                row,
            )

    def list_recs(self, status: str | None = None) -> list:
        q = (
            "SELECT url,title,category,source,score,hours,deadline,reason,domain,status,body,summary,created_at "
            "FROM recommendations"
        )
        args = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY score DESC"
        with _conn() as c:
            return [dict(zip(_REC_COLUMNS, row)) for row in c.execute(q, args)]

    def set_rec_status(self, url: str, status: str):
        with _conn() as c:
            c.execute("UPDATE recommendations SET status=? WHERE url=?", (status, url))

    def get_rec_status(self, url: str) -> str | None:
        with _conn() as c:
            r = c.execute("SELECT status FROM recommendations WHERE url=?", (url,)).fetchone()
        return r[0] if r else None

    def set_status_where(self, from_status: str, to_status: str) -> int:
        with _conn() as c:
            cur = c.execute("UPDATE recommendations SET status=? WHERE status=?", (to_status, from_status))
            return cur.rowcount

    def get_prefs(self) -> dict:
        with _conn() as c:
            rows = {
                cat: {"주기": cyc, "채널": ch}
                for cat, cyc, ch in c.execute("SELECT category,cycle,channel FROM preferences")
            }
        if not rows:
            for cat, (cyc, ch) in _DEFAULT_PREFS.items():
                self.set_pref(cat, cyc, ch)
            rows = {cat: {"주기": cyc, "채널": ch} for cat, (cyc, ch) in _DEFAULT_PREFS.items()}
        return rows

    def set_pref(self, category: str, cycle: str, channel: str = "웹"):
        with _conn() as c:
            c.execute(
                "INSERT INTO preferences(category,cycle,channel) VALUES(?,?,?) "
                "ON CONFLICT(category) DO UPDATE SET cycle=excluded.cycle, channel=excluded.channel",
                (category, cycle, channel),
            )

    def kv_get(self, key: str):
        with _conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def kv_set(self, key: str, obj):
        with _conn() as c:
            c.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(obj, ensure_ascii=False)),
            )


class SupabaseBackend:
    name = "supabase"

    def __init__(self, url: str, key: str):
        self.base_url = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        prefer: str | None = None,
    ):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        resp = requests.request(
            method,
            f"{self.base_url}/{table}",
            headers=headers,
            params=params,
            json=json_body,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Supabase {method} {table} failed {resp.status_code}: {resp.text[:500]}")
        if not resp.text:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _select(self, table: str, params: dict[str, Any] | None = None) -> list[dict]:
        return self._request("GET", table, params=params) or []

    def _upsert(self, table: str, row: dict, conflict_key: str) -> None:
        self._request(
            "POST",
            table,
            params={"on_conflict": conflict_key},
            json_body=row,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def get_setting(self, key: str, default=None):
        rows = self._select("settings", {"select": "value", "key": f"eq.{key}", "limit": "1"})
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str):
        self._upsert("settings", {"key": key, "value": str(value)}, "key")

    def all_settings(self) -> dict:
        return {row["key"]: row.get("value", "") for row in self._select("settings", {"select": "key,value"})}

    def get_profile(self) -> dict:
        return {row["key"]: row.get("value", "") for row in self._select("profile", {"select": "key,value"})}

    def set_profile(self, d: dict):
        for k, v in d.items():
            self._upsert("profile", {"key": k, "value": str(v)}, "key")

    def rec_exists(self, url: str) -> bool:
        rows = self._select("recommendations", {"select": "url", "url": f"eq.{url}", "limit": "1"})
        return bool(rows)

    def add_rec(self, rec: dict):
        row = {k: rec.get(k) for k in _REC_COLUMNS if k != "created_at"}
        row["created_at"] = rec.get("created_at") or time.time()
        self._upsert("recommendations", row, "url")

    def list_recs(self, status: str | None = None) -> list:
        params: dict[str, Any] = {"select": ",".join(_REC_COLUMNS), "order": "score.desc"}
        if status:
            params["status"] = f"eq.{status}"
        return self._select("recommendations", params)

    def set_rec_status(self, url: str, status: str):
        self._request(
            "PATCH",
            "recommendations",
            params={"url": f"eq.{url}"},
            json_body={"status": status},
            prefer="return=minimal",
        )

    def get_rec_status(self, url: str) -> str | None:
        rows = self._select("recommendations", {"select": "status", "url": f"eq.{url}", "limit": "1"})
        return rows[0]["status"] if rows else None

    def set_status_where(self, from_status: str, to_status: str) -> int:
        rows = self._request(
            "PATCH",
            "recommendations",
            params={"status": f"eq.{from_status}"},
            json_body={"status": to_status},
            prefer="return=representation",
        )
        return len(rows or [])

    def get_prefs(self) -> dict:
        rows = {
            row["category"]: {"주기": row.get("cycle") or "수동", "채널": row.get("channel") or "웹"}
            for row in self._select("preferences", {"select": "category,cycle,channel"})
        }
        if not rows:
            for cat, (cyc, ch) in _DEFAULT_PREFS.items():
                self.set_pref(cat, cyc, ch)
            rows = {cat: {"주기": cyc, "채널": ch} for cat, (cyc, ch) in _DEFAULT_PREFS.items()}
        return rows

    def set_pref(self, category: str, cycle: str, channel: str = "웹"):
        self._upsert("preferences", {"category": category, "cycle": cycle, "channel": channel}, "category")

    def kv_get(self, key: str):
        rows = self._select("kv", {"select": "value", "key": f"eq.{key}", "limit": "1"})
        return json.loads(rows[0]["value"]) if rows else None

    def kv_set(self, key: str, obj):
        self._upsert("kv", {"key": key, "value": json.dumps(obj, ensure_ascii=False)}, "key")


def _supabase_key() -> str:
    return (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_KEY")
        or ""
    )


@lru_cache(maxsize=1)
def _backend():
    backend = os.getenv("STORE_BACKEND", "").strip().lower()
    url = os.getenv("SUPABASE_URL", "").strip()
    key = _supabase_key().strip()
    if backend == "sqlite":
        return SQLiteBackend()
    if backend in {"supabase", "remote"}:
        if not url or not key:
            raise RuntimeError("STORE_BACKEND=supabase requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        return SupabaseBackend(url, key)
    if url and key:
        return SupabaseBackend(url, key)
    return SQLiteBackend()


def active_backend() -> str:
    return _backend().name


# ── settings (API 키, 자동수신 플래그 등) ──────────────────────────
def get_setting(key: str, default=None):
    return _backend().get_setting(key, default)


def set_setting(key: str, value: str):
    return _backend().set_setting(key, value)


def all_settings() -> dict:
    return _backend().all_settings()


def load_keys_into_env():
    for k in KEY_NAMES:
        v = get_setting(k)
        if v:
            os.environ[k] = v


# ── profile (포트폴리오 분석 결과) ────────────────────────────────
def get_profile() -> dict:
    return _backend().get_profile()


def set_profile(d: dict):
    return _backend().set_profile(d)


# ── recommendations (추천 이력) ───────────────────────────────────
def rec_exists(url: str) -> bool:
    return _backend().rec_exists(url)


def add_rec(rec: dict):
    return _backend().add_rec(rec)


def list_recs(status: str | None = None) -> list:
    return _backend().list_recs(status)


def set_rec_status(url: str, status: str):
    return _backend().set_rec_status(url, status)


def get_rec_status(url: str) -> str | None:
    return _backend().get_rec_status(url)


def set_status_where(from_status: str, to_status: str) -> int:
    return _backend().set_status_where(from_status, to_status)


# ── preferences (분야별 수신 설정) ────────────────────────────────
def get_prefs() -> dict:
    return _backend().get_prefs()


def set_pref(category: str, cycle: str, channel: str = "웹"):
    return _backend().set_pref(category, cycle, channel)


# ── kv (졸업진단/이수내역 등 JSON 보관) ───────────────────────────
def kv_get(key: str):
    return _backend().kv_get(key)


def kv_set(key: str, obj):
    return _backend().kv_set(key, obj)
