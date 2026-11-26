"""Plain-text receipts."""

from .cart import Cart
from .money import format_cents
from .pricing import coupon_discount, line_total, subtotal


def render(cart: Cart, coupon: str | None = None) -> str:
    lines = []
    for sku, qty in cart.lines():
        product = cart.catalog.get(sku)
        amount = line_total(product.price_cents, qty, sku)
        lines.append(f"{qty} x {product.name} {format_cents(amount)}")
    sub = subtotal(cart)
    discount = coupon_discount(sub, coupon)
    if discount:
        lines.append(f"Discount ({coupon}) -{format_cents(discount)}")
    lines.append(f"TOTAL {format_cents(sub - discount)}")
    return "\n".join(lines)
