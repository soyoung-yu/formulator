"""
AWS Bedrock 클라이언트 + LLM 1회 호출.
  - _create_bedrock_client()
  - call_llm(): 처방 3안 생성, JSON 파싱 실패 시 최대 2회 재시도
"""

import json
import time
from typing import Any

from formulator.config import BEDROCK_PRICING, DEFAULT_AWS_REGION
from formulator.prompt import SYSTEM_PROMPT, build_user_prompt
from formulator.utils import _build_bedrock_messages_payload, _strip_json_fences, console

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore[assignment]


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


# AWS 프로파일과 리전으로 Bedrock Runtime boto3 클라이언트를 생성해 반환
def _create_bedrock_client(aws_profile: str | None, aws_region: str) -> Any:
    if boto3 is None:
        raise RuntimeError("boto3 미설치 — `pip install boto3` 필요")
    if aws_profile:
        session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
    else:
        session = boto3.Session()
    return session.client(service_name="bedrock-runtime", region_name=aws_region)


def call_llm(
    query: str,
    ctx: dict,
    bedrock_client: Any,
    model_id: str,
    max_retries: int = 2,
) -> tuple[dict | None, dict, dict]:
    """
    처방 3안 생성 LLM 1회 호출.
    JSON 파싱 실패 시에만 재시도 (최대 max_retries회).

    Returns:
        (formula_data | None, cost_dict, prompt_payload_dict)
    """
    total_formulas = ctx.get("total_formulas", 0)
    user_prompt    = build_user_prompt(query, ctx, total_formulas)
    prompt_payload = {
        "model_id":      model_id,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt":   user_prompt,
    }

    console.print(f"[dim]Claude API 호출 중... (모델: {model_id})[/dim]")

    for attempt in range(1, max_retries + 2):
        try:
            response = bedrock_client.invoke_model(
                modelId=model_id,
                body=json.dumps(
                    _build_bedrock_messages_payload(
                        user_prompt, max_tokens=4096, system=SYSTEM_PROMPT
                    ),
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            err = str(e)
            if "ThrottlingException" in err or "Too Many Requests" in err:
                wait = 10 * attempt
                console.print(f"[yellow]Rate limit — {wait}초 후 재시도 ({attempt}/{max_retries})[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"[red]API 호출 오류: {err}[/red]")
            return None, {}, prompt_payload

        body    = json.loads(response["body"].read())
        text    = body["content"][0]["text"]
        usage   = body.get("usage", {})
        in_tok  = usage.get("input_tokens",  0)
        out_tok = usage.get("output_tokens", 0)
        cost    = calc_cost(model_id, in_tok, out_tok)
        console.print(f"[dim]✓ 토큰: 입력 {in_tok:,} / 출력 {out_tok:,}[/dim]")

        raw = _strip_json_fences(text)
        try:
            return json.loads(raw), cost, prompt_payload
        except json.JSONDecodeError:
            if attempt <= max_retries:
                console.print(f"[yellow]JSON 파싱 실패 — 재시도 ({attempt}/{max_retries})[/yellow]")
                user_prompt += "\n\n반드시 JSON 형식만 응답하세요. 다른 텍스트 없이."
                prompt_payload["user_prompt"] = user_prompt
                continue
            console.print(f"[red]JSON 파싱 최종 실패\n{raw[:400]}[/red]")
            return None, cost, prompt_payload

    return None, {}, prompt_payload
