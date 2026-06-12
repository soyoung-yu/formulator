"""
프롬프트 정의 및 빌더.
  - SYSTEM_PROMPT: 처방 전문가 역할 + JSON 출력 형식
  - build_user_prompt(): 컨텍스트 → 유저 프롬프트 문자열
변경 빈도가 높은 파일. 프롬프트만 수정할 때 이 파일만 열면 된다.
"""

from formulator.utils import _format_stat_line

SYSTEM_PROMPT = """당신은 화장품 처방 전문가입니다. 연구원이 실험을 시작할 수 있는 초기 처방 백본을 설계합니다.

[처방 설계 규칙]
1. 모든 성분 함량의 합은 정확히 100.00%여야 합니다. 정제수로 나머지를 채우세요.
2. 정제수가 기본 용제로 가장 높은 비율을 차지합니다 (통상 60~90%).
3. 방부·보존 성분이 반드시 포함되어야 합니다.
4. 성분명은 제공된 [허용 성분 목록]에 있는 것만 사용합니다. 목록에 없는 성분명은 절대 생성하지 마세요.
5. 사용자가 함량을 직접 지정한 경우 해당 함량을 정확히 사용합니다.
6. 제공된 통계(중앙값, 범위)를 참고해 현실적인 함량을 배정하세요. 통계 최대값을 크게 초과하지 마세요.
7. 질의의 사용감 요구(끈적이지 않는, 산뜻한 등)와 제형 특성을 스스로 판단해 성분과 함량에 반영하세요.
8. 처방 설계 후 화장품 화학 관점에서 스스로 검토하세요.
   - 기능적 완전성: 특정 성분의 작동에 필요한 짝 성분이 누락되지 않았는지 (예: 카보머 계열 점증제 → 중화제, 에멀전 제형 → 계면활성제)
   - 화학적 호환성: 함께 사용 시 상호 간섭하거나 효능을 저해하는 조합이 없는지
   - 제형 일관성: 선택한 성분들이 목표 제형(에센스, 크림, 로션 등)의 특성과 부합하는지

[출력 형식 — JSON만 응답, 다른 텍스트 없이]
{
  "formulas": [
    {
      "name": "Formula A",
      "concept": "컨셉 한 줄",
      "key_ingredients": ["핵심성분1", "핵심성분2"],
      "target_aspects": ["미백", "산뜻함"],
      "ingredients": [
        {"name": "성분명", "content": 숫자(%), "role": "역할 설명"}
      ]
    },
    {"name": "Formula B", "concept": "...", "key_ingredients": [], "target_aspects": [], "ingredients": []},
    {"name": "Formula C", "concept": "...", "key_ingredients": [], "target_aspects": [], "ingredients": []}
  ],
  "design_rationale": "3안 설계 근거 요약"
}"""


# 파이프라인 컨텍스트를 받아 Claude에 전달할 유저 프롬프트 문자열을 조립해 반환
def build_user_prompt(query: str, ctx: dict, total_formulas: int) -> str:
    lines: list[str] = []
    query_info     = ctx.get("query_info", {})
    ingredient_map = ctx.get("ingredient_map", {})

    # ── 요구사항 ──────────────────────────────────────────────────────────
    lines.append(f"[요구사항]\n{query}")

    formulation_hints = query_info.get("formulation_hints", [])
    marketing_hints   = query_info.get("marketing_hints", [])
    if formulation_hints:
        lines.append(f"\n[제형·사용감 요구]\n  {', '.join(formulation_hints)}")
    if marketing_hints:
        lines.append(f"\n[마케팅 포인트]\n  {', '.join(marketing_hints)}")

    # ── 유사 처방 ─────────────────────────────────────────────────────────
    similar  = ctx.get("similar_formulas", {})
    group_a  = similar.get("group_a", []) if isinstance(similar, dict) else []
    group_b  = similar.get("group_b", []) if isinstance(similar, dict) else []

    # 유사 처방 한 건을 "처방N: 이름 [매칭 이유] / 성분 구성" 형식 줄 목록으로 포매팅
    def _fmt_similar(idx: int, f: dict) -> list[str]:
        reason_parts = []
        if f.get("matched_keywords"):
            reason_parts.append(f"키워드: {', '.join(f['matched_keywords'])}")
        if f.get("matched_ingredients"):
            reason_parts.append(f"질의 성분: {', '.join(f['matched_ingredients'])}")
        reason = " / ".join(reason_parts)
        return [
            f"처방{idx}: {f['name']}  [{reason}]",
            f"  성분 구성: {', '.join(f['ingredients'])}",
        ]

    if group_a or group_b:
        lines.append("\n[유사 처방 참고 — 성분 구성 참고용, 함량 미포함]")
        if group_a:
            lines.append(f"▶ 마케팅 키워드 매칭 처방 (top-{len(group_a)}건)")
            for idx, f in enumerate(group_a, 1):
                lines.extend(_fmt_similar(idx, f))
        if group_b:
            lines.append(f"▶ 질의 성분 매칭 처방 (top-{len(group_b)}건)")
            for idx, f in enumerate(group_b, 1):
                lines.extend(_fmt_similar(idx, f))
    else:
        lines.append("\n[유사 처방] 조건에 맞는 유사 처방을 확인하지 못함")

    # ── 성분 통계 ──────────────────────────────────────────────────────────
    lines.append(f"\n[성분 통계 — {total_formulas}건 처방 기준]")
    for i in ctx.get("base_ings", [])[:12]:
        lines.append(_format_stat_line(i))
    for i in ctx.get("active_ings", []):
        lines.append(_format_stat_line(i))

    # ── 성분명 매핑 (alias → DB명) ────────────────────────────────────────
    # ingredient_map에서 alias 매핑된 항목만 표시 (DB 직접 매칭은 자명하므로 제외)
    alias_mappings = {
        term: db_names
        for term, db_names in ingredient_map.items()
        if db_names and term not in ctx.get("allowed_ingredients", [])
    }
    if alias_mappings:
        lines.append("\n[성분명 매핑 — 질의 표현과 DB 성분명 대응]")
        for term, db_names in alias_mappings.items():
            lines.append(f"  · '{term}' → {', '.join(db_names)}")

    # ── 사용자 지정 함량 ───────────────────────────────────────────────────
    user_constraints: dict = ctx.get("user_constraints", {})
    if user_constraints:
        lines.append("\n[사용자 지정 함량 — 반드시 이 함량 정확히 준수]")
        for db_name, amt in user_constraints.items():
            lines.append(f"  · {db_name}: 정확히 {amt}%")

    # ── 허용 성분 목록 ────────────────────────────────────────────────────
    allowed = ctx.get("allowed_ingredients", [])
    if allowed:
        lines.append(
            f"\n[허용 성분 목록 — 반드시 이 목록에서만 선택 (총 {len(allowed)}종)]\n"
            + ", ".join(allowed)
        )

    # ── 3안 설계 지침 ─────────────────────────────────────────────────────
    lines.append(
        "\n[3안 설계 지침]\n"
        "- Formula A: 핵심 활성 성분 고함량 + 성분 수 최소화 (심플 & 집중 효능)\n"
        "- Formula B: 핵심 효능 + 보습·진정 복합 기능 밸런스 (올라운드 실용 처방)\n"
        "- Formula C: 트렌드 성분 또는 복합 활성 성분 추가, 마케팅 소구점 강화 (프리미엄·차별화)\n"
        "- 각 안의 함량 합계가 정확히 100.00%가 되도록 정제수 함량으로 조정하세요.\n"
        "- [허용 성분 목록]에 없는 성분은 절대 사용하지 마세요."
    )
    if formulation_hints:
        lines.append(f"- 사용감 요구({', '.join(formulation_hints)})를 성분 선택·함량에 반영하세요.")
    if marketing_hints:
        lines.append(f"- 마케팅 포인트({', '.join(marketing_hints)})를 target_aspects와 설계 근거에 반영하세요.")

    return "\n".join(lines)
