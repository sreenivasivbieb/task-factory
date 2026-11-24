#!/usr/bin/env bash
# run_tests.sh REPORT [TEST_FILE ...]: run every test_* function, write a JUnit XML report.
# With no files, runs t/test_*.sh. Each test runs in its own subshell.
report="$1"; shift
files=("$@")
[[ ${#files[@]} -eq 0 ]] && files=(t/test_*.sh)
mkdir -p "$(dirname "$report")"
xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'; }
cases=""; failed=0; total=0
for f in "${files[@]}"; do
  [[ -f "$f" ]] || { printf 'no such test file: %s\n' "$f" >&2; failed=1; continue; }
  names=$(bash -c "source '$f' >/dev/null 2>&1; declare -F" | awk '{print $3}' | grep '^test_' || true)
  for name in $names; do
    total=$((total + 1))
    if out=$(bash -c "source '$f' && $name" 2>&1); then
      cases+="<testcase classname=\"${f%.sh}\" file=\"$f\" name=\"$name\"/>"
    else
      failed=$((failed + 1))
      cases+="<testcase classname=\"${f%.sh}\" file=\"$f\" name=\"$name\"><failure message=\"failed\">$(printf '%s' "$out" | xml)</failure></testcase>"
    fi
  done
done
printf '<?xml version="1.0"?><testsuites><testsuite name="bash" tests="%s" failures="%s">%s</testsuite></testsuites>\n' \
  "$total" "$failed" "$cases" > "$report"
printf '%s tests, %s failed\n' "$total" "$failed"
[[ $failed -eq 0 ]]
