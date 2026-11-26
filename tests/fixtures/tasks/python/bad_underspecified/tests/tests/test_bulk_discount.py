from toyshop.pricing import BULK_MIN_QTY, BULK_PERCENT, line_total


def test_bulk_constants():
    assert (BULK_MIN_QTY, BULK_PERCENT) == (10, 10)


def test_bulk_discount_boundary():
    assert line_total(100, BULK_MIN_QTY - 1) == 900
    assert line_total(100, BULK_MIN_QTY) == 900
