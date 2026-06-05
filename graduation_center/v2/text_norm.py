"""교과목명 정규화 — 카탈로그 빌드와 런타임 매칭이 동일 규칙을 쓰도록 공유.

요람(이름만)·현황 xlsx(코드+이름)·ON국민 수강내역(코드+이름)을 이름으로 조인할 때,
로마숫자/공백/괄호/전각 차이를 흡수해 동일 키로 만든다. (매칭 1순위는 교과목코드 7자리이고
이 정규화는 보조·요람 조인용.)
"""
from __future__ import annotations

import re
import unicodedata

# 로마숫자(전각/조합) → 아라비아
_ROMAN = {
    "Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3", "Ⅳ": "4", "Ⅴ": "5",
    "Ⅵ": "6", "Ⅶ": "7", "Ⅷ": "8", "Ⅸ": "9", "Ⅹ": "10",
    "ⅰ": "1", "ⅱ": "2", "ⅲ": "3", "ⅳ": "4", "ⅴ": "5",
}

# 후행 로마 알파벳(…I, …II, …III) → 숫자. 단어 끝에서만.
_TRAILING_ROMAN = [
    (re.compile(r"(III)$", re.IGNORECASE), "3"),
    (re.compile(r"(II)$", re.IGNORECASE), "2"),
    (re.compile(r"(I)$", re.IGNORECASE), "1"),
]


def normalize_name(value: str | None) -> str:
    """교과목명을 비교용 정규화 키로 변환.

    NFKC 정규화 → 로마숫자 치환 → 영문 소문자 → 공백·구두점 제거.
    예: 'AI빅데이터프로그래밍Ⅰ' / 'AI빅데이터프로그래밍 I' → 'ai빅데이터프로그래밍1'
    """
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", str(value)).strip()
    for r, a in _ROMAN.items():
        s = s.replace(r, a)
    # 후행 로마 알파벳 치환은 NFKC 후, 공백 제거 전에 단어 끝 기준으로
    for pat, rep in _TRAILING_ROMAN:
        s = pat.sub(rep, s)
    s = s.lower()
    # 공백·구두점·괄호 제거 (한글/영숫자만 남김)
    s = re.sub(r"[^0-9a-z가-힣]", "", s)
    return s


def normalize_code(value: str | int | None) -> str:
    """교과목코드를 7자리 문자열로 정규화 (leading-zero·문자 보존).

    엑셀이 '0118810'을 숫자 118810으로 읽는 경우를 7자리로 복원.
    문자가 섞인 코드(예: '003290F')는 그대로 둔다.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    # 숫자로만 이뤄졌고 7자리 미만이면 zero-pad
    if s.isdigit() and len(s) < 7:
        s = s.zfill(7)
    return s
