# 매핑 모듈 — query.py

성분명 매핑 관련 함수 상세 문서.

---

## 역할

사용자 질의에서 추출한 성분 표현을 `data.csv`의 정식 성분명으로 변환한다.

---

## 함수 구성

### extract_query_info()

질의 텍스트에서 성분·제형 힌트·마케팅 힌트를 추출한다. LLM 없음.

**성분 추출 방법 (2단계)**:

1. **DB set 직접 매칭**: `known_set`의 모든 성분명(2자 이상)을 질의에서 substring 탐색
2. **ALIAS_HINTS 키 매칭**: `config.py`의 관용명 사전 키를 대소문자 무관 매칭

두 결과를 합쳐 `ingredient_map`으로 직접 반환한다.

**출력 예시**:
```python
{
  "나이아신아마이드": ["나이아신아마이드"],   # DB 직접 매칭
  "시카":            ["병풀추출물", "병풀잎추출물"],  # ALIAS_HINTS 확장
}
```

**다중 매핑 주의**:
ALIAS_HINTS 값이 여러 DB 성분명으로 확장될 때, `run_pipeline()`에서 `user_constraints`를 구성할 때 **모든 후보에 동일한 사용자 지정 함량을 적용**한다. LLM이 어느 후보를 최종 선택하든 R1 보호가 동작하기 위함이다.

---

### extract_amount_constraints()

질의에 숫자% 표현이 있을 때만 LLM을 호출해 성분-함량 연결을 추출한다.

```python
def extract_amount_constraints(
    query: str,
    ingredient_names: list[str],   # ingredient_map.keys() — 질의 표현 그대로
    bedrock_client: Any,
    model_id: str,
) -> dict[str, float]
```

**LLM 프롬프트 구성 (3섹션)**:

Python이 먼저 `re.findall(r'\d+(?:\.\d+)?(?=\s*%)', query)`로 함량 값을 추출한 뒤, LLM에 세 가지를 함께 전달한다.

```
[질의]            — 전체 질의 원문 (문맥용)
[추출된 성분]     — ingredient_names 리스트 (Python 추출)
[추출된 함량]     — 숫자% 리스트 (Python regex 추출)
```

LLM은 세 섹션을 함께 보고 각 성분에 해당하는 함량을 연결한다.
전체 질의를 스스로 파싱하는 것보다 안정적이다.

**빠른 탈출 조건** (LLM 호출 없이 `{}` 반환):
- regex 추출 함량 목록이 비어 있음
- `ingredient_names`가 비어 있음
- `bedrock_client`가 None

**검증**: LLM 결과에서 `ingredient_names`에 없는 성분명, 0 이하 값은 제거한다.

**실패 시**: 예외를 잡아 경고 출력 후 `{}` 반환. 파이프라인 계속 진행.
