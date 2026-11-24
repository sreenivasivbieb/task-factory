"""Plain-text receipts."""

from .cart import Cart
from .money import format_cents
from .pricing import coupon_discount, line_total, subtotal


def render(cart: Cart, coupon: str | None = None) -> str:
    lines = []
    for sku, qty in cart.lines():
        product = cart.catalog.get(sku)
        amount = line_total(product.price_cents, qty)
        lines.append(f"{qty} x {product.name} {format_cents(amount)}")
    sub = subtotal(cart)
    discount = coupon_discount(sub, coupon)
    if discount:
        lines.append(f"Discount ({coupon}) -{format_cents(discount)}")
    lines.append(f"TOTAL {format_cents(sub - discount)}")
    return "\n".join(lines)


def render_order(order) -> str:
    """The receipt of a checked-out order; same text as ``render`` for the same cart and coupon."""
    lines = [f"{line.qty} x {line.name} {format_cents(line.total_cents)}" for line in order.lines]
    if order.discount_cents:
        lines.append(f"Discount ({order.coupon}) -{format_cents(order.discount_cents)}")
    lines.append(f"TOTAL {format_cents(order.total_cents)}")
    return "\n".join(lines)
