"""
파이프라인 오케스트레이터.
  - run_pipeline(): 전체 흐름을 단계별로 호출
비즈니스 흐름의 단일 진실 공급원. 각 단계의 구현은 담당 모듈에 있다.
"""

from formulator.config import DEFAULT_AWS_REGION, DEFAULT_MODEL_ID
from formulator.context import build_context
from formulator.data import build_stats, load_external_data, load_formula_data, load_product_data
from formulator.llm import _create_bedrock_client, call_llm
from formulator.output import print_cost_summary, print_results, save_results
from formulator.postprocess import validate_and_fix
from formulator.query import extract_query_info, extract_query_supplements, search_target_product
from formulator.utils import console


# 데이터 로드 → 질의 분석 → 컨텍스트 구성 → LLM 호출 → 후처리 → 저장까지 전체 파이프라인 실행
def run_pipeline(
    data_csv:    str,
    product_csv: str,
    external_csv: str,       # 호환성 유지용, v1.0에서는 사용 안 함
    query:       str,
    aws_profile: str | None = None,
    aws_region:  str        = DEFAULT_AWS_REGION,
    model_id:    str        = DEFAULT_MODEL_ID,
    output_dir:  str        = "output",
) -> None:
    console.rule("AI 기반 화장품 처방 자동 생성 PoC v1.0 (AWS Bedrock)")

    # ── Bedrock 클라이언트 ────────────────────────────────────────────────
    try:
        bedrock_client = _create_bedrock_client(aws_profile, aws_region)
        console.print("[green]✓ Bedrock 클라이언트 생성[/green]")
    except Exception as e:
        console.print(f"[red]Bedrock 클라이언트 생성 실패: {e}[/red]")
        return

    # 1. 처방 데이터 로드
    df, formula_dict = load_formula_data(data_csv)

    # 2. 통계
    console.print("[dim]통계 분석 중...[/dim]")
    stats     = build_stats(formula_dict)
    known_set = set(stats["ingredient_stats"].keys())
    console.print(f"[green]✓ 성분 {len(known_set)}종 통계[/green]")

    # 3. 마케팅 키워드 DB
    keyword_db = load_product_data(product_csv, formula_dict)

    # 3b. 타사 제품 DB
    external_db = load_external_data(external_csv)

    # 4. 질의 분석 (DB set + ALIAS_HINTS — LLM 없음, ingredient_map 직접 반환)
    console.print("[dim]질의 분석 중...[/dim]")
    query_info = extract_query_info(
        query,
        known_set=known_set,
        marketing_keywords=set(keyword_db.keys()),
    )
    ingredient_map = query_info["ingredient_map"]
    for term, mapped in ingredient_map.items():
        console.print(f"[dim]  '{term}' → {mapped}[/dim]")
    console.print(f"[dim]  마케팅 힌트: {query_info['marketing_hints']}[/dim]")

    # 4b. 질의 보완 추출 + 성분-함량 연결 (LLM 1회)
    console.print("[dim]질의 보완 추출 중 (LLM)...[/dim]")
    supplements = extract_query_supplements(
        query,
        list(ingredient_map.keys()),
        query_info["marketing_hints"],
        bedrock_client,
        model_id,
    )

    # 타겟 제품 DB 탐색 (Python — LLM이 추출한 제품명 기반)
    target_product = None
    if pname := supplements.get("target_product"):
        target_product = search_target_product(pname, formula_dict, external_db)
        if target_product:
            src = target_product["source"]
            console.print(f"[green]✓ 타겟 제품 [{src}] '{target_product['product_name']}' 매칭[/green]")
        else:
            console.print(f"[dim]  타겟 제품 '{pname}' — DB 미매칭[/dim]")

    # 추가 키워드·사용감 병합
    extra_keywords: list[str] = supplements.get("additional_keywords", [])
    if extra_keywords:
        query_info["marketing_hints"] = query_info["marketing_hints"] + extra_keywords
        console.print(f"[dim]  추가 키워드: {extra_keywords}[/dim]")

    # 추가 성분 (DB 미검증 → 힌트로만 전달)
    additional_ingredients: list[str] = supplements.get("additional_ingredients", [])
    if additional_ingredients:
        console.print(f"[dim]  추가 성분 힌트: {additional_ingredients}[/dim]")

    # 성분-함량 연결 결과 → user_constraints로 확장
    raw_constraints: dict[str, float] = supplements.get("ingredient_amounts", {})
    # alias 키 → DB 성분명으로 확장 (다중 매핑 시 모든 후보에 동일 함량 적용)
    user_constraints: dict[str, float] = {
        db_name: amount
        for term, amount in raw_constraints.items()
        if term in ingredient_map
        for db_name in ingredient_map[term]
    }
    if user_constraints:
        shown: set = set()
        for db_names in ingredient_map.values():
            candidates = [n for n in db_names if n in user_constraints]
            if not candidates:
                continue
            label = " 또는 ".join(candidates)
            console.print(f"[dim]  사용자 지정 함량: {label} = {user_constraints[candidates[0]]}%[/dim]")
            shown.update(candidates)
        for db_name, amt in user_constraints.items():
            if db_name not in shown:
                console.print(f"[dim]  사용자 지정 함량: {db_name} = {amt}%[/dim]")

    # 6. 컨텍스트 구성 (Python only)
    ctx = build_context(
        stats, keyword_db, formula_dict, query_info, ingredient_map,
        target_product=target_product,
    )
    ctx["total_formulas"]          = stats["total_formulas"]
    ctx["user_constraints"]        = user_constraints
    ctx["target_product"]          = target_product
    ctx["additional_ingredients"]  = additional_ingredients
    console.print(
        f"[green]✓ base {len(ctx['base_ings'])}종 / active(질의{len(ctx['query_active_ings'])}·타겟{len(ctx['target_active_ings'])}·유사{len(ctx['similar_active_ings'])}·범용{len(ctx['general_active_ings'])})종 / "
        f"유사처방 그룹A {len(ctx['similar_formulas'].get('group_a',[]))}건 "
        f"그룹B {len(ctx['similar_formulas'].get('group_b',[]))}건 / "
        f"허용 성분 {len(ctx['allowed_ingredients']) + len(ctx['remaining_ingredients'])}종 "
        f"(통계 {len(ctx['allowed_ingredients'])}+나머지 {len(ctx['remaining_ingredients'])})[/green]"
    )

    # 7. LLM 1회 호출 — 처방 3안 생성
    formula_data, cost, prompt_payload = call_llm(query, ctx, bedrock_client, model_id)
    if not formula_data:
        console.print("[red]처방 생성 실패[/red]")
        return

    # 8. 후처리 (화이트리스트 검증 + 합계 보정)
    formula_data = validate_and_fix(
        formula_data, stats, known_set=known_set, user_constraints=user_constraints,
        bedrock_client=bedrock_client, model_id=model_id,
    )

    # 9. 출력 & 저장
    print_results(formula_data, query)
    save_results(
        formula_data, stats, keyword_db, query, output_dir,
        cost=cost, prompt_payload=prompt_payload,
    )
    print_cost_summary(cost)
    console.print(f"\n[bold green]완료! 결과: {output_dir}/[/bold green]")
