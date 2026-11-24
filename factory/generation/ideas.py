"""Ideation: read the pinned repository and propose distinct feature-development tasks.

The model sees a digest of the repo (file tree, README, recent changelog, an outline of the
public source) and proposes more ideas than needed. The factory then keeps only ideas that

- are features or behaviour changes (never bug fixes),
- name source files that exist and are not test files,
- do not introduce a name that already exists in the repo (the feature must not exist upstream),
- differ from each other: a different category, and a title/summary that is not a near copy
  of an idea already kept (word overlap), so five tasks are five different behaviours,

and returns them in an order that spreads them across categories and files.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..ecosystems import registry
from ..errors import PackageError
from .author import _ask, _relative, outline

CATEGORIES = [
    "new option or parameter", "new command or API", "validation rule", "output formatting",
    "parsing or conversion", "configuration or environment", "error reporting", "interaction between features",
]

_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__", "docs", "examples",
              ".github", ".factory", "artwork", "site", "coverage"}
_SOURCE_EXT = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
               ".hh", ".go", ".rs", ".java", ".kt", ".sh"}


@dataclass
class Idea:
    id: str
    title: str
    summary: str
    category: str
    files: list[str]
    behaviour: list[str] = field(default_factory=list)
    new_names: list[str] = field(default_factory=list)
    kind: str = "feature"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Idea":
        keys = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in keys})


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "idea"


def _is_test(path: str, language: str | None = None) -> bool:
    """Test files and test/build config: the same paths solvers may not touch (ecosystems.toml)."""
    return re.search(registry().protected_re(), path) is not None


def source_files(repo: Path, language: str | None = None) -> list[str]:
    out = []
    for p in sorted(Path(repo).rglob("*")):
        rel = p.relative_to(repo)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if p.is_file() and not p.is_symlink() and p.suffix in _SOURCE_EXT and not _is_test(rel.as_posix(), language):
            out.append(rel.as_posix())
    return out


def _head(repo: Path, names: tuple[str, ...], limit: int) -> str:
    for name in names:
        for p in sorted(Path(repo).glob(name)):
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")[:limit]
    return ""


def repo_digest(repo: Path, language: str | None, budget: int = 60_000) -> dict:
    files = source_files(repo, language)
    tests = [p.relative_to(repo).as_posix() for p in sorted(Path(repo).rglob("*"))
             if p.is_file() and _is_test(p.relative_to(repo).as_posix(), language)
             and not any(part in _SKIP_DIRS for part in p.relative_to(repo).parts)]
    outlines, remaining = {}, budget
    for name in files:
        text = (Path(repo) / name).read_text(encoding="utf-8", errors="replace")
        o = outline(text, limit=min(8_000, remaining))
        if not o:
            continue
        outlines[name] = o
        remaining -= len(o)
        if remaining <= 0:
            break
    return {
        "language": language,
        "source_files": files,
        "test_files": tests[:200],
        "readme": _head(repo, ("README*", "readme*"), 4_000),
        "changelog": _head(repo, ("CHANGES*", "CHANGELOG*", "HISTORY*", "NEWS*"), 6_000),
        "outlines": outlines,
    }


def _words(text: str) -> set[str]:
    stop = {"the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "with", "add", "new", "support",
            "option", "when", "is", "be", "by", "that", "it", "as", "from"}
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in stop and len(w) > 2}


def similarity(a: Idea, b: Idea) -> float:
    wa, wb = _words(a.title + " " + a.summary), _words(b.title + " " + b.summary)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _exists_upstream(repo: Path, names: list[str], files: list[str]) -> str | None:
    """A proposed new name that already appears in the source: the feature may already exist."""
    corpus = "\n".join((Path(repo) / f).read_text(encoding="utf-8", errors="replace") for f in files)
    for name in names:
        name = name.strip()
        if len(name) >= 4 and re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", corpus):
            return name
    return None


def newest_refs(git_repo: Path | None, base_commit: str | None = None) -> list[str]:
    """Tips of the repository's newest branches (origin/HEAD, main, master), other than the base commit."""
    if git_repo is None or not (Path(git_repo) / ".git").exists():
        return []
    refs = []
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master",
                "refs/heads/main", "refs/heads/master"):
        r = subprocess.run(["git", "-C", str(git_repo), "rev-parse", "--verify", "-q", ref + "^{commit}"],
                           capture_output=True, text=True)
        sha = r.stdout.strip()
        if r.returncode == 0 and sha and sha != base_commit and sha not in refs:
            refs.append(sha)
    return refs


def _exists_in_newer(git_repo: Path | None, refs: list[str], names: list[str]) -> str | None:
    """A proposed name that a newer upstream version already has: the feature exists upstream."""
    for name in names:
        name = name.strip()
        if len(name) < 4:
            continue
        for ref in refs:
            r = subprocess.run(["git", "-C", str(git_repo), "grep", "-q", "-w", "-F", "-e", name, ref, "--",
                                ":(exclude)tests", ":(exclude)test"], capture_output=True, text=True)
            if r.returncode == 0:
                return name
    return None


def check_idea(raw: dict, repo: Path, language: str | None, sources: list[str],
               upstream: Path | None = None, upstream_refs: list[str] | None = None) -> tuple[Idea | None, str]:
    """(idea, "") when usable, else (None, why not)."""
    try:
        title = str(raw["title"]).strip()
        summary = str(raw.get("summary") or "").strip()
        files = [str(f) for f in raw.get("files") or []]
    except (KeyError, TypeError):
        return None, "missing title or files"
    kind = str(raw.get("kind") or "feature")
    if kind not in {"feature", "behavior_change"} or re.search(r"\b(fix|bug|crash|regression)\b",
                                                                 (title + " " + summary).lower()):
        return None, "bug fixes are out of scope"
    good = []
    for f in files:
        try:
            _relative(f)
        except PackageError:
            continue
        if f in sources:
            good.append(f)
    if not good:
        return None, f"names no existing source file ({', '.join(files[:3]) or 'none'})"
    names = [str(n) for n in raw.get("new_names") or []]
    clash = _exists_upstream(repo, names, sources)
    if clash:
        return None, f"{clash!r} already exists in the repository"
    clash = _exists_in_newer(upstream, upstream_refs or [], names)
    if clash:
        return None, f"{clash!r} already exists in a newer upstream version"
    category = str(raw.get("category") or "").strip() or "other"
    idea = Idea(id=_slug(str(raw.get("id") or title)), title=title, summary=summary, category=category,
                files=good, behaviour=[str(b) for b in raw.get("behaviour") or []], new_names=names, kind=kind)
    return idea, ""


def select_distinct(ideas: list[Idea], count: int, existing: list[Idea] | None = None,
                    max_similarity: float = 0.45) -> list[Idea]:
    """Greedy pick: new category first, then new primary file, never a near-duplicate."""
    chosen: list[Idea] = []
    taken = list(existing or [])
    pool = list(ideas)
    while pool and len(chosen) < count:
        cats = {i.category.lower() for i in taken + chosen}
        files = {i.files[0] for i in taken + chosen if i.files}
        pool = [i for i in pool if all(similarity(i, j) <= max_similarity and i.id != j.id
                                       for j in taken + chosen)]
        if not pool:
            break
        pool.sort(key=lambda i: (i.category.lower() in cats, i.files[0] in files))
        chosen.append(pool.pop(0))
    return chosen


def ideate(repo: Path, language: str | None, reply, count: int = 5, existing: list[Idea] | None = None,
           rejected: list[str] | None = None, upstream: Path | None = None,
           base_commit: str | None = None) -> tuple[list[Idea], list[dict]]:
    """Ask the model for ideas; returns (distinct usable ideas, [{"title", "why"}] for those dropped).

    ``upstream`` is a git clone of the repository with its newer branches: a proposed name found
    there means the feature already exists upstream, so the idea is dropped.
    """
    refs = newest_refs(upstream, base_commit)
    digest = repo_digest(repo, language)
    sources = digest["source_files"]
    avoid = [i.title for i in existing or []] + list(rejected or [])
    ask_for = max(count * 2, count + 3)
    prompt = (
        "You design realistic feature-development tasks for evaluating AI coding agents on the "
        "repository described below. Each task asks for a NEW feature or a deliberate CHANGE to existing "
        "behaviour that a maintainer of this project could plausibly accept. Never propose bug fixes, "
        "refactors, documentation, performance work, or anything needing network access, real "
        "credentials, or new third-party dependencies. The feature must not already exist in the code.\n"
        f"Propose {ask_for} ideas that are substantively different from each other: different behaviour, "
        "rules, or interactions (renaming or rewording an idea is not a different idea). Use a different "
        f"category for each where possible, from: {', '.join(CATEGORIES)}.\n"
        "Each idea must be testable with the project's own test runner by a handful of deterministic "
        "unit tests, and small enough for one focused change (one to three source files).\n"
        + (f"Do not repeat these ideas: {json.dumps(avoid)}\n" if avoid else "")
        + "Return only JSON shaped like "
        '{"ideas": [{"id": "short-slug", "title": "...", "summary": "two or three sentences", '
        '"category": "...", "kind": "feature or behavior_change", '
        '"files": ["existing/source/file/to/change"], '
        '"behaviour": ["observable rule 1", "edge case", "error case"], '
        '"new_names": ["new public function, parameter or flag names this adds"]}]}\n'
        f"Repository: {json.dumps(digest, ensure_ascii=False)}"
    )
    data = _ask(reply, prompt)
    raws = data.get("ideas") if isinstance(data.get("ideas"), list) else []
    usable, dropped = [], []
    for raw in raws:
        if not isinstance(raw, dict):
            continue
        idea, why = check_idea(raw, repo, language, sources, upstream, refs)
        if idea is None:
            dropped.append({"title": str(raw.get("title", "?")), "why": why})
        else:
            usable.append(idea)
    chosen = select_distinct(usable, count, existing)
    for idea in usable:
        if idea not in chosen:
            dropped.append({"title": idea.title, "why": "too similar to a chosen idea, or not needed"})
    return chosen, dropped


def save_ideas(path: Path, ideas: list[Idea]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([i.to_dict() for i in ideas], indent=2) + "\n", encoding="utf-8")


def load_ideas(path: Path) -> list[Idea]:
    if not path.exists():
        return []
    return [Idea.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]
