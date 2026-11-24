class ShopError(Exception):
    """Base error for toyshop."""


class UnknownProduct(ShopError):
    """Raised when a SKU is not in the catalog."""


class InvalidQuantity(ShopError):
    """Raised when a quantity is zero, negative or too large."""


class CouponError(ShopError):
    """Raised when a coupon code cannot be applied."""


class CheckoutError(ShopError):
    """Raised when a cart cannot be checked out."""


class OutOfStock(CheckoutError):
    """Raised when a cart line wants more units than are in stock."""

    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(f"{sku}: requested {requested}, only {available} in stock")
        self.sku = sku
        self.requested = requested
        self.available = available
