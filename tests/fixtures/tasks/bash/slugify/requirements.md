# Slugify text

Add `slugify TEXT` to `lib/text.sh`. It prints TEXT as a URL slug:

- letters are lower-cased; ASCII letters and digits are kept;
- every run of other characters (spaces, punctuation) becomes one `-`;
- no `-` at the start or the end.

When the result would be empty (for example for `"  !! "`), `slugify` prints nothing
and returns status 1. Otherwise it returns 0.
