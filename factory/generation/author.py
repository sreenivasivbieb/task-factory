"""Use a configured model to create an ordinary Task Factory draft."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path, PurePosixPath

from ..errors import PackageError
from ..model_client import ModelClient
from ..repo.loader import export_clean


def _json_object(response: str) -> dict | None:
    """The JSON object in a model reply: the whole reply, a ```json block, or the first {...} in the text."""
    text = response.strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if fence:
        candidates.append(fence.group(1))
    if "{" in text:
        candidates.append(text[text.index("{"):])
    decoder = json.JSONDecoder()
    for candidate in candidates:
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
    if data and all(isinstance(v, str) and "/" in k for k, v in data.items()):
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
            "Return only JSON with title, requirements (Markdown), and after "
            "(an object mapping repo-relative source paths to complete replacement text), shaped like "
            '{"title": "...", "requirements": "...", "after": {"pkg/module.py": "<whole file>"}}. '
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

        drafts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".generating-", dir=drafts) as staging:
            path = Path(staging)
            _write_files(path / "after", solution.get("after"), repo, '"after" (the reference change)')
            _write_files(path / "tests", _test_files(tests), repo, '"tests" (the hidden tests)')
            (path / "requirements.md").write_text(requirements, encoding="utf-8")
            (path / "task.json").write_text(
                json.dumps({"title": title, "model": model}), encoding="utf-8",
            )
            return path.rename(path.with_name(path.name.replace(".generating-", "generated-", 1)))
