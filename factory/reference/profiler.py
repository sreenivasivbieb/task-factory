"""P1 Profiler: a full understanding of a repo (the starter repo, and each shortlisted peer).

Static detection first (language, test runner, dependencies, docs, test names, an outline of the
public source), then the profiler sub-agent writes the RepoProfile: purpose, a capability map of
user-facing behaviours, design traits, boundaries. Every capability must cite files; a
deterministic checker verifies each citation against the code (the file exists, the cited symbol
is in it), and the profile records how many held.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tomllib
from pathlib import Path

from ..ecosystems import registry
from ..generation.ideas import _is_test, repo_digest
from ..repo.inspector import detect_language
from .schema import Capability, Ecosystem, RepoProfile, SpotCheck, stable_id
from .snapshot import Snapshot

log = logging.getLogger("factory.reference.profiler")

_TEST_NAME = re.compile(r"\bdef\s+(test_\w+)|\b(?:it|test)\(\s*['\"]([^'\"]{4,80})['\"]|\bTEST(?:_F)?\(\s*\w+\s*,\s*(\w+)\)")


# --------------------------------------------------------------------------- static detection


def test_runner(repo: Path, language: str | None) -> str | None:
    if language is None:
        return None
    eco = registry().ecosystems.get(language)
    if eco is None:
        return None
    for v in eco.variants:
        if v.matches(repo):
            return v.framework or v.name
    return eco.framework


def dependencies(repo: Path) -> list[str]:
    deps: list[str] = []
    py = repo / "pyproject.toml"
    if py.exists():
        try:
            data = tomllib.loads(py.read_text(encoding="utf-8"))
            for d in data.get("project", {}).get("dependencies", []) or []:
                deps.append(re.split(r"[<>=!~;\[ ]", d, maxsplit=1)[0])
        except (tomllib.TOMLDecodeError, OSError):
            pass
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            for k in ("dependencies", "peerDependencies", "devDependencies"):
                deps += list((data.get(k) or {}).keys())
        except (ValueError, OSError):
            pass
    cm = repo / "CMakeLists.txt"
    if cm.exists():
        deps += re.findall(r"find_package\(\s*(\w+)", cm.read_text(encoding="utf-8", errors="replace"))
    return sorted(dict.fromkeys(deps))[:60]


def test_names(repo: Path, limit: int = 250) -> list[str]:
    out = []
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo)
        if not p.is_file() or any(part in {".git", "node_modules"} for part in rel.parts):
            continue
        if not _is_test(rel.as_posix()):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _TEST_NAME.finditer(text):
            out.append(m.group(1) or m.group(2) or m.group(3))
            if len(out) >= limit:
                return out
    return out


def doc_titles(repo: Path, limit: int = 60) -> list[str]:
    out = []
    for d in ("docs", "doc", "documentation"):
        root = repo / d
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() in {".md", ".rst", ".txt"} and p.is_file():
                head = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()[:1]
                out.append(f"{p.relative_to(repo).as_posix()}: {head[0][:80] if head else ''}")
                if len(out) >= limit:
                    return out
    return out


def static_facts(repo: Path, budget: int = 50_000) -> dict:
    language = detect_language(repo)
    digest = repo_digest(repo, language, budget=budget)
    return {
        "language": language,
        "test_runner": test_runner(repo, language),
        "dependencies": dependencies(repo),
        "docs": doc_titles(repo),
        "test_names": test_names(repo),
        **digest,
    }


# --------------------------------------------------------------------------- spot check


def _evidence_holds(repo: Path, ev: str) -> bool:
    path, _, symbol = ev.partition(":")
    path = path.strip().lstrip("./")
    if not path or ".." in Path(path).parts:
        return False
    target = repo / path
    if not target.exists():
        return False
    symbol = symbol.strip()
    if not symbol or target.is_dir():
        return True
    symbol = re.sub(r"\(.*$", "", symbol).strip().split(".")[-1]
    if not symbol or symbol.isdigit():  # "path:123" is a line number
        return True
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"(?<![\w])" + re.escape(symbol) + r"(?![\w])", text) is not None


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[`*_>#]", " ", text).lower().split())


def verify_boundaries(items: list, repo: Path) -> list[str]:
    """``[stated] ...`` when the quoted sentence really is in the cited file (README, docs), else
    ``[inferred] ...``: critics treat only stated boundaries as hard limits."""
    out = []
    for b in items:
        if isinstance(b, str):
            out.append(f"[inferred] {b.strip()}")
            continue
        if not isinstance(b, dict) or not str(b.get("text", "")).strip():
            continue
        text, quote = str(b["text"]).strip(), _norm(str(b.get("quote") or ""))
        src = str(b.get("source") or "").strip().lstrip("./")
        stated = False
        if len(quote) >= 20 and src and ".." not in Path(src).parts and (repo / src).is_file():
            stated = quote in _norm((repo / src).read_text(encoding="utf-8", errors="replace"))
        out.append(f"[stated] {text} (\"{str(b.get('quote')).strip()[:160]}\", {src})" if stated else f"[inferred] {text}")
    return out


def spot_check(profile: RepoProfile, repo: Path) -> RepoProfile:
    """Verify every capability's citations; unverifiable capabilities stay but are marked."""
    caps, failed, passed = [], [], 0
    for c in profile.capability_map:
        ok = bool(c.evidence) and any(_evidence_holds(repo, e) for e in c.evidence)
        caps.append(c.model_copy(update={"verified": ok}))
        if ok:
            passed += 1
        else:
            failed.append(c.name)
    return profile.model_copy(update={"capability_map": caps,
                                      "spot_check": SpotCheck(checked=len(caps), passed=passed, failed=failed)})


# --------------------------------------------------------------------------- the profiler


PROMPT = (
    "You are the Profiler of a coding-task factory. Build a precise understanding of the repository "
    "described below, from its files only.\n"
    "Return only JSON shaped like {{\"purpose\": \"what the project is, who uses it, what problem it "
    "solves (2-3 sentences)\", \"capability_map\": [{{\"name\": \"short name\", \"behaviour\": "
    "\"what a user can do, as observable behaviour\", \"evidence\": [\"path/to/file.ext:symbol\"]}}], "
    "\"design_traits\": [\"architecture, extension points, conventions\"], \"boundaries\": "
    "[{{\"text\": \"what it deliberately does not do\", \"source\": \"README.md\", \"quote\": \"the exact "
    "sentence from that file that says so, or empty when you infer it\"}}]}}.\n"
    "Rules: list {n_caps} user-facing capabilities (public API, CLI behaviour, options, parameter "
    "types, validation, output formats, error reporting, extension hooks), each written as a "
    "behaviour, not a file summary. Every capability cites one to three real files from the listing, "
    "with a symbol (function, class, option) that appears in that file. Do not invent files. "
    "Boundaries: prefer non-goals the README and docs STATE, with the exact quote; an inferred boundary "
    "has an empty quote (it will be treated as a guess, not a rule).\n"
    "Repository: {facts}"
)


PROFILE_VERSION = 2  # bump when the profile's content changes: cached profiles are rebuilt


class Profiler:
    def __init__(self, agent, snapshot: Snapshot | None = None, n_caps: str = "20 to 40") -> None:
        self.agent = agent
        self.snapshot = snapshot
        self.n_caps = n_caps

    def _build(self, repo: Path, name: str, url: str | None, commit: str | None, topics: list[str]) -> dict:
        facts = static_facts(repo)
        data = self.agent.ask_json(PROMPT.format(n_caps=self.n_caps, facts=json.dumps(facts, ensure_ascii=False)))
        caps = []
        for c in data.get("capability_map") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            caps.append(Capability(name=str(c["name"]).strip(), behaviour=str(c.get("behaviour") or "").strip(),
                                   evidence=[str(e) for e in (c.get("evidence") or []) if isinstance(e, str)][:3]))
        profile = RepoProfile(
            repo=name, url=url, commit=commit, purpose=str(data.get("purpose") or "").strip(),
            language=facts["language"], test_runner=facts["test_runner"], capability_map=caps,
            design_traits=[str(x) for x in data.get("design_traits") or []][:15],
            boundaries=verify_boundaries(data.get("boundaries") or [], repo)[:15],
            ecosystem=Ecosystem(deps=facts["dependencies"], topics=list(topics or [])), model=self.agent.model)
        profile = spot_check(profile, repo)
        log.info("profile %s: %d capabilities, %d/%d citations hold", name, len(caps),
                 profile.spot_check.passed, profile.spot_check.checked)
        return profile.model_dump(mode="json")

    def profile(self, repo: Path | None, name: str, url: str | None = None, commit: str | None = None,
                topics: list[str] | None = None, version: str | None = None) -> RepoProfile:
        """Profile ``repo`` (a checkout); with a snapshot the result is saved and replayed by (name, version).

        ``version`` names the state profiled where the commit is only known after a clone (a peer's last
        push date, from the snapshot): replay finds the profile without cloning.
        """
        key = {"repo": name, "commit": version or commit or (stable_id(str(repo)) if repo else None), "v": PROFILE_VERSION}
        if self.snapshot is None:
            return RepoProfile.model_validate(self._build(Path(repo), name, url, commit, topics or []))

        def build():
            if repo is None:
                raise FileNotFoundError(f"no checkout of {name} to profile")
            return self._build(Path(repo), name, url, commit, topics or [])

        return RepoProfile.model_validate(self.snapshot.fetch("profile", key, build))


def shallow_clone(url: str, dest: Path, ref: str | None = None) -> tuple[Path, str]:
    """A depth-1 clone of a public peer repo; returns (path, head commit)."""
    dest = Path(dest)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "-q", "--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
        r = subprocess.run(cmd + [url, str(dest)], capture_output=True, text=True, timeout=600,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode != 0:
            raise RuntimeError(f"git clone {url} failed: {r.stderr.strip()[:300]}")
    head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return dest, head

