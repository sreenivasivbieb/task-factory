"""Pricing: line totals, coupons and the cart total."""

from .cart import Cart
from .errors import CouponError
from .money import percent_of

COUPONS = {"SAVE10": 10, "SAVE25": 25}

BOGO_SKUS = frozenset({"B2"})
BOGO_FREE_EVERY = 2  # one item is free for every this many items


def line_total(price_cents: int, qty: int, sku: str | None = None) -> int:
    """Total for one cart line.

    A line whose SKU is in ``BOGO_SKUS`` gets one item free for every
    ``BOGO_FREE_EVERY`` items: two items cost one, three cost two, and so on.
    Other SKUs (and a line with no SKU) are charged ``price_cents * qty``.
    """
    amount = price_cents * qty
    if sku in BOGO_SKUS and qty >= BOGO_FREE_EVERY:
        amount = price_cents * (qty - qty // BOGO_FREE_EVERY)
    return amount


def subtotal(cart: Cart) -> int:
    return sum(line_total(cart.catalog.get(sku).price_cents, qty, sku) for sku, qty in cart.lines())


def coupon_discount(amount_cents: int, code: str | None) -> int:
    """Discount in cents for ``code`` applied to ``amount_cents``."""
    if code is None:
        return 0
    if code not in COUPONS:
        raise CouponError(f"unknown coupon {code!r}")
    return percent_of(amount_cents, COUPONS[code])


def total(cart: Cart, coupon: str | None = None) -> int:
    sub = subtotal(cart)
    return sub - coupon_discount(sub, coupon)
