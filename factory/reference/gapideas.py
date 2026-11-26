"""Capability references: a peer capability (from the scout's gap list) with the peer's own code.

The research agent's ``peer_capability`` tool uses this: the capability's evidence files and the peer
tests next to them become a ReferenceBundle (instead of a PR diff), so an idea inspired by what a peer
CAN do still carries the peer's code and tests to the author and the test writer.
"""
from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from .github import GitHub
from .miner import TEST_PATH
from .schema import Capability, PeerRepo, PullRef, ReferenceBundle

log = logging.getLogger("factory.reference.gapideas")

def _excerpt(text: str, symbol: str, limit: int) -> str:
    """The part of a file around ``symbol`` (or its head), trimmed to ``limit`` characters."""
    if symbol:
        m = re.search(r"(?m)^[ \t]*(?:async\s+)?(?:def|class|function|const|export\s+\w+)\s+" + re.escape(symbol) + r"\b",
                      text) or re.search(re.escape(symbol), text)
        if m:
            start = text.rfind("\n", 0, max(0, m.start() - 200)) + 1
            return text[start:start + limit]
    return text[:limit]


class CapabilityBundles:
    def __init__(self, github: GitHub, code_chars: int = 8_000, test_chars: int = 6_000) -> None:
        self.gh = github
        self.code_chars = code_chars
        self.test_chars = test_chars

    def bundle(self, peer: PeerRepo, cap: Capability, why: str = "") -> ReferenceBundle | None:
        ref = peer.default_branch or "HEAD"
        code, files = [], []
        for ev in cap.evidence[:3]:
            path, _, symbol = ev.partition(":")
            path = path.strip().lstrip("./")
            text = self.gh.file(peer.full_name, path, ref) if path else None
            if not text:
                continue
            symbol = re.sub(r"\(.*$", "", symbol.strip()).split(".")[-1]
            code.append(f"# {peer.full_name}/{path}\n{_excerpt(text, symbol, self.code_chars // 2)}")
            files.append(path)
        if not code:
            return None
        tests: dict[str, str] = {}
        stems = {PurePosixPath(f).stem.lower() for f in files}
        tree = self.gh.tree(peer.full_name, ref)
        for t in [t for t in tree if TEST_PATH.search(t) and any(s and s in t.lower() for s in stems)][:2]:
            text = self.gh.file(peer.full_name, t, ref)
            if text:
                tests[t] = text[: self.test_chars // 2]
        url = f"https://github.com/{peer.full_name}/blob/{ref}/{files[0]}"
        return ReferenceBundle(
            pr=PullRef(number=0, title=f"capability: {cap.name}", url=url, body=f"{cap.behaviour}\n\n{why}".strip()),
            impl_diff="\n\n".join(code)[: self.code_chars],
            tests="\n\n".join(f"# {k}\n{v}" for k, v in tests.items())[: self.test_chars],
            neighbour_tests=tests, docs="", truncated=[])
