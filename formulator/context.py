"""
컨텍스트 구성.
  - _pick_similar(): 유사 처방 탐색
  - build_context(): 파이프라인 스텝 6 — Python only, LLM 없음
"""

from formulator.config import BASE_ROLES, _KNOWN_BASE


def _pick_similar(
    formula_dict: dict,
    query_ing_names: set[str],
    formula_kw_map: dict[str, list[str]] | None = None,
    n: int = 3,
    exclude_codes: set[str] | None = None,
) -> dict:
    """유사 처방을 두 그룹으로 반환한다.

    group_a: 질의 마케팅 키워드에 등장하는 처방 (동시 매칭 키워드 수 내림차순, 최대 n건)
    group_b: 질의 성분이 포함된 처방 중 group_a에 없는 것 (매칭 성분 수 내림차순, 최대 n건)
    """
    formula_kw_map = formula_kw_map or {}
    exclude_codes  = exclude_codes  or set()

    # ── 그룹 A: 키워드 매칭 처방 ─────────────────────────────────
    kw_candidates: list[tuple] = []
    for code, kws in formula_kw_map.items():
        if code in exclude_codes or code not in formula_dict:
            continue
        matched_kws  = list(dict.fromkeys(kws))
        matched_ings = [nm for nm in formula_dict[code]["ingredients"] if nm in query_ing_names]
        kw_candidates.append((len(matched_kws), code, matched_kws, matched_ings))
    kw_candidates.sort(key=lambda x: -x[0])

    group_a_codes: set[str] = set()
    group_a: list[dict] = []
    for _, c, mkws, mings in kw_candidates[:n]:
        group_a_codes.add(c)
        group_a.append({
            "bulk_code":           c,
            "name":                formula_dict[c]["name"],
            "ingredients":         [{"name": n, "content": v} for n, v in formula_dict[c]["ingredients"].items()],
            "matched_keywords":    mkws,
            "matched_ingredients": mings,
        })

    # ── 그룹 B: 질의 성분 매칭 처방 (그룹 A 제외) ────────────────
    ing_candidates: list[tuple] = []
    for code, fd in formula_dict.items():
        if code in exclude_codes or code in group_a_codes:
            continue
        matched_ings = [nm for nm in fd["ingredients"] if nm in query_ing_names]
        if not matched_ings:
            continue
        ing_candidates.append((len(matched_ings), code, matched_ings))
    ing_candidates.sort(key=lambda x: -x[0])

    group_b: list[dict] = [
        {
            "bulk_code":           c,
            "name":                formula_dict[c]["name"],
            "ingredients":         [{"name": n, "content": v} for n, v in formula_dict[c]["ingredients"].items()],
            "matched_keywords":    [],
            "matched_ingredients": mings,
        }
        for _, c, mings in ing_candidates[:n]
    ]

    return {"group_a": group_a, "group_b": group_b}


def build_context(
    stats: dict,
    keyword_db: dict,
    formula_dict: dict,
    query_info: dict,
    ingredient_map: dict[str, list[str] | None],
    target_product: dict | None = None,
    top_base: int = 15,
    top_active: int = 20,
) -> dict:
    """
    LLM 없이 Python만으로 컨텍스트를 구성한다.
    마케팅 키워드는 query_info["marketing_hints"]를 소비 (query 직접 스캔 없음).

    Returns dict with keys:
        query_info, ingredient_map, user_ing_names,
        matched_keywords, similar_formulas,
        base_ings, active_ings, allowed_ingredients
    """
    ist: dict = stats["ingredient_stats"]

    # 매핑된 DB 성분명
    user_ing_names: set[str] = {
        name
        for mapped in ingredient_map.values()
        if mapped
        for name in mapped
    }

    # 마케팅 키워드 — extract_query_info가 추출한 결과를 keyword_db에서 조회
    matched_keywords: list[tuple[str, dict]] = [
        (kw, keyword_db[kw])
        for kw in query_info.get("marketing_hints", [])
        if kw in keyword_db
    ]

    formula_kw_map: dict[str, list[str]] = {}   # code → [매칭된 키워드, ...]
    for kw, kdata in matched_keywords:
        for code in kdata["formula_codes"]:
            formula_kw_map.setdefault(code, []).append(kw)

    # 유사 처방 (그룹 A: 키워드 매칭 / 그룹 B: 질의 성분 매칭)
    similar = _pick_similar(
        formula_dict,
        user_ing_names,
        formula_kw_map=formula_kw_map,
        n=3,
    )

    # ── 맥락 기반 허용 성분 목록 구성 ───────────────────────────────────
    # 우선순위: 질의 성분 > 타겟 처방 성분 > 유사 처방 성분 > base 고빈도 > active 고빈도
    allowed: set[str] = set()

    # 1) 질의 성분 (사용자 지정 — 반드시 포함)
    allowed.update(user_ing_names)

    # 2) 타겟 처방 성분 — known_set(ist) 기준 exact match만 포함 (타사 성분명 불일치 필터링)
    if target_product:
        for ing in target_product.get("ingredients", []):
            name = ing.get("name", "")
            if name in ist:
                allowed.add(name)

    # 3) 유사 처방 출현 성분
    for f in similar.get("group_a", []) + similar.get("group_b", []):
        for ing in f.get("ingredients", []):
            name = ing["name"]
            if name in ist:
                allowed.add(name)

    # ── base 성분 (빈도 내림차순 top_base) ──────────────────────────────
    base_ings: list[dict] = []
    for name, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"]):
        if s["structural_role"] in BASE_ROLES or name in _KNOWN_BASE:
            if len(base_ings) < top_base:
                base_ings.append({"name": name, **s})
                allowed.add(name)

    # ── active 성분 4그룹 — 중복 없이 우선순위 순서로 배정 ───────────────
    def _is_active(name: str, s: dict) -> bool:
        return s["structural_role"] not in BASE_ROLES and name not in _KNOWN_BASE

    assigned: set[str] = set()

    # 1) 질의 지정 성분
    query_active_ings: list[dict] = []
    for name in user_ing_names:
        if name in ist and _is_active(name, ist[name]):
            query_active_ings.append({"name": name, **ist[name]})
            assigned.add(name)
            allowed.add(name)

    # 2) 타겟 처방 성분 (known_set 매칭, 질의 성분 제외)
    target_active_ings: list[dict] = []
    if target_product:
        for ing in target_product.get("ingredients", []):
            name = ing.get("name", "")
            if name in ist and _is_active(name, ist[name]) and name not in assigned:
                target_active_ings.append({"name": name, **ist[name]})
                assigned.add(name)
                allowed.add(name)

    # 3) 유사 처방 출현 성분 (위 그룹 제외)
    similar_active_ings: list[dict] = []
    seen_similar: set[str] = set()
    for f in similar.get("group_a", []) + similar.get("group_b", []):
        for ing in f.get("ingredients", []):
            name = ing["name"]
            if name in ist and _is_active(name, ist[name]) and name not in assigned and name not in seen_similar:
                similar_active_ings.append({"name": name, **ist[name]})
                seen_similar.add(name)
                assigned.add(name)
                allowed.add(name)

    # 4) 범용 활성 성분 — 고빈도 top_active (위 그룹 제외)
    general_active_ings: list[dict] = []
    for name, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"]):
        if len(general_active_ings) >= top_active:
            break
        if _is_active(name, s) and name not in assigned:
            general_active_ings.append({"name": name, **s})
            allowed.add(name)

    def _sort_by_freq(lst: list[dict]) -> list[dict]:
        return sorted(lst, key=lambda x: (-x["frequency"], x["name"]))

    # 통계 섹션에 등장하지 않은 나머지 DB 전체 성분 (이름만)
    remaining_ingredients = sorted(set(ist.keys()) - allowed)

    return {
        "query_info":              query_info,
        "ingredient_map":          ingredient_map,
        "user_ing_names":          user_ing_names,
        "matched_keywords":        matched_keywords,
        "similar_formulas":        similar,
        "base_ings":               base_ings,
        "query_active_ings":       _sort_by_freq(query_active_ings),
        "target_active_ings":      _sort_by_freq(target_active_ings),
        "similar_active_ings":     _sort_by_freq(similar_active_ings),
        "general_active_ings":     general_active_ings,
        "allowed_ingredients":     sorted(allowed),
        "remaining_ingredients":   remaining_ingredients,
    }
