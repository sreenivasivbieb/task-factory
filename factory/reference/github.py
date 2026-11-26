"""GitHub REST access for the Scout and the Miner, every response saved in the evidence snapshot.

Uses the ``gh`` CLI when it is installed and logged in (higher rate limits, no token handling
here), otherwise HTTPS with ``$GITHUB_TOKEN`` when set. Only capability descriptions and public
repository names go out; never the starter repo's source code.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..errors import EnvironmentProblem
from .snapshot import Snapshot

log = logging.getLogger("factory.reference.github")

API = "https://api.github.com"


class GitHubError(EnvironmentProblem):
    pass


def parse_repo(url_or_name: str) -> str | None:
    """owner/name from a GitHub URL or an ``owner/name`` string."""
    s = url_or_name.strip()
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", s)
    return f"{m.group(1)}/{m.group(2)}" if m else None


class GitHub:
    def __init__(self, snapshot: Snapshot, transport=None) -> None:
        self.snapshot = snapshot
        self._transport = transport
        self._gh = shutil.which("gh") if transport is None else None
        self.calls = 0

    # ------------------------------------------------------------------ transport

    def _live(self, route: str) -> object:
        if self._transport is not None:
            return self._transport(route)
        self.calls += 1
        if self._gh:
            r = subprocess.run([self._gh, "api", "-H", "Accept: application/vnd.github+json", route],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                return json.loads(r.stdout or "null")
            msg = (r.stderr or r.stdout).strip()
            if "404" in msg or "Not Found" in msg:
                return None
            if "rate limit" in msg.lower():
                time.sleep(20)
                return self._live_http(route)
            log.warning("gh api %s failed (%s); trying HTTPS", route, msg[:200])
        return self._live_http(route)

    def _live_http(self, route: str) -> object:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "task-factory"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(3):
            try:
                with urlopen(Request(f"{API}/{route.lstrip('/')}", headers=headers), timeout=60) as resp:
                    return json.load(resp)
            except HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code in (403, 429) and attempt < 2:
                    time.sleep(30 * (attempt + 1))
                    continue
                raise GitHubError(f"GitHub API {route}: HTTP {exc.code}") from exc
            except (URLError, OSError, ValueError) as exc:
                if attempt < 2:
                    time.sleep(3)
                    continue
                raise GitHubError(f"GitHub API {route} unreachable: {exc}") from exc
        return None

    def get(self, route: str):
        return self.snapshot.fetch("github", route, lambda: self._live(route))

    # ------------------------------------------------------------------ endpoints

    def search_repos(self, query: str, per_page: int = 15) -> list[dict]:
        q = urllib.parse.quote(query)
        data = self.get(f"search/repositories?q={q}&sort=stars&order=desc&per_page={per_page}") or {}
        return list(data.get("items") or [])

    def repo(self, full_name: str) -> dict | None:
        return self.get(f"repos/{full_name}")

    def tree(self, full_name: str, ref: str) -> list[str]:
        data = self.get(f"repos/{full_name}/git/trees/{urllib.parse.quote(ref)}?recursive=1") or {}
        return [t["path"] for t in data.get("tree") or [] if t.get("type") == "blob"]

    def file(self, full_name: str, path: str, ref: str | None = None) -> str | None:
        route = f"repos/{full_name}/contents/{urllib.parse.quote(path)}"
        if ref:
            route += f"?ref={urllib.parse.quote(ref)}"
        data = self.get(route)
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(data.get("content") or "").decode("utf-8", errors="replace")
        except ValueError:
            return None

    def search_prs(self, full_name: str, terms: str = "", per_page: int = 30, extra: str = "is:merged") -> list[dict]:
        q = urllib.parse.quote(f"repo:{full_name} is:pr {extra} {terms}".strip())
        data = self.get(f"search/issues?q={q}&per_page={per_page}") or {}
        return list(data.get("items") or [])

    def merged_pulls(self, full_name: str, pages: int = 2) -> list[dict]:
        out = []
        for page in range(1, pages + 1):
            data = self.get(f"repos/{full_name}/pulls?state=closed&sort=updated&direction=desc"
                            f"&per_page=100&page={page}") or []
            out += [p for p in data if p.get("merged_at")]
            if len(data) < 100:
                break
        return out

    def pull(self, full_name: str, number: int) -> dict | None:
        return self.get(f"repos/{full_name}/pulls/{number}")

    def pull_files(self, full_name: str, number: int, pages: int = 3) -> list[dict]:
        out = []
        for page in range(1, pages + 1):
            data = self.get(f"repos/{full_name}/pulls/{number}/files?per_page=100&page={page}") or []
            out += data
            if len(data) < 100:
                break
        return out

    def review_comments(self, full_name: str, number: int) -> list[dict]:
        return list(self.get(f"repos/{full_name}/pulls/{number}/comments?per_page=100") or [])

    def issue_comments(self, full_name: str, number: int) -> list[dict]:
        return list(self.get(f"repos/{full_name}/issues/{number}/comments?per_page=100") or [])

    def issue(self, full_name: str, number: int) -> dict | None:
        return self.get(f"repos/{full_name}/issues/{number}")

    def releases(self, full_name: str, per_page: int = 30) -> list[dict]:
        return list(self.get(f"repos/{full_name}/releases?per_page={per_page}") or [])

    def commit_date(self, full_name: str, sha: str) -> str | None:
        data = self.get(f"repos/{full_name}/commits/{sha}")
        try:
            return data["commit"]["committer"]["date"]
        except (TypeError, KeyError):
            return None
