from toyshop.pricing import line_total, subtotal


def test_bulk_discount_boundary():
    assert line_total(100, 9) == 900
    assert line_total(100, 10) == 900


def test_bulk_discount_rounds_half_up():
    assert line_total(45, 11) == 445


def test_bulk_discount_in_subtotal(cart):
    cart.add("B2", 10)
    cart.add("A1", 1)
    assert subtotal(cart) == 405 + 120
