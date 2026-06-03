"""modules/transcript.py
성적증명서(PDF) 추출 — 이미지형 전자증명서를 LLM 비전으로 구조화.

이 PDF들은 텍스트 레이어가 없어(pdfplumber 불가) 페이지를 이미지로 렌더한 뒤
OpenAI 비전으로 추출한다. '이수구분별 취득학점 합계'와 총학점/GPA 는 정확하게
추출되며, 개별 과목명은 환각 가능성이 있어 진단 핵심으로 쓰지 않는다.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re


def _render_png_b64(pdf_bytes: bytes, scale: float = 2.5) -> str:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_bytes)
    img = pdf[0].render(scale=scale).to_pil()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


_PROMPT = (
    "이 한국 대학 성적증명서 이미지를 읽고 JSON 으로만 추출하라.\n"
    "스키마: {student:{name, major, fusion_major, admission_year}, "
    "summary:{total_credits:number, gpa:number, percentile:number}, "
    "by_category:{이수구분별 취득학점, 한글 구분명:학점(number)}, "
    "courses:[{name, credits:number, category, grade}]}.\n"
    "'<이수구분별 취득학점>' 표의 값을 by_category 에 정확히 넣어라(예: 전공필수, 전공선택, "
    "교양선택, 일반선택, 기초교양, 자유교양 등). 0 인 항목은 생략 가능. JSON 외 텍스트 금지."
)


def extract(pdf_bytes: bytes, model: str | None = None) -> dict:
    """성적증명서 PDF(bytes) → 구조화 dict. 실패 시 빈 dict."""
    from openai import OpenAI
    b64 = _render_png_b64(pdf_bytes)
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=model or os.getenv("TRANSCRIPT_MODEL", "gpt-4o"),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _PROMPT},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
        ]}],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content
    data = json.loads(re.sub(r"```json|```", "", raw).strip())
    # 숫자 보정
    bc = data.get("by_category", {}) or {}
    data["by_category"] = {k: _num(v) for k, v in bc.items() if _num(v)}
    s = data.get("summary", {}) or {}
    data["summary"] = {k: _num(v) for k, v in s.items()}
    return data


def extract_path(path: str, model: str | None = None) -> dict:
    with open(path, "rb") as f:
        return extract(f.read(), model)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
