# 매핑 모듈 — query.py

성분명 매핑 및 질의 보완 관련 함수 상세 문서.

---

## 역할

사용자 질의에서 추출한 성분 표현을 `data.csv`의 정식 성분명으로 변환하고,
Python이 놓친 정보를 LLM 1회 호출로 보완한다.

---

## 함수 구성

### extract_query_info()

질의 텍스트에서 성분·마케팅 힌트를 추출한다. LLM 없음.

**성분 추출 방법 (2단계)**:

1. **DB set 직접 매칭**: `known_set`의 모든 성분명(2자 이상)을 질의에서 substring 탐색
2. **ALIAS_HINTS 키 매칭**: `config.py`의 관용명 사전 키를 `_norm_name()`으로 공백·대소문자 무시해 비교

두 결과를 합쳐 `ingredient_map`으로 직접 반환한다.

```python
def extract_query_info(
    query: str,
    known_set: set[str] | None = None,
    marketing_keywords: set[str] | None = None,
) -> dict
```

**출력 구조**:
```python
{
  "ingredient_map":  {
    "나이아신아마이드": ["나이아신아마이드"],          # DB 직접 매칭
    "시카":            ["병풀추출물", "병풀잎추출물"],  # ALIAS_HINTS 확장
  },
  "marketing_hints": ["미백", "보습"]
}
```

**다중 매핑 주의**:
ALIAS_HINTS 값이 여러 DB 성분명으로 확장될 때, `run_pipeline()`에서 `user_constraints`를 구성할 때 **모든 후보에 동일한 사용자 지정 함량을 적용**한다. LLM이 어느 후보를 최종 선택하든 R1 보호가 동작하기 위함이다.

---

### extract_query_supplements()

Python 추출 결과를 보완하는 **LLM 1회 호출**. 타겟 제품명·추가 성분·추가 키워드·성분-함량 연결을 동시에 반환한다.

```python
def extract_query_supplements(
    query: str,
    ingredient_names: list[str],   # ingredient_map.keys() — 질의 표현 그대로
    marketing_hints: list[str],
    bedrock_client: Any,
    model_id: str,
) -> dict
```

**LLM 프롬프트 구성**:

Python이 먼저 `re.findall(r'\d+(?:\.\d+)?(?=\s*%)', query)`로 함량 값을 추출한 뒤, LLM에 세 섹션을 함께 전달한다.

```
[질의]              — 전체 질의 원문 (문맥용)
[Python 추출 결과]  — 성분명 / 마케팅 키워드 / 함량값 리스트
```

**출력 구조**:
```python
{
  "target_product":         "제품명" | None,
  "additional_ingredients": ["레스베라트롤", ...],  # DB 미검증
  "additional_keywords":    ["광채", ...],
  "ingredient_amounts":     {"나이아신아마이드": 10.0}  # 성분명은 질의 표현 그대로
}
```

**검증 및 폴백**:
- LLM 실패 시 예외 잡아 경고 출력 후 `{}` 반환. 파이프라인 계속 진행.

**user_constraints 변환** (`pipeline.py` 내):
`ingredient_amounts`의 성분명(질의 표현) → `ingredient_map`으로 DB 성분명 확장 → `user_constraints: dict[str, float]`

---

### search_target_product()

LLM이 추출한 제품명으로 자사 → 타사 DB를 순서대로 탐색한다. LLM 없음.

```python
def search_target_product(
    product_name: str,
    formula_dict: dict,
    external_db: dict[str, dict],
) -> dict | None
```

- 자사(`formula_dict`): `bulk_name` exact match 또는 포함 탐색
- 타사(`external_db`): `title` exact match 또는 포함 탐색
- 복수 매칭 시 날짜(`first_in` / `base_time`) 내림차순 → 가나다 오름차순 stable sort
- 미매칭 시 `None` 반환
