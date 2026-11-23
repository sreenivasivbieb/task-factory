from toyshop.pricing import line_total, subtotal, total
from toyshop.receipt import render


def test_bogo_line_total_even_quantity():
    assert line_total(45, 2, "B2") == 45
    assert line_total(45, 4, "B2") == 90


def test_bogo_line_total_odd_quantity():
    assert line_total(45, 1, "B2") == 45
    assert line_total(45, 3, "B2") == 90
    assert line_total(45, 5, "B2") == 135


def test_bogo_only_applies_to_listed_skus():
    assert line_total(120, 2, "A1") == 240
    assert line_total(45, 2, "B2") == 45


def test_line_total_without_sku_is_unchanged():
    assert line_total(120, 3) == 360
    assert line_total(45, 2, "B2") == 45


def test_bogo_in_subtotal_and_total(cart):
    cart.add("B2", 2)
    cart.add("A1", 2)
    assert subtotal(cart) == 45 + 240
    assert total(cart) == 285


def test_bogo_in_receipt(cart):
    cart.add("B2", 2)
    text = render(cart)
    assert "2 x Banana $0.45" in text
    assert text.endswith("TOTAL $0.45")