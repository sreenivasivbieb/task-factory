source t/assert.sh
source lib/text.sh

test_slugify_lowercases_and_joins_words() {
  assert_eq "hello-world" "$(slugify "Hello World")"
}

test_slugify_collapses_runs_and_trims_dashes() {
  assert_eq "a-b-c2" "$(slugify "  --A!!  b...C2--  ")"
}

test_slugify_fails_when_nothing_is_left() {
  local out
  out=$(slugify "  !! ") && return 1
  assert_eq "" "$out" && assert_eq "x" "$(slugify "x")"
}
