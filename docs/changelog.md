# 변경 이력

---

## [v1.2] 2026-06-16

### 유사 처방 성분 함량 공개

- **유사 처방 ingredients 구조 변경** (`context.py`): `_pick_similar()`에서 group_a/group_b의 `ingredients`를 성분명 문자열 리스트 → `[{"name": str, "content": float}]` 딕셔너리 리스트로 변경. 함량 정보를 프롬프트에 노출.
- **프롬프트 성분 구성 표시 강화** (`prompt.py`): 유사 처방 성분 포맷을 `"성분명"` → `"성분명 X%"` 형태로 변경. 헤더 문구도 "성분 구성 참고용, 함량 미포함" → "실제 출시 처방. 성분·함량 구성을 적극 반영해 설계할 것"으로 강화.

### 관용어 추가

- **ALIAS_HINTS `'살리실산'` 추가** (`config.py`): `'살리실산'` → `["살리실릭애씨드"]` 매핑 추가.

---

## [v1.1] 2026-06-12

### 질의 분석 개선

- **ALIAS_HINTS 매칭 공백·대소문자 무시**: "비타민 C"처럼 공백이 포함된 질의에서 alias 키("비타민c")가 매칭되지 않던 문제 수정. `_norm_name()`으로 양쪽 정규화 후 비교.

### 타겟 제품 탐색

- **external.csv 타사 제품 DB 지원**: `load_external_data()`로 타사 제품 전성분 로드. `search_target_product()`에서 자사 → 타사 순으로 탐색.
- **날짜 기반 우선순위 정렬**: 복수 매칭 시 `first_in`(자사) / `base_time`(타사) 내림차순 → 가나다 오름차순 stable sort.

### 컨텍스트 구성 개선

- **성분 통계 5섹션 분리**: 구조 성분 / 질의 성분 / 타겟 처방 성분 / 유사 처방 성분 / 범용 활성 성분으로 분리. 각 성분에 `structural_role` 표시.
- **허용 성분 목록 구조화**: 통계에 등장한 성분(`allowed_ingredients`) + 나머지 DB 전체(`remaining_ingredients`)로 분리. 사실상 DB 전체 허용.
- **타겟 처방 성분 필터링**: 타사 성분명 불일치 시 통계 미제공, 성분명은 그대로 표시.

### 질의 보완 LLM 통합

- **`extract_query_supplements()` 신설**: 기존 타겟 제품명 추출 + 성분-함량 연결 LLM 2회 호출을 1회로 통합. 추가 성분 힌트·키워드도 동시 반환.

### 후처리 개선

- **LLM 대체 성분 제안**: 화이트리스트 검증 실패 시 조용히 제거하는 대신 LLM이 유사 성분 3개 후보 제안 후 DB 매칭된 첫 번째로 교체.

### 암묵지 반영

- **`TACIT_KNOWLEDGE` 추가** (`config.py`): 성분 속성·조합 주의사항 등 연구원 노하우 딕셔너리. 단일 성분 키 및 tuple 키(성분 조합) 지원.
- **`[연구원 노하우]` 프롬프트 주입** (`prompt.py`): `allowed_ingredients`와 교차해 해당 성분의 노하우만 선택적으로 프롬프트에 포함.
- **SYSTEM_PROMPT 규칙 추가**: 연구원 노하우 활용 지시(규칙 9), 허용 성분 출처 명확화(규칙 4), role 활용 지시(규칙 6), 추가 성분 요청 처리(규칙 8).

---

## [v1.0] 2026-06-02

### 전체 재설계 — 단일 파일 → 패키지 구조

v0.9까지의 단일 파일(`formulator_poc_vX.X.py`) 구조를 `formulator/` 패키지로 전면 재설계.
LLM 호출 횟수를 최대 5회 → 1회로 줄이고, 클래스 기반 파이프라인을 함수 기반으로 단순화.

### 패키지 구조

```
formulator/
├── config.py       상수 전용 모듈
├── data.py         CSV 로드 + 통계 계산
├── query.py        질의 분석 + 성분명 매핑
├── context.py      유사 처방 탐색 + 컨텍스트 조립
├── llm.py          Bedrock 클라이언트 + LLM 호출
├── prompt.py       SYSTEM_PROMPT + build_user_prompt()
├── postprocess.py  LLM 출력 검증 + 합계 보정
├── output.py       콘솔 출력 + 파일 저장 + 비용 계산
├── pipeline.py     run_pipeline() 오케스트레이터
└── main.py         CLI 진입점
```

### 파이프라인 변경

| 구분 | v0.9 | v1.0 |
|------|------|------|
| LLM 호출 횟수 | 최대 5회 | 1회 (함량 추출 시 추가 1회) |
| 파이프라인 구조 | 클래스 기반 14단계 | 함수 기반 9단계 |
| 성분명 매핑 | 임베딩 모델 + LLM cascade | ALIAS_HINTS + DB exact match + LLM 보조 |
| 처방 출력 | 최종 Backbone 1안 | 처방 3안 (A/B/C) |

### 추가

- `extract_query_info()` — LLM 없이 DB set + ALIAS_HINTS 기반 질의 분석
- `extract_amount_constraints()` — 숫자% 표현 있을 때만 LLM 호출해 함량 추출
- `map_ingredients()` — ALIAS_HINTS 키 → DB 성분명 확장
- `build_context()` — 유사 처방 그룹A(키워드 매칭)/그룹B(성분 매칭) 분리
- `call_llm()` — JSON 파싱 실패 시 최대 2회 재시도
- `validate_and_fix()` — 화이트리스트 exact match + 정제수 합계 보정
- Excel 저장: 처방 시트, 성분 통계, 키워드-성분 매핑, 설계 근거, 비용 내역, 프롬프트 시트 포함

### 제거

- `classify_formulation_type()` — LLM 처방 생성 프롬프트로 통합
- `select_structural_ingredients()` — LLM 처방 생성 프롬프트로 통합
- `review_active_properties()` — LLM 처방 생성 프롬프트로 통합
- `_apply_user_feel_conflicts()` — LLM 처방 생성 프롬프트로 통합
- `_fetch_replacements_for_flagged()` — LLM 처방 생성 프롬프트로 통합
- `FEEL_CONFLICT_MAP`, `FORMULATION_STRUCTURAL_MAP` 하드코딩 룰
- `sentence-transformers` / `faiss-cpu` 임베딩 의존성
- `input()` 기반 인터랙티브 흐름 (제형 타입 확인, 타겟 제품 선택)

---

## [v0.9] 2026-06-02 (legacy/)

단일 파일 `formulator_poc_v0.9.py`. 클래스 기반 14단계 파이프라인.
현재는 `legacy/` 폴더로 이동됨.

---

## [v0.8 이전] (legacy/)

초기 프로토타입. `legacy/` 폴더 참조.
