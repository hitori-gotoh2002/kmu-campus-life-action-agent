"""Migrate local data/agent.db rows into the configured Supabase backend.

Usage:
  python scripts/migrate_sqlite_to_supabase.py
  python scripts/migrate_sqlite_to_supabase.py --include-settings

Requires:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY  (or SUPABASE_ANON_KEY with suitable policies)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
os.environ["STORE_BACKEND"] = "supabase"

from modules import store  # noqa: E402


def _rows(con: sqlite3.Connection, query: str):
    con.row_factory = sqlite3.Row
    return [dict(row) for row in con.execute(query)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "agent.db"))
    parser.add_argument(
        "--include-settings",
        action="store_true",
        help="Also migrate settings table. This may upload API keys, so leave it off unless you mean it.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"SQLite DB not found: {db_path}")

    try:
        backend = store.active_backend()
    except RuntimeError as e:
        raise SystemExit(str(e)) from e
    print(f"target backend: {backend}")
    if backend != "supabase":
        raise SystemExit("Supabase backend is not active. Check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")

    con = sqlite3.connect(db_path)
    counts = {"settings": 0, "profile": 0, "preferences": 0, "kv": 0, "recommendations": 0}

    if args.include_settings:
        for row in _rows(con, "select key,value from settings"):
            store.set_setting(row["key"], row["value"])
            counts["settings"] += 1

    for row in _rows(con, "select key,value from profile"):
        store.set_profile({row["key"]: row["value"]})
        counts["profile"] += 1

    for row in _rows(con, "select category,cycle,channel from preferences"):
        store.set_pref(row["category"], row["cycle"], row["channel"])
        counts["preferences"] += 1

    for row in _rows(con, "select key,value from kv"):
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError:
            value = row["value"]
        store.kv_set(row["key"], value)
        counts["kv"] += 1

    rec_cols = "url,title,category,source,score,hours,deadline,reason,domain,status,body,summary,created_at"
    for row in _rows(con, f"select {rec_cols} from recommendations"):
        store.add_rec(row)
        counts["recommendations"] += 1

    con.close()
    print("migrated:")
    for key, value in counts.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
