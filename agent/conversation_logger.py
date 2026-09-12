"""Persist independent failure records from the local CLI.

The Homework 1 submission log is intentionally not used here. This module
captures terminal activity directly so local debugging records cannot change
or be mistaken for the student's assessed homework evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _decode_json(value: Any) -> Any:
    """Decode SDK JSON strings so the log remains easy to inspect."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def collect_tool_calls(new_items: list[Any]) -> list[dict[str, Any]]:
    """Pair each tool call with its corresponding SDK output item."""
    outputs = {
        item.call_id: _decode_json(item.output)
        for item in new_items
        if item.type == "tool_call_output_item" and item.call_id is not None
    }
    calls: list[dict[str, Any]] = []
    for item in new_items:
        if item.type != "tool_call_item":
            continue
        raw = item.raw_item
        arguments = (
            raw.get("arguments")
            if isinstance(raw, dict)
            else getattr(raw, "arguments", None)
        )
        call: dict[str, Any] = {
            "name": item.tool_name,
            "arguments": _decode_json(arguments),
        }
        # Missing outputs should remain visibly different from a successful
        # tool returning null, which is why the key is added conditionally.
        if item.call_id in outputs:
            call["result"] = outputs[item.call_id]
        calls.append(call)
    return calls


def append_terminal_record(
    path: Path,
    *,
    session_id: str,
    turn_number: int,
    role: str,
    user_id: int,
    store_id: int | None,
    request: str,
    response: Any,
    new_items: list[Any],
    is_failure: bool | None,
    error: str | None = None,
) -> None:
    """Append one user-confirmed or technical failure as a JSONL record."""
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "turn_number": turn_number,
        "role": role,
        "user_id": user_id,
        "store_id": store_id,
        "request": request,
        "tool_calls": collect_tool_calls(new_items),
        "response": response,
        "is_failure": is_failure,
        "error": error,
    }
    # Logs are runtime data, so create their ignored directory only when the
    # CLI actually needs it rather than requiring setup before every run.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
