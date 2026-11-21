from toyshop.pricing import line_total


def test_small_line_unchanged():
    assert line_total(120, 3) == 360


def test_bulk_line():
    assert line_total(100, 10) == 900
