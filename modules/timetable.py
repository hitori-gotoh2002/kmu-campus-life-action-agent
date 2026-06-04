"""modules/timetable.py
시간표 PDF 업로드/분석.

노션 캘린더에는 회의/모임 같은 실제 일정만 두고,
수업 시간표는 웹에서 PDF로 업로드해 로컬 백엔드에 저장한다.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re

from modules import store

KV_KEY = "timetable"
DAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _num_time(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("：", ":")
    if ":" in s:
        h, m = s.split(":", 1)
        return int(re.sub(r"\D", "", h) or 0) + int(re.sub(r"\D", "", m[:2]) or 0) / 60
    try:
        return float(s)
    except ValueError:
        return 0.0


def _render_png_b64(pdf_bytes: bytes, scale: float = 2.0) -> str:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_bytes)
    img = pdf[0].render(scale=scale).to_pil()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _extract_text(pdf_bytes: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _clean_row(row: dict) -> dict | None:
    day = str(row.get("day") or row.get("요일") or "").strip()[:1]
    if day not in DAYS:
        return None
    start = _num_time(row.get("start") or row.get("시작"))
    end = _num_time(row.get("end") or row.get("종료"))
    if not start or not end or end <= start:
        return None
    return {
        "name": str(row.get("name") or row.get("과목명") or row.get("title") or "수업").strip(),
        "day": day,
        "start": round(start, 2),
        "end": round(end, 2),
        "place": str(row.get("place") or row.get("장소") or "").strip(),
    }


def parse_text(text: str) -> list[dict]:
    """텍스트 레이어가 있는 시간표 PDF를 간단 규칙으로 파싱."""
    rows = []
    time_pat = r"(\d{1,2}(?::\d{2})?)\s*[-~]\s*(\d{1,2}(?::\d{2})?)"
    for line in (text or "").splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if not clean:
            continue
        m = re.search(rf"([월화수목금토일])(?:요일)?\s*{time_pat}", clean)
        if not m:
            m = re.search(rf"{time_pat}\s*([월화수목금토일])(?:요일)?", clean)
            if m:
                start, end, day = m.group(1), m.group(2), m.group(3)
            else:
                continue
        else:
            day, start, end = m.group(1), m.group(2), m.group(3)
        name = re.sub(rf"([월화수목금토일])(?:요일)?|{time_pat}", " ", clean)
        row = _clean_row({"name": name.strip(" -~|"), "day": day, "start": start, "end": end})
        if row:
            rows.append(row)
    return rows


def _llm_extract(pdf_bytes: bytes) -> list[dict]:
    from openai import OpenAI

    b64 = _render_png_b64(pdf_bytes)
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    prompt = (
        "이 대학 시간표 이미지를 읽고 JSON으로만 반환하라. "
        "스키마: {classes:[{name, day, start, end, place}]}. "
        "day는 월/화/수/목/금/토/일 중 하나, start/end는 24시간 숫자 또는 HH:MM. "
        "수업이 여러 요일이면 행을 나누어라."
    )
    resp = client.chat.completions.create(
        model=os.getenv("TIMETABLE_MODEL", os.getenv("TRANSCRIPT_MODEL", "gpt-4o")),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
        ]}],
        response_format={"type": "json_object"},
    )
    data = json.loads(re.sub(r"```json|```", "", resp.choices[0].message.content).strip())
    rows = []
    for row in data.get("classes", []):
        cleaned = _clean_row(row)
        if cleaned:
            rows.append(cleaned)
    return rows


def extract(pdf_bytes: bytes) -> dict:
    text = _extract_text(pdf_bytes)
    rows = parse_text(text)
    method = "pdf-text"
    if not rows and os.getenv("OPENAI_API_KEY"):
        rows = _llm_extract(pdf_bytes)
        method = "openai-vision"
    return {
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "method": method,
        "rows": rows,
    }


def save(data: dict) -> None:
    store.kv_set(KV_KEY, data)


def normalize_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        cleaned = _clean_row(row)
        if cleaned:
            out.append(cleaned)
    return out


def load() -> dict:
    return store.kv_get(KV_KEY) or {"rows": []}


def clear() -> None:
    store.kv_set(KV_KEY, {"rows": []})


def busy_hours(rows: list[dict] | None = None) -> float:
    rows = rows if rows is not None else load().get("rows", [])
    total = 0.0
    for row in rows:
        total += max(0.0, float(row.get("end") or 0) - float(row.get("start") or 0))
    return round(total, 1)
