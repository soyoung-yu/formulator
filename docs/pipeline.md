# 파이프라인 명세 — v1.0

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
    "ingredients":      dict[str, float],   # 성분명 → 함량(%)
    "structural_roles": dict[str, str]      # 성분명 → role
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

## 4. 질의 분석

| 항목 | 내용 |
|------|------|
| 함수 | `extract_query_info(query, known_set, marketing_keywords)` |
| 모듈 | `query.py` |
| 입력 | 질의 문자열, DB 성분명 set, 마케팅 키워드 set |
| 출력 | `query_info: dict` |
| LLM | 없음 |

`query_info` 구조:
```python
{
  "ingredients":       list[str],          # 추출된 성분명 (DB명 또는 alias 키)
  "constraints":       dict[str, None],    # 함량은 5b 단계에서 채워짐
  "formulation_hints": list[str],
  "marketing_hints":   list[str]
}
```

---

## 5. 성분명 매핑

| 항목 | 내용 |
|------|------|
| 함수 | `map_ingredients(terms, known_set)` |
| 모듈 | `query.py` |
| 입력 | 추출된 성분명 리스트, DB 성분명 set |
| 출력 | `ingredient_map: dict[str, list[str] \| None]` |
| LLM | 없음 |

```python
{
  "나이아신아마이드": ["나이아신아마이드"],   # DB 직접 매칭
  "시카":            ["병풀추출물"],          # ALIAS_HINTS 확장
  "알수없는성분":    None                    # 매핑 실패
}
```

---

## 5b. 함량 제약 추출

| 항목 | 내용 |
|------|------|
| 함수 | `extract_amount_constraints(query, ingredient_names, bedrock_client, model_id)` |
| 모듈 | `query.py` |
| 입력 | 질의 문자열, DB 성분명 리스트, Bedrock 클라이언트 |
| 출력 | `user_constraints: dict[str, float]` |
| LLM | 숫자% 패턴 있을 때만 1회 |

실패 시: `{}` 반환, 파이프라인 계속 진행.

---

## 6. 컨텍스트 조립

| 항목 | 내용 |
|------|------|
| 함수 | `build_context(stats, keyword_db, formula_dict, query_info, ingredient_map)` |
| 모듈 | `context.py` |
| 입력 | stats, keyword_db, formula_dict, query_info, ingredient_map |
| 출력 | `ctx: dict` |
| LLM | 없음 |

`ctx` 주요 키:

| 키 | 타입 | 설명 |
|----|------|------|
| `query_info` | dict | 4단계 출력 |
| `ingredient_map` | dict | 5단계 출력 |
| `user_ing_names` | set[str] | 매핑된 DB 성분명 전체 |
| `matched_keywords` | list[tuple] | (키워드, keyword_db 항목) |
| `similar_formulas` | dict | group_a, group_b |
| `base_ings` | list[dict] | 구조 성분 통계 top-15 |
| `active_ings` | list[dict] | 활성 성분 통계 top-15 |
| `allowed_ingredients` | list[str] | 정렬된 전체 성분명 |
| `total_formulas` | int | pipeline.py에서 추가 |
| `user_constraints` | dict[str, float] | pipeline.py에서 추가 |

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
| 함수 | `validate_and_fix(formula_data, stats, known_set, user_constraints)` |
| 모듈 | `postprocess.py` |
| 입력 | LLM 출력 dict, stats, known_set, user_constraints |
| 출력 | `formula_data: dict` (보정됨) |
| LLM | 없음 |

처리 순서:
1. 화이트리스트 검증: 정규화 exact match → 불일치 시 제거
2. 합계 100% 보정: 정제수 함량으로 조정 (user_constraints 성분 제외)

---

## 9. 출력 및 저장

| 항목 | 내용 |
|------|------|
| 함수 | `print_results()`, `save_results()`, `print_cost_summary()` |
| 모듈 | `output.py` |
| 출력 파일 | `{formula}.csv` × 3, `formula_output.xlsx`, `stats_summary.json` |
