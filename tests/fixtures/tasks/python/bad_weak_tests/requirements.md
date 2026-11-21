# Bulk discount on cart lines

Change `toyshop.pricing.line_total(price_cents, qty)` so that a cart line with
**10 or more** items gets **10% off** that line.

- Below 10 items the line total is `price_cents * qty`, unchanged.
- At 10 items or more the line total is `price_cents * qty` minus 10% of that
  amount, where the 10% is rounded half up to a whole cent
  (same rounding as `toyshop.money.percent_of`).
- `toyshop.pricing.subtotal(cart)` and everything built on it (totals, receipts)
  use the discounted line totals.
- Add the module constants `BULK_MIN_QTY = 10` and `BULK_PERCENT = 10` in
  `toyshop/pricing.py`.
