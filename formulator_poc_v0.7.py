"""
AI 기반 에센스 처방 자동 생성 시스템 — PoC v0.7
================================================
v0.7 변경 내용: v0.6 대비
  1. 타겟 제품 슬롯 기반 질의 해석 추가
     - Claude가 사용자 질의에서 성분 / 제형 / 마케팅 포인트 / 타겟 제품을 JSON으로 구조화
     - target_product가 없으면 v0.6 기존 로직 그대로 진행
     - target_product가 있으면 성분명 탐색 전에 타겟 제품 탐색 단계로 분기

  2. 타겟 제품 탐색 파이프라인 추가
     - 1단계: data.csv의 bulk_name을 띄어쓰기 제거 후 부분일치로 자사 제품 탐색
     - 자사 제품 복수 매칭 시 자동 선택하지 않고 사용자 번호 선택 요청
     - 2단계: external.csv의 title을 띄어쓰기 제거 후 부분일치로 타사 제품 탐색
     - 타사 제품 복수 매칭 시 title + base_time 후보를 표시하고 사용자 선택 요청
     - 3단계: 자사/타사 부분일치 실패 시 토큰 80% 이상 일치 후보를 양쪽 DB에서 수집

  3. 타사 제품 성분 정규화 추가
     - representation_ingredients를 | 구분자로 파싱
     - 기존 성분명 탐색 로직(exact → embedding → Claude)을 재사용해 data.csv 성분명으로 정규화
     - 원본 성분명과 정규화명을 쌍으로 보존하고, 실패 시 원본값을 유지
     - 성분 순서를 기반으로 앞쪽 성분에 더 높은 가중치를 부여

  4. 유사처방/컨텍스트/프롬프트 타겟 처방 반영
     - 타겟 처방이 확정되면 이를 베이스로 고정하고 유사처방은 보조 참고용으로만 사용
     - 자사 제품은 전체 처방(성분명 + 성분량)을 컨텍스트에 포함
     - 타사 제품은 원본명 + 정규화명 쌍, 순서 기반 가중치, base_time을 컨텍스트에 포함
     - Claude 프롬프트에 "타겟 처방을 베이스로 요청 방향에 맞게 수정" 지시 추가

  5. 미정규화 타사 성분 처리 지시 추가
     - Claude 생성 단계에서 data.csv 내 대체 가능한 유사 성분을 판단하도록 지시
     - 대체 가능 시 "[대체됨] 원본 → 대체"를, 불가 시 "[대체 불가]"를 출력에 명시
     - 후처리 화이트리스트 검증과 역할이 섞이지 않도록 기존 검증 로직은 유지

전체 흐름:
    입력 질의 → 슬롯 추출(target_product 포함) → 타겟 제품 탐색(있을 때) →
    성분명 탐색 → 통계/마케팅 컨텍스트 구성 → Claude 처방 생성 →
    화이트리스트 검증/함량 보정 → 콘솔/CSV/Excel 저장

Usage:
    python formulator_poc_v0.7.py \\
        --data data.csv \\
        --product product.csv \\
        --external external.csv \\
        --query "파티온 노스카나인 트러블 세럼을 베이스로 진정 에센스"

    python formulator_poc_v0.7.py ... --model us.anthropic.claude-haiku-4-5-20251001-v1:0

Requirements:
    pip install pandas numpy openpyxl boto3
    (편집거리): pip install python-Levenshtein
    (임베딩):   pip install sentence-transformers faiss-cpu
    (컬러출력): pip install rich
"""

import argparse, ast, json, os, re, warnings
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import boto3
import pandas as pd
from openpyxl.styles import Alignment

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

QUERY_CONTEXT_KEYS = ("ingredients", "formulation", "marketing_points", "target_product")


def _empty_query_contexts() -> dict[str, list[str]]:
    return {key: [] for key in QUERY_CONTEXT_KEYS}


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
        kws, asps = [], []
        try: kws  = ast.literal_eval(row["marketing_keywords_list"])
        except: pass
        try: asps = ast.literal_eval(str(row["aspect_list"]))
        except: pass

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
        "다음 화장품 제품 요구사항 문장에서 맥락 표현을 4개 카테고리로만 구조화하세요.\n"
        "반드시 JSON만 반환하세요.\n\n"
        "[카테고리 정의]\n"
        "- ingredients: 원료/효능성 소재명\n"
        "- formulation: 제형, 물성, 외관, 사용감 표현\n"
        "- marketing_points: 미백, 브라이트닝, 안티에이징, 진정 같은 마케팅 소구 표현\n\n"
        "- target_product: 사용자가 베이스/참조/타겟으로 삼으려는 구체적인 제품명\n\n"
        "[중요 제약]\n"
        "- 전체 문장을 복사하지 말 것\n"
        "- 원문에서 직접 뽑은 짧은 명사구만 반환할 것\n"
        "- 없는 카테고리는 빈 배열\n"
        "- 중복 및 부분중복 제거\n"
        "- '비타민', '비타민C', '비타민 C'가 겹치면 가장 정보량이 많은 표현 하나만 남길 것\n"
        "- 숫자/퍼센트만 있는 조각은 제외할 것\n"
        "- 관용명/음차 표현도 성분 표현이면 ingredients에 포함할 것\n"
        "- target_product에는 제품명만 넣고, 일반 효능/제형/성분 표현은 넣지 말 것\n"
        f"{hints_section}\n"
        f"[입력 문장]\n{query}\n\n"
        "반환 형식:\n"
        "{\"ingredients\": [\"...\"], \"formulation\": [\"...\"], "
        "\"marketing_points\": [\"...\"], \"target_product\": [\"...\"]}"
    )

    try:
        resp = bedrock_client.invoke_model(
            modelId=model_id,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 800,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            })
        )
        body = json.loads(resp["body"].read())
        raw = body["content"][0]["text"].strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
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
        max_len = 80 if key == "target_product" else 20
        result[key] = _dedupe_context_phrases(values, max_len=max_len)

    return result


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
            resp = self.bedrock.invoke_model(
                modelId=self.model_id,
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens":        1500,
                    "temperature":       0,
                    "messages": [{"role": "user", "content": prompt}],
                })
            )
            body   = json.loads(resp["body"].read())
            raw    = body["content"][0]["text"].strip()
            raw    = re.sub(r"^```(?:json)?\s*", "", raw)
            raw    = re.sub(r"\s*```$",           "", raw)
            result = json.loads(raw)
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
        if any(token == cand or token in cand or cand in token for cand in candidate_tokens)
    )
    return matched / len(query_tokens)


def _prompt_select_candidate(
    title: str,
    candidates: list[dict],
    formatter,
    allow_none: bool = False,
) -> dict | None:
    console.print(f"[yellow]{title}[/yellow]")
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

    candidates = []
    for _, row in matches[["bulk_code", "bulk_name"]].drop_duplicates().iterrows():
        code = row["bulk_code"]
        if code in formula_dict:
            candidates.append({"bulk_code": code, "bulk_name": row["bulk_name"]})

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

    internal_seen = set()
    for _, row in df[["bulk_code", "bulk_name"]].drop_duplicates().iterrows():
        code = row["bulk_code"]
        if code not in formula_dict or code in internal_seen:
            continue
        score = _token_match_score(query_tokens, row["bulk_name"])
        if score >= 0.80:
            internal_seen.add(code)
            candidates.append({
                "source": "internal",
                "score": score,
                "bulk_code": code,
                "bulk_name": row["bulk_name"],
            })

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
        base_time = _base_time_sort_key(candidate) if candidate["source"] == "external" else pd.Timestamp.max
        return (-candidate["score"], source_rank, -base_time.value)

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
    concept_ings: list | None = None,   # ConceptExpander 성분명 탐색 결과
    target_formula: dict | None = None,
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

    similar_formulas = _pick_similar(
        formula_dict, query_ing_names | target_ing_names,
        priority_codes=keyword_formula_codes,
        n=3,
        exclude_codes=target_exclude_codes,
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


def _pick_similar(formula_dict, query_ing_names, priority_codes=None, n=3, exclude_codes=None):
    priority_codes = priority_codes or set()
    exclude_codes = exclude_codes or set()

    scores = []
    for code, fd in formula_dict.items():
        if code in exclude_codes:
            continue
        overlap     = sum(1 for nm in fd["ingredients"] if nm in query_ing_names)
        is_priority = 1 if code in priority_codes else 0
        scores.append((is_priority, overlap, len(fd["ingredients"]), code))

    scores.sort(reverse=True)
    return [
        {"bulk_code": c, "name": formula_dict[c]["name"],
         "ingredients": formula_dict[c]["ingredients"]}
        for _, _, _, c in scores[:n]
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


def build_user_prompt(query: str, ctx: dict, total_formulas: int) -> str:
    lines = []

    # 요구사항
    lines.append(f"[사용자 요구사항]\n{query}")

    query_contexts = ctx.get("query_contexts", {})
    lines.append("\n[추출된 맥락 표현]")
    lines.append(f"  · 성분 표현: {_join_or_none(query_contexts.get('ingredients', []))}")
    lines.append(f"  · 제형 표현: {_join_or_none(query_contexts.get('formulation', []))}")
    lines.append(f"  · 마케팅 포인트: {_join_or_none(query_contexts.get('marketing_points', []))}")
    lines.append(f"  · 타겟 제품: {_join_or_none(query_contexts.get('target_product', []))}")

    # 성분명 탐색/확정 결과
    if ctx["query_matched_ings"]:
        lines.append("\n[DB 내 최종 반영 성분]")
        for orig, matched, score in ctx["query_matched_ings"]:
            lines.append(f"  · '{orig}' → '{matched}' (유사도 {score:.2f})")

    target_formula = ctx.get("target_formula")
    if target_formula:
        lines.append("\n[타겟 처방 — 베이스 고정]")
        lines.append(
            "아래 타겟 처방을 베이스로, 사용자 요청 방향에 맞게 수정한 처방 3안을 생성하세요. "
            "유사 처방 참고 예시는 보조 참고용이며 타겟 베이스를 대체하면 안 됩니다."
        )
        if target_formula.get("source") == "internal":
            lines.append(
                f"  · 구분: 자사 제품 / bulk_code: {target_formula.get('bulk_code', '')} "
                f"/ 벌크명: {target_formula.get('name', '')}"
            )
            for nm, pct in sorted(target_formula.get("ingredients", {}).items(), key=lambda x: -x[1]):
                lines.append(f"    {nm}: {pct:.4f}%")
        elif target_formula.get("source") == "external":
            lines.append(
                f"  · 구분: 타사 제품 / title: {target_formula.get('title', '')} "
                f"/ base_time: {target_formula.get('base_time', '')}"
            )
            lines.append("  · base_time은 사용자 구분 및 출력 참고 정보이며 탐색/처방 생성 로직의 필터로 쓰지 마세요.")
            lines.append("  · 타사 제품 성분 쌍(원본명 → data.csv 정규화명, 순서 기반 가중치):")
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
                "대체 가능하면 출력에 '[대체됨] 원본: XXX → 대체: YYY'를 명시하고, "
                "대체 불가하면 '[대체 불가] XXX: data.csv 내 유사 성분 없음'을 명시하세요."
            )

    # 마케팅 키워드 패턴
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
        lines.append(_format_stat_line(i))

    # active 성분 통계
    lines.append(f"\n[활성·기능성 성분 통계]")
    active_ings_sorted = sorted(
        ctx["active_ings"],
        key=lambda x: (-x["count"], -x["frequency"], x["name"]),
    )
    for i in active_ings_sorted:
        lines.append(_format_stat_line(i))

    # few-shot 유사처방
    if target_formula:
        lines.append(f"\n[유사 처방 참고 예시 (few-shot, 보조 참고용)]")
    else:
        lines.append(f"\n[유사 처방 참고 예시 (few-shot)]")
    for idx, f in enumerate(ctx["similar_formulas"][:2]):
        lines.append(f"  [참고처방 {idx+1}] {f['name']}")
        for nm, pct in sorted(f["ingredients"].items(), key=lambda x: -x[1])[:14]:
            lines.append(f"    {nm}: {pct:.3f}%")

    # 허용 성분 목록 (화이트리스트 명시)
    allowed = ctx.get("allowed_ingredients", [])
    if allowed:
        lines.append(
            f"\n[허용 성분 목록 — 반드시 이 목록에 있는 성분명만 사용 (총 {len(allowed)}종)]\n"
            + ", ".join(allowed)
        )

    # 설계 지침
    lines.append("""
[3안 설계 지침]
- Formula A: 핵심 활성 성분 고함량 + 성분 수 최소화 (심플 & 집중 효능)
- Formula B: 핵심 효능 + 보습·진정 복합 기능 밸런스 (올라운드 실용 처방)
- Formula C: 트렌드 성분 또는 복합 활성 성분 추가, 마케팅 소구점 강화 (프리미엄·차별화)

각 안의 함량 합계가 정확히 100.00%가 되도록 정제수 함량으로 조정하세요.
마케팅 키워드에서 도출된 성분 패턴과 aspect를 처방 설계에 반영하세요.
[추출된 맥락 표현]의 제형/사용감 요구가 있으면 반드시 반영하세요.
[추출된 맥락 표현]의 마케팅 포인트가 있으면 target_aspects와 설계 근거에 반영하세요.
[허용 성분 목록]에 없는 성분은 절대 사용하지 마세요.""")

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
    if aws_profile:
        session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
    else:
        session = boto3.Session()
    return session.client(service_name="bedrock-runtime", region_name=aws_region)


def call_claude_api(
    query,
    ctx,
    total_formulas,
    bedrock_client=None,        # 외부에서 주입 가능 (ContextExtractor + ConceptExpander와 공유)
    aws_profile  = None,
    aws_region   = "ap-northeast-2",
    model_id     = "anthropic.claude-3-5-sonnet-20240620-v1:0",
    max_retries  = 2,
):
    import time
    prompt_payload = {}

    if bedrock_client is None:
        try:
            bedrock_client = _create_bedrock_client(aws_profile, aws_region)
        except Exception as e:
            console.print(f"[red]Bedrock 클라이언트 생성 실패: {e}[/red]")
            return None, {}, prompt_payload

    user_prompt = build_user_prompt(query, ctx, total_formulas)
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

        raw = assistant_message.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$",          "", raw)

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

def validate_and_fix(
    formula_data: dict,
    stats: dict,
    known_set: set | None = None,       # data.csv 성분 화이트리스트
    mapper: IngredientMapper | None = None,  # 미등록 성분 교체용
) -> dict:
    """
    1) 화이트리스트 검증
       - 미등록 성분을 mapper로 가장 유사한 실존 성분으로 교체 (score ≥ 0.85)
       - 교체 불가 시 경고 태그 부착
    2) 함량 합계 100% 보정 (정제수 조정)
    3) 통계 범위 이상치 경고 태그
    """
    REPLACE_THRESHOLD = 0.85
    ist = stats["ingredient_stats"]

    for formula in formula_data.get("formulas", []):
        ings = formula.get("ingredients", [])

        # ── 화이트리스트 검증 ────────────────────────────────────────────
        if known_set:
            checked = []
            for ing in ings:
                name = ing.get("name", "")
                if name in known_set:
                    checked.append(ing)
                else:
                    if mapper:
                        matched, score, _ = mapper.map(name)
                        if matched and score >= REPLACE_THRESHOLD:
                            console.print(
                                f"[yellow]  ⚠ 미등록 성분 교체: '{name}' → '{matched}' "
                                f"(score {score:.2f})[/yellow]"
                            )
                            ing["name"] = matched
                            ing["role"] = (
                                ing.get("role", "") +
                                f" ['{name}'→'{matched}' 자동교체]"
                            ).strip()
                        else:
                            nearest = matched or "없음"
                            console.print(
                                f"[red]  ✗ 미등록 성분 교체 불가: '{name}' "
                                f"(최근접: {nearest}, score {score:.2f})[/red]"
                            )
                            ing["role"] = (
                                ing.get("role", "") + " ⚠ 미등록 성분(DB 미존재)"
                            ).strip()
                    else:
                        ing["role"] = (
                            ing.get("role", "") + " ⚠ 미등록 성분(DB 미존재)"
                        ).strip()
                    checked.append(ing)
            formula["ingredients"] = checked

        # ── 함량 합계 100% 보정 ──────────────────────────────────────────
        ings  = formula.get("ingredients", [])
        total = sum(i.get("content", 0) for i in ings)
        if abs(total - 100.0) > 0.05:
            for ing in ings:
                if "정제수" in ing.get("name", "") or "water" in ing.get("name", "").lower():
                    ing["content"] = round(ing["content"] + (100.0 - total), 6)
                    ing["role"]    = (ing.get("role", "") + " [합계보정]").strip()
                    break

        # ── 통계 범위 이상치 경고 ────────────────────────────────────────
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
            print(f"{'성분명':<34} {'함량(%)':>10}  역할\n"+"-"*75)
            total = 0.0
            for i in ings:
                c = i.get("content", 0); total += c
                print(f"{i.get('name', ''):<34} {c:>10.4f}  {i.get('role', '')}")
            print("-"*75 + f"\n{'합계':<46} {total:>10.4f}")

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
    cost: dict = {},
    prompt_payload: dict | None = None,
):
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    for formula in formula_data.get("formulas", []):
        fname = formula["name"].replace(" ", "_").lower()
        rows = _formula_ingredient_rows(formula)
        pd.DataFrame(rows).to_csv(f"{output_dir}/{fname}.csv", index=False, encoding="utf-8-sig")
        console.print(f"[green]✓ {output_dir}/{fname}.csv[/green]")

    xl_path = f"{output_dir}/formula_output.xlsx"
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

    with open(f"{output_dir}/stats_summary.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ {output_dir}/stats_summary.json[/green]")


# ─────────────────────────────────────────────────────────────────────────────
# 14. 메인 파이프라인
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    data_csv:    str,
    product_csv: str,
    external_csv: str,
    query:       str,
    aws_profile: str | None = None,
    aws_region:  str        = "ap-northeast-2",
    model_id:    str        = "anthropic.claude-3-5-sonnet-20240620-v1:0",
    output_dir:  str        = "output",
):
    console.rule("AI 기반 에센스 처방 자동 생성 PoC v0.7 (AWS Bedrock / SageMaker)")

    # ── Bedrock 클라이언트 조기 생성 (ConceptExpander + 처방 생성 API 공유) ──
    try:
        bedrock_client = _create_bedrock_client(aws_profile, aws_region)
        console.print("[green]✓ Bedrock 클라이언트 생성[/green]")
    except Exception as e:
        console.print(f"[red]Bedrock 클라이언트 생성 실패: {e}[/red]"); return

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

    # 5. 질의 맥락 표현 추출 + 성분명 탐색
    console.print("[dim]쿼리 내 맥락 표현 분석 중...[/dim]")
    query_contexts = extract_query_contexts(query, bedrock_client=bedrock_client, model_id=model_id)
    console.print(f"[dim]  성분 표현: {query_contexts['ingredients']}[/dim]")
    console.print(f"[dim]  제형 표현: {query_contexts['formulation']}[/dim]")
    console.print(f"[dim]  마케팅 포인트: {query_contexts['marketing_points']}[/dim]")
    console.print(f"[dim]  타겟 제품: {query_contexts['target_product']}[/dim]")

    expander   = ConceptExpander(
        known_ingredients=list(stats["ingredient_stats"].keys()),
        mapper=mapper,
        bedrock_client=bedrock_client,
        model_id=model_id,
    )

    # 5-1. 타겟 제품 탐색(target_product가 있을 때만)
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
    console.print(f"[green]✓ 성분명 탐색[/green]")
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

    # 6. 컨텍스트 구성
    ctx = build_context(
        query,
        stats,
        keyword_db,
        formula_dict,
        query_contexts=query_contexts,
        concept_ings=concept_ings,
        target_formula=target_formula,
    )

    if ctx["matched_keywords"]:
        console.print(f"[dim]  키워드 매핑: {[k['keyword'] for k in ctx['matched_keywords']]}[/dim]")
    if ctx["keyword_aspects"]:
        console.print(f"[dim]  타겟 aspect: {ctx['keyword_aspects']}[/dim]")
    console.print(
        f"[green]✓ base {len(ctx['base_ings'])}종 / active {len(ctx['active_ings'])}종 / "
        f"유사처방 {len(ctx['similar_formulas'])}건 / 허용 성분 {len(ctx['allowed_ingredients'])}종[/green]"
    )

    # 7. 처방 생성
    formula_data, cost, prompt_payload = call_claude_api(
        query, ctx, stats["total_formulas"],
        bedrock_client=bedrock_client,
        aws_profile=aws_profile,
        aws_region=aws_region,
        model_id=model_id,
    )
    if not formula_data:
        console.print("[red]처방 생성 실패[/red]"); return

    # 8. 후처리 (화이트리스트 검증 포함)
    formula_data = validate_and_fix(formula_data, stats, known_set=known_set, mapper=mapper)

    # 9. 출력 & 저장
    print_results(formula_data, query)
    save_results(formula_data, stats, keyword_db, query, output_dir, cost, prompt_payload=prompt_payload)
    print_cost_summary(cost)
    console.print(f"\n[bold green]완료! 결과: {output_dir}/[/bold green]")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="에센스 처방 자동 생성 PoC v0.7 (AWS Bedrock / SageMaker)")
    parser.add_argument("--data",        required=True, help="처방 CSV (data.csv)")
    parser.add_argument("--product",     required=True, help="마케팅 키워드 CSV (product.csv)")
    parser.add_argument("--external",    default="external.csv", help="타사 제품 CSV (external.csv)")
    parser.add_argument("--query",       required=True, help="제품 요구사항 텍스트")
    parser.add_argument("--aws_profile", default=None,  help="AWS 프로파일명 (로컬 개발용)")
    parser.add_argument("--aws_region",  default=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"),
                                                        help="AWS 리전 (기본: ap-northeast-2)")
    parser.add_argument("--model",       default="anthropic.claude-3-5-sonnet-20240620-v1:0",
                                                        help="Bedrock 모델 ID")
    parser.add_argument("--output",      default="output")
    args = parser.parse_args()

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
