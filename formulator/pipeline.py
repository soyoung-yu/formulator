"""
파이프라인 오케스트레이터.
  - run_pipeline(): 전체 흐름을 단계별로 호출
비즈니스 흐름의 단일 진실 공급원. 각 단계의 구현은 담당 모듈에 있다.
"""

from formulator.config import DEFAULT_AWS_REGION, DEFAULT_MODEL_ID
from formulator.context import build_context
from formulator.data import build_stats, load_formula_data, load_product_data
from formulator.llm import _create_bedrock_client, call_llm
from formulator.output import print_cost_summary, print_results, save_results
from formulator.postprocess import validate_and_fix
from formulator.query import extract_amount_constraints, extract_query_info, map_ingredients
from formulator.utils import console


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

    # 4. 질의 분석 (DB set + ALIAS_HINTS — LLM 없음, 마케팅 키워드는 keyword_db 기반)
    console.print("[dim]질의 분석 중...[/dim]")
    query_info = extract_query_info(
        query,
        known_set=known_set,
        marketing_keywords=set(keyword_db.keys()),
    )
    console.print(f"[dim]  추출 성분: {query_info['ingredients']}[/dim]")
    console.print(f"[dim]  제형 힌트: {query_info['formulation_hints']}[/dim]")
    console.print(f"[dim]  마케팅 힌트: {query_info['marketing_hints']}[/dim]")

    # 5. 성분명 매핑 (DB 직접 매칭은 통과 / ALIAS_HINTS 키는 DB 성분명으로 확장)
    ingredient_map: dict[str, list[str] | None] = {}
    if query_info["ingredients"]:
        console.print("[dim]성분명 매핑 중...[/dim]")
        ingredient_map = map_ingredients(query_info["ingredients"], known_set)
        for term, mapped in ingredient_map.items():
            status = f"→ {mapped}" if mapped else "→ 매핑 실패"
            console.print(f"[dim]  '{term}' {status}[/dim]")

    # 5b. 함량 추출 (LLM — 질의에 숫자% 있을 때만 호출)
    all_db_names = [
        db_name
        for mapped in ingredient_map.values()
        if mapped
        for db_name in mapped
    ]
    user_constraints: dict[str, float] = extract_amount_constraints(
        query, all_db_names, bedrock_client, model_id
    )
    if user_constraints:
        console.print(f"[dim]  사용자 지정 함량: {user_constraints}[/dim]")

    # 6. 컨텍스트 구성 (Python only)
    ctx = build_context(
        stats, keyword_db, formula_dict, query_info, ingredient_map
    )
    ctx["total_formulas"]   = stats["total_formulas"]
    ctx["user_constraints"] = user_constraints
    console.print(
        f"[green]✓ base {len(ctx['base_ings'])}종 / active {len(ctx['active_ings'])}종 / "
        f"유사처방 그룹A {len(ctx['similar_formulas'].get('group_a',[]))}건 "
        f"그룹B {len(ctx['similar_formulas'].get('group_b',[]))}건 / "
        f"허용 성분 {len(ctx['allowed_ingredients'])}종[/green]"
    )

    # 7. LLM 1회 호출 — 처방 3안 생성
    formula_data, cost, prompt_payload = call_llm(query, ctx, bedrock_client, model_id)
    if not formula_data:
        console.print("[red]처방 생성 실패[/red]")
        return

    # 8. 후처리 (화이트리스트 검증 + 합계 보정)
    formula_data = validate_and_fix(
        formula_data, stats, known_set=known_set, user_constraints=user_constraints
    )

    # 9. 출력 & 저장
    print_results(formula_data, query)
    save_results(
        formula_data, stats, keyword_db, query, output_dir,
        cost=cost, prompt_payload=prompt_payload,
    )
    print_cost_summary(cost)
    console.print(f"\n[bold green]완료! 결과: {output_dir}/[/bold green]")
