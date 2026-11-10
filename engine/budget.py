"""Budget semantics for pickup vs delivery.

Delivery totals include packing and delivery fees, and those fees are waived
above a merchant threshold that changes over time. Never assume the product
price is the payable amount; use a priced candidate when one is available.

A quote only counts while it is bound to the same items/context and still fresh
(see ``quote_state``). An expired or mismatched quote is dropped, so a stale
"实付" can never survive into a confirmation prompt.
"""

from __future__ import annotations

from typing import Any, Optional

import quote_state


def is_delivery(req: dict[str, Any]) -> bool:
    return str(req.get("dining") or "").lower() == "delivery"


def payable_fen(cand: Any, req: dict[str, Any]) -> int | None:
    """Return the amount actually charged, or None when it is still unknown."""
    if getattr(cand, "meta", None) and isinstance(getattr(cand, "meta"), dict):
        if cand.meta.get("quoteStale") or cand.meta.get("quoteRejected"):
            return None
    quoted = getattr(cand, "payable_fen", None)
    if isinstance(quoted, int) and quoted > 0 and bool(getattr(cand, "price_verified", False)):
        return quoted
    if is_delivery(req):
        # Delivery totals are only trustworthy once calculate-price has quoted them.
        return None
    return int(getattr(cand, "price_fen", 0) or 0)


def budget_check(cand: Any, req: dict[str, Any]) -> str | None:
    """Return a rejection reason, or None when the candidate is inside budget."""
    budget = int(req.get("budgetFen") or 3500)
    payable = payable_fen(cand, req)
    if payable is None:
        price = int(getattr(cand, "price_fen", 0) or 0)
        # Fee waiver is order-dependent, so treat an unquoted delivery price as
        # a lower bound: reject only when even the goods price exceeds budget.
        if price > budget:
            return f"over_budget:{price}>{budget}"
        return "unverified_delivery_total"
    if payable > budget:
        return f"over_budget_payable:{payable}>{budget}"
    return None


def apply_quote(
    cand: Any,
    quote: dict[str, Any],
    *,
    req: Optional[dict[str, Any]] = None,
    items: Optional[list] = None,
    now: Any = None,
) -> Any:
    """Record a calculate-price quote on a candidate.

    The quote is only promoted to a verified amount when it is well formed, not
    expired, and bound to the candidate's items (when both sides carry them).
    Otherwise the candidate is marked ``quoteStale``/``quoteRejected`` and keeps
    its estimate price, so display and budget logic cannot use the old amount.
    """
    meta = getattr(cand, "meta", None)
    if not isinstance(meta, dict):
        meta = {}
        cand.meta = meta

    check = quote_state.check_quote(
        quote,
        items=items if items is not None else getattr(cand, "items", None),
        context=quote_state.context_of(req) if req else None,
        now=now,
        ttl_seconds=quote_state._quote_ttl(req),
    )
    meta["quoteCheck"] = check.reason
    meta["quoteAgeSeconds"] = check.age_seconds
    meta["quoteItemsBound"] = check.items_bound

    if not check.ok:
        cand.payable_fen = None
        cand.price_verified = False
        if check.expired:
            meta["quoteStale"] = True
        meta["quoteRejected"] = check.reason
        return cand

    payable = quote.get("price")
    product = quote.get("productPrice")
    if isinstance(product, int) and not isinstance(product, bool) and product > 0:
        cand.price_fen = product
    cand.payable_fen = int(payable)
    cand.price_verified = True
    meta.pop("quoteRejected", None)
    meta.pop("quoteStale", None)
    return cand
