"""The controller: drives P1-P10 for one starter repo and commit (``factory run``).

Automatic by default (the auto-approve policy is saved with the run); ``--review`` turns on P7.
Everything is resumable: research results come from the evidence snapshot, proposals continue from
their state in MongoDB, packages already in the task store are not rebuilt. The run id depends only
on the repo, the commit and the snapshot, so the same three give the same proposal and task ids.
"""
from __future__ import annotations

import logging
import math
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..config import FactoryConfig
from ..errors import EnvironmentProblem
from ..models import EnvConfig, RepoSpec
from ..repo.loader import export_clean
from . import drafting
from .agents import AgentPool
from .building import Builder, task_id_for, write_side_files
from .github import GitHub, parse_repo
from .harbor import HarborValidator
from .miner import Miner, MinerConfig
from .parallel import pmap
from .profiler import Profiler
from .research import IdeaCritic, Researcher, SpecWriter, Toolbox, research_lanes
from .schema import (Novelty, PeerFeature, PeerRepo, ProposalState as S, Provenance, RepoProfile, Review, TaskProposal,
                     stable_id)
from .scout import Scout, ScoutConfig
from .snapshot import Snapshot
from .store import ReferenceStore

log = logging.getLogger("factory.reference.run")

BUILDABLE = [S.CHECKED, S.APPROVED, S.BUILDING]
DONE_OK = [S.VALIDATED, S.QUEUED, S.RUNNING, S.BENCHMARKED, S.RUN_FAILED]


def run_id_for(spec: RepoSpec, snapshot_id: str) -> str:
    return stable_id(spec.name, spec.commit_sha, snapshot_id)


@dataclass
class RunOptions:
    target: int = 5
    review: bool = False
    harbor: str = "required"  # required | if_available | off
    out: Path | None = None
    benchmark_agents: list[str] = field(default_factory=list)
    stop_after: str | None = None  # "research" | "proposals": stop early (dry inspection)
    continuous: bool = False  # CLI/config enables this; false keeps direct controller use finite
    research_pause_s: float = 30.0


class Controller:
    def __init__(self, cfg: FactoryConfig, spec: RepoSpec, env: EnvConfig, store: ReferenceStore, pool: AgentPool,
                 snapshot: Snapshot, github: GitHub, validate, opts: RunOptions, workdir: Path,
                 on_step=None, harbor_validator: HarborValidator | None = None, task_store=None, cloner=None,
                 example_runner=None) -> None:
        self.cfg = cfg
        self.rc = cfg.reference
        self.spec = spec
        self.env = env
        self.store = store
        self.pool = pool
        self.snapshot = snapshot
        self.gh = github
        self.validate = validate
        self.opts = opts
        self.workdir = Path(workdir)
        self.out = Path(opts.out or self.workdir / "reference" / "harbor")
        self.step = on_step or (lambda m: log.info("%s", m))
        self.run_id = run_id_for(spec, snapshot.id)
        self.harbor = harbor_validator
        self.task_store = task_store
        self.features: dict[str, PeerFeature] = {}
        self.cloner = cloner
        self.example_runner = example_runner  # runs the critic's example on the current code (sandbox)
        self.timings: dict[str, float] = {}  # stage -> seconds (summed over rounds), for the latency report

    @contextmanager
    def _stage(self, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.timings[name] = round(self.timings.get(name, 0.0) + time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ research (P1-P4)

    def starter_profile(self, repo: Path) -> RepoProfile:
        cached = self.store.profile(self.run_id, self.spec.name)
        if cached:
            return cached
        with self._stage("P1 profile"):
            return self._starter_profile(repo)

    def _starter_profile(self, repo: Path) -> RepoProfile:
        self.step("P1 profiler: understanding the starter repo")
        url = None
        full = parse_repo(self.spec.source) if self.spec.source.startswith(("http", "git@")) else None
        topics: list[str] = []
        if full:
            url = f"https://github.com/{full}"
            meta = self.gh.repo(full) or {}
            topics = meta.get("topics") or []
        prof = Profiler(self.pool.one("profiler"), self.snapshot).profile(repo, self.spec.name, url,
                                                                         self.spec.commit_sha, topics)
        self.store.save_profile(self.run_id, "starter", prof)
        self.snapshot.save_record("starter-profile", prof.model_dump(mode="json"))
        return prof

    def research(self, repo: Path, profile: RepoProfile) -> list[PeerRepo]:
        """P2: the peers (by capability). Ideas come from the research agent, which uses them as a tool."""
        peers = self.store.list_peers(self.run_id)
        if not peers:
            scfg = ScoutConfig(k=self.rc.peers_k, recall=self.rc.recall, shortlist=self.rc.shortlist,
                               licences=self.rc.licences, max_age_days=self.rc.max_age_days,
                               min_stars=self.rc.min_stars, manual=list(self.rc.peers))
            scout = Scout(self.pool.one("scout"), self.gh, Profiler(self.pool.one("profiler"), self.snapshot),
                          self.workdir / "reference" / "peers", scfg, offline=self.snapshot.mode == "replay",
                          workers=self.rc.workers, **({"cloner": self.cloner} if self.cloner else {}))
            self.step("P2 scout: matching peer repos by capability")
            with self._stage("P2 scout"):
                peers, _ = scout.run(profile, on_step=self.step)
            self.store.save_peers(self.run_id, peers)
            self.snapshot.save_record("peers", [p.model_dump(mode="json") for p in peers])
        self.features = {f.id: f for f in self.store.list_features(self.run_id)}
        # Start research with the top-K selected peers, then let it broaden to the remaining
        # vetted shortlist rather than treating the ranking cut-off as an idea-source cut-off.
        usable = [p for p in peers if p.status in {"selected", "not_selected"}]
        return sorted(usable, key=lambda p: (p.status != "selected", -p.similarity.total,
                                              -(p.context or 0), -p.stars))

    def toolbox(self, repo: Path, profile: RepoProfile, peers: list[PeerRepo]) -> Toolbox:
        miner = Miner(self.pool.one("miner"), self.gh, self.snapshot.id, MinerConfig(), workers=1,
                      starter_purpose=profile.purpose)
        from .websearch import WebSearch, feature_queries as web_feature_queries

        # replay offers exactly the tools the recorded run had (the prompts, and so the cached replies, depend on it)
        use_web = bool(self.snapshot.manifest().get("web")) if self.snapshot.mode == "replay" else WebSearch.available()
        if self.snapshot.mode != "replay":
            self.snapshot.write_manifest(web=use_web)
        web = WebSearch(self.snapshot) if use_web else None
        tools = Toolbox(repo, profile, self.gh, peers, miner, upstream=Path(self.spec.local_path),
                        base_commit=self.spec.commit_sha, web=web, existing_tasks=self._existing_tasks())
        if web is not None:
            tools.seed_web_context(web_feature_queries(profile))
        return tools

    def _existing_tasks(self) -> list[dict]:
        """Visible summaries of packages already built for the same starter repository.

        The critic sees instructions only: never a reference patch, hidden tests or package credentials.
        """
        if self.task_store is None:
            return []
        summaries = []
        for entry in self.task_store.entries():
            if entry.status == "deleted" or entry.meta.commit_sha != self.spec.commit_sha:
                continue
            instruction = entry.path / "instruction.md"
            if not instruction.is_file():
                continue
            text = instruction.read_text(encoding="utf-8", errors="replace")
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), entry.task_id)
            summaries.append({"task_id": entry.task_id, "title": title, "instruction": text[:6000]})
        return summaries[:100]

    def _references(self, tools: Toolbox, idea: dict) -> tuple[list[Provenance], PeerFeature | None]:
        """Provenance from the idea's references; the first peer reference also gives the role bundles."""
        prov: list[Provenance] = []
        first: PeerFeature | None = None
        for ref in idea.get("references") or []:
            if not isinstance(ref, dict):
                continue
            f = None
            if ref.get("peer") and ref.get("pr"):
                key = f"{ref['peer']}#{ref['pr']}"
                if key not in tools.features:
                    tools.call("peer_pr", {"peer": ref["peer"], "number": ref["pr"]})
                f = tools.features.get(key)
            elif ref.get("peer") and ref.get("doc"):
                key = f"{ref['peer']}:{ref['doc']}"
                if key not in tools.features:
                    tools.call("peer_read", {"peer": ref["peer"], "path": ref["doc"], "start": 1, "end": 120})
                f = tools.features.get(key)
            elif ref.get("peer") and ref.get("capability"):
                key = f"{ref['peer']}:{ref['capability']}"
                if key not in tools.features:
                    tools.call("peer_capability", {"peer": ref["peer"], "name": ref["capability"]})
                f = tools.features.get(key)
            if f is not None:
                prov.append(Provenance(feature_id=f.id, peer_repo=f.peer_repo, pr_url=f.pr_url, issue_url=f.issue_url,
                                       licence=f.licence, attribution=f"Idea informed by {f.peer_repo} ({f.pr_url}), "
                                                                      f"licence {f.licence or 'unknown'}"))
                if f.id not in self.features:
                    self.features[f.id] = f
                    self.store.save_features(self.run_id, [f])
                first = first or f
            elif ref.get("url"):
                prov.append(Provenance(feature_id="url", peer_repo="", pr_url=str(ref["url"])[:300],
                                       attribution="reference link given by the research agent"))
        return prov, first

    def _idea_proposal(self, pid: str, idea: dict, prov: list[Provenance], researcher: str) -> TaskProposal:
        kind = str(idea.get("kind") or "feature").replace("behaviour", "behavior")
        return TaskProposal(
            proposal_id=pid, run_id=self.run_id, title=str(idea.get("title"))[:120].strip(),
            kind=kind if kind in {"feature", "behavior_change"} else "feature",
            summary=str(idea.get("summary") or "").strip(), behaviour_spec=str(idea.get("behaviour") or "").strip(),
            provenance=prov, expected_files=[str(f) for f in idea.get("files") or []][:5], idea=idea,
            test_plan=[str(t) for t in idea.get("how_to_test") or []][:12], drafter=researcher,
            tags=["research idea" if not prov else "referenced idea"])

    def judge_idea(self, repo: Path, profile: RepoProfile, tools: Toolbox, p: TaskProposal, idea: dict) -> None:
        """Critic (tools) -> optional refinement -> critic -> spec writer -> deterministic checks."""
        critic = IdeaCritic(self.pool.one("critic"), tools, runner=self.example_runner)
        verdict, steps = critic.judge(profile, idea)
        self._critic_review(p, verdict, steps)
        if verdict["verdict"] == "fix":
            self.step(f"P6 refine: {p.title[:60]}")
            better, _ = Researcher(self.pool.one("researcher"), tools).refine(profile, idea, verdict)
            if better:
                prov, _ = self._references(tools, better)
                fresh = self._idea_proposal(p.proposal_id, better, prov or p.provenance, p.drafter or "")
                p = self.store.edit_proposal(p.proposal_id, {
                    k: getattr(fresh, k) for k in ("title", "kind", "summary", "behaviour_spec", "expected_files",
                                                   "test_plan")}, by=f"drafter:{self.pool.one('researcher').model}",
                    reason="refined for the critic: " + "; ".join(map(str, verdict.get("problems") or []))[:400])
                if prov:
                    self.store.update_proposal(p.proposal_id, provenance=prov)
                idea = better
                self.store.update_proposal(p.proposal_id, idea=idea)
                verdict, steps = critic.judge(profile, idea)
                self._critic_review(p, verdict, steps)
        evidence = [str(e) for e in verdict.get("evidence") or []][:8]
        self.store.update_proposal(p.proposal_id, novelty=Novelty(
            in_repo=bool(verdict.get("already_exists")), evidence=evidence, checked_by=["critic with tools"]))
        if verdict["verdict"] != "accept":
            why = evidence if verdict.get("already_exists") else verdict.get("problems")
            self.store.transition(p.proposal_id, "critic_objection", "; ".join(map(str, why or ["rejected"]))[:300])
            return
        self.step(f"P6 spec: {p.title[:60]}")
        spec, _ = SpecWriter(self.pool.one("drafter"), tools).write(profile, idea, verdict)
        if spec.get("behaviour_spec"):
            names = [f"new_name:{n}" for n in spec.get("new_names") or [] if isinstance(n, str)][:8]
            category = str(spec.get("category") or "").strip()
            kind = str(spec.get("kind") or p.kind).replace("behaviour", "behavior")
            self.store.update_proposal(
                p.proposal_id, title=str(spec.get("title") or p.title)[:120], summary=str(spec.get("summary") or p.summary),
                behaviour_spec=str(spec["behaviour_spec"]),
                kind=kind if kind in {"feature", "behavior_change"} else p.kind,
                expected_files=[str(f) for f in spec.get("expected_files") or p.expected_files][:5],
                test_plan=[str(t) for t in spec.get("test_plan") or p.test_plan][:15]
                + [str(e) for e in verdict.get("edge_cases") or []][:5],
                difficulty_est=spec.get("difficulty_est") if spec.get("difficulty_est") in {"easy", "medium", "hard"} else "medium",
                tags=([category] if category else []) + p.tags + names)
        p = self.store.get_proposal(p.proposal_id)
        objections, files = drafting.deterministic_checks(p, repo, profile.language)
        thin = [o for o in objections if o.startswith(("behaviour spec too short", "test plan has fewer"))]
        if objections and len(thin) == len(objections):  # the idea passed; only the write-up is too thin: once more
            self.step(f"P6 spec (more detail): {p.title[:60]}")
            spec, _ = SpecWriter(self.pool.one("drafter"), tools).write(
                profile, dict(idea, previous_spec=p.behaviour_spec, problems=thin), verdict)
            if spec.get("behaviour_spec"):
                self.store.update_proposal(p.proposal_id, behaviour_spec=str(spec["behaviour_spec"]),
                                           test_plan=[str(t) for t in spec.get("test_plan") or p.test_plan][:15])
                p = self.store.get_proposal(p.proposal_id)
                objections, files = drafting.deterministic_checks(p, repo, profile.language)
        if objections:
            self.store.add_review(Review(proposal_id=p.proposal_id, reviewer="check:deterministic",
                                         verdict="objection", objections=objections, hard=True))
            self.store.transition(p.proposal_id, "check_failed", "; ".join(objections)[:300])
            return
        self.store.transition(p.proposal_id, "critics_pass", "the critic accepted it after checking the code",
                              expected_files=files)

    def _critic_review(self, p: TaskProposal, verdict: dict, steps: list[dict]) -> None:
        self.store.add_review(Review(
            proposal_id=p.proposal_id, reviewer=f"critic:{self.pool.one('critic').model}",
            verdict="pass" if verdict["verdict"] == "accept" else "objection",
            objections=[str(x) for x in verdict.get("problems") or []][:8], hard=verdict["verdict"] == "reject",
            reason=f"checked with {len(steps)} tool call(s): " + "; ".join(map(str, verdict.get("evidence") or []))[:600],
            edge_cases=[str(e) for e in verdict.get("edge_cases") or []][:6]))

    def propose_round(self, repo: Path, profile: RepoProfile, peers: list[PeerRepo], round_: int,
                      count: int) -> None:
        """P5 + P6 for one durable batch of ideas.

        Every proposal is written before its critic runs, so an interrupted continuous session resumes
        from the same evidence and pending proposals on its next round.
        """
        tools = self.toolbox(repo, profile, peers)
        researcher = Researcher(self.pool.one("researcher"), tools)
        pending = [p for p in self.store.list_proposals(self.run_id, [S.DRAFTED]) if p.idea]
        if pending:  # a run stopped before the critic answered: judge those first
            pmap(lambda p: self.judge_idea(repo, profile, tools, p, p.idea), pending, self.rc.workers)
        props = self.store.list_proposals(self.run_id)
        done = {p.proposal_id for p in props}
        self.step(f"P5 research round {round_}: looking for {count} idea(s)")
        used = sorted({r.peer_repo for p in props for r in p.provenance if r.peer_repo})
        focus = ("" if round_ == 1 else
                 f"round {round_}: explore other parts of the code and peers beyond {', '.join(used) or 'before'}, "
                 "including the additional vetted peers and web evidence")
        lanes = research_lanes(tools, min(self.rc.research_lanes, max(1, self.rc.workers)))
        if lanes != ["all"]:
            self.step(f"P5: {len(lanes)} research agents in parallel ({', '.join(lanes)})")
        with self._stage("P5 research"):
            ideas, steps = researcher.ideas_parallel(profile, count, [p.title for p in props], round_, focus,
                                                     lanes, self.rc.workers)
        self.step(f"P5: {len(ideas)} idea(s) after {len(steps)} tool call(s)")
        fresh: list[tuple[TaskProposal, dict]] = []
        for i, idea in enumerate(ideas):
            pid = stable_id(self.run_id, "idea", str(round_), str(i))
            if pid in done:
                continue
            prov, _ = self._references(tools, idea)
            fresh.append((self.store.add_proposal(self._idea_proposal(pid, idea, prov, researcher.agent.model)), idea))
        alive_states = [S.CHECKED, S.PENDING_REVIEW, S.APPROVED, S.BUILDING] + DONE_OK
        live = self.store.list_proposals(self.run_id, [S.DRAFTED, *alive_states])
        merged = {w.proposal_id for w, _ in drafting.near_duplicates(live)}
        for p, _ in fresh:
            if p.proposal_id in merged:
                self.store.transition(p.proposal_id, "near_duplicate", "near-duplicate of another idea")
        todo = [(p, idea) for p, idea in fresh if p.proposal_id not in merged]
        with self._stage("P6 critic + spec"):
            pmap(lambda pi: self.judge_idea(repo, profile, tools, *pi), todo, self.rc.workers)
        if not ideas:
            self.step(f"P5 research round {round_} gave no ideas; continuing after the pause")

    def propose(self, repo: Path, profile: RepoProfile, peers: list[PeerRepo]) -> None:
        """Finite P5 + P6 mode, retained for direct controller users and ``--once``."""
        alive_states = [S.CHECKED, S.PENDING_REVIEW, S.APPROVED, S.BUILDING] + DONE_OK
        for round_ in range(1, self.rc.research_rounds + 1):
            alive = sum(p.state in alive_states for p in self.store.list_proposals(self.run_id))
            if alive >= self.opts.target:
                return
            self.propose_round(repo, profile, peers, round_,
                               math.ceil((self.opts.target - alive) * self.rc.overgenerate))

    def gate_review(self) -> None:
        if not self.opts.review:
            return
        for p in self.store.list_proposals(self.run_id, [S.CHECKED]):
            self.store.transition(p.proposal_id, "request_review", "review mode is on")

    # ------------------------------------------------------------------ build + validate (P8-P9)

    def _already(self, task_id: str) -> Path | None:
        if self.task_store is None:
            return None
        try:
            e = self.task_store.get(task_id)
        except Exception:  # noqa: BLE001 - not in the store
            return None
        return e.path if e.status != "deleted" else None

    def build_all(self) -> list[TaskProposal]:
        from ..packaging.harbor import to_harbor

        valid = self.store.list_proposals(self.run_id, DONE_OK)
        profile = self.store.profile(self.run_id, self.spec.name)

        def make_tools(repo: Path):
            return Toolbox(repo, profile, self.gh, [], None) if profile else None

        builder = Builder(self.pool.one("author"), self.pool.one("test_writer"), self.spec,
                          self.workdir / "reference" / "drafts", self.validate, repairs=self.rc.repairs,
                          flag_similarity=self.rc.similarity_flag, max_similarity=self.rc.similarity_max,
                          make_tools=make_tools, authors=self.pool.all("author"),
                          test_writers=self.pool.all("test_writer"), workers=self.rc.workers)
        pending = drafting.distinct_order(self.store.list_proposals(self.run_id, BUILDABLE))
        for p in pending:
            if p.state == S.CHECKED:
                if self.opts.review:
                    continue  # review mode: only the reviewer approves
                p = self.store.transition(p.proposal_id, "auto_approve", "auto-approve policy", approved_by="policy")
                self.store.add_review(Review(proposal_id=p.proposal_id, reviewer="policy:auto-approve", verdict="approve"))
            if p.state == S.APPROVED:
                p = self.store.transition(p.proposal_id, "build", "author + test writer")
            feature = self.features.get(p.provenance[0].feature_id) if p.provenance else None
            task_id = task_id_for(self.spec, p)
            build_s = None  # None: the package was built by an earlier run
            pkg = self._already(task_id)
            if pkg is None:
                self.step(f"P8 build: {p.title[:70]}")
                t0 = time.monotonic()
                with self._stage("P8 build"):
                    res = builder.build(p, feature, on_step=self.step)
                build_s = round(time.monotonic() - t0, 1)
                self.store.update_proposal(p.proposal_id, build_attempts=res.attempts, similarity=res.similarity,
                                           task_id=res.task_id)
                if res.verdict == "error":
                    raise EnvironmentProblem(f"build of {p.proposal_id} stopped: {res.reason}")
                if res.verdict != "validated":
                    self.store.transition(p.proposal_id, "repairs_exhausted", res.reason[:300])
                    continue
                pkg = self._already(res.task_id)
                task_id = res.task_id
            harbor_info = None
            harbor_s = None
            if pkg is not None:  # the Harbor folder is the deliverable; "off" only skips running Harbor
                task_dir = to_harbor(pkg, self.out)
                if self.harbor is not None and self.opts.harbor != "off":
                    self.step(f"P9 Harbor nop + oracle: {task_id}")
                    t0 = time.monotonic()
                    with self._stage("P9 Harbor"):
                        rep = self.harbor.validate(task_dir)
                    harbor_s = round(time.monotonic() - t0, 1)
                    harbor_info = rep.to_dict()
                    if rep.infra_error and self.opts.harbor == "required":
                        raise EnvironmentProblem(f"Harbor could not validate {task_id}: {rep.infra_error}")
                    if not rep.ok and not rep.infra_error:
                        self.store.transition(p.proposal_id, "repairs_exhausted", f"Harbor: {rep.reason()}"[:300],
                                              validation={"harbor": harbor_info})
                        if self.task_store is not None:
                            self.task_store.delete(task_id, f"Harbor validation failed: {rep.reason()}"[:300])
                        import shutil
                        shutil.rmtree(task_dir, ignore_errors=True)
                        continue
            meta_validation = self._validation_summary(pkg, harbor_info)
            meta_validation["timings"] = {"build_s": build_s, "harbor_s": harbor_s}
            p = self.store.transition(p.proposal_id, "validated", "gates and Harbor passed", task_id=task_id,
                                      validation=meta_validation)
            write_side_files(self.out, task_id, p, feature, self.snapshot.id, self.pool.declarations(), harbor_info)
            valid.append(p)
        return valid

    def _validation_summary(self, pkg: Path | None, harbor: dict | None) -> dict:
        out: dict = {"harbor": harbor}
        if pkg is not None:
            from ..review.store import read_meta

            try:
                meta = read_meta(pkg)
                out["gates"] = meta.validation.model_dump(mode="json")
                out["fail_to_pass"] = len(meta.fail_to_pass)
                out["pass_to_pass"] = len(meta.pass_to_pass)
                out["package"] = str(pkg)
            except Exception as exc:  # noqa: BLE001
                out["error"] = str(exc)
        return out

    # ------------------------------------------------------------------ benchmark (P10)

    def enqueue_benchmark(self) -> int:
        return enqueue_validated(self.store, self.run_id, self.out, self.opts.benchmark_agents, self.rc.benchmark)

    # ------------------------------------------------------------------ the whole run

    def _continuous(self, repo: Path, profile: RepoProfile, peers: list[PeerRepo], started: float) -> dict:
        """Research, validate and queue new packages until the operator interrupts the session."""
        queued = 0
        round_ = 1
        count = max(1, math.ceil(self.opts.target * self.rc.overgenerate))
        try:
            while True:
                self.propose_round(repo, profile, peers, round_, count)
                self.gate_review()
                self.build_all()
                queued += self.enqueue_benchmark()
                self.store.save_run(self.run_id, {"status": "running", "continuous_round": round_,
                                                  "queued": queued})
                self.step(f"continuous research: round {round_} complete; Ctrl-C stops after saved results")
                time.sleep(max(0.0, self.opts.research_pause_s))
                round_ += 1
        except KeyboardInterrupt:
            valid = self.store.list_proposals(self.run_id, DONE_OK)
            return self._finish(started, "stopped", queued=queued, valid=len(valid))

    def run(self) -> dict:
        started = time.time()
        self.snapshot.write_manifest(repo=self.spec.name, source=self.spec.source, commit=self.spec.commit_sha)
        self.store.save_run(self.run_id, {
            "repo": self.spec.name, "source": self.spec.source, "commit": self.spec.commit_sha,
            "snapshot_id": self.snapshot.id, "snapshot_mode": self.snapshot.mode, "started": started,
            "policy": {"auto_approve": not self.opts.review, "target": self.opts.target,
                       "overgenerate": self.rc.overgenerate, "harbor": self.opts.harbor},
            "out": str(self.out), "status": "running"})
        with tempfile.TemporaryDirectory(prefix="factory-ref-") as tmp:
            repo = export_clean(self.spec, Path(tmp) / "repo")
            profile = self.starter_profile(repo)
            peers = self.research(repo, profile)
            if self.opts.stop_after == "research":
                return self._finish(started, "research done")
            if self.opts.continuous and self.opts.stop_after is None:
                return self._continuous(repo, profile, peers, started)
            self.propose(repo, profile, peers)
            self.gate_review()
            if self.opts.stop_after == "proposals":
                return self._finish(started, "proposals done")
            if self.opts.review and self.store.list_proposals(self.run_id, [S.PENDING_REVIEW]):
                self.step("P7: proposals wait for review (factory proposals review), then run again")
        valid = self.build_all()
        queued = self.enqueue_benchmark()
        status = "done" if len(valid) >= self.opts.target else "short"
        return self._finish(started, status, queued=queued, valid=len(valid))

    def _finish(self, started: float, status: str, **extra) -> dict:
        data = {"status": status, "finished": time.time(), "seconds": round(time.time() - started, 1),
                "declarations": self.pool.declarations(), "github_calls": self.gh.calls,
                "timings": dict(self.timings), "llm_calls": self.pool.call_log[-1000:],
                "snapshot_hits": self.snapshot.hits, "snapshot_misses": self.snapshot.misses, **extra}
        self.store.save_run(self.run_id, data)
        return {"run_id": self.run_id, **data}


def enqueue_validated(store: ReferenceStore, run_id: str, out: Path, agents: list[str], bcfg) -> int:
    """Benchmark jobs for every validated task of the run (idempotent; changed packages re-run)."""
    from .benchmark import enqueue
    from .schema import JobBudget

    if not agents:
        return 0
    n = 0
    for p in store.list_proposals(run_id, [S.VALIDATED, S.QUEUED, S.RUNNING, S.BENCHMARKED, S.RUN_FAILED]):
        if not p.task_id or not (Path(out) / p.task_id).exists():
            continue
        pkg = (p.validation or {}).get("package")
        n += len(enqueue(store, run_id, p.task_id, Path(out) / p.task_id, agents, k=bcfg.k, proposal_id=p.proposal_id,
                         package_dir=Path(pkg) if pkg else None,
                         budget=JobBudget(timeout_s=bcfg.timeout_s, max_retries=bcfg.max_retries,
                                          cost_cap_usd=bcfg.cost_cap_usd)))
    return n
