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
- `external.csv` — 타사 제품 전성분 DB (v1.0 미사용, 호환성 유지)
- LLM 인프라: AWS Bedrock (`bedrock-runtime`)

---

## 파이프라인 전체 흐름

`run_pipeline()` 내부 실행 순서와 정확히 일치한다.
**한 단계의 출력이 다음 단계의 입력이므로, 어떤 단계를 수정해도 이 흐름이 유지되어야 한다.**

```
query (str)
  │
  ▼ [1]  load_formula_data()              → df, formula_dict
  ▼ [2]  build_stats()                   → stats, known_set
  ▼ [3]  load_product_data()             → keyword_db
  ▼ [3b] load_external_data()            → external_db
  ▼ [4]  extract_query_info()            → query_info, ingredient_map (성분·마케팅힌트)
  ▼ [4b] extract_query_supplements()     → supplements (target_product·amounts·keywords·additional_ings)
         └─ search_target_product()      → target_product (Python, LLM 없음)
  ▼ [6]  build_context()                 → ctx (dict)
  ▼ [7]  call_llm() + build_user_prompt() → formula_data (dict)
  ▼ [8]  validate_and_fix()              → formula_data (보정됨)
  ▼ [9]  print_results() + save_results() → 출력 및 저장
```

---

## 모듈 구조

각 파일의 역할 경계를 지킨다. 한 모듈이 다른 모듈의 역할을 침범하지 않는다.

| 파일 | 역할 |
|------|------|
| `config.py` | 상수 전용. 다른 모듈을 import하지 않는다 |
| `utils.py` | 공통 유틸리티: console, Bedrock 페이로드 헬퍼, 포매팅 헬퍼. `config.py`만 import한다 |
| `data.py` | CSV 로드 + 통계 계산 |
| `query.py` | 질의 텍스트 분석 + 성분명 매핑 + 질의 보완 LLM 호출 |
| `context.py` | 유사 처방 탐색 + 컨텍스트 조립 (LLM 없음) |
| `llm.py` | Bedrock 클라이언트 생성 + LLM 호출 |
| `prompt.py` | SYSTEM_PROMPT 상수 + `build_user_prompt()` |
| `postprocess.py` | LLM 출력 검증 + 합계 보정 + LLM 대체 성분 탐색 |
| `output.py` | 콘솔 출력 + 파일 저장 + 비용 계산 |
| `pipeline.py` | `run_pipeline()` — 전체 흐름 오케스트레이터 |
| `main.py` | CLI 진입점 |

---

## 개발 규칙

이 규칙들의 목적은 하나다: **작업 과정을 누구나 따라갈 수 있게 한다.**

### 주석 규칙

새로 추가하는 코드에만 적용한다. 기존 코드는 건드리지 않는다.

- **새 함수**: `def` 바로 위에 `#` 한 줄 주석으로 해당 함수가 하는 일을 설명한다.
- **새 파일**: 파일 맨 위 docstring 첫 줄에 해당 파일의 역할을 한 줄로 명시한다.

```python
# 예시 — 새 함수
# 질의에서 추출한 성분명을 DB 성분명과 대조해 유효한 것만 반환
def filter_valid_ingredients(names: list[str], known_set: set[str]) -> list[str]:
    ...
```

### 작업 원칙

**① 코딩 전에 먼저 말한다**
수정을 시작하기 전에 아래 세 가지를 한 문장으로 먼저 말한다.
- 어떤 파일의 어떤 함수를
- 어떻게 바꾸는지
- 왜 바꾸는지

예: "`postprocess.py`의 `validate_and_fix()`에서 정제수 보정 조건을 수정합니다. 현재는 정제수가 없을 때 조용히 넘어가는데, 경고를 출력하도록 바꿉니다."

**② 요청 범위 밖은 건드리지 않는다**
- 고치는 김에 하는 정리, 리팩토링, 개선은 하지 않는다.
- 요청과 관계없는 파일은 열지도 않는다.
- 범위를 벗어나야 할 것 같으면 먼저 말하고 확인받는다.

**③ 완료 기준을 미리 말한다**
작업 전에 "이렇게 되면 완료"를 한 줄로 명시한다.
예: "함량 합계가 100%가 아닐 때 콘솔에 경고 메시지가 출력되면 완료."

**④ 작업 단위를 섞지 않는다**
관련 없는 변경은 한 번의 응답에 묶지 않는다.
여러 파일을 바꿔야 할 때는 순서와 이유를 먼저 나열한 뒤 진행한다.

### 소통 규칙

**변경 전**: 파일명과 함수명을 명시하고, 무엇을 왜 바꾸는지 말한다.
**변경 후**: 바뀐 내용을 한 문장으로 요약하고, 확인 방법을 안내한다.

확인 방법 안내 예시:
```
터미널에서 아래 명령 실행 후 output/ 폴더에 파일이 생성되면 정상입니다.
python -m formulator --data data.csv --product product.csv --query "테스트 질의"
```

전문 용어를 처음 사용할 때는 괄호 안에 한 줄 설명을 붙인다.
예: `ctx` (파이프라인 각 단계의 결과물을 모아둔 딕셔너리), `known_set` (data.csv 전체 성분명 집합)

---

## 핵심 불변 규칙 (R1~R5)

기능 추가, 리팩토링, 어떤 변경에서도 반드시 유지한다.

### R1. 사용자 지정 함량은 시스템이 덮어쓰지 않는다

`validate_and_fix()`에서 `user_constraints`에 포함된 성분은 함량 보정을 건너뛴다(`continue`).
DB 범위를 벗어나도 보정하지 않고 경고 출력만 한다 (미구현 항목 001).

### R2. DB 화이트리스트 검증은 Python이 담당한다

LLM이 제안한 성분명은 반드시 `known_set`(data.csv 전체 성분명)과 교차 검증 후 확정한다.
이 검증을 우회하거나 생략하는 코드 경로를 만들지 않는다.
적용 위치: `validate_and_fix()` 1단계 / `extract_query_info()` DB 교차 검증

### R3. LLM 호출에는 반드시 예외 처리와 폴백이 있어야 한다

모든 LLM 호출 함수: `try/except` + 실패 시 빈 결과(`{}`, `[]`) 반환 + 파이프라인 계속 실행.

### R4. 파이프라인 단계 간 인터페이스를 변경하면 연쇄 영향을 확인한다

한 단계의 출력 구조(dict key, list 형태)가 바뀌면, 그 출력을 소비하는 모든 하위 단계를 함께 수정한다.
단계별 입출력 명세는 `docs/pipeline.md` 참조.

### R5. 버전 관리

- **버그 픽스, 소규모 튜닝**: 해당 모듈 파일 직접 수정.
- **새 기능 추가 (신규 버전)**: `docs/changelog.md`와 `docs/logic_overview.md`를 반드시 업데이트한다.
- **파이프라인 단계 추가·제거**: `docs/pipeline.md`와 이 파일의 파이프라인 흐름 다이어그램도 업데이트한다.
- **버전 번호 변경 시 반드시 함께 수정할 위치**:
  - `pipeline.py` — `console.rule("... PoC vX.X ...")` (터미널 시작 로그)
  - `main.py` — `parser = argparse.ArgumentParser(description="... PoC vX.X ...")`
  - `docs/changelog.md`, `docs/logic_overview.md`, `docs/pipeline.md` 버전 표기

---

## 판단 기준 — 먼저 물어볼 상황 vs 바로 진행할 상황

### 먼저 물어본다
- 새 파이프라인 단계를 추가하거나 기존 단계를 제거할 때
- `SYSTEM_PROMPT`의 JSON 출력 스키마를 변경할 때
- R1 또는 R2 로직에 영향을 주는 수정일 때
- 파이프라인 단계 실행 순서를 변경할 때

### 바로 진행한다
- `build_user_prompt()` 내용 수정 (텍스트, 로직 개선)
- 버그 픽스, 예외 처리 추가, 폴백 보완
- `docs/` 문서 업데이트
- `ALIAS_HINTS` 등 `config.py` 상수 값 수정

---

## 변경 시 체크리스트

```
[ 작업 전 ]
  [ ] 어느 파이프라인 단계를 건드리는가?
  [ ] 출력 구조가 바뀌는가? → 하위 단계 연쇄 영향 확인 (R4)
  [ ] R1·R2 로직에 영향을 주는가? → 먼저 물어보기

[ 작업 후 ]
  [ ] docs/logic_overview.md 업데이트 (단계 로직·데이터 구조·파일 구조 변경 시 필수)
  [ ] docs/changelog.md 업데이트 (모든 기능 추가·변경 시)
  [ ] 이 파일의 파이프라인 흐름 다이어그램 업데이트 (단계 추가·제거 시)
```

---

## 비자명한 제약

코드만 봐서는 파악하기 어려운, 의도적으로 설계된 제약들.

| 제약 | 적용 위치 | 이유 |
|------|-----------|------|
| `SYSTEM_PROMPT` 스키마 변경 시 `validate_and_fix()` 파싱 로직도 함께 수정 | `llm.py`, `postprocess.py` | LLM 출력 구조와 파싱 로직이 1:1 연동됨 |
| 개념어→DB 성분명 다중 매핑 시 모든 후보에 동일 사용자 지정 함량 적용 | `run_pipeline()` 내 `user_constraints` 구성 | LLM이 어느 후보를 선택하든 R1 보호가 작동해야 함 |

---

## 미구현 항목

| ID | 항목 | 위치 | 내용 |
|----|------|------|------|
| 001 | 사용자 지정 함량 DB 범위 경고 | `validate_and_fix()` 이후 | 보정 없이 "살리실릭애씨드 0.5% — DB 선례 없음 (DB max: 0.2%)" 형태로 출력 |

---

## 문서 지도

| 문서 | 목적 | 업데이트 시점 |
|------|------|--------------|
| `docs/logic_overview.md` | 단계별 로직(WHY/HOW) + 파일 구조 + 중간 데이터 구조 | **기능 추가·변경 시 필수** |
| `docs/changelog.md` | 버전별 변경 이력 | **기능 추가·변경 시 필수** |
| `docs/pipeline.md` | 단계별 입출력 타입, 데이터 구조 명세 | 단계 추가·변경 시 |
| `docs/modules/postprocess.md` | `validate_and_fix()` 로직 상세 | postprocess 변경 시 |
| `docs/modules/mapping.md` | `map_ingredients()`, `extract_amount_constraints()` 로직 상세 | 매핑 로직 변경 시 |
