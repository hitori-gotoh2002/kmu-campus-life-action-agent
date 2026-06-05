"""졸업진단 RAG용 Chroma 인덱스 빌드 — 국민대 요람 PDF → 임베딩 → graduation_center/data/graduation/chroma.

규정 근거 해설(LLM 규정해설)·총평이 요람 원문 chunk를 근거로 인용할 때 사용한다.
한 번만 빌드하면 되며, OPENAI_API_KEY(임베딩)와 요람 PDF가 필요하다.

실행: python scripts/build_graduation_index.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
load_dotenv()

# 저장된 키(로컬 DB)도 환경변수로 끌어올림
try:
    from modules import store
    store.load_keys_into_env()
except Exception:
    pass

_PKG_GRAD = _ROOT / "graduation_center" / "data" / "graduation"
PDF_PATH = _PKG_GRAD / "v2" / "sources" / "korean_curriculum_2025.pdf"
CHROMA_DIR = _PKG_GRAD / "chroma"
COLLECTION_NAME = "kmu_graduation_yoram"
EMBED_MODEL = "text-embedding-3-small"

SECTION_MARKERS = [
    "졸업이수학점", "졸업 이수 학점", "졸업요건", "졸업 요건", "교과과정", "교육과정",
    "교과목 개요", "이수규정", "마이크로디그리", "마이크로 디그리", "소학위",
    "복수전공", "부전공", "융합전공", "졸업예정증명서", "학위수여",
]


def chunk_text(text: str, page_num: int, max_chars: int = 800) -> list[dict]:
    paragraphs = re.split(r"\n{2,}", text.strip())
    chunks, current = [], ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(current) + len(paragraph) > max_chars and current:
            chunks.append({"text": current.strip(), "page": page_num})
            current = paragraph
        else:
            current = f"{current}\n{paragraph}" if current else paragraph
    if current.strip():
        chunks.append({"text": current.strip(), "page": page_num})
    return chunks


def detect_section(text: str) -> str:
    for marker in SECTION_MARKERS:
        if marker in text:
            return marker
    return "일반"


def extract_department(text: str) -> str:
    for pattern in [r"([가-힣]+학과)\s", r"([가-힣]+전공)\s", r"([가-힣]+학부)\s", r"([가-힣]+대학)\s"]:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return "공통"


def embed_batch(client, texts: list[str]) -> list[list[float]]:
    embeddings, batch_size = [], 100
    for index in range(0, len(texts), batch_size):
        batch = [t if t.strip() else " " for t in texts[index:index + batch_size]]
        response = client.embeddings.create(model=EMBED_MODEL, input=batch)
        embeddings.extend([item.embedding for item in response.data])
        print(f"임베딩 {index + len(batch)}/{len(texts)} 완료")
    return embeddings


def _resolve_pdf(default: Path) -> Path:
    if default.exists():
        return default
    parent = default.parent if default.parent.exists() else Path(".")
    target = unicodedata.normalize("NFC", default.name)
    for cand in parent.glob("*.pdf"):
        if unicodedata.normalize("NFC", cand.name) == target:
            return cand
    return default


def build_index(pdf_path: Path = PDF_PATH, chroma_dir: Path = CHROMA_DIR) -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY 가 필요합니다(.env 또는 설정 페이지에 저장).")
    pdf_path = _resolve_pdf(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"요람 PDF를 찾을 수 없습니다: {pdf_path}")

    import chromadb
    import pdfplumber
    from openai import OpenAI

    chroma_dir.mkdir(parents=True, exist_ok=True)
    openai_client = OpenAI()
    chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
    try:
        chroma_client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = chroma_client.create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})

    all_chunks = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total_pages = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text or len(text.strip()) < 30:
                continue
            for chunk in chunk_text(text, page_num):
                chunk["section"] = detect_section(chunk["text"])
                chunk["department"] = extract_department(chunk["text"])
                all_chunks.append(chunk)

    print(f"총 {len(all_chunks)}개 청크 생성 (요람 {total_pages}쪽)")
    embeddings = embed_batch(openai_client, [c["text"] for c in all_chunks])
    for index in range(0, len(all_chunks), 500):
        batch = all_chunks[index:index + 500]
        collection.add(
            ids=[f"grad_chunk_{index + off}" for off in range(len(batch))],
            embeddings=embeddings[index:index + len(batch)],
            documents=[c["text"] for c in batch],
            metadatas=[{"page": c["page"], "section": c["section"], "department": c["department"]}
                       for c in batch],
        )
        print(f"저장 {index + len(batch)}/{len(all_chunks)} 완료")

    stats = {"total_chunks": len(all_chunks), "total_pages": total_pages,
             "embedding_model": EMBED_MODEL, "collection_name": COLLECTION_NAME}
    (chroma_dir.parent / "index_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"졸업 RAG 인덱싱 완료: {chroma_dir}")


if __name__ == "__main__":
    build_index()
