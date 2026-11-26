"""Pricing: line totals, coupons and the cart total."""

from .cart import Cart
from .errors import CouponError
from .money import percent_of
from .tax import tax_cents

COUPONS = {"SAVE10": 10, "SAVE25": 25}


def line_total(price_cents: int, qty: int) -> int:
    """Total for one cart line."""
    return price_cents * qty


def subtotal(cart: Cart) -> int:
    return sum(line_total(cart.catalog.get(sku).price_cents, qty) for sku, qty in cart.lines())


def coupon_discount(amount_cents: int, code: str | None) -> int:
    """Discount in cents for ``code`` applied to ``amount_cents``."""
    if code is None:
        return 0
    if code not in COUPONS:
        raise CouponError(f"unknown coupon {code!r}")
    return percent_of(amount_cents, COUPONS[code])


def total(cart: Cart, coupon: str | None = None, region: str | None = None) -> int:
    sub = subtotal(cart)
    due = sub - coupon_discount(sub, coupon)
    if region is not None:
        due += tax_cents(due, region)
    return due
