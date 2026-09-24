"""Homework 1: the remaining commerce-agent tools.

The three lecture tools (`search_help_center`, `get_order`, `issue_refund`)
are implemented in agent/agent.py and are worked examples of the pattern:
check permissions first, go through agent/db.py for data, and return a
structured dict, never a prose error. The homework tools follow the same
pattern. agent/agent.py already wraps each function below as an SDK tool, so
once a function works here it works in chat with no further wiring.

Result convention (see agent/auth.py):
  - Success: a dict with "ok": True plus the payload fields named in each
    docstring.
  - Failure: {"ok": False, "error": <code>, "reason": <human-readable str>}.

Run the contract tests with: uv run pytest tests/test_hw_holes.py -k hw1
They are marked xfail and flip to passing as you implement each function.
"""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from agent import db
from agent.auth import (
    AuthContext,
    can_cancel_order,
    can_refund_order,
    permission_denied,
)
from agent.config import load_facts
from agent.helpcenter import load_policy_docs
from agent.killswitch import kill_switch
from seed.eligibility import effective_return_window_days, refund_needs_approval

MAX_SEARCH_LIMIT = 25
DEFAULT_ORDER_LIMIT = 20


def _process_refund(
    ctx: AuthContext,
    order_id: int,
    amount_usd: float | None,
    reason: str,
) -> dict[str, Any]:
    """Apply the canonical refund rules for explicit and full-order refunds.

    ``None`` means refund the complete order total. Keeping both public tools
    on this one implementation prevents their security and approval behavior
    from drifting apart as the course evolves.
    """
    paused = kill_switch("issue_refund")
    if paused is not None:
        return {"ok": False, "error": "paused", "reason": paused}
    if amount_usd is not None and amount_usd <= 0:
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "refund amount must be positive",
        }

    facts = load_facts()
    with db.connection() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            return {"ok": False, "error": "not_found", "reason": f"no order #{order_id}"}
        if not can_refund_order(ctx, order.user_id, order.store_id):
            return permission_denied(
                f"role '{ctx.role}' (user {ctx.user_id}) may not refund order #{order_id}"
            )

        refund_amount = order.total_usd if amount_usd is None else amount_usd
        if refund_amount > order.total_usd:
            return {
                "ok": False,
                "error": "invalid_argument",
                "reason": (
                    f"refund amount ${refund_amount:.2f} exceeds order total "
                    f"${order.total_usd:.2f}"
                ),
            }
        if not order.refund_eligible:
            return {
                "ok": False,
                "error": "not_eligible",
                "reason": (
                    f"order #{order_id} is not refund-eligible "
                    f"(status '{order.status}', delivered {order.delivered_at}); "
                    "the return window counts from the delivery date"
                ),
            }

        today = db.world_asof(conn).isoformat()
        threshold = facts["refund_auto_approve_threshold_usd"]
        if refund_needs_approval(refund_amount, threshold):
            refund_id = db.insert_refund(
                conn,
                order_id=order_id,
                amount_cents=round(refund_amount * 100),
                reason=reason,
                status="queued_for_approval",
                created_at=today,
            )
            return {
                "ok": True,
                "status": "queued_for_approval",
                "refund_id": refund_id,
                "order_id": order_id,
                "amount_usd": refund_amount,
                "note": (
                    f"amount is above the ${threshold} auto-approval threshold; "
                    "a human support agent will review it"
                ),
            }

        refund_id = db.insert_refund(
            conn,
            order_id=order_id,
            amount_cents=round(refund_amount * 100),
            reason=reason,
            status="auto_approved",
            created_at=today,
        )
        db.set_order_status(conn, order_id, "refunded")
        return {
            "ok": True,
            "status": "auto_approved",
            "refund_id": refund_id,
            "order_id": order_id,
            "amount_usd": refund_amount,
            "note": (
                "refund goes back to the original payment method in "
                f"{facts['refund_processing_days_min']} to "
                f"{facts['refund_processing_days_max']} business days"
            ),
        }


def _refund_timing_details(order_id: int) -> dict[str, Any]:
    """Return deterministic delivery-window inputs after authorization succeeds."""
    with db.connection() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            raise RuntimeError(f"refund result references missing order #{order_id}")
        store = db.get_store(conn, order.store_id)
        if store is None:
            raise RuntimeError(f"order #{order_id} references a missing store")
        as_of = db.world_asof(conn)

    facts = load_facts()
    window_days = effective_return_window_days(
        facts["return_window_days"], store.return_window_days_override
    )
    delivered_at = order.delivered_at
    days_since_delivery = (as_of - delivered_at).days if delivered_at is not None else None
    policy_id = (
        f"store-{store.slug}-policy"
        if store.return_window_days_override is not None
        else "cw-returns"
    )
    return {
        "order_status": order.status,
        "delivered_at": delivered_at.isoformat() if delivered_at is not None else None,
        "as_of": as_of.isoformat(),
        "days_since_delivery": days_since_delivery,
        "return_window_days": window_days,
        "policy_id": policy_id,
    }


def get_policy(ctx: AuthContext, policy_id: str) -> dict[str, Any]:
    """Fetch one policy doc by its exact id. Risk tier: read.

    Every role may read every policy doc (the corpus is public help-center
    content), so this tool needs no permission check.

    Args:
        ctx: The caller's auth context. Unused here, but every tool takes it.
        policy_id: An exact policy id, e.g. "cw-returns" or
            "store-juniper-home-goods-policy". Matching is exact and
            case-sensitive; ids are the `policy_id` front-matter field of the
            files in data/policies/.

    Returns:
        On success: {"ok": True, "policy_id": str, "title": str,
        "audience": str, "body": str} where body is the markdown body of the
        doc without the front matter.
        If no doc has that id: {"ok": False, "error": "not_found",
        "reason": ...} naming the id that was requested.

    Implementation notes:
        agent.helpcenter.load_policy_docs() returns every parsed doc.
    """
    for document in load_policy_docs():
        if document.policy_id == policy_id:
            return {
                "ok": True,
                "policy_id": document.policy_id,
                "title": document.title,
                "audience": document.audience,
                "body": document.body,
            }

    # A missing document is a normal lookup outcome, so callers receive the
    # structured error contract instead of an exception from the tool loop.
    return {
        "ok": False,
        "error": "not_found",
        "reason": f"no policy with id '{policy_id}'",
    }


def search_products(
    ctx: AuthContext,
    query: str,
    store: str | None = None,
    max_price_usd: float | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Search the product catalog. Risk tier: read.

    Every role may search products. Matching is deterministic keyword
    matching, not semantic search: a product matches when every whitespace
    token of `query` appears case-insensitively as a substring of the
    product's title or description.

    Args:
        ctx: The caller's auth context.
        query: Free-text query. Must be non-empty after stripping whitespace;
            otherwise return {"ok": False, "error": "invalid_argument",
            "reason": ...}.
        store: Optional store filter. Matched with
            agent.db.get_store_by_name (case-insensitive name or slug). If
            given and no store matches, return {"ok": False, "error":
            "not_found", "reason": ...} naming the store string.
        max_price_usd: Optional inclusive price ceiling. If given and not
            strictly positive, return an "invalid_argument" error.
        limit: Maximum products to return. Clamp to the range
            [1, MAX_SEARCH_LIMIT]; do not error on out-of-range values.

    Returns:
        {"ok": True, "products": [...], "count": <len(products)>} where each
        product is {"product_id": int, "store_id": int, "title": str,
        "price_usd": float}. Sort matches by price_usd ascending, then by
        product_id ascending, and truncate to `limit`. No matches is still a
        success: {"ok": True, "products": [], "count": 0}.

    Implementation notes:
        agent.db.list_products(conn, store_id) gives the candidate set.
        Use `with db.connection() as conn:` to close the database automatically.
    """
    normalized_query = query.strip().lower()
    if not normalized_query:
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "product search query must not be empty",
        }
    if max_price_usd is not None and not max_price_usd > 0:
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "maximum price must be greater than zero",
        }

    with db.connection() as conn:
        store_record = db.get_store_by_name(conn, store) if store is not None else None
        if store is not None and store_record is None:
            return {
                "ok": False,
                "error": "not_found",
                "reason": f"no store matching '{store}'",
            }
        candidates = db.list_products(
            conn,
            store_id=store_record.id if store_record is not None else None,
        )

    query_tokens = normalized_query.split()
    matches = []
    for product in candidates:
        searchable_text = f"{product.title} {product.description}".lower()
        if not all(token in searchable_text for token in query_tokens):
            continue
        if max_price_usd is not None and product.price_usd > max_price_usd:
            continue
        matches.append(product)

    # Stable ordering makes repeated evaluations comparable even when the
    # underlying database happens to return rows in a different order.
    matches.sort(key=lambda product: (product.price_usd, product.id))
    result_limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    products = [
        {
            "product_id": product.id,
            "store_id": product.store_id,
            "title": product.title,
            "price_usd": product.price_usd,
        }
        for product in matches[:result_limit]
    ]
    return {"ok": True, "products": products, "count": len(products)}


def list_my_orders(ctx: AuthContext) -> dict[str, Any]:
    """List recent orders in the caller's own scope. Risk tier: read.

    Role behavior, straight from the access matrix in SPEC.md:
        - shopper: the caller's own orders.
        - merchant: the caller's store's orders (ctx.store_id).
        - support: support staff have no orders of their own and look up
          specific orders with get_order instead, so return {"ok": False,
          "error": "invalid_argument", "reason": ...} saying exactly that.

    Returns:
        For shopper and merchant: {"ok": True, "orders": [...],
        "count": <len(orders)>} where each order is
        agent.db.Order.to_public_dict() and the list holds at most
        DEFAULT_ORDER_LIMIT orders, newest first (agent.db.list_orders_for_user
        and list_orders_for_store already sort and limit this way).

    Implementation notes:
        No permission check is needed beyond the role dispatch, because the
        scope is baked into which query you run. That is the point of the
        tool: the model cannot ask for someone else's orders through it.
    """
    if ctx.role == "support":
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "support staff have no orders of their own; use get_order instead",
        }

    with db.connection() as conn:
        if ctx.role == "shopper":
            orders = db.list_orders_for_user(conn, ctx.user_id, DEFAULT_ORDER_LIMIT)
        else:
            # AuthContext guarantees that merchant sessions have a store id,
            # keeping the database query scoped before any records are read.
            assert ctx.store_id is not None
            orders = db.list_orders_for_store(conn, ctx.store_id, DEFAULT_ORDER_LIMIT)

    public_orders = [order.to_public_dict() for order in orders]
    return {"ok": True, "orders": public_orders, "count": len(public_orders)}


def cancel_order(ctx: AuthContext, order_id: int, reason: str) -> dict[str, Any]:
    """Cancel an order. Risk tier: write.

    This is the homework's write tool, and it must enforce two independent
    rules in this order:

    1. The access matrix (scope): use agent.auth.can_cancel_order. Shoppers
       may cancel only their own orders, merchants only their own store's
       orders, support any order. On failure return
       agent.auth.permission_denied(...) with a reason naming the role and
       the order id. Scope is checked before the status rule so that an
       out-of-scope caller learns nothing about the order's state.
    2. The pre-shipment rule (facts.yaml `cancel_cutoff`): only orders whose
       status is exactly "placed" can be cancelled, for every role. If the
       order is in scope but its status is not "placed", return
       {"ok": False, "error": "not_eligible", "reason": ...} that names the
       current status and states that orders can be cancelled only before
       shipment.

    Args:
        ctx: The caller's auth context.
        order_id: The order to cancel.
        reason: Free-text reason from the user; not validated.

    Returns:
        If no order has this id: {"ok": False, "error": "not_found",
        "reason": ...}.
        On success: {"ok": True, "order_id": order_id, "status": "cancelled"}
        after persisting the new status with agent.db.set_order_status.

    Implementation notes:
        Fetch with agent.db.get_order. Note the argument order of
        can_cancel_order(ctx, order_user_id, order_store_id).

    The Module 4 kill switch is checked first (before the scope and
    status rules and before your code), so that a paused write tool touches
    nothing. It is provided; the default ("off") returns None and falls
    through to your implementation.
    """
    paused = kill_switch("cancel_order")
    if paused is not None:
        return {"ok": False, "error": "paused", "reason": paused}

    with db.connection() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            return {
                "ok": False,
                "error": "not_found",
                "reason": f"no order #{order_id}",
            }
        if not can_cancel_order(ctx, order.user_id, order.store_id):
            # Scope is deliberately checked before status so an unauthorized
            # caller cannot infer whether another person's order has shipped.
            return permission_denied(
                f"role '{ctx.role}' (user {ctx.user_id}) may not cancel order #{order_id}"
            )
        if order.status != "placed":
            return {
                "ok": False,
                "error": "not_eligible",
                "reason": (
                    f"order #{order_id} has status '{order.status}'; "
                    "orders can be cancelled only before shipment"
                ),
            }

        db.set_order_status(conn, order_id, "cancelled")

    return {"ok": True, "order_id": order_id, "status": "cancelled"}


def refund_my_order(ctx: AuthContext, order_id: int, reason: str) -> dict[str, Any]:
    """Request a full refund for one shopper order. Risk tier: write.

    The tool uses the complete order total, enforces ownership before
    disclosing order details, and applies the same delivery-window and $100
    approval rules as ``issue_refund``. Successful results include the dates
    and policy inputs behind the decision so the agent can explain it.

    Args:
        ctx: The authenticated shopper context.
        order_id: The order whose complete total should be refunded.
        reason: The shopper's reason for requesting the refund.

    Returns:
        The canonical refund result. Eligible refunds at or below $100 are
        auto-approved; larger refunds are queued for human approval. Expected
        lookup, permission, eligibility, validation, and pause failures use
        the repository's standard structured error format.
    """
    result = _process_refund(ctx, order_id, amount_usd=None, reason=reason)
    if not result["ok"] and result.get("error") != "not_eligible":
        return result

    timing = _refund_timing_details(order_id)
    if not result["ok"]:
        status = timing["order_status"]
        delivered_at = timing["delivered_at"]
        days = timing["days_since_delivery"]
        window = timing["return_window_days"]
        if status != "delivered":
            explanation = (
                f"order #{order_id} has status '{status}'; refunds require a delivered order"
            )
        elif delivered_at is None:
            explanation = f"order #{order_id} has no delivery date"
        elif days is not None and days > window:
            explanation = (
                f"order #{order_id} was delivered on {delivered_at} ({days} calendar days "
                f"ago), outside the applicable {window}-day return window"
            )
        elif days is not None and days < 0:
            explanation = (
                f"order #{order_id} has a future delivery date {delivered_at}; "
                "the refund eligibility record is inconsistent"
            )
        else:
            explanation = (
                f"order #{order_id} is marked refund-ineligible despite a delivery date "
                "inside the calculated window; a human must review the inconsistent record"
            )
        result.update(timing)
        result["reason"] = explanation
        return result

    facts = load_facts()
    approval_reason = (
        "queued for human approval because it exceeds the "
        f"${facts['refund_auto_approve_threshold_usd']} threshold"
        if result["status"] == "queued_for_approval"
        else "automatically approved because it is at or below the "
        f"${facts['refund_auto_approve_threshold_usd']} threshold"
    )
    result.update(
        {
            **timing,
            "decision_reason": (
                f"order #{order_id} was delivered on {timing['delivered_at']} "
                f"({timing['days_since_delivery']} calendar days ago), within the "
                f"{timing['return_window_days']}-day return window; the full "
                f"${result['amount_usd']:.2f} refund was {approval_reason}"
            ),
        }
    )
    return result


def find_order(ctx: AuthContext, query: str) -> dict[str, Any]:
    """Search the caller's orders by product name. Risk tier: read.

    Takes a natural-language query (e.g., "earmuffs I bought last week")
    and searches the authenticated user's orders for products whose name
    matches. Use fuzzy string matching (e.g., thefuzz.fuzz.partial_ratio
    or case-insensitive substring matching) to find orders whose product name is close to the
    query.

    Access rules: a shopper searches only the shopper's own orders, a
    merchant searches orders from the merchant's store, and support staff
    can search any orders. Use agent.db.list_order_search_candidates with
    user_id=ctx.user_id for shoppers, store_id=ctx.store_id for merchants,
    or all_orders=True only for support. Derive the scope from ctx, never
    from the query; reject unsupported roles or missing required identity.
    Use agent.db.list_products to map product IDs to product titles.

    The helper returns the complete authorised scope, newest first with
    order ID descending as the tie-breaker. Match product names first,
    preserve that order, then return at most five matches. Do not search
    only the 20 most recent orders. Convert matches with to_public_dict().

    Args:
        ctx: The caller's auth context.
        query: A natural-language description of the product.

    Returns:
        {"ok": True, "orders": [...]} with a list of matching orders
        (at most 5), each as the dict returned by agent.db. If no orders
        match, return {"ok": True, "orders": []}.
    """
    normalized_query = query.strip().lower()
    if not normalized_query:
        return {"ok": True, "orders": []}

    with db.connection() as conn:
        if ctx.role == "shopper":
            candidates = db.list_orders_for_user(conn, ctx.user_id, limit=None)
        elif ctx.role == "merchant":
            assert ctx.store_id is not None
            candidates = db.list_orders_for_store(conn, ctx.store_id, limit=None)
        else:
            candidates = db.list_orders_for_user(conn, None, limit=None)

        products_by_id = {product.id: product for product in db.list_products(conn)}

    scored_orders = []
    for order in candidates:
        product = products_by_id.get(order.product_id)
        if product is None:
            # Inconsistent generated data should not make the whole lookup
            # crash; an order without a product name simply cannot match.
            continue
        score = fuzz.partial_ratio(product.title.lower(), normalized_query)
        if score >= 70:
            scored_orders.append((score, order))

    scored_orders.sort(key=lambda match: (-match[0], -match[1].ordered_at.toordinal(), -match[1].id))
    return {
        "ok": True,
        "orders": [order.to_public_dict() for _, order in scored_orders[:5]],
    }
