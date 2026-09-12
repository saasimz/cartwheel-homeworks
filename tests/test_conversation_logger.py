"""Tests for the independent terminal JSONL logger."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.conversation_logger import append_terminal_record


def test_append_terminal_record_preserves_failure_label_and_tool_result(tmp_path) -> None:
    path = tmp_path / "logs" / "terminal-conversations.jsonl"
    items = [
        SimpleNamespace(
            type="tool_call_item",
            call_id="call-1",
            tool_name="find_order",
            raw_item={"arguments": '{"order_id": 3980}'},
        ),
        SimpleNamespace(
            type="tool_call_output_item",
            call_id="call-1",
            output='{"ok": false, "error": "not_eligible"}',
        ),
    ]

    append_terminal_record(
        path,
        session_id="cli-shopper-1-example",
        turn_number=1,
        role="shopper",
        user_id=1,
        store_id=None,
        request="Refund order 3980",
        response="This order is not eligible.",
        new_items=items,
        is_failure=True,
    )

    record = json.loads(path.read_text().strip())
    assert record["request"] == "Refund order 3980"
    assert record["is_failure"] is True
    assert record["tool_calls"] == [
        {
            "name": "find_order",
            "arguments": {"order_id": 3980},
            "result": {"ok": False, "error": "not_eligible"},
        }
    ]
