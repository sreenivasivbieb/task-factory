"""Shopping cart."""

from .catalog import Catalog
from .errors import InvalidQuantity

MAX_QTY = 99


class Cart:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._lines: dict[str, int] = {}

    def add(self, sku: str, qty: int = 1) -> None:
        self.catalog.get(sku)  # raises UnknownProduct
        if qty <= 0:
            raise InvalidQuantity(f"quantity must be positive, got {qty}")
        new_qty = self._lines.get(sku, 0) + qty
        if new_qty > MAX_QTY:
            raise InvalidQuantity(f"at most {MAX_QTY} of one item")
        self._lines[sku] = new_qty

    def remove(self, sku: str, qty: int = 1) -> None:
        current = self._lines.get(sku, 0)
        if qty <= 0 or qty > current:
            raise InvalidQuantity(f"cannot remove {qty} of {sku}")
        if qty == current:
            del self._lines[sku]
        else:
            self._lines[sku] = current - qty

    def quantity(self, sku: str) -> int:
        return self._lines.get(sku, 0)

    def lines(self) -> list[tuple[str, int]]:
        """(sku, qty) pairs sorted by SKU."""
        return sorted(self._lines.items())

    def is_empty(self) -> bool:
        return not self._lines
