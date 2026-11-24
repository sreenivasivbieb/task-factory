"""Protect the grading setup from the solver.

Grading must measure the solver's source change, nothing else. Before grading,
every *protected* file the agent touched is undone:

- changed or deleted protected files are put back to the starting version;
- protected files the agent added are removed (e.g. a new ``conftest.py`` that
  marks every test as passed, or a new ``pytest.ini`` that deselects tests).

Protected = test files and folders, plus files that control how tests are
collected or run (``conftest.py``, ``pytest.ini``, ``pyproject.toml``,
``package.json``, jest/vitest/mocha config, ``sitecustomize.py``, ``*.pth``,
build files such as ``go.mod`` or ``Cargo.toml``). Dependencies are installed
when the image is built, never at grading time, so restoring build files cannot
remove anything the grading run uses.

The same rule applies to a task's reference solution: it may not change a
protected file (gate G1), otherwise no solver could pass after the restore.
Hidden tests are written *after* the restore, so a task may still ship its own
``conftest.py`` or test helpers in ``tests/files``.

The same shell snippet runs in our harness and in the Harbor verifier script.
"""

from __future__ import annotations

import re

TEST_PATH_RE = (r"(^|/)(tests?|__tests__|specs?|testing)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$"
                r"|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)conftest\.py$|(^|/)src/test/")
CONFIG_PATH_RE = (r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|sitecustomize\.py|usercustomize\.py"
                  r"|package\.json|\.mocharc[^/]*|(jest|vitest|playwright)\.config\.[cm]?[jt]s"
                  r"|go\.mod|go\.sum|Cargo\.toml|Cargo\.lock|pom\.xml|(build|settings)\.gradle(\.kts)?)$|\.pth$")
PROTECTED_RE = f"{TEST_PATH_RE}|{CONFIG_PATH_RE}"
_PROTECTED_RX = re.compile(PROTECTED_RE)


def is_protected(path: str) -> bool:
    return bool(_PROTECTED_RX.search(path))


def patch_paths(diff: str) -> list[str]:
    """Every path a unified diff touches (old and new side, /dev/null excluded)."""
    out = set()
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            p = line[4:].split("\t", 1)[0].strip()
            if p != "/dev/null":
                out.add(p[2:] if p[:2] in ("a/", "b/") else p)
        elif line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) >= 4:
                out.update(x[2:] if x[:2] in ("a/", "b/") else x for x in parts[2:4])
    return sorted(out)


def protected_in_patch(diff: str) -> list[str]:
    return [p for p in patch_paths(diff) if is_protected(p)]


# POSIX shell, run in the repo inside a container: undo the agent's changes to protected
# files relative to the base tag and print each path that was undone.
RESTORE_SH = (
    "mkdir -p .factory; git add -A >/dev/null 2>&1; "
    "git diff --cached --no-renames --name-status factory-base > .factory/changes.txt 2>/dev/null; "
    "git reset -q >/dev/null 2>&1; "
    ": > .factory/tampered.txt; "
    "while IFS=\"$(printf '\\t')\" read -r st path; do "
    "[ -n \"$path\" ] || continue; "
    "if printf '%s\\n' \"$path\" | grep -qE '" + PROTECTED_RE + "'; then "
    "if [ \"$st\" = A ]; then rm -f -- \"$path\"; else git checkout -q factory-base -- \"$path\"; fi; "
    "printf '%s\\n' \"$path\" >> .factory/tampered.txt; fi; "
    "done < .factory/changes.txt; "
    "cat .factory/tampered.txt"
)
