import pytest

from toyshop.money import format_cents, percent_of, to_cents


def test_to_cents_whole():
    assert to_cents("12") == 1200


def test_to_cents_decimal():
    assert to_cents("12.5") == 1250
    assert to_cents("0.07") == 7


def test_to_cents_negative():
    assert to_cents("-1.25") == -125


def test_to_cents_rejects_bad_input():
    for bad in ["", "abc", "1.234", "1.2.3"]:
        with pytest.raises(ValueError):
            to_cents(bad)


def test_format_cents():
    assert format_cents(1250) == "$12.50"
    assert format_cents(5) == "$0.05"
    assert format_cents(-5) == "-$0.05"


def test_percent_of_rounds_half_up():
    assert percent_of(1000, 10) == 100
    assert percent_of(45, 10) == 5  # 4.5 -> 5
