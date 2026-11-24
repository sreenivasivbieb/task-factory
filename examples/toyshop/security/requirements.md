# Reject tampered quantities, amounts and product text

Several inputs reach prices and receipts without being checked. Each one lets a
caller change what a customer pays or what a receipt says.

## Threats

1. **Fractional or boolean quantities.** `Cart.add("A1", 1.5)` is accepted, so a
   line can hold half an item and its price is no longer whole cents.
   `Cart.add("A1", True)` is accepted as one item. `Cart.remove` has the same hole.
2. **Non-ASCII digits in amounts.** `to_cents("١٢.٥٠")` (Arabic-Indic digits) and
   `to_cents("１２")` (full-width digits) parse as 1250 and 1200, so input that
   looks like something else to a person passes validation.
3. **Product text that forges receipt lines.** A product named
   `"Apple\nTOTAL $0.00"` prints an extra, fake `TOTAL` line on the receipt.
   Terminal escape sequences (`"\x1b[2J"`) and Unicode bidirectional controls
   (U+202E "right-to-left override", as in "Trojan Source") can hide or reorder
   what is shown.
4. **Non-integer prices.** `Product("X", "X", 1.5)` is accepted.

## Required behaviour

- `Cart.add` and `Cart.remove` accept only real `int` quantities (a `bool` is not
  one). Anything else raises `toyshop.errors.InvalidQuantity` and leaves the cart
  unchanged. All existing limits stay as they are.
- `toyshop.money.to_cents` accepts only the ASCII digits `0` to `9` (plus the
  existing optional leading `-` and one `.`). Anything else raises `ValueError`.
  Amounts that are valid today parse to the same value.
- `Catalog.add` raises `ValueError` when the product's `sku` or `name` contains:
  a control character (Unicode category `Cc`, which includes `\n`, `\r`, `\t` and
  `\x1b`), a line or paragraph separator (U+2028, U+2029), or a bidirectional
  control (U+202A to U+202E, U+2066 to U+2069). The catalog is unchanged.
  Ordinary text, including accented letters and emoji, is still accepted.
- `Catalog.add` raises `ValueError` when `price_cents` is not an `int`
  (a `bool` is not one) or is negative. A price of `0` (a free item) is allowed.
