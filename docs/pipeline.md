# 파이프라인 명세

현재 `formulator_poc_v0.9.py`의 구현된 단계만 기록한다.

---

## 1. CLI 설정

| 항목 | 내용 |
|------|------|
| 함수 | `parse_args()` |
| 출력 | `CLIConfig` |

입력 옵션:
- `--data`
- `--product`
- `--external`
- `--query`
- `--aws_profile`
- `--aws_region`
- `--model`
- `--output`
- `--load_embeddings`
- `--allow_embedding_download`

---

## 2. 데이터 로드

| 항목 | 내용 |
|------|------|
| 함수 | `load_data_sources()` |
| 출력 | `formula`, `product`, `external`, `embedding`, `bedrock` |

세부 로더:
- `FormulaDataLoader`
- `ProductKeywordLoader`
- `ExternalProductLoader`
- `EmbeddingModelLoader`
- `BedrockClient`

---

## 3. 처방 DB 로드

| 항목 | 내용 |
|------|------|
| 클래스 | `FormulaDataLoader` |
| 입력 | `data.csv` |
| 출력 | `FormulaDataset` |

필수 컬럼:
- `bulk_code`
- `bulk_name`
- `ingredient_name`
- `ingredient_function`
- `content`

추가 확인:
- role 매핑에 없는 `ingredient_function`은 `unmapped_functions`에 기록
- 로드 요약에서 미매핑 기능명을 표시
- `ingredient_function`의 줄바꿈/중복 공백은 로드 시 정규화

---

## 4. 마케팅 키워드 로드

| 항목 | 내용 |
|------|------|
| 클래스 | `ProductKeywordLoader` |
| 입력 | `product.csv`, `FormulaDataset` |
| 출력 | `ProductKeywordDataset` |

필수 컬럼:
- `bulk_code`
- `marketing_keywords_list`
- `aspect_list`

---

## 5. 타사 제품 로드

| 항목 | 내용 |
|------|------|
| 클래스 | `ExternalProductLoader` |
| 입력 | `external.csv` |
| 출력 | `ExternalProductDataset` |

필수 컬럼:
- `title`
- `representation_ingredients`
- `base_time`

`representation_ingredients`는 `|` 기준으로 분리한다.

---

## 6. 질의 맥락 추출

| 항목 | 내용 |
|------|------|
| 클래스 | `QueryContextExtractor` |
| 입력 | `query` |
| 출력 | `QueryContext` |
| LLM 호출 | Bedrock Claude 1회 |

추출 슬롯:
- `ingredients`
- `formulation`
- `marketing_points`
- `target_product`
- `ingredient_constraints`

`ingredients` 구조:
```python
IngredientMention(
    name="나이아신아마이드",
    solubility="수용성",
    stable_ph_range="pH 5~7"
)
```

실패 시:
- 빈 `QueryContext` 반환
- 실행은 중단하지 않음

---

## 7. 분산계 판단

| 항목 | 내용 |
|------|------|
| 클래스 | `DispersionSystemExtractor` |
| 입력 | `query`, `QueryContext` |
| 출력 | `DispersionJudgement` |
| LLM 호출 | Bedrock Claude 1회 |

판단 후보:
- `수상 솔루션`
- `가용화`
- `O/W 유화`
- `W/O 유화`
- `분산`

검증 규칙:
- Claude의 허용 선택지 판단을 Python이 의미적으로 덮어쓰지 않는다.
- 판단과 추출 성분 물성이 어긋나 보이는 경우에도 원 판단을 유지하고 warning만 출력한다.
- 예: `가용화` 판단인데 추출 성분이 모두 수용성이면 `가용화 판단 근거 확인 필요` warning 출력

실패 시:
- `수상 솔루션` 반환

---

## 8. 제형 판단

| 항목 | 내용 |
|------|------|
| 클래스 | `ProductFormExtractor` |
| 입력 | `query`, `QueryContext`, `DispersionJudgement` |
| 출력 | `ProductFormJudgement` |
| LLM 호출 | Bedrock Claude 1회 |

출력 항목:
- 확정 제품 형상: `워터`, `젤(겔)`, `유액(밀크/로션)`, `오일`

검증 규칙:
- Claude의 허용 선택지 판단을 Python이 의미적으로 덮어쓰지 않는다.
- 제품 형상과 질의 표현/분산계가 어긋나 보이는 경우 판단은 유지하고 warning만 출력한다.
- 예: `워터` 판단인데 점성/젤/불투명 단서가 있으면 `워터 판단 근거 확인 필요` warning 출력

---

## 9. 세부 제형 판단

| 항목 | 내용 |
|------|------|
| 클래스 | `FormulationDetailExtractor` |
| 입력 | `query`, `QueryContext`, `DispersionJudgement`, `ProductFormJudgement` |
| 출력 | `FormulationDetailJudgement` |
| LLM 호출 | Bedrock Claude 1회 |

출력 항목:
- 목표 점도: `저점도`, `중점도`, `고점도`
- 목표 pH 범위: `산성`, `약산성`, `약산성~중성`, `중성`, `약알칼리성`

실패 시:
- `중점도 / 약산성~중성 (pH 5~7)` 반환

주의:
- 목표 제형 타입은 앞 단계의 `DispersionJudgement` 값을 그대로 사용
- 이 단계에서는 확정 제품 형상을 입력으로 받아 목표 점도, 목표 pH 범위만 판단
- Claude의 점도/pH 판단을 Python이 의미적으로 덮어쓰지 않는다.
- 점도와 질의 표현/제품 형상이 어긋나거나, 액티브 안정 pH와 목표 pH가 맞지 않는 경우 warning만 출력한다.

---

## 10. 제형 타입 및 목적 조립

| 항목 | 내용 |
|------|------|
| 함수 | `build_formulation_purpose()` |
| 입력 | `DispersionJudgement`, `ProductFormJudgement`, `FormulationDetailJudgement` |
| 출력 | `FormulationPurpose` |
| LLM 호출 | 없음 |

주의:
- 이 단계는 앞 단계 결과를 조립한 최종 요약본이다.
- 향후 처방 생성 프롬프트에는 중간 판단 로그가 아니라 이 요약본을 전달한다.

---

## 11. 구조 골격 설계

| 항목 | 내용 |
|------|------|
| 클래스 | `StructureSkeletonDesigner` |
| 입력 | `FormulationPurpose`, `FormulaDataset` |
| 출력 | `StructureSkeleton` |
| LLM 호출 | 없음 |

역할군 결정 기준:
- 목표 제형 타입으로 기본 역할군 결정
- 제품 형상으로 oil/water/rheology 역할 보정
- 목표 점도로 rheology/stabilizer 역할 보정
- pH 범위로 `ph_adjuster`, `buffer` 역할 보정

성분 후보 선정:
- `data.csv`에 있는 성분만 사용
- `ingredient_function`과 role 매핑 기준으로 후보 선정
- 성분명 힌트와 사용 빈도로 정렬
- 역할군별 후보는 기본 최대 10개 출력

---

## 12. Backbone 설계

| 항목 | 내용 |
|------|------|
| 클래스 | `BackboneDesigner` |
| 입력 | `FormulationPurpose`, `StructureSkeleton`, `FormulaDataset`, `QueryContext | None` |
| 출력 | `BackboneDesign` |
| LLM 호출 | Bedrock Claude 1회, 실패 시 rule fallback 사용 |

목적:
- 1,2단계에서 확정한 제형 목적과 구조 골격에 따라 가장 기본이 되는 처방 형태를 잡는다.
- role별 후보 목록이 아니라 실제 사용할 기본 성분과 기준 함량을 확정한다.
- 이 단계의 결과를 다음 액티브 적합성 검토의 기준 backbone으로 사용한다.

설계 규칙:
- LLM에 `FormulationPurpose`, `StructureSkeleton`, role별 후보 성분, 후보 성분 통계, 유사 처방/co-occurrence 요약을 제공한다.
- LLM에 사용자 질의의 액티브 이름, 용해성, 안정 pH, 지정 함량을 참고 정보로 제공한다.
- LLM은 role을 기계적으로 채우지 않고 실제 제형이 성립하는 최소 구조 성분과 함량 조합을 설계한다.
- `StructureSkeleton`의 required role은 자동 포함 대상이 아니라 우선 검토 대상이다.
- 각 role은 물리적 안정성, 미생물 안정성, 사용감, 외관, 제조 가능성에 실제로 기여하는 경우에만 포함한다.
- `buffer`, `ph_adjuster`, `stabilizer`, `rheology_modifier`, `solubilizer`는 role 이름이 있다는 이유만으로 자동 포함하지 않는다.
- role을 포함할 때는 `ingredients.reason`, 제외할 때는 `excluded_roles.reason`에 이유를 남긴다.
- Python은 LLM 출력의 `ingredient_name`이 해당 role의 `CandidateIngredients`에 있는지 검증한다.
- Python은 LLM이 액티브 성분을 Backbone 성분으로 출력하면 제거하고 warning을 남긴다.
- Python은 `backbone_type`, `system_checks`, 총합 100%를 검증하고 q.s. 성분으로 총합을 보정한다.
- Python은 포함/제외 이유가 누락된 경우 warning을 남긴다.
- LLM 호출 실패, JSON 파싱 실패, 유효 성분 없음, 총합 보정 실패 시 기존 rule 기반 설계로 fallback한다.

주의:
- 액티브 성분을 확정하거나 사용자 지정 함량을 변경하지 않는다.
- 액티브 성분 자체는 포함하지 않지만, 이후 투입 시 예상되는 용해성, pH, 안정성 요구사항은 Backbone 방향성 판단에 참고할 수 있다.
- 최종 처방 3안 생성, 액티브 투입 후 정제수 재계산, 규제/DB 상한 보정은 아직 수행하지 않는다.
- 연속상 후보가 없으면 억지로 100%에 맞추지 않고 warning을 남긴다.
- 후보에 없는 성분명, 일반명, 임의 번역명은 통과시키지 않는다.

---

## 13. 액티브 적합성 및 Backbone 수정 전략

| 항목 | 내용 |
|------|------|
| 클래스 | `ActiveSuitabilityReviewer`, `BackboneRevisionPlanner` |
| 입력 | `QueryContext`, `FormulationPurpose`, `StructureSkeleton`, `BackboneDesign` |
| 출력 | `list[ActiveSuitabilityReview]`, `BackboneRevision | None` |
| LLM 호출 | Bedrock Claude 최대 2회, 실패 시 rule baseline 사용 |

이 단계는 액티브별 적합성 검토와, 수정 필요 시 Backbone 수정 전략 정리를 하나의 판단 흐름으로 처리한다.
내부 구현은 두 클래스로 나뉘지만 파이프라인상 독립된 처방 설계 단계로 분리하지 않는다.

### 액티브 적합성 검토

클래스:
- `ActiveSuitabilityReviewer`

입력:
- `QueryContext`
- `FormulationPurpose`
- `StructureSkeleton`
- `BackboneDesign`

출력:
- `list[ActiveSuitabilityReview]`

검토 항목:
- 용해성 적합성: 액티브 용해성과 목표 분산계의 정합성
- pH 적합성: 액티브 안정 pH가 목표 pH를 완전히 포함하는지 여부
- 제형 영향: 점도, 투명도, 침전/분리, 산화 안정성 영향
- 액티브 농도 영향: 지정 함량, 검토 포인트
- Backbone 수정 필요 여부

주의:
- 사용자 지정 함량은 보정하지 않고 영향 검토에만 사용한다.
- 안정 pH 또는 용해성 정보가 없으면 `확인 필요`로 남긴다.
- 안정 pH 정보가 파싱되지 않으면 `PH_UNKNOWN`과 함께 안정 pH 재확인 설계 조건을 남긴다.
- pH는 완전 포함이면 `적합`, 일부만 겹치면 `조건부 적합`, 겹치지 않으면 `부적합`으로 판단한다.
- 제형 영향은 `condition`, `impact_type`, `impact_level`, `review_point` 구조로 관리한다.
- 농도 영향은 `condition`, `review_type`, `review_point` 구조로 관리한다.
- 현재 단계에서는 일반 사용 범위와 함량 적합성을 출력하지 않고, 지정 함량 유무에 따른 검토 포인트만 남긴다.
- 고정 함량 임계값만으로 역할군을 자동 추가하지 않는다.
- 수정 필요 시 `issue_codes`, `backbone_actions`, `required_backbone_changes`를 같은 단계의 Backbone 수정 전략 정리에 제공한다.
- `required_backbone_changes`는 표시용 문구이며, Backbone 수정 전략은 `BackboneAction` 구조체를 기준으로 한다.
- LLM 응답은 허용된 `issue_codes`, `action_type`, `role`, `target_value`만 통과시킨다.
- LLM은 `OXIDATION_RISK`, `TRANSPARENCY_RISK`, `PRECIPITATION_RISK`, `SENSORY_RISK`, `PHOTO_STABILITY_RISK` 같은 성분 맥락 리스크를 보정할 수 있다.
- 허용 `action_type`은 `add_role`, `add_design_constraint`, `change_ph_range`, `change_formulation_type`, `review_system`이다.
- `add_design_constraint`는 특정 후보나 우선순위가 아니라 Backbone 수정 전략의 설계 제약조건으로 직접 전달된다.

### Backbone 수정 전략 정리

클래스:
- `BackboneRevisionPlanner`

입력:
- `FormulationPurpose`
- `StructureSkeleton`
- `list[ActiveSuitabilityReview]`
- `FormulaDataset | None` (현재 미사용)

출력:
- `BackboneRevision | None`

실행 조건:
- `ActiveSuitabilityReview.backbone_modification_required`가 하나라도 `True`일 때만 실행

출력 항목:
- 설계 제약조건
- 추가 역할군
- 수정 근거 이슈
- 내부 추적용 source action

주의:
- 원본 `FormulationPurpose`와 `StructureSkeleton`을 자동 변경하지 않는다.
- 이 하위 처리는 무엇을 써야 하는지가 아니라 어떤 조건을 만족해야 하는지를 정의한다.
- 후보 성분 선정과 role 우선순위 확정은 다음 단계인 `최종 Backbone 확정`에서 처리한다.
- 문자열 키워드 매칭이 아니라 `BackboneAction.action_type`, `role`, `target_value`, `reason_code`를 설계 제약조건으로 변환한다.
- `backbone_actions`의 `add_role`은 반드시 추가하라는 명령이 아니라 검토 후보로 해석한다.
- `added_required_roles`에는 현재 제형 목적상 최종 Backbone 구조에 반드시 필요한 role만 넣는다.
- 검토 필요, 실측 후 판단, 조건부 적용, 낮은 우선순위 role은 `added_required_roles`에 넣지 않고 `design_constraints`에만 남긴다.
- pH, 점도, 산화, 외관, 사용감, 고농도 이슈는 기본적으로 role 추가가 아니라 실측/검토/설계 조건으로 정리한다.
- pH 우려는 `buffer` 추가 확정보다 pH control strategy 검토로, 점도 우려는 `rheology_modifier` 추가 확정보다 목표 점도 실측 확인으로 우선 정리한다.
- 산화 안정성 우려는 `antioxidant_support` 추가 확정보다 산화 안정성 확보 전략 검토로 우선 정리한다.
- 목표 제형 타입, 제품 형상, 목표 점도, 목표 pH와 충돌하는 role은 `added_required_roles`로 승격하지 않는다.
- `review_system` 액션은 역할군을 바로 추가하지 않고 관련 시스템 검토가 필요하다는 뜻이다.
- `note`는 해당 action이 필요한 이유이며, `review_system`에서는 설계 제약조건 문구로 우선 사용한다.
- `rationale`은 액티브 적합성 검토의 `issues`를 모아 중복 제거한 단순 집계 결과다.
- `source_actions`는 사용자 출력보다 추적과 LLM 검증을 위한 내부 근거값이다.
- `add_design_constraint` 액션은 `target_value` 또는 `note`를 설계 제약조건 문장으로 반영한다.
- `change_ph_range` 액션은 "목표 pH를 지정 범위로 조정 필요"로 표현한다.
- `change_formulation_type` 액션은 "지정 제형 타입 기반 Backbone 조건 검토"로 표현한다.
- LLM은 설계 제약조건 문장 정리에만 관여하며 후보 원료명과 배합비를 제안하지 않는다.

---

## 14. 최종 Backbone 확정

| 항목 | 내용 |
|------|------|
| 클래스 | `FinalBackboneFinalizer` |
| 입력 | `FormulationPurpose`, `StructureSkeleton`, `BackboneDesign`, `list[ActiveSuitabilityReview]`, `BackboneRevision | None`, `FormulaDataset` |
| 출력 | `FinalBackboneResult` |
| LLM 호출 | Bedrock Claude 1회, 실패 시 기본 Backbone fallback 사용 |

목적:
- 기본 Backbone과 액티브 적합성/수정 전략을 반영해 연구원이 실험 가능한 최종 Backbone 처방표를 확정한다.
- v0.8의 처방표 구조를 참고해 `name`, `concept`, `target_aspects`, `ingredients`, `design_rationale`를 출력한다.

LLM 입력:
- `FormulationPurpose`
- 현재 `BackboneDesign`
- 액티브 검토 요약
- `BackboneRevision`의 설계 제약조건/수정 근거
- 관련 role의 후보 성분과 성분 통계

검증 및 후처리:
- 성분명은 `data.csv` 후보 성분만 통과시킨다.
- 액티브 성분은 최종 Backbone 성분표에서 제외한다.
- 지정 액티브 함량은 성분표에 넣지 않지만 `reserved_actives`로 관리하고 q.s. 계산에서 차감한다.
- q.s. 연속상 성분을 찾아 `100 - 고정 Backbone 성분 합 - 지정 액티브 예약량`으로 함량을 재계산한다.
- q.s. 함량이 음수가 되면 최종 확정 실패로 처리하고 `[3. Backbone 설계]` 재설계 피드백을 남긴다.
- 총합은 `Backbone 성분 소계 + 지정 액티브 예약량 = 100.00%`로 Python에서 검증한다.
- 최종 성분표는 함량 내림차순으로 정렬한다.

---

## 15. 현재 출력

| 항목 | 내용 |
|------|------|
| 함수 | `print_load_summary()`, `print_query_context_summary()`, `print_dispersion_judgement_summary()`, `print_product_form_judgement_summary()`, `print_formulation_detail_summary()`, `print_formulation_purpose_summary()`, `print_structure_skeleton_summary()`, `print_backbone_design_summary()`, `print_active_suitability_review_summary()`, `print_backbone_revision_summary()`, `print_final_backbone_summary()` |
| 출력 | 데이터 로드 요약, 질의 추출 요약, 분산계 판단, 제형 판단, 세부 제형 판단, 제형 타입 및 목적, 구조 골격, 기본 Backbone 설계, 액티브 적합성 및 Backbone 수정 전략, 최종 Backbone 확정 |
