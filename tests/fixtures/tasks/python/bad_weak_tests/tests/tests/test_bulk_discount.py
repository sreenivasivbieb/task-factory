from toyshop.pricing import line_total


def test_big_bulk_line():
    assert line_total(100, 20) == 1800
