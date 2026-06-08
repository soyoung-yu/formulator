"""
공통 유틸리티: console, Bedrock 페이로드 헬퍼, 포매팅 헬퍼.
config.py만 import한다.
"""

import ast
import json
import re
import warnings
from typing import Any

from openpyxl.styles import Alignment

warnings.filterwarnings("ignore")

# ── rich 선택적 임포트 ────────────────────────────────────────────────────
try:
    from rich.console import Console as _RC
    from rich.table import Table as RichTable
    from rich import box as rbox

    _rich_console = _RC()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    RichTable = None  # type: ignore[assignment,misc]
    rbox = None       # type: ignore[assignment]
    _rich_console = None  # type: ignore[assignment]


class _FallbackConsole:
    @staticmethod
    def _s(m: object) -> str:
        return re.sub(r"\[/?[^\]]*\]", "", str(m))

    def print(self, m: object = "", **_: Any) -> None:
        print(self._s(m))

    def rule(self, m: object = "", **_: Any) -> None:
        print("\n" + "=" * 60 + "\n  " + self._s(m) + "\n" + "=" * 60)


console: Any = _rich_console if HAS_RICH else _FallbackConsole()


# ── Bedrock 공통 헬퍼 ────────────────────────────────────────────────────

def _strip_json_fences(raw: str) -> str:
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text)


def _build_bedrock_messages_payload(
    prompt: str,
    max_tokens: int,
    system: str | None = None,
    temperature: float = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system is not None:
        payload["system"] = system
    return payload


def _invoke_bedrock_json(
    bedrock_client: Any,
    model_id: str,
    prompt: str,
    max_tokens: int,
    system: str | None = None,
    temperature: float = 0,
) -> tuple[Any, str]:
    response = bedrock_client.invoke_model(
        modelId=model_id,
        body=json.dumps(
            _build_bedrock_messages_payload(
                prompt, max_tokens=max_tokens, system=system, temperature=temperature
            ),
            ensure_ascii=False,
        ),
    )
    body = json.loads(response["body"].read())
    raw  = _strip_json_fences(body["content"][0]["text"])
    return json.loads(raw), raw


# ── 이름 정규화 ──────────────────────────────────────────────────────────

def _norm_name(s: str) -> str:
    return re.sub(r"[\s\-_·•]", "", str(s)).lower()


# ── 데이터 파싱 헬퍼 ────────────────────────────────────────────────────

def _safe_literal_list(value: Any) -> list:
    try:
        parsed = ast.literal_eval(str(value))
    except (SyntaxError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


# ── 출력 포매팅 헬퍼 ────────────────────────────────────────────────────

def _format_stat_line(i: dict) -> str:
    return (
        f"  · {i['name']}: 빈도 {i['frequency']*100:.0f}% ({i['count']}건), "
        f"함량 {i['min']}~{i['max']}% (중앙값 {i['median']}%)"
    )


def _join_or_none(values: list[str]) -> str:
    return ", ".join(values) or "없음"


def _formula_ingredient_rows(formula: dict) -> list[dict]:
    return [
        {"성분명": i.get("name", ""), "함량(%)": i.get("content", 0), "역할": i.get("role", "")}
        for i in sorted(formula["ingredients"], key=lambda x: -x.get("content", 0))
    ]


def _format_wrapped_sheet(
    ws: Any,
    wrap_columns: list[int],
    width_map: dict[int, float] | None = None,
) -> None:
    width_map = width_map or {}
    for col_idx, width in width_map.items():
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width

    for row in ws.iter_rows():
        max_lines = 1
        for cell in row:
            value = cell.value
            if isinstance(value, str):
                max_lines = max(max_lines, value.count("\n") + 1)
            if cell.column in wrap_columns:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        if row[0].row > 1 and max_lines > 1:
            ws.row_dimensions[row[0].row].height = max(15 * max_lines, 30)
