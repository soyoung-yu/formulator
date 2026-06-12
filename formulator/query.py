"""
질의 분석 및 성분명 매핑.
  - extract_query_info(): DB set + ALIAS_HINTS 기반 질의 분석 (LLM 없음), ingredient_map 직접 반환
  - extract_amount_constraints(): LLM으로 질의 내 함량 정보를 성분에 연결
  - find_target_product(): LLM으로 질의 내 제품명 추출 후 자사→타사 순서로 탐색
"""

import re
from typing import Any

from formulator.config import ALIAS_HINTS, FORMULATION_HINT_KEYWORDS, MARKETING_HINT_KEYWORDS
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
          "ingredient_map":   {"나이아신아마이드": ["나이아신아마이드"], "시카": ["병풀추출물"]},
          "formulation_hints":["에센스"],
          "marketing_hints":  ["수분감", "보습감"],
        }
    """
    ingredient_map: dict[str, list[str]] = {}

    # 1) DB 성분명 set 직접 매칭 (2자 이상) — DB명이 곧 키이자 값
    if known_set:
        for name in known_set:
            if len(name) >= 2 and name in query:
                ingredient_map[name] = [name]

    # 2) ALIAS_HINTS 키 매칭 → values(DB 성분명)로 바로 확장
    for alias, db_names in ALIAS_HINTS.items():
        if alias.lower() in query.lower():
            ingredient_map[alias] = db_names

    formulation_hints = [kw for kw in FORMULATION_HINT_KEYWORDS if kw in query]
    kw_source         = marketing_keywords if marketing_keywords is not None else set(MARKETING_HINT_KEYWORDS)
    marketing_hints   = [kw for kw in kw_source if kw in query]

    return {
        "ingredient_map":    ingredient_map,
        "formulation_hints": formulation_hints,
        "marketing_hints":   marketing_hints,
    }


# 질의에서 언급된 제품명을 LLM으로 추출한 뒤 자사→타사 순서로 탐색해 타겟 처방 정보를 반환
def find_target_product(
    query: str,
    formula_dict: dict,
    external_db: dict[str, list[str]],
    bedrock_client: Any,
    model_id: str,
) -> dict | None:
    # LLM으로 질의에서 제품명 언급 추출
    prompt = (
        "아래 화장품 처방 요청 질의에서 참고 또는 유사 제품으로 언급된 제품명을 추출하세요.\n"
        "제품명이 없으면 null을 반환하세요.\n\n"
        f"[질의]\n{query}\n\n"
        "JSON만 반환:\n"
        "{\"product_name\": \"제품명\" 또는 null}"
    )
    try:
        result, _ = _invoke_bedrock_json(bedrock_client, model_id, prompt, max_tokens=128)
        product_name = result.get("product_name") if isinstance(result, dict) else None
    except Exception:
        return None

    if not product_name:
        return None

    norm_query = _norm_name(product_name)

    # 1) 자사 처방에서 탐색 (formula_dict의 name 필드)
    for code, fd in formula_dict.items():
        if _norm_name(fd.get("name", "")) == norm_query or product_name in fd.get("name", ""):
            ings = fd["ingredients"]
            return {
                "source":       "자사",
                "product_name": fd["name"],
                "code":         code,
                "ingredients":  [
                    {"name": n, "content": c}
                    for n, c in sorted(ings.items(), key=lambda x: -x[1])
                ],
            }

    # 2) 타사 데이터에서 탐색 (external_db의 title)
    for title, ing_list in external_db.items():
        if _norm_name(title) == norm_query or product_name in title:
            return {
                "source":       "타사",
                "product_name": title,
                "code":         None,
                "ingredients":  [{"name": n, "content": None} for n in ing_list],
            }

    return None


def extract_amount_constraints(
    query: str,
    ingredient_names: list[str],
    bedrock_client: Any,
    model_id: str,
) -> dict[str, float]:
    """
    질의에 숫자% 표현이 있을 때, LLM으로 어떤 성분에 어떤 함량이 지정됐는지 연결한다.
    함량 언급이 없으면 LLM 호출 없이 빈 dict 반환.

    Returns: {ingredient_name: amount_float}
    """
    amounts = re.findall(r'\d+(?:\.\d+)?(?=\s*%)', query)
    if not amounts:
        return {}
    if not ingredient_names or not bedrock_client or not model_id:
        return {}

    ings_str     = "\n".join(f"- {n}" for n in ingredient_names)
    amounts_str  = "\n".join(f"- {a}%" for a in amounts)
    prompt = (
        "아래 질의, 추출된 성분, 추출된 함량을 함께 보고 각 성분에 해당하는 함량을 연결하세요.\n\n"
        f"[질의]\n{query}\n\n"
        f"[추출된 성분]\n{ings_str}\n\n"
        f"[추출된 함량]\n{amounts_str}\n\n"
        "규칙:\n"
        "- 함량이 연결되지 않는 성분은 결과에서 제외하세요.\n"
        "- 성분명은 위 목록의 표기 그대로 사용하세요.\n\n"
        "JSON만 반환:\n"
        "{\"성분명\": 숫자, ...}"
    )

    try:
        result, _ = _invoke_bedrock_json(bedrock_client, model_id, prompt, max_tokens=512)
        valid = {}
        for name, val in result.items():
            if name in ingredient_names and isinstance(val, (int, float)) and val > 0:
                valid[name] = float(val)
        return valid
    except Exception as e:
        console.print(f"[yellow]함량 추출 Claude 호출 실패: {e}[/yellow]")
        return {}


