"""
후처리 및 검증.
  - validate_and_fix(): 화이트리스트 exact match + 정제수 합계 보정
"""

from formulator.utils import _norm_name, console


def validate_and_fix(
    formula_data: dict,
    stats: dict,
    known_set: set[str] | None = None,
    user_constraints: dict[str, float] | None = None,
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
                # 매핑 불가 → 제거 (조용히)
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
