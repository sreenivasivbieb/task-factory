"""Languages and test tools as data (``ecosystems.toml``), extendable from ``factory.toml``.

Nothing in the pipeline is written for one language: the inspector, the test runner, the
grader's protected files, the copy filters and the mutation operators all read this
registry. A repo in a language the factory has never seen works once its ecosystem is
described under ``[ecosystems.<name>]`` in ``factory.toml``; see ``ecosystems.toml`` for
the keys.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import ConfigError

BUILTIN = Path(__file__).with_name("ecosystems.toml")
HELPERS_DIR = Path(__file__).parent / "runner"

Select = Literal["ids", "files", "dirs", "none"]


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Install(_Data):
    if_file: str | None = None
    command: str


class Contains(_Data):
    file: str
    pattern: str


class ErrorBranch(_Data):
    files: list[str]
    match: str
    replace: str


class ValueReturn(_Data):
    """How a function hands back a value: groups (prefix, value, suffix) of the statement."""

    files: list[str]
    match: str


class EnvKeys(_Data):
    """The keys an ecosystem or one of its variants may set."""

    framework: str | None = None
    base_image: str | None = None
    setup_commands: list[str] | None = None
    test_command: str | None = None
    test_timeout_s: int | None = None
    select: Select | None = None
    select_all: str | None = None
    deselect: str | None = None
    report_format: str | None = None
    report_path: str | None = None
    helpers: dict[str, str] | None = None


class Variant(EnvKeys):
    name: str
    if_file: str | None = None
    if_contains: Contains | None = None

    def matches(self, root: Path) -> bool:
        if self.if_file and not (root / self.if_file).exists():
            return False
        if self.if_contains:
            try:
                text = (root / self.if_contains.file).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            if not re.search(self.if_contains.pattern, text):
                return False
        return True


class Ecosystem(EnvKeys):
    name: str = ""
    markers: list[str] = Field(default_factory=list)
    adapter: Literal["python"] | None = None
    install: list[Install] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)
    build_files: list[str] = Field(default_factory=list)
    build_patterns: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    # mutation rules for this language, added to the shared [mutations] lists
    error_branch: list[ErrorBranch] = Field(default_factory=list)
    expected_literal: list[str] = Field(default_factory=list)
    comment_prefixes: list[str] = Field(default_factory=list)
    value_return: list[ValueReturn] = Field(default_factory=list)


class Mutations(_Data):
    comment_prefixes: list[str]
    definition_keywords: list[str]
    error_branch: list[ErrorBranch]
    expected_literal: list[str]
    value_return: list[ValueReturn]


class Common(_Data):
    test_paths: list[str] = Field(default_factory=list)
    build_files: list[str] = Field(default_factory=list)
    build_patterns: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class Registry(BaseModel):
    ecosystems: dict[str, Ecosystem]
    common: Common
    mutations: Mutations

    # ------------------------------------------------------------------ detection

    def detect(self, root: Path | str) -> str | None:
        root = Path(root)
        for name, eco in self.ecosystems.items():
            if any((root / m).exists() for m in eco.markers):
                return name
        return None

    def all_markers(self) -> list[str]:
        return [m for eco in self.ecosystems.values() for m in eco.markers]

    # ------------------------------------------------------------------ cross-language lists

    def _all(self, key: str) -> list[str]:
        out = list(getattr(self.common, key))
        for eco in self.ecosystems.values():
            out += [v for v in getattr(eco, key) if v not in out]
        return out

    def protected_re(self) -> str:
        """One POSIX ERE (also valid Python ``re``) for every test or build/test-config path."""
        names = sorted({_ere_escape(n) for n in self._all("build_files")})
        parts = self._all("test_paths") + self._all("build_patterns")
        if names:
            parts.append(r"(^|/)(" + "|".join(names) + r")$")
        return "|".join(parts) or r"^$"

    def artifacts(self) -> list[str]:
        return self._all("artifacts")

    def mutation_rules(self) -> Mutations:
        """The shared [mutations] lists plus every ecosystem's own rules."""
        m = self.mutations.model_copy(deep=True)
        # value_return: the first rule whose `files` match decides, so a language's own rule
        # comes before the shared catch-all
        m.value_return = [r for eco in self.ecosystems.values() for r in eco.value_return] + m.value_return
        for eco in self.ecosystems.values():
            m.error_branch += eco.error_branch
            m.expected_literal += [p for p in eco.expected_literal if p not in m.expected_literal]
            m.comment_prefixes += [p for p in eco.comment_prefixes if p not in m.comment_prefixes]
        return m

    def ignored(self, path: str, ecosystem: str | None = None) -> bool:
        """True for build output such as ``node_modules/`` or ``*.pyc``: the shared artifacts plus
        those of ``ecosystem`` (all ecosystems when it is None)."""
        parts = path.replace("\\", "/").split("/")
        eco = self.ecosystems.get(ecosystem or "")
        patterns = [*self.common.artifacts, *eco.artifacts] if eco else self.artifacts()
        for pattern in patterns:
            if pattern.endswith("/"):
                if any(fnmatch.fnmatch(p, pattern[:-1]) for p in parts[:-1] or parts):
                    return True
            elif fnmatch.fnmatch(parts[-1], pattern):
                return True
        return False


def _ere_escape(name: str) -> str:
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", name)


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


_custom: dict = {}


def configure(custom: dict | None) -> None:
    """Apply the ``[ecosystems]`` table of factory.toml (called once the config is loaded)."""
    global _custom
    _custom = dict(custom or {})
    registry.cache_clear()


@lru_cache(maxsize=1)
def registry() -> Registry:
    data = tomllib.loads(BUILTIN.read_text(encoding="utf-8"))
    common = data.pop("common", {})
    mutations = data.pop("mutations")
    custom = dict(_custom)
    common = _merge(common, custom.pop("common", {}))
    mutations = _merge(mutations, custom.pop("mutations", {}))
    for name, extra in custom.items():
        data[name] = _merge(data.get(name, {}), extra)
    try:
        return Registry(ecosystems={n: Ecosystem(name=n, **d) for n, d in data.items()},
                        common=Common(**common), mutations=Mutations(**mutations))
    except ValueError as exc:
        raise ConfigError(f"bad [ecosystems] in factory.toml: {exc}") from exc


def helper_text(name: str) -> str:
    """A bundled helper (by file name) or any readable file (by path)."""
    bundled = HELPERS_DIR / name
    path = bundled if "/" not in name and bundled.is_file() else Path(name).expanduser()
    if not path.is_file():
        raise ConfigError(f"helper file {name!r} not found (bundled helpers live in {HELPERS_DIR})")
    return path.read_text(encoding="utf-8")
