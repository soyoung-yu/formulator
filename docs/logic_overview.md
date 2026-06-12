# 로직 개요 — v1.0

이 문서는 `formulator/` 패키지(v1.0) 기준이다.

---

## 목적

사용자 자연어 질의를 받아 자사 처방 DB를 참고한 화장품 처방 3안(A/B/C)을 생성한다.
LLM 호출은 1회(처방 생성)로 최소화하고, 데이터 준비와 검증은 Python이 담당한다.

---

## 파이프라인 흐름

```
query (str)
  │
  ▼ [1]  load_formula_data()              CSV → formula_dict + DataFrame
  ▼ [2]  build_stats()                   성분별 빈도·함량 통계
  ▼ [3]  load_product_data()             마케팅 키워드 → keyword_db
  ▼ [4]  extract_query_info()            질의 텍스트 분석 (LLM 없음)
  ▼ [5]  map_ingredients()               관용명 → DB 성분명 매핑
  ▼ [5b] extract_amount_constraints()    숫자% 있을 때만 LLM 호출
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
- 함량 합계가 0 이하인 처방은 제외한다.

### [2] build_stats — `data.py`

- formula_dict 전체를 순회해 성분별 빈도(frequency), min/max/median/p25/p75를 계산한다.
- 이 통계가 LLM 프롬프트의 "성분 통계" 섹션과 `validate_and_fix()`의 기준값으로 사용된다.

### [3] load_product_data — `data.py`

- `product.csv`를 읽어 마케팅 키워드별로 연관 성분과 처방 코드를 인덱싱한다.
- `keyword_db[키워드] = {ingredients: Counter, aspects: set, formula_codes: list}`

### [4] extract_query_info — `query.py`

LLM 없이 두 가지 방법으로 성분을 추출한다.

1. **DB set 직접 매칭**: `known_set`(data.csv 전체 성분명)에 질의 문자열이 포함되면 추출
2. **ALIAS_HINTS 키 매칭**: "비타민C", "시카" 같은 관용명 감지

제형 힌트("산뜻한", "에센스")와 마케팅 힌트("미백")도 별도 키워드 목록으로 추출한다.

### [5] map_ingredients — `query.py`

- DB set 직접 매칭 성분 → 그대로 통과
- ALIAS_HINTS 키 → `ALIAS_HINTS[key]` 값(DB 성분명 목록)으로 확장
- 매핑 실패 시 `None` 반환 (파이프라인은 계속 진행)

### [5b] extract_amount_constraints — `query.py`

- 질의에 `숫자%` 패턴이 없으면 LLM 호출 없이 빈 dict 반환 (빠른 탈출)
- 있으면 LLM에 성분 목록과 질의를 전달해 `{성분명: float}` 추출
- 결과는 `user_constraints`로 R1 보호 대상이 된다

### [6] build_context — `context.py`

LLM 없이 Python만으로 컨텍스트를 구성한다.

**유사 처방 탐색 (`_pick_similar`)**:
- **그룹 A**: 마케팅 키워드 매칭 처방 (동시 매칭 키워드 수 기준 top-3)
- **그룹 B**: 질의 성분이 포함된 처방 중 그룹 A 제외 (매칭 성분 수 기준 top-3)

**성분 분리**:
- base 성분 (용제·점증·보존·pH 조절·킬레이터): 빈도 내림차순 top-15
- active 성분: 질의 성분 우선 배치 후 빈도 내림차순으로 top-15 채움

### [7] call_llm — `llm.py` + `prompt.py`

`build_user_prompt()`가 컨텍스트를 아래 섹션 순서로 조립한다:
1. 요구사항 (질의 원문)
2. 제형·사용감 요구 / 마케팅 포인트
3. 유사 처방 참고 (성분 구성, 함량 미포함)
4. 성분 통계
5. 성분명 매핑 (alias → DB명, alias만 표시)
6. 사용자 지정 함량
7. 허용 성분 목록
8. 3안 설계 지침 (A: 집중형 / B: 올라운드 / C: 차별화)

JSON 파싱 실패 시 최대 2회 재시도하며, ThrottlingException은 지수 대기 후 재시도한다.

### [8] validate_and_fix — `postprocess.py`

두 단계로 구성된다.

**1단계 — 화이트리스트 검증**:
- 성분명이 `known_set`에 있으면 통과
- 없으면 정규화 exact match 시도 (공백·특수문자 제거 후 비교)
- 매핑 불가 시 해당 성분 조용히 제거

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
    "ingredients":      {"정제수": 75.0, "글리세린": 5.0, ...},
    "structural_roles": {"정제수": "base", "글리세린": "base", ...}
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
  "ingredients":       ["나이아신아마이드", "시카"],
  "constraints":       {"나이아신아마이드": None, "시카": None},
  "formulation_hints": ["에센스", "산뜻한"],
  "marketing_hints":   ["미백", "보습"]
}
```

### `ingredient_map`

```python
{
  "나이아신아마이드": ["나이아신아마이드"],  # DB 직접 매칭
  "시카":            ["병풀추출물", "병풀잎추출물"],  # ALIAS_HINTS 확장
}
```

### `ctx` (build_context 출력)

```python
{
  "query_info":          {...},
  "ingredient_map":      {...},
  "user_ing_names":      {"나이아신아마이드", "병풀추출물", "병풀잎추출물"},
  "matched_keywords":    [("미백", {...}), ...],
  "similar_formulas":    {"group_a": [...], "group_b": [...]},
  "base_ings":           [{name, frequency, min, max, median, ...}, ...],
  "active_ings":         [{name, frequency, ...}, ...],
  "allowed_ingredients": ["가수분해대두단백", "글리세린", ...],  # 정렬된 전체 목록
  "total_formulas":      500,
  "user_constraints":    {"나이아신아마이드": 10.0}
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
├── external.csv            타사 제품 DB (v1.0 미사용)
├── formulator/             메인 패키지
│   ├── config.py
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
