from toyshop.receipt import render


def test_render_plain(cart):
    cart.add("A1", 2)
    assert render(cart) == "2 x Apple $2.40\nTOTAL $2.40"


def test_render_with_coupon(cart):
    cart.add("C3")
    text = render(cart, "SAVE25")
    assert "Discount (SAVE25) -$4.00" in text
    assert text.endswith("TOTAL $11.99")
