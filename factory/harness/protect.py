"""Protect existing tests from the solver.

Before grading, every existing test file (and test config such as conftest.py)
that the agent changed is put back to the starting version, so an agent cannot
pass by weakening or deleting tests. The same rule is used by our harness and
by the Harbor verifier script.
"""

from __future__ import annotations

import re

TEST_PATH_RE = (r"(^|/)(tests?|__tests__|specs?|testing)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$"
                r"|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)conftest\.py$|(^|/)src/test/")
_RX = re.compile(TEST_PATH_RE)


def is_test_path(path: str) -> bool:
    return bool(_RX.search(path))


# POSIX shell snippet: restore tracked test files at the base tag (used inside containers).
RESTORE_SH = (
    "git ls-tree -r --name-only factory-base | grep -E '" + TEST_PATH_RE + "' > .factory/protected.txt || true; "
    "if [ -s .factory/protected.txt ]; then "
    "git diff --name-only factory-base -- $(cat .factory/protected.txt) > .factory/tampered.txt 2>/dev/null || true; "
    "[ -s .factory/tampered.txt ] && git checkout factory-base -- $(cat .factory/tampered.txt); fi; "
    "cat .factory/tampered.txt 2>/dev/null || true"
)
