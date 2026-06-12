# 후처리 모듈 — postprocess.py

`validate_and_fix()` 함수 상세 문서.

---

## 역할

LLM이 생성한 처방 JSON을 두 단계로 보정한다.
1. 화이트리스트 검증 — DB에 없는 성분 제거
2. 합계 보정 — 정제수 함량으로 100% 맞춤

---

## 함수 시그니처

```python
def validate_and_fix(
    formula_data: dict,
    stats: dict,
    known_set: set[str] | None = None,
    user_constraints: dict[str, float] | None = None,
) -> dict
```

---

## 처리 흐름

### 1단계 — 화이트리스트 검증

각 처방의 `ingredients` 리스트를 순회한다.

```
성분명 in known_set?
  → Yes: 그대로 통과
  → No:  정규화 exact match 시도
           (_norm_name: 공백·하이픈·특수문자 제거 + 소문자화)
           → 매칭 성공: 성분명을 known_set의 정식 표기로 교체
           → 매칭 실패: 해당 성분 조용히 제거
```

**중요**: 제거 시 경고 메시지를 출력하지 않는다. LLM이 할루시네이션으로 생성한 성분명은 조용히 제거하는 것이 의도된 동작이다.

### 2단계 — 합계 100% 보정

```
합계 계산 → |합계 - 100.0| > 0.05?
  → No:  보정 불필요, 통과
  → Yes: 정제수 성분 탐색
           ("정제수" 포함 또는 "water" 포함, 대소문자 무관)
           → 정제수 있음:
               정제수가 user_constraints에 포함됐는가?
                 → Yes: 보정 건너뜀 (R1)
                 → No:  정제수 함량 += (100 - 현재합계)
                          음수가 되면 경고 출력, 보정 포기
           → 정제수 없음: 경고 출력, 보정 불가
```

---

## 핵심 불변 조건

- **R1 보호**: `user_constraints`에 있는 성분은 어떤 보정도 적용하지 않는다.
  `_norm_name()` 정규화 후 비교하므로 표기가 조금 달라도 매칭된다.
- **R2 보장**: `known_set`이 전달되면 반드시 검증한다. `known_set=None`이면 검증을 건너뛰는 코드 경로가 있으나, 운영 환경에서는 항상 `known_set`을 전달해야 한다.

---

## 입출력 예시

입력 (`formula_data` 일부):
```python
{
  "formulas": [{
    "name": "Formula A",
    "ingredients": [
      {"name": "정제수", "content": 77.5, "role": "기본 용제"},
      {"name": "나이아신아마이드", "content": 10.0, "role": "미백"},
      {"name": "존재하지않는성분", "content": 2.0, "role": "?"},  # 제거됨
      {"name": "글리세린", "content": 5.0, "role": "보습"},
    ]
  }]
}
```

출력 (정제수 보정 + 미등록 성분 제거):
```python
{
  "formulas": [{
    "name": "Formula A",
    "ingredients": [
      {"name": "정제수", "content": 79.5, "role": "기본 용제"},  # 77.5 + 2.0 보정
      {"name": "나이아신아마이드", "content": 10.0, "role": "미백"},
      {"name": "글리세린", "content": 5.0, "role": "보습"},
    ]
  }]
}
```
