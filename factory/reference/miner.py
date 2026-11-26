"""Reference bundles from peer PRs (v2.2 section 5); a tool of the research and critic agents.

A merged PR the research agent looks at becomes a ReferenceBundle: PR and linked issue, review
discussion, implementation diff, the PR's tests, neighbouring tests, follow-up fixes, docs and
changelog. Bundles live in the evidence snapshot and the state store, never in a task folder. The
author and the test writer get their own views (``for_author``, ``for_test_writer``), so the
controller - not a prompt - enforces who sees what.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .github import GitHub
from .schema import PeerRepo, PullRef, ReferenceBundle

log = logging.getLogger("factory.reference.miner")

BUGFIX = re.compile(r"\b(fix(e[sd])?|bug|bugfix|hotfix|regression|crash|typo|revert|security|cve|patch release)\b", re.I)
NOT_FEATURE = re.compile(r"\b(docs?|documentation|readme|ci|chore|bump|deps|dependabot|refactor|lint|format(ting)?|"
                         r"release|changelog|typing|types only|cleanup|internal|test(s|ing)? only)\b", re.I)
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|testing)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$")
DOC_PATH = re.compile(r"(^|/)(docs?|documentation)/|(^|/)(CHANGES|CHANGELOG|HISTORY|NEWS|README)[^/]*$|\.(md|rst|txt)$", re.I)
SOURCE_EXT = re.compile(r"\.(py|pyi|ts|tsx|js|mjs|cjs|cpp|cc|cxx|c|h|hpp|hh|ipp|go|rs|java|kt)$")
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
_NAMES = [
    re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_]\w+)", re.M),
    re.compile(r"^\+\s*class\s+([A-Za-z_]\w+)", re.M),
    re.compile(r"^\+\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]+)", re.M),
    re.compile(r"^\+\s*export\s+(?:const|let|class|type|interface)\s+([A-Za-z_$][\w$]+)", re.M),
    re.compile(r"^\+.*['\"](--[a-z][a-z0-9-]{3,})['\"]", re.M),
]


@dataclass
class MinerConfig:
    prs_per_peer: int = 6
    search_per_gap: int = 8
    gap_items: int = 10  # gap capabilities searched per peer
    min_relevance: float = 0.18
    impl_chars: int = 12_000
    tests_chars: int = 12_000
    docs_chars: int = 4_000
    discussion_chars: int = 6_000
    neighbour_chars: int = 8_000
    followups: int = 3


def added_names(diff: str) -> list[str]:
    out = []
    for rx in _NAMES:
        for m in rx.finditer(diff or ""):
            n = m.group(1)
            if n.startswith("_") and not n.startswith("--"):
                continue
            out.append(n)
    return list(dict.fromkeys(out))[:20]


def _trim(text: str, limit: int, label: str, cut: list[str]) -> str:
    if len(text) <= limit:
        return text
    cut.append(label)
    return text[:limit] + f"\n... ({len(text) - limit} characters cut)"


def _patch(f: dict) -> str:
    head = f"--- a/{f.get('previous_filename') or f['filename']}\n+++ b/{f['filename']}\n"
    return head + (f.get("patch") or "(binary or too large to show)") + "\n"


# --------------------------------------------------------------------------- role views


def for_author(b: ReferenceBundle) -> dict:
    """The peer implementation as a design reference. Never any tests."""
    return {"pr_title": b.pr.title, "pr_description": b.pr.body[:3000], "implementation_diff": b.impl_diff}


def for_test_writer(b: ReferenceBundle) -> dict:
    """Peer tests, edge cases from the discussion and follow-up fixes. Never our patch or peer code."""
    return {"pr_title": b.pr.title, "peer_tests": b.tests, "neighbouring_tests": b.neighbour_tests,
            "edge_cases_from_review": b.discussion,
            "followup_fixes": [{"title": f.get("title"), "body": (f.get("body") or "")[:800]} for f in b.followup_fixes]}



# --------------------------------------------------------------------------- miner


class Miner:
    def __init__(self, agent, github: GitHub, snapshot_id: str, cfg: MinerConfig | None = None,
                 workers: int = 1, starter_purpose: str = "") -> None:
        self.agent = agent
        self.workers = workers
        self.starter_purpose = starter_purpose
        self.gh = github
        self.snapshot_id = snapshot_id
        self.cfg = cfg or MinerConfig()

    # ------------------------------------------------------------------ bundle

    def bundle(self, peer: PeerRepo, number: int) -> ReferenceBundle | None:
        pr = self.gh.pull(peer.full_name, number)
        if not pr or not pr.get("merged_at"):
            return None
        files = self.gh.pull_files(peer.full_name, number)
        tests = [f for f in files if TEST_PATH.search(f["filename"])]
        docs = [f for f in files if f not in tests and DOC_PATH.search(f["filename"])]
        impl = [f for f in files if f not in tests and f not in docs and SOURCE_EXT.search(f["filename"])]
        if not tests or not impl:
            return None
        cut: list[str] = []
        issue = None
        m = CLOSES.search(pr.get("body") or "")
        if m:
            it = self.gh.issue(peer.full_name, int(m.group(1)))
            if it and not it.get("pull_request"):
                issue = {"number": it["number"], "title": it.get("title", ""), "body": (it.get("body") or "")[:3000],
                         "url": it.get("html_url", "")}
        discussion, used = [], 0
        for c in self.gh.review_comments(peer.full_name, number) + self.gh.issue_comments(peer.full_name, number):
            user = (c.get("user") or {}).get("login", "")
            body = (c.get("body") or "").strip()
            if not body or user.endswith("[bot]") or (c.get("user") or {}).get("type") == "Bot":
                continue
            where = f" ({c['path']})" if c.get("path") else ""
            text = f"{user}{where}: {body[:600]}"
            if used + len(text) > self.cfg.discussion_chars:
                cut.append("discussion")
                break
            discussion.append(text)
            used += len(text)
        base_sha = (pr.get("base") or {}).get("sha")
        neighbours, used = {}, 0
        for f in tests:
            if f.get("status") == "added" or used >= self.cfg.neighbour_chars:
                continue
            text = self.gh.file(peer.full_name, f["filename"], base_sha) or ""
            text = text[: self.cfg.neighbour_chars - used]
            if text:
                neighbours[f["filename"]] = text
                used += len(text)
        followups = []
        for it in self.gh.search_prs(peer.full_name, f"{number}", per_page=10):
            if int(it["number"]) == number or not BUGFIX.search(it.get("title") or ""):
                continue
            if (it.get("closed_at") or "") < (pr.get("merged_at") or ""):
                continue
            followups.append({"number": it["number"], "title": it.get("title", ""), "url": it.get("html_url", ""),
                              "body": (it.get("body") or "")[:1500]})
            if len(followups) >= self.cfg.followups:
                break
        return ReferenceBundle(
            pr=PullRef(number=number, title=pr.get("title", ""), url=pr.get("html_url", ""),
                       body=(pr.get("body") or "")[:8000], merged_at=pr.get("merged_at"),
                       labels=[lab.get("name", "") for lab in pr.get("labels") or []],
                       merge_commit=pr.get("merge_commit_sha"), base_commit=base_sha),
            issue=issue, discussion=discussion,
            impl_diff=_trim("".join(_patch(f) for f in impl), self.cfg.impl_chars, "impl_diff", cut),
            tests=_trim("".join(_patch(f) for f in tests), self.cfg.tests_chars, "tests", cut),
            neighbour_tests=neighbours, followup_fixes=followups,
            docs=_trim("".join(_patch(f) for f in docs), self.cfg.docs_chars, "docs", cut), truncated=cut)
