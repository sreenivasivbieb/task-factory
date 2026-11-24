import pytest

from toyshop.checkout import checkout
from toyshop.errors import CheckoutError, CouponError, OutOfStock, ShopError
from toyshop.inventory import Inventory
from toyshop.pricing import coupon_discount, subtotal, total
from toyshop.receipt import render, render_order


def stocked(**units):
    inv = Inventory()
    for sku, qty in units.items():
        inv.set_stock(sku, qty)
    return inv


def test_inventory_counts_and_rejects_bad_stock():
    inv = stocked(A1=5)
    assert inv.available("A1") == 5
    assert inv.available("ZZ") == 0
    inv.set_stock("A1", 0)
    assert inv.available("A1") == 0
    for bad in (-1, 1.5, True, "3"):
        with pytest.raises(ValueError):
            inv.set_stock("A1", bad)


def test_checkout_builds_the_order_in_sku_order(cart):
    cart.add("C3", 1)
    cart.add("A1", 2)
    order = checkout(cart, stocked(A1=5, C3=1))
    assert [(l.sku, l.name, l.qty, l.unit_cents, l.total_cents) for l in order.lines] == [
        ("A1", "Apple", 2, 120, 240),
        ("C3", "Coffee beans", 1, 1599, 1599),
    ]
    assert (order.subtotal_cents, order.discount_cents, order.total_cents, order.coupon) == (1839, 0, 1839, None)


def test_coupon_is_priced_like_pricing_total(cart):
    cart.add("C3", 3)
    cart.add("B2", 7)
    expected_sub, expected_total = subtotal(cart), total(cart, "SAVE25")
    order = checkout(cart, stocked(C3=3, B2=7), coupon="SAVE25")
    assert order.subtotal_cents == expected_sub
    assert order.discount_cents == coupon_discount(expected_sub, "SAVE25")
    assert order.total_cents == expected_total
    assert order.coupon == "SAVE25"


def test_checkout_takes_stock_and_empties_the_cart(cart):
    inv = stocked(A1=5, B2=4)
    cart.add("A1", 5)  # exactly what is left is allowed
    cart.add("B2", 1)
    checkout(cart, inv)
    assert (inv.available("A1"), inv.available("B2")) == (0, 3)
    assert cart.is_empty()


def test_out_of_stock_names_the_first_short_line_in_sku_order(cart):
    cart.add("B2", 5)  # added first, but A1 comes first in SKU order
    cart.add("A1", 3)
    with pytest.raises(OutOfStock) as info:
        checkout(cart, stocked(A1=1))
    err = info.value
    assert (err.sku, err.requested, err.available) == ("A1", 3, 1)
    assert isinstance(err, CheckoutError) and isinstance(err, ShopError)
    assert "A1" in str(err)


def test_a_failed_checkout_changes_nothing(cart):
    inv = stocked(A1=5, C3=0)
    cart.add("A1", 2)
    cart.add("C3", 1)
    with pytest.raises(OutOfStock):
        checkout(cart, inv)  # A1 is fine, C3 is short: A1 must not be taken
    assert inv.available("A1") == 5
    assert cart.lines() == [("A1", 2), ("C3", 1)]

    inv.set_stock("C3", 1)
    with pytest.raises(CouponError):
        checkout(cart, inv, coupon="NOPE")  # an unknown coupon also leaves stock and cart alone
    assert (inv.available("A1"), inv.available("C3")) == (5, 1)
    assert cart.lines() == [("A1", 2), ("C3", 1)]


def test_empty_cart_cannot_be_checked_out(cart):
    with pytest.raises(CheckoutError) as info:
        checkout(cart, stocked(A1=1))
    assert not isinstance(info.value, OutOfStock)


def test_order_receipt_matches_the_cart_receipt(cart):
    for coupon in (None, "SAVE10"):
        cart.add("A1", 3)
        cart.add("C3", 2)
        expected = render(cart, coupon)
        order = checkout(cart, stocked(A1=3, C3=2), coupon=coupon)
        assert render_order(order) == expected


def test_cart_clear_removes_every_line(cart):
    cart.add("A1", 2)
    cart.add("B2", 1)
    cart.clear()
    assert cart.is_empty() and cart.lines() == [] and cart.quantity("A1") == 0
    cart.add("A1", 99)  # the 99 limit starts again from zero
    assert cart.quantity("A1") == 99
