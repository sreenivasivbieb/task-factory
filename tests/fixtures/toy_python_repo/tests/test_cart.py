import pytest

from toyshop.cart import MAX_QTY
from toyshop.errors import InvalidQuantity, UnknownProduct


def test_add_and_quantity(cart):
    cart.add("A1", 2)
    cart.add("A1")
    assert cart.quantity("A1") == 3


def test_add_unknown_sku(cart):
    with pytest.raises(UnknownProduct):
        cart.add("ZZ")


def test_add_non_positive(cart):
    with pytest.raises(InvalidQuantity):
        cart.add("A1", 0)


def test_add_over_max(cart):
    cart.add("A1", MAX_QTY)
    with pytest.raises(InvalidQuantity):
        cart.add("A1")


def test_remove_partial_and_full(cart):
    cart.add("B2", 3)
    cart.remove("B2", 2)
    assert cart.quantity("B2") == 1
    cart.remove("B2")
    assert cart.is_empty()


def test_remove_too_many(cart):
    cart.add("B2", 1)
    with pytest.raises(InvalidQuantity):
        cart.remove("B2", 2)


def test_lines_sorted(cart):
    cart.add("C3")
    cart.add("A1")
    assert cart.lines() == [("A1", 1), ("C3", 1)]
