import pytest

from toyshop.catalog import Product
from toyshop.errors import UnknownProduct


def test_get(catalog):
    assert catalog.get("A1").name == "Apple"


def test_get_unknown(catalog):
    with pytest.raises(UnknownProduct):
        catalog.get("ZZ")


def test_len(catalog):
    assert len(catalog) == 3


def test_negative_price_rejected(catalog):
    with pytest.raises(ValueError):
        catalog.add(Product("N1", "Bad", -1))


def test_search_case_insensitive(catalog):
    assert [p.sku for p in catalog.search("AN")] == ["B2", "C3"]


def test_by_tag(catalog):
    assert [p.sku for p in catalog.by_tag("fruit")] == ["A1", "B2"]
