"""Use a configured model to create an ordinary Task Factory draft."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from ..errors import PackageError
from ..model_client import ModelClient
from ..repo.loader import export_clean


_LINE_VALUE = re.compile(r'^(\s*[{\[]?\s*"[^"\n]+"\s*:\s*")(.*)("\s*,?\s*[}\]]?\s*)$')


def _repair_json(text: str) -> str:
    """Fix the slips models make in JSON: trailing commas, and unescaped quotes inside a one-line
    string value (``"x": "use @group(name="a") here",``)."""
    out = []
    for line in text.splitlines():
        m = _LINE_VALUE.match(line)
        if m and '"' in m.group(2):
            inner = re.sub(r'(?<!\\)"', r'\\"', m.group(2))
            line = m.group(1) + inner + m.group(3)
        out.append(line)
    text = re.sub(r",(\s*[}\]])", r"\1", "\n".join(out))
    return re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)  # an invalid escape such as \- becomes a backslash


def _json_object(response: str) -> dict | None:
    """The JSON object in a model reply: the whole reply, a ```json block, or the first {...} in the text;
    each also once repaired (trailing commas, unescaped inner quotes). Control characters are allowed."""
    text = response.strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if fence:
        candidates.append(fence.group(1))
    if "{" in text:
        candidates.append(text[text.index("{"):])
    decoder = json.JSONDecoder(strict=False)
    repaired = [_repair_json(c) for c in candidates]
    # a reply cut short or missing its last braces: try closing it
    closed = [r.rstrip().rstrip("`").rstrip() + tail for r in repaired[-1:] for tail in ("}", "}}", "]}", "}]}", '"}', '"}]}', '"]}')]
    for candidate in candidates + repaired + closed:
        try:
            data, _ = decoder.raw_decode(candidate.strip())
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _ask(reply, prompt: str) -> dict:
    response = reply(prompt)
    data = _json_object(response or "")
    if data is None:
        start = " ".join((response or "").split())[:160]
        raise PackageError(f"The model did not return a JSON object (it began: {start!r})")
    return data


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts or path.as_posix() != value
            or any(part in {"..", ".git", ".factory"} for part in path.parts)
            or "\\" in value or any(ord(char) < 32 for char in value)):
        raise PackageError(f"Invalid repository path: {value!r}")
    return path


def _test_files(data: dict):
    """``{"tests": {path: text}}``, or the path map itself (models often drop the wrapper)."""
    if "tests" in data:
        return data["tests"]
    if data and all(isinstance(v, str) for v in data.values()) and all(
            "/" in k or "." in k.rsplit("/", 1)[-1] for k in data):
        return data
    return None


def _write_files(root: Path, files, repo: Path, what: str) -> None:
    if not isinstance(files, dict) or not files:
        raise PackageError(f"The model must return {what} as a non-empty map of paths to file contents")
    for name, content in files.items():
        relative = _relative(name)
        if not isinstance(content, str):
            raise PackageError(f"Expected text content for {name}")
        if any((repo / parent).is_symlink() for parent in (*relative.parents, relative)):
            raise PackageError(f"Repository path crosses a symlink: {name}")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def draft_task(spec, drafts: Path, args, *, reply=None) -> Path:
    model = args.model
    if reply is None:
        client = ModelClient(model)
        model = client.model

        def reply(prompt):
            return client.reply([{"role": "user", "content": prompt}])
    with tempfile.TemporaryDirectory(prefix="factory-generate-") as temp:
        root = Path(temp)
        repo = export_clean(spec, root / "repo")
        context = {}
        remaining = 20_000
        for name in args.file:
            path = repo / _relative(name)
            if not path.resolve().is_relative_to(repo.resolve()) or not path.is_file():
                raise PackageError(f"Context file is unavailable at the pinned commit: {name}")
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                value = stream.read(remaining + 1)
            if len(value) > remaining:
                raise PackageError("Selected context exceeds 20,000 characters")
            context[name] = value
            remaining -= len(value)
        baseline = json.dumps({"language": spec.language, "files": context}, ensure_ascii=False)

        solution = _ask(reply,
            "Draft a benign coding exercise for the user request below. "
            "Return only JSON with title, requirements (Markdown), and the reference change as "
            "search/replace edits, shaped like "
            '{"title": "...", "requirements": "...", ' + EDIT_FORMAT[:-1] + "}. "
            "(Instead of edits you may give after: an object mapping repo-relative source paths to "
            "complete replacement text.) "
            "Describe observable behavior. Keep tests out of the reference changes.\n"
            f"Request: {args.prompt}\nBaseline: {baseline}")
        title, requirements = solution.get("title"), solution.get("requirements")
        if not all(isinstance(value, str) and value.strip() for value in (title, requirements)):
            raise PackageError("The model must provide a title and requirements")
        if not requirements.lstrip().startswith("# "):  # the instruction always opens with its title
            requirements = f"# {title.strip()}\n\n{requirements.lstrip()}"
        tests = _ask(reply,
            "Write hidden feature tests for these requirements against the baseline. "
            "Every new test should fail before the feature and pass after it. "
            "Return only JSON with tests (an object mapping repo-relative test paths "
            "to complete file contents, preserving any existing tests you replace), shaped like "
            '{"tests": {"tests/test_feature.py": "<whole file>"}}.\n'
            f"Requirements: {requirements}\nBaseline: {baseline}")

        overlay = solution_overlay(solution, repo)
        drafts.mkdir(parents=True, exist_ok=True)
        final_name = None
        with tempfile.TemporaryDirectory(prefix=".generating-", dir=drafts) as staging:
            path = Path(staging)
            _write_files(path / "after", overlay, repo, '"edits" (the reference change)')
            _write_files(path / "tests", _test_files(tests), repo, '"tests" (the hidden tests)')
            (path / "requirements.md").write_text(requirements, encoding="utf-8")
            (path / "task.json").write_text(
                json.dumps({"title": title, "model": model}), encoding="utf-8",
            )
            final_name = path.with_name(path.name.replace(".generating-", "generated-", 1))
            shutil.move(str(path), str(final_name))
        return final_name


# =========================================================================== edits and repo context
# Large source files (click's core.py is 156 KB) cannot be returned whole by a model, so a model
# changes code with search/replace edits. They are applied to a clean copy of the pinned commit and
# turned into the usual after/ overlay, so the draft format and the gates do not change.

EDIT_FORMAT = (
    '"edits": [{"path": "src/pkg/module.py", "search": "<exact existing lines>", '
    '"replace": "<new lines>"}], "new_files": {"src/pkg/new_module.py": "<whole file>"}. '
    "Each search text must be copied exactly from the current file (including indentation) and "
    "must occur exactly once in it; include a few surrounding lines to make it unique. Edits to "
    "the same file apply in order."
)


def apply_edits(repo: Path, edits, new_files=None) -> dict[str, str]:
    """Apply search/replace edits (and whole new files) to ``repo``; returns {path: new content}."""
    overlay: dict[str, str] = {}
    original: dict[str, str] = {}
    touched: dict[str, list[tuple[int, str]]] = {}  # path -> (edit number, its search text)
    if edits is None:
        edits = []
    if not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits):
        raise PackageError("edits must be a list of {path, search, replace} objects")
    for i, edit in enumerate(edits, start=1):
        path, search, replace = edit.get("path"), edit.get("search"), edit.get("replace")
        if not isinstance(path, str) or not isinstance(search, str) or not isinstance(replace, str):
            raise PackageError(f"edit {i} needs string path, search and replace")
        rel = _relative(path)
        if path not in overlay:
            target = repo / rel
            if not target.is_file() or target.is_symlink():
                raise PackageError(f"edit {i}: {path} does not exist in the repository "
                                   "(put new files in new_files)")
            overlay[path] = original[path] = target.read_text(encoding="utf-8")
        text = overlay[path]
        if not search.strip():
            raise PackageError(f"edit {i}: search text for {path} is empty")
        count = text.count(search)
        if count == 0 and original.get(path, "").count(search) == 1:
            # the lines are in the file, but an earlier edit already rewrote them: edits apply in order
            start = original[path].index(search)
            end = start + len(search)
            earlier = [n for n, s in touched.get(path, [])
                       if (k := original[path].find(s)) != -1 and k < end and start < k + len(s)]
            by = f"edit {earlier[0]}" if earlier else "an earlier edit"
            raise PackageError(
                f"edit {i}: search text not found in {path}: {search[:120]!r}. These lines ARE in the original "
                f"file, but {by} already changed them (edits apply one after another, each to the result of the "
                f"ones before). Merge edit {i} into {by}, or make edit {i}'s search text match the file after "
                f"{by}.")
        if count != 1:
            where = "not found" if count == 0 else f"found {count} times"
            raise PackageError(f"edit {i}: search text {where} in {path}: {search[:120]!r}")
        overlay[path] = text.replace(search, replace, 1)
        touched.setdefault(path, []).append((i, search))
    for path, content in (new_files or {}).items():
        _relative(path)
        if not isinstance(content, str):
            raise PackageError(f"Expected text content for {path}")
        if (repo / path).exists():
            raise PackageError(f"new file {path} already exists; change it with edits")
        overlay[path] = content
    if not overlay:
        raise PackageError("the reference solution changes no files")
    return overlay


def solution_overlay(data: dict, repo: Path) -> dict[str, str]:
    """The reference change from a model reply: ``edits``/``new_files``, or whole files in ``after``."""
    if data.get("edits") or data.get("new_files"):
        return apply_edits(repo, data.get("edits"), data.get("new_files"))
    after = data.get("after")
    if not isinstance(after, dict) or not after:
        raise PackageError("The model must return edits (or after) for the reference change")
    return {k: v for k, v in after.items()}


_SIGNATURE = re.compile(
    r"^\s*(async\s+def|def|class|function|export\s|public\s|private\s|protected\s|static\s|"
    r"(?:[\w:<>,\s\*&]+\s+)?[\w:~]+\s*\([^;]*\)\s*(?:const)?\s*\{?\s*$)")


def outline(text: str, limit: int = 12_000) -> str:
    """Signature lines of a big source file (with line numbers), for context the model cannot read whole."""
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        if _SIGNATURE.match(line) and len(line) < 200:
            out.append(f"{n:5}: {line.rstrip()}")
    body = "\n".join(out)
    return body[:limit] + ("\n... (outline truncated)" if len(body) > limit else "")


def repo_context(repo: Path, files: list[str], budget: int = 160_000, full_limit: int = 170_000) -> dict:
    """{path: full text} for the files that fit the budget, {path: outline} for the others."""
    full, outlines = {}, {}
    remaining = budget
    for name in files:
        try:
            rel = _relative(name)
        except PackageError:
            continue
        path = repo / rel
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) <= min(remaining, full_limit):
            full[name] = text
            remaining -= len(text)
        else:
            outlines[name] = outline(text)
    return {"files": full, "outlines": outlines}
