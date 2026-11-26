"""Web search for the research agent: feature requests, comparisons, blog posts, Q&A about the domain.

Provider by environment key: ``TAVILY_API_KEY``, ``BRAVE_SEARCH_API_KEY`` or ``SERPAPI_API_KEY``. With
none set, the tools are not offered and the agents work from the repo, the peers and the upstream.
Queries carry feature words only (the agents are told never to paste code); every result and page is
saved in the evidence snapshot, so a run replays offline.
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import urllib.parse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .snapshot import Snapshot

PROVIDERS = ("TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY", "SERPAPI_API_KEY")


def feature_queries(profile, limit: int = 4) -> list[str]:
    """Search prompts from public repository metadata, never source code.

    The researcher receives these results before it proposes ideas. Capability names and behaviours
    make the queries concrete enough to find feature discussions beyond the closest peer repositories.
    """
    domain = " ".join(str(x) for x in (getattr(profile, "purpose", ""), *getattr(profile.ecosystem, "topics", [])) if x)
    queries = []
    for capability in getattr(profile, "capability_map", []):
        phrase = " ".join(str(x) for x in (domain, capability.name, capability.behaviour) if x)
        if phrase:
            queries.append(f"{phrase} feature requests alternatives")
    if domain:
        queries.append(f"{domain} feature requests")
    return list(dict.fromkeys(queries))[:limit]


def provider() -> str | None:
    return next((p for p in PROVIDERS if os.environ.get(p)), None)


def _get(url: str, headers: dict | None = None, data: bytes | None = None, timeout: int = 30) -> bytes:
    req = Request(url, data=data, headers={"User-Agent": "task-factory", **(headers or {})})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https only, checked by the callers
        return resp.read(2_000_000)


def _search_live(query: str, n: int = 8) -> list[dict]:
    p = provider()
    if p == "TAVILY_API_KEY":
        body = json.dumps({"api_key": os.environ[p], "query": query, "max_results": n}).encode()
        data = json.loads(_get("https://api.tavily.com/search", {"Content-Type": "application/json"}, body))
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:300]}
                for r in data.get("results") or []]
    if p == "BRAVE_SEARCH_API_KEY":
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": n})
        data = json.loads(_get(url, {"X-Subscription-Token": os.environ[p], "Accept": "application/json"}))
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")[:300]}
                for r in (data.get("web") or {}).get("results") or []]
    if p == "SERPAPI_API_KEY":
        url = "https://serpapi.com/search.json?" + urllib.parse.urlencode({"q": query, "num": n, "api_key": os.environ[p]})
        data = json.loads(_get(url))
        return [{"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")[:300]}
                for r in data.get("organic_results") or []]
    raise RuntimeError("no web search provider configured")


def _public_https(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        for info in socket.getaddrinfo(parts.hostname, 443):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except (socket.gaierror, ValueError):
        return False
    return True


def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|nav|footer|header|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


class WebSearch:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    @staticmethod
    def available() -> bool:
        return provider() is not None

    def search(self, query: str) -> list[dict]:
        return self.snapshot.fetch("web_search", {"q": query}, lambda: _search_live(query))

    def read(self, url: str) -> str:
        def live() -> str:
            if not _public_https(url):
                return "only public https pages can be read"
            try:
                return page_text(_get(url).decode("utf-8", errors="replace"))[:20_000]
            except (HTTPError, URLError, OSError, ValueError) as exc:
                return f"could not read the page: {exc}"
        return self.snapshot.fetch("web_page", {"url": url}, live)
