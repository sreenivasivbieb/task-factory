# Amounts with a currency sign and thousands separators

Merchants paste prices from spreadsheets as `"$1,234.50"`. Extend `toyshop/money.py`.

## Parsing: `to_cents(amount)`

Also accept:

- an optional `$` directly after the optional minus sign: `"$12.50"` → `1250`, `"-$0.05"` → `-5`;
- commas as thousands separators in the whole-number part, in groups of **exactly three** digits
  after the first group, which has one to three digits:
  `"1,234.50"` → `123450`, `"$12,345,678"` → `1234567800`, `"999,999"` → `99999900`.

Reject with `ValueError`:

- badly grouped numbers: `"1,23"`, `"12,34.5"`, `",123"`, `"1,,234"`, `"1234,567"`, `"1,234,"`;
- a sign or `$` with no digits: `"$"`, `"-$"`, `"$.50"`;
- a `$` before the minus sign: `"$-5"`.

Everything that was valid before stays valid with the same result.

## Formatting: `format_cents(cents, grouping=False)`

- With `grouping=True` the whole-number part gets thousands separators:
  `format_cents(123456789, grouping=True)` → `"$1,234,567.89"`,
  `format_cents(-100000, grouping=True)` → `"-$1,000.00"`, `format_cents(99999, grouping=True)` → `"$999.99"`.
- Without it (the default) the output is unchanged.
- Parsing the grouped output gives the amount back: `to_cents(format_cents(n, grouping=True)) == n`.
