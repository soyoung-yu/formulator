# CLAUDE.md — AI 기반 화장품 처방 설계 시스템

이 파일은 Claude Code가 이 프로젝트에서 코드를 수정하거나 기능을 추가할 때
반드시 먼저 읽고 따라야 할 행동 규칙이다.

---

## 핵심 원칙

**이 시스템은 연구원의 판단을 보조하는 도구다. 연구원의 결정을 시스템이 조용히 덮어쓰는 코드는 어떤 형태로도 만들지 않는다.**

이 원칙이 아래 모든 규칙보다 우선하며, 설계 결정이 모호할 때 기준이 된다.

---

## 데이터 파일

- `data.csv` — 자사 처방 DB (`bulk_code` / `ingredient_name` / `ingredient_function` / `content`)
- `product.csv` — 마케팅 키워드 DB
- `external.csv` — 타사 제품 전성분 DB
- `models/KR-SBERT-V40K-klueNLI-augSTS/` — 임베딩 모델 (로컬 캐시)
- LLM 인프라: AWS Bedrock (`bedrock-runtime`)

---

## 파이프라인 전체 흐름

`run_pipeline()` 내부 실행 순서와 정확히 일치한다.
**한 단계의 출력이 다음 단계의 입력이므로, 어떤 단계를 수정해도 이 흐름이 유지되어야 한다.**

```
query (str)
  │
  ▼ [1]  extract_query_contexts()            → query_contexts (슬롯 5종)
  ▼ [2]  classify_formulation_type()         → formulation_type (str)
  ▼ [3]  select_structural_ingredients()     → structural_info (dict)
  ▼ [4]  find_target_product()               → target_formula (dict | None)
  ▼ [5]  ConceptExpander.expand()            → concept_ings (list of tuples)
  ▼ [6]  user_constraints 구성               → user_constraints (dict[str, float])
  ▼ [7]  build_context()                     → ctx (dict)
  ▼ [8]  review_active_properties()          → active_properties (dict)
  ▼ [8b] _apply_user_feel_conflicts()        → active_properties (충돌 태그 추가)
  ▼ [8c] _fetch_replacements_for_flagged()   → active_properties (대체 성분 확정)
  ▼ [9]  call_claude_api() + build_user_prompt() → formula_data (dict)
  ▼ [10] validate_and_fix()                  → formula_data (보정됨)
  ▼ [11] print_results() + save_results()    → 출력 및 저장
```

---

## 프롬프트 섹션 모듈 구조 (v0.9~)

`build_user_prompt()`는 섹션 함수들의 조립기다. 프롬프트 내용을 수정할 때는 반드시 이 구조를 따른다.

### 섹션 함수 규칙

```python
def _ps_XXX(필요한_파라미터, **_) -> list[str]:
    # **_ 필수 — PROMPT_SECTIONS에서 호출 시 모든 kwargs가 전달되므로
    # 이 함수가 쓰지 않는 파라미터를 흡수해야 TypeError가 나지 않는다
    if 조건_불충족:
        return []          # 빈 리스트 = 이 섹션 건너뜀
    return ["섹션 내용", ...]
```

모든 섹션 함수가 받을 수 있는 파라미터:
`query`, `ctx`, `total_formulas`, `formulation_type`, `structural_info`, `active_properties`

### 섹션 순서 관리

`PROMPT_SECTIONS` 리스트만 수정하면 된다:

```python
PROMPT_SECTIONS = [
    _ps_header,
    _ps_formulation_type,
    ...
]
```

| 작업 | 방법 |
|------|------|
| 순서 변경 | `PROMPT_SECTIONS` 리스트 항목 순서 변경 |
| 섹션 내용 수정 | 해당 `_ps_XXX()` 함수만 수정 |
| 신규 섹션 추가 | 함수 작성 → `PROMPT_SECTIONS`에 삽입 |
| 섹션 임시 비활성화 | `PROMPT_SECTIONS`에서 해당 줄 주석 처리 |

### 섹션 재배치 시 주의

섹션 함수 간에는 **코드 의존성이 없다** — 모두 동일한 입력을 읽고 독립적으로 `list[str]`을 반환한다.
단, 섹션 내 텍스트가 다른 섹션을 명시적으로 참조하는 경우 함께 수정해야 한다.

예: `_ps_design_guide()`의 `"[구조 성분 설계 가이드]의..."` 문구는 `_ps_structural_ings`의 섹션 제목을 참조한다.
`_ps_structural_ings`의 제목을 바꾸면 `_ps_design_guide()` 내 참조 문구도 같이 수정한다.

---

## 핵심 불변 규칙 (R1~R5)

기능 추가, 리팩토링, 어떤 변경에서도 반드시 유지한다.

### R1. 사용자 지정 함량은 시스템이 덮어쓰지 않는다

`validate_and_fix()`에서 `user_constraints`에 포함된 성분은 함량 보정을 건너뛴다(`continue`).
DB 범위를 벗어나도 보정하지 않고 경고 출력만 한다 (미구현 항목 001).

### R2. DB 화이트리스트 검증은 Python이 담당한다

LLM이 제안한 성분명은 반드시 `known_set`(data.csv 전체 성분명)과 교차 검증 후 확정한다.
이 검증을 우회하거나 생략하는 코드 경로를 만들지 않는다.
적용 위치: `validate_and_fix()` 1단계 / `_fetch_replacements_for_flagged()` / `ConceptExpander._claude_expand()` 결과 필터링

### R3. LLM 호출에는 반드시 예외 처리와 폴백이 있어야 한다

모든 LLM 호출 함수: `try/except` + 실패 시 빈 결과(`{}`, `[]`) 반환 + 파이프라인 계속 실행.

### R4. 파이프라인 단계 간 인터페이스를 변경하면 연쇄 영향을 확인한다

한 단계의 출력 구조(dict key, list 형태)가 바뀌면, 그 출력을 소비하는 모든 하위 단계를 함께 수정한다.
단계별 입출력 명세는 `docs/pipeline.md` 참조.

### R5. 버전 파일 관리

- **"신규 버전으로 기능 추가해줘"** 요청 시: 새 버전 파일 생성 (`formulator_poc_v0.9.py`). 파일 최상단 docstring에 이전 버전 대비 변경 내용 기재.
- **그 외 수정** (버그 픽스, 소규모 튜닝): 현재 파일 직접 수정.
- **신규 버전 생성 후 필수 문서 업데이트**: `docs/logic_overview.md`를 새 버전 코드 기준으로 반드시 업데이트한다. 변경된 단계 로직, 새로운 함수, 데이터 구조 변경을 반영한다.

신규 버전 docstring 형식:
```python
"""
AI 기반 화장품 처방 자동 생성 시스템 — PoC v0.X
================================================
v0.X 변경 내용: v0.X-1 대비

  [명령 N] 추가된 기능 제목
     - 변경 상세 설명
     - ...

전체 흐름:
    ...
"""
```

---

## 판단 기준 — 먼저 물어볼 상황 vs 바로 진행할 상황

### 먼저 물어본다
- 새 파이프라인 단계를 추가하거나 기존 단계를 제거할 때
- `SYSTEM_PROMPT`의 JSON 출력 스키마를 변경할 때
- R1 또는 R2 로직에 영향을 주는 수정일 때
- 파이프라인 단계 실행 순서를 변경할 때

### 바로 진행한다
- `_ps_XXX()` 섹션 함수 내용 수정 (텍스트, 로직 개선)
- `PROMPT_SECTIONS` 순서 변경 또는 섹션 비활성화
- 신규 `_ps_XXX()` 섹션 함수 추가
- 버그 픽스, 예외 처리 추가, 폴백 보완
- `docs/` 문서 업데이트
- `ALIAS_HINTS`, `FEEL_CONFLICT_MAP` 등 상수 값 수정

---

## 변경 시 체크리스트

```
[ 작업 전 ]
  [ ] 어느 파이프라인 단계를 건드리는가?
  [ ] 출력 구조가 바뀌는가? → 하위 단계 연쇄 영향 확인 (R4)
  [ ] R1·R2 로직에 영향을 주는가? → 먼저 물어보기

[ 작업 후 ]
  [ ] docs/logic_overview.md 업데이트 (단계 로직·데이터 구조·파일 구조 변경 시, 신규 버전 생성 시 필수)
  [ ] docs/changelog.md 업데이트 (모든 기능 추가·변경 시)
  [ ] 이 파일의 파이프라인 흐름 다이어그램 업데이트 (단계 추가·제거 시)
```

---

## 비자명한 제약

코드만 봐서는 파악하기 어려운, 의도적으로 설계된 제약들.

| 제약 | 적용 위치 | 이유 |
|------|-----------|------|
| `SYSTEM_PROMPT` 스키마 변경 시 `validate_and_fix()` 파싱 로직도 함께 수정 | 섹션 8, 11 | LLM 출력 구조와 파싱 로직이 1:1 연동됨 |
| 개념어→DB 성분명 다중 매핑 시 모든 후보에 동일 사용자 지정 함량 적용 | `run_pipeline()` 내 `user_constraints` 구성 | LLM이 어느 후보를 선택하든 R1 보호가 작동해야 함 |
| `input()` 호출을 비즈니스 로직 함수 내부에서 분리 가능하게 유지 | `find_target_product()`, `classify_formulation_type()` | 향후 API 서버 전환 시 인터랙티브/비인터랙티브 분기 필요 |
| 모든 `_ps_XXX()` 함수 시그니처에 `**_` 필수 | 모든 섹션 함수 | `PROMPT_SECTIONS` 호출 시 전체 kwargs가 전달되므로, 쓰지 않는 파라미터를 흡수하지 않으면 TypeError 발생 |
| 섹션 내 타 섹션 참조 텍스트는 해당 섹션 제목 변경 시 함께 수정 | `_ps_design_guide()` 등 | 코드 의존성은 없지만 Claude가 읽는 프롬프트 내 의미적 참조가 깨짐 |

---

## 미구현 항목

| ID | 항목 | 위치 | 내용 |
|----|------|------|------|
| 001 | 사용자 지정 함량 DB 범위 경고 | `validate_and_fix()` 이후 | 보정 없이 "살리실릭애씨드 0.5% — DB 선례 없음 (DB max: 0.2%)" 형태로 출력 |
| 002 | `input()` 비인터랙티브 모드 분리 | `find_target_product()`, `classify_formulation_type()` | API 서버 전환 시 필수 |

---

## 문서 지도

| 문서 | 목적 | 업데이트 시점 |
|------|------|--------------|
| `docs/logic_overview.md` | 단계별 로직(WHY/HOW) + 파일 구조 + 중간 데이터 구조 + 모듈화 계획 | **기능 추가·변경 시 필수. 신규 버전 생성 시 반드시 해당 버전 기준으로 업데이트** |
| `docs/changelog.md` | 버전별 변경 이력 | **기능 추가·변경 시 필수** |
| `docs/pipeline.md` | 단계별 입출력 타입, 데이터 구조 명세 | 단계 추가·변경 시 |
| `docs/modules/postprocess.md` | `validate_and_fix()` 로직 상세 | postprocess 변경 시 |
| `docs/modules/mapping.md` | `IngredientMapper`, `ConceptExpander` cascade | 매핑 로직 변경 시 |
