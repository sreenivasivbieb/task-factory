import pytest

from toyshop.catalog import Catalog, Product
from toyshop.errors import InvalidQuantity
from toyshop.money import format_cents, to_cents
from toyshop.receipt import render
from toyshop.cart import Cart


@pytest.mark.parametrize("qty", [1.5, 2.0, True, "2", None])
def test_add_accepts_only_whole_int_quantities(cart, qty):
    with pytest.raises(InvalidQuantity):
        cart.add("A1", qty)
    assert cart.is_empty()


@pytest.mark.parametrize("qty", [0.5, 1.0, True])
def test_remove_accepts_only_whole_int_quantities(cart, qty):
    cart.add("A1", 2)
    with pytest.raises(InvalidQuantity):
        cart.remove("A1", qty)
    assert cart.quantity("A1") == 2
    cart.remove("A1", 1)  # a real int still works
    assert cart.quantity("A1") == 1


@pytest.mark.parametrize("text", ["١٢.٥٠", "１２", "12.٥", "۳"])
def test_amounts_accept_only_ascii_digits(text):
    with pytest.raises(ValueError):
        to_cents(text)
    assert to_cents("12.50") == 1250 and to_cents("-0.05") == -5 and to_cents(" 7 ") == 700


@pytest.mark.parametrize("name", [
    "Apple\nTOTAL $0.00",   # forges a TOTAL line
    "Apple\rTOTAL $0.00",
    "Apple\tx",
    "\x1b[2JApple",         # clears the terminal
    "Apple TOTAL",     # Unicode line separator
    "Apple TOTAL",
    "Apple‮elppA",     # right-to-left override (Trojan Source)
    "⁦Apple⁩",    # bidi isolate
])
def test_product_names_that_could_forge_receipts_are_rejected(name):
    catalog = Catalog()
    with pytest.raises(ValueError):
        catalog.add(Product("X1", name, 100))
    assert len(catalog) == 0
    catalog.add(Product("X2", "Café ☕ Ünïcode", 100))  # ordinary text still works
    assert catalog.get("X2").name == "Café ☕ Ünïcode"


@pytest.mark.parametrize("sku", ["A\n1", "A\x001", "A‮1"])
def test_skus_with_control_characters_are_rejected(sku):
    catalog = Catalog()
    with pytest.raises(ValueError):
        catalog.add(Product(sku, "Apple", 100))
    assert len(catalog) == 0


@pytest.mark.parametrize("price", [1.5, 100.0, True])
def test_prices_must_be_whole_cents(price):
    catalog = Catalog()
    with pytest.raises(ValueError):
        catalog.add(Product("X1", "Thing", price))
    with pytest.raises(ValueError):
        catalog.add(Product("X1", "Thing", -1))
    assert len(catalog) == 0
    catalog.add(Product("F0", "Free sample", 0))  # zero is a valid price (a free item)
    assert catalog.get("F0").price_cents == 0


def test_a_receipt_cannot_be_forged_end_to_end():
    catalog = Catalog()
    catalog.add(Product("A1", "Apple", 120))
    with pytest.raises(ValueError):
        catalog.add(Product("Z9", "Free\nTOTAL $0.00", 50_000))
    cart = Cart(catalog)
    cart.add("A1", 2)
    receipt = render(cart)
    assert receipt == "2 x Apple $2.40\nTOTAL $2.40"
    assert receipt.count("TOTAL") == 1 and format_cents(to_cents("2.40")) == "$2.40"
