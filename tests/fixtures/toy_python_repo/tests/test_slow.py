import pytest

from toyshop.cart import Cart
from toyshop.pricing import total


@pytest.mark.slow
def test_many_carts(catalog):
    for i in range(3000):
        cart = Cart(catalog)
        cart.add("A1", 1 + i % 5)
        cart.add("C3", 1)
        assert total(cart) == 120 * (1 + i % 5) + 1599
