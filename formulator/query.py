"""
질의 분석 및 성분명 매핑.
  - extract_query_info(): DB set + ALIAS_HINTS 기반 질의 분석 (LLM 없음)
  - extract_amount_constraints(): LLM으로 질의 내 함량 정보를 성분에 연결 (옵션 C)
  - map_ingredients(): ALIAS_HINTS → DB 성분명 확장
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
          "ingredients":      ["나이아신아마이드", "시카"],   # DB 성분명 or ALIAS key
          "constraints":      {"나이아신아마이드": None, "시카": None},  # 함량은 별도 추출
          "formulation_hints":["에센스"],
          "marketing_hints":  ["수분감", "보습감"],
        }
    """
    ingredients: list[str] = []
    constraints: dict[str, float | None] = {}
    seen: set[str] = set()

    # 중복 없이 성분명을 ingredients 리스트와 constraints dict에 추가
    def _add(name: str) -> None:
        key = _norm_name(name)
        if key in seen or len(key) < 2:
            return
        seen.add(key)
        ingredients.append(name)
        constraints[name] = None

    # 1) DB 성분명 set 직접 매칭 (2자 이상)
    if known_set:
        for name in known_set:
            if len(name) >= 2 and name in query:
                _add(name)

    # 2) ALIAS_HINTS 키 매칭 (관용명·약어)
    for alias in ALIAS_HINTS:
        if alias.lower() in query.lower():
            _add(alias)

    formulation_hints = [kw for kw in FORMULATION_HINT_KEYWORDS if kw in query]
    kw_source         = marketing_keywords if marketing_keywords is not None else set(MARKETING_HINT_KEYWORDS)
    marketing_hints   = [kw for kw in kw_source if kw in query]

    return {
        "ingredients":       ingredients,
        "constraints":       constraints,
        "formulation_hints": formulation_hints,
        "marketing_hints":   marketing_hints,
    }


def extract_amount_constraints(
    query: str,
    ingredient_names: list[str],
    bedrock_client: Any,
    model_id: str,
) -> dict[str, float]:
    """
    질의에 숫자% 표현이 있을 때, LLM으로 어떤 성분에 어떤 함량이 지정됐는지 연결한다.
    함량 언급이 없으면 LLM 호출 없이 빈 dict 반환.

    Returns: {db_ingredient_name: amount_float}
    """
    if not re.search(r'\d+(?:\.\d+)?\s*%', query):
        return {}
    if not ingredient_names or not bedrock_client or not model_id:
        return {}

    ings_str = "\n".join(f"- {n}" for n in ingredient_names)
    prompt = (
        "화장품 처방 요청 질의에서 각 성분에 지정된 함량(%)을 추출하세요.\n\n"
        f"[질의]\n{query}\n\n"
        f"[추출된 성분 목록]\n{ings_str}\n\n"
        "규칙:\n"
        "- 질의에서 함량이 명시된 성분만 포함하세요.\n"
        "- 함량이 없는 성분은 결과에서 제외하세요.\n"
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


def map_ingredients(
    terms: list[str],
    known_set: set[str],
) -> dict[str, list[str] | None]:
    """
    ALIAS_HINTS 키 → DB 성분명 확장.
    DB set 직접 매칭으로 들어온 성분명은 그대로 통과.

    Returns: {term: [db_name, ...] or None}
    """
    result: dict[str, list[str] | None] = {}

    for term in terms:
        if term in known_set:
            # DB set에서 직접 찾힌 성분명 — 그대로 사용
            result[term] = [term]
        elif term in ALIAS_HINTS:
            # ALIAS_HINTS 키 — value(DB 성분명 목록) 반환
            db_names = [v for v in ALIAS_HINTS[term] if v in known_set]
            result[term] = db_names if db_names else None
        else:
            result[term] = None

    return result
