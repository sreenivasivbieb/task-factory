"""The run report: valid tasks, provenance, validation evidence, solved vs failed, declarations."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from . import drafting
from .benchmark import summary as bench_summary
from .schema import ProposalState as S
from .store import ReferenceStore

VALID_STATES = {S.VALIDATED, S.QUEUED, S.RUNNING, S.BENCHMARKED, S.RUN_FAILED}


def _tool_versions() -> dict:
    out = {}
    for name, cmd in (("git", ["git", "--version"]), ("docker", ["docker", "--version"]), ("gh", ["gh", "--version"])):
        if shutil.which(cmd[0]):
            try:
                out[name] = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
            except (OSError, subprocess.SubprocessError, IndexError):
                pass
    return out


def latency(run: dict) -> dict:
    """Where the time went: stages, LLM calls per role (live vs cached), the slowest calls."""
    calls = run.get("llm_calls") or []
    roles: dict[str, dict] = {}
    for c in calls:
        r = roles.setdefault(c["role"], {"calls": 0, "cached": 0, "seconds": [], "chars_in": 0, "models": set()})
        r["models"].add(c["model"])
        if c.get("cached"):
            r["cached"] += 1
            continue
        r["calls"] += 1
        r["seconds"].append(c["seconds"])
        r["chars_in"] += c.get("chars_in", 0)
    out = {}
    for role, r in sorted(roles.items(), key=lambda kv: -sum(kv[1]["seconds"])):
        secs = sorted(r["seconds"])
        out[role] = {"calls": r["calls"], "cached": r["cached"], "total_s": round(sum(secs), 1),
                     "mean_s": round(sum(secs) / len(secs), 1) if secs else None,
                     "p50_s": secs[len(secs) // 2] if secs else None, "max_s": secs[-1] if secs else None,
                     "mean_prompt_chars": r["chars_in"] // r["calls"] if r["calls"] else None,
                     "models": sorted(r["models"])}
    slowest = sorted((c for c in calls if not c.get("cached")), key=lambda c: -c["seconds"])[:5]
    return {"stages": run.get("timings") or {}, "total_s": run.get("seconds"), "llm": out, "slowest_calls": slowest}


def build_report(store: ReferenceStore, run_id: str) -> dict:
    run = store.get_run(run_id) or {}
    props = store.list_proposals(run_id)
    feats = store.list_features(run_id)
    peers = store.list_peers(run_id)
    results = store.list_results(run_id)
    jobs = store.list_jobs(run_id)
    states = Counter(p.state.value for p in props)
    valid = [p for p in props if p.state in VALID_STATES]
    critic = [r for p in props for r in p.reviews if r.reviewer.startswith("critic:")]
    checks = [r for p in props for r in p.reviews if r.reviewer.startswith("check:")]
    per_task = []
    for p in valid:
        v = p.validation or {}
        gates = v.get("gates") or {}
        harbor = v.get("harbor") or {}
        rewards = {r.agent_id: r.reward for r in results if r.task_id == p.task_id}
        per_task.append({
            "task_id": p.task_id, "title": p.title, "kind": p.kind, "category": drafting.category(p),
            "source": p.provenance[0].pr_url if p.provenance else None,
            "source_kind": "capability" if p.provenance and "/blob/" in p.provenance[0].pr_url else "pr",
            "peer": p.provenance[0].peer_repo if p.provenance else None,
            "licence": p.provenance[0].licence if p.provenance else None,
            "mutation_score": gates.get("mutation_score"),
            "similarity_patch": p.similarity.patch if p.similarity else None,
            "similarity_tests": p.similarity.tests if p.similarity else None,
            "similarity_flagged": bool(p.similarity and p.similarity.flagged),
            "edge_cases_from_peer": len(p.edge_cases_from_peer),
            "build_attempts": len(p.build_attempts),
            "harbor_nop": (harbor.get("nop") or {}).get("reward"),
            "harbor_oracle": [o.get("reward") for o in harbor.get("oracle") or []],
            "benchmark": rewards, "manual_edits": len(p.edits),
            "build_s": (v.get("timings") or {}).get("build_s"), "harbor_s": (v.get("timings") or {}).get("harbor_s"),
            "attempt_timings": [a.get("timings") for a in p.build_attempts],
        })
    edits = [{"proposal": p.proposal_id, "by": e.by, "reason": e.reason, "fields": e.fields}
             for p in props for e in p.edits if not e.by.startswith("drafter:")]  # manual (human) edits only
    revisions = sum(1 for p in props for e in p.edits if e.by.startswith("drafter:"))
    return {
        "run_id": run_id, "repo": run.get("repo"), "commit": run.get("commit"), "source": run.get("source"),
        "snapshot": run.get("snapshot_id"), "snapshot_mode": run.get("snapshot_mode"), "policy": run.get("policy"),
        "status": run.get("status"), "seconds": run.get("seconds"), "generated": time.time(),
        "yield": {"proposals": len(props), "valid": len(valid), "distinct_valid": drafting.distinct_count(valid),
                  "states": dict(states)},
        "novelty": {"features_mined": len(feats), "rejected": sum(p.novelty.in_repo or p.novelty.in_upstream for p in props),
                    "rejections": [{"feature": p.title[:100], "pr": p.provenance[0].pr_url if p.provenance else "",
                                    "evidence": p.novelty.evidence}
                                   for p in props if p.novelty.in_repo or p.novelty.in_upstream]},
        "critics": {"reviews": len(critic), "passed": sum(r.verdict == "pass" for r in critic), "revisions": revisions,
                    "deterministic_rejections": len(checks)},
        "peers": [{"repo": p.full_name, "status": p.status, "licence": p.licence, "language": p.language,
                   "score": p.similarity.model_dump(mode="json"), "gap": len(p.gap), "reasons": p.reasons[:6]}
                  for p in peers],
        "tasks": per_task,
        "benchmark": bench_summary(results, jobs),
        "manual_edits": edits,
        "latency": latency(run),
        "declarations": {"models": run.get("declarations") or [], "agents_under_test": sorted({j.agent_id for j in jobs}),
                         "tools": _tool_versions()},
    }


def _pct(a: int, b: int) -> str:
    return f"{a}/{b}" + (f" ({100 * a / b:.0f}%)" if b else "")


def render_markdown(r: dict) -> str:
    y, c, n = r["yield"], r["critics"], r["novelty"]
    lines = [f"# Task Factory run {r['run_id']}", "",
             f"- Repository: {r['repo']} at `{r['commit']}` ({r['source']})",
             f"- Snapshot: `{r['snapshot']}` ({r['snapshot_mode']}); policy: {json.dumps(r['policy'])}",
             f"- Status: {r['status']}, {r['seconds']} s", "",
             "## Yield", "",
             f"- Valid tasks / proposals: {_pct(y['valid'], y['proposals'])}; distinct valid tasks: {y['distinct_valid']}",
             f"- Proposal states: {', '.join(f'{k} {v}' for k, v in sorted(y['states'].items()))}",
             f"- Peer references looked at: {n['features_mined']}; ideas the critic found already implemented: {n['rejected']}",
             f"- Critic pass rate: {_pct(c['passed'], c['reviews'])}; deterministic check rejections: "
             f"{c['deterministic_rejections']}", ""]
    if r["peers"]:
        lines += ["## Peer repos", "", "| Peer | Status | Licence | Lang | Overlap | Design | Maturity | Ecosystem | Total | Gap |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for p in r["peers"]:
            s = p["score"]
            lines.append(f"| {p['repo']} | {p['status']} | {p['licence']} | {p['language']} | {s['capability_overlap']} | "
                         f"{s['design_fit']} | {s['maturity']} | {s['ecosystem_fit']} | **{s['total']}** | {p['gap']} |")
        lines.append("")
    if r["tasks"]:
        agents = sorted({a for t in r["tasks"] for a in t["benchmark"]})
        lines += ["## Tasks", "", "| Task | Source (peer PR or capability) | Mutation | Similarity patch/tests | Harbor nop/oracle | "
                  + " | ".join(agents) + (" |" if agents else "|"),
                  "|---|---|---|---|---|" + "---|" * len(agents)]
        for t in r["tasks"]:
            flag = " ⚑" if t["similarity_flagged"] else ""
            bench = " | ".join(str(t["benchmark"].get(a, "")) for a in agents)
            lines.append(f"| `{t['task_id']}` {t['title']} | {t['source']} ({t['licence']}) | {t['mutation_score']} | "
                         f"{t['similarity_patch']}/{t['similarity_tests']}{flag} | {t['harbor_nop']}/{t['harbor_oracle']} | "
                         + bench + (" |" if agents else ""))
        lines.append("")
    b = r["benchmark"]
    if b["agents"] or b["pending_jobs"]:
        lines += ["## Benchmark (AI under test)", ""]
        for agent, a in b["agents"].items():
            n_ = a["solved"] + a["partial"] + a["failed"]
            lines.append(f"- **{agent}**: solved {a['solved']}, partial {a['partial']}, failed {a['failed']} (of {n_}); "
                         f"infrastructure errors {a['infra_error']} (not counted); cost {a['cost']:.2f}; "
                         f"time {a['wall_time_s']:.0f} s")
        if b["pending_jobs"]:
            lines.append(f"- {b['pending_jobs']} job(s) still queued or running")
        if b["nobody_solved"]:
            lines.append(f"- Nobody solved (check they are solvable): {', '.join(b['nobody_solved'])}")
        if b["everybody_solved"]:
            lines.append(f"- Everybody solved (maybe too easy): {', '.join(b['everybody_solved'])}")
        lines.append("")
    if n["rejections"]:
        lines += ["## Novelty rejections", ""]
        lines += [f"- {x['feature']} ({x['pr']}): {'; '.join(x['evidence'])}" for x in n["rejections"]]
        lines.append("")
    lat = r.get("latency") or {}
    if lat.get("stages") or lat.get("llm"):
        lines += ["## Latency", "", f"Total: {lat.get('total_s')} s", "", "| Stage | Seconds |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in lat.get("stages", {}).items()]
        lines += ["", "| Role | Live calls | Cached | Total s | Mean s | p50 s | Max s | Mean prompt chars | Models |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for role, x in lat.get("llm", {}).items():
            lines.append(f"| {role} | {x['calls']} | {x['cached']} | {x['total_s']} | {x['mean_s']} | {x['p50_s']} | "
                         f"{x['max_s']} | {x['mean_prompt_chars']} | {', '.join(x['models'])} |")
        if r["tasks"]:
            lines += ["", "| Task | Build s | Harbor s | Attempts (author / tests / gates s) |", "|---|---|---|---|"]
            for t in r["tasks"]:
                att = "; ".join(f"{(a or {}).get('author_s', '-')}/{(a or {}).get('tests_s', '-')}/{(a or {}).get('gates_s', '-')}"
                                for a in t["attempt_timings"])
                lines.append(f"| `{t['task_id']}` | {t['build_s']} | {t['harbor_s']} | {att} |")
        lines.append("")
    lines += ["## Manual edits", ""]
    lines += [f"- {e['proposal']} by {e['by']}: {e['reason']} ({', '.join(e['fields'])})" for e in r["manual_edits"]] or ["- none"]
    d = r["declarations"]
    lines += ["", "## Declarations (every model, agent and tool used)", ""]
    lines += [f"- {m['role']}: `{m['model']}` ({m['family']}), {m['calls']} calls, {m['cached']} cached" for m in d["models"]]
    lines += [f"- AI under test: `{a}`" for a in d["agents_under_test"]]
    lines += [f"- {k}: {v}" for k, v in d["tools"].items()]
    return "\n".join(lines) + "\n"


def write_report(store: ReferenceStore, run_id: str, out: Path) -> Path:
    r = build_report(store, run_id)
    out = Path(out)
    (out / "benchmark").mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    (out / "benchmark" / "results.json").write_text(json.dumps(
        [x.model_dump(mode="json") for x in store.list_results(run_id)], indent=2) + "\n", encoding="utf-8")
    path = out / "report.md"
    path.write_text(render_markdown(r), encoding="utf-8")
    return path
