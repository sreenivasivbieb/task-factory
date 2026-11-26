"""Deterministic checks on task proposals, and the variation filter.

The research agent proposes, the critic agent verifies with tools (research.py); these checks are
the code-level guard after them: a feature (not a bug fix or performance work), no network or
credentials, not an exploit, expected files exist and are source, names it adds are not already
defined, the spec and the test plan are specific enough. The variation filter merges near-duplicate
ideas (the stronger one stays) and orders builds so categories, files and sources spread.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..generation.ideas import _is_test, source_files
from .schema import TaskProposal
from .text import jaccard, words

log = logging.getLogger("factory.reference.drafting")

# Conservative on purpose: only a stated NEED (a docs link or "works over SSH" is not one). The hidden
# tests' own network and credential use is checked on the package (reference/harbor.py format checks).
NETWORK_NEED = re.compile(
    r"\b((requires?|needs?) (an? )?(internet|network)( access| connection)?|network access (is )?(required|needed)|"
    r"(download|fetch|retriev|request|pull|upload)\w* [^.\n]{0,50}?\b(from|to) (https?://|an? (remote|external)|"
    r"the (internet|web|network)|remote)|remote (server|service|api)|http requests? to|api keys?|credentials|"
    r"access tokens?|oauth)\b", re.I)
EXPLOIT = re.compile(r"\b(exploit|bypass (the )?(auth|security|sandbox)|privilege escalation|malware|keylogger|"
                     r"exfiltrat|shellcode)\b", re.I)
# A proposal reads as a bug fix only in bug-fix language ("fixed bright red" is an adjective, not a fix).
BUG_FIX_TEXT = re.compile(r"\b(bug ?fix(es)?|hot ?fix|fix(es|ing)? (a |an |the )?(\w+ )?(bug|crash|regression|defect|"
                          r"broken|incorrect|wrong)|bugs?|crash(es|ing)?|regressions?)\b", re.I)
_NEGATION = re.compile(r"\b(no|not|without|never|nor|none)\b[^.;:\n]{0,60}$|n't\b[^.;:\n]{0,60}$", re.I)
# Performance-only work is not a feature or behaviour change, and import-time or speed claims cannot be
# checked deterministically (pytest itself imports tempfile, subprocess ...): seen live on click.
PERF_ONLY = re.compile(r"\b(lazy[- ]?(import|load)\w*|startup time|import time|faster|speed ?up|performance|"
                       r"reduce (memory|latency|overhead)|optimi[sz]e)\b", re.I)
MIN_SPEC = 200


def defines(source: str, name: str) -> bool:
    """Whether the source DEFINES ``name`` (function, class, variable, CLI flag), not merely mentions it."""
    n = re.escape(name.lstrip("-"))
    pattern = (rf"\b(def|class|function|const|let|var|struct|enum|interface|type)\s+{n}\b"
               rf"|^\s*(self\.)?{n}\s*(:[^=\n]*)?=(?!=)"
               rf"|['\"]-{{1,2}}{n}['\"=]")
    return re.search(pattern, source, re.M) is not None


def needs_network(text: str) -> str | None:
    """The first network/credential need that is not negated ("no network access is needed")."""
    for m in NETWORK_NEED.finditer(text):
        if not _NEGATION.search(text[max(0, m.start() - 80):m.start()]):
            return m.group(0)
    return None
CATEGORIES = ["new option or parameter", "new command or API", "validation rule", "output formatting",
              "parsing or conversion", "configuration or environment", "error reporting", "interaction between features"]


def new_names(p: TaskProposal) -> list[str]:
    return [t.split(":", 1)[1] for t in p.tags if t.startswith("new_name:")]


def category(p: TaskProposal) -> str:
    return next((t for t in p.tags if not t.startswith("new_name:")), "other")


# --------------------------------------------------------------------------- deterministic checks


def deterministic_checks(p: TaskProposal, repo: Path, language: str | None) -> tuple[list[str], list[str]]:
    """(hard objections, fixed expected files). Run before any LLM critic."""
    objections = []
    text = f"{p.title} {p.summary}"
    m = BUG_FIX_TEXT.search(text)
    if m:
        objections.append(f"reads as a bug fix ({m.group(0)!r}): tasks must be features or behaviour changes")
    m = PERF_ONLY.search(f"{p.title} {p.summary}")
    if m:
        objections.append(f"performance-only change ({m.group(0)!r}): not a feature, and not testable deterministically")
    need = needs_network(f"{p.behaviour_spec} {' '.join(p.test_plan)}")
    if need:
        objections.append(f"needs network access or credentials ({need!r})")
    if EXPLOIT.search(f"{p.title} {p.summary} {p.behaviour_spec}"):
        objections.append("looks like a security exploit")
    if len(p.behaviour_spec) < MIN_SPEC:
        objections.append(f"behaviour spec too short ({len(p.behaviour_spec)} characters) to be testable")
    if len(p.test_plan) < 3:
        objections.append("test plan has fewer than three behaviours")
    sources = set(source_files(repo, language))
    files = [f for f in p.expected_files if f in sources and not _is_test(f)]
    if not files:
        objections.append(f"expected files are not existing source files: {p.expected_files[:3]}")
    corpus = None
    for name in new_names(p):
        if len(name) < 5:
            continue
        if corpus is None:
            corpus = "\n".join((repo / s).read_text(encoding="utf-8", errors="replace") for s in sources)
        if defines(corpus, name):
            objections.append(f"the repo already defines {name!r}: the feature may exist")
            break
    return objections, files


# --------------------------------------------------------------------------- variation filter


def proposal_similarity(a: TaskProposal, b: TaskProposal) -> float:
    ta = words(f"{a.title} {a.summary} {' '.join(a.test_plan)}")
    tb = words(f"{b.title} {b.summary} {' '.join(b.test_plan)}")
    # Text only: research ideas citing the same peer reference are often different features.
    return jaccard(ta, tb)


def strength(p: TaskProposal) -> tuple:
    passes = sum(1 for r in p.reviews if r.verdict == "pass")
    return (passes, len(p.test_plan), len(p.behaviour_spec))


def near_duplicates(props: list[TaskProposal], threshold: float = 0.5) -> list[tuple[TaskProposal, TaskProposal]]:
    """(weaker, stronger) pairs; each weaker one is merged into a stronger one that stays."""
    order = sorted(props, key=strength, reverse=True)
    kept: list[TaskProposal] = []
    merged = []
    for p in order:
        dup = next((k for k in kept if proposal_similarity(p, k) >= threshold), None)
        if dup is None:
            kept.append(p)
        else:
            merged.append((p, dup))
    return merged


def distinct_order(props: list[TaskProposal]) -> list[TaskProposal]:
    """Build order that spreads categories, files and peers: new ones first, then strength."""
    pool = sorted(props, key=strength, reverse=True)
    out: list[TaskProposal] = []
    while pool:
        cats = {category(p) for p in out}
        files = {p.expected_files[0] for p in out if p.expected_files}
        peers = {p.provenance[0].peer_repo for p in out if p.provenance}
        pool.sort(key=lambda p: (category(p) in cats, bool(p.expected_files) and p.expected_files[0] in files,
                                 bool(p.provenance) and p.provenance[0].peer_repo in peers))
        out.append(pool.pop(0))
    return out


def distinct_count(props: list[TaskProposal], threshold: float = 0.5) -> int:
    kept: list[TaskProposal] = []
    for p in props:
        if all(proposal_similarity(p, k) < threshold for k in kept):
            kept.append(p)
    return len(kept)
