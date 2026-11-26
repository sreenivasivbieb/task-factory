"""Plain-text receipts."""

from .cart import Cart
from .money import format_cents
from .pricing import coupon_discount, line_total, subtotal
from .tax import rate_label, tax_cents


def render(cart: Cart, coupon: str | None = None, region: str | None = None) -> str:
    lines = []
    for sku, qty in cart.lines():
        product = cart.catalog.get(sku)
        amount = line_total(product.price_cents, qty)
        lines.append(f"{qty} x {product.name} {format_cents(amount)}")
    sub = subtotal(cart)
    discount = coupon_discount(sub, coupon)
    if discount:
        lines.append(f"Discount ({coupon}) -{format_cents(discount)}")
    due = sub - discount
    if region is not None:
        tax = tax_cents(due, region)
        if tax:
            lines.append(f"Tax ({rate_label(region)}) {format_cents(tax)}")
        due += tax
    lines.append(f"TOTAL {format_cents(due)}")
    return "\n".join(lines)
