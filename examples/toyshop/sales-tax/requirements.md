# Regional sales tax on the order total

The shop now sells in several US states and must charge sales tax.

## Tax rates

- Add a new module `toyshop/tax.py` with `TAX_RATES`, a dict from region code to rate in
  **basis points** (1 bp = 0.01 %):
  `{"CA": 725, "NY": 400, "TX": 625, "OR": 0}`.
- Add `tax_cents(amount_cents, region)` to `toyshop/tax.py`. It returns the tax on
  `amount_cents`, **rounded half up** to a whole cent (e.g. 1,000 cents in CA is 72.5 → 73 cents).
- Region codes are case-insensitive: `"ca"` and `"CA"` are the same region.
- An unknown region raises a new error `toyshop.errors.UnknownRegion`, a subclass of
  `ShopError`, whose message contains the region as given.

## Totals

- `toyshop.pricing.total(cart, coupon=None, region=None)`: when a region is given, tax is
  charged on the subtotal **after** the coupon discount and added to the total.
- Without a region the total is exactly what it is today.

## Receipts

- `toyshop.receipt.render(cart, coupon=None, region=None)`: when a region is given and the tax is
  not zero, add the line `Tax (<REGION> <rate>%) <amount>` just before the `TOTAL` line, with the
  region upper-cased and the rate printed with two decimals, e.g. `Tax (CA 7.25%) $1.16`.
- The `TOTAL` line includes the tax.
- A region whose tax is zero (e.g. `OR`) prints no tax line.
- Without a region the receipt is exactly what it is today.
