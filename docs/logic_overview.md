# 로직 개요

이 문서는 현재 새로 작성 중인 `formulator_poc_v0.9.py` 기준이다.
구현 범위는 아직 데이터 입력, 사용자 질의 추출, 분산계 판단, 제형 판단, 세부 제형 판단, 구조 골격 설계, 기본 Backbone 설계, 액티브 적합성 및 Backbone 수정 전략, 최종 Backbone 확정까지다.

---

## 목적

사용자 자연어 질의를 받아 화장품 처방 생성을 위한 입력 데이터를 구조화한다.
최종 목표는 v0.8과 동일하게 처방 3안을 생성하는 것이지만, v0.9는 코드를 새 구조로 다시 작성한다.

---

## 현재 구현 범위

```text
CLI 입력
→ data.csv 로드
→ product.csv 로드
→ external.csv 로드
→ 성분 통계 생성
→ 선택적으로 임베딩 인덱스 로드
→ 사용자 질의 슬롯 추출
→ 분산계 판단
→ 제형 판단
→ 세부 제형 판단
→ 제형 타입 및 목적 조립
→ 구조 골격 설계
→ 기본 Backbone 설계
→ 액티브 적합성 및 Backbone 수정 전략
→ 최종 Backbone 확정
→ 로드/추출 결과 요약 출력
```

---

## 주요 구성

| 구성 | 역할 |
|------|------|
| `CLIConfig` | CLI 입력값 보관 |
| `FormulaDataLoader` | 자사 처방 DB 로드 및 성분 통계 생성 |
| `ProductKeywordLoader` | 마케팅 키워드와 처방 성분 연결 |
| `ExternalProductLoader` | 타사 제품명과 대표 성분 로드 |
| `EmbeddingModelLoader` | 성분명 임베딩 인덱스 준비 |
| `BedrockClient` | AWS Bedrock Claude 호출 래퍼 |
| `QueryContextExtractor` | 사용자 질의를 설계 슬롯으로 추출 |
| `DispersionSystemExtractor` | 수상 솔루션/가용화/유화/분산 계열 먼저 판단 |
| `ProductFormExtractor` | 확정 분산계와 질의 맥락을 받아 확정 제품 형상 판단 |
| `FormulationDetailExtractor` | 질의 맥락/분산계/확정 제품 형상을 받아 점도와 pH 판단 |
| `StructureSkeletonDesigner` | 제형 목적 기반 role 결정 및 DB 후보 성분 선정 |
| `BackboneDesigner` | 구조 골격 후보에서 실제 기본 Backbone 성분과 함량을 확정 |
| `ActiveSuitabilityReviewer` | 기본 Backbone을 참고해 질의 액티브의 용해성, pH, 제형 영향, Backbone 수정 필요 여부 검토 |
| `BackboneRevisionPlanner` | 액티브 검토의 하위 전략 정리기로, rule baseline과 LLM 검토를 결합해 설계 제약조건과 추가 역할군 정의 |
| `FinalBackboneFinalizer` | 기본 Backbone과 수정 전략을 반영해 최종 Backbone 처방표를 확정하고 총합/q.s.를 검증 |

---

## 데이터 구조

### `FormulaDataset`

```python
FormulaDataset(
    raw=DataFrame,
    formulas={
        "bulk_code": {
            "name": "벌크명",
            "ingredients": {"성분명": 함량},
            "structural_roles": {"성분명": "base"}
        }
    },
    ingredient_stats={
        "성분명": {
            "frequency": 0.5,
            "count": 10,
            "min": 0.1,
            "max": 5.0,
            "median": 1.2
        }
    }
)
```

`FormulaDataset.unmapped_functions`에는 role 매핑에 연결되지 않은 `ingredient_function` 값이 들어간다.
새 기능명이 data.csv에 추가되면 이 목록을 보고 role 매핑 추가 여부를 판단한다.

### `QueryContext`

```python
QueryContext(
    ingredients=[
        IngredientMention(
            name="어성초",
            solubility="수용성",
            stable_ph_range="불명"
        )
    ],
    formulation=["진정 에센스"],
    marketing_points=["진정"],
    target_product=[],
    ingredient_constraints=[
        IngredientConstraint(ingredient="어성초", amount=70.0)
    ]
)
```

### `DispersionJudgement`

```python
DispersionJudgement(
    dispersion_system="수상 솔루션",
    reason="수용성 성분 중심의 투명 제형",
    warnings=[]
)
```

### `FormulationPurpose`

### `ProductFormJudgement`

```python
ProductFormJudgement(
    product_form="젤(겔)",
    reason="젤타입 표현",
    warnings=[]
)
```

### `FormulationDetailJudgement`

```python
FormulationDetailJudgement(
    target_viscosity="저점도",
    ph_range_label="약산성",
    ph_min=4.5,
    ph_max=6.0,
    reason="끈적이지 않는 사용감과 나이아신아마이드 안정 pH",
    warnings=[]
)
```

### `FormulationPurpose`

```python
FormulationPurpose(
    product_form="워터",
    formulation_type="수상 솔루션",
    target_viscosity="저점도",
    ph_range_label="약산성~중성",
    ph_min=5.0,
    ph_max=7.0,
    reason="투명한 에센스 사용감 요구"
)
```

### `StructureSkeleton`

```python
StructureSkeleton(
    formulation_type="가용화",
    role_status={
        "water_phase": "required",
        "humectant": "required",
        "solubilizer": "required"
    },
    structure={
        "water_phase": ["정제수", "글리세린"],
        "humectant": ["글리세린", "부틸렌글라이콜"],
        "solubilizer": ["폴리솔베이트20"]
    },
    warnings=[]
)
```

### `BackboneDesign`

```python
BackboneDesign(
    backbone_type="가용화",
    design_summary="투명 수상 제형에 필요한 보습, 보존, 최소 가용화 구조 중심 설계",
    formulation_type="가용화",
    product_form="워터",
    continuous_phase_role="water_phase",
    ingredients=[
        BackboneIngredient(
            role="water_phase",
            name="정제수",
            amount=91.75,
            amount_note="100% q.s.",
            role_status="required"
        ),
        BackboneIngredient(
            role="humectant",
            name="글리세린",
            amount=5.0,
            amount_note="DB median 기반 role 기본값",
            role_status="required"
        )
    ],
    role_ingredients={"water_phase": ["정제수"], "humectant": ["글리세린"]},
    excluded_roles=[
        BackboneExcludedRole(role="buffer", reason="액티브 투입 전 산/염기 유지 조건이 없어 제외")
    ],
    system_checks={
        "preservation_system": "적정",
        "rheology_system": "불필요",
        "solubilization_system": "적정",
        "emulsification_system": "불필요",
        "ph_system": "불필요"
    },
    total_content=100.0,
    warnings=[]
)
```

`BackboneDesign`은 1,2단계에서 확정한 제형 목적과 구조 골격에 따라 가장 기본이 되는 처방 형태를 잡는다.
성분 후보 목록만 제공하는 `StructureSkeleton`과 달리 실제 사용할 성분과 기준 함량을 포함한다.
정상 경로에서는 Bedrock Claude가 role별 후보, 성분 통계, 유사 처방/co-occurrence 요약을 보고 설계한다.
사용자 질의의 액티브 성분명, 용해성, 안정 pH, 지정 함량은 참고 정보로 전달되지만,
액티브 성분 자체는 Backbone 성분으로 포함하지 않는다.
다만 이후 액티브 투입 시 예상되는 용해성, pH, 안정성 요구사항은 pH 시스템,
용매/가용화 방향, 점증/안정화 방향을 정할 때 참고할 수 있다.
`StructureSkeleton`의 required role도 자동 포함하지 않고, 물리적 안정성, 미생물 안정성, 사용감, 외관,
제조 가능성에 실제로 기여하는지 평가한 뒤 포함 여부를 결정한다.
`buffer`, `ph_adjuster`, `stabilizer`, `rheology_modifier`, `solubilizer`는 role 이름이 있다는 이유만으로 자동 포함하지 않는다.
포함 role과 제외 role에는 각각 필요 이유와 제외 이유가 있어야 하며, 누락 시 warning을 남긴다.
Python은 LLM 출력의 성분명이 role별 후보에 포함되는지, enum 값이 허용되는지, 총합이 100%인지 검증한다.
LLM이 액티브 성분을 Backbone 성분으로 출력하면 Python이 제거하고 warning을 남긴다.
LLM 호출 실패 또는 검증 실패 시에는 rule 기반 fallback을 사용한다.
연속상 역할군(`water_phase`, `dispersion_medium`, `oil_phase`)은 나머지 고정 성분 합을 제외한 100% q.s.로 계산하거나 보정한다.
이 단계는 액티브 성분을 확정하거나 사용자 지정 함량을 변경하지 않는다.
액티브 적합성 및 Backbone 수정 전략은 이 기본 Backbone을 참고해 진행한다.

### `ActiveSuitabilityReview`

```python
ActiveSuitabilityReview(
    ingredient="나이아신아마이드",
    amount=10.0,
    solubility="수용성",
    stable_ph_range="pH 5~7",
    solubility_fit="적합",
    ph_fit="적합",
    formulation_effects=[
        FormulationEffect(
            condition="수상 솔루션 + 수용성",
            impact_type="투명도",
            impact_level="낮음",
            review_point="투명도 영향 없음"
        ),
        FormulationEffect(
            condition="수상 솔루션 + 수용성",
            impact_type="침전",
            impact_level="낮음",
            review_point="침전 위험 낮음"
        )
    ],
    concentration_review_points=[
        ConcentrationReviewPoint(
            condition="지정 함량 10%",
            review_type="일반",
            review_point="성분별 일반 사용 범위를 확인해 지정 함량의 적정성 검토 필요"
        ),
        ConcentrationReviewPoint(
            condition="지정 함량 10%",
            review_type="제형",
            review_point="지정 함량에 따른 제형 안정성 확인 필요"
        )
    ],
    backbone_modification_required=False,
    backbone_adjustment="없음",
    issue_codes=[],
    issues=[],
    backbone_actions=[],
    required_backbone_changes=[]
)
```

`issue_codes`는 `PH_MISMATCH`, `OXIDATION_RISK`, `SOLUBILITY_DISPERSION_MISMATCH` 같은 안정적인 내부 코드다.
`backbone_actions`는 4단계 하위 Backbone 수정 전략이 집계하는 구조화된 수정 액션이며,
`required_backbone_changes`는 콘솔 표시용 문구다.
Bedrock 사용이 가능하면 LLM이 baseline 검토를 보정하되, Python이 허용된 코드와 액션만 통과시킨다.
LLM 보정용 리스크 코드에는 `OXIDATION_RISK`, `TRANSPARENCY_RISK`, `PRECIPITATION_RISK`, `SENSORY_RISK`, `PHOTO_STABILITY_RISK`가 포함된다.
허용 `action_type`은 `add_role`, `add_design_constraint`, `change_ph_range`, `change_formulation_type`, `review_system`이다.
`add_design_constraint`는 후보나 role 우선순위를 고르지 않고 4단계 하위 수정 전략의 설계 제약조건으로 직접 전달된다.
`review_system`은 role 추가가 아니라 관련 시스템 검토가 필요하다는 action이며, `note`는 해당 검토의 근거 문구다.
4단계 하위 수정 전략에서 `change_ph_range`는 목표 pH 조정 필요, `change_formulation_type`은 해당 제형 타입 기반 Backbone 조건 검토로 표현한다.
LLM 호출 실패 시 baseline 검토 결과를 그대로 사용한다.
Baseline은 명확한 적합 판정만 `적합`으로 두고, O/W 유화의 난용성/불명 성분,
분산 제형의 수용성/유용성 성분, pH 일부 overlap 같은 경우는 `확인 필요` 또는 `조건부 적합`으로 남긴다.
현재 단계에서는 일반 사용 범위와 함량 적합성을 출력하지 않고 농도 영향은 검토 포인트로만 출력한다.

### `BackboneAction`

```python
BackboneAction(
    action_type="add_role",
    role="chelator",
    target_value=None,
    reason_code="OXIDATION_RISK",
    note="산화 민감 액티브 보조 안정화"
)
```

### `BackboneRevision`

```python
BackboneRevision(
    design_constraints=[
        "목표 pH 3~4 유지",
        "산화 안정성 확보 전략 검토 필요",
        "산성 조건에서 점도 변화 실측 확인 필요"
    ],
    added_required_roles=["chelator"],
    rationale=["pH 불일치", "산화 안정성 우려"],
    source_actions=[...]
)
```

`BackboneRevision`은 후보 성분을 선정하지 않는다.
후보 선정, 보존 시스템, 용매 시스템, 점증/가용화/킬레이트/항산화 시스템 구체화는
`FinalBackboneFinalizer`가 필요한 정보만 받아 최종 Backbone 처방표로 확정한다.
Bedrock 사용이 가능하면 LLM이 설계 제약조건 문장을 정리하되, 추가 역할군은 source action에서 나온 role 중
현재 제형 목적상 최종 Backbone 구조에 반드시 필요한 role만 통과시킨다.
`backbone_actions.add_role`은 추가 명령이 아니라 검토 후보이며, 검토 필요/실측 후 판단/조건부 적용 항목은
`added_required_roles`가 아니라 `design_constraints`에만 남긴다.
pH, 점도, 산화, 외관, 사용감, 고농도 이슈는 기본적으로 role 추가가 아니라 실측/검토/설계 조건으로 정리한다.
예를 들어 pH 우려는 `buffer` 추가 확정보다 pH control strategy 검토로,
점도 우려는 `rheology_modifier` 추가 확정보다 목표 점도 실측 확인으로 우선 정리한다.
`rationale`은 4단계 `issues`를 단순 집계해 만든 수정 근거이며,
`source_actions`는 4단계 하위 수정 전략을 만든 원본 `BackboneAction` 추적값이다.

### `FinalBackboneResult`

```python
FinalBackboneResult(
    backbone_formula=FinalBackboneFormula(
        name="Final Backbone",
        concept="액티브 투입 전 최종 Backbone",
        backbone_type="수상 솔루션",
        target_aspects=["워터", "저점도", "약산성"],
        ingredients=[
            FinalBackboneIngredient(
                name="정제수",
                content=80.0,
                role="q.s. 연속상",
                reason="액티브 예약량 포함 총합 100% 보정"
            )
        ],
        reserved_actives=[
            FinalActiveReservation(
                ingredient="아스코빅애씨드",
                content=20.0,
                reason="사용자 지정 액티브 함량: 최종 Backbone q.s. 계산에서 예약"
            )
        ],
        backbone_pct=80.0,
        total_pct=100.0
    ),
    validation=FinalBackboneValidation(
        total_pct_valid=True,
        continuous_phase_qs_valid=True,
        sorted_by_content_desc=True,
        needs_backbone_redesign=False
    ),
    redesign_feedback=[],
    design_rationale="최종 Backbone 확정 근거",
    applied_revision=["목표 pH 조정 필요"],
    deferred_checks=["최종 pH 실측 후 조정"],
    warnings=[]
)
```

`FinalBackboneFinalizer`는 LLM이 제안한 최종 Backbone 처방표를 Python에서 검증한다.
성분명은 `data.csv` 후보 성분만 통과시키고, 액티브 성분은 최종 Backbone에서 제외한다.
다만 지정 액티브 함량은 `reserved_actives`로 관리하고 q.s. 계산에서 차감한다.
q.s. 연속상 성분을 찾아 나머지 고정 Backbone 성분 합과 지정 액티브 예약량을 기준으로 100%를 보정하며,
q.s. 함량이 음수가 되면 최종 확정 실패로 처리하고 `[3. Backbone 설계]` 재설계 피드백을 남긴다.
최종 성분표는 함량 내림차순으로 정렬한다.

---

## 다음 구현 예정

- 성분명 DB 매핑
- 타겟 제품 탐색
- 최종 처방 3안 생성 프롬프트 구성
- Claude 최종 처방 3안 생성
- 후처리 검증
