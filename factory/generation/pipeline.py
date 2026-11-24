"""The automated task factory: ideas -> drafts -> gates -> repair -> task store.

``factory produce --count 5`` runs the whole loop on the pinned repository:

1. **Ideate** (ideas.py): the model reads a digest of the repo and proposes distinct feature ideas;
   the factory drops bug fixes, near-duplicates and ideas whose names already exist upstream.
2. **Draft** each idea: the model writes the requirements and the reference change as search/replace
   edits against the pinned commit (so it can change large files), then, in a separate request that
   never shows the reference change, the hidden tests from the requirements alone.
3. **Validate** with gates G0-G6 in fresh Docker sandboxes (baseline, patch applies, feature tests
   pass with / fail without the change, regressions, mutation testing, stability).
4. **Repair**: a rejected draft goes back to the model with the gate's verdict and test output; up
   to ``--repairs`` corrected versions are tried. Every attempt is recorded in ``generated_by``.
5. A valid task is written to the task store (pending human review unless ``--approve``).

When the model cannot produce enough valid tasks from the first ideas, it is asked for more
ideas (avoiding the ones already used or rejected), up to ``--max-ideas`` in total.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import FactoryError, ModelProviderError, PackageError
from ..models import RejectCode, RepoSpec, ValidationReport
from ..repo.loader import export_clean
from .author import EDIT_FORMAT, _ask, _relative, _test_files, _write_files, repo_context, solution_overlay
from .ideas import Idea, _is_test, ideate, load_ideas, save_ideas

log = logging.getLogger("factory.generation")

FEEDBACK_LIMIT = 6_000


# =========================================================================== prompts

def _test_examples(repo: Path, idea: Idea, limit: int = 7_000) -> dict[str, str]:
    """Existing test files about the same modules (style, fixtures, helpers), trimmed."""
    tests = [p.relative_to(repo).as_posix() for p in sorted(repo.rglob("*"))
             if p.is_file() and _is_test(p.relative_to(repo).as_posix())
             and ".git" not in p.relative_to(repo).parts]
    stems = [Path(f).stem.lower() for f in idea.files]
    picked = [t for t in tests if any(s and s in Path(t).stem.lower() for s in stems)][:2]
    conftests = [t for t in tests if t.endswith("conftest.py")][:1]
    if not picked:
        picked = [t for t in tests if Path(t).name.startswith(("test_", "test.")) or ".test." in t][:1]
    out = {}
    for t in conftests + picked:
        text = (repo / t).read_text(encoding="utf-8", errors="replace")
        out[t] = text[:limit] + ("\n# ... (truncated)" if len(text) > limit else "")
    return out


def _idea_text(idea: Idea) -> str:
    return json.dumps({"title": idea.title, "summary": idea.summary, "category": idea.category,
                       "kind": idea.kind, "behaviour": idea.behaviour, "files": idea.files,
                       "new_names": idea.new_names}, ensure_ascii=False)


def solution_prompt(idea: Idea, spec: RepoSpec, context: dict) -> str:
    return (
        f"You are a maintainer of the {spec.name} repository ({spec.language}). Implement this feature "
        "request as a benign, self-contained change, and write the task statement a developer would "
        "receive for it.\n"
        f"Feature: {_idea_text(idea)}\n"
        "Requirements (Markdown) must start with '# <title>' and fully specify the observable behaviour: "
        "the public names, parameters and defaults, return values, the exact error type and message "
        "text where callers can see them, and the edge cases. A developer must be able to implement it "
        "from the requirements alone, and hidden tests will check exactly what they say. Do not "
        "mention tests, the reference change, or file names other than the public API.\n"
        "The reference change must touch source files only (no tests, no build or test configuration, "
        "no new dependencies), keep every existing test passing, and follow the code style around it.\n"
        "Return only JSON shaped like "
        '{"title": "...", "requirements": "# ...", ' + EDIT_FORMAT[:-1] + "}.\n"
        f"Source (complete files; outlines with line numbers for files too large to show): "
        f"{json.dumps(context, ensure_ascii=False)}"
    )


def tests_prompt(idea: Idea, spec: RepoSpec, requirements: str, examples: dict, test_path: str,
                 context: dict) -> str:
    return (
        f"Write the hidden feature tests for this task on the {spec.name} repository, using the "
        "project's own test framework and conventions (see the example test files).\n"
        f"Requirements:\n{requirements}\n"
        "Rules: test only behaviour the requirements state; every test must FAIL on the current code "
        "(before the feature) and PASS once the requirements are implemented; cover the main rule, "
        "the boundaries and each error case, with exact expected values, so a subtly wrong "
        "implementation fails at least one test; deterministic, no network, no sleeping, no reliance "
        "on test order. Aim for 4 to 10 focused tests.\n"
        f"Put all tests in ONE new file at {test_path} (it must not replace an existing file).\n"
        "Return only JSON shaped like "
        f'{{"tests": {{"{test_path}": "<whole file>"}}}}.\n'
        f"Example tests: {json.dumps(examples, ensure_ascii=False)}\n"
        f"Source the tests run against (before the change): {json.dumps(context, ensure_ascii=False)}"
    )


def repair_prompt(idea: Idea, spec: RepoSpec, previous: dict, feedback: str, context: dict,
                  test_path: str) -> str:
    return (
        f"A task you drafted for the {spec.name} repository was rejected by automated validation. "
        "Fix it.\n"
        f"Feature: {_idea_text(idea)}\n"
        f"Your previous draft: {json.dumps(previous, ensure_ascii=False)}\n"
        f"Why it was rejected:\n{feedback}\n"
        "How validation works: the reference change (edits) is applied to the ORIGINAL source; the "
        "hidden tests must pass with it and fail without it; every existing test must still pass; "
        "small deliberate mistakes in the reference change (flipped comparisons, removed error "
        "branches, half-reverted code) must make at least one hidden test fail.\n"
        "Return the complete corrected draft as JSON shaped like "
        '{"title": "...", "requirements": "# ...", ' + EDIT_FORMAT[:-1]
        + f', "tests": {{"{test_path}": "<whole file>"}}}}. '
        "Edits are against the ORIGINAL source shown below, not your previous version. Keep the "
        "requirements and tests consistent with each other.\n"
        f"Original source: {json.dumps(context, ensure_ascii=False)}"
    )


# =========================================================================== feedback from gates

def _log_tail(path: str, limit: int = 2_500) -> str:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    exe = data.get("exec") if isinstance(data, dict) else None
    if not isinstance(exe, dict):
        return ""
    out = ((exe.get("stdout") or "") + "\n" + (exe.get("stderr") or "")).strip()
    return out[-limit:]


def gate_feedback(report: ValidationReport) -> str:
    lines = [f"Verdict: {report.reject_code.value if report.reject_code else 'rejected'}: {report.reject_reason}"]
    hints = {
        RejectCode.PATCH_APPLY_FAIL: "The reference change does not apply or does not build.",
        RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH: "Some hidden tests fail even with the reference change: "
                                                  "the tests or the change are wrong (see the output).",
        RejectCode.FEATURE_TESTS_PASS_WITHOUT_PATCH: "Some hidden tests already pass without the feature: "
                                                     "every test must exercise the new behaviour.",
        RejectCode.REGRESSION: "The reference change breaks existing tests; keep existing behaviour.",
        RejectCode.NEG_CONTROL_PASSED: "A deliberately broken version of the change still passes every "
                                       "hidden test: add tests for the boundaries and error branches.",
        RejectCode.FLAKY: "Tests gave different results on repeated runs: remove nondeterminism.",
        RejectCode.SOLUTION_TOUCHES_PROTECTED: "The reference change edits test or build files; "
                                               "change source files only.",
    }
    if report.reject_code in hints:
        lines.append(hints[report.reject_code])
    failed = [g for g in report.gates if not g.passed]
    for g in failed[-1:]:
        if g.details:
            lines.append(f"{g.gate}: {g.details}")
        for p in g.exec_log_paths[-1:]:
            tail = _log_tail(p)
            if tail:
                lines.append(f"Test output (end):\n{tail}")
    return "\n".join(lines)[-FEEDBACK_LIMIT:]


# =========================================================================== drafting

def _test_path(repo: Path, idea: Idea, language: str | None) -> str:
    """A new test file next to the existing ones, named after the idea."""
    slug = idea.id.replace("-", "_")[:40]
    tests_dir = next((d for d in ("tests", "test", "__tests__", "src/test") if (repo / d).is_dir()), "tests")
    if language in {"node", "javascript", "typescript"}:
        ext = ".ts" if any(repo.rglob("*.test.ts")) else ".js"
        name = f"{tests_dir}/{idea.id}.test{ext}"
    elif language == "cpp":
        name = f"{tests_dir}/test_{slug}.cpp"
    else:
        name = f"{tests_dir}/test_{slug}.py"
    n = 2
    base = name
    while (repo / name).exists():
        stem, dot, ext = base.rpartition(".")
        name, n = f"{stem}_{n}.{ext}", n + 1
    return name


MIN_REQUIREMENTS_CHARS = 250
_TEST_NAME = re.compile(r"\b(?:def|function)\s+(test_?\w+)|\b(?:it|test)\(\s*['\"]([^'\"]{6,})['\"]")


def check_requirements(requirements: str, tests: dict) -> None:
    """The instruction is the only file the solver sees: it must specify the behaviour and must not
    give the hidden tests away (their file names or test names)."""
    body = requirements.split("\n", 1)[-1].strip()
    if len(body) < MIN_REQUIREMENTS_CHARS:
        raise PackageError(f"the requirements are too short ({len(body)} characters) to specify the "
                           "behaviour; describe the API, rules, edge cases and errors")
    low = requirements.lower()
    for path, text in tests.items():
        name = path.rsplit("/", 1)[-1]
        if name.lower() in low:
            raise PackageError(f"the requirements name the hidden test file {name}; describe behaviour instead")
        for m in _TEST_NAME.finditer(text if isinstance(text, str) else ""):
            test_name = m.group(1) or m.group(2)
            if test_name and len(test_name) > 8 and test_name.lower() in low:
                raise PackageError(f"the requirements mention the hidden test {test_name!r}; "
                                   "describe behaviour instead")


def write_draft(drafts: Path, repo: Path, idea: Idea, data: dict, test_path: str, model: str,
                repairs: list[dict]) -> Path:
    """Turn a model's JSON (requirements, edits, tests) into an ordinary draft folder."""
    title, requirements = data.get("title"), data.get("requirements")
    if not all(isinstance(v, str) and v.strip() for v in (title, requirements)):
        raise PackageError("The model must provide a title and requirements")
    if not requirements.lstrip().startswith("# "):
        requirements = f"# {title.strip()}\n\n{requirements.lstrip()}"
    overlay = solution_overlay(data, repo)
    for path in overlay:
        if _is_test(path):
            raise PackageError(f"the reference change edits a test or build file: {path}")
    tests = _test_files(data) if "tests" in data else None
    if not isinstance(tests, dict) or not tests:
        raise PackageError("The model must return the hidden tests as a map of paths to file contents")
    check_requirements(requirements, tests)
    for path in tests:
        _relative(path)
        if (repo / path).exists():
            raise PackageError(f"hidden tests must go in a new file, not replace {path} (use {test_path})")
    dest = drafts / idea.id
    if dest.exists():
        import shutil
        shutil.rmtree(dest)
    _write_files(dest / "after", overlay, repo, '"edits" (the reference change)')
    _write_files(dest / "tests", tests, repo, '"tests" (the hidden tests)')
    (dest / "requirements.md").write_text(requirements, encoding="utf-8")
    (dest / "task.json").write_text(json.dumps({
        "title": title.strip(), "model": model, "kind": idea.kind,
        "tags": [idea.category], "idea_id": idea.id, "request": idea.summary or idea.title,
        "repair_attempts": repairs,
    }, indent=2), encoding="utf-8")
    (dest / "idea.json").write_text(json.dumps(idea.to_dict(), indent=2), encoding="utf-8")
    return dest


@dataclass
class Outcome:
    idea: Idea
    verdict: str  # valid | rejected | error
    task_id: str | None = None
    attempts: int = 0
    reasons: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {"idea": self.idea.id, "title": self.idea.title, "category": self.idea.category,
                "verdict": self.verdict, "task_id": self.task_id, "attempts": self.attempts,
                "reasons": self.reasons, "seconds": round(self.seconds, 1)}


def build_task(idea: Idea, spec: RepoSpec, reply, model: str, drafts: Path, validate, repairs: int = 2,
               on_step=None) -> Outcome:
    """Draft one idea and repair it until the gates pass or the attempts run out.

    ``validate(draft_dir) -> (report, task_id | None)`` runs the gates and stores a valid task.
    """
    start = time.monotonic()
    out = Outcome(idea, "error")
    step = on_step or (lambda message: None)
    with tempfile.TemporaryDirectory(prefix="factory-produce-") as tmp:
        repo = export_clean(spec, Path(tmp) / "repo")
        context = repo_context(repo, idea.files)
        test_path = _test_path(repo, idea, spec.language)
        data: dict | None = None
        history: list[dict] = []
        for attempt in range(repairs + 1):
            out.attempts = attempt + 1
            try:
                if data is None:
                    step("writing the requirements and the reference change")
                    data = _ask(reply, solution_prompt(idea, spec, context))
                    requirements = str(data.get("requirements") or "")
                    step("writing the hidden tests (from the requirements only)")
                    tests = _ask(reply, tests_prompt(idea, spec, requirements, _test_examples(repo, idea),
                                                     test_path, repo_context(repo, idea.files, budget=60_000)))
                    data["tests"] = _test_files(tests)
                draft = write_draft(drafts, repo, idea, data, test_path,
                                    model() if callable(model) else model, history)
                step(f"draft {attempt + 1}: running gates G0-G6 in Docker")
                report, task_id = validate(draft)
                if report.verdict == "valid":
                    out.verdict, out.task_id = "valid", task_id
                    break
                feedback = gate_feedback(report)
                code = report.reject_code.value if report.reject_code else "REJECTED"
            except ModelProviderError as exc:
                out.reasons.append(f"error: {exc}")
                break
            except PackageError as exc:
                feedback, code = f"The draft could not be built: {exc}", "DRAFT_ERROR"
            except FactoryError as exc:  # sandbox or model provider trouble: retrying the draft will not help
                out.reasons.append(f"error: {exc}")
                break
            first = feedback.splitlines()[0]
            first = first.split(": ", 2)[-1] if first.startswith("Verdict: ") else first
            out.reasons.append(f"attempt {attempt + 1}: {code}: {first[:300]}")
            out.verdict = "rejected"
            log.info("%s attempt %d rejected: %s", idea.id, attempt + 1, code)
            history.append({"strategy": "model_repair", "reject_code": code, "attempt": attempt + 1})
            if attempt == repairs:
                break
            previous = {k: data.get(k) for k in ("title", "requirements", "edits", "new_files", "after", "tests")
                        if data and data.get(k)}
            try:
                step(f"draft {attempt + 1} rejected ({code}); asking the model for a repair")
                data = _ask(reply, repair_prompt(idea, spec, previous, feedback, context, test_path))
                if "tests" not in data and previous.get("tests"):
                    data["tests"] = previous["tests"]
            except ModelProviderError as exc:
                out.reasons.append(f"error: {exc}")
                break
            except PackageError as exc:
                out.reasons.append(f"repair {attempt + 1}: {exc}")
                data = None  # start the idea over
            except FactoryError as exc:
                out.reasons.append(f"error: {exc}")
                break
    out.seconds = time.monotonic() - start
    return out


def produce(spec: RepoSpec, reply, model: str, workdir_drafts: Path, ideas_file: Path, validate,
            count: int = 5, repairs: int = 2, max_ideas: int = 12, on_outcome=None,
            done: set[str] | None = None, on_error=None, on_step=None, on_idea=None,
            rejected_before: dict[str, str] | None = None) -> list[Outcome]:
    """Run ideate -> draft -> validate -> repair until ``count`` tasks are valid or ideas run out.

    ``done`` holds idea ids that already have a task in the store; they are not built again but
    still count as "used", so new ideas stay different from them.
    """
    outcomes: list[Outcome] = []
    used = load_ideas(ideas_file)
    tried: set[str] = set(done or ())
    valid = 0
    # ideas the gates rejected in earlier runs: not built again, and new ideas must differ from them
    tried |= set(rejected_before or {})
    rejected_titles: list[str] = list((rejected_before or {}).values())
    ideation_failures = 0
    with tempfile.TemporaryDirectory(prefix="factory-ideas-") as tmp:
        repo = export_clean(spec, Path(tmp) / "repo")
        pending = [i for i in used if i.id not in tried]
        budget = len(tried) + max_ideas
        while valid < count and len(tried) < budget:
            if not pending:
                if on_step:
                    on_step(f"reading the repository and proposing {count - valid} distinct feature idea(s)")
                try:
                    fresh, dropped = ideate(repo, spec.language, reply, count=count - valid, existing=used,
                                            rejected=rejected_titles, upstream=Path(spec.local_path),
                                            base_commit=spec.commit_sha)
                except ModelProviderError as exc:
                    log.error("ideation stopped: %s", exc)
                    if on_error:
                        on_error(f"ideation: {exc}")
                    break
                except PackageError as exc:  # an unusable reply (not JSON): ask once more, then give up
                    ideation_failures += 1
                    log.warning("ideation reply unusable (%s)", exc)
                    if ideation_failures >= 2:
                        if on_error:
                            on_error(f"ideation: {exc}")
                        break
                    continue
                for d in dropped:
                    log.info("idea dropped: %s (%s)", d["title"], d["why"])
                if on_step and dropped:
                    on_step(f"kept {len(fresh)} idea(s), dropped {len(dropped)} "
                            "(bug fixes, duplicates, already upstream)")
                fresh = [i for i in fresh if i.id not in tried]
                if not fresh:
                    break
                used += fresh
                save_ideas(ideas_file, used)
                pending = fresh
            idea = pending.pop(0)
            if idea.id in tried:
                continue
            tried.add(idea.id)
            if on_idea:
                on_idea(idea, len(tried))
            outcome = build_task(idea, spec, reply, model, workdir_drafts, validate, repairs, on_step=on_step)
            outcomes.append(outcome)
            if on_outcome:
                on_outcome(outcome)
            if outcome.verdict == "valid":
                valid += 1
            else:
                rejected_titles.append(idea.title)
                if any(r.startswith("error:") for r in outcome.reasons):
                    break  # the model provider or sandbox failed; later ideas would fail the same way
    return outcomes
