"""Pricing: line totals, coupons and the cart total."""

from .cart import Cart
from .errors import CouponError
from .money import percent_of

COUPONS = {"SAVE10": 10, "SAVE25": 25}

BULK_MIN_QTY = 10
BULK_PERCENT = 10


def line_total(price_cents: int, qty: int) -> int:
    """Total for one cart line. Lines with BULK_MIN_QTY or more items get BULK_PERCENT % off."""
    amount = price_cents * qty
    if qty >= BULK_MIN_QTY:
        amount -= (amount * BULK_PERCENT + 50) // 100
    return amount


def subtotal(cart: Cart) -> int:
    return sum(line_total(cart.catalog.get(sku).price_cents, qty) for sku, qty in cart.lines())


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
