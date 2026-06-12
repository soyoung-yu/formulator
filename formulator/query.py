"""
질의 분석 및 성분명 매핑.
  - extract_query_info(): DB set + ALIAS_HINTS 기반 질의 분석 (LLM 없음)
  - extract_query_supplements(): Python 추출 결과를 보완하는 단일 LLM 호출
  - search_target_product(): 제품명으로 자사→타사 DB 탐색 (LLM 없음)
"""

import re
from typing import Any

from formulator.config import ALIAS_HINTS, MARKETING_HINT_KEYWORDS
from formulator.utils import _invoke_bedrock_json, _norm_name, console


def extract_query_info(
    query: str,
    known_set: set[str] | None = None,
    marketing_keywords: set[str] | None = None,
) -> dict:
    """
    DB set + ALIAS_HINTS 기반 질의 분석. LLM 호출 없음.

    Args:
        known_set:           data.csv 전체 성분명 set. 전달 시 DB 직접 매칭.
                             None이면 성분 추출을 건너뜀 (하위 호환).
        marketing_keywords:  keyword_db.keys() 전달 시 해당 set으로 매칭.
                             None이면 config.MARKETING_HINT_KEYWORDS 폴백.

    Returns:
        {
          "ingredient_map":  {"나이아신아마이드": ["나이아신아마이드"], "시카": ["병풀추출물"]},
          "marketing_hints": ["수분감", "보습감"],
        }
    """
    ingredient_map: dict[str, list[str]] = {}

    # 1) DB 성분명 set 직접 매칭 (2자 이상) — DB명이 곧 키이자 값
    if known_set:
        for name in known_set:
            if len(name) >= 2 and name in query:
                ingredient_map[name] = [name]

    # 2) ALIAS_HINTS 키 매칭 → values(DB 성분명)로 바로 확장 (공백·대소문자 무시)
    query_norm = _norm_name(query)
    for alias, db_names in ALIAS_HINTS.items():
        if _norm_name(alias) in query_norm:
            ingredient_map[alias] = db_names

    kw_source       = marketing_keywords if marketing_keywords is not None else set(MARKETING_HINT_KEYWORDS)
    marketing_hints = [kw for kw in kw_source if kw in query]

    return {
        "ingredient_map":  ingredient_map,
        "marketing_hints": marketing_hints,
    }


# Python 추출 결과를 보완하는 단일 LLM 호출 — 타겟 제품명·추가 성분·추가 키워드·성분-함량 연결 동시 반환
def extract_query_supplements(
    query: str,
    ingredient_names: list[str],
    marketing_hints: list[str],
    bedrock_client: Any,
    model_id: str,
) -> dict:
    amounts = re.findall(r'\d+(?:\.\d+)?(?=\s*%)', query)

    ings_str    = ", ".join(ingredient_names) if ingredient_names else "없음"
    kws_str     = ", ".join(marketing_hints)  if marketing_hints  else "없음"
    amounts_str = ", ".join(f"{a}%" for a in amounts) if amounts else "없음"

    prompt = (
        "아래 화장품 처방 요청 질의와 Python 추출 결과를 보고, 누락된 항목을 보완하세요.\n\n"
        f"[질의]\n{query}\n\n"
        "[Python 추출 결과]\n"
        f"- 성분명: {ings_str}\n"
        f"- 마케팅·사용감 키워드: {kws_str}\n"
        f"- 함량값: {amounts_str}\n\n"
        "추출 규칙:\n"
        "1. target_product: 질의에서 참고/유사 제품으로 언급된 제품명. 없으면 null.\n"
        "2. additional_ingredients: Python이 놓친 추가 성분명. 없으면 [].\n"
        "3. additional_keywords: Python이 놓친 마케팅·사용감 키워드. 없으면 [].\n"
        "4. ingredient_amounts: 성분-함량 연결. 함량이 지정된 성분만 포함, 없으면 {}.\n"
        "   성분명은 Python 추출 결과의 표기 그대로 사용.\n\n"
        "JSON만 반환:\n"
        '{"target_product": "제품명" or null, '
        '"additional_ingredients": [...], '
        '"additional_keywords": [...], '
        '"ingredient_amounts": {"성분명": 숫자, ...}}'
    )

    try:
        result, _ = _invoke_bedrock_json(bedrock_client, model_id, prompt, max_tokens=512)
        if not isinstance(result, dict):
            return {}
        return result
    except Exception as e:
        console.print(f"[yellow]질의 보완 추출 실패: {e}[/yellow]")
        return {}


# 제품명으로 자사(formula_dict) → 타사(external_db) 순서로 탐색해 타겟 처방 정보를 반환
# 여러 매칭 시 날짜(first_in / base_time) 내림차순 → 가나다 순으로 최우선 항목 선택
def search_target_product(
    product_name: str,
    formula_dict: dict,
    external_db: dict[str, dict],
) -> dict | None:
    norm_query = _norm_name(product_name)

    # 1) 자사 처방 — 후보 수집 후 정렬
    inhouse_candidates = [
        (code, fd) for code, fd in formula_dict.items()
        if _norm_name(fd.get("name", "")) == norm_query or product_name in fd.get("name", "")
    ]
    if inhouse_candidates:
        inhouse_candidates.sort(key=lambda x: x[1].get("name", ""))
        inhouse_candidates.sort(key=lambda x: x[1].get("first_in", ""), reverse=True)
        code, fd = inhouse_candidates[0]
        return {
            "source":       "자사",
            "product_name": fd["name"],
            "code":         code,
            "ingredients":  [
                {"name": n, "content": c}
                for n, c in sorted(fd["ingredients"].items(), key=lambda x: -x[1])
            ],
        }

    # 2) 타사 데이터 — 후보 수집 후 정렬
    external_candidates = [
        (title, data) for title, data in external_db.items()
        if _norm_name(title) == norm_query or product_name in title
    ]
    if external_candidates:
        external_candidates.sort(key=lambda x: x[0])
        external_candidates.sort(key=lambda x: x[1].get("base_time", ""), reverse=True)
        title, data = external_candidates[0]
        return {
            "source":       "타사",
            "product_name": title,
            "code":         None,
            "ingredients":  [{"name": n, "content": None} for n in data["ingredients"]],
        }

    return None
