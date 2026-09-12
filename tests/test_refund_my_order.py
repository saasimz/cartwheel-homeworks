"""Offline tests for the Homework 1 full-refund convenience tool."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent import tools
from agent.agent import TOOLS_BY_ROLE
from agent.auth import AuthContext

SHOPPER_1 = AuthContext(user_id=1, role="shopper")
SHOPPER_2 = AuthContext(user_id=2, role="shopper")


def test_full_refund_auto_approves_and_explains_delivery_date(world_copy: Path) -> None:
    result = tools.refund_my_order(SHOPPER_1, 4127, "arrived chipped")

    assert result["ok"] is True
    assert result["status"] == "auto_approved"
    assert result["amount_usd"] == 84.0
    assert result["delivered_at"] == "2026-06-19"
    assert result["days_since_delivery"] == 12
    assert result["return_window_days"] == 30
    assert result["policy_id"] == "cw-returns"
    assert "$100" in result["decision_reason"]

    with sqlite3.connect(world_copy) as conn:
        status = conn.execute("SELECT status FROM orders WHERE id = 4127").fetchone()[0]
    assert status == "refunded"


def test_full_refund_above_threshold_queues_without_marking_refunded(
    world_copy: Path,
) -> None:
    result = tools.refund_my_order(SHOPPER_1, 4455, "no longer needed")

    assert result["ok"] is True
    assert result["status"] == "queued_for_approval"
    assert result["amount_usd"] == 240.0
    assert result["days_since_delivery"] == 5
    assert "queued for human approval" in result["decision_reason"]

    with sqlite3.connect(world_copy) as conn:
        status = conn.execute("SELECT status FROM orders WHERE id = 4455").fetchone()[0]
    assert status == "delivered"


def test_full_refund_outside_window_returns_exact_timing_reason(world_copy: Path) -> None:
    result = tools.refund_my_order(SHOPPER_1, 3980, "no longer needed")

    assert result["ok"] is False
    assert result["error"] == "not_eligible"
    assert result["delivered_at"] == "2026-05-17"
    assert result["days_since_delivery"] == 45
    assert result["return_window_days"] == 30
    assert "outside the applicable 30-day return window" in result["reason"]


def test_full_refund_denies_another_shoppers_order_without_details(
    world_copy: Path,
) -> None:
    result = tools.refund_my_order(SHOPPER_2, 4127, "not my order")

    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    assert "delivered_at" not in result


def test_full_refund_tool_is_exposed_only_to_shoppers() -> None:
    names_by_role = {
        role: {tool.name for tool in role_tools}
        for role, role_tools in TOOLS_BY_ROLE.items()
    }

    assert "refund_my_order" in names_by_role["shopper"]
    assert "refund_my_order" not in names_by_role["merchant"]
    assert "refund_my_order" not in names_by_role["support"]
