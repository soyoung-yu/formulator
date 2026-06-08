# Claude Code 지시문 — formulator_poc 구조 재설계

---

## 배경 및 목적

이 코드는 화장품 ODM 연구원이 자연어로 제품 요구사항을 입력하면,
실험 가능한 초기 처방 백본(성분 + 함량 3안)을 생성하는 시스템이다.

현재 v0.8은 아래 구조적 문제가 있어 전면 재설계한다:
- LLM 호출이 5회(슬롯 추출, 제형 분류, 물성 검토, 대체 성분 조회, 처방 생성)로 분산되어 오류가 누적됨
- 하드코딩 룰(FEEL_CONFLICT_MAP, FORMULATION_STRUCTURAL_MAP 등)이 LLM의 판단을 대체하고 있음
- 통계 상한 초과 경고를 출력하면서도 처방을 그대로 내보내는 신뢰 문제

---

## 재설계 목표

**LLM 호출을 1회로 줄이고, Python은 데이터 준비와 합계 보정만 담당한다.**

```
[Python] 데이터 로드 + 컨텍스트 추출
    → [LLM 1회] 처방 3안 생성 (제형 판단, 성분 선택, 함량 결정 포함)
    → [Python] 합계 100% 보정 + 허용 성분 검증
    → 출력
```

---

## 삭제할 것 (기존 코드에서 제거)

다음 함수 및 구조를 완전히 삭제한다:

| 삭제 대상 | 이유 |
|---|---|
| `classify_formulation_type()` | LLM 처방 생성 프롬프트에 통합 |
| `select_structural_ingredients()` | LLM 처방 생성 프롬프트에 통합 |
| `review_active_properties()` | LLM 처방 생성 프롬프트에 통합 |
| `_apply_user_feel_conflicts()` | LLM 처방 생성 프롬프트에 통합 |
| `_fetch_replacements_for_flagged()` | LLM 처방 생성 프롬프트에 통합 |
| `extract_query_contexts()` (LLM 호출 부분) | 아래 참고 |
| `FEEL_CONFLICT_MAP` | 하드코딩 룰 제거 |
| `FORMULATION_STRUCTURAL_MAP` | 하드코딩 룰 제거 |
| `IngredientMapper` 임베딩 모델 부분 | sentence-transformers 의존성 제거, exact match + Claude 2단계로 대체 |
| 통계 상한 초과 `⚠` 경고 출력 | 조용히 보정하거나 프롬프트로 제어 |

`extract_query_contexts()`는 LLM 호출 없이 **정규식 기반으로만** 재작성한다.
성분명, 함량 수치, 제형 키워드를 텍스트에서 추출하는 것은 정규식으로 충분하다.

---

## 새 파이프라인 (의사코드)

```python
def run_pipeline(data_csv, product_csv, external_csv, query, ...):

    # 1. 데이터 로드 (기존 유지)
    df, formula_dict = load_formula_data(data_csv)
    stats = build_stats(formula_dict)
    keyword_db = load_product_data(product_csv, formula_dict)

    # 2. 질의에서 성분명/함량 추출 (정규식, LLM 호출 없음)
    query_info = extract_query_info(query)
    # query_info = {
    #   "ingredients": ["판테놀"],          # 성분명 텍스트
    #   "constraints": {"판테놀": None},    # 함량 지정 시 float, 없으면 None
    #   "formulation_hints": ["끈적이지 않는", "에센스"],
    #   "marketing_hints": ["미백"]
    # }

    # 3. 성분명 → DB 성분명 매핑 (exact match 우선, 실패 시 Claude 소호출 1회)
    ingredient_map = map_ingredients(query_info["ingredients"], stats, bedrock_client, model_id)
    # ingredient_map = {"판테놀": "판테놀"}  # DB에 있으면 그대로, 없으면 Claude가 후보 반환

    # 4. 컨텍스트 구성 (Python만, LLM 호출 없음)
    context = build_context(query, stats, keyword_db, formula_dict, query_info, ingredient_map)
    # context에 포함:
    #   - 유사 처방 top-3 (성분명 + 함량)
    #   - 고빈도 base 성분 통계 (top-15)
    #   - 관련 active 성분 통계 (top-15)
    #   - 허용 성분 목록 (전체)
    #   - 사용자 지정 함량 (있을 경우)

    # 5. LLM 1회 호출 — 처방 3안 생성
    formula_data, cost = call_llm(query, context, bedrock_client, model_id)

    # 6. 후처리 (Python, 최소화)
    formula_data = validate_and_fix(formula_data, stats, known_set)
    # - 허용 성분 목록에 없는 성분명 → exact match로 교체 시도, 불가 시 제거
    # - 합계 != 100% → 정제수 함량으로 보정
    # - 사용자 지정 함량 보호 (덮어쓰지 않음)

    # 7. 출력 + 저장 (기존 유지)
    print_results(formula_data, query)
    save_results(formula_data, stats, keyword_db, query, output_dir, cost)
```

---

## 새 프롬프트 설계

### SYSTEM_PROMPT

```
당신은 화장품 처방 전문가입니다. 연구원이 실험을 시작할 수 있는 초기 처방 백본을 설계합니다.

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
    {"name": "Formula B", ...},
    {"name": "Formula C", ...}
  ],
  "design_rationale": "3안 설계 근거 요약"
}
```

### USER_PROMPT 구조

```
[요구사항]
{query}

[유사 처방 참고 (few-shot, top-3)]
처방1: {name}
  {성분명}: {함량}%
  ...
처방2: ...

[성분 통계 — {N}건 처방 기준]
· {성분명}: 사용빈도 {X}%, 함량 중앙값 {median}%, 범위 {min}~{max}%
...

[사용자 지정 함량]  ← 있을 때만
· {성분명}: {함량}%

[허용 성분 목록 — 반드시 이 목록에서만 선택]
{성분1}, {성분2}, ...
```

---

## 성분명 매핑 로직 (2단계로 단순화)

```python
def map_ingredients(terms, stats, bedrock_client, model_id):
    known_set = set(stats["ingredient_stats"].keys())
    result = {}

    for term in terms:
        # 1단계: 정규화 exact match (공백/특수문자 제거 후 비교)
        normalized = re.sub(r"[\s\-_·•]", "", term).lower()
        matched = next(
            (k for k in known_set if re.sub(r"[\s\-_·•]", "", k).lower() == normalized),
            None
        )
        if matched:
            result[term] = [matched]
            continue

        # 2단계: Claude 소호출 (전체 known_set 전달, 후보 최대 5개 반환)
        # 1단계 실패한 term들을 모아서 1번의 Claude 호출로 처리
        result[term] = None  # 일단 None, 아래에서 일괄 처리

    # 1단계 실패한 것들 Claude 일괄 처리
    unmapped = [t for t, v in result.items() if v is None]
    if unmapped and bedrock_client:
        claude_result = _claude_map_ingredients(unmapped, known_set, bedrock_client, model_id)
        result.update(claude_result)

    return result
```

---

## 유지할 것 (기존 코드에서 그대로 사용)

- `load_formula_data()` — 데이터 로드
- `build_stats()` — 통계 계산
- `load_product_data()` — 마케팅 키워드 로드
- `_pick_similar()` — 유사 처방 탐색
- `validate_and_fix()` — 단, 통계 상한 초과 경고 출력 제거, 보정만 수행
- `print_results()` — 출력
- `save_results()` — 저장
- `calc_cost()`, `print_cost_summary()` — 비용 계산
- CLI (`parse_args`, `main`) — 그대로 유지

---

## 파일명

`formulator_poc_v0_9.py` 로 새로 작성한다. 기존 v0.8은 수정하지 않는다.

---

## 주의사항

- `sentence-transformers`, `faiss-cpu` 의존성을 제거한다. 임베딩 매핑은 사용하지 않는다.
- `input()` 을 이용한 인터랙티브 흐름(제형 타입 재확인, 타겟 제품 선택)은 제거한다.
  모든 판단은 LLM이 하거나 Python이 자동으로 처리한다.
- 타겟 제품 탐색(`find_target_product`) 기능은 이번 재설계에서 제외한다.
  query에 타겟 제품명이 있으면 프롬프트에 텍스트로 전달하는 것으로 대체한다.
- 처방 생성 실패 시 재시도는 최대 2회, JSON 파싱 실패 시에만 재시도한다.
