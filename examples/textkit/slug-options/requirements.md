# Slug separator and length options

File names and CSS classes need slugs with other separators. Extend `slugify` in `lib/slug.js`.

- The second argument may now be an options object `{ maxLength = 64, separator = '-' }`.
  A number is still accepted and means `maxLength`, as today: `slugify('a b c d e f', 5)` → `'a-b-c'`.
- `separator` must be one of `'-'`, `'_'` or `'.'`; anything else throws a `RangeError`.
  The separator replaces every run of other characters, exactly like `-` today:
  `slugify('Hello, World!', { separator: '_' })` → `'hello_world'`.
- `maxLength` (from a number or an object) must be a positive integer; otherwise throw a
  `RangeError` (this includes `0`, `-1`, `2.5` and `NaN`).
- Cutting to `maxLength` never leaves a separator at the start or the end:
  `slugify('ab cd', { maxLength: 3, separator: '.' })` → `'ab'`.
- Non-strings still throw `TypeError`; everything else behaves as before.
