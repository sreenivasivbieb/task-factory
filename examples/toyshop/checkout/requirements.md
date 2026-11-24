# Checkout with stock reservation

Add stock tracking and a checkout step that turns a cart into an order. Today a
cart can hold any quantity of a product and there is no way to finish a purchase.

## Inventory (`toyshop/inventory.py`)

`Inventory()` holds the number of units in stock per SKU.

- `set_stock(sku, qty)` sets the stock of `sku` to `qty`. `qty` must be an `int`
  of 0 or more (a `bool` is not one), otherwise raise `ValueError`.
- `available(sku)` returns the units in stock; a SKU that was never set has 0.

## Orders and checkout (`toyshop/checkout.py`)

`checkout(cart, inventory, coupon=None)` returns an `Order` and:

1. Raises `toyshop.errors.CheckoutError` if the cart is empty.
2. Checks **every** line before changing anything. If any line wants more units
   than are available, raise `toyshop.errors.OutOfStock` for the **first such line
   in SKU order**. The exception has the attributes `sku`, `requested` and
   `available`, and `str()` of it names the SKU.
3. Prices the cart exactly like `toyshop.pricing.total` (same coupons, same
   rounding). An unknown coupon raises `toyshop.errors.CouponError`.
4. Only when all checks pass: takes the units out of the inventory and empties the
   cart. If checkout raises for any reason, the inventory and the cart are left
   exactly as they were.

`Order` exposes:

- `lines`: one entry per cart line, in SKU order, each with `sku`, `name`, `qty`,
  `unit_cents` and `total_cents`;
- `subtotal_cents`, `discount_cents`, `total_cents` (`total = subtotal - discount`);
- `coupon`: the coupon code used, or `None`.

`CheckoutError` and `OutOfStock` are new subclasses of `toyshop.errors.ShopError`;
`OutOfStock` is also a `CheckoutError`.

## Cart

Add `Cart.clear()`, which removes every line.

## Receipts

Add `toyshop.receipt.render_order(order)`. For the same cart and coupon it returns
exactly the text that `render(cart, coupon)` returns today.
