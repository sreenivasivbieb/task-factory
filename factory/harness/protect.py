"""Protect the grading setup from the solver.

Grading must measure the solver's source change, nothing else. Before grading,
every *protected* file the agent touched is undone:

- changed or deleted protected files are put back to the starting version;
- protected files the agent added are removed (e.g. a new ``conftest.py`` that
  marks every test as passed, or a new ``pytest.ini`` that deselects tests).

Protected = the ``test_paths``, ``build_files`` and ``build_patterns`` of every ecosystem in
``ecosystems.toml`` (plus any added in factory.toml): test files and folders, and files that
control how tests are collected or run (``conftest.py``, ``pyproject.toml``, ``package.json``,
jest config, ``go.mod``, ``Cargo.toml``, ...). Dependencies are installed when the image is
built, never at grading time, so restoring build files cannot remove anything the grading
run uses.

The same rule applies to a task's reference solution: it may not change a
protected file (gate G1), otherwise no solver could pass after the restore.
Hidden tests are written *after* the restore, so a task may still ship its own
``conftest.py`` or test helpers in ``tests/files``.

The same shell snippet runs in our harness and in the Harbor verifier script.
"""

from __future__ import annotations

import re

from ..ecosystems import registry


def protected_re() -> str:
    """POSIX ERE (also Python ``re``) matching every protected path, from the ecosystem registry."""
    return registry().protected_re()


def is_protected(path: str, pattern: str | None = None) -> bool:
    return bool(re.search(pattern or protected_re(), path))


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


def protected_in_patch(diff: str, pattern: str | None = None) -> list[str]:
    return [p for p in patch_paths(diff) if is_protected(p, pattern)]


def restore_sh(pattern: str | None = None) -> str:
    """POSIX shell, run in the repo inside a container: undo the agent's changes to protected
    files relative to the base tag and print each path that was undone.

    ``pattern``: the package's own ``env.protected_paths``; default: the current registry."""
    pattern = (pattern or protected_re()).replace("'", "'\\''")
    return (
        "mkdir -p .factory; git add -A >/dev/null 2>&1; "
        "git diff --cached --no-renames --name-status factory-base > .factory/changes.txt 2>/dev/null; "
        "git reset -q >/dev/null 2>&1; "
        ": > .factory/tampered.txt; "
        "while IFS=\"$(printf '\\t')\" read -r st path; do "
        "[ -n \"$path\" ] || continue; "
        "if printf '%s\\n' \"$path\" | grep -qE '" + pattern + "'; then "
        "if [ \"$st\" = A ]; then rm -f -- \"$path\"; else git checkout -q factory-base -- \"$path\"; fi; "
        "printf '%s\\n' \"$path\" >> .factory/tampered.txt; fi; "
        "done < .factory/changes.txt; "
        "cat .factory/tampered.txt"
    )
