# assert_eq EXPECTED ACTUAL: fail the current test when they differ.
assert_eq() {
  if [[ "$1" != "$2" ]]; then
    printf 'expected [%s] but got [%s]\n' "$1" "$2" >&2
    return 1
  fi
}
