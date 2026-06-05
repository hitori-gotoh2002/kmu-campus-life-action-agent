"""규정 근거 해설 노드 — 보고서 내장형 RAG (챗 UI 없음).

audit의 주요 부족 항목 2~3개를 결정론으로 선정 → 요람 Chroma 검색 → LLM이
검색 chunk만 근거로 해설 생성(source_ids enum 강제) → 결정론 validator가 인용
해소·수치 정합·마스킹 검증 → 컨설팅 보고서의 '규정 근거 해설' 섹션으로 삽입.

판정은 결정론 본체가 source of truth — LLM은 규정 원문의 자연어 해설만 한다.
동일 입력 캐시(해시 키)로 데모 일관성·지연을 보장하고, 실패 시 해당 섹션만
graceful degrade(audit 응답은 정상 완료).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from graduation_center.v2.models_v2 import (
    AuditResult, ExplainLine, ExplainSection, NodeTraceEvent, RequirementProfile,
    Source, StudentContext,
)

# 데이터는 패키지 내부에 동봉 — CWD 비의존(__file__ 기준 앵커링).
_PKG_GRAD = Path(__file__).resolve().parents[1] / "data" / "graduation"
CHROMA_DIR = str(_PKG_GRAD / "chroma")
COLLECTION = "kmu_graduation_yoram"
EMBEDDING_MODEL = "text-embedding-3-small"
CACHE_PATH = _PKG_GRAD / "v2" / "explain_cache.json"
MAX_ITEMS = 3
TOP_K = 4
_STUDENT_ID = re.compile(r"\b(20\d{2})\d{4}\b")


def _yoram_url_2025(page=None):
    """Y chunk(2025 요람 기반) 원문 PDF 링크 — 클릭 시 해당 페이지(사용자 제안 2026-06-05)."""
    try:
        from graduation_center.v2.pipeline import yoram_url
        return yoram_url(2025, page)
    except Exception:
        return None


# ---------- 1) 해설 대상 선정 (결정론) ----------
def select_explain_items(audit: AuditResult, profile: RequirementProfile,
                         ctx: StudentContext) -> list[dict]:
    """부족 항목을 우선순위(융합 > 영역 > 핵심교양 세부)로 최대 3개 선정.

    필수과목(missing_required)은 LLM 해설 대상에서 **제외**(2026-06-06, 3파트 develop §A) —
    캐시 실측에서 전체 미확인 줄의 61%가 이 항목이었고 원인은 검색 실패가 아니라 LLM 인용
    누락. 필수 지정은 교과과정표 데이터로 이미 결정론 확정이라 pipeline이 결정론 섹션
    (deterministic=True·G1 근거)을 합성한다 — "필수=결정론, 정책=RAG" 역할 분리.
    """
    items: list[dict] = []
    dept = profile.department_name_ko

    # S(학점·요람 요건 충족) 학생: 부족 항목이 없어 items가 비므로 조기졸업 안내를 삽입
    # (2026-06-05 사용자 지시 — 판정은 안 함·평점 데이터 없음, 규정 RAG 안내만)
    from graduation_center.v2.risk import already_met
    if already_met(audit, ctx):
        items.append({
            "key": "early_graduation",
            "title": "조기졸업 요건 안내 (요건 충족 학생)",
            "query": "조기졸업 신청 승인 요건 평점 등록학기 제95조",
            "context": "학점·요람 요건 전부 충족 — 조기졸업 가능성 검토(평점·등록학기 등은 본인 확인)",
        })

    for cc in audit.convergence_checks:
        short_groups = [gc for gc in cc.get("group_checks", []) if gc["gap"] > 0]
        if cc.get("gap", 0) > 0 or short_groups:
            gtxt = ", ".join(f"{gc['group']} {gc['gap']:.0f}학점 부족" for gc in short_groups)
            items.append({
                "key": f"conv:{cc['program_id']}",
                "title": f"{cc['name']}({cc['track']}) 이수 요건",
                "query": f"연계 융합전공 {cc['name']} 이수 요건 학점 중복인정 한도 {cc['track']}",
                "context": f"{cc['name']} {cc['earned']:.0f}/{cc['required']:.0f}학점"
                           + (f" · {gtxt}" if gtxt else "")
                           + f" · 중복인정 {cc['double_used']:.0f}/{cc['double_cap']:.0f}",
            })
    for g in audit.area_gaps:
        if g.gap > 0 and g.area in ("전공", "기초교양", "핵심교양", "자유교양"):
            items.append({
                "key": f"area:{g.area}",
                "title": f"{g.area} 이수학점 부족 ({g.gap:.0f}학점)",
                "query": f"{dept} 졸업요건 {g.area} 최저 이수학점",
                "context": f"{g.area} {g.earned:.0f}/{g.required:.0f}학점 (부족 {g.gap:.0f})",
            })
    core_short = [g for g in audit.core_area_gaps if g.gap > 0]
    if core_short:
        areas = ", ".join(g.area for g in core_short)
        items.append({
            "key": "core_areas",
            "title": f"핵심교양 영역별 최저 미충족 ({areas})",
            "query": f"핵심교양 영역별 최저 이수 기준 {areas}",
            "context": "; ".join(f"{g.area} {g.earned:.0f}/{g.required:.0f}" for g in core_short),
        })
    return items[:MAX_ITEMS]


# ---------- 2) 요람 검색 (tool) ----------
def retrieve_yoram(query: str, client, top_k: int = TOP_K) -> list[dict]:
    import chromadb
    col = chromadb.PersistentClient(path=CHROMA_DIR).get_collection(COLLECTION)
    emb = client.embeddings.create(model=EMBEDDING_MODEL, input=[query]).data[0].embedding
    r = col.query(query_embeddings=[emb], n_results=top_k, include=["documents", "metadatas"])
    out = []
    for doc, meta in zip(r.get("documents", [[]])[0], r.get("metadatas", [[]])[0]):
        out.append({"page": (meta or {}).get("page"), "section": (meta or {}).get("section", "요람"),
                    "text": (doc or "")[:600]})
    return out


# ---------- 3) LLM 해설 생성 (llm) ----------
def _schema(item_keys: list[str], source_ids: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "explanations": {
                "type": "array", "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "item_key": {"type": "string", "enum": item_keys},
                        "lines": {
                            "type": "array", "maxItems": 4,
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "properties": {
                                    "text": {"type": "string", "maxLength": 220},
                                    "source_ids": {"type": "array", "maxItems": 3,
                                                   "items": {"type": "string", "enum": source_ids}},
                                },
                                "required": ["text", "source_ids"],
                            },
                        },
                    },
                    "required": ["item_key", "lines"],
                },
            },
        },
        "required": ["explanations"],
    }


def _prompt(items: list[dict], chunks_by_item: dict[str, list[dict]],
            id_by_chunk: dict[int, str]) -> str:
    blocks = []
    for it in items:
        src = "\n".join(
            f"[{id_by_chunk[id(c)]}] 요람 p.{c['page']} ({c['section']})\n{c['text']}"
            for c in chunks_by_item.get(it["key"], []))
        blocks.append(f"### 항목 {it['key']} — {it['title']}\n진단 결과: {it['context']}\n근거 후보:\n{src}")
    body = "\n\n".join(blocks)
    return f"""졸업사정 컨설팅 보고서의 '규정 근거 해설' 섹션을 작성한다.

규칙:
- 각 항목마다 2~4줄. 제공된 근거 chunk의 내용만 사용해 해당 규정/요건을 해설한다.
- 줄마다 직접 근거가 된 chunk ID만 source_ids에 넣는다(근거 없는 줄 금지).
- 진단 결과의 숫자는 인용 가능. 근거 chunk에 없는 새 수치·요건을 만들지 않는다.
- 졸업 가능/불가 판정은 하지 않는다 — 판정은 시스템이 이미 했고, 너는 규정 해설만 한다.
- 학생이 다음에 할 행동(학과사무실 확인 등)으로 끝맺어도 좋다. 한국어, 존댓말.

{body}"""


def _call_llm(client, model: str, items: list[dict], chunks_by_item: dict,
              id_by_chunk: dict) -> dict:
    sids = sorted(id_by_chunk.values())
    kwargs = {
        "model": model,
        "input": [
            {"role": "system",
             "content": "You write grounded Korean explanations of university regulations using only provided yoram chunks."},
            {"role": "user", "content": _prompt(items, chunks_by_item, id_by_chunk)},
        ],
        "text": {"format": {"type": "json_schema", "name": "explain_sections",
                            "schema": _schema([i["key"] for i in items], sids), "strict": True}},
    }
    if model.startswith(("gpt-5", "o")):
        kwargs["reasoning"] = {"effort": "minimal"}
    try:
        resp = client.responses.create(**kwargs, temperature=0.1)
    except Exception as exc:
        if "temperature" in str(exc).lower():
            resp = client.responses.create(**kwargs)
        else:
            raise
    return json.loads(getattr(resp, "output_text", "") or "")


# ---------- 4) 인용 검증 (validator, 결정론) ----------
def validate_explanations(raw: dict, items: list[dict], chunks_by_item: dict,
                          id_by_chunk: dict) -> list[ExplainSection]:
    chunk_text_by_id = {}
    allowed_by_item: dict[str, set] = {}
    for it in items:
        ids = set()
        for c in chunks_by_item.get(it["key"], []):
            cid = id_by_chunk[id(c)]
            ids.add(cid)
            chunk_text_by_id[cid] = c["text"]
        allowed_by_item[it["key"]] = ids
    title_by_key = {i["key"]: i["title"] for i in items}
    ctx_by_key = {i["key"]: i["context"] for i in items}

    sections: list[ExplainSection] = []
    for ex in raw.get("explanations", []):
        key = ex.get("item_key")
        if key not in title_by_key:
            continue
        lines: list[ExplainLine] = []
        for ln in ex.get("lines", [])[:4]:
            text = _STUDENT_ID.sub(r"\1XXXX", str(ln.get("text", "")).strip())[:220]
            if not text:
                continue
            sids = [s for s in ln.get("source_ids", []) if s in allowed_by_item[key]]
            chunk_txt = " ".join(chunk_text_by_id.get(s, "") for s in sids)
            # 새 수치 생성 가드: 숫자는 인용 chunk 원문에 있어야 함. 진단 결과의 숫자(취득
            # 30학점 등)는 '진단을 가리키는 줄'에만 허용 — 안 그러면 취득학점을 최저요건으로
            # 바꿔 말하는 규정 오염이 통과한다(신규코드 검증 라운드 codex).
            diag_ref = any(w in text for w in ("진단", "현재", "보고서", "이수 현황", "부족", "귀하"))
            allowed_nums = chunk_txt + (" " + ctx_by_key[key] if diag_ref else "")
            grounded = bool(sids) and all(n in allowed_nums for n in re.findall(r"\d{2,}", text))
            if not grounded:
                text += " ※ 요람 원문에서 직접 확인되지 않음 — 학과사무실 확인 권장"
            lines.append(ExplainLine(text=text, source_ids=sids, grounded=grounded))
        if lines:
            sections.append(ExplainSection(key=key, title=title_by_key[key], lines=lines))
    return sections


# ---------- 캐시 (동일 입력 = 동일 해설 · 데모 지연 0) ----------
def _cache_key(model: str, items: list[dict], ctx: StudentContext) -> str:
    # 학과·입학연도 포함 — 같은 갭 구성의 다른 학생/연도가 같은 키로 충돌하지 않게
    payload = {"m": model, "i": items, "p": ctx.program_id, "y": ctx.admission_year}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()[:24]


def _cache_get(key: str):
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8")).get(key)
    except Exception:
        return None


def _cache_put(key: str, value: dict) -> None:
    try:
        data = {}
        if CACHE_PATH.exists():
            try:
                data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}                              # 손상 파일 자가복구(새로 시작)
        data[key] = value
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")           # 원자적 쓰기 — torn write 방지
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass  # 캐시 실패는 침묵 — 해설 생성 자체를 막지 않음


def _get_client():
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None
    try:
        from openai import OpenAI
        return OpenAI(timeout=20)
    except Exception:
        return None


def skip_explain_trace(summary: str, branch: str, status: str = "skip") -> list[NodeTraceEvent]:
    """해설 생략 시 placeholder 2노드 — 없으면 그래프에서 리포트 노드가 고립됨.
    run_explain 내부와 pipeline.run_audit(skip_explain=True)가 공용(모듈 레벨로 추출)."""
    return [
        NodeTraceEvent(node="요람 RAG 해설", kind="llm", status=status,
                       summary=summary, branch_taken=branch),
        NodeTraceEvent(node="해설 검증", kind="validator", status="skip",
                       summary="해설 없음 — 검증 생략", branch_taken="생략"),
    ]


# ---------- 오케스트레이션 ----------
def run_explain(audit: AuditResult, profile: RequirementProfile, ctx: StudentContext,
                client=None) -> tuple[list[ExplainSection], list[Source], list[NodeTraceEvent], str | None]:
    """반환: (sections, Y-sources, trace 2노드, fallback_reason)."""
    items = select_explain_items(audit, profile, ctx)
    model = os.getenv("OPENAI_GRADUATION_MODEL", "gpt-4o-mini")

    # skip/fail에도 '해설 검증' placeholder를 방출 — 없으면 그래프에서 리포트 노드가
    # lit 엣지 없이 고립됨(신규코드 검증 라운드: 키 없는 환경 재현)
    _skip_trace = skip_explain_trace

    if not items:
        return [], [], _skip_trace("부족 항목 없음 — 해설 생략", "해설 불필요"), None

    ck = _cache_key(model, items, ctx)
    cached = _cache_get(ck)
    if cached:
        sections = [ExplainSection.model_validate(s) for s in cached["sections"]]
        sources = [Source.model_validate(s) for s in cached["sources"]]
        for s_ in sources:                            # 구 캐시(url 부재) 호환 — 원문 링크 보강
            if s_.source_type == "yoram_rag" and not s_.url:
                s_.url = _yoram_url_2025(s_.page)
        # 캐시 경로도 라이브와 동일하게 미확인 줄을 반영 — '통과' 고정 표기는
        # 화면의 '※ 요람 원문에서 직접 확인되지 않음' 줄과 모순(데모 시나리오 검증 라운드)
        ungrounded = sum(1 for sec in sections for ln in sec.lines if not ln.grounded)
        trace = [
            NodeTraceEvent(node="요람 RAG 해설", kind="llm",
                           summary=f"{len(sections)}개 항목 해설 (캐시 — 동일 입력 동일 결과)",
                           branch_taken=f"{len(sections)}개 항목 해설"),
            NodeTraceEvent(node="해설 검증", kind="validator",
                           status="ok" if ungrounded == 0 else "warn",
                           summary=("인용 해소·수치 정합·마스킹 통과" if ungrounded == 0
                                    else f"근거 미확인 {ungrounded}줄 표시"),
                           branch_taken=("통과" if ungrounded == 0 else "일부 미확인 표시")),
        ]
        return sections, sources, trace, None

    client = client or _get_client()
    if client is None:
        return [], [], _skip_trace("LLM 미설정 — 결정론 진단·G 근거는 유효",
                                   "해설 생략(키 없음)"), "LLM 미설정(OPENAI_API_KEY 없음)"

    try:
        chunks_by_item: dict[str, list[dict]] = {}
        id_by_chunk: dict[int, str] = {}
        n = 0
        for it in items:
            chunks = retrieve_yoram(it["query"], client)
            chunks_by_item[it["key"]] = chunks
            for c in chunks:
                n += 1
                id_by_chunk[id(c)] = f"Y{n}"
        raw = _call_llm(client, model, items, chunks_by_item, id_by_chunk)
        sections = validate_explanations(raw, items, chunks_by_item, id_by_chunk)
        # 실제 인용된 chunk만 근거 목록에 노출
        used = {sid for sec in sections for ln in sec.lines for sid in ln.source_ids}
        sources = []
        for it in items:
            for c in chunks_by_item[it["key"]]:
                cid = id_by_chunk[id(c)]
                if cid in used:
                    sources.append(Source(id=cid, doc="2025 국민대학교 요람",
                                          page=c.get("page"), source_type="yoram_rag",
                                          ref=c.get("section"), url=_yoram_url_2025(c.get("page"))))
        ungrounded = sum(1 for sec in sections for ln in sec.lines if not ln.grounded)
        trace = [
            NodeTraceEvent(node="요람 RAG 해설", kind="llm",
                           summary=f"{len(items)}개 항목 · 요람 chunk {n}건 검색 · {model}",
                           branch_taken=f"{len(sections)}개 항목 해설"),
            NodeTraceEvent(node="해설 검증", kind="validator",
                           status="ok" if ungrounded == 0 else "warn",
                           summary=("인용 해소·수치 정합·마스킹 통과" if ungrounded == 0
                                    else f"근거 미확인 {ungrounded}줄 표시"),
                           branch_taken=("통과" if ungrounded == 0 else "일부 미확인 표시")),
        ]
        _cache_put(ck, {"sections": [s.model_dump() for s in sections],
                        "sources": [s.model_dump() for s in sources]})
        return sections, sources, trace, None
    except Exception as exc:
        return [], [], _skip_trace(f"해설 생성 불가({type(exc).__name__}) — 결정론 진단·G 근거는 유효",
                                   "해설 실패(degrade)", status="fail"), f"해설 생성 불가: {type(exc).__name__}"
