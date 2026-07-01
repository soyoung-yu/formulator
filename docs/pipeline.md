# 파이프라인 명세 — v1.2

`run_pipeline()` (`formulator/pipeline.py`) 기준.

---

## 1. 처방 데이터 로드

| 항목 | 내용 |
|------|------|
| 함수 | `load_formula_data(csv_path)` |
| 모듈 | `data.py` |
| 입력 | `data.csv` 경로 |
| 출력 | `(DataFrame, formula_dict)` |
| LLM | 없음 |

필수 컬럼: `bulk_code`, `bulk_name`, `ingredient_name`, `ingredient_function`, `content`

`formula_dict` 구조:
```python
{
  "BULK001": {
    "name":             str,
    "first_in":         str,                 # 날짜 문자열 (YYYY-MM-DD), 없으면 ""
    "ingredients":      dict[str, float],    # 성분명 → 함량(%)
    "structural_roles": dict[str, str]       # 성분명 → role
  }
}
```

---

## 2. 통계 계산

| 항목 | 내용 |
|------|------|
| 함수 | `build_stats(formula_dict)` |
| 모듈 | `data.py` |
| 입력 | `formula_dict` |
| 출력 | `stats: dict` |
| LLM | 없음 |

`stats` 구조:
```python
{
  "ingredient_stats": {
    "성분명": {
      "structural_role": str,
      "frequency": float,   # 전체 처방 중 사용 비율
      "count": int,
      "min": float, "max": float, "median": float,
      "mean": float, "std": float, "p25": float, "p75": float
    }
  },
  "total_formulas": int
}
```

---

## 3. 마케팅 키워드 DB 로드

| 항목 | 내용 |
|------|------|
| 함수 | `load_product_data(product_csv, formula_dict)` |
| 모듈 | `data.py` |
| 입력 | `product.csv` 경로, `formula_dict` |
| 출력 | `keyword_db: dict` |
| LLM | 없음 |

`keyword_db` 구조:
```python
{
  "미백": {
    "ingredients":    Counter,       # 성분명 → 등장 횟수
    "aspects":        set[str],
    "formula_codes":  list[str]
  }
}
```

---

## 3b. 타사 제품 DB 로드

| 항목 | 내용 |
|------|------|
| 함수 | `load_external_data(csv_path)` |
| 모듈 | `data.py` |
| 입력 | `external.csv` 경로 |
| 출력 | `external_db: dict` |
| LLM | 없음 |

필수 컬럼: `title`, `representation_ingredients` (성분명을 `|`로 구분)

`external_db` 구조:
```python
{
  "브랜드A 제품명": {
    "ingredients": ["정제수", "글리세린", ...],
    "base_time":   "2024-03-01"
  }
}
```

파일 없거나 필수 컬럼 누락 시 빈 dict 반환. 파이프라인 계속 진행.

---

## 4. 질의 분석

| 항목 | 내용 |
|------|------|
| 함수 | `extract_query_info(query, known_set, marketing_keywords)` |
| 모듈 | `query.py` |
| 입력 | 질의 문자열, DB 성분명 set, 마케팅 키워드 set |
| 출력 | `query_info: dict` (ingredient_map 포함) |
| LLM | 없음 |

`query_info` 구조:
```python
{
  "ingredient_map":  dict[str, list[str]],  # 질의 표현 → DB 성분명 리스트
  "marketing_hints": list[str]
}
```

`ingredient_map` 예시:
```python
{
  "나이아신아마이드": ["나이아신아마이드"],          # DB 직접 매칭
  "시카":            ["병풀추출물", "병풀잎추출물"],  # ALIAS_HINTS 확장
}
```

---

## 4b. 질의 보완 추출 (LLM) + 타겟 제품 탐색

| 항목 | 내용 |
|------|------|
| 함수 | `extract_query_supplements(query, ingredient_names, marketing_hints, bedrock_client, model_id)` |
| 모듈 | `query.py` |
| 입력 | 질의 원문, ingredient_map.keys(), marketing_hints, Bedrock 클라이언트 |
| 출력 | `supplements: dict` |
| LLM | 1회 (실패 시 `{}` 반환, 파이프라인 계속) |

Python 추출 결과를 보완하는 단일 LLM 호출. 아래 4가지를 동시에 반환한다.

`supplements` 구조:
```python
{
  "target_product":         "제품명" | None,
  "additional_ingredients": list[str],   # DB 미검증
  "additional_keywords":    list[str],
  "ingredient_amounts":     dict[str, float]  # 성분명(질의 표현) → 함량
}
```

**search_target_product** (Python, LLM 없음):
- LLM이 추출한 제품명으로 자사(`formula_dict`) → 타사(`external_db`) 순서로 탐색
- 복수 매칭 시 날짜(`first_in` / `base_time`) 내림차순 → 가나다 오름차순 stable sort

`target_product` 구조:
```python
# 자사
{"source": "자사", "product_name": str, "code": str, "ingredients": [{"name": str, "content": float}]}
# 타사
{"source": "타사", "product_name": str, "code": None, "ingredients": [{"name": str, "content": None}]}
```

**user_constraints 구성** (`pipeline.py` 내):
alias 키 → DB 성분명 확장 후 다중 매핑 시 모든 후보에 동일 함량 적용 (R1 보호).

```python
user_constraints: dict[str, float] = {
    db_name: amount
    for term, amount in raw_constraints.items()
    if term in ingredient_map
    for db_name in ingredient_map[term]
}
```

---

## 6. 컨텍스트 조립

| 항목 | 내용 |
|------|------|
| 함수 | `build_context(stats, keyword_db, formula_dict, query_info, ingredient_map, target_product)` |
| 모듈 | `context.py` |
| 입력 | stats, keyword_db, formula_dict, query_info, ingredient_map, target_product |
| 출력 | `ctx: dict` |
| LLM | 없음 |

`ctx` 주요 키:

| 키 | 타입 | 설명 |
|----|------|------|
| `query_info` | dict | 4단계 출력 |
| `ingredient_map` | dict | 4단계 출력 |
| `user_ing_names` | set[str] | 매핑된 DB 성분명 전체 |
| `matched_keywords` | list[tuple] | (키워드, keyword_db 항목) |
| `similar_formulas` | dict | `{group_a: [...], group_b: [...]}` |
| `base_ings` | list[dict] | 구조 성분 통계 top-15 |
| `query_active_ings` | list[dict] | 질의 지정 active 성분 |
| `target_active_ings` | list[dict] | 타겟 처방 active 성분 (known_set 필터링) |
| `similar_active_ings` | list[dict] | 유사 처방 출현 active 성분 |
| `general_active_ings` | list[dict] | 고빈도 범용 active 성분 top-20 |
| `allowed_ingredients` | list[str] | 위 5섹션에 등장한 성분 (통계 full 제공) |
| `remaining_ingredients` | list[str] | DB 전체 中 allowed에 없는 나머지 (이름만) |
| `total_formulas` | int | pipeline.py에서 추가 |
| `user_constraints` | dict[str, float] | pipeline.py에서 추가 |
| `target_product` | dict \| None | pipeline.py에서 추가 |
| `additional_ingredients` | list[str] | pipeline.py에서 추가 (DB 미검증 힌트) |

---

## 7. LLM 호출 (처방 3안 생성)

| 항목 | 내용 |
|------|------|
| 함수 | `call_llm(query, ctx, bedrock_client, model_id, max_retries)` |
| 모듈 | `llm.py` |
| 입력 | 질의, ctx, Bedrock 클라이언트, 모델 ID |
| 출력 | `(formula_data \| None, cost_dict, prompt_payload_dict)` |
| LLM | 1회 (JSON 파싱 실패 시 최대 2회 재시도) |

실패 시: `(None, {}, prompt_payload)` 반환 → `run_pipeline()`에서 조기 종료.

---

## 8. 후처리 검증

| 항목 | 내용 |
|------|------|
| 함수 | `validate_and_fix(formula_data, stats, known_set, user_constraints, bedrock_client, model_id)` |
| 모듈 | `postprocess.py` |
| 입력 | LLM 출력 dict, stats, known_set, user_constraints, Bedrock 클라이언트(선택), 모델 ID(선택) |
| 출력 | `formula_data: dict` (보정됨) |
| LLM | 대체 성분 탐색 시 성분당 1회 (bedrock_client 전달 시에만) |

처리 순서:
1. 화이트리스트 검증: 정규화 exact match → 매핑 불가 시 LLM 대체 성분 3개 후보 탐색 → 실패 시 제거
2. 합계 100% 보정: 정제수 함량으로 조정 (user_constraints 성분 제외)

---

## 9. 출력 및 저장

| 항목 | 내용 |
|------|------|
| 함수 | `print_results()`, `save_results()`, `print_cost_summary()` |
| 모듈 | `output.py` |
| 출력 파일 | `{formula}.csv` × 3, `formula_output.xlsx`, `stats_summary.json` |

Excel 시트 구성: 처방 시트(×3), 성분_통계, 키워드_성분매핑, 설계_근거, 비용_내역, Claude_프롬프트
