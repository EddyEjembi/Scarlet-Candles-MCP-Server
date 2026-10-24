"""Stdout logs for every tool call."""

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

from core.errors import ToolFailure

ResultT = TypeVar("ResultT", bound=dict[str, object])


def log_tool(tool: str, inputs: dict[str, object], result: dict[str, object]) -> None:
    """Write one tool call: timestamp, tool name, inputs, and result."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (
        stamp
        + " tool="
        + tool
        + " inputs="
        + json.dumps(inputs, default=str)
        + " result="
        + json.dumps(result, default=str)
    )
    print(line, flush=True)


def run_tool(tool: str, inputs: dict[str, object], body: Callable[[], ResultT]) -> ResultT | dict[str, object]:
    """Run a tool body, convert a known failure into ok:false, and log the call."""
    try:
        result: ResultT | dict[str, object] = body()
    except ToolFailure as exc:
        result = {"ok": False, "error": str(exc)}
    log_tool(tool, inputs, result)
    return result
