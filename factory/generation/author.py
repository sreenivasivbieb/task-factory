"""Use a configured model to create an ordinary Task Factory draft."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path, PurePosixPath

from ..errors import PackageError
from ..model_client import ModelClient
from ..repo.loader import export_clean


def _ask(reply, prompt: str) -> dict:
    response = reply(prompt).strip()
    if response.startswith("```json") and response.endswith("```"):
        response = response[7:-3].strip()
    try:
        data = json.loads(response)
    except ValueError as exc:
        raise PackageError("The model did not return a valid JSON object") from exc
    if not isinstance(data, dict):
        raise PackageError("The model did not return a JSON object")
    return data


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts or path.as_posix() != value
            or any(part in {"..", ".git", ".factory"} for part in path.parts)
            or "\\" in value or any(ord(char) < 32 for char in value)):
        raise PackageError(f"Invalid repository path: {value!r}")
    return path


def _write_files(root: Path, files, repo: Path) -> None:
    if not isinstance(files, dict) or not files:
        raise PackageError("The model must return a non-empty map of paths to file contents")
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
            "(an object mapping repo-relative source paths to complete replacement text). "
            "Describe observable behavior. Keep tests out of the reference changes.\n"
            f"Request: {args.prompt}\nBaseline: {baseline}")
        title, requirements = solution.get("title"), solution.get("requirements")
        if not all(isinstance(value, str) and value.strip() for value in (title, requirements)):
            raise PackageError("The model must provide a title and requirements")
        tests = _ask(reply,
            "Write hidden feature tests for these requirements against the baseline. "
            "Every new test should fail before the feature and pass after it. "
            "Return only JSON with tests (an object mapping repo-relative test paths "
            "to complete file contents, preserving any existing tests you replace).\n"
            f"Requirements: {requirements}\nBaseline: {baseline}")

        drafts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".generating-", dir=drafts) as staging:
            path = Path(staging)
            _write_files(path / "after", solution.get("after"), repo)
            _write_files(path / "tests", tests.get("tests"), repo)
            (path / "requirements.md").write_text(requirements, encoding="utf-8")
            (path / "task.json").write_text(
                json.dumps({"title": title, "model": model}), encoding="utf-8",
            )
            return path.rename(path.with_name(path.name.replace(".generating-", "generated-", 1)))
