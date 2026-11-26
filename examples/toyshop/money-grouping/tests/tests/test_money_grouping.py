import pytest

from toyshop.money import format_cents, to_cents

MALFORMED = ["1,23", "12,34.5", ",123", "1,,234", "1234,567", "1,234,", "1,2345",
             "$", "-$", "$.50", "$-5", "$$5", "1,234.567"]


def test_dollar_sign():
    assert to_cents("$12.50") == 1250
    assert to_cents("-$0.05") == -5
    assert to_cents(" $7 ") == 700


def test_thousands_separators_and_old_inputs():
    assert to_cents("1,234.50") == 123450
    assert to_cents("$12,345,678") == 1234567800
    assert to_cents("999,999") == 99999900
    assert to_cents("-$1,000.01") == -100001
    # everything valid before keeps its value
    assert [to_cents(x) for x in ["12", "12.5", "0.07", "-1.25", "1234"]] == [1200, 1250, 7, -125, 123400]


def test_grouping_is_strict():
    assert to_cents("1,234") == 123400
    for bad in MALFORMED:
        with pytest.raises(ValueError):
            to_cents(bad)


def test_format_grouping():
    assert format_cents(123456789, grouping=True) == "$1,234,567.89"
    assert format_cents(-100000, grouping=True) == "-$1,000.00"
    assert format_cents(99999, grouping=True) == "$999.99"
    assert format_cents(123456789) == "$1234567.89"


def test_grouped_round_trip():
    for n in [0, 5, -5, 99999, 100000, 123456789, -987654321]:
        assert to_cents(format_cents(n, grouping=True)) == n
