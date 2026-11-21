import pytest

from toyshop.errors import CouponError
from toyshop.pricing import coupon_discount, line_total, subtotal, total


def test_line_total():
    assert line_total(120, 3) == 360


def test_subtotal(cart):
    cart.add("A1", 2)
    cart.add("B2", 1)
    assert subtotal(cart) == 285


def test_total_with_coupon(cart):
    cart.add("C3", 1)
    assert total(cart, "SAVE10") == 1599 - 160


def test_unknown_coupon(cart):
    cart.add("A1")
    with pytest.raises(CouponError):
        total(cart, "BOGUS")


def test_no_coupon_no_discount():
    assert coupon_discount(1000, None) == 0
