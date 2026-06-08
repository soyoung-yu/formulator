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
            "ingredients":         list(formula_dict[c]["ingredients"].keys()),
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
            "ingredients":         list(formula_dict[c]["ingredients"].keys()),
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
    top_base: int = 15,
    top_active: int = 15,
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

    # base / active 성분 분리
    priority = user_ing_names
    base_ings:   list[dict] = []
    active_ings: list[dict] = []

    for name, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"]):
        entry = {"name": name, **s}
        if s["structural_role"] in BASE_ROLES or name in _KNOWN_BASE:
            if len(base_ings) < top_base:
                base_ings.append(entry)
        else:
            if name in priority:
                active_ings.insert(0, entry)
            elif len(active_ings) < top_active:
                active_ings.append(entry)

    if len(active_ings) < top_active:
        exist = {a["name"] for a in active_ings}
        extras = [
            {"name": n, **s}
            for n, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"])
            if s["structural_role"] not in BASE_ROLES
            and n not in _KNOWN_BASE
            and n not in exist
        ]
        active_ings += extras[: top_active - len(active_ings)]

    return {
        "query_info":          query_info,
        "ingredient_map":      ingredient_map,
        "user_ing_names":      user_ing_names,
        "matched_keywords":    matched_keywords,
        "similar_formulas":    similar,
        "base_ings":           base_ings,
        "active_ings":         active_ings[:top_active],
        "allowed_ingredients": sorted(ist.keys()),
    }
