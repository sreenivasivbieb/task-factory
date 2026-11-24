#!/usr/bin/env bash
# Small text helpers.

upper() {
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]'
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

word_count() {
  if [[ -z "$(trim "$1")" ]]; then
    printf '0'
    return 0
  fi
  printf '%s' "$1" | wc -w | tr -d ' '
}
