"""modules/telegram_callbacks.py
텔레그램 버튼 콜백 처리기.

기존 digest 버튼은 실행 중인 프로세스가 기다리는 동안만 동작했다.
이 모듈은 callback_data 또는 메시지 본문의 원문 URL로 로컬 추천 이력을 찾아
승인/거절을 나중에 눌러도 처리할 수 있게 한다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import traceback
from pathlib import Path
from types import SimpleNamespace

import requests
from dotenv import load_dotenv

from modules import executor, history, store


load_dotenv()
store.load_keys_into_env()

LOG_PATH = Path("data/telegram_callbacks.log")


def _sanitize(message: str) -> str:
    message = re.sub(r"/bot[^/\s]+/", "/bot<hidden>/", message or "")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<hidden>", message)


def callback_key(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


def _log(message: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {_sanitize(message)}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _action(data: str) -> str | None:
    if data.startswith(("ap:", "approve")):
        return "approve"
    if data.startswith(("rj:", "reject")):
        return "reject"
    return None


def _token(data: str) -> str:
    return data.split(":", 1)[1] if ":" in data else ""


def _url_from_text(text: str) -> str:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0).rstrip(".,)]}") if m else ""


def _title_from_text(text: str) -> str:
    first = (text or "").splitlines()[0] if text else ""
    m = re.match(r"\s*📌\s*\[[^\]]+\]\s*(.+)", first)
    return (m.group(1) if m else first).strip()


def _find_recommendation(data: str, text: str) -> dict | None:
    url = _url_from_text(text)
    token = _token(data)
    title = _title_from_text(text)

    rows = store.list_recs()
    if url:
        for row in rows:
            if row["url"] == url:
                return row
    if token and not token.isdigit():
        for row in rows:
            if callback_key(row["url"]) == token:
                return row
    if token.isdigit():
        pending = store.list_recs("추천완료")
        idx = int(token)
        if 0 <= idx < len(pending):
            return pending[idx]
    if title:
        for row in rows:
            if row["title"] == title or row["title"] in title or title in row["title"]:
                return row
    return None


def _candidate_from_row(row: dict) -> dict:
    notice = SimpleNamespace(
        title=row["title"],
        url=row["url"],
        date=row.get("deadline") or "",
        source=row.get("source") or "",
        category=row.get("category") or "",
        body=row.get("body") or "",
    )
    analysis = SimpleNamespace(
        suitability_score=int(row.get("score") or 0),
        estimated_hours_needed=int(row.get("hours") or 0),
        matching_reason=row.get("reason") or "",
        summary=row.get("summary") or "",
        domain=row.get("domain") or "",
    )
    return {"notice": notice, "analysis": analysis, "category": row.get("category") or ""}


def handle_callback(callback: dict) -> str:
    data = callback.get("data") or ""
    msg = callback.get("message") or {}
    text = msg.get("text") or ""
    action = _action(data)
    if not action:
        _log(f"[callback] ignored data={data[:24]}")
        return "ignored"

    row = _find_recommendation(data, text)
    if not row:
        title = _title_from_text(text)
        url = _url_from_text(text)
        _log(f"[callback] not_found action={action} token={_token(data)} title={title[:80]} url={url}")
        return "not_found"

    if action == "reject":
        history.mark(row["url"], "거절")
        _log(f"[callback] rejected title={row['title'][:80]} url={row['url']}")
        return "rejected"

    was_approved = row.get("status") == "승인"

    try:
        created = executor.execute_actions(_candidate_from_row(row))
    except Exception as e:
        _log(
            f"[callback] execute_failed title={row['title'][:80]} url={row['url']} "
            f"error={type(e).__name__}: {e}\n{traceback.format_exc().rstrip()}"
        )
        return "execute_failed"
    if was_approved:
        result = "approved_restored" if created else "already_approved"
        _log(f"[callback] {result} title={row['title'][:80]} url={row['url']}")
        return result
    history.mark(row["url"], "승인")
    _log(f"[callback] approved title={row['title'][:80]} url={row['url']}")
    return "approved"


def poll_once(timeout: int = 0, acknowledge: bool = True) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN missing", "handled": 0}

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"timeout": timeout, "allowed_updates": ["callback_query"]},
            timeout=timeout + 10,
        )
    except requests.RequestException as e:
        _log(f"[callback] poll_failed error={type(e).__name__}: {e}")
        return {"ok": False, "error": type(e).__name__, "updates": 0, "handled": 0}
    payload = resp.json()
    updates = payload.get("result", [])
    handled = 0
    ack_update_id = None

    for update in updates:
        update_id = update.get("update_id")
        cb = update.get("callback_query")
        if not cb:
            ack_update_id = update_id
            continue
        result = handle_callback(cb)
        answer = {
            "approved": "노션 캘린더에 추가했어요.",
            "approved_restored": "노션 캘린더에 다시 추가했어요.",
            "already_approved": "이미 처리된 추천이에요.",
            "rejected": "추천을 무시 처리했어요.",
            "not_found": "추천 이력을 찾지 못했어요.",
            "execute_failed": "노션 추가 중 오류가 났어요. 로그를 확인해 주세요.",
        }.get(result, "처리할 수 없는 버튼이에요.")
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                json={"callback_query_id": cb["id"], "text": answer},
                timeout=5,
            )
        except Exception:
            pass
        if result in {"approved", "approved_restored", "already_approved", "rejected"}:
            handled += 1
            ack_update_id = update_id
        elif result not in {"not_found", "execute_failed"}:
            ack_update_id = update_id
        else:
            break

    if acknowledge and ack_update_id is not None:
        try:
            requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": ack_update_id + 1, "timeout": 0, "allowed_updates": ["callback_query"]},
                timeout=10,
            )
        except requests.RequestException as e:
            _log(f"[callback] ack_failed update_id={ack_update_id} error={type(e).__name__}: {e}")
    return {"ok": payload.get("ok", False), "updates": len(updates), "handled": handled}


if __name__ == "__main__":
    print(poll_once(timeout=0))
