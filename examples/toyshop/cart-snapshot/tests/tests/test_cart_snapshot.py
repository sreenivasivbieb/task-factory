import json

import pytest

from toyshop.cart import MAX_QTY, Cart
from toyshop.errors import InvalidQuantity, UnknownProduct


def test_to_dict_sorted_and_versioned(cart):
    cart.add("C3")
    cart.add("A1", 2)
    assert cart.to_dict() == {"version": 1, "lines": [{"sku": "A1", "qty": 2}, {"sku": "C3", "qty": 1}]}
    assert Cart(cart.catalog).to_dict() == {"version": 1, "lines": []}


def test_round_trip_through_json(cart, catalog):
    cart.add("B2", 5)
    cart.add("A1")
    restored = Cart.from_dict(catalog, json.loads(json.dumps(cart.to_dict())))
    assert restored is not cart
    assert restored.lines() == [("A1", 1), ("B2", 5)]
    assert Cart.from_dict(catalog, {"version": 1}).is_empty()


def test_version_is_checked(catalog):
    for bad, shown in [({"version": 2, "lines": []}, "2"), ({"lines": []}, "None")]:
        with pytest.raises(ValueError, match=shown):
            Cart.from_dict(catalog, bad)


def test_lines_follow_add_rules(catalog):
    with pytest.raises(UnknownProduct):
        Cart.from_dict(catalog, {"version": 1, "lines": [{"sku": "ZZ", "qty": 1}]})
    for qty in (0, -3, MAX_QTY + 1):
        with pytest.raises(InvalidQuantity):
            Cart.from_dict(catalog, {"version": 1, "lines": [{"sku": "A1", "qty": qty}]})
    assert Cart.from_dict(catalog, {"version": 1, "lines": [{"sku": "A1", "qty": MAX_QTY}]}).quantity("A1") == MAX_QTY


def test_quantity_must_be_a_real_int(catalog):
    for qty in (True, 2.0, "2", None):
        with pytest.raises(InvalidQuantity):
            Cart.from_dict(catalog, {"version": 1, "lines": [{"sku": "A1", "qty": qty}]})


def test_repeated_skus_add_up_within_the_limit(catalog):
    data = {"version": 1, "lines": [{"sku": "A1", "qty": 2}, {"sku": "B2", "qty": 1}, {"sku": "A1", "qty": 3}]}
    assert Cart.from_dict(catalog, data).lines() == [("A1", 5), ("B2", 1)]
    over = {"version": 1, "lines": [{"sku": "A1", "qty": 60}, {"sku": "A1", "qty": 40}]}
    with pytest.raises(InvalidQuantity):
        Cart.from_dict(catalog, over)
