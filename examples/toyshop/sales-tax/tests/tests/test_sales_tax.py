import pytest

from toyshop.errors import ShopError, UnknownRegion
from toyshop.pricing import total
from toyshop.receipt import render
from toyshop.tax import TAX_RATES, tax_cents


def test_rates_table():
    assert TAX_RATES == {"CA": 725, "NY": 400, "TX": 625, "OR": 0}


def test_tax_rounds_half_up_to_a_cent():
    assert tax_cents(10000, "CA") == 725
    assert tax_cents(1000, "CA") == 73      # 72.5 -> 73
    assert tax_cents(1599, "NY") == 64      # 63.96 -> 64
    assert tax_cents(1599, "TX") == 100     # 99.94 -> 100
    assert tax_cents(4, "TX") == 0          # 0.25 -> 0
    assert tax_cents(12345678, "CA") == 895062
    assert tax_cents(5000, "OR") == 0


def test_region_is_case_insensitive():
    assert tax_cents(10000, "ny") == tax_cents(10000, "NY") == 400


def test_unknown_region():
    with pytest.raises(UnknownRegion, match="ZZ"):
        tax_cents(100, "ZZ")
    assert issubclass(UnknownRegion, ShopError)


def test_total_taxes_the_discounted_subtotal(cart):
    cart.add("C3", 1)                       # 1599
    assert total(cart) == 1599
    assert total(cart, region="CA") == 1599 + 116
    # SAVE25: 1599 - 400 = 1199, CA tax 86.9 -> 87
    assert total(cart, "SAVE25", "CA") == 1199 + 87
    assert total(cart, "SAVE25") == 1199


def test_total_unknown_region(cart):
    cart.add("A1")
    with pytest.raises(UnknownRegion):
        total(cart, region="Mars")


def test_receipt_shows_tax_line_before_total(cart):
    cart.add("C3")
    text = render(cart, region="ca")
    assert text.splitlines()[-2:] == ["Tax (CA 7.25%) $1.16", "TOTAL $17.15"]
    assert render(cart, "SAVE25", "NY").splitlines()[-3:] == [
        "Discount (SAVE25) -$4.00", "Tax (NY 4.00%) $0.48", "TOTAL $12.47"]


def test_receipt_without_tax(cart):
    cart.add("A1", 2)
    assert render(cart, region="OR") == "2 x Apple $2.40\nTOTAL $2.40"
    assert render(cart, region="TX").splitlines()[-2:] == ["Tax (TX 6.25%) $0.15", "TOTAL $2.55"]
