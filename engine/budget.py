"""Budget semantics for pickup vs delivery.

Delivery totals include packing and delivery fees, and those fees are waived
above a merchant threshold that changes over time. Never assume the product
price is the payable amount; use a priced candidate when one is available.
"""

from __future__ import annotations

from typing import Any


def is_delivery(req: dict[str, Any]) -> bool:
    return str(req.get("dining") or "").lower() == "delivery"


def payable_fen(cand: Any, req: dict[str, Any]) -> int | None:
    """Return the amount actually charged, or None when it is still unknown."""
    quoted = getattr(cand, "payable_fen", None)
    if isinstance(quoted, int) and quoted > 0:
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


def apply_quote(cand: Any, quote: dict[str, Any]) -> None:
    """Record a calculate-price quote on a candidate."""
    payable = quote.get("price")
    product = quote.get("productPrice")
    if isinstance(payable, int) and payable > 0:
        cand.payable_fen = payable
    if isinstance(product, int) and product > 0:
        cand.price_fen = product
    cand.price_verified = bool(isinstance(payable, int) and payable > 0)
