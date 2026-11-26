"""Research agent, critic agent and spec writer: agents that LOOK before they decide.

The earlier mechanical path (peer PR -> word-matching novelty -> drafter -> critic) let ideas through
that nobody had checked against the starter's code, and rejected others on guesses. Here every
decision is made by an agent with tools over the evidence:

- Researcher: reads the starter repo (tree, search, read), the peers (gap capabilities, merged PRs,
  their code) and the starter's upstream (merged PRs, open issues), then proposes ideas that fit,
  each with where it would live in the code, why it fits, how to test it, and references when it has
  them (references are optional: user choice 2026-11-26).
- IdeaCritic (another model, fresh context, same tools): is it already in the starter (code, docs,
  upstream)? Feasible as one focused change? Deterministically testable, no network, no new
  dependency? Verdict accept / fix / reject; one refinement round for "fix".
- SpecWriter: turns an accepted idea into the task proposal, reading the code to get names, defaults
  and messages right.

Tools are called through a JSON action protocol, so any model works (Bedrock, OpenCode, HTTP):
the model answers {"tool": name, "args": {...}} or {"final": ...}; every step is a cached call, so a
run replays offline from its snapshot.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import EnvironmentProblem, ModelProviderError
from ..generation.ideas import _SKIP_DIRS, newest_refs, source_files
from .github import GitHub, parse_repo
from .miner import Miner, for_author
from .schema import PeerFeature, PeerRepo, RepoProfile

log = logging.getLogger("factory.reference.research")

OBS_LIMIT = 3_500
_TEXT_EXT = {".py", ".pyi", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".go",
             ".rs", ".java", ".kt", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml"}


# =========================================================================== tools


class Toolbox:
    """What the agents may look at: the starter repo, the peers, the starter's upstream."""

    def __init__(self, repo: Path, profile: RepoProfile, github: GitHub, peers: list[PeerRepo], miner: Miner,
                 upstream: Path | None = None, base_commit: str | None = None, web=None,
                 existing_tasks: list[dict] | None = None) -> None:
        self.web = web  # WebSearch, or None when no provider key is set
        self.web_seed: list[tuple[str, list[dict]]] = []
        self.existing_tasks = existing_tasks or []
        self.repo = Path(repo)
        self.profile = profile
        self.gh = github
        self.peers = {p.full_name.lower(): p for p in peers}
        self.miner = miner
        self.upstream = upstream
        self.base_commit = base_commit
        self.starter = parse_repo(profile.url or "")
        self._refs = newest_refs(upstream, base_commit) if upstream else []
        self.features: dict[str, PeerFeature] = {}  # references the agents looked at, by "peer#number" / "peer:capability"
        self.sources = source_files(self.repo, profile.language)
        self._files = sorted(p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*")
                             if p.is_file() and p.suffix in _TEXT_EXT
                             and not any(part in _SKIP_DIRS - {"docs", "examples"} or part.startswith(".")
                                         for part in p.relative_to(self.repo).parts[:-1]))

    SPEC = {
        "repo_tree": ("files of the starter repo under a folder", {"prefix": "src/pkg (optional)"}),
        "repo_search": ("regex search in the starter repo's files (code, docs, tests)", {"pattern": "regex", "path": "folder or file prefix (optional)"}),
        "repo_read": ("read lines of a starter repo file", {"path": "file", "start": "first line (default 1)", "end": "last line (default start+150)"}),
        "peers": ("the peer projects and the capabilities they have that the starter lacks", {}),
        "peer_docs": ("a peer's documentation files: README, docs/, design and architecture notes", {"peer": "owner/name"}),
        "peer_read": ("read a peer documentation file (where its features are described)", {"peer": "owner/name", "path": "file", "start": "first line (default 1)", "end": "last line"}),
        "peer_search_prs": ("search a peer's merged pull requests", {"peer": "owner/name", "query": "words"}),
        "peer_pr": ("a peer pull request: description, files, code diff, tests", {"peer": "owner/name", "number": "PR number"}),
        "peer_capability": ("a peer capability with the peer's code for it", {"peer": "owner/name", "name": "capability name"}),
        "upstream_search": ("the starter's own GitHub pull requests or issues (merged/open), to see if a feature exists or was declined", {"query": "words", "state": "merged | open | closed"}),
        "upstream_grep": ("is this name defined on a NEWER upstream branch of the starter?", {"name": "identifier"}),
        "existing_tasks": ("already-built task packages for this repository, with their visible instructions", {}),
        "web_context": ("web-search results automatically seeded from the starter repository's capability profile", {}),
        "web_search": ("search the web (feature requests, comparisons, blog posts, Q&A); feature words only, never code", {"query": "words"}),
        "web_read": ("read a public web page found by web_search", {"url": "https://..."}),
    }

    def describe(self, names: list[str]) -> str:
        return "\n".join(f"- {n}: {self.SPEC[n][0]}; args {json.dumps(self.SPEC[n][1])}" for n in names)

    def call(self, name: str, args: dict) -> str:
        fn = getattr(self, f"t_{name}", None)
        if fn is None or name not in self.SPEC:
            return f"unknown tool {name!r}; tools: {', '.join(self.SPEC)}"
        try:
            out = fn(**{k: v for k, v in (args or {}).items() if k in self.SPEC[name][1]})
        except (EnvironmentProblem, ModelProviderError):
            raise
        except Exception as exc:  # noqa: BLE001 - a bad call is an observation, not a crash
            out = f"error: {type(exc).__name__}: {exc}"
        out = str(out)
        return out if len(out) <= OBS_LIMIT else out[:OBS_LIMIT] + f"\n... ({len(out) - OBS_LIMIT} characters cut)"

    # ------------------------------------------------------------------ starter repo

    def _safe(self, path: str) -> Path:
        p = (self.repo / str(path).lstrip("/")).resolve()
        if not p.is_relative_to(self.repo.resolve()):
            raise ValueError("path outside the repository")
        return p

    def t_repo_tree(self, prefix: str = "") -> str:
        files = [f for f in self._files if f.startswith(str(prefix or "").strip("/"))]
        return "\n".join(files[:200]) + (f"\n... {len(files) - 200} more" if len(files) > 200 else "") or "no files"

    def t_repo_search(self, pattern: str, path: str = "") -> str:
        try:
            rx = re.compile(str(pattern), re.I)
        except re.error:
            rx = re.compile(re.escape(str(pattern)), re.I)
        hits = []
        for f in self._files:
            if path and not f.startswith(str(path).strip("/")):
                continue
            for n, line in enumerate((self.repo / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{n}: {line.strip()[:160]}")
                    if len(hits) >= 40:
                        return "\n".join(hits) + "\n... (more matches: narrow the pattern or path)"
        return "\n".join(hits) or "no matches"

    def t_repo_read(self, path: str, start=1, end=None) -> str:
        p = self._safe(path)
        if not p.is_file():
            near = [f for f in self._files if Path(f).name == Path(str(path)).name][:5]
            return f"no such file {path}" + (f"; did you mean {', '.join(near)}?" if near else "")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end or start + 150), start + 200)
        return f"{path} lines {start}-{end} of {len(lines)}\n" + "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))

    # ------------------------------------------------------------------ peers

    def _peer(self, peer: str) -> PeerRepo:
        p = self.peers.get(str(peer).lower())
        if p is None:
            raise ValueError(f"unknown peer {peer!r}; peers: {', '.join(self.peers)}")
        return p

    def t_peers(self) -> str:
        out = []
        for p in self.peers.values():
            gap = "; ".join(c.name for c in p.gap[:20])
            tier = "primary" if p.status == "selected" else "additional vetted"
            out.append(f"{p.full_name} [{tier}] ({p.role or 'peer'}, {p.language}, licence {p.licence}): "
                       f"lacks in starter -> {gap}")
        return "\n".join(out) or "no peers"

    DOC = re.compile(r"(^|/)(readme|architecture|design|features?|guide|usage|changelog|changes|history)[^/]*$|"
                     r"(^|/)(docs?|documentation|design|rfcs?|adr)/.*\.(md|rst|txt)$", re.I)

    def t_peer_docs(self, peer: str) -> str:
        p = self._peer(peer)
        tree = self.gh.tree(p.full_name, p.default_branch or "HEAD")
        docs = [t for t in tree if self.DOC.search(t) and not t.lower().endswith((".png", ".svg", ".jpg"))]
        docs.sort(key=lambda t: (not re.search(r"(?i)readme|design|architecture", t), t.count("/"), t))
        return "\n".join(docs[:80]) or "no documentation files found"

    def t_peer_read(self, peer: str, path: str, start=1, end=None) -> str:
        p = self._peer(peer)
        ref = p.default_branch or "HEAD"
        text = self.gh.file(p.full_name, str(path), ref)
        if text is None:
            return f"no such file {path} in {p.full_name}; list them with peer_docs"
        lines = text.splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end or start + 150), start + 200)
        url = f"https://github.com/{p.full_name}/blob/{ref}/{path}"
        key = f"{p.full_name}:{path}"
        if key not in self.features:
            from .schema import PullRef, ReferenceBundle
            self.features[key] = PeerFeature(
                id=f"doc-{p.full_name}-{path}".replace("/", "-")[:90], peer_repo=p.full_name, pr_url=url,
                summary=f"{p.full_name} documentation: {path}", licence=p.licence, language=p.language,
                snapshot_id=self.miner.snapshot_id if self.miner else "", source="doc",
                reference_bundle=ReferenceBundle(pr=PullRef(number=0, title=f"doc: {path}", url=url), docs=text[:8000]))
        return f"{url} lines {start}-{end} of {len(lines)}\n" + "\n".join(f"{n}: {ln}" for n, ln in
                                                                           enumerate(lines[start - 1:end], start))

    def t_peer_search_prs(self, peer: str, query: str) -> str:
        p = self._peer(peer)
        items = self.gh.search_prs(p.full_name, str(query), per_page=15)
        return "\n".join(f"#{i['number']} {i.get('title', '')}" for i in items) or "no merged PRs match"

    def t_peer_pr(self, peer: str, number) -> str:
        p = self._peer(peer)
        b = self.miner.bundle(p, int(number))
        if b is None:
            return "not usable: not merged, or no source and test changes"
        key = f"{p.full_name}#{int(number)}"
        self.features[key] = PeerFeature(
            id=f"pr-{p.full_name}-{int(number)}".replace("/", "-"), peer_repo=p.full_name, pr_url=b.pr.url,
            issue_url=(b.issue or {}).get("url"), summary=b.pr.title, reference_bundle=b, licence=p.licence,
            language=p.language, snapshot_id=self.miner.snapshot_id)
        files = re.findall(r"^\+\+\+ b/(.+)$", b.impl_diff + b.tests, re.M)
        return (f"{b.pr.title} ({b.pr.url})\n{b.pr.body[:1200]}\nfiles: {', '.join(files[:15])}\n"
                f"--- code ---\n{for_author(b)['implementation_diff'][:1500]}\n--- tests ---\n{b.tests[:700]}")

    def t_peer_capability(self, peer: str, name: str) -> str:
        from .gapideas import CapabilityBundles

        p = self._peer(peer)
        cap = next((c for c in p.gap if c.name.lower() == str(name).lower()), None)
        if cap is None:
            return f"no gap capability {name!r} for {p.full_name}; see the peers tool"
        b = CapabilityBundles(self.gh).bundle(p, cap)
        if b is None:
            return f"{cap.name}: {cap.behaviour} (no code found at its evidence)"
        self.features[f"{p.full_name}:{cap.name}"] = PeerFeature(
            id=f"cap-{p.full_name}-{cap.name}".replace("/", "-").replace(" ", "-")[:80], peer_repo=p.full_name,
            pr_url=b.pr.url, summary=cap.behaviour, gap_capability=cap.name, reference_bundle=b, licence=p.licence,
            language=p.language, snapshot_id=self.miner.snapshot_id, source="capability")
        return f"{cap.name}: {cap.behaviour}\n{b.pr.url}\n{b.impl_diff[:2500]}"

    # ------------------------------------------------------------------ upstream

    def t_upstream_search(self, query: str, state: str = "merged") -> str:
        if not self.starter:
            return "the starter repo is not on GitHub: no upstream search"
        extra = {"merged": "is:merged", "open": "is:open", "closed": "is:closed is:unmerged"}.get(str(state), "is:merged")
        items = self.gh.search_prs(self.starter, str(query), per_page=10, extra=extra)
        if state != "merged":
            import urllib.parse

            q = urllib.parse.quote(f"repo:{self.starter} is:issue is:{'open' if state == 'open' else 'closed'} {query}")
            items += (self.gh.get(f"search/issues?q={q}&per_page=10") or {}).get("items") or []
        return "\n".join(f"{i.get('html_url', '')} [{i.get('state')}] {i.get('title', '')}" for i in items[:20]) or "nothing found"

    def t_existing_tasks(self) -> str:
        if not self.existing_tasks:
            return "no existing task packages"
        return json.dumps(self.existing_tasks, ensure_ascii=False)

    def t_web_search(self, query: str) -> str:
        if self.web is None:
            return "web search is not configured"
        res = self.web.search(str(query))
        return "\n".join(f"{r['url']} | {r['title']} | {r['snippet']}" for r in res) or "no results"

    def seed_web_context(self, queries: list[str]) -> None:
        """Fetch capability-derived web results once, before the researcher starts its tool loop.

        This makes web research part of idea generation when a search provider is configured, while
        retaining the snapshot's offline replay behaviour.
        """
        if self.web is None:
            return
        self.web_seed = [(query, self.web.search(query)) for query in queries]

    def t_web_context(self) -> str:
        if self.web is None:
            return "web search is not configured"
        if not self.web_seed:
            return "no seeded web results"
        sections = []
        for query, results in self.web_seed:
            rows = [f"{r['url']} | {r['title']} | {r['snippet']}" for r in results]
            sections.append(f"query: {query}\n" + ("\n".join(rows) or "no results"))
        return "\n\n".join(sections)

    def t_web_read(self, url: str) -> str:
        if self.web is None:
            return "web search is not configured"
        return self.web.read(str(url))

    def available(self, names: list[str]) -> list[str]:
        """The tools that can work in this run (web tools only with a provider)."""
        return [n for n in names if not (n.startswith("web_") and self.web is None)]

    def t_upstream_grep(self, name: str) -> str:
        if not self._refs:
            return "no newer upstream branch available"
        for ref in self._refs:
            r = subprocess.run(["git", "-C", str(self.upstream), "grep", "-n", "-w", "-F", "-e", str(name), ref, "--",
                                ":(exclude)tests", ":(exclude)test"], capture_output=True, text=True)
            if r.returncode == 0:
                return f"yes, on {ref[:10]}:\n" + "\n".join(r.stdout.splitlines()[:10])
        return f"no: {name!r} is not on the newer upstream branches"


# =========================================================================== the action loop


@dataclass
class AgentRun:
    final: object = None
    steps: list[dict] = field(default_factory=list)  # {"tool", "args"}


def _json_objects(text: str) -> list[dict]:
    """Every top-level JSON object in a reply (models send several tool calls in one turn)."""
    from ..generation.author import _repair_json

    out, i, dec = [], 0, json.JSONDecoder(strict=False)
    while True:
        i = text.find("{", i)
        if i < 0:
            return out
        for candidate in (text[i:], _repair_json(text[i:])):
            try:
                obj, end = dec.raw_decode(candidate)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
            i += end if candidate is text[i:] else 1
            break
        else:
            i += 1


def _transcript(transcript: list[tuple[str, str]]) -> str:
    lines = []
    for i, (act, obs) in enumerate(transcript, 1):
        keep = obs if i > len(transcript) - 6 else obs[:400] + (" ..." if len(obs) > 400 else "")
        lines.append(f"[{i}] {act}\n=> {keep}")
    return "\n".join(lines)


def run_agent(agent, instructions: str, task: str, tools: Toolbox, names: list[str], final_shape: str,
              max_steps: int = 12, calls_per_turn: int = 4) -> AgentRun:
    """Ask ``agent`` turn by turn; each turn is one fresh, cached call with the whole transcript. Several
    tool calls in one reply are all run (up to ``calls_per_turn``); a model that never stops exploring
    gets a last call with no tools in the prompt at all."""
    run = AgentRun()
    names = tools.available(names)
    transcript: list[tuple[str, str]] = []
    for step in range(max_steps):
        last = step == max_steps - 1
        done = _transcript(transcript)
        prompt = (
            f"{instructions}\n"
            "Work step by step with the tools below. Each turn, reply with JSON: "
            '{"tool": "<name>", "args": {...}, "why": "<short>"} to use a tool (up to '
            f"{calls_per_turn} such objects in one reply run together), or "
            '{"final": <answer>} when you are done. Look things up instead of guessing.\n'
            f"Tools:\n{tools.describe(names)}\n"
            f"Final answer shape: {final_shape}\n"
            f"Task: {task}\n"
            + ("Your steps so far:\n" + done + "\n" if done else "")
            + f"This is turn {step + 1} of {max_steps}.\n"
            + ("Few turns left: stop exploring soon and write the final answer.\n"
               if not last and step >= int(max_steps * 0.7) else "")
            + ("THIS IS YOUR LAST TURN: reply with {\"final\": ...} now.\n" if last else "")
        )
        try:
            text = agent.ask(prompt)
        except (ModelProviderError, EnvironmentProblem):  # quota or replay miss: stop, never a verdict
            raise
        objs = _json_objects(text or "")
        final = next((o["final"] for o in objs if "final" in o), None)
        if final is not None:
            run.final = final
            return run
        calls = [o for o in objs if o.get("tool")]
        if not calls:
            if objs:  # models often send the final answer without the {"final": ...} wrapper
                run.final = objs[0]
                return run
            transcript.append(("(no JSON)", "your reply had no JSON object; reply with JSON only"))
            continue
        for o in calls[:calls_per_turn]:
            name = str(o["tool"])
            args = o.get("args") if isinstance(o.get("args"), dict) else {}
            if name not in names:
                transcript.append((f"tool={name!r}", f"not available here; use one of: {', '.join(names)}, or final"))
                continue
            run.steps.append({"tool": name, "args": args})
            transcript.append((f"tool={name} args={json.dumps(args, ensure_ascii=False)[:200]}", tools.call(name, args)))
    # it never stopped exploring: ask once more with no tools offered at all
    prompt = (f"{instructions}\nTask: {task}\nWhat you found:\n{_transcript(transcript)}\n"
              f"No tools are available now. Reply with ONLY the final answer as JSON shaped like {final_shape}.")
    try:
        objs = _json_objects(agent.ask(prompt) or "")
    except (ModelProviderError, EnvironmentProblem):
        raise
    if objs:
        run.final = objs[0].get("final", objs[0]) if not objs[0].get("tool") else None
    return run


# =========================================================================== the three agents


# Ideas come from peers' documentation (README, docs, design and architecture notes) - user choice 2026-11-26 -
# and from the starter's own code and upstream; not from peer pull requests.
RESEARCH_TOOLS = ["repo_tree", "repo_search", "repo_read", "peers", "peer_docs", "peer_read", "peer_capability",
                  "upstream_search", "web_context", "web_search", "web_read"]
CRITIC_TOOLS = ["repo_search", "repo_read", "upstream_search", "upstream_grep", "existing_tasks", "peer_read",
                "peer_capability", "web_search"]
SPEC_TOOLS = ["repo_search", "repo_read", "repo_tree"]

IDEA_SHAPE = ('{"title": "...", "kind": "feature | behavior_change", "summary": "2 sentences", '
              '"behaviour": "what the user can do afterwards, with names and defaults", '
              '"files": ["existing source files to change"], "where": "functions/classes you read that it touches", '
              '"references": [{"peer": "owner/name", "doc": "docs/file.md"} | {"peer": "owner/name", "capability": '
              '"name"} | {"url": "..."}], "why_fits": "...", "how_to_test": ["deterministic test 1", "..."]}')


def _profile_brief(p: RepoProfile) -> str:
    return json.dumps({"name": p.repo, "purpose": p.purpose, "language": p.language, "test_runner": p.test_runner,
                       "capabilities": [c.name for c in p.capability_map][:60], "design": p.design_traits[:10],
                       "boundaries": p.boundaries}, ensure_ascii=False)


# Research lanes: independent research agents that run AT THE SAME TIME, each from one kind of source,
# so one slow sequential tool loop does not decide how long research takes (user choice 2026-11-26).
# Each lane still reads the starter's code before it proposes; the critic checks every idea afterwards.
RESEARCH_LANES: dict[str, tuple[str, list[str]]] = {
    "peers": (
        "Your source is the PEER projects. First call peers and investigate the selected, highest-ranked peers "
        "before the additional vetted peers; expand to those when the primary peers give too few distinct ideas. "
        "Get ideas from how they DESCRIBE their features: README, docs, design and architecture notes "
        "(peer_docs, peer_read, peer_capability).",
        ["repo_tree", "repo_search", "repo_read", "peers", "peer_docs", "peer_read", "peer_capability"]),
    "starter": (
        "Your source is the STARTER itself and its upstream: what its own docs, code comments, extension points "
        "and option lists suggest is missing, and what users asked for or maintainers declined upstream "
        "(upstream_search). Do not use peers or the web.",
        ["repo_tree", "repo_search", "repo_read", "upstream_search"]),
    "web": (
        "Your source is the WEB: you MUST call web_context first, then use web_search and web_read for follow-up "
        "feature queries grounded in the repository profile (what users of this kind of library ask for, how "
        "other ecosystems solve it).",
        ["repo_tree", "repo_search", "repo_read", "web_context", "web_search", "web_read"]),
}


def research_lanes(tools: "Toolbox", wanted: int) -> list[str]:
    """The lanes this run can use (web only with a search provider), at most ``wanted`` (1 = one agent
    with every source, the behaviour before lanes)."""
    if wanted <= 1:
        return ["all"]
    lanes = [lane for lane in RESEARCH_LANES if lane != "web" or tools.web is not None]
    return lanes[:wanted]


def _title_key(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(title).lower()))


class Researcher:
    def __init__(self, agent, tools: Toolbox, max_steps: int = 24, lane_steps: int = 14) -> None:
        self.agent = agent
        self.tools = tools
        self.max_steps = max_steps  # one agent with every source
        self.lane_steps = lane_steps  # a lane has one kind of source, so it needs fewer turns

    def ideas(self, profile: RepoProfile, n: int, avoid: list[str], round_: int = 1,
              focus: str = "", lane: str = "all") -> tuple[list[dict], list[dict]]:
        if lane == "all":
            sources = (
                "First call peers and investigate the selected, highest-ranked peers before the additional vetted "
                "peers. When the primary peers do not provide enough distinct ideas, expand to those additional "
                "peers; they are valid sources too. Get ideas from how similar projects DESCRIBE their features: "
                "read their README, docs, design and architecture notes (peer_docs, peer_read); also from the "
                "starter's own code and upstream (what was asked for or declined). When web tools are available, "
                "you MUST call web_context before proposing ideas, then use web_search for follow-up feature "
                "queries grounded in the repository profile. ")
            names, steps = RESEARCH_TOOLS, self.max_steps
        else:
            sources, names = RESEARCH_LANES[lane]
            sources, steps = sources + " ", self.lane_steps
        run = run_agent(
            self.agent,
            f"You are the research agent of a coding-task factory for {profile.repo}. Find {n} realistic NEW "
            "features or deliberate behaviour changes a maintainer could accept, each a focused change to one "
            "to three existing source files, testable with the project's own test runner by deterministic "
            "unit tests. Never bug fixes, refactors, docs, performance work, network access, credentials or new "
            "third-party dependencies. " + sources +
            "READ the starter's code to make sure the idea fits and does not exist yet. References are welcome "
            "but optional. Make the ideas different from each other and from the ones already proposed or "
            "rejected.",
            json.dumps({"repository": json.loads(_profile_brief(profile)), "ideas_needed": n, "round": round_,
                        "focus": focus, "already_proposed_or_rejected": avoid[:60]}, ensure_ascii=False),
            self.tools, names, '{"ideas": [' + IDEA_SHAPE + ", ...]}", steps)
        final = run.final if isinstance(run.final, dict) else {}
        ideas = [i for i in final.get("ideas") or [] if isinstance(i, dict) and i.get("title")]
        for i in ideas:
            i.setdefault("lane", lane)
        return ideas[: max(n, 1) * 2], run.steps

    def ideas_parallel(self, profile: RepoProfile, n: int, avoid: list[str], round_: int = 1, focus: str = "",
                       lanes: list[str] | None = None, workers: int = 1) -> tuple[list[dict], list[dict]]:
        """Every lane at once; ideas merged in lane order (so ids stay stable on replay), same titles once."""
        from .parallel import pmap

        lanes = lanes or ["all"]
        each = max(1, -(-n // len(lanes)))  # ceil: every lane brings its share
        results = pmap(lambda lane: self.ideas(profile, each, avoid, round_, focus, lane), lanes, workers)
        ideas, steps, seen = [], [], {_title_key(t) for t in avoid}
        for lane_ideas, lane_steps in results:
            steps += lane_steps
            for idea in lane_ideas:
                key = _title_key(idea["title"])
                if key and key not in seen:
                    seen.add(key)
                    ideas.append(idea)
        return ideas[: max(n, 1) * 2], steps

    def refine(self, profile: RepoProfile, idea: dict, critique: dict) -> tuple[dict | None, list[dict]]:
        run = run_agent(
            self.agent,
            f"You are the research agent for {profile.repo}. A critic checked your idea against the code and "
            "asked for changes. Fix every problem (look the facts up), or drop the idea if it cannot be fixed.",
            json.dumps({"idea": idea, "critic": critique}, ensure_ascii=False),
            self.tools, RESEARCH_TOOLS, '{"idea": ' + IDEA_SHAPE + '} or {"drop": "reason"}', self.max_steps // 2)
        final = run.final if isinstance(run.final, dict) else {}
        return (final["idea"] if isinstance(final.get("idea"), dict) else None), run.steps


_IDENT = re.compile(r"`([A-Za-z_][\w.]*)(?:\(|`)|\b([a-z][a-z0-9]*_[a-z0-9_]+)\b")


def names_in_code(repo: Path, idea: dict, files: list[str], limit: int = 12) -> list[str]:
    """Where the idea's public names are already DEFINED (def, class, a parameter of a signature,
    an attribute set in __init__): evidence the critic must weigh, found by code, not by the model."""
    text = " ".join(str(idea.get(k) or "") for k in ("title", "summary", "behaviour", "where"))
    names = []
    for m in _IDENT.finditer(text):
        n = (m.group(1) or m.group(2) or "").split(".")[-1]
        if len(n) >= 4 and n not in names:
            names.append(n)
    for w in re.findall(r"[a-z]{5,}", str(idea.get("title") or "").lower()):  # plain title words: "callback"
        if w not in names:
            names.append(w)
    hits = []
    for n in names[:15]:
        rx = re.compile(rf"^\s*(def|class)\s+{re.escape(n)}\b|^\s*{re.escape(n)}\s*:\s*[^=\n]*=|"
                        rf"\bself\.{re.escape(n)}\s*=|^\s*{re.escape(n)}\s*=")
        for f in files:
            for k, line in enumerate((repo / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{n}: {f}:{k}: {line.strip()[:120]}")
                    break
            if len(hits) >= limit:
                return hits
    return hits


class ExampleRunner:
    """Runs a critic's example (the usage an idea promises) on the CURRENT code, in a sandbox without
    network. Whether the example works today is then a fact, not the model's reading of the code."""

    PRELUDE = "import os, sys, json, pathlib\n"
    # an example that breaks on these proves nothing about the code under test
    INCONCLUSIVE = ("NameError", "SyntaxError", "IndentationError", "ModuleNotFoundError", "ImportError")

    def __init__(self, factory, spec, env, timeout_s: int = 30, packages: list[str] | None = None) -> None:
        self.factory = factory
        self.spec = spec
        self.env = env
        self.timeout_s = timeout_s
        self._image = None
        self.packages = packages if packages is not None else top_packages(Path(spec.local_path))

    def __call__(self, code: str) -> tuple[bool, str]:
        if self._image is None:
            self._image = self.factory.build_image(self.spec, self.env)
        with self.factory.new_sandbox(self._image, network=False) as sb:
            imports = "".join(f"try:\n    import {p}\nexcept Exception:\n    pass\n" for p in self.packages)
            sb.write_file("/tmp/factory_example.py", self.PRELUDE + imports + code)
            r = sb.exec(["python", "/tmp/factory_example.py"], timeout_s=self.timeout_s, cwd=self.env.workdir)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return (r.exit_code == 0 and not r.timed_out), out[-1500:] or f"exit {r.exit_code}"


def top_packages(repo: Path) -> list[str]:
    """The repo's importable top-level Python packages (src/ layout or flat), for the example prelude."""
    out = []
    for base in (repo / "src", repo):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if (d / "__init__.py").is_file() and d.name.isidentifier() and d.name not in {"tests", "test", "docs"}:
                    out.append(d.name)
    return list(dict.fromkeys(out))[:3]


class IdeaCritic:
    def __init__(self, agent, tools: Toolbox, max_steps: int = 10, runner=None) -> None:
        self.agent = agent
        self.tools = tools
        self.max_steps = max_steps
        self.runner = runner  # ExampleRunner (or any code -> (ran_ok, output)); None: no execution

    def _run_example(self, profile: RepoProfile, idea: dict, final: dict, run: AgentRun) -> dict:
        """Execute the critic's example; when the result contradicts its works_today claim, it decides again."""
        code = _example_code(final)
        if self.runner is None or not code or "works_today" not in final:
            return final
        try:
            ran, output = self.runner(code)
        except (EnvironmentProblem, ModelProviderError):
            raise
        except Exception as exc:  # noqa: BLE001 - no sandbox: judge without the fact
            log.warning("example run failed to start: %s", exc)
            return final
        run.steps.append({"tool": "run_example", "args": {"ran": ran}})
        if not ran and any(e in output for e in ExampleRunner.INCONCLUSIVE):
            final.setdefault("evidence", []).append("example could not run as written (inconclusive): " + output[-160:])
            return final
        if ran == bool(final.get("works_today")):
            final.setdefault("evidence", []).insert(0, f"example {'ran' if ran else 'failed'} in the sandbox: {output[:200]}")
            return final
        return _judge_with_fact(self, profile, idea, final, ran, output, run)

    def judge(self, profile: RepoProfile, idea: dict) -> tuple[dict, list[dict]]:
        # Existing task instructions are visible to the critic before it decides. A semantically
        # equivalent package is already built even when its title and identifiers differ.
        built = self.tools.call("existing_tasks", {})
        run = run_agent(
            self.agent,
            f"You are an independent critic for a coding-task factory on {profile.repo}. Check this idea with "
            "the tools, do not trust it: (1) does the starter ALREADY do this, under any name? search the code, "
            "the docs and the upstream (merged PRs; a newer branch); (2) is it feasible as one focused change "
            "in the files named, consistent with the code you read? (3) can deterministic unit tests check it, "
            "with no network, no timing, no new dependency? (4) is it a feature or behaviour change (not a bug "
            "fix, refactor or performance work)? (5) compare it with the existing task packages supplied below. "
            "If one already asks for the same observable feature or behaviour, REJECT it as already built, even "
            "if the package uses different names. A peer having the feature is expected, never a problem. "
            "Decide (1) concretely: write the user code the idea promises (its main example) and answer whether "
            "it ALREADY WORKS on the current code, counting inherited parameters and methods (an Option gets "
            "everything a Parameter accepts). If it works today, the idea already exists. "
            "Verdict: accept, fix (concrete, fixable problems), or reject (exists already, or cannot work).",
            json.dumps({"idea": idea, "repository": json.loads(_profile_brief(profile)), "existing_task_packages": built,
                        "names_already_defined_in_code": names_in_code(self.tools.repo, idea, self.tools.sources)},
                       ensure_ascii=False),
            self.tools, CRITIC_TOOLS,
            '{"example": "the user code the idea promises", "works_today": false, "why_not_today": "what fails '
            'today (file:line)", "verdict": "accept | fix | reject", "already_exists": false, "evidence": ["file:line '
            'or url you checked"], "problems": ["..."], "fix": "what to change", "edge_cases": ["tests to add"]}',
            self.max_steps)
        final = run.final if isinstance(run.final, dict) else {}
        final = self._run_example(profile, idea, final, run)
        verdict = str(final.get("verdict", "")).lower()
        if verdict not in {"accept", "fix", "reject"}:
            final = {"verdict": "fix", "problems": ["the critic gave no clear verdict"], "fix": ""}
        if final.get("works_today") is True:  # the idea's own example runs on the current code
            final["already_exists"] = True
            final.setdefault("evidence", []).insert(0, f"works today: {str(final.get('example', ''))[:200]}")
        if final.get("already_exists"):
            final["verdict"] = "reject"
        return final, run.steps


def _example_code(final: dict) -> str:
    code = str(final.get("example") or "").strip()
    m = re.search(r"```(?:python)?\s*\n(.*?)```", code, re.S)
    return (m.group(1) if m else code).strip()


def _judge_with_fact(self, profile: RepoProfile, idea: dict, final: dict, ran: bool, output: str, run: AgentRun) -> dict:
    fact = ("Your example RAN on the current code without an error" if ran else "Your example FAILED on the current code")
    again = run_agent(
        self.agent,
        f"You are an independent critic for a coding-task factory on {profile.repo}. You judged an idea; then "
        f"your example was executed in a sandbox: {fact}. Output:\n{output[:1200]}\n"
        "Decide again with this fact. If the example ran, the idea exists already unless the behaviour it "
        "promises differs from what ran (say exactly how, with file:line). If it failed, say whether it failed "
        "because the feature is missing (then the idea is new) or because the example was wrong.",
        json.dumps({"idea": idea, "your_first_verdict": final}, ensure_ascii=False),
        self.tools, ["repo_read", "repo_search"],
        '{"example": "...", "works_today": true, "verdict": "accept | fix | reject", "already_exists": false, '
        '"evidence": ["..."], "problems": ["..."], "fix": "...", "edge_cases": ["..."]}', 4)
    run.steps += again.steps
    second = again.final if isinstance(again.final, dict) else final
    second.setdefault("evidence", []).insert(0, f"example {'ran' if ran else 'failed'} in the sandbox: {output[:200]}")
    return second


SPEC_SHAPE = ('{"title": "...", "kind": "feature | behavior_change", "summary": "...", "behaviour_spec": "Markdown: '
              'public names, parameters and defaults, return values, exact error types and messages, edge cases", '
              '"expected_files": ["existing source files"], "new_names": ["public names it adds"], "test_plan": '
              '["one behaviour or edge case per item"], "difficulty_est": "easy | medium | hard", "category": "..."}')


class SpecWriter:
    def __init__(self, agent, tools: Toolbox, max_steps: int = 8) -> None:
        self.agent = agent
        self.tools = tools
        self.max_steps = max_steps

    def write(self, profile: RepoProfile, idea: dict, critique: dict) -> tuple[dict, list[dict]]:
        run = run_agent(
            self.agent,
            f"You write the task proposal for an accepted idea on {profile.repo}. Read the code it touches and "
            "specify the behaviour exactly as this project would: real module and function names, parameters "
            "and defaults, error types and message texts, edge cases. Only name files that exist. The spec must "
            "not contradict itself or the code.",
            json.dumps({"idea": idea, "critic_notes": critique}, ensure_ascii=False),
            self.tools, SPEC_TOOLS, SPEC_SHAPE, self.max_steps)
        return (run.final if isinstance(run.final, dict) else {}), run.steps
