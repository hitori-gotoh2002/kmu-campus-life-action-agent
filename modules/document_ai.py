"""modules/document_ai.py
첨부파일(PDF) 분석 에이전트.

  - 첨부 URL 을 requests.get 으로 받아 io.BytesIO 메모리 스트림으로 처리 (디스크 미저장)
  - pdfplumber 로 문단 텍스트 + 평가 배점이 담긴 표(Table) 를 한 번에 추출
  - 데모 모드에서는 가짜 파싱 결과 반환
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

import requests

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


@dataclass
class ParsedDocument:
    text: str
    tables: list[list[list[str]]]
    eval_criteria: dict


def _is_demo() -> bool:
    return os.getenv("DEMO_MODE", "true").lower() == "true"


_DEMO_DOCS: dict[str, ParsedDocument] = {
    "bigdata_2025.pdf": ParsedDocument(
        text="예측 모델 성능(F1), 분석 인사이트, 발표 완성도를 평가합니다.",
        tables=[[["평가항목", "배점"], ["모델 성능", "40"], ["인사이트", "35"], ["발표", "25"]]],
        eval_criteria={"모델 성능": 40, "인사이트": 35, "발표": 25},
    ),
    "hackathon.pdf": ParsedDocument(
        text="아이디어 창의성, 프로토타입 완성도, 기술 난이도를 평가합니다.",
        tables=[[["평가항목", "배점"], ["창의성", "30"], ["완성도", "40"], ["기술", "30"]]],
        eval_criteria={"창의성": 30, "완성도": 40, "기술": 30},
    ),
    "marketing_plan.pdf": ParsedDocument(
        text="시장분석 타당성, 기획 논리, 고객 페르소나 정교함을 평가합니다.",
        tables=[[["평가항목", "배점"], ["시장분석", "35"], ["기획논리", "40"], ["페르소나", "25"]]],
        eval_criteria={"시장분석": 35, "기획논리": 40, "페르소나": 25},
    ),
}


def _criteria_from_tables(tables: list) -> dict:
    criteria: dict = {}
    for table in tables:
        for row in table[1:]:
            if len(row) >= 2 and row[1] and str(row[1]).strip().isdigit():
                criteria[str(row[0]).strip()] = int(str(row[1]).strip())
    return criteria


def parse_attachment(attachment_url: str | None) -> ParsedDocument | None:
    if not attachment_url:
        return None

    if _is_demo():
        key = attachment_url.split("/")[-1]
        doc = _DEMO_DOCS.get(key)
        if doc:
            print(f"   [document_ai] PDF 메모리 스트림 파싱 완료 (demo: {key}) "
                  f"- 평가항목 {len(doc.eval_criteria)}개")
        return doc

    # .pdf 만 파싱 (.hwp 등은 별도 파서 필요 → skip). 실패해도 파이프라인은 계속.
    if pdfplumber is None or not attachment_url.lower().split("?")[0].endswith(".pdf"):
        return None
    try:
        resp = requests.get(attachment_url, timeout=15)
        resp.raise_for_status()
        stream = io.BytesIO(resp.content)

        text_parts: list[str] = []
        tables: list = []
        with pdfplumber.open(stream) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                for tbl in page.extract_tables():
                    tables.append(tbl)

        stream.close()
        criteria = _criteria_from_tables(tables)
        print(f"   [document_ai] PDF 메모리 스트림 파싱 완료 - 평가항목 {len(criteria)}개")
        return ParsedDocument(text="\n".join(text_parts), tables=tables, eval_criteria=criteria)
    except Exception as e:
        print(f"   [document_ai] PDF 파싱 실패({attachment_url[:60]}…): {e} → 첨부 없이 진행")
        return None
