class ShopError(Exception):
    """Base error for toyshop."""


class UnknownProduct(ShopError):
    """Raised when a SKU is not in the catalog."""


class InvalidQuantity(ShopError):
    """Raised when a quantity is zero, negative or too large."""


class CouponError(ShopError):
    """Raised when a coupon code cannot be applied."""


class UnknownRegion(ShopError):
    """Raised when a tax region is not known."""
