"""
출력·저장·비용 계산.
  - calc_cost(), print_cost_summary()
  - print_results()
  - save_results()
"""

import json
from pathlib import Path

import pandas as pd

from formulator.config import BEDROCK_PRICING
from formulator.utils import (
    HAS_RICH,
    RichTable,
    _format_wrapped_sheet,
    _formula_ingredient_rows,
    _rich_console,
    console,
    rbox,
)


# 모델 ID와 토큰 수로 USD·KRW 호출 비용을 계산해 반환
def calc_cost(model_id: str, input_tokens: int, output_tokens: int) -> dict:
    KRW_PER_USD = 1380
    pricing = BEDROCK_PRICING.get(model_id)
    if pricing is None:
        pricing    = {"input": 3.00, "output": 15.00}
        price_note = f"⚠ '{model_id}' 요금 미등록 → Sonnet 기본 요금 적용"
    else:
        price_note = f"입력 ${pricing['input']}/1M · 출력 ${pricing['output']}/1M (서울 리전)"

    input_cost  = input_tokens  / 1_000_000 * pricing["input"]
    output_cost = output_tokens / 1_000_000 * pricing["output"]
    total_usd   = input_cost + output_cost

    return {
        "input_tokens":    input_tokens,
        "output_tokens":   output_tokens,
        "input_cost_usd":  round(input_cost,  6),
        "output_cost_usd": round(output_cost, 6),
        "total_cost_usd":  round(total_usd,   6),
        "total_cost_krw":  round(total_usd * KRW_PER_USD, 2),
        "price_note":      price_note,
    }


# 비용 dict를 받아 토큰 수·USD·KRW 비용 요약을 콘솔에 출력
def print_cost_summary(cost: dict) -> None:
    lines = [
        "💰 비용 요약",
        f"  토큰: 입력 {cost['input_tokens']:,} / 출력 {cost['output_tokens']:,} / "
        f"합계 {cost['input_tokens'] + cost['output_tokens']:,}",
        f"  입력 비용: ${cost['input_cost_usd']:.6f} USD",
        f"  출력 비용: ${cost['output_cost_usd']:.6f} USD",
        f"  총 비용:   ${cost['total_cost_usd']:.6f} USD  "
        f"(약 {cost['total_cost_krw']:,.2f}원, 환율 1,380원/USD 기준)",
        f"  요금 기준: {cost['price_note']}",
    ]
    console.print("\n".join(lines))


# 생성된 처방 3안을 성분 테이블 형식으로 콘솔에 출력 (rich 사용 가능 시 컬러 테이블)
def print_results(formula_data: dict, query: str) -> None:
    console.rule(f"처방 생성 결과 — {query}")
    for formula in formula_data.get("formulas", []):
        name    = formula.get("name", "")
        concept = formula.get("concept", "")
        key_i   = formula.get("key_ingredients", [])
        aspects = formula.get("target_aspects", [])
        ings    = sorted(formula.get("ingredients", []), key=lambda x: -x.get("content", 0))

        if HAS_RICH and _rich_console is not None and RichTable is not None and rbox is not None:
            _rich_console.print(f"\n[bold cyan]{'─'*65}[/bold cyan]")
            _rich_console.print(f"[bold yellow]{name}[/bold yellow]  │  {concept}")
            if key_i:   _rich_console.print(f"[dim]핵심 성분: {', '.join(key_i)}[/dim]")
            if aspects: _rich_console.print(f"[dim]타겟 aspect: {', '.join(aspects)}[/dim]")
            _rich_console.print(f"[bold cyan]{'─'*65}[/bold cyan]")
            tbl = RichTable(box=rbox.SIMPLE, header_style="bold white on dark_blue")
            tbl.add_column("성분명",  min_width=34)
            tbl.add_column("함량(%)", justify="right", min_width=10, style="green")
            tbl.add_column("역할",    min_width=28)
            total = 0.0
            for i in ings:
                c = i.get("content", 0); total += c
                tbl.add_row(i.get("name", ""), f"{c:.4f}", i.get("role", ""))
            tbl.add_row("─"*34, "─"*8, "")
            tbl.add_row("[bold]합계[/bold]", f"[bold]{total:.4f}[/bold]", "")
            _rich_console.print(tbl)
        else:
            print(f"\n{'─'*65}\n{name}  │  {concept}")
            if key_i:   print(f"핵심 성분: {', '.join(key_i)}")
            if aspects: print(f"타겟 aspect: {', '.join(aspects)}")
            print(f"{'─'*65}")
            print(f"{'성분명':<34} {'함량(%)':>10}  역할\n" + "-"*75)
            total = 0.0
            for i in ings:
                c = i.get("content", 0); total += c
                print(f"{i.get('name', ''):<34} {c:>10.4f}  {i.get('role', '')}")
            print("-"*75 + f"\n{'합계':<46} {total:>10.4f}")

    if dr := formula_data.get("design_rationale"):
        print(f"\n{'─'*65}\n[설계 근거]\n{dr}")


# 처방 CSV·Excel·통계 JSON을 output_dir에 저장 (비용·프롬프트 시트 포함)
def save_results(
    formula_data: dict,
    stats: dict,
    keyword_db: dict,
    query: str,
    output_dir: str,
    cost: dict | None = None,
    prompt_payload: dict | None = None,
) -> None:
    cost = cost or {}
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # 처방별 CSV
    for formula in formula_data.get("formulas", []):
        fname    = formula["name"].replace(" ", "_").lower()
        rows     = _formula_ingredient_rows(formula)
        csv_path = output_path / f"{fname}.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
        console.print(f"[green]✓ {csv_path}[/green]")

    xl_path = output_path / "formula_output.xlsx"
    with pd.ExcelWriter(xl_path, engine="openpyxl") as writer:
        # 처방 시트
        for formula in formula_data.get("formulas", []):
            rows = _formula_ingredient_rows(formula)
            df_f = pd.DataFrame(rows)
            df_f.loc[len(df_f)] = {"성분명": "합계", "함량(%)": df_f["함량(%)"].sum(), "역할": ""}
            df_f.to_excel(writer, sheet_name=formula["name"], index=False)
            _format_wrapped_sheet(
                writer.sheets[formula["name"]],
                wrap_columns=[1, 3],
                width_map={1: 34, 2: 12, 3: 48},
            )

        # 성분 통계 시트
        ist = stats["ingredient_stats"]
        pd.DataFrame([
            {
                "성분명": n, "구조적역할": s["structural_role"],
                "사용빈도": f"{s['frequency']*100:.1f}%", "사용처방수": s["count"],
                "최소(%)": s["min"], "25%(%)": s["p25"], "중앙값(%)": s["median"],
                "75%(%)": s["p75"], "최대(%)": s["max"],
            }
            for n, s in sorted(ist.items(), key=lambda x: -x[1]["frequency"])
        ]).to_excel(writer, sheet_name="성분_통계", index=False)
        _format_wrapped_sheet(
            writer.sheets["성분_통계"],
            wrap_columns=[1, 2],
            width_map={1: 28, 2: 16, 3: 12, 4: 12, 5: 10, 6: 10, 7: 12, 8: 10, 9: 10},
        )

        # 키워드-성분 매핑 시트
        kw_rows = []
        for kw, kdata in keyword_db.items():
            for ing, cnt in kdata["ingredients"].most_common(5):
                kw_rows.append({
                    "키워드": kw, "연결성분": ing, "등장횟수": cnt,
                    "관련aspect": ", ".join(kdata["aspects"]),
                    "연결처방수": len(kdata["formula_codes"]),
                })
        pd.DataFrame(kw_rows).to_excel(writer, sheet_name="키워드_성분매핑", index=False)
        _format_wrapped_sheet(
            writer.sheets["키워드_성분매핑"],
            wrap_columns=[1, 2, 4],
            width_map={1: 22, 2: 26, 3: 10, 4: 28, 5: 12},
        )

        # 설계 근거 시트
        meta = [
            {"항목": "요구사항", "내용": query},
            {"항목": "설계 근거", "내용": formula_data.get("design_rationale", "")},
        ]
        for f in formula_data.get("formulas", []):
            meta.append({
                "항목": f["name"],
                "내용": (
                    f"{f.get('concept', '')} | "
                    f"핵심: {', '.join(f.get('key_ingredients', []))} | "
                    f"aspect: {', '.join(f.get('target_aspects', []))}"
                ),
            })
        pd.DataFrame(meta).to_excel(writer, sheet_name="설계_근거", index=False)
        _format_wrapped_sheet(
            writer.sheets["설계_근거"],
            wrap_columns=[1, 2],
            width_map={1: 18, 2: 120},
        )

        # 비용 내역 시트
        if cost:
            cost_rows = [
                {"항목": "입력 토큰",      "값": f"{cost.get('input_tokens', 0):,}"},
                {"항목": "출력 토큰",      "값": f"{cost.get('output_tokens', 0):,}"},
                {"항목": "입력 비용",      "값": f"${cost.get('input_cost_usd', 0):.4f}"},
                {"항목": "출력 비용",      "값": f"${cost.get('output_cost_usd', 0):.4f}"},
                {"항목": "총 비용 (USD)", "값": f"${cost.get('total_cost_usd', 0):.6f}"},
                {"항목": "총 비용 (KRW)", "값": f"{cost.get('total_cost_krw', 0):,.2f}원"},
                {"항목": "요금 기준",      "값": cost.get("price_note", "")},
            ]
            pd.DataFrame(cost_rows).to_excel(writer, sheet_name="비용_내역", index=False)
            _format_wrapped_sheet(
                writer.sheets["비용_내역"],
                wrap_columns=[1, 2],
                width_map={1: 18, 2: 40},
            )

        # Claude 프롬프트 시트
        if prompt_payload:
            prompt_rows = [
                {"구분": "model_id",      "내용": prompt_payload.get("model_id", "")},
                {"구분": "system_prompt", "내용": prompt_payload.get("system_prompt", "")},
                {"구분": "user_prompt",   "내용": prompt_payload.get("user_prompt", "")},
            ]
            pd.DataFrame(prompt_rows).to_excel(writer, sheet_name="Claude_프롬프트", index=False)
            _format_wrapped_sheet(
                writer.sheets["Claude_프롬프트"],
                wrap_columns=[1, 2],
                width_map={1: 18, 2: 120},
            )

    console.print(f"[green]✓ {xl_path}[/green]")

    stats_path = output_path / "stats_summary.json"
    with stats_path.open("w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ {stats_path}[/green]")
