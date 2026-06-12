"""
데이터 로드 및 통계 계산.
  - load_formula_data(): 처방 CSV → formula_dict
  - build_stats(): 성분별 통계
  - load_product_data(): 마케팅 키워드 DB
  - load_external_data(): 타사 제품 CSV → external_db
"""

from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from formulator.config import BASE_ROLES, _FUNC_TO_ROLE, _KNOWN_BASE
from formulator.utils import _safe_literal_list, console


# 처방 CSV를 읽어 DataFrame과 bulk_code 키의 formula_dict를 반환
def load_formula_data(csv_path: str) -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(csv_path, engine="python")
    required = {"bulk_code", "bulk_name", "ingredient_name", "ingredient_function", "content"}
    if missing := required - set(df.columns):
        raise ValueError(f"필수 컬럼 누락: {missing}")

    df["ingredient_name"]     = df["ingredient_name"].str.strip()
    df["ingredient_function"] = (
        df["ingredient_function"].str.strip().str.replace(r"\s+", " ", regex=True)
    )
    df["content"] = pd.to_numeric(df["content"], errors="coerce")
    df = df.dropna(subset=["content"])

    formula_dict: dict = {}
    for bulk_code, grp in df.groupby("bulk_code"):
        if grp["content"].sum() <= 0:
            continue
        ingredients: dict[str, float] = {}
        structural_roles: dict[str, str] = {}
        for _, row in grp.iterrows():
            name = row["ingredient_name"]
            ingredients[name] = round(float(row["content"]), 6)
            role = _FUNC_TO_ROLE.get(row["ingredient_function"])
            if role:
                structural_roles[name] = role
            elif name in _KNOWN_BASE:
                structural_roles[name] = "base"

        formula_dict[bulk_code] = {
            "name":             grp["bulk_name"].iloc[0],
            "ingredients":      ingredients,
            "structural_roles": structural_roles,
        }

    console.print(f"[green]✓ 처방 데이터: {len(formula_dict)}건 로드[/green]")
    return df, formula_dict


# formula_dict 전체를 순회해 성분별 빈도·함량 통계(min/max/median 등)를 계산
def build_stats(formula_dict: dict) -> dict:
    idata: dict[str, list[float]] = defaultdict(list)
    iroles: dict[str, str] = {}

    for fd in formula_dict.values():
        for name, pct in fd["ingredients"].items():
            idata[name].append(pct)
            role = fd["structural_roles"].get(name)
            if role and name not in iroles:
                iroles[name] = role

    N = len(formula_dict)
    stats: dict[str, dict] = {}
    for name, vals in idata.items():
        arr = np.array(vals, dtype=float)
        stats[name] = {
            "structural_role": iroles.get(name, "active_or_unknown"),
            "frequency":       round(len(vals) / N, 3),
            "count":           len(vals),
            "min":             round(float(arr.min()),              4),
            "max":             round(float(arr.max()),              4),
            "median":          round(float(np.median(arr)),         4),
            "mean":            round(float(arr.mean()),             4),
            "std":             round(float(arr.std()),              4),
            "p25":             round(float(np.percentile(arr, 25)), 4),
            "p75":             round(float(np.percentile(arr, 75)), 4),
        }
    return {"ingredient_stats": stats, "total_formulas": N}


# 마케팅 키워드 CSV를 로드해 키워드별 연관 성분·처방 코드를 인덱싱한 keyword_db 반환
def load_product_data(product_csv: str, formula_dict: dict) -> dict:
    prod = pd.read_csv(product_csv, engine="python")
    overlap = set(prod["bulk_code"]) & set(formula_dict.keys())
    console.print(f"[green]✓ 마케팅 데이터: {len(prod)}건 / 처방 연결: {len(overlap)}건[/green]")

    keyword_db: dict = defaultdict(
        lambda: {"ingredients": Counter(), "aspects": set(), "formula_codes": []}
    )

    for _, row in (
        prod[prod["bulk_code"].isin(list(overlap))]
        .drop_duplicates(subset="bulk_code")
        .iterrows()
    ):
        code = row["bulk_code"]
        kws  = _safe_literal_list(row.get("marketing_keywords_list", []))
        asps = _safe_literal_list(row.get("aspect_list", []))

        fd = formula_dict[code]
        active_ings = [
            name
            for name, _ in sorted(fd["ingredients"].items(), key=lambda x: -x[1])
            if fd["structural_roles"].get(name) not in BASE_ROLES and name not in _KNOWN_BASE
        ][:10]

        for kw in set(kws) | set(asps):
            keyword_db[kw]["ingredients"].update(active_ings)
            keyword_db[kw]["aspects"].update(asps)
            keyword_db[kw]["formula_codes"].append(code)

    console.print(f"[green]✓ 키워드 {len(keyword_db)}종 인덱싱[/green]")
    return dict(keyword_db)


# 타사 제품 CSV를 로드해 {title: [성분명, ...]} 형태의 external_db 반환
def load_external_data(csv_path: str) -> dict[str, list[str]]:
    try:
        df = pd.read_csv(csv_path, engine="python")
    except FileNotFoundError:
        console.print(f"[yellow]⚠ 타사 데이터 파일 없음: {csv_path}[/yellow]")
        return {}

    required = {"title", "representation_ingredients"}
    if missing := required - set(df.columns):
        console.print(f"[yellow]⚠ 타사 데이터 필수 컬럼 누락: {missing}[/yellow]")
        return {}

    external_db: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        title = str(row["title"]).strip()
        ings  = [s.strip() for s in str(row["representation_ingredients"]).split("|") if s.strip()]
        if title and ings:
            external_db[title] = ings

    console.print(f"[green]✓ 타사 제품 데이터: {len(external_db)}건 로드[/green]")
    return external_db
