"""
AI 기반 화장품 처방 자동 생성 시스템 — PoC v0.8
================================================
v0.8 변경 내용: v0.7 대비

  [명령 1] 제형 목적 확정 단계 신설
     - 슬롯 추출 직후 Claude가 formulation 슬롯 값으로 제형 타입 분류
     - 분류 기준: O/W 유화 / W/O 유화 / 가용화 / 겔 / 폼 / 기타
     - confidence low 또는 분류 실패 시 사용자 입력 요청 → 재추정 (최대 3회)
     - 3회 후에도 불명확하면 Claude 최근 추정값 임의 배정
     - 확정된 제형 타입은 이후 모든 단계의 컨텍스트로 유지

  [명령 2] 구조 성분 선정 단계 신설
     - 제형 목적 확정 직후 FORMULATION_STRUCTURAL_MAP에서 필요 카테고리 도출
     - data.csv의 ingredient_function 컬럼으로 구조 성분 후보 선정
     - 선정된 구조 성분은 처방 생성 시 필수 포함 성분으로 처리
     - _pick_similar 유사처방 탐색 시 구조 성분 포함 여부를 가중치로 반영

  [명령 3] 액티브 성분 물성 검토 단계 강화
     - (기존) 사용자 지정 성분(concept_ings)만 검토하던 것을
       build_context 이후 LLM 프롬프트에 실제 전달되는 active_ings 전체로 확장
       → 소듐하이알루로네이트 등 고빈도 자동 진입 성분도 검토 대상에 포함
     - 물성 검토 호출 위치를 build_context 이후로 이동
       (active_ings 목록 확정 후 검토 가능하도록 순서 조정)
     - 확인 속성 확장: 수용성/유용성, pH 민감도, 점도 영향, 제형 충돌 여부,
       규제 상한(regulatory_limit), 대체 성분 제안(suggested_replacement)
     - FEEL_CONFLICT_MAP 신설: 사용자 사용감 요구(끈적이지 않는, 산뜻한, 가벼운 등)와
       물성(viscosity_impact, solubility)을 자동으로 교차해 충돌을 formulation_conflict에 태그
       → Claude가 감지하지 못한 사용자 요구 기반 충돌을 Python 로직으로 보완
     - _fetch_replacements_for_flagged 신설: 충돌 태그된 성분에 대해 Claude가
       INCI 지식 기반으로 대체 성분을 자유롭게 제안 → data.csv(known_set) 교차 검증 후 확정
       DB 미등록 제안은 제외하고 등록된 성분만 suggested_replacement로 확정
     - 프롬프트 내 [사용 제한 성분] 섹션 신설:
       충돌 + 대체 후보가 모두 있는 경우에만 해당 성분을 통계 섹션에서 분리하여
       "반드시 배제 + 대체 성분 활용" 지시 전달
       대체 후보를 끝내 찾지 못한 경우 원래 성분을 그대로 사용 허용
     - 충돌 성분 터미널 로그 상세화: 성분별 속성 전체 + 대체 후보 출력

  [명령 4] 함량 결정 로직 강화
     - 기존 단순 합산 보정 삭제
     - 각 성분 함량이 data.csv 통계 max × 1.1 초과 시 상한 보정 + 경고
     - 보정 후 나머지를 정제수로 채워 합계 100% 확보
     - 정제수가 음수가 되는 경우 경고 출력
     - 사용자 지정 함량 맵 다중 매핑 수정:
       하나의 개념어(예: '비타민 C')가 여러 DB 성분명에 매핑되는 경우
       기존에는 마지막 매핑 하나만 보호하던 것을 → 매핑된 모든 후보에 동일 함량 적용
       LLM이 어느 후보를 선택하든 validate_and_fix에서 보정되지 않도록 보호

  [명령 5] 처방 생성 프롬프트 재구성
     - 프롬프트 섹션 순서 재편:
       1. 제형 타입 및 목적
       2. 구조 성분 목록 (필수 포함)
       3. 액티브 성분 + 물성 정보 (충돌 성분 제외, 대체 후보 포함)
       4. 함량 배정 제약 조건
       5. 타겟 처방 또는 유사처방 (참고용)
       + 기존 마케팅 키워드 / 통계 / 허용 성분 / 설계 지침 유지
     - 사용자 지정 함량 섹션 명확화:
       DB 매핑 후보 성분명 전체를 명시하고 "1종 선택 후 정확히 N% 배정" 지시 추가
       기존의 모호한 "최솟값/정확한 값 중 적절히 반영" 표현 제거

전체 흐름:
    입력 질의 → 슬롯 추출 →
    [명령 1] 제형 타입 확정 →
    [명령 2] 구조 성분 선정 →
    타겟 제품 탐색(있을 때) →
    성분명 탐색 →
    컨텍스트 구성(active_ings 확정) →
    [명령 3-A] 액티브 성분 물성 검토 (active_ings 전체) →
    [명령 3-B] 사용자 요구 ↔ 물성 충돌 자동 연결 (FEEL_CONFLICT_MAP) →
    [명령 3-C] 충돌 성분 대체 후보 조회 + data.csv 교차 검증 →
    [명령 5] Claude 처방 생성 (재구성된 프롬프트) →
    [명령 4] 후처리(함량 상한 검증 + 정제수 보충) →
    콘솔/CSV/Excel 저장

Usage:
    python formulator_poc_v0.8.py \\
        --data data.csv \\
        --product product.csv \\
        --external external.csv \\
        --query "파티온 노스카나인 트러블 세럼을 베이스로 진정 에센스"

    python formulator_poc_v0.8.py ... --model us.anthropic.claude-haiku-4-5-20251001-v1:0

Requirements:
    pip install pandas numpy openpyxl boto3
    (편집거리): pip install python-Levenshtein
    (임베딩):   pip install sentence-transformers faiss-cpu
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

ANTHROPIC_VERSION = "bedrock-2023-05-31"
DEFAULT_AWS_REGION = "ap-northeast-2"
DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"

QUERY_CONTEXT_KEYS = (
    "ingredients", "formulation", "marketing_points",
    "target_product", "ingredient_constraints",
)


def _empty_query_contexts() -> dict[str, list]:
    return {key: [] for key in QUERY_CONTEXT_KEYS}


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
            _build_bedrock_messages_payload(
                prompt,
                max_tokens=max_tokens,
                system=system,
                temperature=temperature,
            ),
            ensure_ascii=False,
        ),
    )
    body = json.loads(response["body"].read())
    raw = _strip_json_fences(body["content"][0]["text"])
    return json.loads(raw), raw


# ─────────────────────────────────────────────────────────────────────────────
# 구조적 역할 분류
# ─────────────────────────────────────────────────────────────────────────────
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

# ── 제형 타입 목록 및 타입별 필수 구조 성분 카테고리 (명령 1·2) ─────────────
FORMULATION_TYPES = ["O/W 유화", "W/O 유화", "가용화", "겔", "폼", "기타"]

FORMULATION_STRUCTURAL_MAP: dict[str, list[str]] = {
    "O/W 유화": [
        "계면활성제", "유화제", "점도조절제", "유화안정제",
        "용제", "벌킹제", "살균보존제", "항균제", "항미생물제",
        "pH조절제", "pH 완충제", "금속이온봉쇄제",
    ],
    "W/O 유화": [
        "계면활성제", "유화제", "용제", "벌킹제",
        "살균보존제", "항균제", "항미생물제",
    ],
    "가용화": [
        "계면활성제", "용제", "벌킹제",
        "살균보존제", "항균제", "항미생물제",
        "pH조절제", "pH 완충제", "금속이온봉쇄제",
    ],
    "겔": [
        "점도조절제", "유화안정제", "용제", "벌킹제",
        "살균보존제", "항균제", "항미생물제",
        "pH조절제", "pH 완충제", "금속이온봉쇄제",
    ],
    "폼": [
        "계면활성제", "유화제", "용제",
        "살균보존제", "항균제", "항미생물제",
    ],
    "기타": [
        "용제", "벌킹제", "살균보존제", "항균제", "항미생물제",
    ],
}



# Claude 3단계 호출 시 프롬프트에 힌트로 제공할 이명(異名) 사전.
# Claude가 스스로 연결짓지 못하는 한국 관용명 ↔ DB 등록명 쌍을 등록.
# 값은 DB 성분명과 정확히 일치하지 않아도 되며, Claude의 추론을 돕는 용도.
ALIAS_HINTS: dict[str, list[str]] = {
    "어성초추출물":      ["약모밀추출물"],
    "피디알엔": ["소듐디엔에이", "하이드롤라이즈드디엔에이"],
    "PDRN": ["소듐디엔에이", "하이드롤라이즈드디엔에이"],
    "비타민c":     ["3-O-에틸아스코빅애씨드", "아스코빅애씨드"],
    "글루타치온" : ["글루타티온"],
    "PHA" :["글루코노락톤","락토바이오닉애씨드"],
    "AHA":["글라이콜릭애씨드", "락틱애씨드", "말릭애씨드", "시트릭애씨드","타르타릭애씨드"],
    "BHA":["살리실릭애씨드"],
    "LHA":["카프릴로일살리실릭애씨드"],
    "시카":["병풀"]


}


def _build_alias_hint_section(texts: list[str], title: str) -> str:
    hint_lines = []
    lowered_texts = [str(text).lower() for text in texts]
    for alias, targets in ALIAS_HINTS.items():
        alias_lower = alias.lower()
        if any(alias_lower in text for text in lowered_texts):
            hint_lines.append(f"  · {alias} → {', '.join(targets)}")
    if not hint_lines:
        return ""
    return f"\n[{title}]\n" + "\n".join(hint_lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 처방 데이터 전처리
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


def _safe_literal_list(value) -> list:
    try:
        parsed = ast.literal_eval(str(value))
    except (SyntaxError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


# ─────────────────────────────────────────────────────────────────────────────
# 2. 마케팅 키워드 데이터 로드 및 키워드-성분 매핑 구축
# ─────────────────────────────────────────────────────────────────────────────

def load_product_data(product_csv: str, formula_dict: dict) -> dict:
    prod = pd.read_csv(product_csv, engine="python")
    overlap = set(prod["bulk_code"]) & set(formula_dict.keys())
    console.print(f"[green]✓ 마케팅 데이터: {len(prod)}건 로드 / 처방 연결: {len(overlap)}건[/green]")

    keyword_db   = defaultdict(lambda: {"ingredients": Counter(), "aspects": set(), "formula_codes": []})
    all_keywords = []
    all_aspects  = []

    for _, row in prod[prod["bulk_code"].isin(overlap)].drop_duplicates("bulk_code").iterrows():
        code = row["bulk_code"]
        kws = _safe_literal_list(row.get("marketing_keywords_list", []))
        asps = _safe_literal_list(row.get("aspect_list", []))

        fd = formula_dict[code]
        active_ings = [
            name for name, pct in sorted(fd["ingredients"].items(), key=lambda x: -x[1])
            if fd["structural_roles"].get(name) not in BASE_ROLES
            and name not in _KNOWN_BASE
        ][:10]

        for kw in kws:
            keyword_db[kw]["ingredients"].update(active_ings)
            keyword_db[kw]["aspects"].update(asps)
            keyword_db[kw]["formula_codes"].append(code)
            all_keywords.append(kw)
        all_aspects.extend(asps)

    console.print(f"[green]✓ 키워드 {len(keyword_db)}종 / 전체 키워드 토큰 {len(all_keywords)}개 인덱싱[/green]")
    return dict(keyword_db)


def match_query_keywords(query: str, keyword_db: dict) -> list[tuple[str, dict]]:
    matched = []
    for kw, data in keyword_db.items():
        if kw in query:
            matched.append((kw, data))
    return matched


def _normalize_context_phrase(text: str) -> str:
    return re.sub(r"[\s\-_·•,/]+", "", str(text)).lower()


def _cleanup_context_phrase(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip(" ,./")
    text = re.sub(r"비타민\s*([A-Za-z])", lambda m: f"비타민 {m.group(1).upper()}", text, flags=re.IGNORECASE)
    return text


def _dedupe_context_phrases(phrases: list[str], max_len: int = 20) -> list[str]:
    cleaned = []
    seen = set()

    for raw in phrases or []:
        text = _cleanup_context_phrase(raw)
        if not text:
            continue
        if re.fullmatch(r"[\d%\s.,~+\-]+", text):
            continue
        if len(text) > max_len:
            continue
        key = _normalize_context_phrase(text)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    cleaned.sort(key=lambda x: len(_normalize_context_phrase(x)), reverse=True)
    deduped = []
    kept_norms = []
    for text in cleaned:
        key = _normalize_context_phrase(text)
        if any(key != kept and key in kept for kept in kept_norms):
            continue
        deduped.append(text)
        kept_norms.append(key)

    return deduped


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


def extract_query_contexts(query: str, bedrock_client=None, model_id: str = "") -> dict[str, list[str]]:
    default = _empty_query_contexts()
    if not bedrock_client or not model_id:
        return default
    hints_section = _build_alias_hint_section(
        [query],
        "표현 이명 힌트 — 아래 관용명/음차 표현은 성분 표현으로 해석하세요",
    )

    prompt = (
        "다음 화장품 제품 요구사항 문장에서 맥락 표현을 5개 카테고리로만 구조화하세요.\n"
        "반드시 JSON만 반환하세요.\n\n"
        "[카테고리 정의]\n"
        "- ingredients: 원료/효능성 소재명\n"
        "- formulation: 제형, 물성, 외관, 사용감 표현\n"
        "- marketing_points: 미백, 브라이트닝, 안티에이징, 진정 같은 마케팅 소구 표현\n"
        "- target_product: 사용자가 베이스/참조/타겟으로 삼으려는 구체적인 제품명\n"
        "- ingredient_constraints: 특정 성분에 함량(%)을 직접 지정한 경우 "
        "(예: '약모밀 추출물 70% 이상' → {\"ingredient\": \"약모밀추출물\", \"amount\": 70})\n\n"
        "[중요 제약]\n"
        "- 전체 문장을 복사하지 말 것\n"
        "- 원문에서 직접 뽑은 짧은 명사구만 반환할 것\n"
        "- 없는 카테고리는 빈 배열\n"
        "- 중복 및 부분중복 제거\n"
        "- '비타민', '비타민C', '비타민 C'가 겹치면 가장 정보량이 많은 표현 하나만 남길 것\n"
        "- 숫자/퍼센트만 있는 조각은 제외할 것\n"
        "- 관용명/음차 표현도 성분 표현이면 ingredients에 포함할 것\n"
        "- target_product에는 제품명만 넣고, 일반 효능/제형/성분 표현은 넣지 말 것\n"
        "- ingredient_constraints의 amount는 반드시 숫자만 입력할 것 (단위 제외)\n"
        f"{hints_section}\n"
        f"[입력 문장]\n{query}\n\n"
        "반환 형식:\n"
        "{\"ingredients\": [\"...\"], \"formulation\": [\"...\"], "
        "\"marketing_points\": [\"...\"], \"target_product\": [\"...\"], "
        "\"ingredient_constraints\": [{\"ingredient\": \"성분명\", \"amount\": 숫자}]}"
    )

    try:
        parsed, _ = _invoke_bedrock_json(
            bedrock_client,
            model_id,
            prompt,
            max_tokens=800,
        )
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception as e:
        console.print(f"[yellow]맥락 표현 추출 실패: {e}[/yellow]")
        parsed = {}

    result = {}
    for key in QUERY_CONTEXT_KEYS:
        values = parsed.get(key, [])
        if not isinstance(values, list):
            values = []

        if key == "ingredient_constraints":
            clean = []
            for c in values:
                if isinstance(c, dict) and "ingredient" in c and "amount" in c:
                    try:
                        clean.append({
                            "ingredient": str(c["ingredient"]).strip(),
                            "amount": float(c["amount"]),
                        })
                    except (ValueError, TypeError):
                        pass
            result[key] = clean
        else:
            max_len = 80 if key == "target_product" else 20
            result[key] = _dedupe_context_phrases(values, max_len=max_len)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2-A. 제형 목적 확정 (명령 1)
# ─────────────────────────────────────────────────────────────────────────────

def classify_formulation_type(
    formulation_terms: list[str],
    query: str,
    bedrock_client=None,
    model_id: str = "",
    max_attempts: int = 3,
) -> str:
    """
    formulation 슬롯 값을 기반으로 제형 타입을 분류한다.
    confidence low / 실패 시 사용자 입력을 받아 재추정. 최대 max_attempts회.
    끝까지 high confidence가 나오지 않으면 Claude 최근 추정값을 임의 배정.
    """
    if not bedrock_client or not model_id:
        console.print("[yellow]Bedrock 클라이언트 없음 — 제형 타입 임의 배정: 가용화[/yellow]")
        return "가용화"

    types_str = " / ".join(FORMULATION_TYPES)
    extra_context = ""
    best_guess: str | None = None

    for attempt in range(max_attempts):
        context_lines = [
            f"사용자 질의: {query}",
            f"제형 표현: {', '.join(formulation_terms) if formulation_terms else '없음'}",
        ]
        if extra_context:
            context_lines.append(f"추가 설명: {extra_context}")

        prompt = (
            "다음 화장품 정보를 보고 제형 타입을 분류하세요.\n\n"
            "[분류 기준]\n"
            "- O/W 유화: 수중유 에멀전 (유화 에센스, 로션, 크림 등 — 뿌옇거나 흰색)\n"
            "- W/O 유화: 유중수 에멀전 (선크림, 오일리 크림 등)\n"
            "- 가용화: 투명한 수용성 제형 (투명 에센스, 토너, 앰플 등)\n"
            "- 겔: 겔 또는 젤 질감의 제형\n"
            "- 폼: 거품 또는 폼 타입\n"
            "- 기타: 위 분류에 해당하지 않는 경우\n\n"
            f"[입력 정보]\n{chr(10).join(context_lines)}\n\n"
            f"JSON만 반환:\n"
            f"{{\"type\": \"{types_str} 중 하나\", "
            f"\"confidence\": \"high 또는 low\", "
            f"\"reason\": \"분류 근거 한 줄\"}}\n"
            "type에는 분류 기준 이름 그대로 입력. "
            "명확히 특정되면 confidence=high, 추정이면 low."
        )

        try:
            result, _ = _invoke_bedrock_json(
                bedrock_client,
                model_id,
                prompt,
                max_tokens=200,
            )

            ft         = result.get("type", "")
            confidence = result.get("confidence", "low")
            reason     = result.get("reason", "")

            if ft in FORMULATION_TYPES:
                best_guess = ft
                console.print(f"[dim]  제형 타입 추정: {ft} (confidence={confidence}, {reason})[/dim]")
                if confidence == "high":
                    console.print(f"[green]✓ 제형 타입 확정: {ft}[/green]")
                    return ft
            else:
                console.print(f"[dim]  제형 타입 추정 결과가 기준 외: '{ft}'[/dim]")

        except Exception as e:
            console.print(f"[yellow]제형 타입 분류 Claude 호출 실패 (시도 {attempt + 1}): {e}[/yellow]")

        if attempt < max_attempts - 1:
            console.print(
                "[yellow]제형 타입을 명확히 판단하기 어렵습니다. "
                "추가 설명을 입력하면 재시도합니다 "
                "(예: '투명한 물 타입', '흰색 유화 타입').[/yellow]"
            )
            try:
                user_input = input("추가 설명 (Enter 시 현재 추정값 적용): ").strip()
                if not user_input:
                    break
                extra_context = user_input
            except EOFError:
                break

    fallback = best_guess or FORMULATION_TYPES[0]
    console.print(f"[yellow]제형 타입 임의 배정: {fallback}[/yellow]")
    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# 2-B. 구조 성분 선정 (명령 2)
# ─────────────────────────────────────────────────────────────────────────────

def select_structural_ingredients(
    formulation_type: str,
    df: "pd.DataFrame",
    stats: dict,
) -> dict:
    """
    제형 타입에 따라 FORMULATION_STRUCTURAL_MAP에서 필요 카테고리를 조회하고
    data.csv에서 구조 성분 후보를 선정한다.
    """
    required_categories = FORMULATION_STRUCTURAL_MAP.get(
        formulation_type, FORMULATION_STRUCTURAL_MAP["기타"]
    )
    ist = stats["ingredient_stats"]

    struct_df = df[df["ingredient_function"].isin(required_categories)].copy()

    candidates: list[dict] = []
    seen: set[str] = set()

    for _, row in struct_df[["ingredient_name", "ingredient_function"]].drop_duplicates(
        "ingredient_name"
    ).iterrows():
        name = row["ingredient_name"]
        if name in seen or name not in ist:
            continue
        seen.add(name)
        s = ist[name]
        candidates.append({
            "name":      name,
            "category":  row["ingredient_function"],
            "frequency": s["frequency"],
            "median":    s["median"],
            "max":       s["max"],
        })

    for name in _KNOWN_BASE:
        if name not in seen and name in ist:
            seen.add(name)
            s = ist[name]
            candidates.append({
                "name":      name,
                "category":  "base(기본)",
                "frequency": s["frequency"],
                "median":    s["median"],
                "max":       s["max"],
            })

    candidates.sort(key=lambda x: -x["frequency"])

    console.print(
        f"[green]✓ 구조 성분 선정: {len(candidates)}종 "
        f"(제형: {formulation_type}, "
        f"필요 카테고리: {', '.join(required_categories)})[/green]"
    )
    return {
        "formulation_type":    formulation_type,
        "required_categories": required_categories,
        "candidates":          candidates[:25],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2-C. 액티브 성분 물성 검토 (명령 3)
# ─────────────────────────────────────────────────────────────────────────────

# 사용자 제형 표현 → (충돌 판단할 물성 키, 값, 경고 메시지)
FEEL_CONFLICT_MAP: dict[str, tuple[str, object, str]] = {
    "끈적이지 않는": ("viscosity_impact", True,   "끈적임 유발 가능 — 사용자 요구(끈적이지 않는)와 충돌"),
    "산뜻한":        ("viscosity_impact", True,   "끈적임 유발 가능 — 사용자 요구(산뜻한)와 충돌"),
    "산뜻하게":      ("viscosity_impact", True,   "끈적임 유발 가능 — 사용자 요구(산뜻하게)와 충돌"),
    "가벼운":        ("viscosity_impact", True,   "점도 영향으로 무거운 사용감 우려 — 사용자 요구(가벼운)와 충돌"),
    "가볍게":        ("viscosity_impact", True,   "점도 영향으로 무거운 사용감 우려 — 사용자 요구(가볍게)와 충돌"),
    "오일프리":      ("solubility",      "유용성", "유용성 성분 — 오일프리 요구와 충돌"),
}


def _apply_user_feel_conflicts(properties: dict, formulation_terms: list[str]) -> dict:
    """
    review_active_properties 결과에 사용자 사용감 요구를 교차 적용한다.
    Claude가 감지하지 못한 요구 기반 충돌(예: 끈적이지 않는 + viscosity_impact)을
    formulation_conflict 필드에 자동으로 추가한다.
    """
    for term in formulation_terms:
        if term not in FEEL_CONFLICT_MAP:
            continue
        prop_key, prop_val, message = FEEL_CONFLICT_MAP[term]
        for name, props in properties.items():
            if not isinstance(props, dict):
                continue
            if props.get(prop_key) == prop_val and not props.get("formulation_conflict"):
                props["formulation_conflict"] = message
                console.print(
                    f"[yellow]  ⚠ 사용자 요구 충돌 자동 감지 — {name}: {message}[/yellow]"
                )
    return properties


def _fetch_replacements_for_flagged(
    properties: dict,
    formulation_type: str,
    formulation_terms: list[str],
    known_set: set,
    bedrock_client,
    model_id: str,
) -> dict:
    """
    _apply_user_feel_conflicts로 충돌이 새로 태그된 성분 중
    suggested_replacement가 없는 것에 대해 Claude에 대체 성분을 자유롭게 제안받고,
    제안된 성분이 data.csv(known_set)에 실제로 존재하는지 교차 검증한다.
    """
    needs = [
        (name, props["formulation_conflict"])
        for name, props in properties.items()
        if isinstance(props, dict)
        and props.get("formulation_conflict")
        and not props.get("suggested_replacement")
    ]
    if not needs or not bedrock_client:
        return properties

    flagged_str = "\n".join(f"- {name}: {conflict}" for name, conflict in needs)
    terms_str   = ", ".join(formulation_terms) or "없음"

    prompt = (
        f"다음 성분들은 화장품 처방에서 사용자 요구({terms_str}) 및 "
        f"제형 타입({formulation_type})과 충돌합니다.\n\n"
        f"[충돌 성분 및 이유]\n{flagged_str}\n\n"
        "각 성분의 기능(보습, 유화, 점도 조절 등)을 유지하면서 충돌을 해소할 수 있는 "
        "대체 성분을 한국 INCI 표기로 최대 5개 제안하세요.\n"
        "제안은 실제 화장품 원료로 사용되는 성분명이어야 합니다.\n"
        "JSON만 반환 (충돌 성분명을 key로):\n"
        "{\"충돌성분명\": [\"대체성분1\", \"대체성분2\", ...]}"
    )

    try:
        result, _ = _invoke_bedrock_json(
            bedrock_client,
            model_id,
            prompt,
            max_tokens=800,
        )

        for name, replacements in result.items():
            if name not in properties or not isinstance(properties[name], dict):
                continue
            if not isinstance(replacements, list):
                continue

            # data.csv 교차 검증
            valid   = [r for r in replacements if r in known_set]
            invalid = [r for r in replacements if r not in known_set]

            if invalid:
                console.print(
                    f"[dim]    {name} — DB 미등록 제외: {', '.join(invalid)}[/dim]"
                )
            if valid:
                properties[name]["suggested_replacement"] = valid
                console.print(
                    f"[dim]  → {name} 대체 후보 확정: {', '.join(valid)}[/dim]"
                )
            else:
                console.print(
                    f"[yellow]  → {name}: Claude 제안 성분이 모두 DB 미등록 — 대체 후보 없음[/yellow]"
                )

    except Exception as e:
        console.print(f"[yellow]충돌 성분 대체 조회 실패: {e}[/yellow]")

    return properties


def review_active_properties(
    active_ingredient_names: list[str],
    formulation_type: str,
    bedrock_client=None,
    model_id: str = "",
) -> dict:
    """
    탐색된 액티브 성분 목록을 한국 INCI명 그대로 Claude에게 전달해
    물성(수용성/유용성, pH 민감도, 점도 영향, 제형 충돌)을 일괄 조회한다.
    """
    if not bedrock_client or not active_ingredient_names:
        return {}

    names_str = "\n".join(f"- {n}" for n in active_ingredient_names)

    prompt = (
        "아래는 화장품 처방에 사용될 액티브 성분 목록입니다 (한국 INCI 표기).\n"
        f"제형 타입: {formulation_type}\n\n"
        f"[성분 목록]\n{names_str}\n\n"
        "각 성분에 대해 다음 속성을 판단하고 JSON으로 반환하세요:\n"
        "- solubility: '수용성' / '유용성' / '양친성'\n"
        "- ph_sensitive: true / false\n"
        "- ph_requirement: pH 민감 시 적정 범위(예: '3.5~4.5'), 아니면 null\n"
        "- viscosity_impact: true / false\n"
        "- formulation_conflict: 제형 타입과 충돌 가능성 있으면 경고 문구, 없으면 null\n"
        "- regulatory_limit: 화장품법/INCI 기준 최대 허용 함량(%, 숫자만). "
        "알려진 규제 상한이 없으면 null\n"
        "- suggested_replacement: formulation_conflict가 있을 경우 해당 성분을 대체할 수 있는 "
        "INCI명 목록(한국어 표기, 최대 3개). 충돌이 없으면 null\n\n"
        "JSON만 반환 (위에서 제공한 성분명을 key로 글자 그대로 사용, 절대 변경 금지):\n"
        "{\"성분명\": {\"solubility\": \"...\", \"ph_sensitive\": true/false, "
        "\"ph_requirement\": \"...\", \"viscosity_impact\": true/false, "
        "\"formulation_conflict\": \"...또는 null\", "
        "\"regulatory_limit\": 숫자또는null, "
        "\"suggested_replacement\": [\"대체성분1\"] 또는 null}, ...}"
    )

    try:
        properties, _ = _invoke_bedrock_json(
            bedrock_client,
            model_id,
            prompt,
            max_tokens=3000,
        )

        console.print(f"[green]✓ 액티브 성분 물성 검토: {len(properties)}종[/green]")
        for name, props in properties.items():
            if not isinstance(props, dict):
                continue
            parts = []
            if props.get("solubility"):
                parts.append(f"용해성: {props['solubility']}")
            if props.get("ph_sensitive"):
                ph_req = props.get("ph_requirement") or "주의 필요"
                parts.append(f"pH 민감({ph_req})")
            else:
                parts.append("pH 안정")
            if props.get("viscosity_impact"):
                parts.append("점도 영향 있음")
            reg = props.get("regulatory_limit")
            if isinstance(reg, (int, float)):
                parts.append(f"규제 상한 {reg}%")
            prop_str = ", ".join(parts)
            conflict = props.get("formulation_conflict")
            replacement = props.get("suggested_replacement")
            if conflict:
                repl_str = (
                    f" → 대체 후보: {', '.join(replacement)}"
                    if isinstance(replacement, list) and replacement else ""
                )
                console.print(f"[dim]  · {name}: {prop_str}[/dim]")
                console.print(f"[yellow]    ⚠ 제형 충돌: {conflict}{repl_str}[/yellow]")
            else:
                console.print(f"[dim]  · {name}: {prop_str}[/dim]")
        return properties

    except Exception as e:
        console.print(f"[yellow]물성 검토 Claude 호출 실패: {e}[/yellow]")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 3. 성분명 유사도 매핑
# ─────────────────────────────────────────────────────────────────────────────

class IngredientMapper:
    def __init__(self, known_ingredients, embed_threshold=0.82, lev_threshold=3):
        self.known       = known_ingredients
        self.known_norm  = [self._norm(i) for i in known_ingredients]
        self.embed_thr   = embed_threshold
        self.lev_thr     = lev_threshold
        self._model      = None
        self._embeddings = None
        self._load_embeddings()

    @staticmethod
    def _norm(s):
        return re.sub(r"[\s\-_·•]", "", str(s)).lower()

    def _load_embeddings(self):
        try:
            from sentence_transformers import SentenceTransformer

            console.print("[dim]임베딩 모델 로딩...[/dim]")

            model_name = "snunlp/KR-SBERT-V40K-klueNLI-augSTS"
            model_dir  = Path("./models/KR-SBERT-V40K-klueNLI-augSTS")

            if model_dir.exists():
                console.print("[dim]로컬 임베딩 모델 사용...[/dim]")
                self._model = SentenceTransformer(str(model_dir), local_files_only=True)
            else:
                console.print("[dim]로컬 모델 없음 → Hugging Face에서 다운로드...[/dim]")
                self._model = SentenceTransformer(model_name)
                self._model.save(str(model_dir))
                console.print(f"[green]✓ 모델 로컬 저장 완료: {model_dir}[/green]")

            self._embeddings = self._model.encode(
                self.known,
                normalize_embeddings=True,
                show_progress_bar=False
            )
            console.print("[green]✓ 임베딩 모델 로드[/green]")

        except ImportError as e:
            console.print("[yellow]sentence-transformers 미설치 → 편집거리 매핑 사용[/yellow]")
            console.print(f"[red]{type(e).__name__}: {e}[/red]")
        except Exception as e:
            console.print("[yellow]임베딩 모델 로딩 실패 → 편집거리 매핑 사용[/yellow]")
            console.print(f"[red]{type(e).__name__}: {e}[/red]")

    def map(self, query: str):
        qn = self._norm(query)
        if qn in self.known_norm:
            return self.known[self.known_norm.index(qn)], 1.0, "exact"
        try:
            from Levenshtein import distance as ld
            dists = [(ld(qn, k), i) for i, k in enumerate(self.known_norm)]
            bd, bi = min(dists, key=lambda x: x[0])
            if bd <= self.lev_thr:
                return self.known[bi], round(1 - bd/10, 3), "levenshtein"
        except ImportError:
            pass
        if self._model is not None:
            qe   = self._model.encode([query], normalize_embeddings=True)
            sims = (self._embeddings @ qe.T).flatten()
            bi   = int(np.argmax(sims)); sc = float(sims[bi])
            if sc >= self.embed_thr:
                return self.known[bi], round(sc, 3), "embedding"
        return None, 0.0, "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# 4. 맥락 표현 → 성분명 변환
# ─────────────────────────────────────────────────────────────────────────────

class ConceptExpander:
    """
    성분 슬롯 표현(비타민C, 초저분자 히아루론산 등) → data.csv 성분명 3단계 cascade 탐색
      1단계: 정규화 exact match (띄어쓰기 제거 + 소문자화, 특수문자는 유지)
      2단계: 임베딩 유사도 (보수적 임계값 0.90)
      3단계: Claude 소규모 호출 → DB 목록에서 후보 재선택 → 화이트리스트 교차 검증
    모든 단계에서 data.csv 화이트리스트 검증 후 결과 반환
    """

    EMBED_THRESHOLD = 0.90  # 잘못된 매핑 방지를 위한 보수적 임계값

    def __init__(
        self,
        known_ingredients: list,
        mapper: IngredientMapper,
        bedrock_client=None,
        model_id: str = "",
    ):
        self.known     = known_ingredients
        self.known_set = set(known_ingredients)
        self.mapper    = mapper
        self.bedrock   = bedrock_client
        self.model_id  = model_id
        self.last_search_logs = []
        self.known_exact = {}
        for known in known_ingredients:
            self.known_exact.setdefault(self._exact_norm(known), []).append(known)

    @staticmethod
    def _exact_norm(s: str) -> str:
        return re.sub(r"\s+", "", str(s)).lower()

    # ── 1단계: 정규화 exact match ─────────────────────────────────────────
    def _exact_expand(self, term: str) -> list[str]:
        return self.known_exact.get(self._exact_norm(term), [])

    # ── 2단계: 임베딩 유사도 (보수적 임계값) ─────────────────────────────
    def _embed_expand(self, term: str) -> list[str]:
        if self.mapper._model is None or self.mapper._embeddings is None:
            return []
        try:
            qe   = self.mapper._model.encode([term], normalize_embeddings=True)
            sims = (self.mapper._embeddings @ qe.T).flatten()
            return [
                self.known[i]
                for i in np.argsort(sims)[::-1][:5]
                if float(sims[i]) >= self.EMBED_THRESHOLD
            ]
        except Exception:
            return []

    # ── 3단계: Claude 소규모 호출 ─────────────────────────────────────────
    def _claude_expand(self, query: str, unmapped_terms: list[str]) -> dict[str, list[str]]:
        if not self.bedrock or not unmapped_terms:
            return {}

        terms_str  = "\n".join(f"- {t}" for t in unmapped_terms)
        known_list = "\n".join(self.known)
        hints_section = _build_alias_hint_section(
            [query, *unmapped_terms],
            "성분 이명 힌트 — 아래 관용명이 DB에서 어떤 성분명으로 등록됐는지 참고",
        )

        prompt = (
            "다음은 화장품 처방 DB에 등록된 성분명 전체 목록입니다.\n\n"
            f"[DB 성분 목록]\n{known_list}\n"
            f"{hints_section}\n"
            "아래 표현에 대해 위 목록 안에서 가장 일치하는 실제 성분명을 다시 찾으세요.\n"
            "**목록에 있는 성분명만 사용. 없는 성분명 생성 절대 금지.**\n"
            "**새 성분명을 추론하거나 일반 개념을 설명하지 말고, DB 목록 안에서만 재선택하세요.**\n\n"
            f"[매핑 대상 (쿼리 맥락: \"{query}\")]\n{terms_str}\n\n"
            "JSON만 반환 (표현당 최대 5개, 매핑 불가 시 빈 배열):\n"
            "{\"표현1\": [\"성분명A\"], \"표현2\": [\"성분명B\", \"성분명C\"]}"
        )

        try:
            result, _ = _invoke_bedrock_json(
                self.bedrock,
                self.model_id,
                prompt,
                max_tokens=1500,
            )
            return {
                k: [c for c in v if c in self.known_set]
                for k, v in result.items()
                if isinstance(v, list)
            }
        except Exception as e:
            console.print(f"[yellow]성분명 탐색 Claude 호출 실패: {e}[/yellow]")
            return {}

    # ── 메인 ─────────────────────────────────────────────────────────────
    def expand(self, query: str, query_contexts: dict | None = None) -> list[tuple[str, str, float, str]]:
        """
        성분 슬롯 맥락 표현 → data.csv 성분명 (3단계 cascade)
        Returns: [(원래표현, 매핑성분명, 신뢰도, 방법), ...]
        """
        self.last_search_logs = []
        results    = []
        mapped_set = set()  # 중복 방지
        unmapped   = []
        query_contexts = query_contexts or {}
        ingredient_terms = _dedupe_context_phrases(query_contexts.get("ingredients", []), max_len=30)

        for tok in ingredient_terms:
            # 1단계
            hits = self._exact_expand(tok)
            if hits:
                self.last_search_logs.append(f"  - '{tok}' 1단계(exact): {hits}")
                for h in hits:
                    if h not in mapped_set:
                        results.append((tok, h, 1.0, "concept_exact"))
                        mapped_set.add(h)
                continue
            self.last_search_logs.append(f"  - '{tok}' 1단계(exact): 없음")

            # 2단계
            hits = self._embed_expand(tok)
            if hits:
                self.last_search_logs.append(f"  - '{tok}' 2단계(임베딩): {hits}")
                for h in hits:
                    if h not in mapped_set:
                        results.append((tok, h, self.EMBED_THRESHOLD, "concept_embed"))
                        mapped_set.add(h)
                continue
            self.last_search_logs.append(f"  - '{tok}' 2단계(임베딩): 없음")

            unmapped.append(tok)

        if unmapped:
            self.last_search_logs.append(f"  - 3단계(Claude) 탐색 대상: {unmapped}")
            claude_map = self._claude_expand(query, unmapped)
            if not claude_map:
                self.last_search_logs.append("  - 3단계(Claude) 결과: 없음")
            for term in unmapped:
                cands = claude_map.get(term, [])
                self.last_search_logs.append(
                    f"  - '{term}' 3단계(Claude): {cands if cands else '없음'}"
                )
                for c in cands:
                    if c not in mapped_set:
                        results.append((term, c, 0.85, "concept_claude"))
                        mapped_set.add(c)
        elif ingredient_terms:
            self.last_search_logs.append("  - 3단계(Claude): 탐색 불필요")
        else:
            self.last_search_logs.append("  - 성분 표현이 없어 성분명 탐색 생략")

        return results

    def normalize_terms_preserve_order(self, terms: list[str], query_label: str = "") -> list[dict]:
        """
        타사 제품 representation_ingredients 정규화용.
        기존 3단계 탐색을 쓰되 입력 순서와 원본명을 보존하고, 성분당 대표 매칭 1개만 반환한다.
        """
        cleaned_terms = [str(t).strip() for t in terms if str(t).strip()]
        results: list[dict | None] = [None] * len(cleaned_terms)
        unmapped: list[tuple[int, str]] = []
        stage_logs: dict[int, list[str]] = {idx: [] for idx in range(len(cleaned_terms))}

        for idx, term in enumerate(cleaned_terms):
            hits = self._exact_expand(term)
            if hits:
                stage_logs[idx].append(f"1단계(exact): {hits}")
                results[idx] = {
                    "original_name": term,
                    "normalized_name": hits[0],
                    "normalized": True,
                    "score": 1.0,
                    "method": "exact",
                    "applied_stage": "1단계(exact)",
                }
                continue
            stage_logs[idx].append("1단계(exact): 없음")

            hits = self._embed_expand(term)
            if hits:
                stage_logs[idx].append(f"2단계(임베딩): {hits}")
                results[idx] = {
                    "original_name": term,
                    "normalized_name": hits[0],
                    "normalized": True,
                    "score": self.EMBED_THRESHOLD,
                    "method": "embedding",
                    "applied_stage": "2단계(임베딩)",
                }
                continue
            stage_logs[idx].append("2단계(임베딩): 없음")

            unmapped.append((idx, term))

        if unmapped:
            for idx, _ in unmapped:
                stage_logs[idx].append("3단계(Claude): 탐색 대상")
            claude_map = self._claude_expand(
                query_label or "타사 제품 대표 성분 정규화",
                [term for _, term in unmapped],
            )
            for idx, term in unmapped:
                cands = claude_map.get(term, [])
                if cands:
                    stage_logs[idx].append(f"3단계(Claude): {cands}")
                    results[idx] = {
                        "original_name": term,
                        "normalized_name": cands[0],
                        "normalized": True,
                        "score": 0.85,
                        "method": "claude",
                        "applied_stage": "3단계(Claude)",
                    }
                else:
                    stage_logs[idx].append("3단계(Claude): 없음")
                    results[idx] = {
                        "original_name": term,
                        "normalized_name": term,
                        "normalized": False,
                        "score": 0.0,
                        "method": "unmapped",
                        "applied_stage": "미정규화",
                    }

        total = max(len(cleaned_terms), 1)
        normalized_rows = []
        for idx, item in enumerate(results):
            if item is None:
                continue
            item["rank"] = idx + 1
            item["order_weight"] = round((total - idx) / total, 4)
            item["normalization_log"] = stage_logs.get(idx, [])
            normalized_rows.append(item)
        return normalized_rows


# ─────────────────────────────────────────────────────────────────────────────
# 5. 타겟 제품 탐색
# ─────────────────────────────────────────────────────────────────────────────

def _strip_text(value) -> str:
    return str(value).strip() if not pd.isna(value) else ""


def _compact_for_partial_match(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).lower()


def _parse_representation_ingredients(value) -> list[str]:
    if pd.isna(value):
        return []
    return [part.strip() for part in str(value).split("|") if part.strip()]


def _base_time_sort_key(candidate: dict):
    parsed = pd.to_datetime(candidate.get("base_time", ""), errors="coerce")
    if pd.isna(parsed):
        return pd.Timestamp.min
    return parsed


TARGET_PRODUCT_TOKEN_STOPWORDS = {
    "세럼", "에센스", "로션", "크림", "앰플", "토너", "스킨", "젤", "겔", "밤", "오일",
    "미스트", "패드", "마스크", "팩", "폼", "클렌저", "클렌징", "워시", "선크림",
    "선", "선블록", "선스크린", "리필", "기획", "대용량", "본품", "증정", "세트",
}


def _tokenize_target_product_name(value: str) -> list[str]:
    text = str(value).lower()
    raw_tokens = re.split(r"[\s\[\]\(\)\{\}/,+:_\-·•&|]+", text)
    tokens = []
    seen = set()
    for token in raw_tokens:
        token = token.strip(" .!?'\"")
        if not token:
            continue
        if token in TARGET_PRODUCT_TOKEN_STOPWORDS:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?(?:ml|g|매|개|호|%)?", token):
            continue
        if len(token) < 2:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _token_match_score(query_tokens: list[str], candidate_text: str) -> float:
    if not query_tokens:
        return 0.0
    candidate_tokens = set(_tokenize_target_product_name(candidate_text))
    if not candidate_tokens:
        return 0.0
    matched = sum(
        1
        for token in query_tokens
        if any(token in cand for cand in candidate_tokens)
    )
    return matched / len(query_tokens)


def _prompt_select_candidate(
    title: str,
    candidates: list[dict],
    formatter,
    allow_none: bool = False,
    max_display: int = 10,
) -> dict | None:
    total = len(candidates)
    candidates = candidates[:max_display]

    console.print(f"[yellow]{title}[/yellow]")
    if total > max_display:
        console.print(f"[dim]  (전체 {total}건 중 유력 후보 {max_display}건만 표시)[/dim]")
    if allow_none:
        console.print("찾으시는 제품이 없으면 '없음'을 입력해주세요")
    for idx, cand in enumerate(candidates, start=1):
        console.print(f"  {idx}. {formatter(cand)}")

    while True:
        try:
            raw = input("선택할 번호를 입력하세요(취소: Enter): ").strip()
        except EOFError:
            console.print("[red]사용자 선택을 받을 수 없어 타겟 제품 확정을 중단합니다.[/red]")
            return None
        if allow_none and raw == "없음":
            console.print("[yellow]타겟 제품 후보에서 선택하지 않았습니다.[/yellow]")
            return None
        if raw == "":
            console.print("[yellow]타겟 제품 선택이 취소되었습니다.[/yellow]")
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        console.print(f"[red]1~{len(candidates)} 사이 번호를 입력하세요.[/red]")


def _formula_target_from_code(formula_dict: dict, bulk_code: str, query_value: str) -> dict:
    fd = formula_dict[bulk_code]
    return {
        "source": "internal",
        "query": query_value,
        "bulk_code": bulk_code,
        "name": fd["name"],
        "ingredients": fd["ingredients"],
        "structural_roles": fd.get("structural_roles", {}),
    }


def _find_internal_target_product(
    df: pd.DataFrame,
    formula_dict: dict,
    target_product: str,
) -> tuple[dict | None, str]:
    target = _strip_text(target_product)
    target_norm = _compact_for_partial_match(target)
    if not target_norm:
        return None, "자사 제품 부분일치: 실패 (정규화된 검색어가 비어 있음)"

    matches = df[
        df["bulk_name"].map(lambda name: target_norm in _compact_for_partial_match(name))
    ]
    if matches.empty:
        return None, "자사 제품 부분일치: 없음"

    # event_time 기준 최신 순 정렬 (컬럼이 없으면 원래 순서 유지)
    has_event_time = "event_time" in matches.columns
    cols = ["bulk_code", "bulk_name"] + (["event_time"] if has_event_time else [])
    matches_dedup = (
        matches[cols]
        .assign(event_time=lambda d: pd.to_datetime(d["event_time"], errors="coerce"))
        .sort_values("event_time", ascending=False)
        .drop_duplicates("bulk_code")
        if has_event_time
        else matches[cols].drop_duplicates("bulk_code")
    )

    candidates = []
    for _, row in matches_dedup.iterrows():
        code = row["bulk_code"]
        if code in formula_dict:
            cand = {"bulk_code": code, "bulk_name": row["bulk_name"]}
            if has_event_time:
                cand["event_time"] = str(row["event_time"])[:10]
            candidates.append(cand)

    if len(candidates) == 1:
        code = candidates[0]["bulk_code"]
        return (
            _formula_target_from_code(formula_dict, code, target),
            f"자사 제품 부분일치: 1건 → 타겟 처방 확정 ({candidates[0]['bulk_name']}, bulk_code: {code})",
        )

    if len(candidates) > 1:
        selected = _prompt_select_candidate(
            "자사 제품 부분일치 결과가 복수입니다. 자동 선택하지 않습니다.",
            candidates,
            lambda c: f"{c['bulk_name']} (bulk_code: {c['bulk_code']})",
        )
        if not selected:
            return (
                {"source": "unresolved_internal_selection", "query": target},
                f"자사 제품 부분일치: {len(candidates)}건, 후보 선택 미완료",
            )
        return (
            _formula_target_from_code(formula_dict, selected["bulk_code"], target),
            f"자사 제품 부분일치: {len(candidates)}건 → 타겟 처방 확정 "
            f"({selected['bulk_name']}, bulk_code: {selected['bulk_code']})",
        )

    return None, "자사 제품 부분일치: 없음"


def _find_external_target_product(
    external_csv: str,
    target_product: str,
    expander: ConceptExpander,
) -> tuple[dict | None, str]:
    if not external_csv or not Path(external_csv).exists():
        return None, f"타사 제품 부분일치: 실패 (external.csv 파일 없음: {external_csv})"

    try:
        ext = pd.read_csv(external_csv, engine="python")
    except Exception as e:
        return None, f"타사 제품 부분일치: 실패 (external.csv 로드 오류: {e})"

    required = {"title", "representation_ingredients", "base_time"}
    if missing := required - set(ext.columns):
        return None, f"타사 제품 부분일치: 실패 (external.csv 필수 컬럼 누락: {missing})"

    target_norm = _compact_for_partial_match(target_product)
    if not target_norm:
        return None, "타사 제품 부분일치: 실패 (정규화된 검색어가 비어 있음)"

    matches = ext[
        ext["title"].map(lambda title: target_norm in _compact_for_partial_match(title))
    ].copy()
    if matches.empty:
        return None, "타사 제품 부분일치: 없음"

    candidates = []
    for _, row in matches.drop_duplicates(["title", "base_time"]).iterrows():
        candidates.append({
            "title": row["title"],
            "representation_ingredients": row["representation_ingredients"],
            "base_time": row["base_time"],
        })
    candidates.sort(key=_base_time_sort_key, reverse=True)

    if len(candidates) == 1:
        selected = candidates[0]
    else:
        selected = _prompt_select_candidate(
            "타사 제품 부분일치 결과가 복수입니다. 자동 선택하지 않습니다.",
            candidates,
            lambda c: f"{c['title']} (base_time: {c['base_time']})",
        )
        if not selected:
            return None, f"타사 제품 부분일치: {len(candidates)}건, 후보 선택 미완료"

    target = _external_target_from_candidate(selected, target_product, expander)

    return target, (
        f"타사 제품 부분일치: {len(candidates)}건 → 타겟 제품 확정 "
        f"({selected['title']}, base_time: {selected['base_time']}, "
        f"대표 성분 {target['raw_ingredient_count']}개)"
    )


def _external_target_from_candidate(
    selected: dict,
    target_product: str,
    expander: ConceptExpander,
) -> dict:
    raw_ingredients = _parse_representation_ingredients(selected["representation_ingredients"])
    normalized = expander.normalize_terms_preserve_order(
        raw_ingredients,
        query_label=f"타사 제품 성분 정규화: {selected['title']}",
    )

    return {
        "source": "external",
        "query": target_product,
        "title": selected["title"],
        "base_time": selected["base_time"],
        "ingredients": normalized,
        "raw_ingredient_count": len(raw_ingredients),
    }


def _collect_token_target_candidates(
    df: pd.DataFrame,
    formula_dict: dict,
    external_csv: str,
    target_product: str,
) -> tuple[list[dict], str]:
    query_tokens = _tokenize_target_product_name(target_product)
    if not query_tokens:
        return [], "토큰 기반 탐색: 실패 (검색 토큰 없음)"

    candidates = []

    has_event_time = "event_time" in df.columns
    internal_cols = ["bulk_code", "bulk_name"] + (["event_time"] if has_event_time else [])
    internal_df = (
        df[internal_cols]
        .assign(event_time=lambda d: pd.to_datetime(d["event_time"], errors="coerce"))
        .sort_values("event_time", ascending=False)
        .drop_duplicates("bulk_code")
        if has_event_time
        else df[internal_cols].drop_duplicates("bulk_code")
    )
    internal_seen = set()
    for _, row in internal_df.iterrows():
        code = row["bulk_code"]
        if code not in formula_dict or code in internal_seen:
            continue
        score = _token_match_score(query_tokens, row["bulk_name"])
        if score >= 0.80:
            internal_seen.add(code)
            cand: dict = {
                "source": "internal",
                "score": score,
                "bulk_code": code,
                "bulk_name": row["bulk_name"],
            }
            if has_event_time:
                cand["event_time"] = row["event_time"]
            candidates.append(cand)

    external_status = ""
    if external_csv and Path(external_csv).exists():
        try:
            ext = pd.read_csv(external_csv, engine="python")
            required = {"title", "representation_ingredients", "base_time"}
            if missing := required - set(ext.columns):
                external_status = f" / external.csv 컬럼 누락 {missing}"
            else:
                external_seen = set()
                for _, row in ext.drop_duplicates(["title", "base_time"]).iterrows():
                    key = (row["title"], row["base_time"])
                    if key in external_seen:
                        continue
                    score = _token_match_score(query_tokens, row["title"])
                    if score >= 0.80:
                        external_seen.add(key)
                        candidates.append({
                            "source": "external",
                            "score": score,
                            "title": row["title"],
                            "representation_ingredients": row["representation_ingredients"],
                            "base_time": row["base_time"],
                        })
        except Exception as e:
            external_status = f" / external.csv 로드 오류 {e}"
    else:
        external_status = f" / external.csv 파일 없음 {external_csv}"

    def sort_key(candidate: dict):
        source_rank = 0 if candidate["source"] == "internal" else 1
        if candidate["source"] == "external":
            time_val = _base_time_sort_key(candidate)
        else:
            time_val = candidate.get("event_time")
            if not isinstance(time_val, pd.Timestamp) or pd.isna(time_val):
                time_val = pd.Timestamp.min
        return (-candidate["score"], source_rank, -time_val.value)

    candidates.sort(key=sort_key)
    return candidates, (
        f"토큰 기반 탐색: {len(candidates)}건 "
        f"(검색 토큰: {', '.join(query_tokens)}){external_status}"
    )


def _select_token_target_candidate(
    candidates: list[dict],
    target_product: str,
    formula_dict: dict,
    expander: ConceptExpander,
) -> tuple[dict | None, str]:
    if not candidates:
        return None, "토큰 기반 탐색: 없음"

    selected = _prompt_select_candidate(
        "토큰 기반 탐색 후보입니다. 자동 선택하지 않습니다.",
        candidates,
        lambda c: (
            f"[자사] {c['bulk_name']} (bulk_code: {c['bulk_code']}, match {c['score']*100:.0f}%)"
            if c["source"] == "internal"
            else f"[타사] {c['title']} (base_time: {c['base_time']}, match {c['score']*100:.0f}%)"
        ),
        allow_none=True,
    )
    if not selected:
        return None, f"토큰 기반 탐색: {len(candidates)}건, 후보 선택 없음"

    if selected["source"] == "internal":
        return (
            _formula_target_from_code(formula_dict, selected["bulk_code"], target_product),
            f"토큰 기반 탐색: {len(candidates)}건 → 자사 타겟 처방 확정 "
            f"({selected['bulk_name']}, bulk_code: {selected['bulk_code']})",
        )

    target = _external_target_from_candidate(selected, target_product, expander)
    return target, (
        f"토큰 기반 탐색: {len(candidates)}건 → 타사 타겟 제품 확정 "
        f"({selected['title']}, base_time: {selected['base_time']}, "
        f"대표 성분 {target['raw_ingredient_count']}개)"
    )


def find_target_product(
    target_products: list[str],
    df: pd.DataFrame,
    formula_dict: dict,
    external_csv: str,
    expander: ConceptExpander,
) -> dict | None:
    for target_product in target_products or []:
        target_product = _strip_text(target_product)
        if not target_product:
            continue

        console.print(f"[dim]타겟 제품 탐색: {target_product}[/dim]")

        target, internal_log = _find_internal_target_product(df, formula_dict, target_product)
        console.print(f"[dim]  {internal_log}[/dim]")
        if target:
            if target.get("source") == "unresolved_internal_selection":
                console.print("[yellow]자사 제품 후보 선택이 완료되지 않아 타겟 제품 탐색을 중단합니다.[/yellow]")
                return None
            return target

        target, external_log = _find_external_target_product(external_csv, target_product, expander)
        if target:
            unmapped = [i for i in target["ingredients"] if not i["normalized"]]
            console.print(
                f"[dim]  {external_log}, 미정규화 {len(unmapped)}개[/dim]"
            )
            for ing in target["ingredients"]:
                status = "정규화" if ing["normalized"] else "미정규화"
                console.print(
                    f"[dim]    {ing['rank']}. {ing['original_name']} → "
                    f"{ing['normalized_name']} "
                    f"({status}, {ing.get('applied_stage', '')}, weight {ing['order_weight']:.4f})[/dim]"
                )
                for log_line in ing.get("normalization_log", []):
                    console.print(f"[dim]       - {log_line}[/dim]")
            return target

        console.print(f"[dim]  {external_log}[/dim]")

        token_candidates, token_log = _collect_token_target_candidates(
            df,
            formula_dict,
            external_csv,
            target_product,
        )
        console.print(f"[dim]  {token_log}[/dim]")
        target, token_select_log = _select_token_target_candidate(
            token_candidates,
            target_product,
            formula_dict,
            expander,
        )
        if target:
            if target.get("source") == "external":
                unmapped = [i for i in target["ingredients"] if not i["normalized"]]
                console.print(f"[dim]  {token_select_log}, 미정규화 {len(unmapped)}개[/dim]")
                for ing in target["ingredients"]:
                    status = "정규화" if ing["normalized"] else "미정규화"
                    console.print(
                        f"[dim]    {ing['rank']}. {ing['original_name']} → "
                        f"{ing['normalized_name']} "
                        f"({status}, {ing.get('applied_stage', '')}, weight {ing['order_weight']:.4f})[/dim]"
                    )
                    for log_line in ing.get("normalization_log", []):
                        console.print(f"[dim]       - {log_line}[/dim]")
            else:
                console.print(f"[dim]  {token_select_log}[/dim]")
            return target

        console.print(f"[dim]  {token_select_log}[/dim]")
        console.print(f"[yellow]타겟 제품 탐색 실패: {target_product}[/yellow]")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 6. 통계 분석
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
# 7. 컨텍스트 구성
# ─────────────────────────────────────────────────────────────────────────────

def build_context(
    query: str,
    stats: dict,
    keyword_db: dict,
    formula_dict: dict,
    query_contexts: dict | None = None,
    concept_ings: list | None = None,
    target_formula: dict | None = None,
    structural_info: dict | None = None,
    top_base: int = 15,
    top_active: int = 15,
) -> dict:
    ist = stats["ingredient_stats"]
    query_contexts = query_contexts or _empty_query_contexts()

    # 1) 성분명 탐색 결과 반영
    query_matched_ings = []
    existing_matched = set()
    if concept_ings:
        for orig, matched, score, method in concept_ings:
            if matched not in existing_matched:
                query_matched_ings.append((orig, matched, score))
                existing_matched.add(matched)

    query_ing_names = {m for _, m, _ in query_matched_ings}
    target_ing_names = set()
    target_exclude_codes = set()
    if target_formula:
        if target_formula.get("source") == "internal":
            target_ing_names.update(target_formula.get("ingredients", {}).keys())
            if target_formula.get("bulk_code"):
                target_exclude_codes.add(target_formula["bulk_code"])
        elif target_formula.get("source") == "external":
            for ing in target_formula.get("ingredients", []):
                normalized = ing.get("normalized_name", "")
                if normalized in ist:
                    target_ing_names.add(normalized)

    # 2) 마케팅 키워드 매핑
    search_texts = [query]
    if query_contexts["formulation"]:
        search_texts.append(" ".join(query_contexts["formulation"]))
    if query_contexts["marketing_points"]:
        search_texts.append(" ".join(query_contexts["marketing_points"]))

    matched_keywords = []
    seen_keywords = set()
    for search_text in search_texts:
        for kw, data in match_query_keywords(search_text, keyword_db):
            if kw in seen_keywords:
                continue
            matched_keywords.append((kw, data))
            seen_keywords.add(kw)
    keyword_ing_names = set()
    keyword_aspects   = set(query_contexts["marketing_points"])
    for kw, kdata in matched_keywords:
        top_kw_ings = [ing for ing, _ in kdata["ingredients"].most_common(8)]
        keyword_ing_names.update(top_kw_ings)
        keyword_aspects.update(kdata["aspects"])

    # 3) base / active 성분 분리
    base_ings   = []
    active_ings = []
    priority_names = query_ing_names | keyword_ing_names | target_ing_names

    for name, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"]):
        role  = s["structural_role"]
        entry = {"name": name, **s}
        if role in BASE_ROLES or name in _KNOWN_BASE:
            if len(base_ings) < top_base:
                base_ings.append(entry)
        else:
            if name in priority_names:
                active_ings.insert(0, entry)
            elif len(active_ings) < top_active:
                active_ings.append(entry)

    if len(active_ings) < top_active:
        exist = {a["name"] for a in active_ings}
        extras = [
            {"name": n, **s}
            for n, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"])
            if s["structural_role"] not in BASE_ROLES
            and n not in _KNOWN_BASE and n not in exist
        ]
        active_ings += extras[:top_active - len(active_ings)]

    # 4) 키워드 연결 처방을 few-shot 후보로 선정
    keyword_formula_codes = set()
    for _, kdata in matched_keywords:
        keyword_formula_codes.update(kdata["formula_codes"])

    structural_names = (
        {c["name"] for c in structural_info["candidates"]}
        if structural_info else set()
    )
    similar_formulas = _pick_similar(
        formula_dict, query_ing_names | target_ing_names,
        priority_codes=keyword_formula_codes,
        n=3,
        exclude_codes=target_exclude_codes,
        structural_names=structural_names,
    )

    # 5) 키워드별 대표 성분 요약
    keyword_summary = []
    for kw, kdata in matched_keywords:
        top_ings   = [ing for ing, _ in kdata["ingredients"].most_common(5)]
        aspects    = list(kdata["aspects"])[:4]
        n_formulas = len(kdata["formula_codes"])
        keyword_summary.append({
            "keyword":    kw,
            "top_ings":   top_ings,
            "aspects":    aspects,
            "n_formulas": n_formulas,
        })

    return {
        "query_contexts":      query_contexts,
        "query_matched_ings":  query_matched_ings,
        "matched_keywords":    keyword_summary,
        "keyword_aspects":     list(keyword_aspects),
        "base_ings":           base_ings,
        "active_ings":         active_ings[:top_active],
        "similar_formulas":    similar_formulas,
        "allowed_ingredients": sorted(ist.keys()),
        "target_formula":      target_formula,
    }


def _pick_similar(
    formula_dict,
    query_ing_names,
    priority_codes=None,
    n=3,
    exclude_codes=None,
    structural_names=None,
):
    priority_codes  = priority_codes or set()
    exclude_codes   = exclude_codes or set()
    structural_names = structural_names or set()

    scores = []
    for code, fd in formula_dict.items():
        if code in exclude_codes:
            continue
        overlap            = sum(1 for nm in fd["ingredients"] if nm in query_ing_names)
        is_priority        = 1 if code in priority_codes else 0
        structural_overlap = sum(1 for nm in fd["ingredients"] if nm in structural_names)
        scores.append((is_priority, structural_overlap, overlap, len(fd["ingredients"]), code))

    scores.sort(reverse=True)
    return [
        {"bulk_code": c, "name": formula_dict[c]["name"],
         "ingredients": formula_dict[c]["ingredients"]}
        for _, _, _, _, c in scores[:n]
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 8. 프롬프트 빌더
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """당신은 화장품 처방 전문가입니다. 에센스 제형 처방을 설계하는 전문가로서 다음 규칙을 반드시 준수합니다.

[에센스 처방 기본 규칙]
1. 모든 성분 함량의 합은 정확히 100.00%여야 합니다.
2. 정제수(Aqua/Water)가 기본 용제(base)로 가장 높은 비율을 차지합니다 (보통 65~93%).
3. 방부·보존 기능 성분이 반드시 포함되어야 합니다.
   (예: 1,2-헥산다이올, 에틸헥실글리세린, 펜틸렌글라이콜, 페녹시에탄올 등)
4. 활성 성분(active ingredient)은 효능을 발현하는 최소 유효 농도 이상으로 배합합니다.
5. 성분명은 INCI에 가까운 한국어 표기를 사용합니다.
6. 제공된 통계 데이터(중앙값·범위)를 참고하되, 컨셉에 맞게 함량을 조정하세요.
7. [허용 성분 목록]에 있는 성분명만 사용합니다. 목록에 없는 성분명을 임의로 생성하거나
   추가하는 것은 절대 금지입니다. 반드시 목록에 존재하는 성분명만 처방에 포함하세요.
8. 타겟 처방이 제공되면 그 처방을 베이스로 고정하고, 사용자 요청 방향에 맞게 수정한 3안을 만듭니다.
9. 타사 제품의 미정규화 성분은 생성 단계에서 data.csv 내 대체 가능한 성분을 판단합니다.
   대체 가능 시 "[대체됨] 원본: XXX → 대체: YYY", 대체 불가 시
   "[대체 불가] XXX: data.csv 내 유사 성분 없음"을 substitution_notes 또는 design_rationale에 명시하세요.

[출력 형식 — JSON만 응답, 다른 텍스트 절대 금지]
{
  "substitution_notes": [
    {
      "type": "[대체됨] 또는 [대체 불가]",
      "original": "원본 성분명",
      "substitute": "대체 성분명 또는 없음",
      "reason": "판단 근거"
    }
  ],
  "formulas": [
    {
      "name": "Formula A",
      "concept": "컨셉 한 줄",
      "key_ingredients": ["핵심 성분1", "핵심 성분2"],
      "target_aspects": ["수분감", "흡수력"],
      "ingredients": [
        {"name": "성분명", "content": 숫자(%), "role": "이 성분의 역할 설명"}
      ]
    },
    {"name": "Formula B", ...},
    {"name": "Formula C", ...}
  ],
  "design_rationale": "3안 전체 설계 근거 및 각 안의 차별화 포인트 요약"
}"""


def build_user_prompt(
    query: str,
    ctx: dict,
    total_formulas: int,
    formulation_type: str | None = None,
    structural_info: dict | None = None,
    active_properties: dict | None = None,
) -> str:
    lines = []

    # ── 사용자 요구사항 ──────────────────────────────────────────────────────
    lines.append(f"[사용자 요구사항]\n{query}")

    query_contexts = ctx.get("query_contexts", {})
    lines.append("\n[추출된 맥락 표현]")
    lines.append(f"  · 성분 표현: {_join_or_none(query_contexts.get('ingredients', []))}")
    lines.append(f"  · 제형 표현: {_join_or_none(query_contexts.get('formulation', []))}")
    lines.append(f"  · 마케팅 포인트: {_join_or_none(query_contexts.get('marketing_points', []))}")
    lines.append(f"  · 타겟 제품: {_join_or_none(query_contexts.get('target_product', []))}")

    # ── 1. 제형 타입 및 목적 ─────────────────────────────────────────────────
    if formulation_type:
        lines.append(f"\n[1. 제형 타입 및 목적]")
        lines.append(f"  · 제형 타입: {formulation_type}")
        lines.append(f"  · 이 처방은 {formulation_type} 제형 규격에 맞게 설계되어야 합니다.")

    # ── 2. 구조 성분 목록 ────────────────────────────────────────────────────
    if structural_info and structural_info.get("candidates"):
        lines.append("\n[2. 구조 성분 목록 — 처방에 반드시 포함]")
        lines.append(
            f"  · 필요 카테고리: {', '.join(structural_info.get('required_categories', []))}"
        )
        lines.append("  · 아래 구조 성분 후보 중 적절한 것을 반드시 처방에 포함하세요:")
        for c in structural_info["candidates"][:15]:
            lines.append(
                f"    - {c['name']} ({c['category']}, "
                f"사용빈도 {c['frequency']*100:.0f}%, "
                f"통상 함량 중앙값 {c['median']}%)"
            )

    # ── 3. 액티브 성분 및 물성 정보 ──────────────────────────────────────────
    lines.append("\n[3. 액티브 성분 및 물성 정보]")
    if ctx["query_matched_ings"]:
        lines.append("  · DB 매핑 성분 및 물성:")
        for orig, matched, score in ctx["query_matched_ings"]:
            prop_parts: list[str] = []
            if active_properties and matched in active_properties:
                p = active_properties[matched]
                if isinstance(p, dict):
                    if p.get("solubility"):
                        prop_parts.append(p["solubility"])
                    if p.get("ph_sensitive"):
                        ph_req = p.get("ph_requirement") or "pH 주의"
                        prop_parts.append(f"pH 민감({ph_req})")
                    if p.get("viscosity_impact"):
                        prop_parts.append("점도 영향")
                    if p.get("formulation_conflict"):
                        prop_parts.append(f"⚠ {p['formulation_conflict']}")
            prop_info = f" [{', '.join(prop_parts)}]" if prop_parts else ""
            lines.append(
                f"    '{orig}' → '{matched}' (유사도 {score:.2f}){prop_info}"
            )
    else:
        lines.append("  · 성분 표현 없음 — 마케팅 키워드 기반으로 액티브 성분 선정")

    if active_properties:
        conflicts = [
            (n, p["formulation_conflict"])
            for n, p in active_properties.items()
            if isinstance(p, dict) and p.get("formulation_conflict")
        ]
        if conflicts:
            lines.append("  · 물성 충돌 경고 (처방 생성 시 반드시 고려):")
            for name, conflict in conflicts:
                lines.append(f"    ⚠ {name}: {conflict}")

    # ── 4. 함량 배정 제약 조건 ───────────────────────────────────────────────
    lines.append("\n[4. 함량 배정 제약 조건]")
    lines.append("  · 구조 성분을 먼저 배정하고, 그 다음 액티브 성분을 배정하세요.")
    lines.append("  · 각 성분 함량은 아래 우선순위로 제한됩니다:")
    lines.append(
        "    ① 사용자가 직접 지정한 함량 — 최우선. "
        "문맥을 보고 최솟값인지 정확한 값인지 판단하여 설계하세요."
    )
    lines.append(
        "    ② 해당 성분의 화장품법·INCI 규제 상한선 — "
        "알고 있는 규제 상한이 있으면 반드시 준수하세요."
    )
    lines.append(
        "    ③ 규제 정보가 없으면 data.csv 통계 최대값 × 1.1을 초과하지 마세요."
    )
    lines.append("  · 정제수(Aqua)로 나머지 함량을 채워 합계 정확히 100.00%를 맞추세요.")
    lines.append("  · 방부/보존 성분은 반드시 유효 농도 이상으로 배정하세요.")

    user_constraints_list = ctx.get("query_contexts", {}).get("ingredient_constraints", [])
    if user_constraints_list:
        # 개념어 → DB 성분명 다중 매핑 재구성 (프롬프트에 후보 명시용)
        concept_to_db: dict[str, list[str]] = {}
        for orig, matched, _ in ctx.get("query_matched_ings", []):
            concept_to_db.setdefault(orig, []).append(matched)

        lines.append(
            "  · 사용자 지정 함량 (①번 최우선 — 어떤 성분을 선택하더라도 반드시 이 함량 준수):"
        )
        for c in user_constraints_list:
            if isinstance(c, dict):
                ing_name = c.get("ingredient", "")
                amount   = c.get("amount", "")
                db_names = concept_to_db.get(ing_name, [])
                candidates_str = (
                    f" [DB 매핑 후보: {', '.join(db_names)} — 제형에 적합한 성분 1종 선택]"
                    if db_names else ""
                )
                lines.append(
                    f"    - {ing_name}: 정확히 {amount}%{candidates_str}"
                )

    # ── 5. 타겟 처방 또는 유사처방 ──────────────────────────────────────────
    target_formula = ctx.get("target_formula")
    if target_formula:
        lines.append("\n[5. 타겟 처방 — 베이스 고정]")
        lines.append(
            "아래 타겟 처방을 베이스로, 사용자 요청 방향에 맞게 수정한 처방 3안을 생성하세요. "
            "유사 처방은 보조 참고용이며 타겟 베이스를 대체하면 안 됩니다."
        )
        if target_formula.get("source") == "internal":
            lines.append(
                f"  · 구분: 자사 제품 / bulk_code: {target_formula.get('bulk_code', '')} "
                f"/ 벌크명: {target_formula.get('name', '')}"
            )
            for nm, pct in sorted(
                target_formula.get("ingredients", {}).items(), key=lambda x: -x[1]
            ):
                lines.append(f"    {nm}: {pct:.4f}%")
        elif target_formula.get("source") == "external":
            lines.append(
                f"  · 구분: 타사 제품 / title: {target_formula.get('title', '')} "
                f"/ base_time: {target_formula.get('base_time', '')}"
            )
            lines.append(
                "  · base_time은 사용자 구분 및 출력 참고 정보이며 "
                "탐색/처방 생성 로직의 필터로 쓰지 마세요."
            )
            lines.append(
                "  · 타사 제품 성분 쌍(원본명 → data.csv 정규화명, 순서 기반 가중치):"
            )
            for ing in target_formula.get("ingredients", []):
                status = "정규화" if ing.get("normalized") else "미정규화"
                lines.append(
                    f"    {ing.get('rank', '')}. {ing.get('original_name', '')} → "
                    f"{ing.get('normalized_name', '')} "
                    f"({status}, {ing.get('applied_stage', '')}, "
                    f"weight {ing.get('order_weight', 0):.4f}, method {ing.get('method', '')})"
                )
            lines.append(
                "  · 미정규화 성분은 data.csv 허용 성분 중 대체 가능한 유사 성분을 판단하세요. "
                "대체 가능하면 '[대체됨] 원본: XXX → 대체: YYY'를, "
                "불가하면 '[대체 불가] XXX: 유사 성분 없음'을 명시하세요."
            )

        if ctx["similar_formulas"]:
            lines.append("\n  [유사 처방 참고 예시 — 보조 참고용]")
            for idx, f in enumerate(ctx["similar_formulas"][:2]):
                lines.append(f"    [참고처방 {idx+1}] {f['name']}")
                for nm, pct in sorted(
                    f["ingredients"].items(), key=lambda x: -x[1]
                )[:12]:
                    lines.append(f"      {nm}: {pct:.3f}%")
    else:
        if ctx["similar_formulas"]:
            lines.append("\n[5. 유사 처방 참고 예시 (few-shot)]")
            for idx, f in enumerate(ctx["similar_formulas"][:2]):
                lines.append(f"  [참고처방 {idx+1}] {f['name']}")
                for nm, pct in sorted(
                    f["ingredients"].items(), key=lambda x: -x[1]
                )[:14]:
                    lines.append(f"    {nm}: {pct:.3f}%")

    # ── 마케팅 키워드 분석 ────────────────────────────────────────────────────
    if ctx["matched_keywords"]:
        lines.append("\n[마케팅 키워드 분석 — 실제 처방 데이터 기반]")
        for ks in ctx["matched_keywords"]:
            lines.append(
                f"  · 키워드 '{ks['keyword']}' ({ks['n_formulas']}건 처방에서 사용):"
            )
            lines.append(f"    - 주요 활성 성분: {', '.join(ks['top_ings'])}")
            if ks["aspects"]:
                lines.append(f"    - 소비자 체감 aspect: {', '.join(ks['aspects'])}")

    if ctx["keyword_aspects"]:
        lines.append(f"\n[소비자 체감 타겟 aspect]\n  {', '.join(ctx['keyword_aspects'])}")

    # ── 기본 구성 성분 통계 ───────────────────────────────────────────────────
    lines.append(f"\n[기본 구성 성분 통계 — 총 {total_formulas}건 기준]")
    for i in ctx["base_ings"][:12]:
        lines.append(_format_stat_line(i))

    # 충돌 성분과 정상 성분 분리
    # 대체 후보가 있는 경우에만 제한 성분으로 분류 — 대체 후보가 없으면 원래 성분 그대로 사용
    _ap = active_properties or {}
    conflict_ings: list[tuple[dict, dict]] = []
    normal_ings:   list[dict]              = []
    for i in sorted(ctx["active_ings"], key=lambda x: (-x["count"], -x["frequency"], x["name"])):
        props = _ap.get(i["name"], {})
        has_conflict     = isinstance(props, dict) and props.get("formulation_conflict")
        has_replacement  = isinstance(props, dict) and isinstance(props.get("suggested_replacement"), list) and props.get("suggested_replacement")
        if has_conflict and has_replacement:
            conflict_ings.append((i, props))
        else:
            normal_ings.append(i)

    lines.append("\n[활성·기능성 성분 통계]")
    for i in normal_ings:
        lines.append(_format_stat_line(i))

    if conflict_ings:
        lines.append(
            "\n[사용 제한 성분 — 아래 이유로 처방에서 반드시 배제하고 대체 성분을 활용하세요]"
        )
        for i, props in conflict_ings:
            conflict_msg = props.get("formulation_conflict", "")
            replacement  = props.get("suggested_replacement")
            repl_str = (
                f" → 대체 후보: {', '.join(replacement)}"
                if isinstance(replacement, list) and replacement else ""
            )
            lines.append(f"  · {i['name']}: {conflict_msg}{repl_str}")

    # ── 허용 성분 목록 ────────────────────────────────────────────────────────
    allowed = ctx.get("allowed_ingredients", [])
    if allowed:
        lines.append(
            f"\n[허용 성분 목록 — 반드시 이 목록에 있는 성분명만 사용 (총 {len(allowed)}종)]\n"
            + ", ".join(allowed)
        )

    # ── 3안 설계 지침 ─────────────────────────────────────────────────────────
    formulation_note = (
        f"\n- 제형 타입 '{formulation_type}'에 맞는 처방을 설계하세요."
        if formulation_type else ""
    )
    structural_note = (
        "\n- [2. 구조 성분 목록]에 제시된 구조 성분을 반드시 처방에 포함하세요."
        if structural_info else ""
    )
    lines.append(
        f"\n[3안 설계 지침]\n"
        f"- Formula A: 핵심 활성 성분 고함량 + 성분 수 최소화 (심플 & 집중 효능)\n"
        f"- Formula B: 핵심 효능 + 보습·진정 복합 기능 밸런스 (올라운드 실용 처방)\n"
        f"- Formula C: 트렌드 성분 또는 복합 활성 성분 추가, 마케팅 소구점 강화 "
        f"(프리미엄·차별화){formulation_note}{structural_note}\n\n"
        "각 안의 함량 합계가 정확히 100.00%가 되도록 정제수 함량으로 조정하세요.\n"
        "마케팅 키워드에서 도출된 성분 패턴과 aspect를 처방 설계에 반영하세요.\n"
        "[추출된 맥락 표현]의 제형/사용감 요구가 있으면 반드시 반영하세요.\n"
        "[추출된 맥락 표현]의 마케팅 포인트가 있으면 target_aspects와 설계 근거에 반영하세요.\n"
        "[허용 성분 목록]에 없는 성분은 절대 사용하지 마세요."
    )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 9. 비용 계산
# ─────────────────────────────────────────────────────────────────────────────

BEDROCK_PRICING = {
    "anthropic.claude-3-5-sonnet-20240620-v1:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-5-haiku-20241022-v1:0":  {"input": 1.00, "output":  5.00},
    "anthropic.claude-3-sonnet-20240229-v1:0":   {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-haiku-20240307-v1:0":    {"input": 0.25, "output":  1.25},
    "anthropic.claude-3-opus-20240229-v1:0":     {"input": 15.00,"output": 75.00},
    "anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-haiku-4-5-20251001-v1:0":  {"input": 1.00, "output":  5.00},
    "anthropic.claude-opus-4-5-20251101-v1:0":   {"input": 5.00, "output": 25.00},
    "anthropic.claude-sonnet-4-6-v1":            {"input": 3.00, "output": 15.00},
    "anthropic.claude-opus-4-6-v1":              {"input": 5.00, "output": 25.00},
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
# 10. AWS Bedrock 클라이언트 & Claude API 호출
# ─────────────────────────────────────────────────────────────────────────────

def _create_bedrock_client(aws_profile: str | None, aws_region: str):
    if boto3 is None:
        raise RuntimeError("boto3 미설치 — AWS Bedrock 호출을 위해 `pip install boto3`가 필요합니다.")
    if aws_profile:
        session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
    else:
        session = boto3.Session()
    return session.client(service_name="bedrock-runtime", region_name=aws_region)


def call_claude_api(
    query,
    ctx,
    total_formulas,
    bedrock_client=None,
    aws_profile       = None,
    aws_region        = DEFAULT_AWS_REGION,
    model_id          = DEFAULT_MODEL_ID,
    max_retries       = 2,
    formulation_type  = None,
    structural_info   = None,
    active_properties = None,
):
    import time
    prompt_payload = {}

    if bedrock_client is None:
        try:
            bedrock_client = _create_bedrock_client(aws_profile, aws_region)
        except Exception as e:
            console.print(f"[red]Bedrock 클라이언트 생성 실패: {e}[/red]")
            return None, {}, prompt_payload

    user_prompt = build_user_prompt(
        query, ctx, total_formulas,
        formulation_type=formulation_type,
        structural_info=structural_info,
        active_properties=active_properties,
    )
    prompt_payload = {
        "model_id": model_id,
        "aws_region": aws_region,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": user_prompt,
    }
    console.print(f"[dim]Claude API 호출 중... (모델: {model_id}, 리전: {aws_region})[/dim]")

    for attempt in range(1, max_retries + 2):
        try:
            response = bedrock_client.invoke_model(
                modelId = model_id,
                body    = json.dumps(
                    _build_bedrock_messages_payload(
                        user_prompt,
                        max_tokens=4096,
                        system=SYSTEM_PROMPT,
                    ),
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            err_msg = str(e)
            if "ThrottlingException" in err_msg or "Too Many Requests" in err_msg:
                wait = 10 * attempt
                console.print(f"[yellow]Rate limit — {wait}초 후 재시도 ({attempt}/{max_retries})[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"[red]API 호출 오류: {err_msg}[/red]")
            return None, {}, prompt_payload

        response_body     = json.loads(response["body"].read())
        assistant_message = response_body["content"][0]["text"]

        usage   = response_body.get("usage", {})
        in_tok  = usage.get("input_tokens",  0)
        out_tok = usage.get("output_tokens", 0)
        cost    = calc_cost(model_id, in_tok, out_tok)
        console.print(
            f"[dim]✓ 토큰 사용: 입력 {in_tok:,} / 출력 {out_tok:,} / "
            f"합계 {in_tok + out_tok:,}[/dim]"
        )

        raw = _strip_json_fences(assistant_message)

        try:
            return json.loads(raw), cost, prompt_payload
        except json.JSONDecodeError:
            if attempt <= max_retries:
                console.print(f"[yellow]JSON 파싱 실패 — 재시도 ({attempt}/{max_retries})[/yellow]")
                user_prompt = user_prompt + "\n\n반드시 JSON 형식만 응답하세요. 다른 텍스트 없이."
                prompt_payload["user_prompt"] = user_prompt
                continue
            console.print(f"[red]JSON 파싱 최종 실패\n{raw[:600]}[/red]")
            return None, cost, prompt_payload

    return None, {}, prompt_payload


# ─────────────────────────────────────────────────────────────────────────────
# 11. 후처리 및 검증
# ─────────────────────────────────────────────────────────────────────────────

def _norm_ing_name(s: str) -> str:
    return re.sub(r"[\s\-_·•]", "", str(s)).lower()


def _find_substitutes_for_unresolved(
    unresolved_ings: list[dict],
    known_set: set,
    bedrock_client,
    model_id: str,
) -> dict[str, str | None]:
    """
    IngredientMapper로 교체 불가 판정된 미등록 성분에 대해
    Claude에게 동일 기능의 대체 성분을 자유롭게 제안받고,
    known_set(data.csv) 교차 검증 후 첫 번째 DB 등록 성분을 반환한다.

    Returns: {원래성분명: 대체성분명 또는 None}
      - 대체 성분 확정 시: str (처방 교체 대상)
      - 대체 불가 시:      None (처방에서 제거)
    """
    if not unresolved_ings or not bedrock_client:
        return {ing.get("name", ""): None for ing in unresolved_ings}

    items_str = "\n".join(
        f"- {ing.get('name', '')}: {ing.get('role', '역할 불명')}"
        for ing in unresolved_ings
    )

    prompt = (
        "다음은 화장품 처방 DB에 등록되지 않은 성분들입니다.\n"
        "각 성분의 기능(역할)을 유지하면서 대체할 수 있는 화장품 원료를 "
        "한국 INCI 표기로 최대 5개 제안하세요.\n\n"
        f"[미등록 성분 및 역할]\n{items_str}\n\n"
        "제안은 실제 화장품 원료로 사용되는 성분명이어야 합니다.\n"
        "JSON만 반환 (미등록 성분명을 key로):\n"
        "{\"미등록성분명\": [\"대체성분1\", \"대체성분2\", ...]}"
    )

    try:
        result, _ = _invoke_bedrock_json(
            bedrock_client,
            model_id,
            prompt,
            max_tokens=800,
        )

        substitutes: dict[str, str | None] = {}
        for ing in unresolved_ings:
            name = ing.get("name", "")
            candidates = result.get(name, [])
            if not isinstance(candidates, list):
                candidates = []

            valid   = [c for c in candidates if c in known_set]
            invalid = [c for c in candidates if c not in known_set]

            if invalid:
                console.print(f"[dim]    {name} — DB 미등록 제외: {', '.join(invalid)}[/dim]")
            if valid:
                substitutes[name] = valid[0]
                console.print(
                    f"[dim]  → '{name}' 대체 성분 확정: '{valid[0]}' "
                    f"(후보: {', '.join(valid)})[/dim]"
                )
            else:
                substitutes[name] = None
                console.print(
                    f"[yellow]  → '{name}': Claude 제안이 모두 DB 미등록 — 처방에서 제거[/yellow]"
                )
        return substitutes

    except Exception as e:
        console.print(f"[yellow]미등록 성분 대체 조회 실패: {e}[/yellow]")
        return {ing.get("name", ""): None for ing in unresolved_ings}


def validate_and_fix(
    formula_data: dict,
    stats: dict,
    known_set: set | None = None,
    mapper: "IngredientMapper | None" = None,
    user_constraints: dict | None = None,
    active_properties: dict | None = None,
    bedrock_client=None,
    model_id: str = "",
) -> dict:
    """
    1) 화이트리스트 검증 (미등록 성분 교체/대체/제거)
       - mapper score ≥ 0.85 → 자동 교체
       - mapper score < 0.85 → Claude 대체 탐색 → 확정 시 교체, 불가 시 제거
    2) 함량 상한 보정 — 3단계 우선순위
       ① 사용자 지정 함량 → 보정 없이 통과
       ② Claude 규제 상한선 → 초과 시 보정
       ③ 통계 max × 1.1 → 초과 시 보정
    3) 합계 100% 정제수 보충
    4) 통계 범위 이상치 경고 태그 (역할 필드)
    """
    REPLACE_THRESHOLD = 0.85
    ist = stats["ingredient_stats"]

    norm_user: dict[str, float] = {
        _norm_ing_name(k): v
        for k, v in (user_constraints or {}).items()
    }

    for formula in formula_data.get("formulas", []):
        ings = formula.get("ingredients", [])

        # ── 1) 화이트리스트 검증 ─────────────────────────────────────────
        if known_set:
            checked    = []
            unresolved = []  # mapper로 교체 불가 판정된 성분 목록

            for ing in ings:
                name = ing.get("name", "")
                if name in known_set:
                    checked.append(ing)
                    continue

                if mapper:
                    matched, score, _ = mapper.map(name)
                    if matched and score >= REPLACE_THRESHOLD:
                        console.print(
                            f"[yellow]  ⚠ 미등록 성분 교체: '{name}' → '{matched}' "
                            f"(score {score:.2f})[/yellow]"
                        )
                        ing["name"] = matched
                        checked.append(ing)
                    else:
                        nearest = matched or "없음"
                        console.print(
                            f"[yellow]  ⚠ '{name}' mapper 교체 불가 "
                            f"(최근접: {nearest}, score {score:.2f}) — Claude 대체 탐색[/yellow]"
                        )
                        unresolved.append(ing)
                else:
                    unresolved.append(ing)

            # ── 1-B) Claude 대체 성분 탐색 ───────────────────────────────
            if unresolved:
                console.print(
                    f"[dim]미등록 성분 {len(unresolved)}종 Claude 대체 탐색 중...[/dim]"
                )
                substitutes = _find_substitutes_for_unresolved(
                    unresolved, known_set, bedrock_client, model_id
                )
                for ing in unresolved:
                    name = ing.get("name", "")
                    sub  = substitutes.get(name)
                    if sub:
                        ing["role"] = ing.get("role", "")
                        ing["name"] = sub
                        checked.append(ing)
                    else:
                        console.print(
                            f"[red]  ✗ '{name}' — 대체 성분 없음, 처방에서 제거 "
                            f"(함량 {ing.get('content', 0):.4f}% → 정제수 보충)[/red]"
                        )

            formula["ingredients"] = checked

        # ── 2) 함량 상한 보정 (3단계 우선순위) ──────────────────────────
        ings = formula.get("ingredients", [])
        formulation_notes: list[str] = []

        for ing in ings:
            name    = ing.get("name", "")
            content = ing.get("content", 0)
            name_n  = _norm_ing_name(name)

            # ① 사용자 지정 함량 → 보정 없이 통과
            if name_n in norm_user:
                console.print(
                    f"[dim]  ✓ 사용자 지정 함량 유지: '{name}' {content:.4f}%[/dim]"
                )
                continue

            props = active_properties.get(name, {}) if active_properties else {}
            if not isinstance(props, dict):
                props = {}

            # ② Claude 규제 상한선
            reg_limit = props.get("regulatory_limit")
            if isinstance(reg_limit, (int, float)) and reg_limit > 0:
                if content > reg_limit:
                    console.print(
                        f"[yellow]  ⚠ 규제 상한 초과 보정: '{name}' "
                        f"{content:.4f}% → {reg_limit:.4f}%[/yellow]"
                    )
                    _maybe_add_formulation_note(
                        formulation_notes, name, reg_limit, "규제 상한", props
                    )
                    ing["content"] = round(reg_limit, 6)
                continue

            # ③ 통계 max × 1.1
            if name in ist:
                cap = round(ist[name]["max"] * 1.1, 6)
                if content > cap:
                    console.print(
                        f"[yellow]  ⚠ 통계 상한 초과 보정: '{name}' "
                        f"{content:.4f}% → {cap:.4f}% "
                        f"(통계 max {ist[name]['max']}% × 1.1)[/yellow]"
                    )
                    _maybe_add_formulation_note(
                        formulation_notes, name, cap, "통계 상한", props
                    )
                    ing["content"] = cap

        if formulation_notes:
            formula["formulation_notes"] = formulation_notes

        # ── 3) 합계 100% 정제수 보충 ─────────────────────────────────────
        total = sum(i.get("content", 0) for i in ings)
        if abs(total - 100.0) > 0.05:
            water_ing = next(
                (i for i in ings
                 if "정제수" in i.get("name", "")
                 or "water" in i.get("name", "").lower()),
                None,
            )
            if water_ing:
                new_water = round(water_ing["content"] + (100.0 - total), 6)
                if new_water < 0:
                    console.print(
                        f"[red]  ✗ 정제수 함량이 음수({new_water:.4f}%)가 되어 "
                        f"합계 보정 불가 — 처방 함량 재검토 필요[/red]"
                    )
                else:
                    water_ing["content"] = new_water
                    water_ing["role"] = (
                        water_ing.get("role", "") + " [합계보정]"
                    ).strip()
            else:
                console.print(
                    "[yellow]  ⚠ 정제수 성분이 없어 합계 보정 불가[/yellow]"
                )

        # ── 4) 통계 범위 이상치 경고 ─────────────────────────────────────
        for ing in ings:
            name, content = ing.get("name", ""), ing.get("content", 0)
            if name in ist:
                s = ist[name]
                if content > s["max"] * 1.5:
                    ing["role"] = (
                        ing.get("role", "") + f" ⚠ 통계 최대치({s['max']}%) 초과"
                    ).strip()
                elif 0 < content < s["min"] * 0.5:
                    ing["role"] = (
                        ing.get("role", "") + f" ⚠ 통계 최소치({s['min']}%) 미만"
                    ).strip()

    return formula_data


def _maybe_add_formulation_note(
    notes: list[str],
    name: str,
    cap: float,
    cap_type: str,
    props: dict,
) -> None:
    conflict = props.get("formulation_conflict")
    viscosity = props.get("viscosity_impact", False)
    if conflict:
        notes.append(
            f"'{name}' 함량이 {cap_type}({cap:.1f}%)으로 제한됨 — {conflict}"
        )
    elif viscosity:
        notes.append(
            f"'{name}' 함량이 {cap_type}({cap:.1f}%)으로 제한됨 — 점도에 영향을 줄 수 있음"
        )


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
            for note in formula.get("formulation_notes", []):
                _rich_console.print(f"  [yellow]⚠ {note}[/yellow]")
        else:
            print(f"\n{'─'*65}\n{name}  │  {concept}")
            if key_i:   print(f"핵심 성분: {', '.join(key_i)}")
            if aspects: print(f"타겟 aspect: {', '.join(aspects)}")
            print(f"{'─'*65}")
            print(f"{'성분명':<34} {'함량(%)':>10}  역할\n"+"-"*75)
            total = 0.0
            for i in ings:
                c = i.get("content", 0); total += c
                print(f"{i.get('name', ''):<34} {c:>10.4f}  {i.get('role', '')}")
            print("-"*75 + f"\n{'합계':<46} {total:>10.4f}")
            for note in formula.get("formulation_notes", []):
                print(f"  ⚠ {note}")

    if dr := formula_data.get("design_rationale"):
        print(f"\n{'─'*65}\n[설계 근거]\n{dr}")

    if notes := formula_data.get("substitution_notes"):
        print(f"\n{'─'*65}\n[대체/미대체 성분 판단]")
        for note in notes:
            if isinstance(note, dict):
                typ = note.get("type", "")
                original = note.get("original", "")
                substitute = note.get("substitute", "")
                reason = note.get("reason", "")
                arrow = f" → {substitute}" if substitute and substitute != "없음" else ""
                print(f"{typ} {original}{arrow}: {reason}".strip())
            else:
                print(str(note))


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
        rows = _formula_ingredient_rows(formula)
        csv_path = output_path / f"{fname}.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
        console.print(f"[green]✓ {csv_path}[/green]")

    xl_path = output_path / "formula_output.xlsx"
    with pd.ExcelWriter(xl_path, engine="openpyxl") as writer:
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

        meta = [
            {"항목": "요구사항", "내용": query},
            {"항목": "설계 근거", "내용": formula_data.get("design_rationale", "")},
        ]
        if formula_data.get("substitution_notes"):
            meta.append({
                "항목": "대체/미대체 성분 판단",
                "내용": json.dumps(formula_data.get("substitution_notes", []), ensure_ascii=False),
            })
        for f in formula_data.get("formulas", []):
            meta.append({
                "항목": f["name"],
                "내용": f"{f.get('concept', '')} | 핵심: {', '.join(f.get('key_ingredients', []))} | aspect: {', '.join(f.get('target_aspects', []))}",
            })
        pd.DataFrame(meta).to_excel(writer, sheet_name="설계_근거", index=False)
        _format_wrapped_sheet(
            writer.sheets["설계_근거"],
            wrap_columns=[1, 2],
            width_map={1: 18, 2: 120},
        )

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

        if prompt_payload:
            prompt_rows = [
                {"구분": "model_id", "내용": prompt_payload.get("model_id", "")},
                {"구분": "aws_region", "내용": prompt_payload.get("aws_region", "")},
                {"구분": "system_prompt", "내용": prompt_payload.get("system_prompt", "")},
                {"구분": "user_prompt", "내용": prompt_payload.get("user_prompt", "")},
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
    console.rule("AI 기반 화장품 처방 자동 생성 PoC v0.8 (AWS Bedrock / SageMaker)")

    # ── Bedrock 클라이언트 조기 생성 (ConceptExpander + 처방 생성 API 공유) ──
    try:
        bedrock_client = _create_bedrock_client(aws_profile, aws_region)
        console.print("[green]✓ Bedrock 클라이언트 생성[/green]")
    except Exception as e:
        console.print(f"[red]Bedrock 클라이언트 생성 실패: {e}[/red]")
        return

    # 1. 처방 데이터
    df, formula_dict = load_formula_data(data_csv)

    # 2. 통계
    console.print("[dim]통계 분석 중...[/dim]")
    stats = build_stats(formula_dict)
    console.print(f"[green]✓ 성분 {len(stats['ingredient_stats'])}종 통계[/green]")

    known_set = set(stats["ingredient_stats"].keys())

    # 3. 마케팅 키워드 DB
    keyword_db = load_product_data(product_csv, formula_dict)

    # 4. Mapper
    mapper = IngredientMapper(list(stats["ingredient_stats"].keys()))

    # 5. 질의 맥락 표현 추출
    console.print("[dim]쿼리 내 맥락 표현 분석 중...[/dim]")
    query_contexts = extract_query_contexts(query, bedrock_client=bedrock_client, model_id=model_id)
    console.print(f"[dim]  성분 표현: {query_contexts['ingredients']}[/dim]")
    console.print(f"[dim]  제형 표현: {query_contexts['formulation']}[/dim]")
    console.print(f"[dim]  마케팅 포인트: {query_contexts['marketing_points']}[/dim]")
    console.print(f"[dim]  타겟 제품: {query_contexts['target_product']}[/dim]")

    # 5-A. [명령 1] 제형 타입 확정
    console.print("[dim]제형 타입 분류 중...[/dim]")
    formulation_type = classify_formulation_type(
        query_contexts.get("formulation", []),
        query,
        bedrock_client=bedrock_client,
        model_id=model_id,
    )

    # 5-B. [명령 2] 구조 성분 선정
    structural_info = select_structural_ingredients(formulation_type, df, stats)

    expander = ConceptExpander(
        known_ingredients=list(stats["ingredient_stats"].keys()),
        mapper=mapper,
        bedrock_client=bedrock_client,
        model_id=model_id,
    )

    # 5-C. 타겟 제품 탐색(target_product가 있을 때만)
    target_formula = None
    if query_contexts.get("target_product"):
        target_formula = find_target_product(
            query_contexts["target_product"],
            df,
            formula_dict,
            external_csv,
            expander,
        )
        if not target_formula:
            console.print(
                "[yellow]타겟 제품을 확정하지 못해 기존 일반 처방 생성 흐름으로 계속 진행합니다.[/yellow]"
            )

    concept_ings = expander.expand(query, query_contexts=query_contexts)
    console.print("[green]✓ 성분명 탐색[/green]")
    for log_line in expander.last_search_logs:
        console.print(f"[dim]{log_line}[/dim]")
    if concept_ings:
        console.print(f"[dim]  탐색 결과 반영: {len(concept_ings)}건[/dim]")
        for orig, matched, score, method in concept_ings:
            console.print(
                f"[dim]    '{orig}' → '{matched}' ({method}, score {score:.2f})[/dim]"
            )
    else:
        console.print("[dim]  탐색 결과 반영: 없음[/dim]")

    # 5-D. 사용자 지정 함량 맵 구성 (concept_ings 매핑 결과 활용)
    # 하나의 개념어(예: '비타민 C')가 여러 DB 성분명에 매핑될 수 있으므로
    # 매핑된 모든 후보에 동일한 사용자 지정 함량을 적용해
    # LLM이 어느 후보를 선택하든 validate_and_fix에서 보정되지 않도록 보호한다.
    concept_name_multimap: dict[str, list[str]] = {}
    for orig, matched, _, _ in concept_ings:
        concept_name_multimap.setdefault(orig, []).append(matched)

    user_constraints: dict[str, float] = {}
    for c in query_contexts.get("ingredient_constraints", []):
        if not isinstance(c, dict):
            continue
        raw    = c.get("ingredient", "")
        amount = c.get("amount")
        if not raw or amount is None:
            continue
        try:
            amount_f = float(amount)
        except (ValueError, TypeError):
            continue
        mapped_names = concept_name_multimap.get(raw)
        if mapped_names:
            for name in mapped_names:
                user_constraints[name] = amount_f
        else:
            user_constraints[raw] = amount_f
    if user_constraints:
        console.print(f"[dim]  사용자 지정 함량: {user_constraints}[/dim]")

    # 6. 컨텍스트 구성
    ctx = build_context(
        query,
        stats,
        keyword_db,
        formula_dict,
        query_contexts=query_contexts,
        concept_ings=concept_ings,
        target_formula=target_formula,
        structural_info=structural_info,
    )

    if ctx["matched_keywords"]:
        console.print(f"[dim]  키워드 매핑: {[k['keyword'] for k in ctx['matched_keywords']]}[/dim]")
    if ctx["keyword_aspects"]:
        console.print(f"[dim]  타겟 aspect: {ctx['keyword_aspects']}[/dim]")
    console.print(
        f"[green]✓ base {len(ctx['base_ings'])}종 / active {len(ctx['active_ings'])}종 / "
        f"유사처방 {len(ctx['similar_formulas'])}건 / 허용 성분 {len(ctx['allowed_ingredients'])}종[/green]"
    )

    # 6-A. [명령 3] 액티브 성분 물성 검토 (컨텍스트 active_ings 전체 대상)
    # concept_ings(사용자 지정)만 보던 기존 방식 대신, LLM 프롬프트에 실제로
    # 전달되는 active_ings 전체를 검토해 소듐하이알루로네이트 등 고빈도 자동
    # 진입 성분의 사용감·충돌 문제도 프롬프트에 반영한다.
    all_active_names = [ing["name"] for ing in ctx["active_ings"]]
    console.print(f"[dim]액티브 성분 물성 검토 중... ({len(all_active_names)}종)[/dim]")
    active_properties = review_active_properties(
        all_active_names, formulation_type,
        bedrock_client=bedrock_client,
        model_id=model_id,
    )

    # 6-B. [1순위] 사용자 요구 ↔ 물성 충돌 자동 연결
    active_properties = _apply_user_feel_conflicts(
        active_properties,
        query_contexts.get("formulation", []),
    )
    # 6-C. 자동 태그된 충돌 성분의 대체 성분 조회 (suggested_replacement 없는 것만)
    # Claude가 INCI 지식으로 자유롭게 제안 → data.csv(known_set) 교차 검증 후 확정
    active_properties = _fetch_replacements_for_flagged(
        active_properties,
        formulation_type,
        query_contexts.get("formulation", []),
        known_set,
        bedrock_client,
        model_id,
    )

    # 7. 처방 생성
    formula_data, cost, prompt_payload = call_claude_api(
        query, ctx, stats["total_formulas"],
        bedrock_client=bedrock_client,
        aws_profile=aws_profile,
        aws_region=aws_region,
        model_id=model_id,
        formulation_type=formulation_type,
        structural_info=structural_info,
        active_properties=active_properties,
    )
    if not formula_data:
        console.print("[red]처방 생성 실패[/red]")
        return

    # 8. 후처리 (화이트리스트 검증 + 함량 상한 보정)
    formula_data = validate_and_fix(
        formula_data, stats,
        known_set=known_set,
        mapper=mapper,
        user_constraints=user_constraints,
        active_properties=active_properties,
        bedrock_client=bedrock_client,
        model_id=model_id,
    )

    # 9. 출력 & 저장
    print_results(formula_data, query)
    save_results(formula_data, stats, keyword_db, query, output_dir, cost, prompt_payload=prompt_payload)
    print_cost_summary(cost)
    console.print(f"\n[bold green]완료! 결과: {output_dir}/[/bold green]")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="화장품 처방 자동 생성 PoC v0.8 (AWS Bedrock / SageMaker)")
    parser.add_argument("--data",        required=True, help="처방 CSV (data.csv)")
    parser.add_argument("--product",     required=True, help="마케팅 키워드 CSV (product.csv)")
    parser.add_argument("--external",    default="external.csv", help="타사 제품 CSV (external.csv)")
    parser.add_argument("--query",       required=True, help="제품 요구사항 텍스트")
    parser.add_argument("--aws_profile", default=None,  help="AWS 프로파일명 (로컬 개발용)")
    parser.add_argument("--aws_region",  default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_AWS_REGION),
                                                        help="AWS 리전 (기본: ap-northeast-2)")
    parser.add_argument("--model",       default=DEFAULT_MODEL_ID,
                                                        help="Bedrock 모델 ID")
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
