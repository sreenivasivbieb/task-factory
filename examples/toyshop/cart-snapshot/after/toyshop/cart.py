"""Shopping cart."""

from .catalog import Catalog
from .errors import InvalidQuantity

MAX_QTY = 99
SNAPSHOT_VERSION = 1


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

    def to_dict(self) -> dict:
        """Plain data for JSON: ``{"version": 1, "lines": [{"sku": ..., "qty": ...}]}``."""
        return {"version": SNAPSHOT_VERSION, "lines": [{"sku": sku, "qty": qty} for sku, qty in self.lines()]}

    @classmethod
    def from_dict(cls, catalog: Catalog, data: dict) -> "Cart":
        """A new cart from :meth:`to_dict` output; every line is checked like :meth:`add`."""
        version = data.get("version")
        if version != SNAPSHOT_VERSION:
            raise ValueError(f"unsupported cart snapshot version {version!r}")
        cart = cls(catalog)
        for line in data.get("lines", []):
            qty = line.get("qty")
            if type(qty) is not int:
                raise InvalidQuantity(f"quantity must be an integer, got {qty!r}")
            cart.add(line.get("sku"), qty)
        return cart
