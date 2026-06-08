# 매핑 모듈

현재 새 `formulator_poc_v0.9.py`에는 성분명 DB 매핑 모듈이 아직 구현되지 않았다.

---

## 현재 있는 관련 기능

| 구성 | 상태 |
|------|------|
| `EmbeddingModelLoader` | 구현됨 |
| `ALIAS_HINTS` | 구현됨 |
| `QueryContextExtractor`의 성분 슬롯 추출 | 구현됨 |

---

## 다음 구현 예정

- 질의 성분 표현을 `data.csv` 성분명으로 매핑
- exact match
- 임베딩 유사도 검색
- Claude 보조 매핑
- DB 화이트리스트 검증
- 타사 제품 대표 성분 정규화
