"""G7, spec coverage: the hidden tests may only call an API the solver can know about.

A task is ambiguous when its hidden tests import a name that neither exists in the repository
before the change nor appears in the requirements: a correct solver cannot guess
``apply_bulk_discount`` when the requirements say "add a bulk discount". This gate finds such
names statically (no sandbox, milliseconds) so they are rejected before any test runs.

Checked:
- Python: ``from pkg.mod import a, b`` and ``import pkg.mod as m`` + ``m.attr`` where ``pkg`` is a
  package of the repository; the module itself must exist or be named (dotted or as a path).
- JavaScript/TypeScript: ``import {a} from './x'`` and ``const {a} = require('./x')`` with relative
  paths into the repository.

Not checked (by design, the tests and the review cover them): methods called on instances,
keyword argument names, error message texts.
"""

from __future__ import annotations

import ast
import posixpath
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from functools import cached_property

from ..models import RepoSpec


@dataclass
class SpecFinding:
    test_file: str
    name: str
    source: str  # the module or file the test imports it from
    reason: str

    def __str__(self) -> str:
        return f"{self.test_file}: {self.name} from {self.source} ({self.reason})"


@dataclass
class BaseRepo:
    """Read-only view of the repository at the pinned commit."""

    spec: RepoSpec
    _cache: dict[str, str | None] = field(default_factory=dict)

    @cached_property
    def files(self) -> set[str]:
        if self.spec.archive:
            with tarfile.open(self.spec.archive) as tf:
                return {m.name.removeprefix("./") for m in tf.getmembers() if m.isfile()}
        r = subprocess.run(["git", "-C", self.spec.local_path, "ls-tree", "-r", "--name-only", self.spec.commit_sha],
                           capture_output=True, text=True)
        return set(r.stdout.split("\n")) - {""} if r.returncode == 0 else set()

    def read(self, path: str) -> str | None:
        if path in self._cache:
            return self._cache[path]
        text = None
        if path in self.files:
            if self.spec.archive:
                with tarfile.open(self.spec.archive) as tf:
                    member = tf.getmember(path) if path in tf.getnames() else tf.getmember("./" + path)
                    fh = tf.extractfile(member)
                    text = fh.read().decode("utf-8", "replace") if fh else None
            else:
                r = subprocess.run(["git", "-C", self.spec.local_path, "show", f"{self.spec.commit_sha}:{path}"],
                                   capture_output=True)
                text = r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
        self._cache[path] = text
        return text

    # ---------------------------------------------------------------- python
    def py_module(self, dotted: str) -> str | None:
        """Path of a module at the base commit, or None."""
        rel = dotted.replace(".", "/")
        for prefix in ("", "src/", "lib/"):
            for cand in (f"{prefix}{rel}.py", f"{prefix}{rel}/__init__.py"):
                if cand in self.files:
                    return cand
        return None

    def py_package_root(self, dotted: str) -> bool:
        """True when the top-level package belongs to this repository (not a third-party import)."""
        top = dotted.split(".")[0]
        return any(f.startswith((f"{top}/", f"src/{top}/", f"lib/{top}/")) or f in (f"{top}.py", f"src/{top}.py")
                   for f in self.files)


def _py_defined(source: str) -> set[str]:
    """Names a Python module defines or re-exports at top level (plus class attributes, conservatively)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(re.findall(r"\b[A-Za-z_]\w*\b", source))  # unparsable: be permissive
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def _mentioned(name: str, requirements: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", requirements) is not None


def _module_mentioned(dotted: str, requirements: str) -> bool:
    rel = dotted.replace(".", "/")
    return any(s in requirements for s in (dotted, f"{rel}.py", f"{rel}/")) or _mentioned(dotted.split(".")[-1],
                                                                                            requirements)


def _check_python(path: str, code: str, base: BaseRepo, req: str) -> list[SpecFinding]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # G2 reports a test file that does not compile
    out: list[SpecFinding] = []
    aliases: dict[str, str] = {}  # local name -> repo module

    def check(name: str, module: str) -> None:
        mod_path = base.py_module(module)
        if mod_path is None:
            if not _module_mentioned(module, req):
                out.append(SpecFinding(path, name, module, "new module never named in the requirements"))
                return
            if not _mentioned(name, req):
                out.append(SpecFinding(path, name, module, "new name never named in the requirements"))
            return
        if name in _py_defined(base.read(mod_path) or ""):
            return
        # a submodule imported from a package: ``from pkg import mod``
        if base.py_module(f"{module}.{name}") is not None:
            return
        if not _mentioned(name, req):
            out.append(SpecFinding(path, name, module, "not in the repository and never named in the requirements"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and base.py_package_root(node.module):
            for a in node.names:
                if a.name == "*":
                    continue
                sub = f"{node.module}.{a.name}"
                if base.py_module(sub) is not None:  # ``from pkg import mod``: an existing submodule
                    aliases[a.asname or a.name] = sub
                    continue
                check(a.name, node.module)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if base.py_package_root(a.name):
                    if base.py_module(a.name) is None and not _module_mentioned(a.name, req):
                        out.append(SpecFinding(path, a.name, a.name, "new module never named in the requirements"))
                    aliases[a.asname or a.name] = a.name
    # attribute access on an imported repo module: ``pricing.bulk_discount(...)``
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            module = aliases[node.value.id]
            if base.py_module(f"{module}.{node.attr}") is None:
                check(node.attr, module)
    # one finding per (name, module)
    seen, unique = set(), []
    for f in out:
        if (f.name, f.source) not in seen:
            seen.add((f.name, f.source))
            unique.append(f)
    return unique


_JS_IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*['\"](\.[^'\"]+)['\"]")
_JS_REQUIRE_RE = re.compile(r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\(\s*['\"](\.[^'\"]+)['\"]\s*\)")
_JS_EXT = ("", ".js", ".mjs", ".cjs", ".ts", ".tsx", "/index.js", "/index.ts")


def _check_js(path: str, code: str, base: BaseRepo, req: str) -> list[SpecFinding]:
    out = []
    for rx in (_JS_IMPORT_RE, _JS_REQUIRE_RE):
        for m in rx.finditer(code):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), m.group(2)))
            src_path = next((target + e for e in _JS_EXT if target + e in base.files), None)
            src = base.read(src_path) if src_path else None
            for part in m.group(1).split(","):
                name = re.split(r"\s+as\s+|\s*:\s*", part.strip())[0].strip()
                if not name:
                    continue
                if src is not None and re.search(rf"\b{re.escape(name)}\b", src):
                    continue
                if src is None and not _module_mentioned(m.group(2).lstrip("./"), req) \
                        and not _mentioned(posixpath.basename(target), req):
                    out.append(SpecFinding(path, name, m.group(2), "new module never named in the requirements"))
                elif not _mentioned(name, req):
                    out.append(SpecFinding(path, name, m.group(2),
                                           "not in the repository and never named in the requirements"))
    return out


def spec_findings(requirements_md: str, test_files: dict[str, str], spec: RepoSpec) -> list[SpecFinding]:
    """Names the hidden tests rely on that a solver reading only the requirements could not know."""
    base = BaseRepo(spec)
    findings: list[SpecFinding] = []
    for path, code in sorted(test_files.items()):
        if path.endswith(".py"):
            findings += _check_python(path, code, base, requirements_md)
        elif path.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx")):
            findings += _check_js(path, code, base, requirements_md)
    return findings
