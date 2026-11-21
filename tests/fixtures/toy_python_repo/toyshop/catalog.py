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

    def add(self, product: Product) -> None:
        if product.price_cents < 0:
            raise ValueError("price must not be negative")
        self._products[product.sku] = product

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
        """Products carrying ``tag``, sorted by SKU."""
        return sorted((p for p in self._products.values() if tag in p.tags), key=lambda p: p.sku)
