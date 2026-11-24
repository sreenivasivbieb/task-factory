source t/assert.sh
source lib/text.sh

test_upper() {
  assert_eq "HELLO" "$(upper hello)"
}

test_trim() {
  assert_eq "a b" "$(trim "  a b  ")"
}

test_word_count() {
  assert_eq "3" "$(word_count "one two  three")" && assert_eq "0" "$(word_count "   ")"
}
