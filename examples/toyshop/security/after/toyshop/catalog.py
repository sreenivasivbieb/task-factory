"""Product catalog."""

import unicodedata
from dataclasses import dataclass, field

from .errors import UnknownProduct


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int
    tags: frozenset = field(default_factory=frozenset)


# Text that could forge or hide receipt lines: line breaks, terminal escapes, bidi overrides.
_BIDI = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}


def _unsafe_char(text: str) -> str | None:
    for ch in text:
        if unicodedata.category(ch) in ("Cc", "Zl", "Zp") or ch in _BIDI:
            return ch
    return None


class Catalog:
    def __init__(self) -> None:
        self._products: dict[str, Product] = {}

    def add(self, product: Product) -> None:
        price = product.price_cents
        if isinstance(price, bool) or not isinstance(price, int):
            raise ValueError(f"price must be whole cents, got {price!r}")
        if price < 0:
            raise ValueError("price must not be negative")
        for field_name in ("sku", "name"):
            bad = _unsafe_char(getattr(product, field_name))
            if bad is not None:
                raise ValueError(f"product {field_name} contains a forbidden character U+{ord(bad):04X}")
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
