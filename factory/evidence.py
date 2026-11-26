"""Evidence report: everything a reviewer needs to trust the tasks and the benchmark, from files.

``factory evidence`` reads task packages, their validation reports, rejections, the gate self-test and
harness runs, and writes::

    <out>/REPORT.md                       the human-readable report (headline numbers first)
    <out>/evidence/validation_matrix.csv  one row per task: every gate, F2P/P2P, mutation score, oracle/noop
    <out>/evidence/validation_matrix.json
    <out>/evidence/gate_selftest.json     known-bad tasks and the reject code each one got
    <out>/evidence/rejections.jsonl       every rejected draft with its reason and logs
    <out>/diversity/stats.json            how varied the tasks are (kinds, tiers, files, near-duplicates)
    <out>/benchmark/results.jsonl         every graded attempt of every agent
    <out>/benchmark/leaderboard.csv       resolved@k, pass@1, partial credit, failure categories per model
    <out>/benchmark/per_task.csv          task x model solve rates (empirical difficulty)

Nothing here runs a model or a container; the numbers are recomputed from the logs every time.
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

SANITY_AGENTS = {"oracle", "noop"}
GATE_ORDER = ["G0", "G1", "G7", "G2", "G3", "G4", "G6", "G5"]
GATE_NAMES = {
    "G0": "baseline suite passes", "G1": "reference patch applies, touches no tests",
    "G7": "spec names every API the tests use", "G2": "hidden tests pass with the reference",
    "G3": "hidden tests fail without it", "G4": "no existing test breaks",
    "G6": "plausible-but-wrong mutants are caught", "G5": "same results on repeats",
}


# --------------------------------------------------------------------------- tasks


@dataclass
class TaskRow:
    task_id: str
    repo: str
    language: str
    kind: str
    tags: list[str]
    author: str
    files_touched: int
    lines_added: int
    lines_removed: int
    hunks: int
    f2p: int
    p2p: int
    hidden_test_files: int
    gates: dict[str, bool]
    mutants_run: int
    mutants_caught: int
    mutation_score: float | None
    survivors: list[str]
    backend: str | None
    manual_edit: bool
    instruction_words: int
    tier: str = ""
    instruction: str = field(default="", repr=False)
    sanity_marks: tuple[str, str] = ("–", "–")  # oracle, noop

    def as_csv(self) -> dict:
        row = {k: v for k, v in self.__dict__.items()
               if k not in ("instruction", "gates", "tags", "survivors", "sanity_marks")}
        row["tags"] = ";".join(self.tags)
        row["survivors"] = ";".join(self.survivors)
        for g in GATE_ORDER:
            row[g] = {True: "pass", False: "FAIL", None: "-"}[self.gates.get(g)]
        return row


def _diff_stats(diff: str) -> tuple[int, int, int, int]:
    files = sum(1 for ln in diff.splitlines() if ln.startswith("diff --git "))
    added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    hunks = sum(1 for ln in diff.splitlines() if ln.startswith("@@"))
    return files, added, removed, hunks


def tier_of(files: int, changed: int, f2p: int) -> str:
    """S / M / L by the size of the reference change (an a-priori difficulty, before any model runs)."""
    if files >= 3 or changed > 80 or f2p >= 10:
        return "L"
    if files >= 2 or changed > 25 or f2p >= 5:
        return "M"
    return "S"


def _package_dirs(roots: list[Path]) -> list[Path]:
    out = []
    for root in roots:
        root = Path(root)
        if (root / "task.toml").exists():
            out.append(root)
        elif root.is_dir():
            out += sorted(p for p in root.iterdir() if (p / "task.toml").exists())
    return out


def read_task(pkg: Path) -> TaskRow:
    meta = tomllib.loads((pkg / "task.toml").read_text(encoding="utf-8"))
    report_path = pkg / "validation" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    diff_path = pkg / "solution" / "solution.diff"
    diff = diff_path.read_text(encoding="utf-8") if diff_path.exists() else ""
    files, added, removed, hunks = _diff_stats(diff)
    instruction = (pkg / "instruction.md").read_text(encoding="utf-8") if (pkg / "instruction.md").exists() else ""
    gates = {g["gate"]: g["passed"] for g in report.get("gates", [])}
    if not gates:  # older packages only list the gates that ran
        gates = {g: True for g in meta.get("validation", {}).get("gates", [])}
    gen = meta.get("generated_by", {})
    author = gen.get("model") or "human"
    v = meta.get("validation", {})
    row = TaskRow(
        task_id=meta["task_id"], repo=meta.get("repo_name", ""), language=meta.get("language") or "",
        kind=meta.get("kind", ""), tags=list(meta.get("tags", [])), author=author,
        files_touched=files or len(meta.get("touched_files", [])), lines_added=added, lines_removed=removed,
        hunks=hunks, f2p=len(meta.get("fail_to_pass", [])), p2p=len(meta.get("pass_to_pass", [])),
        hidden_test_files=len(meta.get("test_files", [])), gates=gates,
        mutants_run=report.get("mutants_run", v.get("mutants_run", 0)),
        mutants_caught=report.get("mutants_caught", v.get("mutants_caught", 0)),
        mutation_score=report.get("mutation_score", v.get("mutation_score")),
        survivors=report.get("surviving_mutants", []), backend=report.get("backend") or v.get("backend"),
        manual_edit=bool(meta.get("manual_edit")), instruction_words=len(instruction.split()),
        instruction=instruction)
    row.tier = tier_of(row.files_touched, added + removed, row.f2p)
    return row


# --------------------------------------------------------------------------- diversity


_WORD = re.compile(r"[a-z_][a-z0-9_]{3,}")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def diversity(rows: list[TaskRow], dup_threshold: float = 0.6) -> dict:
    toks = {r.task_id: _tokens(r.instruction) for r in rows}
    pairs = [(a.task_id, b.task_id, jaccard(toks[a.task_id], toks[b.task_id])) for a, b in combinations(rows, 2)]
    sims = [s for _, _, s in pairs]
    all_tags = Counter(t for r in rows for t in r.tags)

    def dist(key):
        return dict(sorted(Counter(key(r) for r in rows).items()))

    def spread(values):
        return {"min": min(values), "median": statistics.median(values), "max": max(values)} if values else {}

    return {
        "tasks": len(rows),
        "repos": dist(lambda r: r.repo), "languages": dist(lambda r: r.language), "kinds": dist(lambda r: r.kind),
        "tiers": dist(lambda r: r.tier), "authors": dist(lambda r: "human" if r.author == "human" else "model"),
        "tags": dict(all_tags.most_common()),
        "files_touched": spread([r.files_touched for r in rows]),
        "multi_file_tasks": sum(1 for r in rows if r.files_touched > 1),
        "lines_changed": spread([r.lines_added + r.lines_removed for r in rows]),
        "fail_to_pass_tests": spread([r.f2p for r in rows]),
        "instruction_words": spread([r.instruction_words for r in rows]),
        "instruction_similarity": {"mean": round(statistics.mean(sims), 3) if sims else None,
                                   "max": round(max(sims), 3) if sims else None,
                                   "near_duplicates": [{"a": a, "b": b, "jaccard": round(s, 3)}
                                                       for a, b, s in pairs if s >= dup_threshold],
                                   "threshold": dup_threshold},
    }


# --------------------------------------------------------------------------- benchmark


def _harness_runs(roots: list[Path]) -> list[Path]:
    runs = []
    for root in roots:
        root = Path(root)
        if (root / "results.jsonl").exists():
            runs.append(root)
        elif root.is_dir():
            runs += sorted(p for p in root.iterdir() if (p / "results.jsonl").exists())
    return runs


def load_results(roots: list[Path], task_ids: set[str] | None = None) -> list[dict]:
    """Every attempt; a rerun of the same (agent, model, task, attempt) replaces the older one."""
    latest: dict[tuple, dict] = {}
    for run in _harness_runs(roots):
        for line in (run / "results.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if task_ids is not None and r["task_id"] not in task_ids:
                continue
            r["run_id"] = run.name
            latest[(r["agent"], r.get("model"), r["task_id"], r["attempt"])] = r
    return sorted(latest.values(), key=lambda r: (r["agent"], r.get("model") or "", r["task_id"], r["attempt"]))


def label(r: dict) -> str:
    return f"{r['agent']}:{r['model']}" if r.get("model") else r["agent"]


def leaderboard(results: list[dict]) -> list[dict]:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_model[label(r)].append(r)
    rows = []
    for name, rs in by_model.items():
        scored = [r for r in rs if r["category"] != "agent_error"]  # infrastructure, not the model
        tasks = sorted({r["task_id"] for r in scored})
        solved = {t for t in tasks if any(r["resolved"] for r in scored if r["task_id"] == t)}
        partial = [r["f2p_passed"] / r["f2p_total"] for r in scored if r.get("f2p_total")]
        cats = Counter(r["category"] for r in rs)
        costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        rows.append({
            "model": name, "sanity": rs[0]["agent"] in SANITY_AGENTS, "tasks": len(tasks), "attempts": len(scored),
            "resolved_at_k": round(len(solved) / len(tasks), 3) if tasks else None,
            "pass_at_1": round(sum(r["resolved"] for r in scored) / len(scored), 3) if scored else None,
            "mean_hidden_tests_passed": round(statistics.mean(partial), 3) if partial else None,
            "tampered_with_tests": sum(1 for r in scored if r.get("tests_tampered")),
            "mean_agent_time_s": round(statistics.mean(r["agent_duration_s"] for r in scored), 1) if scored else None,
            "cost_usd": round(sum(costs), 4) if costs else None,
            "agent_errors": cats.get("agent_error", 0),
            **{f"n_{c}": cats.get(c, 0) for c in ("resolved", "wrong_behaviour", "regression", "build_error",
                                                   "no_change", "patch_apply_fail", "timeout", "leak")},
        })
    return sorted(rows, key=lambda r: (r["sanity"], -(r["resolved_at_k"] or 0), r["model"]))


def per_task(results: list[dict], task_ids: list[str]) -> tuple[list[str], dict[str, dict[str, float | None]]]:
    models = sorted({label(r) for r in results if r["agent"] not in SANITY_AGENTS})
    table: dict[str, dict[str, float | None]] = {}
    for t in task_ids:
        table[t] = {}
        for m in models:
            rs = [r for r in results if r["task_id"] == t and label(r) == m and r["category"] != "agent_error"]
            table[t][m] = round(sum(r["resolved"] for r in rs) / len(rs), 2) if rs else None
    return models, table


def failure_modes(results: list[dict]) -> dict[str, dict]:
    """Why models fail, from the grades: no attempt, wrong behaviour (none / some hidden tests), regressions."""
    out: dict[str, dict] = {}
    for r in results:
        if r["agent"] in SANITY_AGENTS or r["resolved"] or r["category"] == "agent_error":
            continue
        m = out.setdefault(label(r), Counter())
        cat = r["category"]
        if cat == "wrong_behaviour" and r.get("f2p_total"):
            cat = "wrong_behaviour: partial" if r["f2p_passed"] else "wrong_behaviour: none of the hidden tests"
        m[cat] += 1
        if r.get("p2p_failed"):
            m["broke existing tests"] += 1
        if r.get("tests_tampered"):
            m["edited the tests (restored before grading)"] += 1
    return {k: dict(v.most_common()) for k, v in out.items()}


# --------------------------------------------------------------------------- report


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def _md_table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join("" if c is None else str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        if not rows:
            return
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def build(tasks: list[Path], harness: list[Path], out: Path, *, rejections: list[Path] = (),
          selftests: list[Path] = (), title: str = "Coding Task Factory: evidence report",
          reproduce: str | None = None) -> dict:
    out = Path(out)
    rows = [read_task(p) for p in _package_dirs(list(tasks))]
    ids = {r.task_id for r in rows}
    results = load_results(list(harness), ids)
    board = leaderboard(results)
    sanity = {r["model"]: r for r in board if r["sanity"]}
    models, matrix = per_task(results, [r.task_id for r in rows])
    fails = failure_modes(results)
    div = diversity(rows)

    # oracle / noop per task
    def solved_by(agent: str, task: str):
        rs = [r for r in results if r["agent"] == agent and r["task_id"] == task]
        return None if not rs else all(r["resolved"] for r in rs)

    matrix_rows = []
    for r in rows:
        d = r.as_csv()
        d["oracle_resolves"] = {True: "yes", False: "NO", None: "-"}[solved_by("oracle", r.task_id)]
        d["noop_resolves"] = {True: "YES", False: "no", None: "-"}[solved_by("noop", r.task_id)]
        r.sanity_marks = ({True: "✅", False: "❌", None: "–"}[solved_by("oracle", r.task_id)],
                          {True: "❌", False: "✅", None: "–"}[solved_by("noop", r.task_id)])
        matrix_rows.append(d)

    rejected = []
    for p in rejections:
        if Path(p).exists():
            rejected += [json.loads(ln) for ln in Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]
    selftest = []
    for p in selftests:
        if Path(p).exists():
            selftest += json.loads(Path(p).read_text(encoding="utf-8"))

    # files
    _write_csv(out / "evidence" / "validation_matrix.csv", matrix_rows)
    (out / "evidence" / "validation_matrix.json").write_text(json.dumps(matrix_rows, indent=2), encoding="utf-8")
    (out / "evidence" / "gate_selftest.json").write_text(json.dumps(selftest, indent=2), encoding="utf-8")
    (out / "evidence" / "rejections.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rejected),
                                                       encoding="utf-8")
    (out / "diversity").mkdir(parents=True, exist_ok=True)
    (out / "diversity" / "stats.json").write_text(json.dumps(div, indent=2), encoding="utf-8")
    (out / "benchmark").mkdir(parents=True, exist_ok=True)
    (out / "benchmark" / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results),
                                                     encoding="utf-8")
    _write_csv(out / "benchmark" / "leaderboard.csv", board)
    _write_csv(out / "benchmark" / "per_task.csv",
               [{"task_id": t, **{m: v for m, v in cols.items()}} for t, cols in matrix.items()])
    (out / "benchmark" / "failure_modes.json").write_text(json.dumps(fails, indent=2), encoding="utf-8")

    summary = {
        "tasks": len(rows), "repos": len(div["repos"]), "languages": len(div["languages"]),
        "all_gates_passed": sum(1 for r in rows if all(r.gates.get(g, True) for g in GATE_ORDER)),
        "mean_mutation_score": round(statistics.mean(r.mutation_score for r in rows if r.mutation_score is not None), 3)
        if any(r.mutation_score is not None for r in rows) else None,
        "mutants_total": sum(r.mutants_run for r in rows),
        "oracle_resolved": sanity.get("oracle", {}).get("resolved_at_k"),
        "noop_resolved": sanity.get("noop", {}).get("resolved_at_k"),
        "selftest_pass": sum(1 for s in selftest if s.get("ok")), "selftest_total": len(selftest),
        "rejected_drafts": len(rejected),
        "models_benchmarked": len([b for b in board if not b["sanity"]]),
        "isolated_backend": all((r.backend or "docker") == "docker" for r in rows),
    }
    (out / "evidence" / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "REPORT.md").write_text(_render(title, summary, rows, board, models, matrix, fails, div, rejected,
                                           selftest, reproduce), encoding="utf-8")
    return summary


def _render(title, s, rows, board, models, matrix, fails, div, rejected, selftest, reproduce) -> str:
    L: list[str] = [f"# {title}", ""]
    L += ["## Headline", "",
          f"- **{s['tasks']} validated feature tasks** across {s['repos']} repositor{'y' if s['repos'] == 1 else 'ies'} "
          f"and {s['languages']} language(s); {s['all_gates_passed']}/{s['tasks']} passed all {len(GATE_ORDER)} gates.",
          f"- **Reference solves every task, an empty patch solves none:** oracle {_pct(s['oracle_resolved'])}, "
          f"noop {_pct(s['noop_resolved'])} (graded by the same harness the models use).",
          f"- **Tests catch wrong solutions, not only missing ones:** {s['mutants_total']} plausible-but-wrong mutants "
          f"run, mean mutation score {_pct(s['mean_mutation_score'])}.",
          f"- **The gates reject broken tasks:** gate self-test {s['selftest_pass']}/{s['selftest_total']} known-bad "
          f"tasks rejected with the expected code; {s['rejected_drafts']} draft(s) rejected during generation.",
          f"- **Benchmark:** {s['models_benchmarked']} model(s) under test, hidden tests, no internet except the model API.",
          ]
    if not s["isolated_backend"]:
        L.append("- ⚠️ Some tasks were validated with the local (non-isolated) backend; rerun with Docker for final evidence.")
    if reproduce:
        L += ["", "**Reproduce everything:**", "", "```sh", reproduce, "```"]

    L += ["", "## 1. Validity: every task is proven by executed tests", "",
          "Each gate runs in a fresh offline container; the first failure rejects the task. "
          "G7 is static (no container).", "",
          _md_table(["Gate", "Proves"], [[g, GATE_NAMES[g]] for g in GATE_ORDER]), ""]
    L += [_md_table(["task", "repo", "tier", "files", "F2P", "P2P", *GATE_ORDER, "mutants", "oracle", "noop"],
                    [[r.task_id, r.repo, r.tier, r.files_touched, r.f2p, r.p2p,
                      *[{True: "✅", False: "❌", None: "–"}[r.gates.get(g)] for g in GATE_ORDER],
                      f"{r.mutants_caught}/{r.mutants_run}", *r.sanity_marks]
                     for r in rows]), "",
          "oracle ✅ = the reference solution resolves the task in the harness; noop ✅ = an empty "
          "solution does not.", ""]
    if selftest:
        L += ["### Gate self-test: known-bad tasks must be rejected", "",
              _md_table(["fixture", "expected", "got", "ok"],
                        [[t["fixture"], t["expected"], t["got"], "✅" if t["ok"] else "❌"] for t in selftest]), ""]
    if rejected:
        codes = Counter(r.get("reject_code") for r in rejected)
        L += ["### Drafts the gates rejected", "", _md_table(["reject code", "drafts"], list(codes.most_common())), ""]

    L += ["## 2. Variation", "",
          _md_table(["dimension", "distribution"],
                    [["repositories", div["repos"]], ["languages", div["languages"]], ["kinds", div["kinds"]],
                     ["size tier (S/M/L)", div["tiers"]], ["author", div["authors"]],
                     ["multi-file tasks", f"{div['multi_file_tasks']}/{div['tasks']}"],
                     ["files touched", div["files_touched"]], ["lines changed", div["lines_changed"]],
                     ["hidden F2P tests", div["fail_to_pass_tests"]],
                     ["instruction similarity (Jaccard)", f"mean {div['instruction_similarity']['mean']}, "
                                                          f"max {div['instruction_similarity']['max']}"],
                     ["near-duplicates (≥ 0.6)", len(div["instruction_similarity"]["near_duplicates"])]]), "",
          "Tags: " + ", ".join(f"{k} ({v})" for k, v in div["tags"].items()), ""]

    L += ["## 3. Benchmark", ""]
    models_board = [b for b in board if not b["sanity"]]
    if models_board:
        L += [_md_table(["model", "tasks", "resolved@k", "pass@1", "hidden tests passed (mean)", "wrong behaviour",
                         "regression", "no change", "timeout", "edited tests", "cost $"],
                        [[b["model"], b["tasks"], _pct(b["resolved_at_k"]), _pct(b["pass_at_1"]),
                          _pct(b["mean_hidden_tests_passed"]), b["n_wrong_behaviour"], b["n_regression"],
                          b["n_no_change"], b["n_timeout"], b["tampered_with_tests"], b["cost_usd"]]
                         for b in models_board]), ""]
        L += ["### Per task (share of attempts resolved)", "",
              _md_table(["task", *models, "solved by"],
                        [[t, *[_pct(cols[m]) for m in models],
                          f"{sum(1 for m in models if (cols[m] or 0) > 0)}/{len(models)}"]
                         for t, cols in matrix.items()]), ""]
        unsolved = [t for t, cols in matrix.items() if models and all((cols[m] or 0) == 0 for m in models)]
        if unsolved:
            L += [f"Unsolved by every model ({len(unsolved)}): " + ", ".join(unsolved), ""]
        if fails:
            L += ["### Failure modes", ""]
            for m, counts in fails.items():
                L.append(f"- **{m}:** " + "; ".join(f"{k} × {v}" for k, v in counts.items()))
            L.append("")
    else:
        L += ["No model has been benchmarked yet: run `factory pipeline` with `[pipeline.benchmark] agents` set.", ""]

    L += ["## 4. Files", "",
          "- `evidence/validation_matrix.csv` — one row per task, every gate", "- `evidence/gate_selftest.json`",
          "- `evidence/rejections.jsonl`", "- `diversity/stats.json`",
          "- `benchmark/leaderboard.csv`, `benchmark/per_task.csv`, `benchmark/results.jsonl`, "
          "`benchmark/failure_modes.json`", "- `tasks/<task_id>/` — the packages (instruction, hidden tests, "
          "reference solution, environment, validation logs)", ""]
    return "\n".join(L)
