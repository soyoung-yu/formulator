"""
AI 기반 화장품 처방 자동 생성 시스템 — PoC v0.9
================================================

v0.9는 v0.8과 같은 처방 자동 생성 목적을 유지하되, 코드를 처음부터 다시
구성하는 버전이다. 이 파일의 첫 단계는 데이터 입력 계층을 명확히 분리하는 것이다.

현재 포함 범위:
    - CLI 설정 파싱
    - data.csv 처방 데이터 로드 및 성분 통계 생성
    - product.csv 마케팅 키워드 데이터 로드
    - external.csv 타사 제품 데이터 로드
    - 임베딩 모델 로더 골격
    - AWS Bedrock 클라이언트 래퍼
    - 질의 맥락 추출
    - 분산계/제품 형상/세부 제형 판단
    - 구조 골격 설계
    - 기본 Backbone 설계
    - 액티브 적합성 및 Backbone 수정 전략

다음 단계에서 성분 매핑, 프롬프트 생성, 후처리 검증을 이 구조 위에 붙인다.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    import boto3
except ImportError:  # boto3가 없는 환경에서도 데이터 로딩 코드는 사용할 수 있게 둔다.
    boto3 = None


warnings.filterwarnings("ignore")


DEFAULT_AWS_REGION = "ap-northeast-2"
DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
DEFAULT_EMBEDDING_MODEL = "snunlp/KR-SBERT-V40K-klueNLI-augSTS"
DEFAULT_EMBEDDING_DIR = Path("./models/KR-SBERT-V40K-klueNLI-augSTS")


STRUCTURAL_FUNCTIONS = {
    "base": ["용제", "벌킹제"],
    "thickener": ["점도조절제", "유화안정제"],
    "preservative": ["살균보존제", "항균제", "항미생물제"],
    "ph_adj": ["pH조절제", "pH 완충제"],
    "chelator": ["금속이온봉쇄제"],
    "surfactant": ["계면활성제", "유화제"],
    "fragrance": ["방향제", "향료", "착향제"],
}
FUNC_TO_ROLE = {
    function_name: role
    for role, function_names in STRUCTURAL_FUNCTIONS.items()
    for function_name in function_names
}
BASE_ROLES = {"base", "thickener", "preservative", "ph_adj", "chelator"}


KNOWN_BASE_INGREDIENTS = {
    "정제수",
    "글리세린",
    "부틸렌글라이콜",
    "프로판다이올",
    "1,2-헥산다이올",
    "다이프로필렌글라이콜",
    "에틸헥실글리세린",
    "펜틸렌글라이콜",
    "메틸프로판다이올",
    "잔탄검",
    "트로메타민",
    "카보머",
    "소듐파이테이트",
    "다이소듐이디티에이",
    "향료",
    "토코페롤",
    "암모늄아크릴로일다이메틸타우레이트/브이피코폴리머",
    "아크릴레이트/C10-30알킬아크릴레이트크로스폴리머",
}


FORMULA_REQUIRED_COLUMNS = {
    "bulk_code",
    "bulk_name",
    "ingredient_name",
    "ingredient_function",
    "content",
}
PRODUCT_REQUIRED_COLUMNS = {"bulk_code", "marketing_keywords_list", "aspect_list"}
EXTERNAL_REQUIRED_COLUMNS = {"title", "representation_ingredients", "base_time"}


ALIAS_HINTS: dict[str, list[str]] = {
    "어성초추출물": ["약모밀추출물"],
    "피디알엔": ["소듐디엔에이", "하이드롤라이즈드디엔에이"],
    "PDRN": ["소듐디엔에이", "하이드롤라이즈드디엔에이"],
    "비타민c": ["3-O-에틸아스코빅애씨드", "아스코빅애씨드"],
    "글루타치온": ["글루타티온"],
    "PHA": ["글루코노락톤", "락토바이오닉애씨드"],
    "AHA": ["글라이콜릭애씨드", "락틱애씨드", "말릭애씨드", "시트릭애씨드", "타르타릭애씨드"],
    "BHA": ["살리실릭애씨드"],
    "LHA": ["카프릴로일살리실릭애씨드"],
    "시카": ["병풀"],
}

PRODUCT_FORM_OPTIONS = ("워터", "젤(겔)", "유액(밀크/로션)", "오일")
FORMULATION_TYPE_OPTIONS = ("수상 솔루션", "가용화", "O/W 유화", "W/O 유화", "분산")
TARGET_VISCOSITY_OPTIONS = ("저점도", "중점도", "고점도")
PH_RANGE_PROFILES = {
    "산성": {"min": 3.0, "max": 4.5},
    "약산성": {"min": 4.5, "max": 6.0},
    "약산성~중성": {"min": 5.0, "max": 7.0},
    "중성": {"min": 6.0, "max": 7.5},
    "약알칼리성": {"min": 7.5, "max": 8.5},
}
DEFAULT_PH_RANGE_LABEL = "약산성~중성"
SOLUBILITY_OPTIONS = {"수용성", "유용성", "양친성", "난용성", "불용성", "불명"}
BACKBONE_TYPE_OPTIONS = {
    "수상 솔루션",
    "수분 젤",
    "가용화",
    "O/W 유화",
    "W/O 유화",
    "현탁/분산",
}
SYSTEM_CHECK_OPTIONS = {"적정", "보완 필요", "불필요"}
SYSTEM_CHECK_KEYS = {
    "preservation_system",
    "rheology_system",
    "solubilization_system",
    "emulsification_system",
    "ph_system",
}

ROLE_SETS_BY_FORMULATION_TYPE = {
    "수상 솔루션": {
        "water_phase": "required",
        "humectant": "required",
        "preservative": "required",
        "ph_adjuster": "required",
        "chelator": "optional",
    },
    "가용화": {
        "water_phase": "required",
        "humectant": "required",
        "solubilizer": "required",
        "surfactant": "optional",
        "preservative": "required",
        "ph_adjuster": "required",
        "chelator": "optional",
    },
    "O/W 유화": {
        "water_phase": "required",
        "oil_phase": "required",
        "emulsifier": "required",
        "co_emulsifier": "optional",
        "rheology_modifier": "required",
        "stabilizer": "required",
        "preservative": "required",
        "ph_adjuster": "required",
    },
    "W/O 유화": {
        "oil_phase": "required",
        "water_phase": "required",
        "w_o_emulsifier": "required",
        "oil_structurant": "optional",
        "stabilizer": "required",
        "preservative": "required",
    },
    "분산": {
        "dispersion_medium": "required",
        "dispersed_phase": "required",
        "wetting_agent": "required",
        "dispersant": "required",
        "suspending_agent": "required",
        "rheology_modifier": "required",
        "preservative": "required",
        "ph_adjuster": "required",
    },
}

ROLE_FUNCTION_MAP = {
    "water_phase": ["용제", "벌킹제"],
    "dispersion_medium": ["용제", "벌킹제"],
    "humectant": ["보습제", "용제"],
    "oil_phase": ["피부컨디셔닝제", "피부보호제"],
    "emulsifier": ["유화제", "계면활성제"],
    "co_emulsifier": ["계면활성제", "유화안정제"],
    "w_o_emulsifier": ["계면활성제", "유화안정제"],
    "solubilizer": ["계면활성제", "용제"],
    "surfactant": ["계면활성제"],
    "rheology_modifier": ["점도조절제"],
    "stabilizer": ["유화안정제", "점도조절제", "피막형성제"],
    "oil_structurant": ["점도조절제", "피막형성제", "가소제"],
    "preservative": ["살균보존제", "항균제", "항미생물제", "항진균제"],
    "ph_adjuster": ["pH조절제", "pH 완충제"],
    "buffer": ["pH 완충제", "pH조절제"],
    "chelator": ["금속이온봉쇄제"],
    "wetting_agent": ["계면활성제", "보습제"],
    "dispersant": ["분산제", "계면활성제"],
    "suspending_agent": ["현탁제", "점도조절제"],
    "dispersed_phase": ["착색제", "불투명화제", "흡수제", "피부컨디셔닝제"],
    "binder": ["결합제", "접착제"],
    "uv_light_support": ["광안정화제", "자외선차단제"],
    "deodorant_support": ["방취제"],
    "astringent_support": ["수렴제"],
    "anti_caking": ["안티케이킹제"],
    "abrasive": ["연마제"],
    "tone_color_support": ["착색제", "착색제 자외선차단제", "피부표백제"],
    "antioxidant_support": ["항산화제", "환원제"],
    "anti_acne_support": ["항여드름제"],
    "hair_conditioning_support": ["헤어컨디셔닝제"],
    "denaturant": ["변성제"],
    "unreported": ["미보고"],
}

ROLE_NAME_HINTS = {
    "water_phase": ["정제수", "글리세린", "부틸렌글라이콜", "프로판다이올"],
    "dispersion_medium": ["정제수", "글리세린", "부틸렌글라이콜"],
    "oil_phase": ["오일", "트라이글리세라이드", "스쿠알란", "카프릴릭", "다이메티콘"],
    "emulsifier": ["폴리글리세릴", "스테아레이트", "세테아릴"],
    "w_o_emulsifier": ["폴리글리세릴", "실리콘", "솔비탄"],
    "solubilizer": ["피이지", "폴리솔베이트", "피피지", "하이드로제네이티드캐스터오일"],
    "rheology_modifier": ["카보머", "잔탄검", "아크릴레이트", "셀룰로오스"],
    "stabilizer": ["잔탄검", "카보머", "아크릴레이트"],
    "oil_structurant": ["왁스", "세테아릴", "스테아릴", "하이드로제네이티드"],
    "preservative": ["1,2-헥산다이올", "에틸헥실글리세린", "페녹시에탄올", "펜틸렌글라이콜"],
    "ph_adjuster": ["트로메타민", "시트릭애씨드", "소듐하이드록사이드", "아르지닌"],
    "buffer": ["시트릭애씨드", "소듐시트레이트", "트로메타민"],
    "chelator": ["다이소듐이디티에이", "소듐파이테이트"],
    "dispersant": ["분산", "폴리글리세릴", "계면활성"],
    "suspending_agent": ["잔탄검", "카보머", "셀룰로오스"],
    "binder": ["아크릴레이트", "폴리머", "셀룰로오스"],
    "uv_light_support": ["다이옥사이드", "메톡시", "벤조", "필터"],
    "deodorant_support": ["징크", "트라이에틸"],
    "astringent_support": ["위치하젤", "탄닌", "징크"],
    "anti_caking": ["실리카", "스타치", "탤크"],
    "abrasive": ["실리카", "셀룰로오스"],
    "tone_color_support": ["티타늄", "아이언옥사이드", "마이카", "나이아신아마이드"],
    "antioxidant_support": ["토코페롤", "아스코", "글루타티온", "페룰릭"],
    "anti_acne_support": ["살리실릭", "베타인살리실레이트", "징크"],
    "hair_conditioning_support": ["폴리쿼터늄", "실리콘", "아모다이메티콘"],
    "denaturant": ["에탄올", "알코올"],
}

ROLE_PREFERRED_INGREDIENTS = {
    "water_phase": ["정제수"],
    "dispersion_medium": ["정제수"],
    "humectant": ["글리세린", "부틸렌글라이콜", "프로판다이올", "다이프로필렌글라이콜"],
    "oil_phase": ["카프릴릭/카프릭트라이글리세라이드", "스쿠알란", "다이메티콘"],
    "emulsifier": ["글리세릴스테아레이트", "폴리글리세릴", "세테아릴"],
    "co_emulsifier": ["세테아릴알코올", "솔비탄", "폴리글리세릴"],
    "w_o_emulsifier": ["폴리글리세릴", "솔비탄", "다이메티콘"],
    "solubilizer": ["폴리솔베이트20", "피이지-60하이드로제네이티드캐스터오일"],
    "rheology_modifier": ["카보머", "잔탄검", "암모늄아크릴로일다이메틸타우레이트/브이피코폴리머"],
    "stabilizer": ["잔탄검", "카보머", "아크릴레이트"],
    "oil_structurant": ["세테아릴알코올", "스테아릴알코올", "왁스"],
    "preservative": ["1,2-헥산다이올", "에틸헥실글리세린", "페녹시에탄올"],
    "ph_adjuster": ["트로메타민", "시트릭애씨드", "소듐하이드록사이드"],
    "buffer": ["시트릭애씨드", "소듐시트레이트", "트로메타민"],
    "chelator": ["다이소듐이디티에이", "소듐파이테이트"],
    "wetting_agent": ["글리세린", "부틸렌글라이콜", "폴리글리세릴"],
    "dispersant": ["폴리글리세릴", "분산"],
    "suspending_agent": ["잔탄검", "카보머", "셀룰로오스"],
    "dispersed_phase": ["실리카", "티타늄디옥사이드", "마이카"],
}

ROLE_AMOUNT_RANGES = {
    "water_phase": (0.0, 0.0, 100.0),
    "dispersion_medium": (0.0, 0.0, 100.0),
    "humectant": (2.0, 5.0, 8.0),
    "oil_phase": (3.0, 8.0, 15.0),
    "emulsifier": (1.0, 3.0, 5.0),
    "co_emulsifier": (0.3, 1.0, 3.0),
    "w_o_emulsifier": (1.5, 3.0, 6.0),
    "solubilizer": (0.5, 2.0, 5.0),
    "surfactant": (0.3, 1.0, 3.0),
    "rheology_modifier": (0.1, 0.3, 0.8),
    "stabilizer": (0.1, 0.4, 1.0),
    "oil_structurant": (0.5, 2.0, 4.0),
    "preservative": (0.5, 1.0, 2.0),
    "ph_adjuster": (0.05, 0.2, 0.5),
    "buffer": (0.05, 0.3, 1.0),
    "chelator": (0.02, 0.05, 0.2),
    "wetting_agent": (0.2, 0.7, 2.0),
    "dispersant": (0.3, 1.0, 3.0),
    "suspending_agent": (0.2, 0.6, 1.5),
    "dispersed_phase": (1.0, 3.0, 8.0),
}

OPTIONAL_BACKBONE_ROLES = {
    "chelator",
    "buffer",
    "co_emulsifier",
    "oil_structurant",
}


@dataclass(frozen=True)
class CLIConfig:
    data_csv: str
    product_csv: str
    external_csv: str
    query: str
    aws_profile: str | None
    aws_region: str
    model_id: str
    output_dir: str
    load_embeddings: bool
    allow_embedding_download: bool


@dataclass
class FormulaDataset:
    raw: pd.DataFrame
    formulas: dict[str, dict[str, Any]]
    ingredient_stats: dict[str, dict[str, Any]]
    unmapped_functions: list[str]

    @property
    def total_formulas(self) -> int:
        return len(self.formulas)

    @property
    def ingredient_names(self) -> list[str]:
        return sorted(self.ingredient_stats)


@dataclass
class ProductKeywordDataset:
    raw: pd.DataFrame
    keyword_db: dict[str, dict[str, Any]]
    linked_bulk_count: int


@dataclass
class ExternalProduct:
    title: str
    base_time: str
    representation_ingredients: list[str]


@dataclass
class ExternalProductDataset:
    raw: pd.DataFrame
    products: list[ExternalProduct]


@dataclass(frozen=True)
class IngredientConstraint:
    ingredient: str
    amount: float


@dataclass(frozen=True)
class IngredientMention:
    name: str
    solubility: str
    stable_ph_range: str


@dataclass
class QueryContext:
    ingredients: list[IngredientMention]
    formulation: list[str]
    marketing_points: list[str]
    target_product: list[str]
    ingredient_constraints: list[IngredientConstraint]

    @classmethod
    def empty(cls) -> "QueryContext":
        return cls(
            ingredients=[],
            formulation=[],
            marketing_points=[],
            target_product=[],
            ingredient_constraints=[],
        )


@dataclass
class DispersionJudgement:
    dispersion_system: str
    reason: str
    warnings: list[str] | None = None

    @classmethod
    def fallback(cls, reason: str = "기본값 적용") -> "DispersionJudgement":
        return cls(dispersion_system="수상 솔루션", reason=reason, warnings=[])


@dataclass
class ProductFormJudgement:
    product_form: str
    reason: str
    warnings: list[str] | None = None

    @classmethod
    def fallback(cls, reason: str = "기본값 적용") -> "ProductFormJudgement":
        return cls(product_form="워터", reason=reason, warnings=[])


@dataclass
class FormulationDetailJudgement:
    target_viscosity: str
    ph_range_label: str
    ph_min: float
    ph_max: float
    reason: str
    warnings: list[str] | None = None

    @classmethod
    def fallback(cls, reason: str = "기본값 적용") -> "FormulationDetailJudgement":
        profile = PH_RANGE_PROFILES[DEFAULT_PH_RANGE_LABEL]
        return cls(
            target_viscosity="중점도",
            ph_range_label=DEFAULT_PH_RANGE_LABEL,
            ph_min=profile["min"],
            ph_max=profile["max"],
            reason=reason,
            warnings=[],
        )


@dataclass
class FormulationPurpose:
    product_form: str
    formulation_type: str
    target_viscosity: str
    ph_range_label: str
    ph_min: float
    ph_max: float
    reason: str

    @classmethod
    def fallback(cls, reason: str = "기본값 적용") -> "FormulationPurpose":
        profile = PH_RANGE_PROFILES[DEFAULT_PH_RANGE_LABEL]
        return cls(
            product_form="워터",
            formulation_type="수상 솔루션",
            target_viscosity="중점도",
            ph_range_label=DEFAULT_PH_RANGE_LABEL,
            ph_min=profile["min"],
            ph_max=profile["max"],
            reason=reason,
        )


@dataclass
class StructureSkeleton:
    formulation_type: str
    role_status: dict[str, str]
    structure: dict[str, list[str]]
    warnings: list[str]


@dataclass(frozen=True)
class BackboneIngredient:
    role: str
    name: str
    amount: float
    amount_note: str
    role_status: str


@dataclass(frozen=True)
class BackboneExcludedRole:
    role: str
    reason: str


@dataclass
class BackboneDesign:
    backbone_type: str
    design_summary: str
    formulation_type: str
    product_form: str
    continuous_phase_role: str
    ingredients: list[BackboneIngredient]
    role_ingredients: dict[str, list[str]]
    excluded_roles: list[BackboneExcludedRole]
    system_checks: dict[str, str]
    total_content: float
    warnings: list[str]

    @property
    def ingredient_names(self) -> list[str]:
        return [ingredient.name for ingredient in self.ingredients]


@dataclass(frozen=True)
class BackboneAction:
    action_type: str
    role: str | None = None
    target_value: str | None = None
    reason_code: str = ""
    note: str = ""


@dataclass(frozen=True)
class FormulationEffect:
    condition: str
    impact_type: str
    impact_level: str
    review_point: str


@dataclass(frozen=True)
class ConcentrationReviewPoint:
    condition: str
    review_type: str
    review_point: str


@dataclass
class ActiveSuitabilityReview:
    ingredient: str
    amount: float | None
    solubility: str
    stable_ph_range: str
    solubility_fit: str
    ph_fit: str
    formulation_effects: list[FormulationEffect]
    concentration_review_points: list[ConcentrationReviewPoint]
    backbone_modification_required: bool
    backbone_adjustment: str
    issue_codes: list[str]
    issues: list[str]
    backbone_actions: list[BackboneAction]
    required_backbone_changes: list[str]


@dataclass
class BackboneRevision:
    design_constraints: list[str]
    added_required_roles: list[str]
    rationale: list[str]
    source_actions: list[BackboneAction]


@dataclass(frozen=True)
class FinalBackboneIngredient:
    name: str
    content: float
    role: str
    reason: str


@dataclass
class FinalBackboneFormula:
    name: str
    concept: str
    backbone_type: str
    target_aspects: list[str]
    ingredients: list[FinalBackboneIngredient]
    total_pct: float


@dataclass
class FinalBackboneValidation:
    total_pct_valid: bool
    continuous_phase_qs_valid: bool
    sorted_by_content_desc: bool
    needs_backbone_redesign: bool


@dataclass
class FinalBackboneResult:
    backbone_formula: FinalBackboneFormula | None
    validation: FinalBackboneValidation
    redesign_feedback: list[str]
    design_rationale: str
    applied_revision: list[str]
    deferred_checks: list[str]
    warnings: list[str]


@dataclass
class EmbeddingIndex:
    model: Any | None
    embeddings: np.ndarray | None
    ingredient_names: list[str]

    @property
    def available(self) -> bool:
        return self.model is not None and self.embeddings is not None


def parse_args() -> CLIConfig:
    parser = argparse.ArgumentParser(
        description="화장품 처방 자동 생성 PoC v0.9 — 데이터 입력 계층"
    )
    parser.add_argument("--data", required=True, help="처방 CSV 파일")
    parser.add_argument("--product", required=True, help="마케팅 키워드 CSV 파일")
    parser.add_argument("--external", default="external.csv", help="타사 제품 CSV 파일")
    parser.add_argument("--query", required=True, help="제품 요구사항 텍스트")
    parser.add_argument("--aws_profile", default=None, help="AWS 프로파일명")
    parser.add_argument(
        "--aws_region",
        default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_AWS_REGION),
        help="AWS 리전",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL_ID, help="Bedrock 모델 ID")
    parser.add_argument("--output", default="output", help="결과 저장 디렉터리")
    parser.add_argument(
        "--load_embeddings",
        action="store_true",
        help="성분명 임베딩 인덱스를 로드한다",
    )
    parser.add_argument(
        "--allow_embedding_download",
        action="store_true",
        help="로컬 임베딩 모델이 없으면 Hugging Face에서 다운로드한다",
    )
    args = parser.parse_args()
    return CLIConfig(
        data_csv=args.data,
        product_csv=args.product,
        external_csv=args.external,
        query=args.query,
        aws_profile=args.aws_profile,
        aws_region=args.aws_region,
        model_id=args.model,
        output_dir=args.output,
        load_embeddings=args.load_embeddings,
        allow_embedding_download=args.allow_embedding_download,
    )


def safe_literal_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = ast.literal_eval(str(value))
    except (SyntaxError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value)).lower()


def strip_json_fences(value: Any) -> str:
    text = str(value).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text)


def cleanup_context_phrase(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip(" ,./")
    text = re.sub(
        r"비타민\s*([A-Za-z])",
        lambda match: f"비타민 {match.group(1).upper()}",
        text,
        flags=re.IGNORECASE,
    )
    return text


def normalize_context_phrase(value: Any) -> str:
    return re.sub(r"[\s\-_·•,/]+", "", str(value)).lower()


def dedupe_context_phrases(values: list[Any], max_len: int = 20) -> list[str]:
    """짧은 슬롯 표현만 남기고 중복/부분중복을 제거한다."""
    cleaned: list[str] = []
    seen: set[str] = set()

    for raw in values or []:
        text = cleanup_context_phrase(raw)
        if not text or re.fullmatch(r"[\d%\s.,~+\-]+", text):
            continue
        if len(text) > max_len:
            continue
        key = normalize_context_phrase(text)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    cleaned.sort(key=lambda item: len(normalize_context_phrase(item)), reverse=True)
    result: list[str] = []
    kept_keys: list[str] = []
    for text in cleaned:
        key = normalize_context_phrase(text)
        if any(key != kept and key in kept for kept in kept_keys):
            continue
        result.append(text)
        kept_keys.append(key)
    return result


def parse_pipe_list(value: Any) -> list[str]:
    if pd.isna(value):
        return []
    return [part.strip() for part in str(value).split("|") if part.strip()]


def require_columns(df: pd.DataFrame, required: set[str], source_name: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{source_name} 필수 컬럼 누락: {sorted(missing)}")


def normalize_ingredient_function(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_product_form(value: Any) -> str:
    compact = normalize_context_phrase(value)
    if "오일" in compact or "oil" in compact:
        return "오일"
    if "젤" in compact or "겔" in compact or "gel" in compact:
        return "젤(겔)"
    if any(token in compact for token in ("유액", "밀크", "로션", "lotion", "milk")):
        return "유액(밀크/로션)"
    if any(token in compact for token in ("워터", "water", "토너", "스킨", "에센스", "앰플")):
        return "워터"
    return "워터"


def normalize_dispersion_system(value: Any) -> str:
    compact = normalize_context_phrase(value)
    if "wo" in compact or "유중수" in compact:
        return "W/O 유화"
    if "ow" in compact or "수중유" in compact:
        return "O/W 유화"
    if "가용화" in compact or "solubil" in compact:
        return "가용화"
    if "수상솔루션" in compact or "수용액" in compact or "솔루션" in compact:
        return "수상 솔루션"
    if "분산" in compact or "현탁" in compact or "입자" in compact:
        return "분산"
    return "수상 솔루션"


def normalize_target_viscosity(value: Any) -> str:
    compact = normalize_context_phrase(value)
    if any(token in compact for token in ("저점도", "묽", "산뜻", "가볍", "워터")):
        return "저점도"
    if any(token in compact for token in ("고점도", "꾸덕", "쫀쫀", "리치", "크림")):
        return "고점도"
    return "중점도"


def normalize_ph_label(value: Any) -> str:
    compact = normalize_context_phrase(value)
    if "약알칼리" in compact:
        return "약알칼리성"
    if "약산성중성" in compact:
        return "약산성~중성"
    if "약산성" in compact:
        return "약산성"
    if "중성" in compact:
        return "중성"
    if "산성" in compact:
        return "산성"
    return DEFAULT_PH_RANGE_LABEL


def format_number(value: float) -> str:
    return f"{value:g}"


def format_range(min_value: float, max_value: float) -> str:
    return f"{format_number(min_value)}~{format_number(max_value)}"


def parse_ph_range(value: Any) -> tuple[float, float] | None:
    text = str(value).strip()
    if not text or text == "불명":
        return None

    label = normalize_ph_label(text)
    compact = normalize_context_phrase(text)
    if label in PH_RANGE_PROFILES and compact in {
        normalize_context_phrase(label),
        "ph" + normalize_context_phrase(label),
    }:
        profile = PH_RANGE_PROFILES[label]
        return float(profile["min"]), float(profile["max"])

    numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", text)]
    if len(numbers) >= 2:
        lo, hi = numbers[0], numbers[1]
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    if label in PH_RANGE_PROFILES:
        profile = PH_RANGE_PROFILES[label]
        return float(profile["min"]), float(profile["max"])
    return None


def ranges_overlap(
    left_min: float,
    left_max: float,
    right_min: float,
    right_max: float,
) -> bool:
    return max(left_min, right_min) <= min(left_max, right_max)


def range_contains(
    outer_min: float,
    outer_max: float,
    inner_min: float,
    inner_max: float,
) -> bool:
    return outer_min <= inner_min and inner_max <= outer_max


class FormulaDataLoader:
    """자사 처방 DB를 로드하고, 이후 단계가 공통으로 쓰는 성분 통계를 만든다."""

    def load(self, csv_path: str | Path) -> FormulaDataset:
        raw = pd.read_csv(csv_path, engine="python")
        require_columns(raw, FORMULA_REQUIRED_COLUMNS, "data.csv")

        df = raw.copy()
        df["ingredient_name"] = df["ingredient_name"].astype(str).str.strip()
        df["ingredient_function"] = (
            df["ingredient_function"]
            .astype(str)
            .map(normalize_ingredient_function)
        )
        df["content"] = pd.to_numeric(df["content"], errors="coerce")
        df = df.dropna(subset=["content", "ingredient_name"])

        formulas = self._build_formula_index(df)
        ingredient_stats = self._build_ingredient_stats(formulas)
        unmapped_functions = self._find_unmapped_functions(df)
        return FormulaDataset(
            raw=df,
            formulas=formulas,
            ingredient_stats=ingredient_stats,
            unmapped_functions=unmapped_functions,
        )

    def _build_formula_index(self, df: pd.DataFrame) -> dict[str, dict[str, Any]]:
        formulas: dict[str, dict[str, Any]] = {}

        for bulk_code, group in df.groupby("bulk_code"):
            if group["content"].sum() <= 0:
                continue

            ingredients: dict[str, float] = {}
            structural_roles: dict[str, str] = {}

            for _, row in group.iterrows():
                name = str(row["ingredient_name"]).strip()
                if not name:
                    continue

                ingredients[name] = round(float(row["content"]), 6)
                role = FUNC_TO_ROLE.get(row["ingredient_function"])
                if role:
                    structural_roles[name] = role
                elif name in KNOWN_BASE_INGREDIENTS:
                    structural_roles[name] = "base"

            formulas[str(bulk_code)] = {
                "name": str(group["bulk_name"].iloc[0]),
                "ingredients": ingredients,
                "structural_roles": structural_roles,
            }

        return formulas

    def _build_ingredient_stats(
        self,
        formulas: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        contents_by_ingredient: dict[str, list[float]] = defaultdict(list)
        roles_by_ingredient: dict[str, str] = {}

        for formula in formulas.values():
            for name, content in formula["ingredients"].items():
                contents_by_ingredient[name].append(float(content))
                role = formula["structural_roles"].get(name)
                if role and name not in roles_by_ingredient:
                    roles_by_ingredient[name] = role

        total_formulas = max(len(formulas), 1)
        stats: dict[str, dict[str, Any]] = {}
        for name, values in contents_by_ingredient.items():
            arr = np.array(values, dtype=float)
            stats[name] = {
                "structural_role": roles_by_ingredient.get(name, "active_or_unknown"),
                "frequency": round(len(values) / total_formulas, 3),
                "count": len(values),
                "min": round(float(np.min(arr)), 4),
                "max": round(float(np.max(arr)), 4),
                "median": round(float(np.median(arr)), 4),
                "mean": round(float(np.mean(arr)), 4),
                "std": round(float(np.std(arr)), 4),
                "p25": round(float(np.percentile(arr, 25)), 4),
                "p75": round(float(np.percentile(arr, 75)), 4),
            }
        return stats

    def _find_unmapped_functions(self, df: pd.DataFrame) -> list[str]:
        known_functions = {
            function
            for functions in ROLE_FUNCTION_MAP.values()
            for function in functions
        } | set(FUNC_TO_ROLE)
        functions = set(df["ingredient_function"].dropna().astype(str).str.strip())
        return sorted(functions - known_functions)


class ProductKeywordLoader:
    """마케팅 키워드와 처방 성분의 연결 정보를 만든다."""

    def load(
        self,
        csv_path: str | Path,
        formula_dataset: FormulaDataset,
    ) -> ProductKeywordDataset:
        raw = pd.read_csv(csv_path, engine="python")
        require_columns(raw, PRODUCT_REQUIRED_COLUMNS, "product.csv")

        formula_codes = set(formula_dataset.formulas)
        linked = raw[raw["bulk_code"].astype(str).isin(formula_codes)].copy()
        keyword_db: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"ingredients": Counter(), "aspects": set(), "formula_codes": []}
        )

        for _, row in linked.drop_duplicates("bulk_code").iterrows():
            code = str(row["bulk_code"])
            formula = formula_dataset.formulas[code]
            active_ingredients = self._top_active_ingredients(formula)

            for keyword in safe_literal_list(row.get("marketing_keywords_list", [])):
                keyword = str(keyword).strip()
                if not keyword:
                    continue
                keyword_db[keyword]["ingredients"].update(active_ingredients)
                keyword_db[keyword]["aspects"].update(
                    str(aspect).strip()
                    for aspect in safe_literal_list(row.get("aspect_list", []))
                    if str(aspect).strip()
                )
                keyword_db[keyword]["formula_codes"].append(code)

        return ProductKeywordDataset(
            raw=raw,
            keyword_db=dict(keyword_db),
            linked_bulk_count=linked["bulk_code"].nunique(),
        )

    def _top_active_ingredients(self, formula: dict[str, Any], limit: int = 10) -> list[str]:
        ingredients = sorted(
            formula["ingredients"].items(),
            key=lambda item: -item[1],
        )
        return [
            name
            for name, _ in ingredients
            if formula["structural_roles"].get(name) not in BASE_ROLES
            and name not in KNOWN_BASE_INGREDIENTS
        ][:limit]


class ExternalProductLoader:
    """타사 제품명과 대표 성분 목록을 로드한다."""

    def load(self, csv_path: str | Path) -> ExternalProductDataset:
        path = Path(csv_path)
        if not path.exists():
            return ExternalProductDataset(raw=pd.DataFrame(), products=[])

        raw = pd.read_csv(path, engine="python")
        require_columns(raw, EXTERNAL_REQUIRED_COLUMNS, "external.csv")

        products = [
            ExternalProduct(
                title=str(row["title"]).strip(),
                base_time=str(row["base_time"]).strip(),
                representation_ingredients=parse_pipe_list(
                    row["representation_ingredients"]
                ),
            )
            for _, row in raw.drop_duplicates(["title", "base_time"]).iterrows()
        ]
        products.sort(key=lambda item: pd.to_datetime(item.base_time, errors="coerce"), reverse=True)
        return ExternalProductDataset(raw=raw, products=products)

    def find_by_title_contains(
        self,
        dataset: ExternalProductDataset,
        target_product: str,
    ) -> list[ExternalProduct]:
        target = compact_text(target_product)
        if not target:
            return []
        return [
            product
            for product in dataset.products
            if target in compact_text(product.title)
        ]


class EmbeddingModelLoader:
    """성분명 유사도 검색에 사용할 임베딩 모델을 준비한다."""

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        model_dir: Path = DEFAULT_EMBEDDING_DIR,
    ) -> None:
        self.model_name = model_name
        self.model_dir = model_dir

    def load(
        self,
        ingredient_names: list[str],
        allow_download: bool = False,
    ) -> EmbeddingIndex:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            return EmbeddingIndex(model=None, embeddings=None, ingredient_names=ingredient_names)

        if self.model_dir.exists():
            model = SentenceTransformer(str(self.model_dir), local_files_only=True)
        elif allow_download:
            model = SentenceTransformer(self.model_name)
            model.save(str(self.model_dir))
        else:
            return EmbeddingIndex(model=None, embeddings=None, ingredient_names=ingredient_names)

        embeddings = model.encode(
            ingredient_names,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return EmbeddingIndex(
            model=model,
            embeddings=np.asarray(embeddings),
            ingredient_names=ingredient_names,
        )


class BedrockClient:
    """AWS Bedrock Claude 호출을 한 곳에서 관리한다."""

    def __init__(
        self,
        aws_profile: str | None,
        aws_region: str,
        model_id: str,
    ) -> None:
        self.aws_profile = aws_profile
        self.aws_region = aws_region
        self.model_id = model_id
        self._client = None

    def connect(self) -> None:
        if boto3 is None:
            raise RuntimeError("boto3가 설치되어 있지 않습니다.")
        if self.aws_profile:
            session = boto3.Session(
                profile_name=self.aws_profile,
                region_name=self.aws_region,
            )
        else:
            session = boto3.Session(region_name=self.aws_region)
        self._client = session.client("bedrock-runtime", region_name=self.aws_region)

    def invoke_json(
        self,
        prompt: str,
        max_tokens: int = 1000,
        system: str | None = None,
        temperature: float = 0,
    ) -> Any:
        if self._client is None:
            self.connect()

        payload: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        response = self._client.invoke_model(
            modelId=self.model_id,
            body=json.dumps(payload, ensure_ascii=False),
        )
        body = json.loads(response["body"].read())
        text = str(body["content"][0]["text"]).strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)


class QueryContextExtractor:
    """사용자 질의를 처방 설계용 슬롯으로 변환한다."""

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        self.bedrock = bedrock

    def extract(self, query: str) -> QueryContext:
        if not self.bedrock:
            return QueryContext.empty()

        try:
            parsed = self.bedrock.invoke_json(
                self._build_prompt(query),
                max_tokens=800,
                temperature=0,
            )
        except Exception as exc:
            print(f"질의 맥락 추출 실패: {exc}")
            return QueryContext.empty()

        if not isinstance(parsed, dict):
            return QueryContext.empty()
        return self._parse_response(parsed)

    def _build_prompt(self, query: str) -> str:
        hints = self._build_alias_hint_section(query)
        return (
            "다음 화장품 제품 요구사항 문장을 처방 설계용 슬롯으로 구조화하세요.\n"
            "반드시 JSON만 반환하세요.\n\n"
            "[슬롯 정의]\n"
            "- ingredients: 원료/효능성 소재명과 해당 성분의 용해성, 안정 pH 범위\n"
            "- formulation: 제형, 물성, 외관, 사용감 표현\n"
            "- marketing_points: 미백, 브라이트닝, 안티에이징, 진정 같은 마케팅 소구 표현\n"
            "- target_product: 사용자가 베이스/참조/타겟으로 삼으려는 구체적인 제품명\n"
            "- ingredient_constraints: 특정 성분에 함량(%)을 직접 지정한 경우\n\n"
            "[중요 제약]\n"
            "- 전체 문장을 복사하지 말 것\n"
            "- 원문에서 직접 뽑은 짧은 명사구만 반환할 것\n"
            "- 없는 슬롯은 빈 배열\n"
            "- 숫자/퍼센트만 있는 조각은 제외할 것\n"
            "- target_product에는 제품명만 넣고 일반 효능/제형/성분 표현은 넣지 말 것\n"
            "- ingredient_constraints.amount는 단위 없이 숫자만 입력할 것\n"
            "- 관용명/음차 표현도 성분 표현이면 ingredients에 포함할 것\n"
            "- ingredients.solubility는 수용성, 유용성, 양친성, 난용성, 불용성, 불명 중 하나\n"
            "- ingredients.stable_ph_range는 알려진 안정 pH 범위 문자열. 모르면 불명\n"
            f"{hints}\n"
            f"[입력 문장]\n{query}\n\n"
            "반환 형식:\n"
            "{"
            "\"ingredients\": [{\"name\": \"성분명\", \"solubility\": \"수용성\", \"stable_ph_range\": \"pH 5~7\"}], "
            "\"formulation\": [\"...\"], "
            "\"marketing_points\": [\"...\"], "
            "\"target_product\": [\"...\"], "
            "\"ingredient_constraints\": [{\"ingredient\": \"성분명\", \"amount\": 숫자}]"
            "}"
        )

    def _build_alias_hint_section(self, query: str) -> str:
        lowered_query = query.lower()
        lines = [
            f"  · {alias} → {', '.join(targets)}"
            for alias, targets in ALIAS_HINTS.items()
            if alias.lower() in lowered_query
        ]
        if not lines:
            return ""
        return "\n[성분 이명 힌트]\n" + "\n".join(lines) + "\n"

    def _parse_response(self, parsed: dict[str, Any]) -> QueryContext:
        return QueryContext(
            ingredients=self._parse_ingredients(self._as_list(parsed.get("ingredients"))),
            formulation=dedupe_context_phrases(
                self._as_list(parsed.get("formulation")),
                max_len=30,
            ),
            marketing_points=dedupe_context_phrases(
                self._as_list(parsed.get("marketing_points")),
                max_len=30,
            ),
            target_product=dedupe_context_phrases(
                self._as_list(parsed.get("target_product")),
                max_len=80,
            ),
            ingredient_constraints=self._parse_constraints(
                self._as_list(parsed.get("ingredient_constraints"))
            ),
        )

    def _parse_ingredients(self, values: list[Any]) -> list[IngredientMention]:
        mentions: list[IngredientMention] = []
        seen: set[str] = set()

        for value in values:
            if isinstance(value, dict):
                name = cleanup_context_phrase(value.get("name", ""))
                solubility = cleanup_context_phrase(value.get("solubility", ""))
                stable_ph_range = cleanup_context_phrase(value.get("stable_ph_range", ""))
            else:
                name = cleanup_context_phrase(value)
                solubility = "불명"
                stable_ph_range = "불명"

            if not name or len(name) > 30:
                continue
            key = normalize_context_phrase(name)
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)

            if solubility not in SOLUBILITY_OPTIONS:
                solubility = "불명"
            if not stable_ph_range:
                stable_ph_range = "불명"

            mentions.append(
                IngredientMention(
                    name=name,
                    solubility=solubility,
                    stable_ph_range=stable_ph_range,
                )
            )

        return mentions

    def _parse_constraints(self, values: list[Any]) -> list[IngredientConstraint]:
        constraints: list[IngredientConstraint] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            ingredient = cleanup_context_phrase(value.get("ingredient", ""))
            amount = self._to_float(value.get("amount"))
            if ingredient and amount is not None:
                constraints.append(
                    IngredientConstraint(ingredient=ingredient, amount=amount)
                )
        return constraints

    def _as_list(self, value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            match = re.search(r"-?\d+(?:\.\d+)?", str(value))
            return float(match.group()) if match else None


class DispersionSystemExtractor:
    """질의 성분과 사용감 단서로 분산계를 먼저 판단한다."""

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        self.bedrock = bedrock

    def extract(self, query: str, context: QueryContext) -> DispersionJudgement:
        if not self.bedrock:
            return DispersionJudgement.fallback("Bedrock 클라이언트 없음")

        try:
            parsed = self.bedrock.invoke_json(
                self._build_prompt(query, context),
                max_tokens=400,
                temperature=0,
            )
        except Exception as exc:
            print(f"분산계 판단 실패: {exc}")
            return DispersionJudgement.fallback("Claude 호출 실패")

        if not isinstance(parsed, dict):
            return DispersionJudgement.fallback("Claude 응답 형식 오류")
        return self._parse_response(parsed, context)

    def _build_prompt(self, query: str, context: QueryContext) -> str:
        ingredient_lines = [
            f"{item.name} (용해성: {item.solubility}, 안정 pH: {item.stable_ph_range})"
            for item in context.ingredients
        ]
        return (
            "다음 사용자 질의와 성분 물성 정보를 보고 목표 분산계를 먼저 판단하세요.\n"
            "반드시 JSON만 반환하세요.\n\n"
            f"[선택지]\n{', '.join(FORMULATION_TYPE_OPTIONS)} 중 하나\n\n"
            "[판단 기준]\n"
            "- 수용성 성분 중심의 투명 제형은 수상 솔루션\n"
            "- 유용성/난용성 성분을 투명하게 안정화해야 하면 가용화\n"
            "- 물과 오일이 함께 있고 외상이 물이면 O/W 유화\n"
            "- 물과 오일이 함께 있고 외상이 오일이면 W/O 유화\n"
            "- 불용성 입자, 파우더, 현탁, 캡슐 단서가 있으면 분산\n"
            "- 젤(겔)은 분산계가 아니라 제품 형상/점도 단서\n\n"
            "[입력]\n"
            f"- 사용자 질의: {query}\n"
            f"- 성분 표현: {', '.join(ingredient_lines) or '없음'}\n"
            f"- 제형/사용감 표현: {', '.join(context.formulation) or '없음'}\n"
            f"- 마케팅 포인트: {', '.join(context.marketing_points) or '없음'}\n\n"
            "반환 형식:\n"
            "{"
            "\"dispersion_system\": \"...\", "
            "\"reason\": \"판단 근거 한 줄\""
            "}"
        )

    def _parse_response(
        self,
        parsed: dict[str, Any],
        context: QueryContext,
    ) -> DispersionJudgement:
        dispersion_system = self._normalize_dispersion_system(
            parsed.get("dispersion_system", "")
        )
        reason = cleanup_context_phrase(parsed.get("reason", "")) or "근거 없음"
        return DispersionJudgement(
            dispersion_system=dispersion_system,
            reason=reason,
            warnings=self._build_warnings(dispersion_system, context),
        )

    def _normalize_dispersion_system(self, value: Any) -> str:
        return normalize_dispersion_system(value)

    def _build_warnings(
        self,
        dispersion_system: str,
        context: QueryContext,
    ) -> list[str]:
        if dispersion_system != "가용화" or not context.ingredients:
            return []
        solubilities = {item.solubility for item in context.ingredients}
        if solubilities and solubilities <= {"수용성"}:
            return ["추출 성분이 모두 수용성이므로 가용화 판단 근거 확인 필요"]
        return []


class ProductFormExtractor:
    """질의 맥락과 분산계를 근거로 제품 형상만 판단한다."""

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        self.bedrock = bedrock

    def extract(
        self,
        query: str,
        context: QueryContext,
        dispersion: DispersionJudgement,
    ) -> ProductFormJudgement:
        if not self.bedrock:
            return ProductFormJudgement.fallback("Bedrock 클라이언트 없음")
        try:
            parsed = self.bedrock.invoke_json(
                self._build_prompt(query, context, dispersion),
                max_tokens=300,
                temperature=0,
            )
        except Exception as exc:
            print(f"제형 판단 실패: {exc}")
            return ProductFormJudgement.fallback("Claude 호출 실패")
        if not isinstance(parsed, dict):
            return ProductFormJudgement.fallback("Claude 응답 형식 오류")
        return self._parse_response(parsed, context, dispersion)

    def _build_prompt(
        self,
        query: str,
        context: QueryContext,
        dispersion: DispersionJudgement,
    ) -> str:
        return (
            "다음 질의 맥락과 확정 분산계를 보고 제품 형상만 판단하세요.\n"
            "반드시 JSON만 반환하세요.\n\n"
            f"[선택지]\nproduct_form: {', '.join(PRODUCT_FORM_OPTIONS)} 중 하나\n\n"
            "[판단 기준]\n"
            "- 워터: 토너, 앰플, 에센스처럼 흐름성이 큰 수상 제형\n"
            "- 젤(겔): 젤, 겔, 젤타입, 탱글한 네트워크 질감\n"
            "- 유액(밀크/로션): 밀크, 로션, 유액, 에멀전 질감\n"
            "- 오일: 오일, 밤, 무수 또는 유상 중심 질감\n\n"
            "[입력]\n"
            f"- 사용자 질의: {query}\n"
            f"- 확정 분산계: {dispersion.dispersion_system} ({dispersion.reason})\n"
            f"- 제형/사용감 표현: {', '.join(context.formulation) or '없음'}\n\n"
            "반환 형식:\n"
            "{\"product_form\": \"...\", \"reason\": \"판단 근거 한 줄\"}"
        )

    def _parse_response(
        self,
        parsed: dict[str, Any],
        context: QueryContext,
        dispersion: DispersionJudgement,
    ) -> ProductFormJudgement:
        product_form = normalize_product_form(parsed.get("product_form", ""))
        reason = cleanup_context_phrase(parsed.get("reason", "")) or "근거 없음"
        return ProductFormJudgement(
            product_form=product_form,
            reason=reason,
            warnings=self._build_warnings(product_form, context, dispersion),
        )

    def _build_warnings(
        self,
        product_form: str,
        context: QueryContext,
        dispersion: DispersionJudgement,
    ) -> list[str]:
        terms = " ".join(context.formulation)
        compact_terms = normalize_context_phrase(terms)
        warnings: list[str] = []

        gel_terms = ("젤", "겔", "탱글", "쫀쫀", "점성", "고점도", "gel")
        opaque_terms = ("불투명", "탁", "밀키", "뿌연", "흐린")
        oil_terms = ("오일", "밤", "무수", "유상", "oil")
        transparent_terms = ("투명", "맑", "클리어", "clear")
        lotion_terms = ("유액", "로션", "밀크", "에멀전", "lotion", "milk")

        if product_form == "워터" and self._has_any(compact_terms, gel_terms):
            warnings.append("점성/젤 사용감 단서가 있어 워터 판단 근거 확인 필요")
        if product_form == "워터" and self._has_any(compact_terms, opaque_terms):
            warnings.append("불투명/탁한 외관 단서가 있어 워터 판단 근거 확인 필요")
        if product_form == "오일" and dispersion.dispersion_system == "수상 솔루션":
            warnings.append("수상 솔루션 분산계와 오일 제품 형상 판단 간 근거 확인 필요")
        if product_form == "오일" and not self._has_any(compact_terms, oil_terms):
            warnings.append("오일형 표현이 약해 오일 판단 근거 확인 필요")
        if product_form == "유액(밀크/로션)" and self._has_any(compact_terms, transparent_terms):
            warnings.append("투명/맑은 외관 표현과 유액 판단 간 근거 확인 필요")
        if product_form == "젤(겔)" and self._has_any(compact_terms, lotion_terms):
            warnings.append("로션/유액 표현과 젤 판단 간 근거 확인 필요")

        return warnings

    def _has_any(self, compact_text_value: str, terms: tuple[str, ...]) -> bool:
        return any(normalize_context_phrase(term) in compact_text_value for term in terms)


class FormulationDetailExtractor:
    """질의 맥락, 분산계, 확정 제품 형상을 근거로 점도와 pH만 판단한다."""

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        self.bedrock = bedrock

    def extract(
        self,
        query: str,
        context: QueryContext,
        dispersion: DispersionJudgement,
        product_form: ProductFormJudgement,
    ) -> FormulationDetailJudgement:
        if not self.bedrock:
            return FormulationDetailJudgement.fallback("Bedrock 클라이언트 없음")
        try:
            parsed = self.bedrock.invoke_json(
                self._build_prompt(query, context, dispersion, product_form),
                max_tokens=400,
                temperature=0,
            )
        except Exception as exc:
            print(f"세부 제형 판단 실패: {exc}")
            return FormulationDetailJudgement.fallback("Claude 호출 실패")
        if not isinstance(parsed, dict):
            return FormulationDetailJudgement.fallback("Claude 응답 형식 오류")
        return self._parse_response(parsed, context, product_form)

    def _build_prompt(
        self,
        query: str,
        context: QueryContext,
        dispersion: DispersionJudgement,
        product_form: ProductFormJudgement,
    ) -> str:
        ingredient_lines = [
            f"{item.name} (용해성: {item.solubility}, 안정 pH: {item.stable_ph_range})"
            for item in context.ingredients
        ]
        return (
            "다음 정보를 근거로 목표 점도와 목표 pH 범위만 판단하세요.\n"
            "반드시 JSON만 반환하세요.\n\n"
            f"[점도 선택지]\n{', '.join(TARGET_VISCOSITY_OPTIONS)} 중 하나\n\n"
            "[pH 범위 기준]\n"
            "- 산성: pH 3.0~4.5\n"
            "- 약산성: pH 4.5~6.0\n"
            "- 약산성~중성: pH 5.0~7.0\n"
            "- 중성: pH 6.0~7.5\n"
            "- 약알칼리성: pH 7.5~8.5\n"
            "- 명확한 근거가 없으면 약산성~중성을 선택\n\n"
            "[입력]\n"
            f"- 사용자 질의: {query}\n"
            f"- 확정 분산계: {dispersion.dispersion_system}\n"
            f"- 확정 제품 형상: {product_form.product_form} ({product_form.reason})\n"
            f"- 성분 표현: {', '.join(ingredient_lines) or '없음'}\n"
            f"- 제형/사용감 표현: {', '.join(context.formulation) or '없음'}\n\n"
            "반환 형식:\n"
            "{"
            "\"target_viscosity\": \"...\", "
            "\"ph_range_label\": \"...\", "
            "\"reason\": \"판단 근거 한 줄\""
            "}"
        )

    def _parse_response(
        self,
        parsed: dict[str, Any],
        context: QueryContext,
        product_form: ProductFormJudgement,
    ) -> FormulationDetailJudgement:
        target_viscosity = normalize_target_viscosity(parsed.get("target_viscosity", ""))
        ph_label = normalize_ph_label(parsed.get("ph_range_label", ""))
        profile = PH_RANGE_PROFILES[ph_label]
        reason = cleanup_context_phrase(parsed.get("reason", "")) or "근거 없음"
        return FormulationDetailJudgement(
            target_viscosity=target_viscosity,
            ph_range_label=ph_label,
            ph_min=profile["min"],
            ph_max=profile["max"],
            reason=reason,
            warnings=self._build_warnings(
                target_viscosity,
                ph_label,
                float(profile["min"]),
                float(profile["max"]),
                context,
                product_form,
            ),
        )

    def _build_warnings(
        self,
        target_viscosity: str,
        ph_label: str,
        ph_min: float,
        ph_max: float,
        context: QueryContext,
        product_form: ProductFormJudgement,
    ) -> list[str]:
        terms = " ".join(context.formulation)
        compact_terms = normalize_context_phrase(terms)
        warnings: list[str] = []

        low_viscosity_terms = ("액상", "묽", "산뜻", "가볍", "끈적이지", "워터", "water")
        high_viscosity_terms = ("점성", "고점도", "쫀쫀", "꾸덕", "리치", "젤", "겔", "탱글")

        if target_viscosity == "고점도" and self._has_any(compact_terms, low_viscosity_terms):
            warnings.append("액상/산뜻한 사용감 단서가 있어 고점도 판단 근거 확인 필요")
        if target_viscosity == "저점도" and self._has_any(compact_terms, high_viscosity_terms):
            warnings.append("점성/젤 사용감 단서가 있어 저점도 판단 근거 확인 필요")
        if product_form.product_form == "젤(겔)" and target_viscosity == "저점도":
            warnings.append("젤 제품 형상과 저점도 판단 간 근거 확인 필요")
        if product_form.product_form == "워터" and target_viscosity == "고점도":
            warnings.append("워터 제품 형상과 고점도 판단 간 근거 확인 필요")

        for ingredient in context.ingredients:
            active_range = parse_ph_range(ingredient.stable_ph_range)
            if active_range is None:
                continue
            active_min, active_max = active_range
            if range_contains(active_min, active_max, ph_min, ph_max):
                continue
            if ranges_overlap(active_min, active_max, ph_min, ph_max):
                warnings.append(
                    f"{ingredient.name} 안정 pH와 목표 pH가 일부만 겹쳐 목표 pH 범위 확인 필요"
                )
            else:
                warnings.append(
                    f"{ingredient.name} 안정 pH와 목표 pH가 맞지 않아 pH 판단 근거 확인 필요"
                )

        return self._dedupe(warnings)

    def _has_any(self, compact_text_value: str, terms: tuple[str, ...]) -> bool:
        return any(normalize_context_phrase(term) in compact_text_value for term in terms)

    def _dedupe(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result


def build_formulation_purpose(
    dispersion: DispersionJudgement,
    product_form: ProductFormJudgement,
    detail: FormulationDetailJudgement,
) -> FormulationPurpose:
    return FormulationPurpose(
        product_form=product_form.product_form,
        formulation_type=dispersion.dispersion_system,
        target_viscosity=detail.target_viscosity,
        ph_range_label=detail.ph_range_label,
        ph_min=detail.ph_min,
        ph_max=detail.ph_max,
        reason=f"{product_form.reason} / {detail.reason}",
    )


class StructureSkeletonDesigner:
    """제형 목적을 구조 role로 바꾸고, role별 DB 후보 성분을 고른다."""

    def design(
        self,
        purpose: FormulationPurpose,
        formula_dataset: FormulaDataset,
        candidates_per_role: int = 10,
    ) -> StructureSkeleton:
        role_status = self._decide_roles(purpose)
        structure: dict[str, list[str]] = {}
        warnings: list[str] = []

        for role, status in role_status.items():
            if status == "excluded":
                continue
            candidates = self._select_candidates(role, formula_dataset, candidates_per_role)
            structure[role] = candidates
            if not candidates:
                warnings.append(f"{role}: DB 후보 없음")

        return StructureSkeleton(
            formulation_type=purpose.formulation_type,
            role_status=role_status,
            structure=structure,
            warnings=warnings,
        )

    def _decide_roles(self, purpose: FormulationPurpose) -> dict[str, str]:
        roles = dict(
            ROLE_SETS_BY_FORMULATION_TYPE.get(
                purpose.formulation_type,
                ROLE_SETS_BY_FORMULATION_TYPE["수상 솔루션"],
            )
        )
        self._apply_product_form_rules(roles, purpose.product_form, purpose.formulation_type)
        self._apply_viscosity_rules(roles, purpose.target_viscosity, purpose.product_form)
        self._apply_ph_rules(roles, purpose.ph_range_label)
        return {role: status for role, status in roles.items() if status != "excluded"}

    def _apply_product_form_rules(
        self,
        roles: dict[str, str],
        product_form: str,
        formulation_type: str,
    ) -> None:
        if product_form == "워터":
            if "oil_phase" in roles:
                roles["oil_phase"] = "optional"
            roles["rheology_modifier"] = roles.get("rheology_modifier", "optional")
        elif product_form == "젤(겔)":
            roles["rheology_modifier"] = "required"
            roles["stabilizer"] = "required"
        elif product_form == "유액(밀크/로션)":
            roles["water_phase"] = "required"
            roles["oil_phase"] = "required"
            roles["emulsifier"] = "required"
        elif product_form == "오일":
            roles["oil_phase"] = "required"
            if roles.get("water_phase") == "required" and formulation_type != "W/O 유화":
                roles["water_phase"] = "optional"
            roles["oil_structurant"] = roles.get("oil_structurant", "optional")

    def _apply_viscosity_rules(
        self,
        roles: dict[str, str],
        target_viscosity: str,
        product_form: str,
    ) -> None:
        if product_form == "젤(겔)":
            roles["rheology_modifier"] = "required"
            roles["stabilizer"] = "required"
            return
        if target_viscosity == "저점도":
            if roles.get("rheology_modifier") == "required":
                roles["rheology_modifier"] = "optional"
            if roles.get("stabilizer") == "required":
                roles["stabilizer"] = "optional"
        elif target_viscosity == "고점도":
            roles["rheology_modifier"] = "required"
            roles["stabilizer"] = "required"

    def _apply_ph_rules(self, roles: dict[str, str], ph_range_label: str) -> None:
        roles["ph_adjuster"] = "required"
        if ph_range_label in {"산성", "중성", "약알칼리성"}:
            roles["buffer"] = "optional"

    def _select_candidates(
        self,
        role: str,
        formula_dataset: FormulaDataset,
        limit: int,
    ) -> list[str]:
        functions = set(ROLE_FUNCTION_MAP.get(role, []))
        hints = ROLE_NAME_HINTS.get(role, [])
        rows = formula_dataset.raw[["ingredient_name", "ingredient_function"]].drop_duplicates()

        scored: list[tuple[float, str]] = []
        hinted: list[tuple[float, str]] = []
        for _, row in rows.iterrows():
            name = str(row["ingredient_name"]).strip()
            function = str(row["ingredient_function"]).strip()
            if not name:
                continue

            function_match = function in functions
            hint_match = self._matches_hint(name, hints)
            if not function_match and not hint_match:
                continue

            stats = formula_dataset.ingredient_stats.get(name, {})
            score = float(stats.get("frequency", 0)) * 100
            score += min(float(stats.get("count", 0)), 50) * 0.2
            if hint_match:
                score += 30
            if name in KNOWN_BASE_INGREDIENTS:
                score += 10

            item = (score, name)
            scored.append(item)
            if hint_match:
                hinted.append(item)

        # 기능명이 넓은 role은 이름 힌트 후보를 우선한다.
        source = hinted if role in {"oil_phase", "oil_structurant", "solubilizer"} and hinted else scored
        source.sort(key=lambda item: (-item[0], item[1]))

        result: list[str] = []
        seen: set[str] = set()
        for _, name in source:
            if name in seen:
                continue
            seen.add(name)
            result.append(name)
            if len(result) >= limit:
                break
        return result

    def _matches_hint(self, ingredient_name: str, hints: list[str]) -> bool:
        compact_name = normalize_context_phrase(ingredient_name)
        return any(normalize_context_phrase(hint) in compact_name for hint in hints)


class BackboneDesigner:
    """확정된 제형 목적과 구조 골격을 실제 기본 처방 틀로 변환한다."""

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        self.bedrock = bedrock

    def design(
        self,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        formula_dataset: FormulaDataset,
        context: QueryContext | None = None,
    ) -> BackboneDesign:
        fallback = self._design_rule_based(purpose, skeleton, formula_dataset)
        if not self.bedrock:
            return fallback

        try:
            parsed = self.bedrock.invoke_json(
                self._build_llm_prompt(purpose, skeleton, formula_dataset, context),
                max_tokens=2200,
                temperature=0,
            )
        except Exception as exc:
            print(f"Backbone 설계 LLM 호출 실패: {exc}")
            return fallback

        design = self._parse_llm_design(
            parsed,
            purpose,
            skeleton,
            formula_dataset,
            context,
        )
        return design or fallback

    def _design_rule_based(
        self,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        formula_dataset: FormulaDataset,
    ) -> BackboneDesign:
        continuous_phase_role = self._continuous_phase_role(purpose, skeleton)
        used_names: set[str] = set()
        fixed_ingredients: list[BackboneIngredient] = []
        continuous_ingredient: BackboneIngredient | None = None
        warnings: list[str] = []

        for role, status in skeleton.role_status.items():
            if not self._include_role(role, status, purpose):
                continue
            ingredient_name = self._select_ingredient(
                role,
                skeleton.structure.get(role, []),
                used_names,
            )
            if not ingredient_name:
                warnings.append(f"{role}: Backbone 성분 확정 불가")
                continue

            used_names.add(ingredient_name)
            if role == continuous_phase_role:
                continuous_ingredient = BackboneIngredient(
                    role=role,
                    name=ingredient_name,
                    amount=0.0,
                    amount_note="q.s.",
                    role_status=status,
                )
                continue

            amount = self._target_amount(
                role,
                ingredient_name,
                purpose,
                formula_dataset,
            )
            fixed_ingredients.append(
                BackboneIngredient(
                    role=role,
                    name=ingredient_name,
                    amount=amount,
                    amount_note="DB median 기반 role 기본값",
                    role_status=status,
                )
            )

        fixed_total = round(sum(item.amount for item in fixed_ingredients), 4)
        if continuous_ingredient is None:
            warnings.append(f"{continuous_phase_role}: q.s. 연속상 성분 없음")
            ingredients = self._round_ingredients(fixed_ingredients)
        else:
            residual = round(100.0 - fixed_total, 4)
            if residual < 1.0:
                warnings.append("고정 성분 합이 99% 이상이어서 연속상 함량을 1%로 보정")
                residual = 1.0
                fixed_ingredients = self._scale_fixed_ingredients(
                    fixed_ingredients,
                    target_total=99.0,
                )
            ingredients = [
                BackboneIngredient(
                    role=continuous_ingredient.role,
                    name=continuous_ingredient.name,
                    amount=residual,
                    amount_note="100% q.s.",
                    role_status=continuous_ingredient.role_status,
                )
            ] + fixed_ingredients
            ingredients = self._round_to_total(ingredients)

        role_ingredients: dict[str, list[str]] = defaultdict(list)
        for ingredient in ingredients:
            role_ingredients[ingredient.role].append(ingredient.name)

        return BackboneDesign(
            backbone_type=self._default_backbone_type(purpose),
            design_summary="rule fallback: 구조 골격 후보와 DB 통계 기반 기본 Backbone",
            formulation_type=purpose.formulation_type,
            product_form=purpose.product_form,
            continuous_phase_role=continuous_phase_role,
            ingredients=ingredients,
            role_ingredients=dict(role_ingredients),
            excluded_roles=self._rule_excluded_roles(skeleton, ingredients),
            system_checks=self._default_system_checks(purpose, ingredients),
            total_content=round(sum(item.amount for item in ingredients), 4),
            warnings=warnings,
        )

    def _build_llm_prompt(
        self,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        formula_dataset: FormulaDataset,
        context: QueryContext | None = None,
    ) -> str:
        candidate_stats = self._candidate_stats_payload(skeleton, formula_dataset)
        dataset_summary = self._formula_dataset_summary(skeleton, formula_dataset)
        active_context = self._active_context_payload(context)
        purpose_payload = {
            "product_form": purpose.product_form,
            "formulation_type": purpose.formulation_type,
            "target_viscosity": purpose.target_viscosity,
            "ph_range_label": purpose.ph_range_label,
            "ph_min": purpose.ph_min,
            "ph_max": purpose.ph_max,
            "reason": purpose.reason,
        }
        return (
            "당신은 화장품 ODM 제형 연구원의 초기 처방 설계 보조자입니다.\n"
            "목표는 사용자의 제품 목적과 제형 타입에 맞는 액티브 투입 전 기본 Backbone 처방을 설계하는 것입니다.\n"
            "Backbone은 role별 성분을 기계적으로 1개씩 채우는 목록이 아니라, 실제 제형이 성립하기 위한 최소 구조 성분과 함량 조합입니다.\n"
            "이 단계는 액티브 성분을 투입하기 전의 기본 틀을 잡는 단계입니다. 액티브 성분, 효능 성분, 지정 함량은 이 단계에서 ingredients에 넣지 마세요.\n"
            "다만 이후 액티브 투입 시 예상되는 용해성, pH, 안정성 요구사항은 Backbone 방향성 판단에 참고할 수 있습니다.\n"
            "반드시 JSON만 반환하세요. 설명 문장, markdown, 코드블록은 출력하지 마세요.\n\n"
            "[입력 정보]\n"
            f"- FormulationPurpose:\n{json.dumps(purpose_payload, ensure_ascii=False)}\n"
            f"- ActiveContextForBackbone:\n{json.dumps(active_context, ensure_ascii=False)}\n"
            f"- StructureSkeleton.role_status:\n{json.dumps(skeleton.role_status, ensure_ascii=False)}\n"
            f"- CandidateIngredients:\n{json.dumps(skeleton.structure, ensure_ascii=False)}\n"
            f"- IngredientStats:\n{json.dumps(candidate_stats, ensure_ascii=False)}\n"
            f"- FormulaDatasetSummary:\n{json.dumps(dataset_summary, ensure_ascii=False)}\n\n"
            "[설계 원칙]\n"
            "1. 제형 타입을 먼저 확정적으로 따르세요. 수상 솔루션, 수분 젤, 가용화, O/W 유화, W/O 유화, 현탁/분산 중 어떤 backbone인지 판단하세요.\n"
            "2. StructureSkeleton의 required role은 자동 포함 대상이 아니라 우선 검토 대상입니다. optional role도 동일하게 실제 기여가 있을 때만 포함하세요.\n"
            "3. 각 role은 단순 존재 여부로 판단하지 말고 물리적 안정성, 미생물 안정성, 사용감, 외관, 제조 가능성에 실제로 기여하는지 평가한 후 포함 여부를 결정하세요.\n"
            "4. role을 포함할 때는 ingredients.reason에 반드시 필요 이유를 쓰세요. role을 제외할 때는 excluded_roles.reason에 반드시 제외 이유를 쓰세요.\n"
            "5. buffer, pH adjuster, stabilizer, rheology_modifier, solubilizer는 role 이름이 있다는 이유만으로 자동 포함하지 마세요. 해당 role이 제형 안정성, 사용감, 외관, 제조 가능성에 실제로 기여할 때만 포함하세요.\n"
            "6. 예를 들어 pH 조절 또는 목표 pH 도달/유지가 필요하면 pH adjuster를 포함할 수 있고, 산/염기 시스템 또는 목표 pH 유지 필요성이 명확하면 buffer를 포함할 수 있습니다.\n"
            "7. 점도 형성, 현탁 안정화, 유화 안정화가 필요하면 rheology_modifier 또는 stabilizer를 포함할 수 있고, 오일/향료/유용성 성분의 투명 안정화가 필요하면 solubilizer를 포함할 수 있습니다.\n"
            "8. role별 성분은 시스템 단위로 설계하세요. 보존, 점증/중화, 가용화, 유화, 수상 솔루션 구조를 함께 판단하세요.\n"
            "9. IngredientStats의 median은 참고값일 뿐 그대로 복사하지 말고 p25/p75, min/max, 제형 타입, 사용감을 함께 고려해 보수적 초기 함량을 선택하세요.\n"
            "10. 수상 제형은 정제수 또는 수상 베이스를 q.s. 연속상으로, 유화 제형은 수상/유상 비율이 제형 타입에 맞게 총합 100.00%로 맞추세요.\n"
            "11. FormulaDatasetSummary의 co-occurrence 패턴에서 함께 쓰였을 때 의미 있는 조합을 단일 성분 빈도보다 우선하세요.\n"
            "12. ingredient_name은 반드시 CandidateIngredients에 있는 성분명을 그대로 사용하세요. 후보에 없는 성분, 일반명, 임의 번역명은 출력하지 마세요.\n\n"
            "[액티브 참고 원칙]\n"
            "- ActiveContextForBackbone은 참고 정보입니다. 액티브 자체를 Backbone 성분으로 포함하지 마세요.\n"
            "- 액티브의 용해성, 안정 pH, 예상 안정성 요구사항을 고려해 pH 시스템, 용매/가용화 방향, 점증/안정화 방향을 보수적으로 설계할 수 있습니다.\n"
            "- 예: 비타민C 세럼과 펩타이드 세럼은 액티브를 넣기 전 Backbone이라도 목표 pH와 안정화 방향이 달라질 수 있습니다.\n\n"
            "[role 포함/제외 평가 기준]\n"
            "- 물리적 안정성: 분리, 침전, 석출, 점도 붕괴, 유화/분산 안정성\n"
            "- 미생물 안정성: 보존 시스템, 수상 함량, 방부 보조 필요성\n"
            "- 사용감: 끈적임, 산뜻함, 리치함, 발림성, 흡수감\n"
            "- 외관: 투명도, 탁도, 광택, 균일성\n"
            "- 제조 가능성: 중화 필요성, 용해 순서, 유화/분산 공정 성립성\n\n"
            "[금지 사항]\n"
            "- role 목록을 기계적으로 모두 채우지 마세요.\n"
            "- DB median만으로 함량을 결정하지 마세요.\n"
            "- 액티브 성분을 이 단계에서 넣지 마세요.\n"
            "- CandidateIngredients에 없는 성분명을 만들지 마세요.\n"
            "- pH 조절 목적 없이 pH adjuster를 자동 포함하지 마세요.\n"
            "- 산/염기 시스템 또는 pH 유지 필요성 없이 buffer를 자동 포함하지 마세요.\n"
            "- 오일/향료/유용성 성분의 투명 안정화 필요성 없이 solubilizer를 자동 포함하지 마세요.\n"
            "- 점증 시스템 또는 중화 필요성이 명확하지 않은데 중화제를 자동 포함하지 마세요.\n"
            "- O/W 유화라고 판단했는데 오일상 또는 유화 시스템이 없으면 안 됩니다.\n"
            "- W/O 유화라고 판단했는데 오일상이 q.s. 또는 주요 외상이 아니면 안 됩니다.\n"
            "- 수상 솔루션인데 불필요한 유화/가용화/점증 구조를 넣지 마세요.\n\n"
            "[출력 형식]\n"
            "{"
            "\"backbone_type\": \"수상 솔루션 | 수분 젤 | 가용화 | O/W 유화 | W/O 유화 | 현탁/분산\", "
            "\"design_summary\": \"Backbone 설계 의도 요약\", "
            "\"ingredients\": ["
            "{\"role\": \"water_phase\", \"ingredient_name\": \"정제수\", \"amount_pct\": 00.00, \"reason\": \"q.s. 연속상\"}"
            "], "
            "\"excluded_roles\": ["
            "{\"role\": \"buffer\", \"reason\": \"pH 유지가 필요한 액티브 또는 산/염기 시스템이 없어 제외\"}"
            "], "
            "\"system_checks\": {"
            "\"preservation_system\": \"적정 | 보완 필요 | 불필요\", "
            "\"rheology_system\": \"적정 | 보완 필요 | 불필요\", "
            "\"solubilization_system\": \"적정 | 보완 필요 | 불필요\", "
            "\"emulsification_system\": \"적정 | 보완 필요 | 불필요\", "
            "\"ph_system\": \"적정 | 보완 필요 | 불필요\""
            "}, "
            "\"total_pct\": 100.00, "
            "\"warnings\": [\"주의가 필요한 경우만 작성\"]"
            "}"
        )

    def _active_context_payload(self, context: QueryContext | None) -> dict[str, Any]:
        if context is None:
            return {"ingredients": [], "ingredient_constraints": []}
        return {
            "ingredients": [
                {
                    "name": item.name,
                    "solubility": item.solubility,
                    "stable_ph_range": item.stable_ph_range,
                }
                for item in context.ingredients
            ],
            "ingredient_constraints": [
                {
                    "ingredient": item.ingredient,
                    "amount": item.amount,
                }
                for item in context.ingredient_constraints
            ],
        }

    def _blocked_active_names(self, context: QueryContext | None) -> set[str]:
        if context is None:
            return set()
        names = [
            item.name
            for item in context.ingredients
        ] + [
            item.ingredient
            for item in context.ingredient_constraints
        ]
        return {
            normalize_context_phrase(name)
            for name in names
            if normalize_context_phrase(name)
        }

    def _candidate_stats_payload(
        self,
        skeleton: StructureSkeleton,
        formula_dataset: FormulaDataset,
    ) -> dict[str, list[dict[str, Any]]]:
        payload: dict[str, list[dict[str, Any]]] = {}
        for role, candidates in skeleton.structure.items():
            role_items: list[dict[str, Any]] = []
            for name in candidates[:12]:
                stats = formula_dataset.ingredient_stats.get(name, {})
                role_items.append({
                    "ingredient_name": name,
                    "frequency": stats.get("frequency"),
                    "median": stats.get("median"),
                    "p25": stats.get("p25"),
                    "p75": stats.get("p75"),
                    "min": stats.get("min"),
                    "max": stats.get("max"),
                })
            payload[role] = role_items
        return payload

    def _formula_dataset_summary(
        self,
        skeleton: StructureSkeleton,
        formula_dataset: FormulaDataset,
        limit: int = 8,
    ) -> dict[str, Any]:
        candidate_set = {
            name
            for candidates in skeleton.structure.values()
            for name in candidates
        }
        scored_formulas: list[tuple[int, str, dict[str, Any]]] = []
        pair_counts: Counter[tuple[str, str]] = Counter()

        for code, formula in formula_dataset.formulas.items():
            formula_names = set(formula["ingredients"])
            overlap = sorted(candidate_set & formula_names)
            if not overlap:
                continue
            scored_formulas.append((len(overlap), code, formula))
            for idx, left in enumerate(overlap):
                for right in overlap[idx + 1:]:
                    pair_counts[(left, right)] += 1

        scored_formulas.sort(key=lambda item: (-item[0], item[1]))
        similar_formulas = []
        for _, code, formula in scored_formulas[:limit]:
            structural_items = [
                {
                    "ingredient_name": name,
                    "amount": formula["ingredients"][name],
                    "role": formula["structural_roles"].get(name, "active_or_unknown"),
                }
                for name in formula["ingredients"]
                if name in candidate_set
            ]
            structural_items.sort(key=lambda item: -float(item["amount"]))
            similar_formulas.append({
                "bulk_code": code,
                "bulk_name": formula["name"],
                "matched_ingredients": structural_items[:12],
            })

        return {
            "similar_formulas": similar_formulas,
            "co_occurrence_pairs": [
                {"ingredients": list(pair), "count": count}
                for pair, count in pair_counts.most_common(20)
            ],
        }

    def _parse_llm_design(
        self,
        parsed: Any,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        formula_dataset: FormulaDataset,
        context: QueryContext | None = None,
    ) -> BackboneDesign | None:
        if not isinstance(parsed, dict):
            return None

        warnings = self._parse_text_list(parsed.get("warnings"), max_items=10)
        valid_ingredients = self._parse_llm_ingredients(
            parsed,
            skeleton,
            warnings,
            self._blocked_active_names(context),
        )
        if not valid_ingredients:
            return None

        continuous_phase_role = self._continuous_phase_role(purpose, skeleton)
        valid_ingredients = self._normalize_llm_total(
            valid_ingredients,
            continuous_phase_role,
            warnings,
        )
        if not valid_ingredients:
            return None

        role_ingredients: dict[str, list[str]] = defaultdict(list)
        for ingredient in valid_ingredients:
            role_ingredients[ingredient.role].append(ingredient.name)

        return BackboneDesign(
            backbone_type=self._parse_backbone_type(
                parsed.get("backbone_type"),
                purpose,
            ),
            design_summary=cleanup_context_phrase(parsed.get("design_summary", ""))
            or "LLM 기반 기본 Backbone 설계",
            formulation_type=purpose.formulation_type,
            product_form=purpose.product_form,
            continuous_phase_role=continuous_phase_role,
            ingredients=valid_ingredients,
            role_ingredients=dict(role_ingredients),
            excluded_roles=self._parse_excluded_roles(
                parsed.get("excluded_roles"),
                skeleton,
                warnings,
            ),
            system_checks=self._parse_system_checks(
                parsed.get("system_checks"),
                purpose,
                valid_ingredients,
            ),
            total_content=round(sum(item.amount for item in valid_ingredients), 4),
            warnings=warnings,
        )

    def _parse_llm_ingredients(
        self,
        parsed: dict[str, Any],
        skeleton: StructureSkeleton,
        warnings: list[str],
        blocked_active_names: set[str] | None = None,
    ) -> list[BackboneIngredient]:
        values = parsed.get("ingredients")
        if not isinstance(values, list):
            return []

        blocked_active_names = blocked_active_names or set()
        ingredients: list[BackboneIngredient] = []
        seen: set[tuple[str, str]] = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip()
            name = cleanup_context_phrase(item.get("ingredient_name", ""))
            amount = self._to_amount(item.get("amount_pct"))
            if not role or not name or amount is None:
                continue
            if normalize_context_phrase(name) in blocked_active_names:
                warnings.append(f"{name}: 액티브 성분은 Backbone 단계에서 제외")
                continue
            if role not in skeleton.role_status:
                warnings.append(f"{role}: StructureSkeleton에 없는 role이므로 제외")
                continue
            if name not in set(skeleton.structure.get(role, [])):
                warnings.append(f"{name}: {role} 후보 성분이 아니므로 제외")
                continue
            key = (role, name)
            if key in seen:
                continue
            seen.add(key)
            reason = cleanup_context_phrase(item.get("reason", ""))
            if not reason:
                warnings.append(f"{role}/{name}: 포함 이유 누락")
                reason = "포함 이유 누락"
            ingredients.append(
                BackboneIngredient(
                    role=role,
                    name=name,
                    amount=round(amount, 4),
                    amount_note=reason,
                    role_status=skeleton.role_status.get(role, "optional"),
                )
            )
        return ingredients

    def _normalize_llm_total(
        self,
        ingredients: list[BackboneIngredient],
        continuous_phase_role: str,
        warnings: list[str],
    ) -> list[BackboneIngredient]:
        total = round(sum(item.amount for item in ingredients), 4)
        if total <= 0:
            return []
        diff = round(100.0 - total, 4)
        if abs(diff) < 0.01:
            return self._round_to_total(ingredients)

        target_idx = self._find_qs_index(ingredients, continuous_phase_role)
        if target_idx is None:
            target_idx = 0
            warnings.append("q.s. 연속상 성분이 명확하지 않아 첫 성분으로 총합 보정")

        target = ingredients[target_idx]
        adjusted_amount = round(target.amount + diff, 4)
        if adjusted_amount < 0:
            warnings.append("LLM 함량 총합 보정 실패: q.s. 성분이 음수가 됨")
            return []

        adjusted = list(ingredients)
        adjusted[target_idx] = BackboneIngredient(
            role=target.role,
            name=target.name,
            amount=adjusted_amount,
            amount_note=target.amount_note,
            role_status=target.role_status,
        )
        warnings.append("LLM 함량 총합을 100%로 q.s. 보정")
        return self._round_to_total(adjusted)

    def _find_qs_index(
        self,
        ingredients: list[BackboneIngredient],
        continuous_phase_role: str,
    ) -> int | None:
        for idx, ingredient in enumerate(ingredients):
            if ingredient.role == continuous_phase_role and "q.s" in ingredient.amount_note.lower():
                return idx
        for idx, ingredient in enumerate(ingredients):
            if ingredient.role == continuous_phase_role:
                return idx
        return None

    def _parse_backbone_type(self, value: Any, purpose: FormulationPurpose) -> str:
        text = cleanup_context_phrase(value)
        return text if text in BACKBONE_TYPE_OPTIONS else self._default_backbone_type(purpose)

    def _parse_excluded_roles(
        self,
        value: Any,
        skeleton: StructureSkeleton,
        warnings: list[str],
    ) -> list[BackboneExcludedRole]:
        if not isinstance(value, list):
            return []
        excluded: list[BackboneExcludedRole] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip()
            reason = cleanup_context_phrase(item.get("reason", ""))
            if role not in skeleton.role_status or role in seen:
                continue
            if not reason:
                warnings.append(f"{role}: 제외 이유 누락")
                reason = "제외 이유 누락"
            excluded.append(BackboneExcludedRole(role=role, reason=reason))
            seen.add(role)
        return excluded

    def _parse_system_checks(
        self,
        value: Any,
        purpose: FormulationPurpose,
        ingredients: list[BackboneIngredient],
    ) -> dict[str, str]:
        fallback = self._default_system_checks(purpose, ingredients)
        if not isinstance(value, dict):
            return fallback
        checks: dict[str, str] = {}
        for key in SYSTEM_CHECK_KEYS:
            text = cleanup_context_phrase(value.get(key, ""))
            checks[key] = text if text in SYSTEM_CHECK_OPTIONS else fallback[key]
        return checks

    def _parse_text_list(self, value: Any, max_items: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            cleanup_context_phrase(item)
            for item in value[:max_items]
            if cleanup_context_phrase(item)
        ]

    def _to_amount(self, value: Any) -> float | None:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            match = re.search(r"-?\d+(?:\.\d+)?", str(value))
            amount = float(match.group()) if match else None
        if amount is None or amount < 0 or amount > 100:
            return None
        return amount

    def _default_backbone_type(self, purpose: FormulationPurpose) -> str:
        if purpose.formulation_type == "분산":
            return "현탁/분산"
        if purpose.formulation_type == "수상 솔루션" and purpose.product_form == "젤(겔)":
            return "수분 젤"
        return purpose.formulation_type if purpose.formulation_type in BACKBONE_TYPE_OPTIONS else "수상 솔루션"

    def _rule_excluded_roles(
        self,
        skeleton: StructureSkeleton,
        ingredients: list[BackboneIngredient],
    ) -> list[BackboneExcludedRole]:
        included_roles = {ingredient.role for ingredient in ingredients}
        return [
            BackboneExcludedRole(role=role, reason="rule fallback에서 불필요하거나 후보 확정 불가")
            for role in skeleton.role_status
            if role not in included_roles
        ]

    def _default_system_checks(
        self,
        purpose: FormulationPurpose,
        ingredients: list[BackboneIngredient],
    ) -> dict[str, str]:
        roles = {ingredient.role for ingredient in ingredients}
        return {
            "preservation_system": "적정" if "preservative" in roles else "보완 필요",
            "rheology_system": (
                "적정" if "rheology_modifier" in roles
                else "불필요" if purpose.target_viscosity == "저점도"
                else "보완 필요"
            ),
            "solubilization_system": "적정" if "solubilizer" in roles else "불필요",
            "emulsification_system": (
                "적정" if roles & {"emulsifier", "w_o_emulsifier"}
                else "불필요" if purpose.formulation_type not in {"O/W 유화", "W/O 유화"}
                else "보완 필요"
            ),
            "ph_system": "적정" if roles & {"ph_adjuster", "buffer"} else "불필요",
        }

    def _continuous_phase_role(
        self,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
    ) -> str:
        if purpose.formulation_type == "분산" and "dispersion_medium" in skeleton.role_status:
            return "dispersion_medium"
        if (
            purpose.formulation_type == "W/O 유화"
            or purpose.product_form == "오일"
        ) and "oil_phase" in skeleton.role_status:
            return "oil_phase"
        return "water_phase"

    def _include_role(
        self,
        role: str,
        status: str,
        purpose: FormulationPurpose,
    ) -> bool:
        if status == "required":
            return True
        if role not in OPTIONAL_BACKBONE_ROLES:
            return False
        if role == "chelator":
            return purpose.formulation_type in {"수상 솔루션", "가용화", "O/W 유화", "분산"}
        if role == "buffer":
            return purpose.ph_range_label in {"산성", "중성", "약알칼리성"}
        if role == "co_emulsifier":
            return purpose.formulation_type == "O/W 유화"
        if role == "oil_structurant":
            return purpose.formulation_type == "W/O 유화" or purpose.product_form == "오일"
        return False

    def _select_ingredient(
        self,
        role: str,
        candidates: list[str],
        used_names: set[str],
    ) -> str | None:
        compact_candidates = {
            normalize_context_phrase(candidate): candidate
            for candidate in candidates
        }
        for preferred in ROLE_PREFERRED_INGREDIENTS.get(role, []):
            preferred_key = normalize_context_phrase(preferred)
            for candidate_key, candidate in compact_candidates.items():
                if preferred_key in candidate_key and candidate not in used_names:
                    return candidate

        for candidate in candidates:
            if candidate not in used_names:
                return candidate
        return None

    def _target_amount(
        self,
        role: str,
        ingredient_name: str,
        purpose: FormulationPurpose,
        formula_dataset: FormulaDataset,
    ) -> float:
        low, target, high = self._role_amount_range(role, purpose)
        stats = formula_dataset.ingredient_stats.get(ingredient_name, {})
        median = stats.get("median")
        try:
            amount = float(median)
        except (TypeError, ValueError):
            amount = target
        if amount <= 0:
            amount = target
        return round(min(max(amount, low), high), 4)

    def _role_amount_range(
        self,
        role: str,
        purpose: FormulationPurpose,
    ) -> tuple[float, float, float]:
        if role == "water_phase" and purpose.formulation_type == "W/O 유화":
            return 10.0, 25.0, 40.0
        if role == "oil_phase" and purpose.product_form == "오일":
            return 0.0, 0.0, 100.0
        if role == "rheology_modifier":
            if purpose.target_viscosity == "저점도":
                return 0.05, 0.15, 0.35
            if purpose.target_viscosity == "고점도":
                return 0.3, 0.6, 1.2
        return ROLE_AMOUNT_RANGES.get(role, (0.05, 0.5, 3.0))

    def _scale_fixed_ingredients(
        self,
        ingredients: list[BackboneIngredient],
        target_total: float,
    ) -> list[BackboneIngredient]:
        current_total = sum(item.amount for item in ingredients)
        if current_total <= 0:
            return ingredients
        ratio = target_total / current_total
        return [
            BackboneIngredient(
                role=item.role,
                name=item.name,
                amount=round(item.amount * ratio, 4),
                amount_note=f"{item.amount_note}, 총량 보정",
                role_status=item.role_status,
            )
            for item in ingredients
        ]

    def _round_ingredients(
        self,
        ingredients: list[BackboneIngredient],
    ) -> list[BackboneIngredient]:
        return [
            BackboneIngredient(
                role=item.role,
                name=item.name,
                amount=round(item.amount, 4),
                amount_note=item.amount_note,
                role_status=item.role_status,
            )
            for item in ingredients
        ]

    def _round_to_total(
        self,
        ingredients: list[BackboneIngredient],
    ) -> list[BackboneIngredient]:
        if not ingredients:
            return []
        rounded = [
            BackboneIngredient(
                role=item.role,
                name=item.name,
                amount=round(item.amount, 4),
                amount_note=item.amount_note,
                role_status=item.role_status,
            )
            for item in ingredients
        ]
        diff = round(100.0 - sum(item.amount for item in rounded), 4)
        if abs(diff) < 0.0001:
            return rounded

        first = rounded[0]
        rounded[0] = BackboneIngredient(
            role=first.role,
            name=first.name,
            amount=round(first.amount + diff, 4),
            amount_note=first.amount_note,
            role_status=first.role_status,
        )
        return rounded


class ActiveSuitabilityReviewer:
    """질의 액티브가 현재 제형 목적과 backbone에 들어갈 수 있는지 검토한다."""

    ISSUE_LABELS = {
        "SOLUBILITY_UNKNOWN": "용해성 정보 불명",
        "SOLUBILITY_COMPATIBILITY_REVIEW": "용해/분산 상태 확인 필요",
        "SOLUBILIZATION_NEEDED": "가용화 안정성 확인 필요",
        "SOLUBILITY_DISPERSION_MISMATCH": "용해성-분산계 불일치",
        "INTERNAL_WATER_PHASE_RISK": "내상 수상 안정성 확인 필요",
        "DISPERSION_STABILITY_RISK": "분산 안정성 확인 필요",
        "PH_UNKNOWN": "안정 pH 정보 불명",
        "PH_PARTIAL_OVERLAP": "목표 pH 일부 구간 안정성 우려",
        "PH_MISMATCH": "pH 불일치",
        "ACID_RHEOLOGY_RISK": "산성 점증 안정성 확인 필요",
        "ALKALINE_SYSTEM_RISK": "알칼리 안정 시스템 확인 필요",
        "OXIDATION_RISK": "산화 안정성 우려",
        "TRANSPARENCY_RISK": "투명도/탁도 안정성 우려",
        "PRECIPITATION_RISK": "석출/결정화 가능성",
        "SENSORY_RISK": "사용감 영향 우려",
        "PHOTO_STABILITY_RISK": "광안정성 우려",
        "CONCENTRATION_REVIEW_NEEDED": "액티브 농도 영향 확인 필요",
    }
    FIT_OPTIONS = {"적합", "조건부 적합", "주의", "부적합", "확인 필요", "판단 보류"}
    ACTION_TYPES = {
        "add_role",
        "add_design_constraint",
        "change_ph_range",
        "change_formulation_type",
        "review_system",
    }
    ACTION_ROLES = set(ROLE_FUNCTION_MAP) | {
        role
        for roles in ROLE_SETS_BY_FORMULATION_TYPE.values()
        for role in roles
    }

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        self.bedrock = bedrock

    def review(
        self,
        context: QueryContext,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        backbone: BackboneDesign,
    ) -> list[ActiveSuitabilityReview]:
        actives = self._collect_actives(context)
        baseline_reviews = [
            self._review_active(active, purpose, skeleton, backbone)
            for active in actives
        ]
        return self._refine_reviews_with_llm(
            baseline_reviews,
            purpose,
            skeleton,
            backbone,
        )

    def _collect_actives(self, context: QueryContext) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}

        for mention in context.ingredients:
            key = normalize_context_phrase(mention.name)
            if not key:
                continue
            by_key[key] = {
                "ingredient": mention.name,
                "amount": None,
                "solubility": self._normalize_solubility(mention.solubility),
                "stable_ph_range": mention.stable_ph_range or "불명",
            }

        for constraint in context.ingredient_constraints:
            key = normalize_context_phrase(constraint.ingredient)
            if not key:
                continue
            active = by_key.setdefault(
                key,
                {
                    "ingredient": constraint.ingredient,
                    "amount": None,
                    "solubility": "불명",
                    "stable_ph_range": "불명",
                },
            )
            active["amount"] = constraint.amount

        return list(by_key.values())

    def _review_active(
        self,
        active: dict[str, Any],
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        backbone: BackboneDesign,
    ) -> ActiveSuitabilityReview:
        ingredient = str(active["ingredient"])
        amount = active.get("amount")
        solubility = self._normalize_solubility(active.get("solubility"))
        stable_ph_range = str(active.get("stable_ph_range") or "불명")

        issue_codes: list[str] = []
        actions: list[BackboneAction] = []

        solubility_fit, solubility_effects = self._review_solubility(
            solubility,
            purpose,
            skeleton,
            backbone,
            issue_codes,
            actions,
        )
        ph_fit, ph_effects = self._review_ph(
            stable_ph_range,
            purpose,
            issue_codes,
            actions,
        )
        formulation_effects = self._review_formulation_effects(
            ingredient,
            amount,
            solubility,
            purpose,
            solubility_fit,
            ph_fit,
        )
        formulation_effects.extend(solubility_effects)
        formulation_effects.extend(ph_effects)

        concentration_review_points = self._review_concentration(amount)

        formulation_effects = self._dedupe(formulation_effects)
        issue_codes = self._dedupe(issue_codes)
        issues = [self.ISSUE_LABELS.get(code, code) for code in issue_codes]
        actions = self._dedupe_actions(actions)
        required_changes = [
            self._format_action(action)
            for action in actions
        ]
        modification_required = bool(actions)

        return ActiveSuitabilityReview(
            ingredient=ingredient,
            amount=float(amount) if isinstance(amount, (int, float)) else None,
            solubility=solubility,
            stable_ph_range=stable_ph_range,
            solubility_fit=solubility_fit,
            ph_fit=ph_fit,
            formulation_effects=formulation_effects,
            concentration_review_points=concentration_review_points,
            backbone_modification_required=modification_required,
            backbone_adjustment="있음" if modification_required else "없음",
            issue_codes=issue_codes,
            issues=issues,
            backbone_actions=actions,
            required_backbone_changes=required_changes,
        )

    def _refine_reviews_with_llm(
        self,
        baseline_reviews: list[ActiveSuitabilityReview],
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        backbone: BackboneDesign,
    ) -> list[ActiveSuitabilityReview]:
        if not self.bedrock or not baseline_reviews:
            return baseline_reviews

        try:
            parsed = self.bedrock.invoke_json(
                self._build_llm_prompt(baseline_reviews, purpose, skeleton, backbone),
                max_tokens=1800,
                temperature=0,
            )
        except Exception as exc:
            print(f"액티브 적합성 LLM 검토 실패: {exc}")
            return baseline_reviews

        refined = self._parse_llm_reviews(parsed, baseline_reviews)
        return refined or baseline_reviews

    def _build_llm_prompt(
        self,
        baseline_reviews: list[ActiveSuitabilityReview],
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        backbone: BackboneDesign,
    ) -> str:
        baseline_payload = [
            self._review_to_payload(review)
            for review in baseline_reviews
        ]
        backbone_payload = [
            {
                "role": ingredient.role,
                "name": ingredient.name,
                "amount": ingredient.amount,
                "amount_note": ingredient.amount_note,
            }
            for ingredient in backbone.ingredients
        ]
        backbone_context = {
            "backbone_type": backbone.backbone_type,
            "design_summary": backbone.design_summary,
            "continuous_phase_role": backbone.continuous_phase_role,
            "ingredients": backbone_payload,
            "excluded_roles": [
                {"role": item.role, "reason": item.reason}
                for item in backbone.excluded_roles
            ],
            "system_checks": backbone.system_checks,
            "warnings": backbone.warnings,
        }
        return (
            "화장품 처방 설계에서 액티브 성분이 현재 제형 backbone에 적합한지 검토하세요.\n"
            "룰 기반 baseline은 참고용입니다. 성분 물성, 함량, 목표 제형, 목표 pH, "
            "산화/광/금속이온 민감 가능성, 투명도, 석출/결정화, 사용감 영향을 종합해 "
            "더 타당한 판단이 있으면 보정하세요.\n"
            "반드시 JSON만 반환하세요.\n\n"
            "[중요 제약]\n"
            "- 사용자 지정 함량은 변경하거나 낮추라고 제안하지 말 것\n"
            "- 후보 성분명은 제안하지 말 것. 후보 선정은 다음 단계에서 처리\n"
            "- 농도 영향은 role 추가로 바로 연결하지 말고 검토 포인트로 정리할 것\n"
            "- backbone_actions는 허용된 action_type/role/reason_code만 사용할 것\n"
            "- 알 수 없는 정보는 단정하지 말고 확인 필요로 둘 것\n\n"
            f"[허용 issue_codes]\n{', '.join(self.ISSUE_LABELS)}\n\n"
            f"[허용 action_type]\n{', '.join(sorted(self.ACTION_TYPES))}\n\n"
            f"[허용 role]\n{', '.join(sorted(self.ACTION_ROLES))}\n\n"
            f"[목표 제형]\n"
            f"- 제품 형상: {purpose.product_form}\n"
            f"- 목표 제형 타입: {purpose.formulation_type}\n"
            f"- 목표 점도: {purpose.target_viscosity}\n"
            f"- 목표 pH: {format_range(purpose.ph_min, purpose.ph_max)}\n\n"
            f"[현재 구조 골격 role]\n{json.dumps(skeleton.role_status, ensure_ascii=False)}\n\n"
            f"[현재 Backbone 설계]\n{json.dumps(backbone_context, ensure_ascii=False)}\n\n"
            f"[baseline reviews]\n{json.dumps(baseline_payload, ensure_ascii=False)}\n\n"
            "반환 형식:\n"
            "{"
            "\"reviews\": ["
            "{"
            "\"ingredient\": \"baseline의 ingredient 그대로\", "
            "\"solubility_fit\": \"적합|조건부 적합|주의|부적합|확인 필요\", "
            "\"ph_fit\": \"적합|조건부 적합|주의|부적합|확인 필요\", "
            "\"formulation_effects\": [{\"condition\": \"판단 조건\", \"impact_type\": \"투명도|석출|점도|pH|사용감|기타\", "
            "\"impact_level\": \"낮음|중간|높음|확인 필요\", \"review_point\": \"짧은 검토 문구\"}], "
            "\"concentration_review_points\": [{\"condition\": \"함량 조건\", \"review_type\": \"용해|pH|점도|사용감|일반\", "
            "\"review_point\": \"농도 관련 검토 포인트\"}], "
            "\"issue_codes\": [\"허용 issue code\"], "
            "\"backbone_actions\": ["
            "{\"action_type\": \"add_role|add_design_constraint|change_ph_range|change_formulation_type|review_system\", "
            "\"role\": \"허용 role 또는 null\", "
            "\"target_value\": \"pH범위/제형타입/조건 또는 null\", "
            "\"reason_code\": \"허용 issue code\", "
            "\"note\": \"짧은 근거\"}"
            "]"
            "}"
            "]"
            "}"
        )

    def _review_to_payload(self, review: ActiveSuitabilityReview) -> dict[str, Any]:
        return {
            "ingredient": review.ingredient,
            "amount": review.amount,
            "solubility": review.solubility,
            "stable_ph_range": review.stable_ph_range,
            "solubility_fit": review.solubility_fit,
            "ph_fit": review.ph_fit,
            "formulation_effects": [
                {
                    "condition": effect.condition,
                    "impact_type": effect.impact_type,
                    "impact_level": effect.impact_level,
                    "review_point": effect.review_point,
                }
                for effect in review.formulation_effects
            ],
            "concentration_review_points": [
                {
                    "condition": point.condition,
                    "review_type": point.review_type,
                    "review_point": point.review_point,
                }
                for point in review.concentration_review_points
            ],
            "issue_codes": review.issue_codes,
            "backbone_actions": [
                {
                    "action_type": action.action_type,
                    "role": action.role,
                    "target_value": action.target_value,
                    "reason_code": action.reason_code,
                    "note": action.note,
                }
                for action in review.backbone_actions
            ],
        }

    def _parse_llm_reviews(
        self,
        parsed: Any,
        baseline_reviews: list[ActiveSuitabilityReview],
    ) -> list[ActiveSuitabilityReview]:
        if not isinstance(parsed, dict) or not isinstance(parsed.get("reviews"), list):
            return []

        baseline_by_key = {
            normalize_context_phrase(review.ingredient): review
            for review in baseline_reviews
        }
        refined_by_key: dict[str, ActiveSuitabilityReview] = {}

        for item in parsed["reviews"]:
            if not isinstance(item, dict):
                continue
            key = normalize_context_phrase(item.get("ingredient", ""))
            baseline = baseline_by_key.get(key)
            if not baseline:
                continue

            solubility_fit = self._parse_fit(
                item.get("solubility_fit"),
                baseline.solubility_fit,
            )
            ph_fit = self._parse_fit(item.get("ph_fit"), baseline.ph_fit)
            formulation_effects = self._parse_formulation_effects(
                item.get("formulation_effects"),
                baseline.formulation_effects,
            )
            concentration_review_points = self._parse_concentration_review_points(
                item.get("concentration_review_points"),
                baseline.concentration_review_points,
            )
            issue_codes = self._parse_issue_codes(
                item.get("issue_codes"),
                baseline.issue_codes,
            )
            actions = self._parse_actions(
                item.get("backbone_actions"),
                issue_codes,
            )
            if not actions and baseline.backbone_actions and issue_codes == baseline.issue_codes:
                actions = baseline.backbone_actions

            refined_by_key[key] = self._build_review_variant(
                baseline,
                solubility_fit,
                ph_fit,
                formulation_effects,
                concentration_review_points,
                issue_codes,
                actions,
            )

        return [
            refined_by_key.get(normalize_context_phrase(review.ingredient), review)
            for review in baseline_reviews
        ]

    def _parse_fit(self, value: Any, fallback: str) -> str:
        text = cleanup_context_phrase(value)
        return text if text in self.FIT_OPTIONS else fallback

    def _parse_formulation_effects(
        self,
        value: Any,
        fallback: list[FormulationEffect],
    ) -> list[FormulationEffect]:
        if not isinstance(value, list):
            return fallback
        effects: list[FormulationEffect] = []
        for item in value:
            if isinstance(item, dict):
                review_point = cleanup_context_phrase(item.get("review_point", ""))
                if not review_point:
                    continue
                effects.append(
                    FormulationEffect(
                        condition=cleanup_context_phrase(item.get("condition", "")) or "LLM 보정",
                        impact_type=cleanup_context_phrase(item.get("impact_type", "")) or "기타",
                        impact_level=cleanup_context_phrase(item.get("impact_level", "")) or "확인 필요",
                        review_point=review_point,
                    )
                )
            else:
                review_point = cleanup_context_phrase(item)
                if review_point:
                    effects.append(
                        FormulationEffect(
                            condition="LLM 보정",
                            impact_type="기타",
                            impact_level="확인 필요",
                            review_point=review_point,
                        )
                    )
        return self._dedupe(effects)[:8] or fallback

    def _parse_concentration_review_points(
        self,
        value: Any,
        fallback: list[ConcentrationReviewPoint],
    ) -> list[ConcentrationReviewPoint]:
        if not isinstance(value, list):
            return fallback
        points: list[ConcentrationReviewPoint] = []
        for item in value:
            if isinstance(item, dict):
                review_point = cleanup_context_phrase(item.get("review_point", ""))
                if not review_point:
                    continue
                points.append(
                    ConcentrationReviewPoint(
                        condition=cleanup_context_phrase(item.get("condition", "")) or "LLM 보정",
                        review_type=cleanup_context_phrase(item.get("review_type", "")) or "일반",
                        review_point=review_point,
                    )
                )
            else:
                review_point = cleanup_context_phrase(item)
                if review_point:
                    points.append(
                        ConcentrationReviewPoint(
                            condition="LLM 보정",
                            review_type="일반",
                            review_point=review_point,
                        )
                    )
        return self._dedupe(points)[:8] or fallback

    def _parse_issue_codes(self, value: Any, fallback: list[str]) -> list[str]:
        if not isinstance(value, list):
            return fallback
        codes = [
            str(item).strip()
            for item in value
            if str(item).strip() in self.ISSUE_LABELS
        ]
        return self._dedupe(codes)

    def _parse_actions(
        self,
        value: Any,
        issue_codes: list[str],
    ) -> list[BackboneAction]:
        if not isinstance(value, list):
            return []

        actions: list[BackboneAction] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("action_type", "")).strip()
            if action_type not in self.ACTION_TYPES:
                continue
            reason_code = str(item.get("reason_code", "")).strip()
            if reason_code not in self.ISSUE_LABELS:
                continue
            if issue_codes and reason_code not in issue_codes:
                continue

            role = item.get("role")
            role = str(role).strip() if role is not None else None
            if role == "":
                role = None
            if role is not None and role not in self.ACTION_ROLES:
                continue

            target_value = item.get("target_value")
            target_value = cleanup_context_phrase(target_value) if target_value is not None else None
            if target_value == "":
                target_value = None
            if not self._valid_action_target(action_type, target_value):
                continue

            actions.append(
                BackboneAction(
                    action_type=action_type,
                    role=role,
                    target_value=target_value,
                    reason_code=reason_code,
                    note=cleanup_context_phrase(item.get("note", "")),
                )
            )
        return self._dedupe_actions(actions)

    def _valid_action_target(self, action_type: str, target_value: str | None) -> bool:
        if action_type == "change_ph_range":
            return target_value is not None and parse_ph_range(target_value) is not None
        if action_type == "change_formulation_type":
            return target_value in FORMULATION_TYPE_OPTIONS
        return True

    def _build_review_variant(
        self,
        baseline: ActiveSuitabilityReview,
        solubility_fit: str,
        ph_fit: str,
        formulation_effects: list[FormulationEffect],
        concentration_review_points: list[ConcentrationReviewPoint],
        issue_codes: list[str],
        actions: list[BackboneAction],
    ) -> ActiveSuitabilityReview:
        issues = [self.ISSUE_LABELS.get(code, code) for code in issue_codes]
        required_changes = [
            self._format_action(action)
            for action in actions
        ]
        return ActiveSuitabilityReview(
            ingredient=baseline.ingredient,
            amount=baseline.amount,
            solubility=baseline.solubility,
            stable_ph_range=baseline.stable_ph_range,
            solubility_fit=solubility_fit,
            ph_fit=ph_fit,
            formulation_effects=formulation_effects,
            concentration_review_points=concentration_review_points,
            backbone_modification_required=bool(actions),
            backbone_adjustment="있음" if actions else "없음",
            issue_codes=issue_codes,
            issues=issues,
            backbone_actions=actions,
            required_backbone_changes=required_changes,
        )

    def _review_solubility(
        self,
        solubility: str,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        backbone: BackboneDesign,
        issue_codes: list[str],
        actions: list[BackboneAction],
    ) -> tuple[str, list[FormulationEffect]]:
        effects: list[FormulationEffect] = []
        formulation_type = purpose.formulation_type
        condition = f"{formulation_type} + {solubility}"

        if solubility == "불명":
            self._add_issue(issue_codes, "SOLUBILITY_UNKNOWN")
            return "확인 필요", [
                self._effect("용해성 정보 불명", "용해성", "확인 필요", "용해성 확인 필요")
            ]

        if formulation_type == "수상 솔루션":
            if solubility == "수용성":
                return "적합", []
            if solubility == "양친성":
                effects.append(
                    self._effect(condition, "가용화", "확인 필요", "가용화 안정성 확인 필요")
                )
                self._add_issue(issue_codes, "SOLUBILIZATION_NEEDED")
                actions.append(
                    BackboneAction(
                        action_type="add_role",
                        role="solubilizer",
                        reason_code="SOLUBILIZATION_NEEDED",
                        note="양친성 액티브의 투명 안정화",
                    )
                )
                return "주의", effects
            self._add_issue(issue_codes, "SOLUBILITY_DISPERSION_MISMATCH")
            actions.extend([
                BackboneAction(
                    action_type="add_role",
                    role="solubilizer",
                    reason_code="SOLUBILITY_DISPERSION_MISMATCH",
                    note="유용성/난용성 액티브의 수상 투입 보조",
                ),
                BackboneAction(
                    action_type="change_formulation_type",
                    target_value="가용화",
                    reason_code="SOLUBILITY_DISPERSION_MISMATCH",
                    note="수상 솔루션으로 투명 안정화가 어려운 경우",
                ),
            ])
            effects.extend([
                self._effect(condition, "투명도", "확인 필요", "투명도 영향 가능"),
                self._effect(condition, "석출/분리", "높음", "침전/분리 위험 확인 필요"),
            ])
            return "부적합", effects

        if formulation_type == "가용화":
            if solubility in {"유용성", "양친성"}:
                effects.append(
                    self._effect(condition, "가용화", "확인 필요", "가용화제 요구량 확인 필요")
                )
                return "적합", effects
            if solubility == "수용성":
                effects.append(
                    self._effect(condition, "제형 타입", "확인 필요", "가용화 시스템 필요성 재검토")
                )
                return "적합", effects
            if solubility in {"난용성", "불용성"}:
                self._add_issue(issue_codes, "SOLUBILIZATION_NEEDED")
                actions.append(
                    BackboneAction(
                        action_type="review_system",
                        role="solubilizer",
                        reason_code="SOLUBILIZATION_NEEDED",
                        note="난용성/불용성 액티브의 투명도 및 석출 가능성 검토",
                    )
                )
                effects.extend([
                    self._effect(condition, "투명도", "확인 필요", "투명도 영향 가능"),
                    self._effect(condition, "석출", "확인 필요", "석출 가능성 확인 필요"),
                ])
                return "주의", effects
            return "적합", effects

        if formulation_type == "O/W 유화":
            if solubility in {"수용성", "유용성"}:
                if solubility == "수용성":
                    effects.append(
                        self._effect(condition, "수상/전해질", "확인 필요", "수상 용량/전해질 영향 확인 필요")
                    )
                return "적합", effects
            self._add_issue(issue_codes, "SOLUBILITY_COMPATIBILITY_REVIEW")
            actions.append(
                BackboneAction(
                    action_type="review_system",
                    role="dispersant",
                    reason_code="SOLUBILITY_COMPATIBILITY_REVIEW",
                    note="O/W 내 난용성/불용성 액티브의 분산 상태 검토",
                )
            )
            effects.append(
                self._effect(condition, "분산/석출", "확인 필요", "유화계 내 분산/석출 가능성 확인 필요")
            )
            return "확인 필요", effects

        if formulation_type == "W/O 유화":
            if solubility == "유용성":
                return "적합", effects
            if solubility == "수용성":
                effects.append(
                    self._effect(condition, "내상 수상", "확인 필요", "내상 수상 배합 안정성 확인 필요")
                )
                self._add_issue(issue_codes, "INTERNAL_WATER_PHASE_RISK")
                actions.append(
                    BackboneAction(
                        action_type="review_system",
                        role="water_phase",
                        reason_code="INTERNAL_WATER_PHASE_RISK",
                        note="내상 수상 안정화 검토",
                    )
                )
                return "주의", effects
            self._add_issue(issue_codes, "SOLUBILITY_COMPATIBILITY_REVIEW")
            actions.append(
                BackboneAction(
                    action_type="review_system",
                    role="w_o_emulsifier",
                    reason_code="SOLUBILITY_COMPATIBILITY_REVIEW",
                    note="W/O 내 난용성/불용성 액티브 수용 가능성 검토",
                )
            )
            effects.append(
                self._effect(condition, "배합 위치", "확인 필요", "W/O 내 배합 위치 및 안정성 확인 필요")
            )
            return "확인 필요", effects

        if formulation_type == "분산":
            if solubility in {"난용성", "불용성"}:
                effects.append(
                    self._effect(condition, "분산 안정성", "확인 필요", "분산 안정성 확인 필요")
                )
                if not self._backbone_has_role(backbone, "dispersant"):
                    self._add_issue(issue_codes, "DISPERSION_STABILITY_RISK")
                    actions.append(
                        BackboneAction(
                            action_type="add_role",
                            role="dispersant",
                            reason_code="DISPERSION_STABILITY_RISK",
                            note="분산 안정화 보조",
                        )
                    )
                return "적합", effects
            if solubility == "수용성":
                self._add_issue(issue_codes, "SOLUBILITY_COMPATIBILITY_REVIEW")
                actions.append(
                    BackboneAction(
                        action_type="change_formulation_type",
                        target_value="수상 솔루션",
                        reason_code="SOLUBILITY_COMPATIBILITY_REVIEW",
                        note="수용성 액티브는 분산보다 수상 솔루션 적합성 재검토",
                    )
                )
                effects.append(
                    self._effect(condition, "제형 타입", "확인 필요", "수용성 액티브이므로 제형 타입 재검토 필요")
                )
                return "확인 필요", effects
            if solubility in {"유용성", "양친성"}:
                self._add_issue(issue_codes, "DISPERSION_STABILITY_RISK")
                actions.append(
                    BackboneAction(
                        action_type="review_system",
                        role="dispersant",
                        reason_code="DISPERSION_STABILITY_RISK",
                        note="분산인지 유화/가용화인지 제형 타입 재검토",
                    )
                )
                effects.append(
                    self._effect(condition, "제형 타입", "확인 필요", "분산보다 유화/가용화 적합성 재검토 필요")
                )
                return "확인 필요", effects

        return "확인 필요", [
            self._effect(condition, "분산계", "확인 필요", "분산계 적합성 확인 필요")
        ]

    def _backbone_has_role(self, backbone: BackboneDesign, role: str) -> bool:
        return any(ingredient.role == role for ingredient in backbone.ingredients)

    def _review_ph(
        self,
        stable_ph_range: str,
        purpose: FormulationPurpose,
        issue_codes: list[str],
        actions: list[BackboneAction],
    ) -> tuple[str, list[FormulationEffect]]:
        active_range = parse_ph_range(stable_ph_range)
        if active_range is None:
            self._add_issue(issue_codes, "PH_UNKNOWN")
            actions.append(
                BackboneAction(
                    action_type="add_design_constraint",
                    target_value="액티브 안정 pH 확인 후 목표 pH 적합성 재검토 필요",
                    reason_code="PH_UNKNOWN",
                    note="안정 pH 정보가 없어 목표 pH 적합성 판단 보류",
                )
            )
            return "확인 필요", [
                self._effect("안정 pH 정보 불명", "pH", "확인 필요", "안정 pH 확인 필요")
            ]

        active_min, active_max = active_range
        if range_contains(active_min, active_max, purpose.ph_min, purpose.ph_max):
            return "적합", []

        if ranges_overlap(active_min, active_max, purpose.ph_min, purpose.ph_max):
            overlap_min = max(active_min, purpose.ph_min)
            overlap_max = min(active_max, purpose.ph_max)
            target_range = format_range(overlap_min, overlap_max)
            self._add_issue(issue_codes, "PH_PARTIAL_OVERLAP")
            actions.extend([
                BackboneAction(
                    action_type="change_ph_range",
                    target_value=target_range,
                    reason_code="PH_PARTIAL_OVERLAP",
                    note="목표 pH 일부 구간이 액티브 안정 pH 밖에 있음",
                ),
                BackboneAction(
                    action_type="add_design_constraint",
                    role="buffer",
                    target_value="목표 pH 실측 후 pH control strategy 검토",
                    reason_code="PH_PARTIAL_OVERLAP",
                    note="완충 시스템은 pH 유지 필요성이 확인될 때만 적용",
                ),
            ])
            effects = [
                self._effect(
                    f"액티브 안정 pH {stable_ph_range} + 목표 pH {format_range(purpose.ph_min, purpose.ph_max)}",
                    "pH",
                    "중간",
                    "목표 pH 일부 구간 안정성 확인 필요",
                )
            ]
            if overlap_max <= 4.5:
                self._add_issue(issue_codes, "ACID_RHEOLOGY_RISK")
                actions.append(
                    BackboneAction(
                        action_type="add_design_constraint",
                        role="rheology_modifier",
                        target_value="산성 조건에서 목표 점도 변화 실측 확인 필요",
                        reason_code="ACID_RHEOLOGY_RISK",
                        note="점증/안정화 시스템은 점도 변화 확인 후 적용",
                    )
                )
            elif overlap_min >= 7.5:
                self._add_issue(issue_codes, "ALKALINE_SYSTEM_RISK")
                actions.append(
                    BackboneAction(
                        action_type="review_system",
                        role="ph_adjuster",
                        reason_code="ALKALINE_SYSTEM_RISK",
                        note="알칼리 영역 안정 시스템 검토",
                    )
                )
            return "조건부 적합", effects

        self._add_issue(issue_codes, "PH_MISMATCH")
        target_range = format_range(active_min, active_max)
        actions.extend([
            BackboneAction(
                action_type="change_ph_range",
                target_value=target_range,
                reason_code="PH_MISMATCH",
                note="액티브 안정 pH와 목표 pH 불일치",
            ),
            BackboneAction(
                action_type="add_design_constraint",
                role="buffer",
                target_value="목표 pH 실측 후 pH control strategy 검토",
                reason_code="PH_MISMATCH",
                note="완충 시스템은 pH 유지 필요성이 확인될 때만 적용",
            ),
        ])
        effects = [
            self._effect(
                f"액티브 안정 pH {stable_ph_range} + 목표 pH {format_range(purpose.ph_min, purpose.ph_max)}",
                "pH",
                "높음",
                "pH 안정성 영향 있음",
            )
        ]

        if active_max <= 4.5:
            self._add_issue(issue_codes, "ACID_RHEOLOGY_RISK")
            actions.append(
                BackboneAction(
                    action_type="add_design_constraint",
                    role="rheology_modifier",
                    target_value="산성 조건에서 목표 점도 변화 실측 확인 필요",
                    reason_code="ACID_RHEOLOGY_RISK",
                    note="점증/안정화 시스템은 점도 변화 확인 후 적용",
                )
            )
        elif active_min >= 7.5:
            self._add_issue(issue_codes, "ALKALINE_SYSTEM_RISK")
            actions.append(
                BackboneAction(
                    action_type="review_system",
                    role="ph_adjuster",
                    reason_code="ALKALINE_SYSTEM_RISK",
                    note="알칼리 영역 안정 시스템 검토",
                )
            )
        return "부적합", effects

    def _review_formulation_effects(
        self,
        ingredient: str,
        amount: Any,
        solubility: str,
        purpose: FormulationPurpose,
        solubility_fit: str,
        ph_fit: str,
    ) -> list[FormulationEffect]:
        effects: list[FormulationEffect] = []
        condition = f"{purpose.formulation_type} + {solubility}"

        if purpose.formulation_type in {"수상 솔루션", "가용화"}:
            if solubility == "수용성" and solubility_fit == "적합":
                effects.extend([
                    self._effect(condition, "투명도", "낮음", "투명도 영향 없음"),
                    self._effect(condition, "침전", "낮음", "침전 위험 낮음"),
                ])
            elif solubility in {"유용성", "양친성"}:
                effects.extend([
                    self._effect(condition, "투명도", "확인 필요", "투명도 영향 가능"),
                    self._effect(condition, "가용화", "확인 필요", "가용화 안정성 확인 필요"),
                ])
            elif solubility in {"난용성", "불용성"}:
                effects.extend([
                    self._effect(condition, "투명도", "확인 필요", "투명도 영향 가능"),
                    self._effect(condition, "석출/분리", "높음", "석출/분리 위험 확인 필요"),
                ])

        if ph_fit == "부적합":
            effects.append(
                self._effect("pH 적합성 부적합", "pH", "높음", "pH 안정성 영향 있음")
            )
        elif ph_fit == "조건부 적합":
            effects.append(
                self._effect("pH 조건부 적합", "pH", "중간", "목표 pH 범위 축소 필요")
            )

        return effects

    def _review_concentration(self, amount: Any) -> list[ConcentrationReviewPoint]:
        if not isinstance(amount, (int, float)):
            return [
                self._concentration_point(
                    "지정 함량 없음",
                    "일반",
                    "지정 함량이 없어 농도 영향은 일반적인 액티브 적용 관점에서 검토",
                ),
                self._concentration_point(
                    "지정 함량 없음",
                    "안정성",
                    "성분 특성에 따른 제형/용해/pH/점도 안정성 영향 확인 필요",
                ),
            ]

        condition = f"지정 함량 {amount:g}%"
        return [
            self._concentration_point(condition, "일반", "성분별 일반 사용 범위를 확인해 지정 함량의 적정성 검토 필요"),
            self._concentration_point(condition, "제형", "지정 함량에 따른 제형 안정성 확인 필요"),
            self._concentration_point(condition, "용해", "지정 함량에 따른 용해 안정성 확인 필요"),
            self._concentration_point(condition, "pH", "지정 함량에 따른 pH 안정성 확인 필요"),
            self._concentration_point(condition, "점도", "지정 함량에 따른 점도 안정성 확인 필요"),
        ]

    def _effect(
        self,
        condition: str,
        impact_type: str,
        impact_level: str,
        review_point: str,
    ) -> FormulationEffect:
        return FormulationEffect(
            condition=condition,
            impact_type=impact_type,
            impact_level=impact_level,
            review_point=review_point,
        )

    def _concentration_point(
        self,
        condition: str,
        review_type: str,
        review_point: str,
    ) -> ConcentrationReviewPoint:
        return ConcentrationReviewPoint(
            condition=condition,
            review_type=review_type,
            review_point=review_point,
        )

    def _normalize_solubility(self, value: Any) -> str:
        text = cleanup_context_phrase(value)
        return text if text in SOLUBILITY_OPTIONS else "불명"

    def _add_issue(self, issue_codes: list[str], code: str) -> None:
        if code not in issue_codes:
            issue_codes.append(code)

    def _format_action(self, action: BackboneAction) -> str:
        if action.action_type == "change_ph_range" and action.target_value:
            return f"목표 pH를 {action.target_value}로 변경"
        if action.action_type == "change_formulation_type" and action.target_value:
            return f"분산계를 {action.target_value}로 변경 검토"
        if action.action_type == "add_role" and action.role:
            return f"{action.role} 역할군 추가 검토"
        if action.action_type == "add_design_constraint":
            return action.target_value or action.note or "설계 제약조건 추가"
        if action.action_type == "review_system" and action.role:
            return f"{action.role} 시스템 검토"
        return action.note or action.action_type

    def _dedupe_actions(self, actions: list[BackboneAction]) -> list[BackboneAction]:
        result: list[BackboneAction] = []
        seen: set[BackboneAction] = set()
        for action in actions:
            if action in seen:
                continue
            seen.add(action)
            result.append(action)
        return result

    def _dedupe(self, values: list[Any]) -> list[Any]:
        result: list[Any] = []
        seen: set[Any] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result


class BackboneRevisionPlanner:
    """액티브 검토 결과를 설계 제약조건으로 변환한다."""

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        self.bedrock = bedrock

    def plan(
        self,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        reviews: list[ActiveSuitabilityReview],
        formula_dataset: FormulaDataset | None = None,
    ) -> BackboneRevision | None:
        actionable_reviews = [
            review for review in reviews
            if review.backbone_modification_required
        ]
        if not actionable_reviews:
            return None

        actions = self._dedupe_actions(
            action
            for review in actionable_reviews
            for action in review.backbone_actions
        )
        added_required_roles = self._infer_added_required_roles(
            actions,
            skeleton,
            purpose,
        )
        design_constraints = self._build_design_constraints(
            purpose,
            actions,
        )
        rationale = self._dedupe(
            issue
            for review in actionable_reviews
            for issue in review.issues
        )

        baseline = BackboneRevision(
            design_constraints=design_constraints,
            added_required_roles=added_required_roles,
            rationale=rationale,
            source_actions=actions,
        )
        return self._refine_revision_with_llm(
            baseline,
            purpose,
            skeleton,
            actionable_reviews,
        )

    def _infer_added_required_roles(
        self,
        actions: list[BackboneAction],
        skeleton: StructureSkeleton,
        purpose: FormulationPurpose,
    ) -> list[str]:
        return self._dedupe(
            action.role
            for action in actions
            if self._should_add_required_role(action, skeleton, purpose)
        )

    def _should_add_required_role(
        self,
        action: BackboneAction,
        skeleton: StructureSkeleton,
        purpose: FormulationPurpose,
    ) -> bool:
        if (
            action.action_type != "add_role"
            or not action.role
            or skeleton.role_status.get(action.role) == "required"
        ):
            return False

        if self._is_review_only_action(action):
            return False

        role = action.role
        formulation_type = purpose.formulation_type
        if role in {"rheology_modifier", "stabilizer"}:
            if purpose.target_viscosity == "저점도":
                return False
            return formulation_type in {"O/W 유화", "분산"} or purpose.product_form == "젤(겔)"
        if role == "buffer":
            return False
        if role == "solubilizer":
            return action.reason_code in {"SOLUBILITY_DISPERSION_MISMATCH", "SOLUBILIZATION_NEEDED"} and formulation_type in {"수상 솔루션", "가용화"}
        if role == "dispersant":
            return formulation_type == "분산" and action.reason_code == "DISPERSION_STABILITY_RISK"
        if role in {"chelator", "antioxidant_support"}:
            return False
        return False

    def _is_review_only_action(self, action: BackboneAction) -> bool:
        if action.reason_code in {
            "PH_UNKNOWN",
            "PH_PARTIAL_OVERLAP",
            "PH_MISMATCH",
            "ACID_RHEOLOGY_RISK",
            "ALKALINE_SYSTEM_RISK",
            "OXIDATION_RISK",
            "TRANSPARENCY_RISK",
            "PRECIPITATION_RISK",
            "SENSORY_RISK",
            "PHOTO_STABILITY_RISK",
            "SOLUBILITY_COMPATIBILITY_REVIEW",
            "CONCENTRATION_REVIEW_NEEDED",
        }:
            return True
        note = normalize_context_phrase(action.note)
        return any(
            token in note
            for token in ("검토", "확인", "조건", "가능성", "재검토", "실측")
        )

    def _build_design_constraints(
        self,
        purpose: FormulationPurpose,
        actions: list[BackboneAction],
    ) -> list[str]:
        constraints: list[str] = []
        for action in actions:
            constraint = self._format_design_constraint(purpose, action)
            if constraint:
                constraints.append(constraint)
        return self._dedupe(constraints)

    def _format_design_constraint(
        self,
        purpose: FormulationPurpose,
        action: BackboneAction,
    ) -> str | None:
        if action.action_type == "change_ph_range" and action.target_value:
            return f"목표 pH를 {action.target_value}로 조정 필요"
        if action.action_type == "change_formulation_type" and action.target_value:
            return f"{action.target_value} 기반 Backbone 조건 검토"
        if action.action_type == "add_design_constraint":
            return action.target_value or action.note or "추가 설계 제약조건 필요"
        if action.action_type == "review_system":
            return action.note or f"{action.role or 'backbone'} 시스템 검토 필요"
        if action.action_type != "add_role":
            return action.note

        if action.role == "chelator":
            return "금속이온 영향 검토 필요"
        if action.role == "antioxidant_support":
            return "산화 안정성 확보 전략 검토 필요"
        if action.role == "solubilizer":
            return "난용성/유용성 액티브의 투명 안정화 필요"
        if action.role == "buffer":
            return "목표 pH 실측 후 pH control strategy 검토 필요"
        if action.role == "rheology_modifier":
            return "액티브 투입 후 목표 점도 변화 실측 확인 필요"
        if action.role == "stabilizer":
            return "액티브 투입 후 제형 안정성 실측 확인 필요"
        if action.role == "dispersant":
            return "분산 안정성 확보 필요"
        return action.note or f"{action.role} 역할 조건 필요"

    def _refine_revision_with_llm(
        self,
        baseline: BackboneRevision,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        reviews: list[ActiveSuitabilityReview],
    ) -> BackboneRevision:
        if not self.bedrock:
            return baseline

        try:
            parsed = self.bedrock.invoke_json(
                self._build_revision_llm_prompt(
                    baseline,
                    purpose,
                    skeleton,
                    reviews,
                ),
                max_tokens=1200,
                temperature=0,
            )
        except Exception as exc:
            print(f"Backbone 수정 LLM 검토 실패: {exc}")
            return baseline

        if not isinstance(parsed, dict):
            return baseline

        design_constraints = self._parse_text_list(
            parsed.get("design_constraints"),
            baseline.design_constraints,
            max_items=12,
        )
        added_required_roles = self._parse_roles(
            parsed.get("added_required_roles"),
            baseline.added_required_roles,
            {
                role
                for role in baseline.added_required_roles
            } | {
                action.role
                for action in baseline.source_actions
                if action.action_type == "add_role" and action.role
            },
            baseline.source_actions,
            skeleton,
            purpose,
        )
        rationale = self._parse_text_list(
            parsed.get("rationale"),
            baseline.rationale,
            max_items=12,
        )

        return BackboneRevision(
            design_constraints=design_constraints,
            added_required_roles=added_required_roles,
            rationale=rationale,
            source_actions=baseline.source_actions,
        )

    def _build_revision_llm_prompt(
        self,
        baseline: BackboneRevision,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        reviews: list[ActiveSuitabilityReview],
    ) -> str:
        review_payload = [
            {
                "ingredient": review.ingredient,
                "amount": review.amount,
                "solubility": review.solubility,
                "stable_ph_range": review.stable_ph_range,
                "solubility_fit": review.solubility_fit,
                "ph_fit": review.ph_fit,
                "issue_codes": review.issue_codes,
                "issues": review.issues,
                "actions": [
                    {
                        "action_type": action.action_type,
                        "role": action.role,
                        "target_value": action.target_value,
                        "reason_code": action.reason_code,
                        "note": action.note,
                    }
                    for action in review.backbone_actions
                ],
            }
            for review in reviews
        ]
        baseline_payload = {
            "design_constraints": baseline.design_constraints,
            "added_required_roles": baseline.added_required_roles,
            "rationale": baseline.rationale,
        }
        return (
            "다음 액티브 적합성 검토 결과를 바탕으로 [4. 액티브 적합성 및 Backbone 수정 전략]의 "
            "Backbone 수정 전략에 들어갈 설계 제약조건을 정리하세요.\n"
            "이 단계는 후보 성분을 고르는 단계가 아닙니다. 무엇을 써야 하는지가 아니라 "
            "무슨 조건을 만족해야 하는지만 정의하세요.\n"
            "반드시 JSON만 반환하세요.\n\n"
            "[중요 제약]\n"
            "- 추천 후보 성분명, 원료명, 구체 배합비를 쓰지 말 것\n"
            "- 사용자 지정 함량을 변경하라고 제안하지 말 것\n"
            "- backbone_actions의 add_role은 반드시 추가하라는 명령이 아니라 검토 후보로 볼 것\n"
            "- 문제 발견을 role 추가로 바로 연결하지 말고, pH/점도/산화/외관/사용감/고농도 이슈는 우선 실측/검토/설계 조건으로 정리할 것\n"
            "- added_required_roles는 action에 포함된 role 중에서도 현재 제형 목적상 최종 Backbone 구조에 반드시 추가되어야 하는 role만 작성\n"
            "- 검토 필요, 실측 후 판단, 조건부 적용, 낮은 우선순위 role은 added_required_roles에 넣지 말고 design_constraints에만 반영\n"
            "- pH 우려는 buffer 추가가 아니라 pH control strategy 검토로, 점도 우려는 rheology role 추가가 아니라 목표 점도 실측 확인으로 우선 정리\n"
            "- 산화 안정성 우려는 antioxidant_support 추가 확정이 아니라 산화 안정성 확보 전략 검토로 우선 정리\n"
            "- 목표 제형 타입, 제품 형상, 목표 점도, 목표 pH와 충돌하는 role은 added_required_roles에 넣지 말 것\n"
            "- design_constraints는 짧고 검증 가능한 조건 문장으로 작성\n\n"
            f"[목표 제형]\n"
            f"- 제품 형상: {purpose.product_form}\n"
            f"- 목표 제형 타입: {purpose.formulation_type}\n"
            f"- 목표 점도: {purpose.target_viscosity}\n"
            f"- 목표 pH: {format_range(purpose.ph_min, purpose.ph_max)}\n\n"
            f"[현재 구조 골격 role]\n{json.dumps(skeleton.role_status, ensure_ascii=False)}\n\n"
            f"[액티브 검토]\n{json.dumps(review_payload, ensure_ascii=False)}\n\n"
            f"[baseline Backbone 수정]\n{json.dumps(baseline_payload, ensure_ascii=False)}\n\n"
            "반환 형식:\n"
            "{"
            "\"design_constraints\": [\"설계 제약조건\"], "
            "\"added_required_roles\": [\"role\"], "
            "\"rationale\": [\"수정 근거\"]"
            "}"
        )

    def _parse_text_list(
        self,
        value: Any,
        fallback: list[str],
        max_items: int,
    ) -> list[str]:
        if not isinstance(value, list):
            return fallback
        parsed = [
            cleanup_context_phrase(item)
            for item in value
            if cleanup_context_phrase(item)
            and len(cleanup_context_phrase(item)) <= 80
        ]
        return self._dedupe(parsed)[:max_items] or fallback

    def _parse_roles(
        self,
        value: Any,
        fallback: list[str],
        allowed_roles: set[str],
        source_actions: list[BackboneAction],
        skeleton: StructureSkeleton,
        purpose: FormulationPurpose,
    ) -> list[str]:
        if not isinstance(value, list):
            return fallback
        addable_roles = {
            action.role
            for action in source_actions
            if self._should_add_required_role(action, skeleton, purpose)
        } | set(fallback)
        roles = [
            str(item).strip()
            for item in value
            if str(item).strip() in allowed_roles
            and str(item).strip() in addable_roles
        ]
        parsed = self._dedupe(roles)
        return parsed or fallback

    def _dedupe_actions(self, actions: Any) -> list[BackboneAction]:
        result: list[BackboneAction] = []
        seen: set[BackboneAction] = set()
        for action in actions:
            if not isinstance(action, BackboneAction) or action in seen:
                continue
            seen.add(action)
            result.append(action)
        return result

    def _dedupe(self, values: Any) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result


class FinalBackboneFinalizer:
    """기본 Backbone과 수정 전략을 반영해 최종 Backbone 처방표를 확정한다."""

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        self.bedrock = bedrock

    def finalize(
        self,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        backbone: BackboneDesign,
        active_reviews: list[ActiveSuitabilityReview],
        revision: BackboneRevision | None,
        formula_dataset: FormulaDataset,
    ) -> FinalBackboneResult:
        fallback = self._build_fallback_result(
            purpose,
            backbone,
            active_reviews,
            revision,
            formula_dataset,
        )
        if not self.bedrock:
            return fallback

        try:
            parsed = self.bedrock.invoke_json(
                self._build_llm_prompt(
                    purpose,
                    skeleton,
                    backbone,
                    active_reviews,
                    revision,
                    formula_dataset,
                ),
                max_tokens=2200,
                temperature=0,
            )
        except Exception as exc:
            print(f"최종 Backbone 확정 LLM 호출 실패: {exc}")
            return fallback

        result = self._parse_llm_result(
            parsed,
            purpose,
            backbone,
            active_reviews,
            revision,
            formula_dataset,
        )
        return result or fallback

    def _build_llm_prompt(
        self,
        purpose: FormulationPurpose,
        skeleton: StructureSkeleton,
        backbone: BackboneDesign,
        active_reviews: list[ActiveSuitabilityReview],
        revision: BackboneRevision | None,
        formula_dataset: FormulaDataset,
    ) -> str:
        purpose_payload = {
            "product_form": purpose.product_form,
            "formulation_type": purpose.formulation_type,
            "target_viscosity": purpose.target_viscosity,
            "ph_range": format_range(purpose.ph_min, purpose.ph_max),
            "reason": purpose.reason,
        }
        backbone_payload = {
            "backbone_type": backbone.backbone_type,
            "design_summary": backbone.design_summary,
            "continuous_phase_role": backbone.continuous_phase_role,
            "ingredients": [
                {
                    "role": item.role,
                    "name": item.name,
                    "amount": item.amount,
                    "reason": item.amount_note,
                }
                for item in backbone.ingredients
            ],
            "excluded_roles": [
                {"role": item.role, "reason": item.reason}
                for item in backbone.excluded_roles
            ],
            "system_checks": backbone.system_checks,
            "warnings": backbone.warnings,
        }
        active_payload = [
            {
                "ingredient": review.ingredient,
                "amount": review.amount,
                "solubility": review.solubility,
                "stable_ph_range": review.stable_ph_range,
                "solubility_fit": review.solubility_fit,
                "ph_fit": review.ph_fit,
                "issue_codes": review.issue_codes,
                "issues": review.issues,
                "concentration_review_points": [
                    {
                        "condition": point.condition,
                        "review_type": point.review_type,
                        "review_point": point.review_point,
                    }
                    for point in review.concentration_review_points
                ],
            }
            for review in active_reviews
        ]
        revision_payload = {
            "design_constraints": revision.design_constraints if revision else [],
            "added_required_roles": revision.added_required_roles if revision else [],
            "rationale": revision.rationale if revision else [],
        }
        candidate_payload = self._candidate_payload(
            skeleton,
            backbone,
            revision,
            formula_dataset,
        )
        return (
            "당신은 화장품 ODM 제형 연구원의 최종 Backbone 확정 보조자입니다.\n"
            "목표는 현재 기본 Backbone과 액티브 적합성 검토 결과를 바탕으로, "
            "연구원이 실험 가능한 최종 Backbone 처방안을 확정하는 것입니다.\n"
            "필요한 정보만 근거로 판단하고, 문제 발견을 성분/role 추가로 자동 연결하지 마세요.\n"
            "반드시 JSON만 반환하세요. markdown, 설명 문장, 코드블록은 출력하지 마세요.\n\n"
            "[입력 정보]\n"
            f"- FormulationPurpose:\n{json.dumps(purpose_payload, ensure_ascii=False)}\n"
            f"- CurrentBackbone:\n{json.dumps(backbone_payload, ensure_ascii=False)}\n"
            f"- ActiveReviewSummary:\n{json.dumps(active_payload, ensure_ascii=False)}\n"
            f"- BackboneRevisionStrategy:\n{json.dumps(revision_payload, ensure_ascii=False)}\n"
            f"- CandidateIngredients:\n{json.dumps(candidate_payload, ensure_ascii=False)}\n\n"
            "[표준 규칙]\n"
            "- 후보 성분 목록에 없는 성분은 절대 사용하지 마세요.\n"
            "- 액티브 성분은 최종 Backbone 성분표에 포함하지 마세요. 액티브 지정 함량도 변경하지 마세요.\n"
            "- 단, 지정 액티브 함량은 q.s. 연속상 계산에서 예약량으로 반드시 차감된다는 전제로 설계하세요.\n"
            "- 총합은 반드시 100.00%가 되도록 작성하세요.\n"
            "- q.s. 연속상 성분은 정제수 또는 CurrentBackbone의 continuous_phase_role에 해당하는 성분으로 두세요.\n"
            "- 최종 ingredients는 함량 내림차순으로 작성하세요.\n"
            "- pH, 점도, 산화, 외관, 사용감, 고농도 이슈는 우선 실측/검토 조건으로 해석하세요.\n"
            "- 최종 Backbone에 반드시 필요한 경우에만 성분 또는 role을 반영하세요.\n"
            "- 불필요한 buffer, 점증제, 가용화제, 항산화 보조제를 자동 추가하지 마세요.\n"
            "- 저점도, 투명, 끈적임 낮음 같은 목표 사용감과 충돌하는 구조를 넣지 마세요.\n\n"
            "[출력 형식]\n"
            "{"
            "\"backbone_formula\": {"
            "\"name\": \"Final Backbone\", "
            "\"concept\": \"액티브 투입 전 최종 Backbone\", "
            "\"backbone_type\": \"수상 솔루션\", "
            "\"target_aspects\": [\"투명\", \"저점도\"], "
            "\"ingredients\": ["
            "{\"name\": \"정제수\", \"content\": 00.00, \"role\": \"q.s. 연속상\", \"reason\": \"총합 100% 보정\"}"
            "], "
            "\"total_pct\": 100.00"
            "}, "
            "\"design_rationale\": \"최종 Backbone 확정 근거\", "
            "\"applied_revision\": [\"반영한 수정 전략\"], "
            "\"deferred_checks\": [\"실측/검토로 남길 항목\"], "
            "\"warnings\": [\"주의가 필요한 경우만 작성\"]"
            "}"
        )

    def _candidate_payload(
        self,
        skeleton: StructureSkeleton,
        backbone: BackboneDesign,
        revision: BackboneRevision | None,
        formula_dataset: FormulaDataset,
    ) -> dict[str, list[dict[str, Any]]]:
        roles = set(backbone.role_ingredients) | set(skeleton.role_status)
        if revision:
            roles.update(revision.added_required_roles)
            roles.update(
                action.role
                for action in revision.source_actions
                if action.role
            )

        payload: dict[str, list[dict[str, Any]]] = {}
        for role in sorted(role for role in roles if role):
            names = list(skeleton.structure.get(role, []))
            if not names:
                names = self._select_role_candidates(role, formula_dataset)
            payload[role] = [
                {
                    "ingredient_name": name,
                    "median": formula_dataset.ingredient_stats.get(name, {}).get("median"),
                    "p25": formula_dataset.ingredient_stats.get(name, {}).get("p25"),
                    "p75": formula_dataset.ingredient_stats.get(name, {}).get("p75"),
                    "min": formula_dataset.ingredient_stats.get(name, {}).get("min"),
                    "max": formula_dataset.ingredient_stats.get(name, {}).get("max"),
                }
                for name in names[:10]
            ]
        return payload

    def _select_role_candidates(
        self,
        role: str,
        formula_dataset: FormulaDataset,
        limit: int = 10,
    ) -> list[str]:
        functions = set(ROLE_FUNCTION_MAP.get(role, []))
        rows = formula_dataset.raw[["ingredient_name", "ingredient_function"]].drop_duplicates()
        scored: list[tuple[float, str]] = []
        for _, row in rows.iterrows():
            name = str(row["ingredient_name"]).strip()
            function = str(row["ingredient_function"]).strip()
            if function not in functions:
                continue
            stats = formula_dataset.ingredient_stats.get(name, {})
            score = float(stats.get("frequency", 0)) * 100 + float(stats.get("count", 0))
            scored.append((score, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [name for _, name in scored[:limit]]

    def _build_fallback_result(
        self,
        purpose: FormulationPurpose,
        backbone: BackboneDesign,
        active_reviews: list[ActiveSuitabilityReview],
        revision: BackboneRevision | None,
        formula_dataset: FormulaDataset,
    ) -> FinalBackboneResult:
        formula = FinalBackboneFormula(
            name="Final Backbone",
            concept="액티브 투입 전 최종 Backbone",
            backbone_type=backbone.backbone_type,
            target_aspects=self._target_aspects(purpose, backbone),
            ingredients=[
                FinalBackboneIngredient(
                    name=item.name,
                    content=item.amount,
                    role=item.role,
                    reason=item.amount_note,
                )
                for item in backbone.ingredients
            ],
            reserved_actives=[],
            backbone_pct=round(sum(item.amount for item in backbone.ingredients), 4),
            total_pct=round(sum(item.amount for item in backbone.ingredients), 4),
        )
        return self._validate_and_finalize(
            formula=formula,
            purpose=purpose,
            backbone=backbone,
            active_reviews=active_reviews,
            formula_dataset=formula_dataset,
            design_rationale="LLM 미사용: 기본 Backbone과 수정 전략을 기준으로 확정",
            applied_revision=revision.design_constraints if revision else [],
            deferred_checks=self._deferred_checks(revision),
            warnings=[],
        )

    def _parse_llm_result(
        self,
        parsed: Any,
        purpose: FormulationPurpose,
        backbone: BackboneDesign,
        active_reviews: list[ActiveSuitabilityReview],
        revision: BackboneRevision | None,
        formula_dataset: FormulaDataset,
    ) -> FinalBackboneResult | None:
        if not isinstance(parsed, dict) or not isinstance(parsed.get("backbone_formula"), dict):
            return None
        formula_payload = parsed["backbone_formula"]
        ingredients = self._parse_ingredients(formula_payload.get("ingredients"))
        if not ingredients:
            return None
        formula = FinalBackboneFormula(
            name=cleanup_context_phrase(formula_payload.get("name", "")) or "Final Backbone",
            concept=cleanup_context_phrase(formula_payload.get("concept", "")) or "액티브 투입 전 최종 Backbone",
            backbone_type=self._parse_backbone_type(formula_payload.get("backbone_type"), backbone),
            target_aspects=self._parse_text_list(formula_payload.get("target_aspects"), max_items=8),
            ingredients=ingredients,
            reserved_actives=[],
            backbone_pct=0.0,
            total_pct=self._to_float(formula_payload.get("total_pct")) or 0.0,
        )
        return self._validate_and_finalize(
            formula=formula,
            purpose=purpose,
            backbone=backbone,
            active_reviews=active_reviews,
            formula_dataset=formula_dataset,
            design_rationale=cleanup_context_phrase(parsed.get("design_rationale", "")),
            applied_revision=self._parse_text_list(parsed.get("applied_revision"), max_items=12),
            deferred_checks=self._parse_text_list(parsed.get("deferred_checks"), max_items=12),
            warnings=self._parse_text_list(parsed.get("warnings"), max_items=12),
        )

    def _parse_ingredients(self, values: Any) -> list[FinalBackboneIngredient]:
        if not isinstance(values, list):
            return []
        ingredients: list[FinalBackboneIngredient] = []
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            name = cleanup_context_phrase(item.get("name", ""))
            content = self._to_float(item.get("content"))
            if not name or content is None:
                continue
            key = normalize_context_phrase(name)
            if key in seen:
                continue
            seen.add(key)
            ingredients.append(
                FinalBackboneIngredient(
                    name=name,
                    content=round(content, 4),
                    role=cleanup_context_phrase(item.get("role", "")) or "역할 미기재",
                    reason=cleanup_context_phrase(item.get("reason", "")) or "근거 미기재",
                )
            )
        return ingredients

    def _validate_and_finalize(
        self,
        formula: FinalBackboneFormula,
        purpose: FormulationPurpose,
        backbone: BackboneDesign,
        active_reviews: list[ActiveSuitabilityReview],
        formula_dataset: FormulaDataset,
        design_rationale: str,
        applied_revision: list[str],
        deferred_checks: list[str],
        warnings: list[str],
    ) -> FinalBackboneResult:
        known_set = set(formula_dataset.ingredient_names)
        reserved_actives = self._active_reservations(active_reviews)
        reserved_total = round(sum(item.content for item in reserved_actives), 4)
        blocked_actives = {
            normalize_context_phrase(review.ingredient)
            for review in active_reviews
        }
        checked: list[FinalBackboneIngredient] = []
        for item in formula.ingredients:
            if item.name not in known_set:
                warnings.append(f"{item.name}: DB 후보 성분이 아니므로 제외")
                continue
            if normalize_context_phrase(item.name) in blocked_actives:
                warnings.append(f"{item.name}: 액티브 성분은 최종 Backbone에서 제외")
                continue
            if item.content < 0 or item.content > 100:
                warnings.append(f"{item.name}: 함량 범위 오류로 제외")
                continue
            checked.append(item)

        qs_idx = self._find_qs_index(checked, backbone)
        if qs_idx is None:
            return self._redesign_result(
                formula,
                design_rationale,
                applied_revision,
                deferred_checks,
                warnings + ["q.s. 연속상 성분을 찾지 못했습니다."],
            )

        fixed_total = round(
            sum(item.content for idx, item in enumerate(checked) if idx != qs_idx),
            4,
        )
        qs_content = round(100.0 - fixed_total - reserved_total, 4)
        if qs_content < 0:
            return self._redesign_result(
                formula,
                design_rationale,
                applied_revision,
                deferred_checks,
                warnings + [
                    "고정 Backbone 성분과 지정 액티브 함량의 합이 100%를 초과해 q.s. 연속상 함량이 음수가 됩니다.",
                    "[3. Backbone 설계]에서 고정 성분 함량을 낮추거나 optional role을 줄여 다시 설계해야 합니다.",
                ],
            )

        qs_item = checked[qs_idx]
        qs_reason = qs_item.reason or "총합 100% q.s. 보정"
        if reserved_total > 0 and "액티브" not in qs_reason:
            qs_reason = f"{qs_reason}, 액티브 예약량 포함 q.s. 보정"
        checked[qs_idx] = FinalBackboneIngredient(
            name=qs_item.name,
            content=qs_content,
            role=qs_item.role,
            reason=qs_reason,
        )
        checked = sorted(checked, key=lambda item: (-item.content, item.name))
        backbone_pct = round(sum(item.content for item in checked), 4)
        total_pct = round(backbone_pct + reserved_total, 4)
        total_valid = abs(total_pct - 100.0) <= 0.01
        sorted_valid = all(
            checked[idx].content >= checked[idx + 1].content
            for idx in range(len(checked) - 1)
        )
        final_formula = FinalBackboneFormula(
            name=formula.name,
            concept=formula.concept,
            backbone_type=formula.backbone_type,
            target_aspects=formula.target_aspects or self._target_aspects(purpose, backbone),
            ingredients=checked,
            reserved_actives=reserved_actives,
            backbone_pct=backbone_pct,
            total_pct=100.0 if total_valid else total_pct,
        )
        return FinalBackboneResult(
            backbone_formula=final_formula if total_valid else None,
            validation=FinalBackboneValidation(
                total_pct_valid=total_valid,
                continuous_phase_qs_valid=qs_content >= 0,
                sorted_by_content_desc=sorted_valid,
                needs_backbone_redesign=not total_valid,
            ),
            redesign_feedback=[] if total_valid else ["총합 100% 검증에 실패했습니다."],
            design_rationale=design_rationale or "최종 Backbone 확정 근거 없음",
            applied_revision=applied_revision,
            deferred_checks=deferred_checks,
            warnings=warnings,
        )

    def _redesign_result(
        self,
        formula: FinalBackboneFormula,
        design_rationale: str,
        applied_revision: list[str],
        deferred_checks: list[str],
        feedback: list[str],
    ) -> FinalBackboneResult:
        return FinalBackboneResult(
            backbone_formula=None,
            validation=FinalBackboneValidation(
                total_pct_valid=False,
                continuous_phase_qs_valid=False,
                sorted_by_content_desc=False,
                needs_backbone_redesign=True,
            ),
            redesign_feedback=feedback,
            design_rationale=design_rationale or "최종 Backbone 확정 실패",
            applied_revision=applied_revision,
            deferred_checks=deferred_checks,
            warnings=[],
        )

    def _find_qs_index(
        self,
        ingredients: list[FinalBackboneIngredient],
        backbone: BackboneDesign,
    ) -> int | None:
        continuous_names = {
            normalize_context_phrase(item.name)
            for item in backbone.ingredients
            if item.role == backbone.continuous_phase_role
        }
        for idx, item in enumerate(ingredients):
            text = normalize_context_phrase(f"{item.role} {item.reason}")
            if "qs" in text or "연속상" in text:
                return idx
        for idx, item in enumerate(ingredients):
            if normalize_context_phrase(item.name) in continuous_names:
                return idx
        for idx, item in enumerate(ingredients):
            if item.name == "정제수":
                return idx
        return None

    def _active_reservations(
        self,
        active_reviews: list[ActiveSuitabilityReview],
    ) -> list[FinalActiveReservation]:
        reservations: list[FinalActiveReservation] = []
        seen: set[str] = set()
        for review in active_reviews:
            if review.amount is None or review.amount <= 0:
                continue
            key = normalize_context_phrase(review.ingredient)
            if not key or key in seen:
                continue
            seen.add(key)
            reservations.append(
                FinalActiveReservation(
                    ingredient=review.ingredient,
                    content=round(float(review.amount), 4),
                    reason="사용자 지정 액티브 함량: 최종 Backbone q.s. 계산에서 예약",
                )
            )
        return sorted(reservations, key=lambda item: (-item.content, item.ingredient))

    def _target_aspects(
        self,
        purpose: FormulationPurpose,
        backbone: BackboneDesign,
    ) -> list[str]:
        aspects = [
            purpose.product_form,
            purpose.target_viscosity,
            purpose.ph_range_label,
        ]
        if backbone.backbone_type not in aspects:
            aspects.append(backbone.backbone_type)
        return [item for item in aspects if item]

    def _deferred_checks(self, revision: BackboneRevision | None) -> list[str]:
        if revision is None:
            return []
        return [
            item
            for item in revision.design_constraints
            if any(token in item for token in ("검토", "확인", "실측", "전략"))
        ]

    def _parse_text_list(self, value: Any, max_items: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            cleanup_context_phrase(item)
            for item in value
            if cleanup_context_phrase(item)
        ][:max_items]

    def _parse_backbone_type(self, value: Any, backbone: BackboneDesign) -> str:
        text = cleanup_context_phrase(value)
        return text if text in BACKBONE_TYPE_OPTIONS else backbone.backbone_type

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            match = re.search(r"-?\d+(?:\.\d+)?", str(value))
            return float(match.group()) if match else None


def load_data_sources(config: CLIConfig) -> dict[str, Any]:
    formula_dataset = FormulaDataLoader().load(config.data_csv)
    product_dataset = ProductKeywordLoader().load(config.product_csv, formula_dataset)
    external_dataset = ExternalProductLoader().load(config.external_csv)

    embedding_index = EmbeddingIndex(
        model=None,
        embeddings=None,
        ingredient_names=formula_dataset.ingredient_names,
    )
    if config.load_embeddings:
        embedding_index = EmbeddingModelLoader().load(
            formula_dataset.ingredient_names,
            allow_download=config.allow_embedding_download,
        )

    bedrock = BedrockClient(
        aws_profile=config.aws_profile,
        aws_region=config.aws_region,
        model_id=config.model_id,
    )

    return {
        "formula": formula_dataset,
        "product": product_dataset,
        "external": external_dataset,
        "embedding": embedding_index,
        "bedrock": bedrock,
    }


def print_load_summary(data_sources: dict[str, Any]) -> None:
    formula: FormulaDataset = data_sources["formula"]
    product: ProductKeywordDataset = data_sources["product"]
    external: ExternalProductDataset = data_sources["external"]
    embedding: EmbeddingIndex = data_sources["embedding"]

    print("데이터 로드 완료")
    print(f"- 처방: {formula.total_formulas}건")
    print(f"- 성분 통계: {len(formula.ingredient_stats)}종")
    print(f"- 마케팅 키워드: {len(product.keyword_db)}종")
    print(f"- 마케팅-처방 연결: {product.linked_bulk_count}건")
    print(f"- 타사 제품: {len(external.products)}건")
    print(f"- 임베딩 인덱스: {'로드됨' if embedding.available else '미로드'}")
    if formula.unmapped_functions:
        shown = ", ".join(formula.unmapped_functions[:20])
        suffix = " ..." if len(formula.unmapped_functions) > 20 else ""
        print(f"- 미매핑 ingredient_function: {shown}{suffix}")


def print_query_context_summary(context: QueryContext) -> None:
    print("질의 맥락 추출 결과")
    print("- 성분:")
    if context.ingredients:
        for item in context.ingredients:
            print(
                f"  · {item.name} | 용해성: {item.solubility} "
                f"| 안정 pH: {item.stable_ph_range}"
            )
    else:
        print("  · 없음")
    print(f"- 제형/사용감: {context.formulation}")
    print(f"- 마케팅 포인트: {context.marketing_points}")
    print(f"- 타겟 제품: {context.target_product}")
    constraints = [
        f"{item.ingredient} {item.amount:g}%"
        for item in context.ingredient_constraints
    ]
    print(f"- 지정 함량: {constraints}")


def print_dispersion_judgement_summary(dispersion: DispersionJudgement) -> None:
    print("[분산계 판단]")
    print(f"  · 목표 제형 타입(분산계): {dispersion.dispersion_system}")
    print(f"  · 판단 근거: {dispersion.reason}")
    for warning in dispersion.warnings or []:
        print(f"  ! {warning}")


def print_product_form_judgement_summary(product_form: ProductFormJudgement) -> None:
    print("[제형 판단]")
    print(f"  · 확정 제품 형상: {product_form.product_form}")
    print(f"  · 판단 근거: {product_form.reason}")
    for warning in product_form.warnings or []:
        print(f"  ! {warning}")


def print_formulation_detail_summary(detail: FormulationDetailJudgement) -> None:
    print("[세부 제형 판단]")
    print(f"  · 목표 점도: {detail.target_viscosity}")
    print(f"  · 목표 pH 범위: {detail.ph_range_label} (pH {detail.ph_min:g}~{detail.ph_max:g})")
    print(f"  · 판단 근거: {detail.reason}")
    for warning in detail.warnings or []:
        print(f"  ! {warning}")


def format_ph_range(purpose: FormulationPurpose) -> str:
    return (
        f"{purpose.ph_range_label} "
        f"(pH {purpose.ph_min:g}~{purpose.ph_max:g})"
    )


def print_formulation_purpose_summary(purpose: FormulationPurpose) -> None:
    print("[1. 제형 타입 및 목적]")
    print(f"  · 제품 형상: {purpose.product_form}")
    print(f"  · 목표 제형 타입(분산계): {purpose.formulation_type}")
    print(f"  · 목표 점도: {purpose.target_viscosity}")
    print(f"  · 목표 pH 범위: {format_ph_range(purpose)}")


def print_structure_skeleton_summary(skeleton: StructureSkeleton) -> None:
    print("[2. 구조 골격 설계]")
    required = [
        role for role, status in skeleton.role_status.items()
        if status == "required"
    ]
    optional = [
        role for role, status in skeleton.role_status.items()
        if status == "optional"
    ]
    print(f"  · 필수 역할군: {', '.join(required) or '없음'}")
    print(f"  · 선택 역할군: {', '.join(optional) or '없음'}")
    for role, ingredients in skeleton.structure.items():
        print(f"  · {role}: {', '.join(ingredients) or 'DB 후보 없음'}")
    for warning in skeleton.warnings:
        print(f"  ! {warning}")


def print_backbone_design_summary(backbone: BackboneDesign) -> None:
    print("[3. Backbone 설계]")
    print(f"  · Backbone 타입: {backbone.backbone_type}")
    print(f"  · 설계 의도: {backbone.design_summary}")
    print(f"  · 연속상 역할군: {backbone.continuous_phase_role}")
    print(f"  · 총 함량: {backbone.total_content:g}%")
    for ingredient in backbone.ingredients:
        print(
            f"  · {ingredient.role}: {ingredient.name} "
            f"{ingredient.amount:g}% ({ingredient.amount_note})"
        )
    if backbone.excluded_roles:
        print("  · 제외 role:")
        for excluded in backbone.excluded_roles:
            print(f"    - {excluded.role}: {excluded.reason}")
    if backbone.system_checks:
        print("  · 시스템 점검:")
        for key, value in backbone.system_checks.items():
            print(f"    - {key}: {value}")
    for warning in backbone.warnings:
        print(f"  ! {warning}")


def format_active_label(review: ActiveSuitabilityReview) -> str:
    if review.amount is None:
        return review.ingredient
    return f"{review.ingredient} {review.amount:g}%"


def print_active_suitability_review_summary(
    reviews: list[ActiveSuitabilityReview],
) -> None:
    print("[4. 액티브 적합성 및 Backbone 수정 전략]")
    print("  [액티브 적합성 검토]")
    if not reviews:
        print("  · 검토할 액티브 성분 없음")
        return

    for review in reviews:
        print(f"  · 액티브: {format_active_label(review)}")
        print(f"    - 용해성: {review.solubility}")
        print(f"    - 안정 pH: {review.stable_ph_range}")
        print(f"    - 용해성 적합성: {review.solubility_fit}")
        print(f"    - pH 적합성: {review.ph_fit}")
        print("    - 제형 영향:")
        for effect in review.formulation_effects:
            print(
                f"      · [{effect.impact_type}/{effect.impact_level}] "
                f"{effect.review_point} (조건: {effect.condition})"
            )
        print("    - 액티브 농도 영향:")
        amount_text = f"{review.amount:g}%" if review.amount is not None else "없음"
        print(f"      · 지정 함량: {amount_text}")
        print("      · 검토 포인트:")
        for point in review.concentration_review_points:
            print(
                f"        - [{point.review_type}] {point.review_point} "
                f"(조건: {point.condition})"
            )
        print(f"    - Backbone 수정 필요: {review.backbone_adjustment}")
        if review.backbone_modification_required:
            print("    - issues:")
            for issue in review.issues:
                print(f"      · {issue}")
            print("    - required_backbone_changes:")
            for change in review.required_backbone_changes:
                print(f"      · {change}")


def print_backbone_revision_summary(revision: BackboneRevision | None) -> None:
    if revision is None:
        print("  [Backbone 수정 전략]")
        print("    - 수정 전략 없음")
        return

    print("  [Backbone 수정 전략]")
    print("  · 설계 제약조건:")
    if revision.design_constraints:
        for constraint in revision.design_constraints:
            print(f"    - {constraint}")
    else:
        print("    - 없음")

    print("  · 추가 역할군:")
    if revision.added_required_roles:
        for role in revision.added_required_roles:
            print(f"    - {role}")
    else:
        print("    - 없음")

    if revision.rationale:
        print("  · 수정 근거:")
        for note in revision.rationale:
            print(f"    - {note}")


def print_final_backbone_summary(result: FinalBackboneResult) -> None:
    print("[5. 최종 Backbone 확정]")
    if result.backbone_formula is None:
        print("  · 최종 Backbone 확정 실패")
        for feedback in result.redesign_feedback:
            print(f"  ! {feedback}")
        return

    formula = result.backbone_formula
    print(f"  · 이름: {formula.name}")
    print(f"  · 컨셉: {formula.concept}")
    print(f"  · Backbone 타입: {formula.backbone_type}")
    print(f"  · 타겟 aspect: {', '.join(formula.target_aspects) or '없음'}")
    print("  · 성분:")
    for ingredient in formula.ingredients:
        print(
            f"    - {ingredient.name}: {ingredient.content:g}% "
            f"({ingredient.role}; {ingredient.reason})"
        )
    if formula.reserved_actives:
        print("  · 액티브 예약 함량:")
        for active in formula.reserved_actives:
            print(
                f"    - {active.ingredient}: {active.content:g}% "
                f"({active.reason})"
            )
        print(f"  · Backbone 성분 소계: {formula.backbone_pct:g}%")
    print(f"  · 액티브 예약 포함 총합: {formula.total_pct:g}%")
    if result.applied_revision:
        print("  · 반영한 수정 전략:")
        for item in result.applied_revision:
            print(f"    - {item}")
    if result.deferred_checks:
        print("  · 후속 확인:")
        for item in result.deferred_checks:
            print(f"    - {item}")
    for warning in result.warnings:
        print(f"  ! {warning}")


def main() -> None:
    config = parse_args()
    data_sources = load_data_sources(config)
    print_load_summary(data_sources)

    query_context = QueryContextExtractor(data_sources["bedrock"]).extract(config.query)
    print_query_context_summary(query_context)

    dispersion = DispersionSystemExtractor(data_sources["bedrock"]).extract(
        config.query,
        query_context,
    )
    print_dispersion_judgement_summary(dispersion)

    product_form = ProductFormExtractor(data_sources["bedrock"]).extract(
        config.query,
        query_context,
        dispersion,
    )
    print_product_form_judgement_summary(product_form)

    formulation_detail = FormulationDetailExtractor(data_sources["bedrock"]).extract(
        config.query,
        query_context,
        dispersion,
        product_form,
    )
    print_formulation_detail_summary(formulation_detail)

    formulation_purpose = build_formulation_purpose(
        dispersion,
        product_form,
        formulation_detail,
    )
    print_formulation_purpose_summary(formulation_purpose)

    structure_skeleton = StructureSkeletonDesigner().design(
        formulation_purpose,
        data_sources["formula"],
    )
    print_structure_skeleton_summary(structure_skeleton)

    backbone_design = BackboneDesigner(data_sources["bedrock"]).design(
        formulation_purpose,
        structure_skeleton,
        data_sources["formula"],
        query_context,
    )
    print_backbone_design_summary(backbone_design)

    active_reviews = ActiveSuitabilityReviewer(data_sources["bedrock"]).review(
        query_context,
        formulation_purpose,
        structure_skeleton,
        backbone_design,
    )
    print_active_suitability_review_summary(active_reviews)

    backbone_revision = BackboneRevisionPlanner(data_sources["bedrock"]).plan(
        formulation_purpose,
        structure_skeleton,
        active_reviews,
        data_sources["formula"],
    )
    print_backbone_revision_summary(backbone_revision)

    final_backbone = FinalBackboneFinalizer(data_sources["bedrock"]).finalize(
        formulation_purpose,
        structure_skeleton,
        backbone_design,
        active_reviews,
        backbone_revision,
        data_sources["formula"],
    )
    print_final_backbone_summary(final_backbone)


if __name__ == "__main__":
    main()
