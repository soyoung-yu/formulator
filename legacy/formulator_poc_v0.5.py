"""
AI 기반 에센스 처방 자동 생성 시스템 — PoC v5
================================================
v5 변경 내용:
  - 임베딩 모델 호출 방식 
    - 기존: 허깅페이스 모델 호출 시도 -> 실패시 편집거리 사용
    - 변경: 로컬에 저장된 모델 호출 시도 -> 실패시 허깅페이스 모델 호출 시도 -> 실패시 편집거리 매핑 사용

Usage:

    # 인증 방법: IAM Role
    python formulator_poc_v0.4.py \\
        --data data.csv \\
        --product product.csv \\
        --query "나이아신아마이드가 포함된 안티에이징 에센스"

    # 모델 변경 예시 (Haiku로 저비용 테스트)
    python formulator_poc_v0.4.py ... --model us.anthropic.claude-haiku-4-5-20251001-v1:0

Requirements:
    pip install pandas numpy openpyxl boto3
    (편집거리): pip install python-Levenshtein
    (임베딩):   pip install sentence-transformers faiss-cpu
    (컬러출력): pip install rich

AWS Bedrock 사전 준비 (최초 1회):
    AWS Console → Amazon Bedrock → Model Access → Anthropic Claude 모델 접근 권한 요청
"""

import argparse, ast, json, os, re, warnings
from collections import defaultdict, Counter
from pathlib import Path

import numpy as np
import boto3
import pandas as pd

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
    def _s(m): return re.sub(r"\[/?[^\]]*\]", "", str(m))
    def print(self, m="", **k): print(self._s(m))
    def rule(self, m="", **k): print("\n"+"="*60+"\n  "+self._s(m)+"\n"+"="*60)

console = _rich_console if HAS_RICH else _FallbackConsole()


# ─────────────────────────────────────────────────────────────────────────────
# 구조적 역할 분류 (ingredient_function 신뢰 가능한 카테고리만)
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

# base 성분 하드코딩 보조 목록 (function 태그 없는 케이스 대비)
_KNOWN_BASE = {
    "정제수", "글리세린", "부틸렌글라이콜", "프로판다이올", "1,2-헥산다이올",
    "다이프로필렌글라이콜", "에틸헥실글리세린", "펜틸렌글라이콜", "메틸프로판다이올",
    "잔탄검", "트로메타민", "카보머", "소듐파이테이트", "다이소듐이디티에이",
    "향료", "토코페롤", "암모늄아크릴로일다이메틸타우레이트/브이피코폴리머",
    "아크릴레이트/C10-30알킬아크릴레이트크로스폴리머",
}


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


# ─────────────────────────────────────────────────────────────────────────────
# 2. 마케팅 키워드 데이터 로드 및 키워드-성분 매핑 구축
# ─────────────────────────────────────────────────────────────────────────────

def load_product_data(product_csv: str, formula_dict: dict) -> dict:
    """
    product.csv 로드 후 키워드-성분 패턴 매핑 구축.

    Returns:
        keyword_db: {
            keyword: {
                "ingredients": Counter({성분명: 등장횟수}),
                "aspects":     set([사용감 키워드, ...]),
                "formula_codes": [bulk_code, ...]
            }
        }
    """
    prod = pd.read_csv(product_csv, engine="python")
    overlap = set(prod["bulk_code"]) & set(formula_dict.keys())
    console.print(f"[green]✓ 마케팅 데이터: {len(prod)}건 로드 / 처방 연결: {len(overlap)}건[/green]")

    keyword_db    = defaultdict(lambda: {"ingredients": Counter(), "aspects": set(), "formula_codes": []})
    all_keywords  = []
    all_aspects   = []

    for _, row in prod[prod["bulk_code"].isin(overlap)].drop_duplicates("bulk_code").iterrows():
        code = row["bulk_code"]
        kws, asps = [], []
        try: kws  = ast.literal_eval(row["marketing_keywords_list"])
        except: pass
        try: asps = ast.literal_eval(str(row["aspect_list"]))
        except: pass

        # active 성분만 추출 (base 제외)
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
    """쿼리 문자열에서 keyword_db에 있는 키워드를 찾아 반환"""
    matched = []
    for kw, data in keyword_db.items():
        if kw in query:
            matched.append((kw, data))
    return matched


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
            from pathlib import Path
            from sentence_transformers import SentenceTransformer

            console.print("[dim]임베딩 모델 로딩...[/dim]")

            model_name = "snunlp/KR-SBERT-V40K-klueNLI-augSTS"
            model_dir = Path("./models/KR-SBERT-V40K-klueNLI-augSTS")

            # 1) 이미 로컬에 저장된 모델이 있으면 로컬 모델만 사용
            if model_dir.exists():
                console.print("[dim]로컬 임베딩 모델 사용...[/dim]")
                self._model = SentenceTransformer(
                    str(model_dir),
                    local_files_only=True
                )

            # 2) 로컬에 없으면 외부망에서 다운로드 후 저장
            else:
                console.print("[dim]로컬 모델 없음 → Hugging Face에서 다운로드...[/dim]")
                self._model = SentenceTransformer(model_name)
                self._model.save(str(model_dir))
                console.print(f"[green]✓ 모델 로컬 저장 완료: {model_dir}[/green]")

            # 3) 성분명 임베딩 생성
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

    def resolve_from_query(self, query: str) -> list[tuple[str, str, float]]:
        """쿼리에서 DB 성분명 직접 탐색"""
        results = []
        for known in self.known:
            if known in query:
                results.append((known, known, 1.0))
        if not results:
            for tok in re.split(r"[\s,·/]+", query):
                if len(tok) < 2: continue
                m, score, _ = self.map(tok)
                if m and score >= 0.75:
                    results.append((tok, m, score))
        return results


# ─────────────────────────────────────────────────────────────────────────────
# 4. 통계 분석
# ─────────────────────────────────────────────────────────────────────────────

def build_stats(formula_dict: dict) -> dict:
    idata = defaultdict(list)
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
# 5. 컨텍스트 구성 (통계 + 키워드 패턴)
# ─────────────────────────────────────────────────────────────────────────────

def build_context(
    query: str,
    stats: dict,
    mapper: IngredientMapper,
    keyword_db: dict,
    formula_dict: dict,
    top_base: int = 15,
    top_active: int = 15,
) -> dict:
    ist = stats["ingredient_stats"]

    # 1) 쿼리에서 성분명 직접 매핑
    query_matched_ings = mapper.resolve_from_query(query)
    query_ing_names    = {m for _, m, _ in query_matched_ings}

    # 2) 쿼리에서 마케팅 키워드 매핑
    matched_keywords = match_query_keywords(query, keyword_db)
    keyword_ing_names = set()
    keyword_aspects   = set()
    for kw, kdata in matched_keywords:
        top_kw_ings = [ing for ing, _ in kdata["ingredients"].most_common(8)]
        keyword_ing_names.update(top_kw_ings)
        keyword_aspects.update(kdata["aspects"])

    # 3) base / active 성분 분리
    base_ings   = []
    active_ings = []
    priority_names = query_ing_names | keyword_ing_names

    for name, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"]):
        role  = s["structural_role"]
        entry = {"name": name, **s}
        if role in BASE_ROLES or name in _KNOWN_BASE:
            if len(base_ings) < top_base:
                base_ings.append(entry)
        else:
            if name in priority_names:
                active_ings.insert(0, entry)   # 우선 배치
            elif len(active_ings) < top_active:
                active_ings.append(entry)

    # active 부족하면 빈도 순으로 보충
    if len(active_ings) < top_active:
        exist = {a["name"] for a in active_ings}
        extras = [
            {"name": n, **s}
            for n, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"])
            if s["structural_role"] not in BASE_ROLES
            and n not in _KNOWN_BASE and n not in exist
        ]
        active_ings += extras[:top_active - len(active_ings)]

    # 4) 키워드 연결 처방을 few-shot 후보로 우선 선정
    keyword_formula_codes = set()
    for _, kdata in matched_keywords:
        keyword_formula_codes.update(kdata["formula_codes"])

    similar_formulas = _pick_similar(
        query, formula_dict, mapper,
        priority_codes=keyword_formula_codes,
        n=3
    )

    # 5) 키워드별 대표 성분 요약 (프롬프트용)
    keyword_summary = []
    for kw, kdata in matched_keywords:
        top_ings  = [ing for ing, _ in kdata["ingredients"].most_common(5)]
        aspects   = list(kdata["aspects"])[:4]
        n_formulas = len(kdata["formula_codes"])
        keyword_summary.append({
            "keyword":    kw,
            "top_ings":   top_ings,
            "aspects":    aspects,
            "n_formulas": n_formulas,
        })

    return {
        "query_matched_ings": query_matched_ings,
        "matched_keywords":   keyword_summary,
        "keyword_aspects":    list(keyword_aspects),
        "base_ings":          base_ings,
        "active_ings":        active_ings[:top_active],
        "similar_formulas":   similar_formulas,
    }


def _pick_similar(query, formula_dict, mapper, priority_codes=None, n=3):
    priority_codes = priority_codes or set()
    query_names    = {m for _, m, _ in mapper.resolve_from_query(query)}

    scores = []
    for code, fd in formula_dict.items():
        overlap    = sum(1 for nm in fd["ingredients"] if nm in query_names or nm in query)
        is_priority = 1 if code in priority_codes else 0
        scores.append((is_priority, overlap, len(fd["ingredients"]), code))

    scores.sort(reverse=True)
    return [
        {"bulk_code": c, "name": formula_dict[c]["name"],
         "ingredients": formula_dict[c]["ingredients"]}
        for _, _, _, c in scores[:n]
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 6. 프롬프트 빌더
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

[출력 형식 — JSON만 응답, 다른 텍스트 절대 금지]
{
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


def build_user_prompt(query: str, ctx: dict, total_formulas: int) -> str:
    lines = []

    # 요구사항
    lines.append(f"[사용자 요구사항]\n{query}")

    # 성분명 직접 매핑 결과
    if ctx["query_matched_ings"]:
        lines.append("\n[DB 내 성분명 매핑 결과]")
        for orig, matched, score in ctx["query_matched_ings"]:
            lines.append(f"  · '{orig}' → '{matched}' (유사도 {score:.2f})")

    # 마케팅 키워드 패턴 (핵심 섹션)
    if ctx["matched_keywords"]:
        lines.append(f"\n[마케팅 키워드 분석 — 실제 처방 데이터 기반]")
        for ks in ctx["matched_keywords"]:
            lines.append(
                f"  · 키워드 '{ks['keyword']}' ({ks['n_formulas']}건 처방에서 사용):"
            )
            lines.append(f"    - 주요 활성 성분: {', '.join(ks['top_ings'])}")
            if ks["aspects"]:
                lines.append(f"    - 소비자 체감 aspect: {', '.join(ks['aspects'])}")

    # aspect 종합
    if ctx["keyword_aspects"]:
        lines.append(f"\n[소비자 체감 타겟 aspect]\n  {', '.join(ctx['keyword_aspects'])}")

    # base 성분 통계
    lines.append(f"\n[기본 구성 성분 통계 — 총 {total_formulas}건 기준]")
    for i in ctx["base_ings"][:12]:
        lines.append(
            f"  · {i['name']}: 빈도 {i['frequency']*100:.0f}% ({i['count']}건), "
            f"함량 {i['min']}~{i['max']}% (중앙값 {i['median']}%)"
        )

    # active 성분 통계
    lines.append(f"\n[활성·기능성 성분 통계]")
    for i in ctx["active_ings"]:
        lines.append(
            f"  · {i['name']}: 빈도 {i['frequency']*100:.0f}% ({i['count']}건), "
            f"함량 {i['min']}~{i['max']}% (중앙값 {i['median']}%)"
        )

    # few-shot 유사처방
    lines.append(f"\n[유사 처방 참고 예시 (few-shot)]")
    for idx, f in enumerate(ctx["similar_formulas"][:2]):
        lines.append(f"  [참고처방 {idx+1}] {f['name']}")
        for nm, pct in sorted(f["ingredients"].items(), key=lambda x: -x[1])[:14]:
            lines.append(f"    {nm}: {pct:.3f}%")

    # 설계 지침
    lines.append("""
[3안 설계 지침]
- Formula A: 핵심 활성 성분 고함량 + 성분 수 최소화 (심플 & 집중 효능)
- Formula B: 핵심 효능 + 보습·진정 복합 기능 밸런스 (올라운드 실용 처방)
- Formula C: 트렌드 성분 또는 복합 활성 성분 추가, 마케팅 소구점 강화 (프리미엄·차별화)

각 안의 함량 합계가 정확히 100.00%가 되도록 정제수 함량으로 조정하세요.
마케팅 키워드에서 도출된 성분 패턴과 aspect를 처방 설계에 반영하세요.""")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# 7. 비용 계산
#    서울 리전(ap-northeast-2) 기준 AWS Bedrock 요금 (USD per 1M tokens)
#    출처: AWS Bedrock 콘솔 → 아시아 태평양(서울)
# ─────────────────────────────────────────────────────────────────────────────

# 모델별 요금표 (input / output, USD per 1M tokens)
BEDROCK_PRICING = {
    # Claude 3.5 / 3 계열 (서울 리전)
    "anthropic.claude-3-5-sonnet-20240620-v1:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-5-haiku-20241022-v1:0":  {"input": 1.00, "output":  5.00},
    "anthropic.claude-3-sonnet-20240229-v1:0":   {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-haiku-20240307-v1:0":    {"input": 0.25, "output":  1.25},
    "anthropic.claude-3-opus-20240229-v1:0":     {"input": 15.00,"output": 75.00},
    # Claude 4 계열 (서울 리전, 이미지 기준)
    "anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-haiku-4-5-20251001-v1:0":  {"input": 1.00, "output":  5.00},
    "anthropic.claude-opus-4-5-20251101-v1:0":   {"input": 5.00, "output": 25.00},
    "anthropic.claude-sonnet-4-6-v1":            {"input": 3.00, "output": 15.00},
    "anthropic.claude-opus-4-6-v1":              {"input": 5.00, "output": 25.00},
    # cross-region inference profile (us.* prefix)
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.00, "output": 15.00},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0":  {"input": 1.00, "output":  5.00},
}

def calc_cost(model_id: str, input_tokens: int, output_tokens: int) -> dict:
    """
    토큰 수로 API 호출 비용을 계산.
    Returns: {
        "input_tokens": int, "output_tokens": int,
        "input_cost_usd": float, "output_cost_usd": float,
        "total_cost_usd": float, "total_cost_krw": float,
        "price_note": str
    }
    """
    KRW_PER_USD = 1380  # 대략적인 환율 (참고용)

    pricing = BEDROCK_PRICING.get(model_id)
    if pricing is None:
        # 모델이 테이블에 없으면 Sonnet 요금을 기본값으로
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
    """비용 요약을 콘솔에 출력"""
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
# 7. AWS Bedrock 클라이언트 & Claude API 호출
#    - SageMaker 환경 기준: boto3.Session() IAM Role 자동 인증
#    - invoke_model 방식 사용
# ─────────────────────────────────────────────────────────────────────────────

def call_claude_api(
    query,
    ctx,
    total_formulas,
    aws_profile  = None,
    aws_region   = "ap-northeast-2",
    model_id     = "anthropic.claude-3-5-sonnet-20240620-v1:0",
    max_retries  = 2,
):
    """
    AWS Bedrock invoke_model 방식으로 Claude API 호출

    인증: boto3.Session() → SageMaker IAM Role 자동 적용
          aws_profile 지정 시 해당 프로파일 사용 (로컬 개발 환경용)
    """
    import time

    # ── 클라이언트 생성 ─────────────────────
    try:
        if aws_profile:
            session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
        else:
            session = boto3.Session()   # SageMaker: IAM Role 자동 감지
        bedrock = session.client(service_name="bedrock-runtime", region_name=aws_region)
    except Exception as e:
        console.print(f"[red]Bedrock 클라이언트 생성 실패: {e}[/red]")
        return None

    user_prompt = build_user_prompt(query, ctx, total_formulas)
    console.print(f"[dim]Claude API 호출 중... (모델: {model_id}, 리전: {aws_region})[/dim]")

    for attempt in range(1, max_retries + 2):
        # ── API 호출 ────────────────────────
        try:
            response = bedrock.invoke_model(
                modelId = model_id,
                body    = json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens":        4096,
                    "temperature":       0,
                    "system":            SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                })
            )
        except Exception as e:
            err_msg = str(e)
            if "ThrottlingException" in err_msg or "Too Many Requests" in err_msg:
                wait = 10 * attempt
                console.print(f"[yellow]Rate limit — {wait}초 후 재시도 ({attempt}/{max_retries})[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"[red]API 호출 오류: {err_msg}[/red]")
            return None

        # ── 응답 파싱 ───────────────────────
        response_body    = json.loads(response["body"].read())
        assistant_message = response_body["content"][0]["text"]

        # 토큰 사용량 + 비용 계산
        usage   = response_body.get("usage", {})
        in_tok  = usage.get("input_tokens",  0)
        out_tok = usage.get("output_tokens", 0)
        cost    = calc_cost(model_id, in_tok, out_tok)
        console.print(
            f"[dim]✓ 토큰 사용: 입력 {in_tok:,} / 출력 {out_tok:,} / "
            f"합계 {in_tok + out_tok:,}[/dim]"
        )

        # ── JSON 파싱 ──────────────────────────────────────────────────────
        raw = assistant_message.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$",          "", raw)

        try:
            return json.loads(raw), cost
        except json.JSONDecodeError:
            if attempt <= max_retries:
                console.print(f"[yellow]JSON 파싱 실패 — 재시도 ({attempt}/{max_retries})[/yellow]")
                user_prompt = user_prompt + "\n\n반드시 JSON 형식만 응답하세요. 다른 텍스트 없이."
                continue
            console.print(f"[red]JSON 파싱 최종 실패\n{raw[:600]}[/red]")
            return None, cost

    return None, {}

# ─────────────────────────────────────────────────────────────────────────────
# 8. 후처리 및 검증
# ─────────────────────────────────────────────────────────────────────────────

def validate_and_fix(formula_data: dict, stats: dict) -> dict:
    """
    1) 함량 합계 100% 보정 (정제수 조정)
    2) 통계 범위 이상치 경고 태그
    """
    ist = stats["ingredient_stats"]
    for formula in formula_data.get("formulas", []):
        ings  = formula.get("ingredients", [])
        total = sum(i.get("content", 0) for i in ings)
        if abs(total - 100.0) > 0.05:
            for ing in ings:
                if "정제수" in ing.get("name", "") or "water" in ing.get("name", "").lower():
                    ing["content"] = round(ing["content"] + (100.0 - total), 6)
                    ing["role"]    = (ing.get("role", "") + " [합계보정]").strip()
                    break
        for ing in ings:
            name, content = ing.get("name", ""), ing.get("content", 0)
            if name in ist:
                s = ist[name]
                if content > s["max"] * 1.5:
                    ing["role"] = (ing.get("role", "") + f" ⚠ 통계 최대치({s['max']}%) 초과").strip()
                elif 0 < content < s["min"] * 0.5:
                    ing["role"] = (ing.get("role", "") + f" ⚠ 통계 최소치({s['min']}%) 미만").strip()
    return formula_data


# ─────────────────────────────────────────────────────────────────────────────
# 9. 출력
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
            print(f"{'성분명':<34} {'함량(%)':>10}  역할\n"+"-"*75)
            total = 0.0
            for i in ings:
                c = i.get("content", 0); total += c
                print(f"{i.get('name', ''):<34} {c:>10.4f}  {i.get('role', '')}")
            print("-"*75 + f"\n{'합계':<46} {total:>10.4f}")

    if dr := formula_data.get("design_rationale"):
        print(f"\n{'─'*65}\n[설계 근거]\n{dr}")


# ─────────────────────────────────────────────────────────────────────────────
# 10. 파일 저장
# ─────────────────────────────────────────────────────────────────────────────

def save_results(formula_data: dict, stats: dict, keyword_db: dict, query: str, output_dir: str, cost: dict = {}):
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # CSV (처방별)
    for formula in formula_data.get("formulas", []):
        fname = formula["name"].replace(" ", "_").lower()
        rows  = [
            {"성분명": i.get("name", ""), "함량(%)": i.get("content", 0), "역할": i.get("role", "")}
            for i in sorted(formula["ingredients"], key=lambda x: -x.get("content", 0))
        ]
        pd.DataFrame(rows).to_csv(f"{output_dir}/{fname}.csv", index=False, encoding="utf-8-sig")
        console.print(f"[green]✓ {output_dir}/{fname}.csv[/green]")

    # Excel
    xl_path = f"{output_dir}/formula_output.xlsx"
    with pd.ExcelWriter(xl_path, engine="openpyxl") as writer:
        # 처방 시트
        for formula in formula_data.get("formulas", []):
            rows = [
                {"성분명": i.get("name", ""), "함량(%)": i.get("content", 0), "역할": i.get("role", "")}
                for i in sorted(formula["ingredients"], key=lambda x: -x.get("content", 0))
            ]
            df_f = pd.DataFrame(rows)
            df_f.loc[len(df_f)] = {"성분명": "합계", "함량(%)": df_f["함량(%)"].sum(), "역할": ""}
            df_f.to_excel(writer, sheet_name=formula["name"], index=False)

        # 성분 통계 시트
        ist = stats["ingredient_stats"]
        pd.DataFrame([
            {"성분명": n, "구조적역할": s["structural_role"],
             "사용빈도": f"{s['frequency']*100:.1f}%", "사용처방수": s["count"],
             "최소(%)": s["min"], "25%(%)": s["p25"], "중앙값(%)": s["median"],
             "75%(%)": s["p75"], "최대(%)": s["max"]}
            for n, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"])
        ]).to_excel(writer, sheet_name="성분_통계", index=False)

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

        # 설계 근거 시트
        meta = [
            {"항목": "요구사항", "내용": query},
            {"항목": "설계 근거", "내용": formula_data.get("design_rationale", "")},
        ]
        for f in formula_data.get("formulas", []):
            meta.append({
                "항목": f["name"],
                "내용": f"{f.get('concept', '')} | 핵심: {', '.join(f.get('key_ingredients', []))} | aspect: {', '.join(f.get('target_aspects', []))}",
            })
        pd.DataFrame(meta).to_excel(writer, sheet_name="설계_근거", index=False)

        # 비용 시트
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

    console.print(f"[green]✓ {xl_path}[/green]")

    # 통계 JSON
    with open(f"{output_dir}/stats_summary.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ {output_dir}/stats_summary.json[/green]")

# ─────────────────────────────────────────────────────────────────────────────
# 11. 메인 파이프라인
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    data_csv:    str,
    product_csv: str,
    query:       str,
    aws_profile: str | None = None,
    aws_region:  str        = "ap-northeast-2",
    model_id:    str        = "anthropic.claude-3-5-sonnet-20240620-v1:0",
    output_dir:  str        = "output",
):
    console.rule("AI 기반 에센스 처방 자동 생성 PoC v4 (AWS Bedrock / SageMaker)")

    # 1. 처방 데이터
    df, formula_dict = load_formula_data(data_csv)

    # 2. 통계
    console.print("[dim]통계 분석 중...[/dim]")
    stats = build_stats(formula_dict)
    console.print(f"[green]✓ 성분 {len(stats['ingredient_stats'])}종 통계[/green]")

    # 3. 마케팅 키워드 DB
    keyword_db = load_product_data(product_csv, formula_dict)

    # 4. Mapper
    mapper = IngredientMapper(list(stats["ingredient_stats"].keys()))

    # 5. 컨텍스트 구성
    ctx = build_context(query, stats, mapper, keyword_db, formula_dict)

    # 로그
    if ctx["query_matched_ings"]:
        for orig, matched, score in ctx["query_matched_ings"]:
            console.print(f"[dim]  성분 매핑: '{orig}' → '{matched}' (score {score:.2f})[/dim]")
    if ctx["matched_keywords"]:
        console.print(f"[dim]  키워드 매핑: {[k['keyword'] for k in ctx['matched_keywords']]}[/dim]")
    if ctx["keyword_aspects"]:
        console.print(f"[dim]  타겟 aspect: {ctx['keyword_aspects']}[/dim]")
    console.print(
        f"[green]✓ base {len(ctx['base_ings'])}종 / active {len(ctx['active_ings'])}종 / "
        f"유사처방 {len(ctx['similar_formulas'])}건[/green]"
    )

    # 6. 처방 생성
    formula_data, cost = call_claude_api(
        query, ctx, stats["total_formulas"],
        aws_profile = aws_profile,
        aws_region  = aws_region,
        model_id    = model_id,
    )
    if not formula_data:
        console.print("[red]처방 생성 실패[/red]"); return

    # 7. 후처리
    formula_data = validate_and_fix(formula_data, stats)

    # 8. 출력 & 저장
    print_results(formula_data, query)
    save_results(formula_data, stats, keyword_db, query, output_dir, cost)
    print_cost_summary(cost)
    console.print(f"\n[bold green]완료! 결과: {output_dir}/[/bold green]")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="에센스 처방 자동 생성 PoC v4 (AWS Bedrock / SageMaker)")
    parser.add_argument("--data",        required=True, help="처방 CSV (data.csv)")
    parser.add_argument("--product",     required=True, help="마케팅 키워드 CSV (product.csv)")
    parser.add_argument("--query",       required=True, help="제품 요구사항 텍스트")
    parser.add_argument("--aws_profile", default=None,  help="AWS 프로파일명 (로컬 개발용, SageMaker에서는 불필요)")
    parser.add_argument("--aws_region",  default=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"),
                                                        help="AWS 리전 (기본: ap-northeast-2)")
    parser.add_argument("--model",       default="anthropic.claude-3-5-sonnet-20240620-v1:0",
                                                        help="Bedrock 모델 ID")
    parser.add_argument("--output",      default="output")
    args = parser.parse_args()

    run_pipeline(
        data_csv    = args.data,
        product_csv = args.product,
        query       = args.query,
        aws_profile = args.aws_profile,
        aws_region  = args.aws_region,
        model_id    = args.model,
        output_dir  = args.output,
    )
