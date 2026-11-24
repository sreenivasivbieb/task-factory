"""Units in stock per SKU."""


class Inventory:
    def __init__(self) -> None:
        self._stock: dict[str, int] = {}

    def set_stock(self, sku: str, qty: int) -> None:
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
            raise ValueError(f"stock must be a whole number of 0 or more, got {qty!r}")
        self._stock[sku] = qty

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def take(self, sku: str, qty: int) -> None:
        """Remove ``qty`` units; the caller has checked they are available."""
        self._stock[sku] = self.available(sku) - qty
