# Example tasks for the toyshop repo

Three hand-written task drafts, one per kind of challenge, for the toy repo in
`tests/fixtures/toy_python_repo`. Each is an ordinary draft folder
(`task.json`, `requirements.md`, `after/` with the reference change, `tests/`
with the hidden tests), so the factory treats them exactly like a generated task:
every gate must pass before one is added.

| Draft | Kind | What the solver must do | Hidden tests |
| --- | --- | --- | --- |
| `checkout/` | feature, end to end | new `Inventory` and `checkout()` that turn a cart into an order; stock is checked for **every** line before anything changes, an unknown coupon also changes nothing, the cart is emptied, a new `OutOfStock` error names the first short line in SKU order; `Cart.clear()` and an order receipt that matches the cart receipt | 9 |
| `security/` | security fix | reject fractional and boolean quantities (price tampering), non-ASCII digits in amounts (`"١٢.٥٠"`), product text that could forge or hide receipt lines (newlines, terminal escapes, Unicode line separators, bidi overrides as in "Trojan Source"), and non-integer prices | 27 |
| `scalability/` | scalability fix | `Catalog.by_tag` must stop scanning the whole catalog (an index that stays right when a product is replaced), plus `Catalog.remove`; a load test runs 20,000 lookups and 1,000 replacements on 50,000 products within 5 s, and fails fast (not after minutes) when the code still scans | 4 |

Validate them (Docker, strict gates):

```sh
cp -r tests/fixtures/toy_python_repo /tmp/toyshop && git -C /tmp/toyshop init -q \
  && git -C /tmp/toyshop add -A && git -C /tmp/toyshop commit -qm init
uv run factory load --repo /tmp/toyshop && uv run factory inspect
cat > strict.toml <<'EOF'
[gates]
stability_repeats = 3
mutations = ["empty", "revert_half", "flip_comparison", "swap_arguments", "drop_error_branch", "hardcode_expected"]
EOF
for t in checkout security scalability; do
  uv run factory tasks add examples/toyshop/$t --pending --config strict.toml
done
```

## What writing them taught us

- The strict G6 gate caught two weak spots in the first versions. The security
  tests never checked that a price of 0 is still allowed, so a mutant that also
  rejected 0 passed; a test and a sentence in the requirements were added. In the
  checkout reference, `line_total(qty, price)` with swapped arguments is an
  *equivalent* mutant (it only multiplies), which no test can catch; the reference
  now passes keyword arguments, which is also clearer.
- A behaviour that already works today (replacing a product keeps `by_tag`
  right, because it scans) cannot be its own hidden test: G3 needs every hidden
  test to fail without the change. It is checked inside the `remove` test instead.
- The load test checks the clock inside its loop and stops at the budget, so the
  validator's "fails without the patch" run takes 5 s instead of minutes.
- Solvers wrote their own test files, one with the same name as a hidden test.
  The harness removed or restored all of them before grading (`tests_tampered`),
  so the grade came only from the hidden tests.
