"""P2 Scout: find the closest peer repos by comparing what the repos DO (v2.2 section 4).

recall (20-30 candidates)  ->  cheap cut (~8)  ->  deep profile  ->  compare capability maps  ->  top K

- Recall: the scout sub-agent turns the starter profile's capability descriptions into GitHub search
  queries and names well-known alternatives (standing in for "alternatives to X" pages and awesome
  lists); GitHub topic search adds projects in the same ecosystem. Only capability descriptions
  leave the machine, never source code.
- Cheap cut: a permissive licence (a gate, not a score), tests in the tree, recent activity, not
  archived or a fork; same language ranks first.
- Deep profile: the same Profiler runs on a shallow clone of each shortlisted peer.
- Score: capability overlap, design fit, maturity, ecosystem fit, with the reasons shown. The gap
  list (peer capabilities the starter repo lacks) is computed from the two structured maps.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import PERMISSIVE_LICENCES as PERMISSIVE
from ..errors import ModelProviderError, PackageError
from .github import GitHub, parse_repo
from .parallel import pmap
from .profiler import Profiler, shallow_clone
from .schema import Capability, PeerRepo, PeerSimilarity, RepoProfile
from .text import jaccard, match_score, words

log = logging.getLogger("factory.reference.scout")

GITHUB_LANG = {"python": ["Python"], "node": ["TypeScript", "JavaScript"], "cpp": ["C++"], "c": ["C"],
               "go": ["Go"], "rust": ["Rust"], "java": ["Java", "Kotlin"]}
TEST_DIR = re.compile(r"(^|/)(tests?|__tests__|spec|specs|testing)(/|$)|(^|/)test_[^/]+$|_test\.|\.(test|spec)\.")


@dataclass
class ScoutConfig:
    k: int = 3
    recall: int = 25
    shortlist: int = 8
    licences: list[str] = field(default_factory=lambda: list(PERMISSIVE))
    max_age_days: int = 730
    min_stars: int = 50
    classify: int = 40  # candidates the scout classifies (alternative / dependent ...) in one call
    min_relevance: float = 0.3  # cheap relevance (named alternative, description) needed for a deep profile
    match_threshold: float = 0.42  # a starter capability counts as matched at this score
    gap_threshold: float = 0.3  # a peer capability below this best match is a gap
    manual: list[str] = field(default_factory=list)  # owner/name peers that are always included
    # Maturity and ecosystem fit are near 1 for every popular same-language repo, so they barely tell
    # peers apart: capability overlap carries the score (seen live: a TUI framework with overlap 0
    # scored close to a real alternative).
    weights: dict = field(default_factory=lambda: {"capability_overlap": 0.6, "design_fit": 0.2,
                                                   "maturity": 0.1, "ecosystem_fit": 0.1})
    min_overlap: float = 0.05  # a peer sharing almost no capability with the starter is not a peer


def _norm_dep(name: str) -> str:
    return re.sub(r"[-_.]+", "-", (name or "").lower().split("/")[-1]).strip("-")


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def _peer_from_meta(meta: dict) -> PeerRepo:
    lic = (meta.get("license") or {}).get("spdx_id") if isinstance(meta.get("license"), dict) else None
    return PeerRepo(full_name=meta["full_name"], url=meta.get("html_url") or f"https://github.com/{meta['full_name']}",
                    licence=lic, language=meta.get("language"), last_commit=meta.get("pushed_at"),
                    stars=int(meta.get("stargazers_count") or 0), archived=bool(meta.get("archived")),
                    fork=bool(meta.get("fork")), default_branch=meta.get("default_branch"),
                    description=" ".join([meta.get("description") or "", *(meta.get("topics") or [])])[:400])


# --------------------------------------------------------------------------- comparison


def compare(starter: RepoProfile, peer: RepoProfile, cfg: ScoutConfig) -> tuple[float, list[Capability], list[str]]:
    """(capability overlap, gap list, reasons) from the two capability maps."""
    a = starter.capability_map
    b = peer.capability_map
    if not a or not b:
        return 0.0, list(b), ["one of the capability maps is empty"]
    matched_pairs = []
    for ca in a:
        best = max(b, key=lambda cb: match_score(f"{ca.name} {ca.behaviour}", f"{cb.name} {cb.behaviour}"))
        s = match_score(f"{ca.name} {ca.behaviour}", f"{best.name} {best.behaviour}")
        if s >= cfg.match_threshold:
            matched_pairs.append((ca.name, best.name, s))
    overlap = 2 * len(matched_pairs) / (len(a) + len(b))
    gap = []
    for cb in b:
        best = max(match_score(f"{cb.name} {cb.behaviour}", f"{ca.name} {ca.behaviour}") for ca in a)
        if best < cfg.gap_threshold:
            gap.append(cb)
    reasons = [f"{len(matched_pairs)} of the starter's {len(a)} capabilities have a counterpart"]
    reasons += [f"shared: {x} ~ {y}" for x, y, _ in sorted(matched_pairs, key=lambda t: -t[2])[:4]]
    reasons.append(f"{len(gap)} peer capabilities the starter lacks (gap list)")
    return overlap, gap, reasons


def maturity(peer: PeerRepo, tree: list[str]) -> float:
    stars = min(math.log10(peer.stars + 1) / 4.5, 1.0)
    age = _age_days(peer.last_commit)
    active = 1.0 if age is not None and age < 180 else 0.7 if age is not None and age < 365 else 0.4
    ci = 1.0 if any(p.startswith(".github/workflows/") for p in tree) else 0.0
    return round(0.4 * stars + 0.3 * active + 0.2 * float(peer.has_tests) + 0.1 * ci, 3)


def ecosystem_fit(starter: RepoProfile, peer_lang: str | None, peer: RepoProfile | None) -> float:
    same = peer_lang in GITHUB_LANG.get(starter.language or "", [])
    shared = 0.0
    if peer is not None:
        shared = jaccard(set(starter.ecosystem.deps) | set(starter.ecosystem.topics),
                         set(peer.ecosystem.deps) | set(peer.ecosystem.topics))
    return round((0.75 if same else 0.3) + 0.25 * min(shared * 3, 1.0), 3)


# --------------------------------------------------------------------------- scout


ROLE_WEIGHT = {"alternative": 1.0, "relative": 0.6, "dependent": 0.05, "unrelated": 0.0}

CLASSIFY_PROMPT = (
    "You classify candidate peer repositories for {name} ({purpose}). For each repository below say "
    "what it is relative to {name}: \"alternative\" (does the same job; a user could use it instead), "
    "\"relative\" (solves a closely related problem in the same domain, e.g. an extension that adds "
    "features of the same kind), \"dependent\" (an application, plugin or tool that merely uses {name} "
    "or has a command line), or \"unrelated\". Also rate its context from 0 to 1: how close its purpose, "
    "users and domain are to {name}'s (1 = the same kind of project), judged from its description and README.\n"
    "Return only JSON shaped like {{\"repos\": {{\"owner/name\": {{\"role\": \"alternative\", \"context\": 0.9, "
    "\"why\": \"one short reason\"}}}}}} with every repository listed.\n"
    "Repositories: {listing}"
)

RECALL_PROMPT = (
    "You are the Scout of a coding-task factory. From the capability description below (no source "
    "code), find open-source projects that do the same job: direct alternatives first (a library or tool "
    "a user could pick INSTEAD of {name}), then close relatives that solve the same problem. Never list "
    "projects that merely USE or are built on {name} (applications, plugins, tools with a command line).\n"
    "Return only JSON shaped like {{\"queries\": [\"GitHub repository search text describing what the "
    "project does\", ...], \"alternatives\": [\"owner/name\", ...]}} with 4 to 6 queries (plain words, "
    "no qualifiers) and up to 15 alternatives you know exist on GitHub. Exclude {name} itself.\n"
    "Project: {desc}"
)


class Scout:
    def __init__(self, agent, github: GitHub, profiler: Profiler, clones: Path, cfg: ScoutConfig | None = None,
                 offline: bool = False, cloner=shallow_clone, workers: int = 1) -> None:
        self.agent = agent
        self.workers = workers
        self.cloner = cloner
        self.gh = github
        self.profiler = profiler
        self.clones = Path(clones)
        self.cfg = cfg or ScoutConfig()
        self.offline = offline

    def describe(self, starter: RepoProfile) -> dict:
        """What may leave the machine: purpose, capability descriptions, language, topics."""
        return {"name": starter.repo, "purpose": starter.purpose, "language": starter.language,
                "capabilities": [f"{c.name}: {c.behaviour}" for c in starter.capability_map][:40],
                "topics": starter.ecosystem.topics[:10]}

    # ------------------------------------------------------------------ recall

    def recall(self, starter: RepoProfile) -> list[PeerRepo]:
        desc = self.describe(starter)
        own = (parse_repo(starter.url or "") or starter.repo).lower()
        data = self.agent.ask_json(RECALL_PROMPT.format(name=starter.repo, desc=desc))
        langs = GITHUB_LANG.get(starter.language or "", [])
        found: dict[str, PeerRepo] = {}

        def add(meta: dict, route: str) -> None:
            name = (meta.get("full_name") or "").strip()
            if not name or name.lower() == own:
                return
            if name not in found:
                found[name] = _peer_from_meta(meta)
            if route not in found[name].found_by:
                found[name].found_by.append(route)

        searches = []  # GitHub repository search only (user choice): the model writes the queries  # (GitHub query, route label); run in parallel, added in order
        for q in (data.get("queries") or [])[:6]:
            q = re.sub(r"[^\w\s.+#-]", " ", str(q)).strip()
            if q:
                searches.append((f"{q} language:{langs[0]}" if langs else q, f"search: {q}"))
        for topic in starter.ecosystem.topics[:3]:
            searches.append((f"topic:{topic}" + (f" language:{langs[0]}" if langs else ""), f"topic: {topic}"))
        results = pmap(lambda qr: self.gh.search_repos(qr[0], per_page=15), searches, self.workers)
        for (_, label), metas in zip(searches, results):
            for meta in metas:
                add(meta, label)
        for name in self.cfg.manual:
            meta = self.gh.repo(name)
            if meta:
                add(meta, "manual override")
                found[meta["full_name"]].manual = True
        prelim = sorted(found.values(), key=lambda p: (-p.manual, -p.stars))  # GitHub's top repos first
        self.classify(starter, prelim[: self.cfg.classify])
        ranked = sorted(found.values(), key=lambda p: (-p.manual, -self.relevance(starter, p), -p.stars))
        return ranked[: max(self.cfg.recall, len(self.cfg.manual))]

    # ------------------------------------------------------------------ cheap cut

    def cheap_cut(self, starter: RepoProfile, peers: list[PeerRepo]) -> list[PeerRepo]:
        langs = GITHUB_LANG.get(starter.language or "", [])
        own_owner = ((parse_repo(starter.url or "") or "").split("/")[0] or "").lower()
        out = []
        need_tree = [p for p in peers if p.licence in self.cfg.licences and not (p.archived or p.fork)]
        trees = dict(zip((p.full_name for p in need_tree),
                         pmap(lambda p: self.gh.tree(p.full_name, p.default_branch or "HEAD"), need_tree, self.workers)))
        for p in peers:
            why = None
            if p.licence not in self.cfg.licences:
                why = f"licence {p.licence or 'unknown'} is not on the permissive allow-list"
            elif own_owner and p.full_name.split("/")[0].lower() == own_owner and not p.manual:
                why = "same organisation as the starter repo (a sibling, not a similar project)"
            elif p.archived or p.fork:
                why = "archived" if p.archived else "a fork"
            elif (age := _age_days(p.last_commit)) is not None and age > self.cfg.max_age_days:
                why = f"no push for {int(age)} days"
            elif p.stars < self.cfg.min_stars and not p.manual:
                why = f"{p.stars} stars (minimum {self.cfg.min_stars})"
            if why is None:
                tree = trees.get(p.full_name) or self.gh.tree(p.full_name, p.default_branch or "HEAD")
                p.has_tests = any(TEST_DIR.search(t) for t in tree)
                if not p.has_tests:
                    why = "no tests in the tree"
                else:
                    p.similarity.maturity = maturity(p, tree)
            if why:
                p.status = "cut"
                p.reasons.append(f"cut: {why}")
                out.append(p)
                continue
            p.status = "shortlisted"
            out.append(p)
        kept = [p for p in out if p.status == "shortlisted"]
        kept.sort(key=lambda p: (-p.manual, p.language not in langs, -self.relevance(starter, p), -p.similarity.maturity))
        for i, p in enumerate(kept):
            if p.manual:
                continue
            rel = self.relevance(starter, p)
            if rel < self.cfg.min_relevance:
                p.status = "cut"
                why = (f"a {p.role} of the starter ({p.role_reason})" if p.role in {"dependent", "unrelated"}
                       else "not described like the starter repo")
                p.reasons.append(f"cut: {why}, relevance {rel}")
            elif i >= self.cfg.shortlist:
                p.status = "cut"
                p.reasons.append("cut: outside the shortlist")
        return out

    @staticmethod
    def relevance(starter: RepoProfile, p: PeerRepo) -> float:
        """Cheap ranking before deep profiles. With the scout's classification: alternatives first, then
        relatives; projects that only USE the starter (dependents) or do something else score near 0.
        Without one (an old snapshot, a failed call): named by the scout, described like the starter."""
        mine = words(" ".join([starter.purpose, *(c.name for c in starter.capability_map), *starter.ecosystem.topics]))
        theirs = words(p.description)
        described = len(theirs & mine) / len(theirs) if theirs else 0.0
        twice = float(len(p.found_by) > 1)
        if p.manual:
            return 1.0
        if p.role in ROLE_WEIGHT and p.context is not None:
            return round(p.context * (0.2 if p.role in {"dependent", "unrelated"} else 1.0), 3)
        if p.role in ROLE_WEIGHT:
            return round(0.7 * ROLE_WEIGHT[p.role] + 0.2 * described + 0.1 * twice, 3)
        named = any(r == "named alternative" for r in p.found_by)
        return round(0.5 * named + 0.35 * described + 0.15 * twice, 3)

    def classify(self, starter: RepoProfile, peers: list[PeerRepo]) -> None:
        """One call for all candidates: is each an alternative to the starter, or only built with it?"""
        if not peers:
            return
        def readme(p: PeerRepo) -> str:
            for name in ("README.md", "README.rst", "README"):
                text = self.gh.file(p.full_name, name, p.default_branch)
                if text:
                    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|!\[[^]]*]\([^)]*\)", " ", text))[:700]
            return ""

        heads = pmap(readme, peers, self.workers)
        listing = [{"repo": p.full_name, "description": p.description[:200], "readme_start": h, "language": p.language,
                    "stars": p.stars} for p, h in zip(peers, heads)]
        try:
            data = self.agent.ask_json(CLASSIFY_PROMPT.format(name=starter.repo, purpose=starter.purpose,
                                                              listing=listing))
        except ModelProviderError:
            raise
        except PackageError as exc:  # an unusable reply: rank without the classification
            log.warning("scout classification failed: %s", exc)
            return
        raw = data.get("repos") or {}
        if isinstance(raw, list):  # [{"repo": ..., "role": ...}]
            raw = {str(x.get("repo")): x for x in raw if isinstance(x, dict)}
        verdicts = {}
        for k, v in raw.items() if isinstance(raw, dict) else []:
            v = {"role": v} if isinstance(v, str) else v  # {"owner/name": "alternative"} is common
            if isinstance(v, dict) and str(v.get("role", "")).lower() in ROLE_WEIGHT:
                try:
                    ctx = min(max(float(v.get("context")), 0.0), 1.0) if v.get("context") is not None else None
                except (TypeError, ValueError):
                    ctx = None
                verdicts[str(k).lower()] = {"role": str(v["role"]).lower(), "why": str(v.get("why") or "")[:200],
                                            "context": ctx}
        for p in peers:
            v = verdicts.get(p.full_name.lower())
            if v:
                p.role, p.role_reason, p.context = v["role"], v["why"], v["context"]

    # ------------------------------------------------------------------ deep profile + score

    def deep(self, starter: RepoProfile, peer: PeerRepo) -> RepoProfile | None:
        clone = None
        commit = None
        if not self.offline:
            try:
                clone, commit = self.cloner(f"https://github.com/{peer.full_name}.git",
                                              self.clones / peer.full_name.replace("/", "__"))
            except (RuntimeError, OSError) as exc:
                peer.reasons.append(f"cut: clone failed ({exc})")
                return None
        meta = self.gh.repo(peer.full_name) or {}
        try:
            return self.profiler.profile(clone, peer.full_name, url=peer.url, commit=commit,
                                         topics=meta.get("topics") or [], version=peer.last_commit or peer.default_branch)
        except FileNotFoundError:
            return None
        except ModelProviderError:  # quota: stop the run, never cut a peer for it
            raise
        except PackageError as exc:  # the profiler's reply was unusable twice: cut this peer, keep the run
            peer.reasons.append(f"cut: profile failed ({str(exc)[:120]})")
            return None

    def score(self, starter: RepoProfile, peer: PeerRepo, prof: RepoProfile) -> PeerRepo:
        overlap, gap, reasons = compare(starter, prof, self.cfg)
        design = round(min(jaccard(words(" ".join(starter.design_traits)), words(" ".join(prof.design_traits))) * 2.5, 1.0), 3)
        eco = ecosystem_fit(starter, peer.language, prof)
        sim = PeerSimilarity(capability_overlap=round(overlap, 3), design_fit=design,
                             maturity=peer.similarity.maturity, ecosystem_fit=eco)
        w = self.cfg.weights
        sim.total = round(sum(getattr(sim, k) * v for k, v in w.items()), 3)
        if peer.language not in GITHUB_LANG.get(starter.language or "", []):
            reasons.append(f"cross-language peer ({peer.language}): its code is a design reference only")
        return peer.model_copy(update={"similarity": sim, "gap": gap, "reasons": peer.reasons + reasons})

    def run(self, starter: RepoProfile, on_step=None) -> tuple[list[PeerRepo], dict[str, RepoProfile]]:
        step = on_step or (lambda m: None)
        step("scout: recall by capability")
        cands = self.recall(starter)
        step(f"scout: {len(cands)} candidates; cheap cut (licence, tests, activity)")
        cands = self.cheap_cut(starter, cands)
        shortlist = [p for p in cands if p.status == "shortlisted"]
        # most similar context first, then GitHub's ranking (stars); profile them until K peers are chosen
        shortlist.sort(key=lambda p: (-p.manual, -self.relevance(starter, p), -p.stars))
        profiles: dict[str, RepoProfile] = {}
        scored: list[PeerRepo] = []
        chosen: set[str] = set()
        own = _norm_dep(starter.repo)
        for batch_start in range(0, len(shortlist), max(1, self.workers)):
            if len(chosen) >= self.cfg.k and not any(p.manual for p in shortlist[batch_start:]):
                break
            batch = shortlist[batch_start:batch_start + max(1, self.workers)]
            for p in batch:
                step(f"scout: deep profile {p.full_name} (context {p.context}, {p.stars} stars)")
            for p, prof in zip(batch, pmap(lambda p: self.deep(starter, p), batch, self.workers)):
                if prof is not None and not p.manual and own in {_norm_dep(d) for d in prof.ecosystem.deps}:
                    p.reasons.append(f"cut: built on {starter.repo} (it depends on it): a derivative, not a similar project")
                    prof = None
                if prof is None:
                    p.status = "cut"
                    scored.append(p)
                    continue
                profiles[p.full_name] = prof
                p = self.score(starter, p, prof)
                scored.append(p)
                if p.gap and (p.manual or len(chosen) < self.cfg.k):
                    chosen.add(p.full_name)
        out = []
        for p in cands:
            p = next((s for s in scored if s.full_name == p.full_name), p)
            if p.status == "shortlisted":
                p = p.model_copy(update={"status": "selected" if p.full_name in chosen else "not_selected"})
            out.append(p)
        out.sort(key=lambda p: (p.status != "selected", -(p.context or 0), -p.stars))
        return out, profiles
