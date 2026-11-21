"""Negative-control mutants for gate G6 (section 10).

Each mutant is a plausible-but-wrong variant of the reference patch. Mutants
only change lines the patch adds, so they apply wherever the reference does.
The empty-patch control is covered by G3 and is not generated here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class Hunk:
    old_start: int
    new_start: int
    tail: str
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        old = sum(1 for ln in self.lines if ln[:1] in (" ", "-"))
        new = sum(1 for ln in self.lines if ln[:1] in (" ", "+"))
        out = [f"@@ -{self.old_start},{old} +{self.new_start},{new} @@{self.tail}"]
        out += self.lines
        return "\n".join(out)


@dataclass
class FilePatch:
    header: list[str]
    hunks: list[Hunk] = field(default_factory=list)


def parse_patch(diff: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = FilePatch(header=[line])
            files.append(current)
            hunk = None
            continue
        if current is None:
            current = FilePatch(header=[])
            files.append(current)
        m = _HUNK_RE.match(line)
        if m:
            hunk = Hunk(int(m.group(1)), int(m.group(3)), m.group(5))
            current.hunks.append(hunk)
        elif hunk is not None and (line[:1] in (" ", "+", "-", "\\") or line == ""):
            hunk.lines.append(line if line else " ")
        else:
            current.header.append(line)
            hunk = None
    return files


def render_patch(files: list[FilePatch]) -> str:
    out = []
    for f in files:
        out += f.header
        out += [h.render() for h in f.hunks]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- line helpers

_COMMENT_RE = re.compile(r"^\s*(#|//|\*|/\*)")
_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")


def _mask_strings(code: str) -> str:
    """Replace string literal contents with spaces so operators inside strings are ignored."""
    return _STRING_RE.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], code)


def _added(files: list[FilePatch]):
    """Yield (file_idx, hunk_idx, line_idx, code) for every added, non-comment line."""
    for fi, f in enumerate(files):
        for hi, h in enumerate(f.hunks):
            for li, line in enumerate(h.lines):
                if line.startswith("+") and not line.startswith("+++") and not _COMMENT_RE.match(line[1:]):
                    yield fi, hi, li, line[1:]


def _with_line(files: list[FilePatch], fi: int, hi: int, li: int, new_code: str) -> str:
    original = files[fi].hunks[hi].lines[li]
    files[fi].hunks[hi].lines[li] = "+" + new_code
    try:
        return render_patch(files)
    finally:
        files[fi].hunks[hi].lines[li] = original


# --------------------------------------------------------------------------- mutation operators

_FLIP = {"<=": "<", ">=": ">", "<": "<=", ">": ">=", "==": "!=", "!=": "==", "===": "!==", "!==": "==="}
_CMP_RE = re.compile(r"===|!==|(?<![<>=!\-])(?:<=|>=|==|!=|<|>)(?![<>=])")
_DEF_RE = re.compile(r"^\s*(def|class|function|func|fn|public|private|protected|static|import|from|export)\b")


def flip_comparison(diff: str, limit: int = 2) -> list[str]:
    files = parse_patch(diff)
    out = []
    for fi, hi, li, code in _added(files):
        if _DEF_RE.match(code):
            continue
        masked = _mask_strings(code)
        m = _CMP_RE.search(masked)
        if not m:
            continue
        op = m.group(0)
        new = code[: m.start()] + _FLIP[op] + code[m.end():]
        out.append(_with_line(files, fi, hi, li, new))
        if len(out) >= limit:
            break
    return out


_CALL_RE = re.compile(r"(\b[A-Za-z_][\w.]*\()\s*([\w.'\"-]+)\s*,\s*([\w.'\"-]+)\s*\)")
_RETURN_PAIR_RE = re.compile(r"^(\s*return\s+)([\w.]+)\s*,\s*([\w.]+)(\s*;?\s*)$")


def swap_arguments(diff: str, limit: int = 2) -> list[str]:
    files = parse_patch(diff)
    out = []
    for fi, hi, li, code in _added(files):
        if _DEF_RE.match(code):
            continue
        m = _RETURN_PAIR_RE.match(code)
        if m and m.group(2) != m.group(3):
            new = f"{m.group(1)}{m.group(3)}, {m.group(2)}{m.group(4)}"
        else:
            m = _CALL_RE.search(code)
            if not m or m.group(2) == m.group(3):
                continue
            new = code[: m.start()] + f"{m.group(1)}{m.group(3)}, {m.group(2)})" + code[m.end():]
        out.append(_with_line(files, fi, hi, li, new))
        if len(out) >= limit:
            break
    return out


_RAISE_RE = re.compile(r"^(\s*)raise\b")
_THROW_RE = re.compile(r"^(\s*)throw\b")


def drop_error_branch(diff: str, limit: int = 2) -> list[str]:
    files = parse_patch(diff)
    out = []
    for fi, hi, li, code in _added(files):
        m = _RAISE_RE.match(code)
        if m:
            new = f"{m.group(1)}pass"
        else:
            m = _THROW_RE.match(code)
            if not m:
                continue
            new = f"{m.group(1)};"
        out.append(_with_line(files, fi, hi, li, new))
        if len(out) >= limit:
            break
    return out


_EXPECT_RES = [
    re.compile(r"==\s*(-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")"),
    re.compile(r"assert\.(?:strict)?[eE]qual\([^,]+,\s*(-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")"),
    re.compile(r"\.toBe\(\s*(-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")\s*\)"),
    re.compile(r"assertEquals\(\s*(-?\d+(?:\.\d+)?|\"[^\"]*\")\s*,"),
]
_RETURN_RE = re.compile(r"^(\s*return\s+)(.+?)(\s*;?\s*)$")


def expected_literals(test_files: dict[str, str]) -> list[str]:
    found: list[str] = []
    for content in test_files.values():
        for rx in _EXPECT_RES:
            for m in rx.finditer(content):
                if m.group(1) not in found:
                    found.append(m.group(1))
    return found


def hardcode_expected(diff: str, test_files: dict[str, str], limit: int = 1) -> list[str]:
    literals = expected_literals(test_files)
    if not literals:
        return []
    files = parse_patch(diff)
    out = []
    for fi, hi, li, code in _added(files):
        m = _RETURN_RE.match(code)
        if not m or m.group(2).strip() in literals:
            continue
        new = f"{m.group(1)}{literals[0]}{m.group(3)}"
        out.append(_with_line(files, fi, hi, li, new))
        if len(out) >= limit:
            break
    return out


def revert_half(diff: str) -> list[str]:
    """Partial implementation: drop every second hunk, or the second half of a single hunk's additions."""
    files = parse_patch(diff)
    all_hunks = [(fi, hi) for fi, f in enumerate(files) for hi in range(len(f.hunks))]
    if len(all_hunks) >= 2:
        drop = set(all_hunks[1::2])
        kept = []
        for fi, f in enumerate(files):
            hunks = [h for hi, h in enumerate(f.hunks) if (fi, hi) not in drop]
            if hunks:
                kept.append(FilePatch(header=f.header, hunks=hunks))
        return [render_patch(kept)] if kept else []
    if not all_hunks:
        return []
    fi, hi = all_hunks[0]
    hunk = files[fi].hunks[hi]
    plus = [i for i, ln in enumerate(hunk.lines) if ln.startswith("+")]
    if len(plus) < 2:
        return []
    drop_idx = set(plus[len(plus) // 2:])
    original = hunk.lines
    hunk.lines = [ln for i, ln in enumerate(original) if i not in drop_idx]
    try:
        return [render_patch(files)]
    finally:
        hunk.lines = original


@dataclass
class Mutant:
    name: str
    patch: str


def make_mutants(diff: str, test_files: dict[str, str], names: list[str]) -> list[Mutant]:
    """Build the configured mutants; drop ones identical to the reference or to each other."""
    makers = {
        "revert_half": lambda: revert_half(diff),
        "flip_comparison": lambda: flip_comparison(diff),
        "swap_arguments": lambda: swap_arguments(diff),
        "drop_error_branch": lambda: drop_error_branch(diff),
        "hardcode_expected": lambda: hardcode_expected(diff, test_files),
    }
    seen = {diff.strip()}
    mutants = []
    for name in names:
        if name == "empty" or name not in makers:
            continue
        for i, patch in enumerate(makers[name]()):
            if patch.strip() in seen:
                continue
            seen.add(patch.strip())
            mutants.append(Mutant(name=f"{name}#{i + 1}", patch=patch))
    return mutants
