"""Run a repo's tests inside a sandbox and return per-test results (section 9)."""

from __future__ import annotations

import json
import logging
import posixpath
import shlex
from pathlib import Path

from ..errors import ParseError
from ..models import EnvConfig, ExecResult, RunSummary
from ..sandbox.base import Sandbox
from .parsers import PARSERS, STDOUT_FORMATS

log = logging.getLogger("factory.runner")

NODE_REPORTER_PATH = ".factory/node-junit.mjs"
NODE_REPORTER = (Path(__file__).parent / "node_junit_reporter.mjs").read_text(encoding="utf-8")
NODE_TEST_COMMAND = (f"node --test --test-reporter=./{NODE_REPORTER_PATH} "
                     "--test-reporter-destination={report} {select}")

# Frameworks whose command line can select individual files; others run the
# full suite and the results are filtered afterwards.
_FILE_SELECT = {"pytest", "node", "jest"}


def _file_part(item: str) -> str:
    return item.split("::", 1)[0]


def render_select(env: EnvConfig, select: list[str] | None) -> str:
    """Turn test IDs or file paths into command-line selectors for the framework."""
    if not select:
        return "./..." if env.framework == "go" else ""
    if env.framework == "pytest":
        return " ".join(shlex.quote(s) for s in select)
    if env.framework in ("node", "jest"):
        files = dict.fromkeys(_file_part(s) for s in select)
        return " ".join(shlex.quote(f) for f in files)
    if env.framework == "go":
        dirs = dict.fromkeys("./" + (posixpath.dirname(_file_part(s)) or ".") for s in select
                             if _file_part(s).endswith(".go"))
        return " ".join(shlex.quote(d) for d in dirs) or "./..."
    return ""  # maven, gradle, cargo: run all, filter results


def build_command(env: EnvConfig, select: list[str] | None = None) -> str:
    cmd = env.test_command.replace("{report}", shlex.quote(env.report_path))
    return cmd.replace("{select}", render_select(env, select)).strip()


def _matches(test_id: str, select: list[str]) -> bool:
    for s in select:
        if test_id == s or test_id.startswith(s + "::") or _file_part(test_id) == s:
            return True
    return False


def _report_is_dir(env: EnvConfig) -> bool:
    return env.report_path.endswith("/") or "." not in posixpath.basename(env.report_path)


def _read_report(sb: Sandbox, env: EnvConfig, result: ExecResult) -> list[str]:
    if env.report_format in STDOUT_FORMATS:
        return [result.stdout] if result.stdout.strip() else []
    if _report_is_dir(env):
        ls = sb.exec(["sh", "-c", f"ls -1 {shlex.quote(env.report_path.rstrip('/'))}/*.xml 2>/dev/null"],
                     timeout_s=30)
        paths = [p for p in ls.stdout.splitlines() if p.strip()]
    else:
        paths = [env.report_path]
    texts = []
    for p in paths:
        try:
            texts.append(sb.read_file(p).decode("utf-8", errors="replace"))
        except FileNotFoundError:
            continue
    return texts


def run_tests(sb: Sandbox, env: EnvConfig, select: list[str] | None = None,
              log_to: Path | None = None) -> RunSummary:
    """Run the test command once. ``select`` = test IDs or test file paths (None = full suite).

    A non-zero exit with zero parsed tests is reported as a crash (``error``),
    never as all passed.
    """
    target = env.report_path.rstrip("/")
    sb.exec(["sh", "-c", f"rm -rf {shlex.quote(target)}; mkdir -p {shlex.quote(posixpath.dirname(target) or '.')}"],
            timeout_s=30)
    if NODE_REPORTER_PATH in env.test_command:
        sb.write_file(NODE_REPORTER_PATH, NODE_REPORTER)
    cmd = build_command(env, select)
    result = sb.exec(["sh", "-c", cmd], timeout_s=env.test_timeout_s, env=env.env_vars or None)
    texts = _read_report(sb, env, result)
    parser = PARSERS[env.report_format]
    roots = [getattr(sb, "repo_root", env.workdir), env.workdir]
    results = []
    parse_error = None
    for text in texts:
        try:
            results.extend(parser(text, roots))
        except ParseError as exc:
            parse_error = str(exc)
    if select and env.framework in _FILE_SELECT:
        results = [r for r in results if _matches(r.test_id, select)]
    error = None
    if result.timed_out:
        error = f"test command timed out after {env.test_timeout_s}s"
    elif not results and result.exit_code != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-2000:]
        error = f"exit {result.exit_code} with no parsed tests (build or collection failure): {tail}"
    elif not results and parse_error:
        error = parse_error
    elif not results and not texts:
        error = "test command produced no report"
    summary = RunSummary.build(results, result.exit_code, timed_out=result.timed_out, error=error)
    if log_to is not None:
        _write_log(log_to, cmd, result, texts, summary)
    log.debug("tests: %s", summary.counts, extra={"data": {"cmd": cmd, "counts": summary.counts,
                                                           "duration_s": result.duration_s}})
    return summary


def _write_log(path: Path, cmd: str, result: ExecResult, reports: list[str], summary: RunSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"command": cmd, "exec": result.model_dump(), "reports": reports, "summary": summary.model_dump()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def flaky_ids(summaries: list[RunSummary]) -> set[str]:
    """Tests whose status differs across runs (missing in a run counts as a different status)."""
    all_ids = set().union(*(s.status_of().keys() for s in summaries)) if summaries else set()
    flaky = set()
    for tid in all_ids:
        statuses = {s.status_of().get(tid, "missing") for s in summaries}
        if len(statuses) > 1:
            flaky.add(tid)
    return flaky


def run_repeated(sb: Sandbox, env: EnvConfig, n: int = 3,
                 select: list[str] | None = None) -> tuple[list[RunSummary], set[str]]:
    """Run the tests ``n`` times in one sandbox; return the runs and the flaky test IDs.

    Repeating inside one sandbox catches both random flakiness and tests that
    depend on state left behind by an earlier run (see docs/decisions.md D10).
    """
    runs = [run_tests(sb, env, select) for _ in range(n)]
    return runs, flaky_ids(runs)
