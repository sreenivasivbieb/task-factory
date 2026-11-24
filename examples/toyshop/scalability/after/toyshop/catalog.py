"""Product catalog."""

from dataclasses import dataclass, field

from .errors import UnknownProduct


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int
    tags: frozenset = field(default_factory=frozenset)


class Catalog:
    def __init__(self) -> None:
        self._products: dict[str, Product] = {}
        self._by_tag: dict[str, set[str]] = {}  # tag -> SKUs carrying it

    def add(self, product: Product) -> None:
        if product.price_cents < 0:
            raise ValueError("price must not be negative")
        old = self._products.get(product.sku)
        if old is not None:
            self._unindex(old)  # a replacement may have different tags
        self._products[product.sku] = product
        for tag in product.tags:
            self._by_tag.setdefault(tag, set()).add(product.sku)

    def remove(self, sku: str) -> None:
        product = self.get(sku)  # raises UnknownProduct
        del self._products[sku]
        self._unindex(product)

    def _unindex(self, product: Product) -> None:
        for tag in product.tags:
            skus = self._by_tag.get(tag)
            if skus is not None:
                skus.discard(product.sku)
                if not skus:
                    del self._by_tag[tag]

    def get(self, sku: str) -> Product:
        try:
            return self._products[sku]
        except KeyError:
            raise UnknownProduct(sku) from None

    def __len__(self) -> int:
        return len(self._products)

    def search(self, text: str) -> list[Product]:
        """Case-insensitive substring search on product names, sorted by SKU."""
        needle = text.lower()
        return sorted((p for p in self._products.values() if needle in p.name.lower()),
                      key=lambda p: p.sku)

    def by_tag(self, tag: str) -> list[Product]:
        """Products carrying ``tag``, sorted by SKU. Cost depends on the matches, not the catalog size."""
        return [self._products[sku] for sku in sorted(self._by_tag.get(tag, ()))]
