"""
후처리 및 검증.
  - validate_and_fix(): 화이트리스트 exact match + LLM 대체 탐색 + 정제수 합계 보정
"""

from typing import Any

from formulator.utils import _invoke_bedrock_json, _norm_name, console


# DB 미매칭 성분에 대해 LLM으로 대체 성분 3개를 제안받고 known_set에서 첫 번째 매칭을 반환
def _suggest_substitutes(
    name: str,
    role: str,
    known_set: set[str],
    bedrock_client: Any,
    model_id: str,
) -> str | None:
    prompt = (
        f"화장품 처방에서 '{name}'({role}) 성분을 대체할 수 있는 성분 3개를 제시하세요.\n\n"
        "다음 항목을 모두 검토하여 가장 유사한 성분을 추천하세요:\n"
        "- 주요 효능 (보습·미백·주름개선 등)\n"
        "- 역할 (pH 조절제·점증제·유화제 등)\n"
        "- 점도 영향성\n"
        "- pH 영향성\n"
        "- 규제 (사용 농도 제한, 금지 여부 등)\n\n"
        "JSON만 반환 (화장품 원료 공식 명칭 사용):\n"
        "{\"substitutes\": [\"성분명1\", \"성분명2\", \"성분명3\"]}"
    )
    try:
        result, _ = _invoke_bedrock_json(bedrock_client, model_id, prompt, max_tokens=256)
        candidates = result.get("substitutes", []) if isinstance(result, dict) else []
        norm_known = {_norm_name(k): k for k in known_set}
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            if candidate in known_set:
                return candidate
            normed = _norm_name(candidate)
            if normed in norm_known:
                return norm_known[normed]
    except Exception:
        pass
    return None


def validate_and_fix(
    formula_data: dict,
    stats: dict,
    known_set: set[str] | None = None,
    user_constraints: dict[str, float] | None = None,
    bedrock_client: Any = None,
    model_id: str | None = None,
) -> dict:
    """
    1) 화이트리스트 검증: 정규화 exact match → 불일치 시 조용히 제거
    2) 합계 100% 보정: 정제수 함량으로 조정
    3) 사용자 지정 함량 보호: user_constraints 성분은 보정하지 않음
    """
    user_constraints = user_constraints or {}
    norm_user = {_norm_name(k): v for k, v in user_constraints.items()}

    for formula in formula_data.get("formulas", []):
        ings: list[dict] = formula.get("ingredients", [])

        # ── 1) 화이트리스트 검증 ─────────────────────────────────────────
        if known_set:
            checked: list[dict] = []
            for ing in ings:
                name = ing.get("name", "")
                if name in known_set:
                    checked.append(ing)
                    continue
                matched = next(
                    (k for k in known_set if _norm_name(k) == _norm_name(name)),
                    None,
                )
                if matched:
                    ing["name"] = matched
                    checked.append(ing)
                elif bedrock_client and model_id:
                    # 매핑 불가 → LLM 대체 성분 탐색
                    console.print(f"[yellow]  ⚠ '{name}' — DB에 없음. 대체 성분 탐색 중...[/yellow]")
                    substitute = _suggest_substitutes(
                        name, ing.get("role", ""), known_set, bedrock_client, model_id
                    )
                    if substitute:
                        console.print(f"[yellow]    → '{substitute}'로 교체 (대체 성분 매칭)[/yellow]")
                        ing["name"] = substitute
                        checked.append(ing)
                    else:
                        console.print(f"[yellow]  ⚠ '{name}' — 대체 성분 3개 모두 미매칭 → 제거[/yellow]")
                # bedrock_client 없으면 기존처럼 조용히 제거
            formula["ingredients"] = checked

        ings = formula.get("ingredients", [])

        # ── 2) 합계 100% 정제수 보충 ─────────────────────────────────────
        total = sum(i.get("content", 0) for i in ings)
        if abs(total - 100.0) > 0.05:
            water = next(
                (
                    i for i in ings
                    if "정제수" in i.get("name", "")
                    or "water" in i.get("name", "").lower()
                ),
                None,
            )
            if water:
                w_norm = _norm_name(water.get("name", ""))
                if w_norm not in norm_user:
                    new_w = round(water["content"] + (100.0 - total), 6)
                    if new_w >= 0:
                        water["content"] = new_w
                    else:
                        console.print(
                            f"[red]  ✗ 정제수 보정 불가 (음수: {new_w:.4f}%) — "
                            f"처방 함량 재검토 필요[/red]"
                        )
            else:
                console.print("[yellow]  ⚠ 정제수 없음 — 합계 보정 불가[/yellow]")

    return formula_data
