# Buy one get one free on SKU B2

Add a buy-one-get-one-free (BOGO) discount for the product with SKU `B2`.

- In `toyshop/pricing.py`, extend `line_total` to accept the line's SKU as an
  optional third argument: `line_total(price_cents, qty, sku=None)`.
- Add a module-level set `BOGO_SKUS` (initially just `"B2"`) and a
  `BOGO_FREE_EVERY` constant (initially `2`).
- When `sku` is in `BOGO_SKUS`, one item is free for every `BOGO_FREE_EVERY`
  items on the line: a line of `qty` items is charged for
  `qty - qty // BOGO_FREE_EVERY` items. With the default value this means
  1 item is full price, 2 items cost one item, 3 items cost two, and 4 items
  cost two.
- `sku` is optional: when it is omitted, `line_total` keeps its current
  behaviour (`price_cents * qty`).
- The discount applies only to SKUs in `BOGO_SKUS`; every other product is
  unchanged.
- `subtotal(cart)` must pass each line's SKU to `line_total`, so `total(...)`
  uses the discounted amount.
- Update `toyshop/receipt.py` so each line and the total show the discounted
  amount for B2.

All amounts are integer cents; the BOGO discount needs no rounding.
