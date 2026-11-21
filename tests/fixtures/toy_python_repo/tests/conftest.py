import pytest

from toyshop.cart import Cart
from toyshop.catalog import Catalog, Product


@pytest.fixture
def catalog():
    c = Catalog()
    c.add(Product("A1", "Apple", 120, frozenset({"fruit"})))
    c.add(Product("B2", "Banana", 45, frozenset({"fruit"})))
    c.add(Product("C3", "Coffee beans", 1599, frozenset({"pantry"})))
    return c


@pytest.fixture
def cart(catalog):
    return Cart(catalog)
