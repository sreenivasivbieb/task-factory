# Filters in template placeholders

Templates need simple formatting, e.g. `Hello {{ name | title }}!`. Extend `lib/template.js`.

- A placeholder may be followed by one or more filters, each written as `| <filter>`:
  `{{ name | upper }}`, `{{name|trim|upper}}`. Spaces around `|` are optional. Filter names are
  lower-case letters only.
- Filters run left to right on the value converted with `String(value)`:
  - `upper` – upper case,
  - `lower` – lower case,
  - `trim` – remove leading and trailing whitespace,
  - `title` – lower-case everything, then upper-case the first character of each word
    (words are separated by whitespace): `"hELLO wORLD"` → `"Hello World"`.
- Any other filter name throws an `Error` whose message is `unknown filter "<name>"`. This
  includes names of built-in object properties such as `constructor` or `toString`.
- A missing value still throws `missing value for <name>`, with or without filters.
- `placeholders(template)` returns only the placeholder names (no filters), in order of first
  appearance: `placeholders('{{a | upper}} {{b}} {{a}}')` → `['a', 'b']`.
- Templates without filters render exactly as before.
