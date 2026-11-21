# toyshop

A tiny shop library: a product catalog, carts, pricing with coupons and
plain-text receipts. Amounts are integer cents.

## Features

- Catalog with search by name and by tag
- Cart with quantity limits (at most 99 of one item)
- Coupons `SAVE10` and `SAVE25`
- Text receipts with a discount line

## Roadmap

- Bulk discount: 10% off a cart line when its quantity is 10 or more.
- Minimum subtotal for coupons: a coupon may require a minimum subtotal.
- Price range filter in the catalog (min and max price, inclusive).
- Remove a product from the catalog.

Run the tests with `python -m pytest`.
