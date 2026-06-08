# AGENT.md — Codex 작업 규칙

이 파일은 Codex가 이 프로젝트에서 코드를 수정하거나 기능을 추가할 때
먼저 읽고 따라야 할 작업 규칙이다. `CLAUDE.md`의 프로젝트 제약을 Codex 환경에 맞게
재정리한 문서이며, 도메인 규칙과 파이프라인 불변 조건은 동일하게 유지한다.

---

## 핵심 원칙

**이 시스템은 연구원의 판단을 보조하는 도구다. 연구원의 결정을 시스템이 조용히 덮어쓰는 코드는 어떤 형태로도 만들지 않는다.**

이 원칙이 아래 모든 규칙보다 우선하며, 설계 결정이 모호할 때 기준이 된다.

응답은 핵심만 요약해서 간결하게 작성한다.

---

## 주요 데이터와 인프라

- `data.csv` — 자사 처방 DB (`bulk_code` / `ingredient_name` / `ingredient_function` / `content`)
- `product.csv` — 마케팅 키워드 DB
- `external.csv` — 타사 제품 전성분 DB
- `models/KR-SBERT-V40K-klueNLI-augSTS/` — 임베딩 모델 로컬 캐시
- LLM 인프라 — AWS Bedrock `bedrock-runtime`

---

## 파이프라인 기준 흐름

`formulator_poc_v0.9.py`의 `main()` 실행 순서가 기준이다. 한 단계의 출력이 다음 단계의 입력이므로,
출력 구조를 바꾸면 하위 소비 지점을 함께 확인한다.

```
query (str)
  │
  ▼ [1]  QueryContextExtractor.extract()          → QueryContext
  ▼ [2]  DispersionSystemExtractor.extract()      → DispersionJudgement
  ▼ [3]  ProductFormExtractor.extract()           → ProductFormJudgement
  ▼ [4]  FormulationDetailExtractor.extract()     → FormulationDetailJudgement
  ▼ [5]  build_formulation_purpose()              → FormulationPurpose
  ▼ [6]  StructureSkeletonDesigner.design()       → StructureSkeleton
  ▼ [7]  BackboneDesigner.design()                → BackboneDesign
  ▼ [8]  ActiveSuitabilityReviewer.review()
          + BackboneRevisionPlanner.plan()        → 액티브 적합성 및 Backbone 수정 전략
  ▼ [9]  FinalBackboneFinalizer.finalize()        → FinalBackboneResult
  ▼ [10] print_*_summary()                        → 콘솔 출력
```

---

## 프롬프트 섹션 모듈 규칙

`build_user_prompt()`는 섹션 함수 조립기다. 프롬프트 내용을 바꿀 때는 이 구조를 유지한다.

```python
def _ps_XXX(필요한_파라미터, **_) -> list[str]:
    if 조건_불충족:
        return []
    return ["섹션 내용", ...]
```

- 모든 `_ps_XXX()` 함수는 쓰지 않는 인자를 흡수하기 위해 `**_`를 둔다.
- 섹션 순서 변경은 `PROMPT_SECTIONS` 리스트만 수정한다.
- 섹션 제목을 바꾸면 다른 섹션의 참조 문구도 함께 확인한다.

---

## 불변 규칙

### R1. 사용자 지정 함량은 시스템이 덮어쓰지 않는다

`validate_and_fix()`에서 `user_constraints`에 포함된 성분은 함량 보정을 건너뛴다.
DB 범위 또는 규제 상한을 넘더라도 자동 보정하지 않고, 필요한 경우 경고만 출력한다.

### R2. DB 화이트리스트 검증은 Python이 담당한다

LLM이 제안한 성분명은 반드시 `known_set`과 교차 검증한다.
적용 지점: `validate_and_fix()`, `_fetch_replacements_for_flagged()`,
`ConceptExpander._claude_expand()`.

### R3. 모든 LLM 호출에는 예외 처리와 폴백이 있어야 한다

LLM 호출 함수는 `try/except`를 갖고 실패 시 `{}` 또는 `[]` 같은 빈 결과로
파이프라인을 계속 진행해야 한다.

### R4. 단계 간 인터페이스 변경은 연쇄 영향을 확인한다

dict key, list 형태, 반환 tuple 등 출력 구조가 바뀌면 그 값을 소비하는 모든 하위 단계를 함께 수정한다.
상세 입출력은 `docs/pipeline.md`를 기준으로 확인한다.

### R5. 버전 파일 관리

- 사용자가 신규 버전 생성을 명시하면 새 버전 파일을 만들고 상단 docstring에 변경 내용을 쓴다.
- 버그 픽스나 소규모 튜닝은 현재 파일을 직접 수정한다.
- 기능 또는 로직 변경 시 `docs/logic_overview.md`와 `docs/changelog.md`를 업데이트한다.
- 단계 추가·삭제 또는 입출력 변경 시 `docs/pipeline.md`도 업데이트한다.

---

## 먼저 확인해야 하는 변경

다음 변경은 구현 전에 사용자 확인이 필요하다.

- 새 파이프라인 단계 추가 또는 기존 단계 제거
- `SYSTEM_PROMPT`의 JSON 출력 스키마 변경
- R1 또는 R2 로직에 영향을 주는 수정
- 파이프라인 단계 실행 순서 변경

다음 변경은 바로 진행할 수 있다.

- `_ps_XXX()` 섹션 함수 내용 수정
- `PROMPT_SECTIONS` 순서 변경 또는 섹션 비활성화
- 신규 `_ps_XXX()` 섹션 추가
- 버그 픽스, 예외 처리 추가, 폴백 보완
- `docs/` 문서 업데이트
- `ALIAS_HINTS`, `FEEL_CONFLICT_MAP` 등 상수 값 수정

---

## 작업 체크리스트

작업 전:
- 어느 파이프라인 단계를 건드리는지 확인한다.
- 출력 구조가 바뀌는지 확인한다.
- R1·R2 또는 파이프라인 순서에 영향이 있으면 먼저 사용자 확인을 받는다.

작업 후:
- 기능·로직 변경 시 `docs/logic_overview.md`와 `docs/changelog.md`를 업데이트한다.
- 단계 입출력 변경 시 `docs/pipeline.md`를 업데이트한다.
- `python -m py_compile formulator_poc_v0.9.py`로 최소 문법 검증을 수행한다.

---

## 비자명한 제약

| 제약 | 적용 위치 | 이유 |
|------|-----------|------|
| `SYSTEM_PROMPT` 스키마 변경 시 `validate_and_fix()` 파싱 로직도 함께 확인 | 생성/후처리 | 출력 구조와 파싱 로직이 연동됨 |
| 개념어가 여러 DB 성분명으로 매핑되면 모든 후보에 동일 사용자 지정 함량 적용 | `run_pipeline()` | LLM 선택 후보와 무관하게 R1 보호 |
| `input()` 의존 함수는 향후 비인터랙티브 분리가 가능하도록 유지 | `find_target_product()`, `classify_formulation_type()` | API 서버 전환 대비 |
| 모든 `_ps_XXX()` 함수 시그니처에 `**_` 유지 | 프롬프트 섹션 | 전체 kwargs 전달 시 TypeError 방지 |

---

## 문서 지도

| 문서 | 목적 |
|------|------|
| `docs/logic_overview.md` | 단계별 로직, 데이터 구조, 파일 구조 |
| `docs/changelog.md` | 버전별 변경 이력 |
| `docs/pipeline.md` | 단계별 입출력 타입 명세 |
| `docs/modules/postprocess.md` | `validate_and_fix()` 상세 |
| `docs/modules/mapping.md` | `IngredientMapper`, `ConceptExpander` cascade |
