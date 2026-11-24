# Tag lookups that scale with large catalogs

`Catalog.by_tag(tag)` looks at every product in the catalog on every call. With a
catalog of tens of thousands of products, a shop that renders tag pages spends
almost all of its time there. The catalog also has no way to remove a product.

## Required behaviour

- `Catalog.by_tag(tag)` must not examine every product. Its cost must depend on
  the number of products carrying `tag`, not on the size of the catalog. It still
  returns the products carrying `tag`, sorted by SKU, and `[]` for a tag no
  product has. Callers must not be able to change the catalog by changing the
  returned list.
- Adding a product whose SKU is already in the catalog replaces it, as today:
  `by_tag` must then reflect the **new** product's tags only.
- New: `Catalog.remove(sku)` removes the product. It raises
  `toyshop.errors.UnknownProduct` for a SKU that is not in the catalog. Afterwards
  the product is gone from `get`, `len`, `search` and `by_tag`.
- Everything else behaves as before, including `search` and the negative-price
  check.

## Load the change must handle

A catalog of 50,000 products spread over 500 tags must answer 20,000 `by_tag`
calls, interleaved with 1,000 product replacements, within a few seconds on an
ordinary machine. Today the same work takes minutes.
