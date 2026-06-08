"""
AI 기반 화장품 처방 자동 생성 시스템 — PoC v1.0
================================================
v1.0 변경 내용: v0.8 대비

  [명령 1] LLM 호출 단일화
     - 기존 5회 LLM 호출(슬롯 추출, 제형 분류, 물성 검토, 대체 성분 조회, 처방 생성)을
       처방 생성 1회(+ 성분명 미매핑 시 소호출 1회 추가)로 축소
     - Python은 데이터 준비와 합계 보정만 담당

  [명령 2] 하드코딩 룰 제거
     - FEEL_CONFLICT_MAP, FORMULATION_STRUCTURAL_MAP 완전 삭제
     - 제형 판단, 성분 선택, 함량 결정, 사용감 충돌 판단을 LLM에 위임

  [명령 3] 질의 분석 정규식 전환
     - extract_query_contexts() LLM 호출 제거
     - 성분명·함량 수치·제형·마케팅 키워드를 정규식으로만 추출하는 extract_query_info()로 대체

  [명령 4] 성분명 매핑 단순화
     - sentence-transformers / faiss-cpu 의존성 제거
     - 정규화 exact match 1단계 + Claude 소호출 2단계로 대체

  [명령 5] 인터랙티브 입력 제거
     - input() 기반 흐름 완전 제거
     - 타겟 제품 탐색(find_target_product) 제외;
       query 텍스트로 프롬프트에 자연어로 전달

  [명령 6] 후처리 조용히 보정
     - 통계 상한 초과 경고 출력 제거
     - validate_and_fix: 화이트리스트 검증 + 정제수 합계 보정만 수행

전체 흐름:
    query
      → extract_query_info()   [정규식, LLM 없음]
      → map_ingredients()      [exact match 우선, 실패 시 Claude 소호출 1회]
      → build_context()        [Python only]
      → call_llm()             [처방 3안 생성, LLM 1회]
      → validate_and_fix()     [화이트리스트 검증 + 합계 보정]
      → print_results() + save_results()

Usage:
    python formulator_poc_v1.0.py \\
        --data data.csv \\
        --product product.csv \\
        --external external.csv \\
        --query "끈적이지 않는 산뜻한 미백 에센스, 나이아신아마이드 5%"

    python formulator_poc_v1.0.py ... --model us.anthropic.claude-haiku-4-5-20251001-v1:0

Requirements:
    pip install pandas numpy openpyxl boto3
    (컬러출력): pip install rich
"""

import argparse
import ast
import json
import os
import re
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from openpyxl.styles import Alignment

try:
    import boto3
except ImportError:
    boto3 = None

warnings.filterwarnings("ignore")

# ── rich 선택적 임포트 ────────────────────────────────────────────────────
try:
    from rich.console import Console as _RC
    from rich.table import Table as RichTable
    from rich import box as rbox
    _rich_console = _RC()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class _FallbackConsole:
    @staticmethod
    def _s(m):
        return re.sub(r"\[/?[^\]]*\]", "", str(m))

    def print(self, m="", **k):
        print(self._s(m))

    def rule(self, m="", **k):
        print("\n" + "=" * 60 + "\n  " + self._s(m) + "\n" + "=" * 60)


console = _rich_console if HAS_RICH else _FallbackConsole()

ANTHROPIC_VERSION  = "bedrock-2023-05-31"
DEFAULT_AWS_REGION = "ap-northeast-2"
DEFAULT_MODEL_ID   = "anthropic.claude-3-5-sonnet-20240620-v1:0"

# ── 구조적 역할 분류 ─────────────────────────────────────────────────────
STRUCTURAL_FUNCTIONS = {
    "base":        ["용제", "벌킹제"],
    "thickener":   ["점도조절제", "유화안정제"],
    "preservative":["살균보존제", "항균제", "항미생물제"],
    "ph_adj":      ["pH조절제", "pH 완충제"],
    "chelator":    ["금속이온봉쇄제"],
    "surfactant":  ["계면활성제", "유화제"],
    "fragrance":   ["방향제", "향료", "착향제"],
}
_FUNC_TO_ROLE = {fn: role for role, fns in STRUCTURAL_FUNCTIONS.items() for fn in fns}
BASE_ROLES = {"base", "thickener", "preservative", "ph_adj", "chelator"}

_KNOWN_BASE = {
    "정제수", "글리세린", "부틸렌글라이콜", "프로판다이올", "1,2-헥산다이올",
    "다이프로필렌글라이콜", "에틸헥실글리세린", "펜틸렌글라이콜", "메틸프로판다이올",
    "잔탄검", "트로메타민", "카보머", "소듐파이테이트", "다이소듐이디티에이",
    "향료", "토코페롤", "암모늄아크릴로일다이메틸타우레이트/브이피코폴리머",
    "아크릴레이트/C10-30알킬아크릴레이트크로스폴리머",
}

# ── 관용명/음차 힌트 사전 ────────────────────────────────────────────────
ALIAS_HINTS: dict[str, list[str]] = {
    "어성초추출물":  ["약모밀추출물"],
    "피디알엔":      ["소듐디엔에이", "하이드롤라이즈드디엔에이"],
    "PDRN":          ["소듐디엔에이", "하이드롤라이즈드디엔에이"],
    "비타민c":       ["3-O-에틸아스코빅애씨드", "아스코빅애씨드"],
    "글루타치온":    ["글루타티온"],
    "PHA":           ["글루코노락톤", "락토바이오닉애씨드"],
    "AHA":           ["글라이콜릭애씨드", "락틱애씨드", "말릭애씨드", "시트릭애씨드", "타르타릭애씨드"],
    "BHA":           ["살리실릭애씨드"],
    "LHA":           ["카프릴로일살리실릭애씨드"],
    "시카":          ["병풀"],
}

# ── 제형·사용감 키워드 (정규식 추출용) ───────────────────────────────────
FORMULATION_HINT_KEYWORDS = [
    "끈적이지 않는", "끈적임 없는", "산뜻한", "산뜻하게", "가벼운", "가볍게",
    "오일프리", "촉촉한", "촉촉하게", "가용화", "워터리",
    "에센스", "세럼", "앰플", "토너", "로션", "크림", "젤", "겔", "폼",
    "투명한", "유화", "에멀전",
]

MARKETING_HINT_KEYWORDS = [
    "미백", "브라이트닝", "안티에이징", "항노화", "진정", "탄력",
    "재생", "피부장벽", "트러블", "민감성", "광채", "주름", "모공",
    "각질", "피부결", "수분", "보습", "영양",
]


# ─────────────────────────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────────────────────────

def _strip_json_fences(raw: str) -> str:
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text)


def _build_bedrock_messages_payload(
    prompt: str,
    max_tokens: int,
    system: str | None = None,
    temperature: float = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "anthropic_version": ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system is not None:
        payload["system"] = system
    return payload


def _invoke_bedrock_json(
    bedrock_client,
    model_id: str,
    prompt: str,
    max_tokens: int,
    system: str | None = None,
    temperature: float = 0,
) -> tuple[Any, str]:
    response = bedrock_client.invoke_model(
        modelId=model_id,
        body=json.dumps(
            _build_bedrock_messages_payload(prompt, max_tokens=max_tokens, system=system, temperature=temperature),
            ensure_ascii=False,
        ),
    )
    body = json.loads(response["body"].read())
    raw  = _strip_json_fences(body["content"][0]["text"])
    return json.loads(raw), raw


def _norm_name(s: str) -> str:
    return re.sub(r"[\s\-_·•]", "", str(s)).lower()


def _safe_literal_list(value) -> list:
    try:
        parsed = ast.literal_eval(str(value))
    except (SyntaxError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _format_stat_line(i: dict) -> str:
    return (
        f"  · {i['name']}: 빈도 {i['frequency']*100:.0f}% ({i['count']}건), "
        f"함량 {i['min']}~{i['max']}% (중앙값 {i['median']}%)"
    )


def _join_or_none(values: list[str]) -> str:
    return ", ".join(values) or "없음"


def _formula_ingredient_rows(formula: dict) -> list[dict]:
    return [
        {"성분명": i.get("name", ""), "함량(%)": i.get("content", 0), "역할": i.get("role", "")}
        for i in sorted(formula["ingredients"], key=lambda x: -x.get("content", 0))
    ]


def _format_wrapped_sheet(ws, wrap_columns: list[int], width_map: dict[int, float] | None = None):
    width_map = width_map or {}
    for col_idx, width in width_map.items():
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width
    for row in ws.iter_rows():
        max_lines = 1
        for cell in row:
            value = cell.value
            if isinstance(value, str):
                max_lines = max(max_lines, value.count("\n") + 1)
            if cell.column in wrap_columns:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        if row[0].row > 1 and max_lines > 1:
            ws.row_dimensions[row[0].row].height = max(15 * max_lines, 30)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 처방 데이터 로드
# ─────────────────────────────────────────────────────────────────────────────

def load_formula_data(csv_path: str):
    df = pd.read_csv(csv_path, engine="python")
    required = {"bulk_code", "bulk_name", "ingredient_name", "ingredient_function", "content"}
    if missing := required - set(df.columns):
        raise ValueError(f"필수 컬럼 누락: {missing}")

    df["ingredient_name"]     = df["ingredient_name"].str.strip()
    df["ingredient_function"] = df["ingredient_function"].str.strip().str.replace(r"\s+", " ", regex=True)
    df["content"]             = pd.to_numeric(df["content"], errors="coerce")
    df = df.dropna(subset=["content"])

    formula_dict = {}
    for bulk_code, grp in df.groupby("bulk_code"):
        total = grp["content"].sum()
        if total <= 0:
            continue
        ingredients      = {}
        structural_roles = {}
        for _, row in grp.iterrows():
            name = row["ingredient_name"]
            ingredients[name] = round(row["content"], 6)
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


# ─────────────────────────────────────────────────────────────────────────────
# 2. 통계 분석
# ─────────────────────────────────────────────────────────────────────────────

def build_stats(formula_dict: dict) -> dict:
    idata  = defaultdict(list)
    iroles = {}
    for fd in formula_dict.values():
        for name, pct in fd["ingredients"].items():
            idata[name].append(pct)
            role = fd["structural_roles"].get(name)
            if role and name not in iroles:
                iroles[name] = role

    N = len(formula_dict)
    stats = {}
    for name, vals in idata.items():
        stats[name] = {
            "structural_role": iroles.get(name, "active_or_unknown"),
            "frequency":       round(len(vals) / N, 3),
            "count":           len(vals),
            "min":             round(min(vals),  4),
            "max":             round(max(vals),  4),
            "median":          round(float(np.median(vals)), 4),
            "mean":            round(float(np.mean(vals)),   4),
            "std":             round(float(np.std(vals)),    4),
            "p25":             round(float(np.percentile(vals, 25)), 4),
            "p75":             round(float(np.percentile(vals, 75)), 4),
        }
    return {"ingredient_stats": stats, "total_formulas": N}


# ─────────────────────────────────────────────────────────────────────────────
# 3. 마케팅 키워드 DB
# ─────────────────────────────────────────────────────────────────────────────

def load_product_data(product_csv: str, formula_dict: dict) -> dict:
    prod = pd.read_csv(product_csv, engine="python")
    overlap = set(prod["bulk_code"]) & set(formula_dict.keys())
    console.print(f"[green]✓ 마케팅 데이터: {len(prod)}건 / 처방 연결: {len(overlap)}건[/green]")

    keyword_db   = defaultdict(lambda: {"ingredients": Counter(), "aspects": set(), "formula_codes": []})
    all_keywords = []

    for _, row in prod[prod["bulk_code"].isin(overlap)].drop_duplicates("bulk_code").iterrows():
        code = row["bulk_code"]
        kws  = _safe_literal_list(row.get("marketing_keywords_list", []))
        asps = _safe_literal_list(row.get("aspect_list", []))

        fd = formula_dict[code]
        active_ings = [
            name for name, pct in sorted(fd["ingredients"].items(), key=lambda x: -x[1])
            if fd["structural_roles"].get(name) not in BASE_ROLES and name not in _KNOWN_BASE
        ][:10]

        for kw in kws:
            keyword_db[kw]["ingredients"].update(active_ings)
            keyword_db[kw]["aspects"].update(asps)
            keyword_db[kw]["formula_codes"].append(code)
            all_keywords.append(kw)

    console.print(f"[green]✓ 키워드 {len(keyword_db)}종 인덱싱[/green]")
    return dict(keyword_db)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 유사 처방 탐색
# ─────────────────────────────────────────────────────────────────────────────

def _pick_similar(
    formula_dict: dict,
    query_ing_names: set,
    priority_codes: set | None = None,
    n: int = 3,
    exclude_codes: set | None = None,
) -> list[dict]:
    priority_codes = priority_codes or set()
    exclude_codes  = exclude_codes  or set()

    scores = []
    for code, fd in formula_dict.items():
        if code in exclude_codes:
            continue
        overlap     = sum(1 for nm in fd["ingredients"] if nm in query_ing_names)
        is_priority = 1 if code in priority_codes else 0
        scores.append((is_priority, overlap, len(fd["ingredients"]), code))

    scores.sort(reverse=True)
    return [
        {"bulk_code": c, "name": formula_dict[c]["name"], "ingredients": formula_dict[c]["ingredients"]}
        for _, _, _, c in scores[:n]
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 5. 질의 분석 (정규식 — LLM 호출 없음)
# ─────────────────────────────────────────────────────────────────────────────

def extract_query_info(query: str) -> dict:
    """
    정규식 기반 질의 분석. LLM 호출 없음.
    Returns:
        {
          "ingredients":      ["나이아신아마이드", "판테놀"],
          "constraints":      {"나이아신아마이드": 5.0, "판테놀": None},
          "formulation_hints":["끈적이지 않는", "에센스"],
          "marketing_hints":  ["미백"],
        }
    """
    ingredients: list[str] = []
    constraints: dict[str, float | None] = {}
    seen: set[str] = set()

    def _add(name: str, amount: float | None = None):
        key = _norm_name(name)
        if key in seen or len(key) < 2:
            return
        seen.add(key)
        ingredients.append(name)
        constraints[name] = amount

    # 1) 함량 지정 패턴: "성분명 N%" (최대 4개 단어)
    amount_pat = re.compile(
        r'([가-힣a-zA-Z0-9\-_·•]+(?:\s+[가-힣a-zA-Z0-9\-_·•]+){0,3})\s+(\d+(?:\.\d+)?)\s*%'
    )
    for m in amount_pat.finditer(query):
        _add(m.group(1).strip(), float(m.group(2)))

    # 2) ALIAS_HINTS 키 매칭
    for alias in ALIAS_HINTS:
        if alias.lower() in query.lower():
            _add(alias)

    # 3) 비타민 패턴
    for m in re.finditer(r'비타민\s*([A-Za-z]\d*)', query, re.IGNORECASE):
        _add(f"비타민{m.group(1).upper()}")

    # 4) 영문 약어 (2자 이상 대문자)
    for m in re.finditer(r'\b([A-Z]{2,}(?:\d+)?)\b', query):
        _add(m.group(1))

    formulation_hints = [kw for kw in FORMULATION_HINT_KEYWORDS if kw in query]
    marketing_hints   = [kw for kw in MARKETING_HINT_KEYWORDS if kw in query]

    return {
        "ingredients":       ingredients,
        "constraints":       constraints,
        "formulation_hints": formulation_hints,
        "marketing_hints":   marketing_hints,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. 성분명 매핑 (2단계: exact match → Claude 소호출)
# ─────────────────────────────────────────────────────────────────────────────

def _claude_map_ingredients(
    terms: list[str],
    known_set: set,
    bedrock_client,
    model_id: str,
) -> dict[str, list[str] | None]:
    terms_str  = "\n".join(f"- {t}" for t in terms)
    known_list = "\n".join(sorted(known_set))

    hints_lines = []
    for alias, targets in ALIAS_HINTS.items():
        if any(alias.lower() in t.lower() for t in terms):
            hints_lines.append(f"  · {alias} → {', '.join(targets)}")
    hints_section = ("\n[성분 이명 힌트]\n" + "\n".join(hints_lines) + "\n") if hints_lines else ""

    prompt = (
        "다음은 화장품 처방 DB에 등록된 성분명 전체 목록입니다.\n\n"
        f"[DB 성분 목록]\n{known_list}\n"
        f"{hints_section}\n"
        "아래 표현에 대해 위 목록 안에서 가장 일치하는 실제 성분명을 찾으세요.\n"
        "목록에 있는 성분명만 사용. 없는 성분명 생성 절대 금지.\n"
        "매핑 불가 시 빈 배열 반환.\n\n"
        f"[매핑 대상]\n{terms_str}\n\n"
        "JSON만 반환 (표현당 최대 5개):\n"
        "{\"표현1\": [\"성분명A\"], \"표현2\": []}"
    )

    try:
        result, _ = _invoke_bedrock_json(bedrock_client, model_id, prompt, max_tokens=1500)
        return {
            k: ([c for c in v if c in known_set] or None)
            for k, v in result.items()
            if isinstance(v, list)
        }
    except Exception as e:
        console.print(f"[yellow]성분명 매핑 Claude 호출 실패: {e}[/yellow]")
        return {t: None for t in terms}


def map_ingredients(
    terms: list[str],
    stats: dict,
    bedrock_client=None,
    model_id: str = "",
) -> dict[str, list[str] | None]:
    """
    1단계: 정규화 exact match (공백/특수문자 제거 후 비교)
    2단계: 1단계 실패 시 Claude 소호출로 일괄 처리
    Returns: {term: [db_name, ...] or None}
    """
    known_set = set(stats["ingredient_stats"].keys())
    result: dict[str, list[str] | None] = {}

    for term in terms:
        norm = _norm_name(term)
        matched = next((k for k in known_set if _norm_name(k) == norm), None)
        if matched:
            result[term] = [matched]
        else:
            result[term] = None

    unmapped = [t for t, v in result.items() if v is None]
    if unmapped and bedrock_client and model_id:
        claude_result = _claude_map_ingredients(unmapped, known_set, bedrock_client, model_id)
        result.update(claude_result)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 7. 컨텍스트 구성 (Python only — LLM 없음)
# ─────────────────────────────────────────────────────────────────────────────

def build_context(
    query: str,
    stats: dict,
    keyword_db: dict,
    formula_dict: dict,
    query_info: dict,
    ingredient_map: dict,
    top_base: int = 15,
    top_active: int = 15,
) -> dict:
    ist = stats["ingredient_stats"]

    # 매핑된 DB 성분명
    user_ing_names: set[str] = set()
    for mapped in ingredient_map.values():
        if mapped:
            user_ing_names.update(mapped)

    # 마케팅 키워드 매핑
    matched_keywords: list[tuple[str, dict]] = []
    seen_kws: set[str] = set()
    for kw, data in keyword_db.items():
        if kw in query and kw not in seen_kws:
            matched_keywords.append((kw, data))
            seen_kws.add(kw)

    keyword_ing_names: set[str] = set()
    keyword_formula_codes: set[str] = set()
    for _, kdata in matched_keywords:
        keyword_ing_names.update(ing for ing, _ in kdata["ingredients"].most_common(8))
        keyword_formula_codes.update(kdata["formula_codes"])

    # 유사 처방
    similar = _pick_similar(
        formula_dict,
        user_ing_names | keyword_ing_names,
        priority_codes=keyword_formula_codes,
        n=3,
    )

    # base / active 성분 분리
    priority = user_ing_names | keyword_ing_names
    base_ings:   list[dict] = []
    active_ings: list[dict] = []

    for name, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"]):
        role  = s["structural_role"]
        entry = {"name": name, **s}
        if role in BASE_ROLES or name in _KNOWN_BASE:
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
            if s["structural_role"] not in BASE_ROLES and n not in _KNOWN_BASE and n not in exist
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


# ─────────────────────────────────────────────────────────────────────────────
# 8. 프롬프트
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """당신은 화장품 처방 전문가입니다. 연구원이 실험을 시작할 수 있는 초기 처방 백본을 설계합니다.

[처방 설계 규칙]
1. 모든 성분 함량의 합은 정확히 100.00%여야 합니다. 정제수로 나머지를 채우세요.
2. 정제수가 기본 용제로 가장 높은 비율을 차지합니다 (통상 60~90%).
3. 방부·보존 성분이 반드시 포함되어야 합니다.
4. 성분명은 제공된 [허용 성분 목록]에 있는 것만 사용합니다. 목록에 없는 성분명은 절대 생성하지 마세요.
5. 사용자가 함량을 직접 지정한 경우 해당 함량을 정확히 사용합니다.
6. 제공된 통계(중앙값, 범위)를 참고해 현실적인 함량을 배정하세요. 통계 최대값을 크게 초과하지 마세요.
7. 질의의 사용감 요구(끈적이지 않는, 산뜻한 등)와 제형 특성을 스스로 판단해 성분과 함량에 반영하세요.

[출력 형식 — JSON만 응답, 다른 텍스트 없이]
{
  "formulas": [
    {
      "name": "Formula A",
      "concept": "컨셉 한 줄",
      "key_ingredients": ["핵심성분1", "핵심성분2"],
      "target_aspects": ["미백", "산뜻함"],
      "ingredients": [
        {"name": "성분명", "content": 숫자(%), "role": "역할 설명"}
      ]
    },
    {"name": "Formula B", "concept": "...", "key_ingredients": [], "target_aspects": [], "ingredients": []},
    {"name": "Formula C", "concept": "...", "key_ingredients": [], "target_aspects": [], "ingredients": []}
  ],
  "design_rationale": "3안 설계 근거 요약"
}"""


def build_user_prompt(query: str, ctx: dict, total_formulas: int) -> str:
    lines: list[str] = []
    query_info = ctx.get("query_info", {})

    # ── 요구사항 ──────────────────────────────────────────────────────────
    lines.append(f"[요구사항]\n{query}")

    formulation_hints = query_info.get("formulation_hints", [])
    marketing_hints   = query_info.get("marketing_hints", [])
    if formulation_hints:
        lines.append(f"\n[제형·사용감 요구]\n  {', '.join(formulation_hints)}")
    if marketing_hints:
        lines.append(f"\n[마케팅 포인트]\n  {', '.join(marketing_hints)}")

    # ── 유사 처방 (few-shot) ──────────────────────────────────────────────
    similar = ctx.get("similar_formulas", [])
    if similar:
        lines.append(f"\n[유사 처방 참고 (few-shot, top-{len(similar)}건)]")
        for idx, f in enumerate(similar, 1):
            lines.append(f"처방{idx}: {f['name']}")
            for nm, pct in sorted(f["ingredients"].items(), key=lambda x: -x[1])[:14]:
                lines.append(f"  {nm}: {pct:.3f}%")

    # ── 성분 통계 ──────────────────────────────────────────────────────────
    lines.append(f"\n[성분 통계 — {total_formulas}건 처방 기준]")
    for i in ctx.get("base_ings", [])[:12]:
        lines.append(_format_stat_line(i))
    for i in ctx.get("active_ings", []):
        lines.append(_format_stat_line(i))

    # ── 사용자 지정 함량 ───────────────────────────────────────────────────
    constraints   = query_info.get("constraints", {})
    ingredient_map = ctx.get("ingredient_map", {})
    specified = {name: amt for name, amt in constraints.items() if amt is not None}
    if specified:
        lines.append("\n[사용자 지정 함량 — 반드시 이 함량 정확히 준수]")
        for name, amt in specified.items():
            db_names = ingredient_map.get(name) or []
            if db_names:
                lines.append(f"  · {name} (DB 후보: {', '.join(db_names)} — 1종 선택 후 정확히 {amt}% 배정)")
            else:
                lines.append(f"  · {name}: 정확히 {amt}%")

    # ── 허용 성분 목록 ────────────────────────────────────────────────────
    allowed = ctx.get("allowed_ingredients", [])
    if allowed:
        lines.append(
            f"\n[허용 성분 목록 — 반드시 이 목록에서만 선택 (총 {len(allowed)}종)]\n"
            + ", ".join(allowed)
        )

    # ── 3안 설계 지침 ─────────────────────────────────────────────────────
    lines.append(
        "\n[3안 설계 지침]\n"
        "- Formula A: 핵심 활성 성분 고함량 + 성분 수 최소화 (심플 & 집중 효능)\n"
        "- Formula B: 핵심 효능 + 보습·진정 복합 기능 밸런스 (올라운드 실용 처방)\n"
        "- Formula C: 트렌드 성분 또는 복합 활성 성분 추가, 마케팅 소구점 강화 (프리미엄·차별화)\n"
        "- 각 안의 함량 합계가 정확히 100.00%가 되도록 정제수 함량으로 조정하세요.\n"
        "- [허용 성분 목록]에 없는 성분은 절대 사용하지 마세요."
    )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 9. 비용 계산
# ─────────────────────────────────────────────────────────────────────────────

BEDROCK_PRICING = {
    "anthropic.claude-3-5-sonnet-20240620-v1:0":    {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-5-haiku-20241022-v1:0":     {"input": 1.00, "output":  5.00},
    "anthropic.claude-3-sonnet-20240229-v1:0":      {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-haiku-20240307-v1:0":       {"input": 0.25, "output":  1.25},
    "anthropic.claude-3-opus-20240229-v1:0":        {"input": 15.00,"output": 75.00},
    "anthropic.claude-sonnet-4-5-20250929-v1:0":    {"input": 3.00, "output": 15.00},
    "anthropic.claude-haiku-4-5-20251001-v1:0":     {"input": 1.00, "output":  5.00},
    "anthropic.claude-opus-4-5-20251101-v1:0":      {"input": 5.00, "output": 25.00},
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.00, "output": 15.00},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0":  {"input": 1.00, "output":  5.00},
}


def calc_cost(model_id: str, input_tokens: int, output_tokens: int) -> dict:
    KRW_PER_USD = 1380
    pricing = BEDROCK_PRICING.get(model_id)
    if pricing is None:
        pricing    = {"input": 3.00, "output": 15.00}
        price_note = f"⚠ '{model_id}' 요금 미등록 → Sonnet 기본 요금 적용"
    else:
        price_note = f"입력 ${pricing['input']}/1M · 출력 ${pricing['output']}/1M (서울 리전)"

    input_cost  = input_tokens  / 1_000_000 * pricing["input"]
    output_cost = output_tokens / 1_000_000 * pricing["output"]
    total_usd   = input_cost + output_cost

    return {
        "input_tokens":    input_tokens,
        "output_tokens":   output_tokens,
        "input_cost_usd":  round(input_cost,  6),
        "output_cost_usd": round(output_cost, 6),
        "total_cost_usd":  round(total_usd,   6),
        "total_cost_krw":  round(total_usd * KRW_PER_USD, 2),
        "price_note":      price_note,
    }


def print_cost_summary(cost: dict):
    lines = [
        "💰 비용 요약",
        f"  토큰: 입력 {cost['input_tokens']:,} / 출력 {cost['output_tokens']:,} / 합계 {cost['input_tokens']+cost['output_tokens']:,}",
        f"  입력 비용: ${cost['input_cost_usd']:.6f} USD",
        f"  출력 비용: ${cost['output_cost_usd']:.6f} USD",
        f"  총 비용:   ${cost['total_cost_usd']:.6f} USD  (약 {cost['total_cost_krw']:,.2f}원, 환율 1,380원/USD 기준)",
        f"  요금 기준: {cost['price_note']}",
    ]
    console.print("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# 10. LLM 호출 (처방 생성 — 1회)
# ─────────────────────────────────────────────────────────────────────────────

def _create_bedrock_client(aws_profile: str | None, aws_region: str):
    if boto3 is None:
        raise RuntimeError("boto3 미설치 — `pip install boto3` 필요")
    if aws_profile:
        session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
    else:
        session = boto3.Session()
    return session.client(service_name="bedrock-runtime", region_name=aws_region)


def call_llm(
    query: str,
    ctx: dict,
    bedrock_client,
    model_id: str,
    max_retries: int = 2,
) -> tuple[dict | None, dict, dict]:
    import time

    total_formulas = ctx.get("total_formulas", 0)
    user_prompt    = build_user_prompt(query, ctx, total_formulas)
    prompt_payload = {
        "model_id":      model_id,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt":   user_prompt,
    }

    console.print(f"[dim]Claude API 호출 중... (모델: {model_id})[/dim]")

    for attempt in range(1, max_retries + 2):
        try:
            response = bedrock_client.invoke_model(
                modelId=model_id,
                body=json.dumps(
                    _build_bedrock_messages_payload(user_prompt, max_tokens=4096, system=SYSTEM_PROMPT),
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            err = str(e)
            if "ThrottlingException" in err or "Too Many Requests" in err:
                wait = 10 * attempt
                console.print(f"[yellow]Rate limit — {wait}초 후 재시도 ({attempt}/{max_retries})[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"[red]API 호출 오류: {err}[/red]")
            return None, {}, prompt_payload

        body    = json.loads(response["body"].read())
        text    = body["content"][0]["text"]
        usage   = body.get("usage", {})
        in_tok  = usage.get("input_tokens",  0)
        out_tok = usage.get("output_tokens", 0)
        cost    = calc_cost(model_id, in_tok, out_tok)
        console.print(f"[dim]✓ 토큰: 입력 {in_tok:,} / 출력 {out_tok:,}[/dim]")

        raw = _strip_json_fences(text)
        try:
            return json.loads(raw), cost, prompt_payload
        except json.JSONDecodeError:
            if attempt <= max_retries:
                console.print(f"[yellow]JSON 파싱 실패 — 재시도 ({attempt}/{max_retries})[/yellow]")
                user_prompt += "\n\n반드시 JSON 형식만 응답하세요. 다른 텍스트 없이."
                prompt_payload["user_prompt"] = user_prompt
                continue
            console.print(f"[red]JSON 파싱 최종 실패\n{raw[:400]}[/red]")
            return None, cost, prompt_payload

    return None, {}, prompt_payload


# ─────────────────────────────────────────────────────────────────────────────
# 11. 후처리 및 검증
# ─────────────────────────────────────────────────────────────────────────────

def validate_and_fix(
    formula_data: dict,
    stats: dict,
    known_set: set | None = None,
    user_constraints: dict | None = None,
) -> dict:
    """
    1) 화이트리스트 검증: 정규화 exact match → 불일치 시 제거
    2) 합계 100% 보정: 정제수 함량으로 조정
    3) 사용자 지정 함량 보호: user_constraints 성분은 보정 대상에서 제외
    """
    user_constraints = user_constraints or {}
    norm_user = {_norm_name(k): v for k, v in user_constraints.items()}

    for formula in formula_data.get("formulas", []):
        ings = formula.get("ingredients", [])

        # ── 1) 화이트리스트 검증 ─────────────────────────────────────────
        if known_set:
            checked: list[dict] = []
            for ing in ings:
                name = ing.get("name", "")
                if name in known_set:
                    checked.append(ing)
                    continue
                # 정규화 exact match
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
                (i for i in ings
                 if "정제수" in i.get("name", "") or "water" in i.get("name", "").lower()),
                None,
            )
            if water:
                # 사용자 지정 함량이면 정제수라도 보정하지 않음
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


# ─────────────────────────────────────────────────────────────────────────────
# 12. 출력
# ─────────────────────────────────────────────────────────────────────────────

def print_results(formula_data: dict, query: str):
    console.rule(f"처방 생성 결과 — {query}")
    for formula in formula_data.get("formulas", []):
        name    = formula.get("name", "")
        concept = formula.get("concept", "")
        key_i   = formula.get("key_ingredients", [])
        aspects = formula.get("target_aspects", [])
        ings    = sorted(formula.get("ingredients", []), key=lambda x: -x.get("content", 0))

        if HAS_RICH:
            _rich_console.print(f"\n[bold cyan]{'─'*65}[/bold cyan]")
            _rich_console.print(f"[bold yellow]{name}[/bold yellow]  │  {concept}")
            if key_i:   _rich_console.print(f"[dim]핵심 성분: {', '.join(key_i)}[/dim]")
            if aspects: _rich_console.print(f"[dim]타겟 aspect: {', '.join(aspects)}[/dim]")
            _rich_console.print(f"[bold cyan]{'─'*65}[/bold cyan]")
            tbl = RichTable(box=rbox.SIMPLE, header_style="bold white on dark_blue")
            tbl.add_column("성분명",  min_width=34)
            tbl.add_column("함량(%)", justify="right", min_width=10, style="green")
            tbl.add_column("역할",    min_width=28)
            total = 0.0
            for i in ings:
                c = i.get("content", 0); total += c
                tbl.add_row(i.get("name", ""), f"{c:.4f}", i.get("role", ""))
            tbl.add_row("─"*34, "─"*8, "")
            tbl.add_row("[bold]합계[/bold]", f"[bold]{total:.4f}[/bold]", "")
            _rich_console.print(tbl)
        else:
            print(f"\n{'─'*65}\n{name}  │  {concept}")
            if key_i:   print(f"핵심 성분: {', '.join(key_i)}")
            if aspects: print(f"타겟 aspect: {', '.join(aspects)}")
            print(f"{'─'*65}")
            print(f"{'성분명':<34} {'함량(%)':>10}  역할\n" + "-"*75)
            total = 0.0
            for i in ings:
                c = i.get("content", 0); total += c
                print(f"{i.get('name', ''):<34} {c:>10.4f}  {i.get('role', '')}")
            print("-"*75 + f"\n{'합계':<46} {total:>10.4f}")

    if dr := formula_data.get("design_rationale"):
        print(f"\n{'─'*65}\n[설계 근거]\n{dr}")


# ─────────────────────────────────────────────────────────────────────────────
# 13. 파일 저장
# ─────────────────────────────────────────────────────────────────────────────

def save_results(
    formula_data: dict,
    stats: dict,
    keyword_db: dict,
    query: str,
    output_dir: str,
    cost: dict | None = None,
    prompt_payload: dict | None = None,
):
    cost = cost or {}
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    for formula in formula_data.get("formulas", []):
        fname = formula["name"].replace(" ", "_").lower()
        rows  = _formula_ingredient_rows(formula)
        csv_path = output_path / f"{fname}.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
        console.print(f"[green]✓ {csv_path}[/green]")

    xl_path = output_path / "formula_output.xlsx"
    with pd.ExcelWriter(xl_path, engine="openpyxl") as writer:
        # 처방 시트
        for formula in formula_data.get("formulas", []):
            rows = _formula_ingredient_rows(formula)
            df_f = pd.DataFrame(rows)
            df_f.loc[len(df_f)] = {"성분명": "합계", "함량(%)": df_f["함량(%)"].sum(), "역할": ""}
            df_f.to_excel(writer, sheet_name=formula["name"], index=False)
            _format_wrapped_sheet(
                writer.sheets[formula["name"]],
                wrap_columns=[1, 3],
                width_map={1: 34, 2: 12, 3: 48},
            )

        # 성분 통계 시트
        ist = stats["ingredient_stats"]
        pd.DataFrame([
            {"성분명": n, "구조적역할": s["structural_role"],
             "사용빈도": f"{s['frequency']*100:.1f}%", "사용처방수": s["count"],
             "최소(%)": s["min"], "25%(%)": s["p25"], "중앙값(%)": s["median"],
             "75%(%)": s["p75"], "최대(%)": s["max"]}
            for n, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"])
        ]).to_excel(writer, sheet_name="성분_통계", index=False)
        _format_wrapped_sheet(
            writer.sheets["성분_통계"],
            wrap_columns=[1, 2],
            width_map={1: 28, 2: 16, 3: 12, 4: 12, 5: 10, 6: 10, 7: 12, 8: 10, 9: 10},
        )

        # 키워드-성분 매핑 시트
        kw_rows = []
        for kw, kdata in keyword_db.items():
            for ing, cnt in kdata["ingredients"].most_common(5):
                kw_rows.append({
                    "키워드": kw, "연결성분": ing, "등장횟수": cnt,
                    "관련aspect": ", ".join(kdata["aspects"]),
                    "연결처방수": len(kdata["formula_codes"]),
                })
        pd.DataFrame(kw_rows).to_excel(writer, sheet_name="키워드_성분매핑", index=False)
        _format_wrapped_sheet(
            writer.sheets["키워드_성분매핑"],
            wrap_columns=[1, 2, 4],
            width_map={1: 22, 2: 26, 3: 10, 4: 28, 5: 12},
        )

        # 설계 근거 시트
        meta = [{"항목": "요구사항", "내용": query},
                {"항목": "설계 근거", "내용": formula_data.get("design_rationale", "")}]
        for f in formula_data.get("formulas", []):
            meta.append({
                "항목": f["name"],
                "내용": (
                    f"{f.get('concept', '')} | "
                    f"핵심: {', '.join(f.get('key_ingredients', []))} | "
                    f"aspect: {', '.join(f.get('target_aspects', []))}"
                ),
            })
        pd.DataFrame(meta).to_excel(writer, sheet_name="설계_근거", index=False)
        _format_wrapped_sheet(
            writer.sheets["설계_근거"],
            wrap_columns=[1, 2],
            width_map={1: 18, 2: 120},
        )

        # 비용 내역 시트
        if cost:
            cost_rows = [
                {"항목": "입력 토큰",      "값": f"{cost.get('input_tokens', 0):,}"},
                {"항목": "출력 토큰",      "값": f"{cost.get('output_tokens', 0):,}"},
                {"항목": "입력 비용",      "값": f"${cost.get('input_cost_usd', 0):.4f}"},
                {"항목": "출력 비용",      "값": f"${cost.get('output_cost_usd', 0):.4f}"},
                {"항목": "총 비용 (USD)", "값": f"${cost.get('total_cost_usd', 0):.6f}"},
                {"항목": "총 비용 (KRW)", "값": f"{cost.get('total_cost_krw', 0):,.2f}원"},
                {"항목": "요금 기준",      "값": cost.get("price_note", "")},
            ]
            pd.DataFrame(cost_rows).to_excel(writer, sheet_name="비용_내역", index=False)
            _format_wrapped_sheet(
                writer.sheets["비용_내역"],
                wrap_columns=[1, 2],
                width_map={1: 18, 2: 40},
            )

        # Claude 프롬프트 시트
        if prompt_payload:
            prompt_rows = [
                {"구분": "model_id",      "내용": prompt_payload.get("model_id", "")},
                {"구분": "system_prompt", "내용": prompt_payload.get("system_prompt", "")},
                {"구분": "user_prompt",   "내용": prompt_payload.get("user_prompt", "")},
            ]
            pd.DataFrame(prompt_rows).to_excel(writer, sheet_name="Claude_프롬프트", index=False)
            _format_wrapped_sheet(
                writer.sheets["Claude_프롬프트"],
                wrap_columns=[1, 2],
                width_map={1: 18, 2: 120},
            )

    console.print(f"[green]✓ {xl_path}[/green]")

    stats_path = output_path / "stats_summary.json"
    with stats_path.open("w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ {stats_path}[/green]")


# ─────────────────────────────────────────────────────────────────────────────
# 14. 메인 파이프라인
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    data_csv:    str,
    product_csv: str,
    external_csv: str,
    query:       str,
    aws_profile: str | None = None,
    aws_region:  str        = DEFAULT_AWS_REGION,
    model_id:    str        = DEFAULT_MODEL_ID,
    output_dir:  str        = "output",
):
    console.rule("AI 기반 화장품 처방 자동 생성 PoC v1.0 (AWS Bedrock)")

    # ── Bedrock 클라이언트 ────────────────────────────────────────────────
    try:
        bedrock_client = _create_bedrock_client(aws_profile, aws_region)
        console.print("[green]✓ Bedrock 클라이언트 생성[/green]")
    except Exception as e:
        console.print(f"[red]Bedrock 클라이언트 생성 실패: {e}[/red]")
        return

    # 1. 처방 데이터 로드
    df, formula_dict = load_formula_data(data_csv)

    # 2. 통계
    console.print("[dim]통계 분석 중...[/dim]")
    stats     = build_stats(formula_dict)
    known_set = set(stats["ingredient_stats"].keys())
    console.print(f"[green]✓ 성분 {len(known_set)}종 통계[/green]")

    # 3. 마케팅 키워드 DB
    keyword_db = load_product_data(product_csv, formula_dict)

    # 4. 질의 분석 (정규식 — LLM 없음)
    console.print("[dim]질의 분석 중...[/dim]")
    query_info = extract_query_info(query)
    console.print(f"[dim]  성분 표현: {query_info['ingredients']}[/dim]")
    console.print(f"[dim]  제형 힌트: {query_info['formulation_hints']}[/dim]")
    console.print(f"[dim]  마케팅 힌트: {query_info['marketing_hints']}[/dim]")

    # 5. 성분명 매핑 (exact match + 필요 시 Claude 소호출)
    ingredient_map: dict[str, list[str] | None] = {}
    if query_info["ingredients"]:
        console.print("[dim]성분명 매핑 중...[/dim]")
        ingredient_map = map_ingredients(
            query_info["ingredients"], stats, bedrock_client, model_id
        )
        for term, mapped in ingredient_map.items():
            status = f"→ {mapped}" if mapped else "→ 매핑 실패"
            console.print(f"[dim]  '{term}' {status}[/dim]")

    # 사용자 지정 함량 맵 구성
    # 하나의 표현이 여러 DB 성분에 매핑될 경우 모든 후보에 동일 함량 적용
    user_constraints: dict[str, float] = {}
    for name, amount in query_info["constraints"].items():
        if amount is None:
            continue
        mapped = ingredient_map.get(name)
        if mapped:
            for db_name in mapped:
                user_constraints[db_name] = amount
        else:
            user_constraints[name] = amount
    if user_constraints:
        console.print(f"[dim]  사용자 지정 함량: {user_constraints}[/dim]")

    # 6. 컨텍스트 구성 (Python only)
    ctx = build_context(
        query, stats, keyword_db, formula_dict, query_info, ingredient_map
    )
    ctx["total_formulas"] = stats["total_formulas"]
    console.print(
        f"[green]✓ base {len(ctx['base_ings'])}종 / active {len(ctx['active_ings'])}종 / "
        f"유사처방 {len(ctx['similar_formulas'])}건 / 허용 성분 {len(ctx['allowed_ingredients'])}종[/green]"
    )

    # 7. LLM 1회 호출 — 처방 3안 생성
    formula_data, cost, prompt_payload = call_llm(query, ctx, bedrock_client, model_id)
    if not formula_data:
        console.print("[red]처방 생성 실패[/red]")
        return

    # 8. 후처리
    formula_data = validate_and_fix(
        formula_data, stats, known_set=known_set, user_constraints=user_constraints
    )

    # 9. 출력 & 저장
    print_results(formula_data, query)
    save_results(
        formula_data, stats, keyword_db, query, output_dir,
        cost=cost, prompt_payload=prompt_payload,
    )
    print_cost_summary(cost)
    console.print(f"\n[bold green]완료! 결과: {output_dir}/[/bold green]")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="화장품 처방 자동 생성 PoC v1.0 (AWS Bedrock)")
    parser.add_argument("--data",        required=True,  help="처방 CSV (data.csv)")
    parser.add_argument("--product",     required=True,  help="마케팅 키워드 CSV (product.csv)")
    parser.add_argument("--external",    default="external.csv", help="타사 제품 CSV (사용 안 함, 호환용)")
    parser.add_argument("--query",       required=True,  help="제품 요구사항 텍스트")
    parser.add_argument("--aws_profile", default=None,   help="AWS 프로파일명 (로컬 개발용)")
    parser.add_argument("--aws_region",  default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_AWS_REGION),
                        help="AWS 리전 (기본: ap-northeast-2)")
    parser.add_argument("--model",       default=DEFAULT_MODEL_ID, help="Bedrock 모델 ID")
    parser.add_argument("--output",      default="output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_pipeline(
        data_csv    = args.data,
        product_csv = args.product,
        external_csv= args.external,
        query       = args.query,
        aws_profile = args.aws_profile,
        aws_region  = args.aws_region,
        model_id    = args.model,
        output_dir  = args.output,
    )


if __name__ == "__main__":
    main()
