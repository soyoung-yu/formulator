---
inclusion: auto
---

# Formulator 프로젝트 작업 규칙

이 파일은 formulation_automation 프로젝트에서 코드 수정 작업 시 Kiro가 반드시 따라야 할 규칙을 정의한다.

## 수정 요청을 받으면 반드시 이 순서로 동작한다

1. `docs/pipeline.md`에서 해당 단계의 입출력 명세 확인
2. `docs/decisions/`에서 관련 의사결정 이력 확인
3. 수정 전 현재 동작을 한 문장으로 설명하고 확인받기
4. 코드 수정
5. 수정 내용이 기존 decisions와 충돌하거나 새로운 결정이면 `docs/decisions/`에 항목 추가

## 절대 하지 말 것

- 코드를 직접 읽지 않고 동작을 추측하지 말 것
- `docs/decisions/` 기록 없이 로직 변경하지 말 것
- 문서와 코드가 불일치하면 반드시 지적하고 어느 쪽이 맞는지 확인받을 것
- 섹션 번호나 함수명만 보고 내용을 가정하지 말 것

## 현재 파일 구조

메인 코드: `formulator_v2.py` (단일 파일, 2158줄)

| 섹션 | 라인 | 내용 |
|------|------|------|
| 섹션 1 | L97~L215 | FormulatorConfig — 모든 도메인 상수 |
| 섹션 2 | L216~L300 | TypedDict 타입 정의 |
| 섹션 3 | L301~L372 | llm_call() — LLM 호출 단일 진입점 |
| 섹션 4 | L373~L481 | 데이터 로딩 (load_formula_data, build_stats) |
| 섹션 5 | L482~L548 | 텍스트 유틸리티 |
| 섹션 6 | L549~L765 | IngredientMapper, ConceptExpander |
| 섹션 7 | L766~L1387 | 파이프라인 단계 함수들 (622줄) |
| 섹션 8 | L1388~L1629 | build_prompt(), SYSTEM_PROMPT |
| 섹션 9 | L1630~L1801 | generate_formulas(), validate_and_fix() |
| 섹션 10 | L1802~L1992 | 출력 계층 (print_results, save_results) |
| 섹션 11 | L1993~L2158 | run_pipeline() — 메인 파이프라인 |

## 자주 헷갈리는 핵심 로직 (반드시 숙지)

### validate_and_fix()의 함량 보정 우선순위 (섹션 9, L1693)

```
for ing in ings:
    # ① 사용자 지정 → continue (모든 보정 면제)
    if name_n in norm_user:
        continue   ← 이 continue가 ②③을 모두 스킵

    # ② 규제 상한 (regulatory_limit)
    ...
    continue       ← ②에 해당하면 ③ 스킵

    # ③ 통계 max × 1.1
    ...
```

**핵심**: ①번 `continue`는 해당 성분의 모든 보정을 면제한다.
DB max를 초과해도 보정하지 않는다. 연구원의 판단을 시스템이 덮어쓰지 않는다.

**주의**: 보정 면제 ≠ 경고 없음. DB 범위 이탈 경고는 별도 로직(미구현, decisions/001 참조)에서 처방 생성 전에 처리해야 한다.

### ConceptExpander의 3단계 cascade (섹션 6, L616)

```
1단계: _exact() — 공백 제거 + 소문자화 exact match
2단계: _embed() — 임베딩 코사인 유사도 (임계값 0.90, 보수적)
3단계: _llm()  — LLM 호출 → DB 화이트리스트 교차 검증
```

각 단계에서 매핑 성공 시 다음 단계로 넘어가지 않는다.
LLM이 제안한 성분도 반드시 known_set 교차 검증 후 확정한다.

### 타겟 제품 탐색 순서 (섹션 7, L1071)

```
1. _internal_target() — 자사 data.csv 부분일치
2. _external_target() — 타사 external.csv 부분일치
3. 토큰 기반 폴백 — 제품명 토큰화 후 0.80 이상 매칭
```

탐색 실패 시 None 반환, 처방 생성은 타겟 없이 계속 진행한다.

### LLM 역할 경계

- LLM은 DB 목록에서 고르는 것이 아니라 메커니즘 기반으로 추론해야 한다
- DB 화이트리스트 검증은 항상 Python이 담당한다
- LLM 호출은 반드시 llm_call()을 통한다 (직접 invoke_model 호출 금지)

## 관련 문서 위치

- `docs/architecture.md` — 전체 구조 및 데이터 흐름
- `docs/pipeline.md` — 파이프라인 각 단계 입출력 명세
- `docs/modules/postprocess.md` — validate_and_fix 상세
- `docs/modules/mapping.md` — IngredientMapper, ConceptExpander 상세
- `docs/decisions/` — 의사결정 이력
