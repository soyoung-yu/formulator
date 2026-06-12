# 로직 개요 — v1.1

이 문서는 `formulator/` 패키지(v1.1) 기준이다.

---

## 목적

사용자 자연어 질의를 받아 자사 처방 DB를 참고한 화장품 처방 3안(A/B/C)을 생성한다.
LLM 호출은 최소화(질의 보완 1회 + 처방 생성 1회)하고, 데이터 준비와 검증은 Python이 담당한다.

---

## 파이프라인 흐름

```
query (str)
  │
  ▼ [1]  load_formula_data()              CSV → formula_dict + DataFrame
  ▼ [2]  build_stats()                   성분별 빈도·함량 통계
  ▼ [3]  load_product_data()             마케팅 키워드 → keyword_db
  ▼ [3b] load_external_data()            타사 제품 전성분 → external_db
  ▼ [4]  extract_query_info()            질의 텍스트 분석 (LLM 없음)
  ▼ [4b] extract_query_supplements()     질의 보완 추출 (LLM 1회)
         └─ search_target_product()      타겟 제품 DB 탐색 (Python)
  ▼ [6]  build_context()                 컨텍스트 조립 (LLM 없음)
  ▼ [7]  call_llm()                      처방 3안 생성 (LLM 1회)
  ▼ [8]  validate_and_fix()              화이트리스트 검증 + 합계 보정
  ▼ [9]  print_results() + save_results() 출력 및 저장
```

---

## 단계별 로직

### [1] load_formula_data — `data.py`

- `data.csv`를 읽어 `bulk_code` 단위로 처방 dict를 구성한다.
- `ingredient_function`을 `config.py`의 `_FUNC_TO_ROLE`로 변환해 `structural_roles`를 붙인다.
- `first_in` 컬럼을 포함해 날짜 기반 정렬에 활용한다.
- 함량 합계가 0 이하인 처방은 제외한다.

### [2] build_stats — `data.py`

- formula_dict 전체를 순회해 성분별 빈도(frequency), min/max/median/p25/p75를 계산한다.
- 이 통계가 LLM 프롬프트의 "성분 통계" 섹션과 `validate_and_fix()`의 기준값으로 사용된다.

### [3] load_product_data — `data.py`

- `product.csv`를 읽어 마케팅 키워드별로 연관 성분과 처방 코드를 인덱싱한다.
- `keyword_db[키워드] = {ingredients: Counter, aspects: set, formula_codes: list}`

### [3b] load_external_data — `data.py`

- `external.csv`를 읽어 타사 제품명 → 전성분 목록 dict를 구성한다.
- `external_db[제품명] = {ingredients: [성분명, ...], base_time: "날짜"}`

### [4] extract_query_info — `query.py`

LLM 없이 두 가지 방법으로 성분을 추출한다.

1. **DB set 직접 매칭**: `known_set`(data.csv 전체 성분명)에 질의 문자열이 포함되면 추출
2. **ALIAS_HINTS 키 매칭**: "비타민 C", "시카" 같은 관용명 감지. `_norm_name()`으로 공백·대소문자 무시 후 비교.

마케팅 힌트("미백", "보습")도 별도 키워드 목록으로 추출한다.

### [4b] extract_query_supplements — `query.py`

Python 추출 결과를 보완하는 **LLM 1회 호출**. 아래 4가지를 동시에 반환한다:

- `target_product`: 질의에 언급된 참고 제품명
- `additional_ingredients`: Python이 놓친 추가 성분 힌트 (DB 미검증)
- `additional_keywords`: Python이 놓친 마케팅·사용감 키워드
- `ingredient_amounts`: 성분-함량 연결 `{성분명: float}` — `user_constraints`의 원천

**search_target_product** (Python):
- LLM이 추출한 제품명으로 자사 → 타사 순서로 DB 탐색
- 복수 매칭 시 날짜(`first_in` / `base_time`) 내림차순 → 가나다 오름차순 stable sort

### [6] build_context — `context.py`

LLM 없이 Python만으로 컨텍스트를 구성한다.

**유사 처방 탐색 (`_pick_similar`)**:
- **그룹 A**: 마케팅 키워드 매칭 처방 (동시 매칭 키워드 수 기준 top-3)
- **그룹 B**: 질의 성분이 포함된 처방 중 그룹 A 제외 (매칭 성분 수 기준 top-3)

**성분 통계 5섹션** (중복 없이 우선순위 순서로 배정):
1. 구조 성분 (`base_ings`): 빈도 내림차순 top-15
2. 질의 성분 (`query_active_ings`): 사용자가 지정한 active 성분
3. 타겟 처방 성분 (`target_active_ings`): known_set exact match만 (타사 성분명 불일치 필터링)
4. 유사 처방 성분 (`similar_active_ings`): 그룹 A+B 출현 active 성분
5. 범용 활성 성분 (`general_active_ings`): 고빈도 top-20

**허용 성분 목록**:
- `allowed_ingredients`: 위 5섹션에 등장한 모든 성분 (full stats 제공)
- `remaining_ingredients`: data.csv 전체 중 allowed에 없는 나머지 (이름만 나열)
- 두 출처를 합치면 DB 전체 커버

### [7] call_llm — `llm.py` + `prompt.py`

`build_user_prompt()`가 컨텍스트를 아래 섹션 순서로 조립한다:

1. 요구사항 (질의 원문)
2. 마케팅 포인트
3. 타겟 처방 정보 (자사: 성분+함량 / 타사: 성분명만)
4. 추가 성분 요청 (DB 미검증 힌트)
5. 유사 처방 참고 (그룹 A/B, 성분 구성만)
6. 성분 통계 (5섹션, role 포함)
7. 성분명 매핑 (alias → DB명)
8. 사용자 지정 함량 (다중 매핑 시 "A 또는 B: X%" 형태)
9. 연구원 노하우 (`allowed_ingredients`와 교차해 해당 항목만)
10. 허용 성분 목록
11. 3안 설계 지침 (A: 집중형 / B: 올라운드 / C: 차별화)

JSON 파싱 실패 시 최대 2회 재시도하며, ThrottlingException은 지수 대기 후 재시도한다.

### [8] validate_and_fix — `postprocess.py`

두 단계로 구성된다.

**1단계 — 화이트리스트 검증**:
- 성분명이 `known_set`에 있으면 통과
- 없으면 `_norm_name()` 정규화 exact match 시도
- 매핑 불가 시 LLM에 대체 성분 3개 후보 요청 → DB 매칭된 첫 번째로 교체
- LLM 대체도 실패 시 해당 성분 제거 후 경고 출력

**2단계 — 합계 100% 보정**:
- 합계가 100±0.05% 범위를 벗어나면 정제수 함량으로 조정
- `user_constraints` 성분은 보정하지 않음 (R1)
- 정제수가 없으면 경고 출력

### [9] print_results / save_results — `output.py`

출력 파일 목록:
- `{formula_name}.csv` × 3 (처방별 성분표)
- `formula_output.xlsx` (처방 시트, 성분 통계, 키워드-성분 매핑, 설계 근거, 비용 내역, Claude 프롬프트)
- `stats_summary.json`

---

## 중간 데이터 구조

### `formula_dict`

```python
{
  "BULK001": {
    "name":             "벌크명",
    "first_in":         "2024-01-15",
    "ingredients":      {"정제수": 75.0, "글리세린": 5.0, ...},
    "structural_roles": {"정제수": "base", "글리세린": "base", ...}
  },
  ...
}
```

### `external_db`

```python
{
  "브랜드A 제품명": {
    "ingredients": ["정제수", "글리세린", ...],
    "base_time":   "2024-03-01"
  },
  ...
}
```

### `stats`

```python
{
  "ingredient_stats": {
    "나이아신아마이드": {
      "structural_role": "active_or_unknown",
      "frequency": 0.42,
      "count": 210,
      "min": 0.5, "max": 10.0, "median": 2.0,
      "mean": 2.3, "std": 1.1, "p25": 1.0, "p75": 3.0
    },
    ...
  },
  "total_formulas": 500
}
```

### `query_info`

```python
{
  "ingredient_map":  {"나이아신아마이드": ["나이아신아마이드"], "시카": ["병풀추출물", "병풀잎추출물"]},
  "marketing_hints": ["미백", "보습"]
}
```

### `ingredient_map`

```python
{
  "나이아신아마이드": ["나이아신아마이드"],          # DB 직접 매칭
  "시카":            ["병풀추출물", "병풀잎추출물"],  # ALIAS_HINTS 확장
}
```

### `supplements` (extract_query_supplements 출력)

```python
{
  "target_product":         "제품명" or None,
  "additional_ingredients": ["레스베라트롤", ...],   # DB 미검증
  "additional_keywords":    ["광채", ...],
  "ingredient_amounts":     {"나이아신아마이드": 10.0}
}
```

### `target_product` (search_target_product 출력)

```python
# 자사
{
  "source":       "자사",
  "product_name": "벌크명",
  "code":         "BULK001",
  "ingredients":  [{"name": "정제수", "content": 75.0}, ...]
}
# 타사
{
  "source":       "타사",
  "product_name": "브랜드A 제품명",
  "code":         None,
  "ingredients":  [{"name": "정제수", "content": None}, ...]
}
```

### `ctx` (build_context 출력)

```python
{
  "query_info":            {...},
  "ingredient_map":        {...},
  "user_ing_names":        {"나이아신아마이드", "병풀추출물", "병풀잎추출물"},
  "matched_keywords":      [("미백", {...}), ...],
  "similar_formulas":      {"group_a": [...], "group_b": [...]},
  "base_ings":             [{name, frequency, structural_role, ...}, ...],
  "query_active_ings":     [{name, frequency, ...}, ...],
  "target_active_ings":    [{name, frequency, ...}, ...],
  "similar_active_ings":   [{name, frequency, ...}, ...],
  "general_active_ings":   [{name, frequency, ...}, ...],
  "allowed_ingredients":   ["가수분해대두단백", "글리세린", ...],
  "remaining_ingredients": ["기타성분A", ...],
  # pipeline.py에서 추가
  "total_formulas":        500,
  "user_constraints":      {"나이아신아마이드": 10.0},
  "target_product":        {...},
  "additional_ingredients":["레스베라트롤"]
}
```

### `formula_data` (LLM 출력)

```python
{
  "formulas": [
    {
      "name": "Formula A",
      "concept": "나이아신아마이드 고함량 집중 미백",
      "key_ingredients": ["나이아신아마이드"],
      "target_aspects": ["미백", "산뜻함"],
      "ingredients": [
        {"name": "정제수", "content": 80.0, "role": "기본 용제"},
        {"name": "나이아신아마이드", "content": 10.0, "role": "미백 활성 성분"},
        ...
      ]
    },
    {"name": "Formula B", ...},
    {"name": "Formula C", ...}
  ],
  "design_rationale": "3안 설계 근거"
}
```

---

## 파일 구조

```
formulation_automation/
├── CLAUDE.md               Claude Code 행동 규칙
├── data.csv                자사 처방 DB
├── product.csv             마케팅 키워드 DB
├── external.csv            타사 제품 전성분 DB
├── formulator/             메인 패키지
│   ├── config.py           상수 (ALIAS_HINTS, TACIT_KNOWLEDGE 등)
│   ├── data.py
│   ├── query.py
│   ├── context.py
│   ├── llm.py
│   ├── prompt.py
│   ├── postprocess.py
│   ├── output.py
│   ├── pipeline.py
│   ├── main.py
│   └── __main__.py
├── docs/
│   ├── logic_overview.md   (이 파일)
│   ├── changelog.md
│   ├── pipeline.md
│   └── modules/
│       ├── postprocess.md
│       └── mapping.md
├── legacy/                 이전 버전 단일 파일들
└── output/                 생성 결과물
```
