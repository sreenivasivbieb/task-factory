"""Turn a cart into an order, reserving stock."""

from dataclasses import dataclass

from .cart import Cart
from .errors import CheckoutError, OutOfStock
from .inventory import Inventory
from .pricing import coupon_discount, line_total


@dataclass(frozen=True)
class OrderLine:
    sku: str
    name: str
    qty: int
    unit_cents: int
    total_cents: int


@dataclass(frozen=True)
class Order:
    lines: tuple[OrderLine, ...]
    subtotal_cents: int
    discount_cents: int
    total_cents: int
    coupon: str | None = None


def checkout(cart: Cart, inventory: Inventory, coupon: str | None = None) -> Order:
    if cart.is_empty():
        raise CheckoutError("cart is empty")
    lines = []
    for sku, qty in cart.lines():  # SKU order, so the first short line is reported
        available = inventory.available(sku)
        if qty > available:
            raise OutOfStock(sku, qty, available)
        product = cart.catalog.get(sku)
        lines.append(OrderLine(sku, product.name, qty, product.price_cents, line_total(price_cents=product.price_cents, qty=qty)))
    subtotal = sum(line.total_cents for line in lines)
    discount = coupon_discount(subtotal, coupon)  # may raise CouponError: nothing changed yet
    for line in lines:
        inventory.take(line.sku, line.qty)
    cart.clear()
    return Order(tuple(lines), subtotal, discount, subtotal - discount, coupon)
