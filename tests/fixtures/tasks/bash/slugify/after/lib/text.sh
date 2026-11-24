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

slugify() {
  local s
  s=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9]\{1,\}/-/g' -e 's/^-//' -e 's/-$//')
  if [[ -z "$s" ]]; then
    return 1
  fi
  printf '%s' "$s"
}
