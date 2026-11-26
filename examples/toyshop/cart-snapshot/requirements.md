# Save and restore carts as plain data

Carts must survive a page reload, so the web layer stores them as JSON. Add two methods to
`toyshop.cart.Cart`:

## `Cart.to_dict()`

Returns `{"version": 1, "lines": [{"sku": "A1", "qty": 2}, ...]}` with the lines sorted by SKU.
An empty cart gives `{"version": 1, "lines": []}`.

## `Cart.from_dict(catalog, data)` (a classmethod)

Builds a **new** cart for `catalog` from such a dict.

- `data["version"]` must be `1`. Any other value, or a missing `"version"`, raises `ValueError`
  whose message contains the version found (e.g. `2` or `None`).
- A missing `"lines"` key means an empty cart.
- Every line goes through the same rules as `Cart.add`: an unknown SKU raises `UnknownProduct`,
  a quantity that is zero, negative or over `MAX_QTY` raises `InvalidQuantity`.
- A quantity must be a real `int`: a `bool`, `float` or string quantity raises `InvalidQuantity`
  (even `2.0` or `"2"`).
- The same SKU may appear on several lines: the quantities add up, and the sum is still limited to
  `MAX_QTY`.
- Restoring a saved cart gives the same lines: `Cart.from_dict(catalog, cart.to_dict()).lines() == cart.lines()`.
