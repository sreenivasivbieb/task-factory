"""Negative-control mutants for gate G6 (section 10).

Each mutant is a plausible-but-wrong variant of the reference patch. Mutants
only change lines the patch adds, so they apply wherever the reference does.
The empty-patch control is covered by G3 and is not generated here.
"""

from __future__ import annotations

import fnmatch
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

def _rules():
    """The language-specific parts of the operators, from ecosystems.toml ([mutations])."""
    from ..ecosystems import registry

    return registry().mutation_rules()


def _comment_re() -> re.Pattern:
    return re.compile(r"^\s*(" + "|".join(re.escape(p) for p in _rules().comment_prefixes) + ")")


def _def_re() -> re.Pattern:
    return re.compile(r"^\s*(" + "|".join(map(re.escape, _rules().definition_keywords)) + r")\b")


_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")


def _mask_strings(code: str) -> str:
    """Replace string literal contents with spaces so operators inside strings are ignored."""
    return _STRING_RE.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], code)


_TRIPLE_RE = re.compile(r'"""|\'\'\'|`')


def _added(files: list[FilePatch]):
    """Yield (file_idx, hunk_idx, line_idx, code) for every added line that is code.

    Comments are skipped, and so is every line of a multi-line string (docstrings, template
    literals): a mutant there cannot change behaviour, so it would be an equivalent mutant.
    """
    comment = _comment_re()
    for fi, f in enumerate(files):
        for hi, h in enumerate(f.hunks):
            in_string = False
            for li, line in enumerate(h.lines):
                if line.startswith("-"):
                    continue  # not in the new file
                # a line that opens or closes a multi-line string, or sits inside one; strings that
                # open and close on the same line are masked by the operators themselves
                quotes = len(_TRIPLE_RE.findall(line[1:]))
                in_text = in_string or quotes % 2 == 1
                if quotes % 2:
                    in_string = not in_string
                if in_text or line[1:].lstrip().startswith(('"""', "'''", 'r"""', "r'''")):
                    continue  # a docstring statement cannot change behaviour
                if line.startswith("+") and not line.startswith("+++") and not comment.match(line[1:]):
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


def flip_comparison(diff: str, limit: int = 2) -> list[str]:
    files = parse_patch(diff)
    out = []
    for fi, hi, li, code in _added(files):
        if _def_re().match(code):
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
        if _def_re().match(code):
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


def _file_name(fp: FilePatch) -> str:
    """The changed file's name, from the ``+++ b/...`` (or ``--- a/...``) header line."""
    for prefix in ("+++ ", "--- "):
        for line in fp.header:
            if line.startswith(prefix) and "/dev/null" not in line:
                return line[4:].split("\t", 1)[0].strip().rsplit("/", 1)[-1]
    return ""


def drop_error_branch(diff: str, limit: int = 2) -> list[str]:
    """Turn an added error statement (raise, throw, panic, ...) into one that does nothing."""
    files = parse_patch(diff)
    rules = [(r.files, re.compile(r.match), r.replace) for r in _rules().error_branch]
    out = []
    for fi, hi, li, code in _added(files):
        name = _file_name(files[fi])
        for globs, rx, replace in rules:
            if any(fnmatch.fnmatch(name, g) for g in globs) and rx.match(code):
                out.append(_with_line(files, fi, hi, li, rx.sub(replace, code, count=1)))
                break
        if len(out) >= limit:
            break
    return out




def expected_literals(test_files: dict[str, str]) -> list[str]:
    found: list[str] = []
    for content in test_files.values():
        for rx in map(re.compile, _rules().expected_literal):
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
    rules = [(r.files, re.compile(r.match)) for r in _rules().value_return]
    for fi, hi, li, code in _added(files):
        name = _file_name(files[fi])
        rx = next((rx for globs, rx in rules if any(fnmatch.fnmatch(name, g) for g in globs)), None)
        m = rx.match(code) if rx else None
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


# --------------------------------------------------------------------------- semantic operators
# Each targets one family of mistakes coding models make: boundaries off by one, an operator
# mixed up, a condition inverted, a boolean connective swapped. Like the others they only touch
# lines the patch adds, skip comments, strings and definition lines, and never change a test.

_INT_RE = re.compile(r"(?<![\w.])(\d+)(?![\w.])")


def _mutate_lines(diff: str, change, limit: int) -> list[str]:
    """Apply ``change(code, file_name) -> new_code | None`` to added lines, one mutant per line."""
    files = parse_patch(diff)
    out = []
    for fi, hi, li, code in _added(files):
        if _def_re().match(code) or code.lstrip().startswith("@"):  # decorators configure, they rarely compute
            continue
        new = change(code, _file_name(files[fi]))
        if new is None or new == code:
            continue
        out.append(_with_line(files, fi, hi, li, new))
        if len(out) >= limit:
            break
    return out


def _sub_masked(code: str, rx: re.Pattern, repl) -> str | None:
    """Replace the first match of ``rx`` outside string literals; ``repl(match) -> str``."""
    m = rx.search(_mask_strings(code))
    if not m:
        return None
    return code[: m.start()] + repl(m) + code[m.end():]


def off_by_one(diff: str, limit: int = 2) -> list[str]:
    """``qty >= 10`` -> ``qty >= 11``: a boundary or constant one off."""
    return _mutate_lines(diff, lambda code, _: _sub_masked(code, _INT_RE, lambda m: str(int(m.group(1)) + 1)), limit)


_ARITH = {"+": "-", "-": "+", "*": "/", "/": "*"}
_ARITH_RE = re.compile(r"(?<=\s)([+\-*/])(=?)(?=\s)")


def swap_arithmetic(diff: str, limit: int = 2) -> list[str]:
    """``a - b`` -> ``a + b``, ``a * b`` -> ``a / b`` (binary operators only, ``**`` and ``//`` untouched)."""
    return _mutate_lines(diff, lambda code, _: _sub_masked(
        code, _ARITH_RE, lambda m: _ARITH[m.group(1)] + m.group(2)), limit)


_PY_COND_RE = re.compile(r"^(\s*)(if|elif|while)\s+(.+?):(\s*(?:#.*)?)$")
_C_COND_RE = re.compile(r"^(\s*(?:\}\s*else\s+)?)(if|while)\s*\((.+)\)(\s*\{?\s*)$")


def negate_condition(diff: str, limit: int = 2) -> list[str]:
    """``if x > y:`` -> ``if not (x > y):`` (Python) or ``if (!(x > y)) {`` (C-like languages)."""
    def change(code: str, name: str) -> str | None:
        if name.endswith(".py"):
            m = _PY_COND_RE.match(code)
            return f"{m.group(1)}{m.group(2)} not ({m.group(3)}):{m.group(4)}" if m else None
        m = _C_COND_RE.match(code)
        return f"{m.group(1)}{m.group(2)} (!({m.group(3)})){m.group(4)}" if m else None
    return _mutate_lines(diff, change, limit)


_BOOL = {" and ": " or ", " or ": " and ", "&&": "||", "||": "&&", "True": "False", "False": "True",
         "true": "false", "false": "true"}
# keyword-argument literals (``sort(reverse=True)``) are configuration, so they are left alone
_BOOL_RE = re.compile(r" and | or |&&|\|\||(?<!\w=)\b(?:True|False|true|false)\b")


def swap_boolean(diff: str, limit: int = 2) -> list[str]:
    """``a and b`` -> ``a or b``, ``True`` -> ``False`` (and the C-like equivalents)."""
    return _mutate_lines(diff, lambda code, _: _sub_masked(code, _BOOL_RE, lambda m: _BOOL[m.group(0)]), limit)


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
        "off_by_one": lambda: off_by_one(diff),
        "swap_arithmetic": lambda: swap_arithmetic(diff),
        "negate_condition": lambda: negate_condition(diff),
        "swap_boolean": lambda: swap_boolean(diff),
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
