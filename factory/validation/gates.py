"""Validation runner (section 10): prove a task is valid by running code.

Gate order: G0 baseline, G1 patch applies, G7 spec coverage (static: the tests only
call names the requirements give), G2 feature passes, G3 feature fails without the
feature, G4 regression, G6 negative controls (mutants), then G5 stability (G5 runs
last because it is the most expensive). Every gate uses a fresh
sandbox from the same image. The first failing gate rejects the task. No model
opinion is involved anywhere.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import GatesConfig
from ..models import EnvConfig, GateResult, ImageRef, RejectCode, RepoSpec, RunSummary, TaskDraft, ValidationReport
from ..harness.protect import protected_in_patch
from ..runner.test_runner import flaky_ids, run_tests
from ..sandbox.base import Sandbox, SandboxFactory
from .mutations import make_mutants

log = logging.getLogger("factory.validation")


class _Reject(Exception):
    def __init__(self, code: RejectCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _names(ids, limit: int = 10) -> str:
    ids = sorted(ids)
    more = f" (+{len(ids) - limit} more)" if len(ids) > limit else ""
    return ", ".join(ids[:limit]) + more


@dataclass
class Baseline:
    summary: RunSummary
    passed: set[str] = field(default_factory=set)
    known_failures: set[str] = field(default_factory=set)
    all_ids: set[str] = field(default_factory=set)


class Validator:
    """Runs the gates for tasks on one pinned repo + environment."""

    def __init__(self, factory: SandboxFactory, spec: RepoSpec, env: EnvConfig, cfg: GatesConfig | None = None,
                 log_dir: Path | None = None, cache_dir: Path | None = None) -> None:
        self.factory = factory
        self.spec = spec
        self.env = env
        self.cfg = cfg or GatesConfig()
        self.log_dir = Path(log_dir) if log_dir else None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._image: ImageRef | None = None
        self._baseline: Baseline | None = None

    # ------------------------------------------------------------------ helpers

    @property
    def image(self) -> ImageRef:
        if self._image is None:
            self._image = self.factory.build_image(self.spec, self.env)
        return self._image

    def _log(self, task_id: str, name: str) -> Path | None:
        return self.log_dir / task_id / f"{name}.json" if self.log_dir else None

    def _sandbox(self) -> Sandbox:
        return self.factory.new_sandbox(self.image)

    def _prepare(self, sb: Sandbox, draft: TaskDraft, patch: str | None, task_id: str, gate: str) -> None:
        if patch is not None:
            r = sb.apply_patch(patch)
            if not r.ok:
                raise _Reject(RejectCode.PATCH_APPLY_FAIL, f"{gate}: patch did not apply: {r.stderr.strip()[:500]}")
        for path, content in draft.test_files.items():
            sb.write_file(path, content)

    # ------------------------------------------------------------------ G0

    def baseline(self) -> Baseline:
        """Run the full existing suite on the starting repo. Cached per image."""
        if self._baseline is not None:
            return self._baseline
        cache_file = None
        if self.cache_dir is not None:
            # keyed by image AND test settings: a changed test command must not reuse an old baseline
            env_key = hashlib.sha256(self.env.to_json().encode()).hexdigest()[:12]
            name = self.image.tag.replace("/", "_").replace(":", "_") + f"-{env_key}.json"
            cache_file = self.cache_dir / "baseline" / name
        if cache_file is not None and cache_file.exists():
            summary = RunSummary.load(cache_file)
        else:
            with self._sandbox() as sb:
                summary = run_tests(sb, self.env, log_to=self._log("_baseline", "G0"))
            if cache_file is not None and not summary.crashed:
                summary.save(cache_file)
        known = summary.ids_with("failed", "error")
        self._baseline = Baseline(summary=summary, passed=summary.ids_with("passed"), known_failures=known,
                                  all_ids=set(summary.status_of()))
        return self._baseline

    def _gate_g0(self) -> GateResult:
        b = self.baseline()
        total = len(b.all_ids)
        if b.summary.crashed:
            raise _Reject(RejectCode.BASELINE_FAIL, f"baseline run crashed: {b.summary.error}")
        if not b.passed:
            raise _Reject(RejectCode.BASELINE_FAIL, "baseline has no passing tests")
        ratio = len(b.known_failures) / total if total else 1.0
        if ratio > self.cfg.max_known_failure_ratio:
            raise _Reject(RejectCode.BASELINE_FAIL,
                          f"{len(b.known_failures)}/{total} baseline tests fail "
                          f"(limit {self.cfg.max_known_failure_ratio:.0%}): {_names(b.known_failures)}")
        details = f"{len(b.passed)} passed"
        if b.known_failures:
            details += f"; {len(b.known_failures)} known failures excluded: {_names(b.known_failures)}"
        return GateResult(gate="G0", passed=True, details=details)

    # ------------------------------------------------------------------ G1..G6

    def _gate_g1(self, draft: TaskDraft) -> GateResult:
        if not draft.solution_patch.strip():
            raise _Reject(RejectCode.PATCH_APPLY_FAIL, "reference patch is empty")
        protected = protected_in_patch(draft.solution_patch, self.env.protected_paths)
        if protected:
            # the grader restores these before grading, so no solver could reproduce this solution
            raise _Reject(RejectCode.SOLUTION_TOUCHES_PROTECTED,
                          f"reference patch changes test or test-config files: {_names(protected)}; "
                          "put test changes in the hidden tests instead")
        with self._sandbox() as sb:
            r = sb.apply_patch(draft.solution_patch)
        if not r.ok:
            raise _Reject(RejectCode.PATCH_APPLY_FAIL, f"git apply --check failed: {r.stderr.strip()[:800]}")
        return GateResult(gate="G1", passed=True, details="patch applies cleanly")

    def _run_feature(self, draft: TaskDraft, patch: str | None, gate: str, select_all: bool = False) -> RunSummary:
        with self._sandbox() as sb:
            self._prepare(sb, draft, patch, draft.task_id, gate)
            select = None if select_all else sorted(draft.test_files)
            return run_tests(sb, self.env, select, log_to=self._log(draft.task_id, gate))

    def _gate_g2(self, draft: TaskDraft, base: Baseline) -> tuple[GateResult, set[str], RunSummary]:
        if not draft.test_files:
            raise _Reject(RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH, "task has no feature test files")
        s = self._run_feature(draft, draft.solution_patch, "G2")
        if s.crashed:
            raise _Reject(RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH, f"feature test run crashed: {s.error}")
        feature = set(s.status_of()) - base.all_ids
        if not feature:
            raise _Reject(RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH, "no new tests found in the feature test files")
        not_passed = {t for t in feature if s.status_of()[t] != "passed"}
        if not_passed:
            raise _Reject(RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH,
                          f"feature tests fail with the reference patch: {_names(not_passed)}")
        return GateResult(gate="G2", passed=True, details=f"{len(feature)} feature tests pass"), feature, s

    @staticmethod
    def _passing(s: RunSummary, feature: set[str]) -> set[str]:
        """Feature tests that did NOT fail in run ``s`` (absent tests count as failed when the run failed)."""
        status = s.status_of()
        out = set()
        for t in feature:
            st = status.get(t)
            if st in ("failed", "error"):
                continue
            if st is None and (s.raw_exit_code != 0 or s.crashed):
                continue  # not collected because the feature is missing (e.g. ImportError)
            out.add(t)
        return out

    def _gate_g3(self, draft: TaskDraft, feature: set[str]) -> GateResult:
        s = self._run_feature(draft, None, "G3")
        passing = self._passing(s, feature)
        if passing:
            raise _Reject(RejectCode.FEATURE_TESTS_PASS_WITHOUT_PATCH,
                          f"not valid feature tests (they pass without the feature): {_names(passing)}")
        return GateResult(gate="G3", passed=True, details=f"all {len(feature)} feature tests fail without the patch")

    def _gate_g4(self, draft: TaskDraft, base: Baseline, feature: set[str]) -> tuple[GateResult, RunSummary]:
        s = self._run_feature(draft, draft.solution_patch, "G4", select_all=True)
        if s.crashed:
            raise _Reject(RejectCode.REGRESSION, f"full suite crashed with the patch: {s.error}")
        status = s.status_of()
        broken = {t for t in base.passed if status.get(t) != "passed"}
        if broken:
            raise _Reject(RejectCode.REGRESSION, f"existing tests broken by the patch: {_names(broken)}")
        feature_bad = {t for t in feature if status.get(t) != "passed"}
        if feature_bad:
            raise _Reject(RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH,
                          f"feature tests fail when run with the full suite: {_names(feature_bad)}")
        return GateResult(gate="G4", passed=True, details=f"{len(base.passed)} existing tests still pass"), s

    def _gate_g7(self, draft: TaskDraft) -> GateResult:
        """Spec coverage (static): the tests only call names the solver can know about."""
        if not self.cfg.spec_check:
            return GateResult(gate="G7", passed=True, details="skipped (gates.spec_check = false)")
        from .spec_check import spec_findings

        findings = spec_findings(draft.requirements_md, draft.test_files, self.spec)
        if findings:
            raise _Reject(RejectCode.UNDERSPECIFIED,
                          "the hidden tests use names the requirements never give, so a correct solver could not "
                          "know them: " + "; ".join(map(str, findings[:8]))
                          + (f" (+{len(findings) - 8} more)" if len(findings) > 8 else ""))
        return GateResult(gate="G7", passed=True,
                          details="every repository name the tests import exists or is named in the requirements")

    def _gate_g6(self, draft: TaskDraft, feature: set[str], survivors: list[str]) -> tuple[GateResult, int, int]:
        # The empty-patch control is exactly G3, which has already passed.
        run, caught = 1, 1
        notes = ["empty: caught (G3)"]
        mutants = make_mutants(draft.solution_patch, draft.test_files, self.cfg.mutations)
        if not mutants:
            notes.append("no code mutant could be made from this patch, so only the empty control ran")
        need = self.cfg.min_mutation_score
        applicable = 1 + len(mutants)  # upper bound; mutants that do not apply are dropped from it
        for m in mutants:
            with self._sandbox() as sb:
                if not sb.apply_patch(m.patch).ok:
                    notes.append(f"{m.name}: skipped (does not apply)")
                    applicable -= 1
                    continue
                for path, content in draft.test_files.items():
                    sb.write_file(path, content)
                s = run_tests(sb, self.env, sorted(draft.test_files),
                              log_to=self._log(draft.task_id, f"G6-{m.name.replace('#', '-')}"))
            run += 1
            if self._passing(s, feature) == feature:
                survivors.append(m.name)
                notes.append(f"{m.name}: SURVIVED")
                # stop as soon as the threshold can no longer be reached
                if (applicable - len(survivors)) / applicable < need:
                    raise _Reject(RejectCode.NEG_CONTROL_PASSED,
                                  f"mutant {m.name} passes every feature test; the tests are too weak "
                                  f"(caught {caught}/{run}; survivors: {', '.join(survivors)})")
                continue
            caught += 1
            notes.append(f"{m.name}: caught")
        return GateResult(gate="G6", passed=True, details="; ".join(notes)), run, caught

    def _gate_g5(self, draft: TaskDraft, g2: RunSummary, g4: RunSummary) -> tuple[GateResult, set[str]]:
        n = self.cfg.stability_repeats
        if n <= 0:
            return GateResult(gate="G5", passed=True, details="skipped (gates.stability_repeats = 0)"), set()
        select_runs, full_runs = [g2], [g4]
        with self._sandbox() as sb:
            self._prepare(sb, draft, draft.solution_patch, draft.task_id, "G5")
            for i in range(n):
                select_runs.append(run_tests(sb, self.env, sorted(draft.test_files),
                                             log_to=self._log(draft.task_id, f"G5-feature-{i + 1}")))
                full_runs.append(run_tests(sb, self.env, None, log_to=self._log(draft.task_id, f"G5-full-{i + 1}")))
        flaky = flaky_ids(select_runs) | flaky_ids(full_runs)
        if flaky:
            raise _FlakyReject(flaky)
        return GateResult(gate="G5", passed=True, details=f"same result in {n} repeat(s)"), set()

    # ------------------------------------------------------------------ driver

    def validate(self, draft: TaskDraft) -> ValidationReport:
        """Run every gate; the first failure rejects with its reject code."""
        gates: list[GateResult] = []
        report = ValidationReport(task_id=draft.task_id, verdict="rejected",
                                  backend=getattr(self.factory, "backend_name", None))
        current = "G0"
        start = time.monotonic()
        try:
            t = time.monotonic()
            g = self._gate_g0()
            gates.append(_timed(g, t))
            base = self.baseline()
            report.known_failures = sorted(base.known_failures)
            current, t = "G1", time.monotonic()
            gates.append(_timed(self._gate_g1(draft), t))
            current, t = "G7", time.monotonic()
            gates.append(_timed(self._gate_g7(draft), t))
            current, t = "G2", time.monotonic()
            g, feature, g2_run = self._gate_g2(draft, base)
            gates.append(_timed(g, t))
            current, t = "G3", time.monotonic()
            gates.append(_timed(self._gate_g3(draft, feature), t))
            current, t = "G4", time.monotonic()
            g, g4_run = self._gate_g4(draft, base, feature)
            gates.append(_timed(g, t))
            current, t = "G6", time.monotonic()
            g, report.mutants_run, report.mutants_caught = self._gate_g6(draft, feature, report.surviving_mutants)
            gates.append(_timed(g, t))
            current, t = "G5", time.monotonic()
            g, _ = self._gate_g5(draft, g2_run, g4_run)
            gates.append(_timed(g, t))
            report.verdict = "valid"
            report.fail_to_pass = sorted(feature)
            report.pass_to_pass = sorted(base.passed)
        except _FlakyReject as exc:
            report.flaky_tests = sorted(exc.ids)
            gates.append(GateResult(gate=current, passed=False, details=exc.reason))
            report.reject_code, report.reject_reason = exc.code, exc.reason
        except _Reject as exc:
            gates.append(GateResult(gate=current, passed=False, details=exc.reason))
            report.reject_code, report.reject_reason = exc.code, exc.reason
        report.gates = gates
        if report.mutants_run:
            report.mutation_score = round(report.mutants_caught / report.mutants_run, 3)
        if self.log_dir is not None:
            for gr in report.gates:
                p = self._log("_baseline" if gr.gate == "G0" else draft.task_id, gr.gate)
                if p is not None and p.exists():
                    gr.exec_log_paths.append(str(p))
            mutant_log_dir = self.log_dir / draft.task_id
            mutant_logs = sorted(mutant_log_dir.glob("G[56]-*.json")) if mutant_log_dir.is_dir() else []
            for gr in report.gates:
                if gr.gate in ("G5", "G6"):
                    gr.exec_log_paths += [str(p) for p in mutant_logs if p.name.startswith(gr.gate)]
            report.save(self.log_dir / draft.task_id / "report.json")
        log.info("%s: %s%s (%.1fs)", draft.task_id, report.verdict,
                 f" [{report.reject_code.value}]" if report.reject_code else "", time.monotonic() - start)
        return report


class _FlakyReject(_Reject):
    def __init__(self, ids: set[str]) -> None:
        super().__init__(RejectCode.FLAKY, f"results differ between repeated runs: {_names(ids)}")
        self.ids = ids


def _timed(g: GateResult, start: float) -> GateResult:
    g.duration_s = round(time.monotonic() - start, 3)
    return g


def validate_task(factory: SandboxFactory, spec: RepoSpec, env: EnvConfig, draft: TaskDraft,
                  cfg: GatesConfig | None = None, log_dir: Path | None = None,
                  cache_dir: Path | None = None) -> ValidationReport:
    """Validate one draft (convenience wrapper around ``Validator``)."""
    return Validator(factory, spec, env, cfg, log_dir, cache_dir).validate(draft)


def append_rejection(path: Path, report: ValidationReport) -> None:
    """Append a rejected task to ``rejections.jsonl`` with its reason and log paths."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"task_id": report.task_id, "reject_code": report.reject_code.value if report.reject_code else None,
             "reject_reason": report.reject_reason,
             "logs": [p for g in report.gates for p in g.exec_log_paths]}
    line = json.dumps(entry)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    from ..records import line_doc, records

    if records.enabled:
        records.put("rejections", line_doc(path.parent, line))
