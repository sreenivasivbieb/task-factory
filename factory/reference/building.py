"""P8 build: Author, Test writer, Package writer, with the repair loop (v2.2 sections 6 and 10).

- The Author gets the approved proposal, the repo and the peer implementation diff (a design
  reference); never any tests. It writes instruction.md and the reference patch.
- The Test writer gets the instruction, the repo (before the change), the peer's tests, review
  edge cases and follow-up fixes; never our patch, never the peer implementation.
- The Package writer assembles an ordinary draft folder; the gates (G0-G6, in Docker) validate it
  and write the package to the task store; the Harbor export then gets exactly the five official
  parts, with provenance, reviews and benchmark data written beside it, outside the task folder.
- Repairs go back to the role that owns the failing part (patch -> Author, tests -> Test writer),
  so the independence rules hold during repair too.
- A similarity check compares our patch and tests with the peer's diff and tests; a high score is
  flagged, a near-copy is sent back for adaptation.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import FactoryError, ModelProviderError, PackageError
from ..generation.author import EDIT_FORMAT, _relative, _test_files, _write_files, repo_context, solution_overlay
from ..generation.ideas import _is_test
from ..generation.pipeline import check_requirements, gate_feedback
from ..models import RejectCode, RepoSpec, slugify
from ..repo.loader import export_clean
from .miner import for_author, for_test_writer
from .schema import PeerFeature, Similarity, TaskProposal
from .text import code_similarity

log = logging.getLogger("factory.reference.building")

AUTHOR_CODES = {RejectCode.PATCH_APPLY_FAIL, RejectCode.REGRESSION, RejectCode.SOLUTION_TOUCHES_PROTECTED}
TESTS_CODES = {RejectCode.FEATURE_TESTS_PASS_WITHOUT_PATCH, RejectCode.NEG_CONTROL_PASSED, RejectCode.FLAKY}


def task_id_for(spec: RepoSpec, p: TaskProposal) -> str:
    """Deterministic: same repo, commit and snapshot (so the same proposal) give the same task id."""
    return f"{slugify(spec.name, 24)}-{spec.commit_sha[:7]}-{slugify(p.title)}-{p.proposal_id[:6]}"


def test_path_for(repo: Path, p: TaskProposal, language: str | None) -> str:
    slug = slugify(p.title, 40).replace("-", "_")
    tests_dir = next((d for d in ("tests", "test", "__tests__", "src/test") if (repo / d).is_dir()), "tests")
    if language in {"node", "javascript", "typescript"}:
        ext = ".ts" if any(repo.rglob("*.test.ts")) else ".js"
        name = f"{tests_dir}/{slug.replace('_', '-')}.test{ext}"
    elif language == "cpp":
        name = f"{tests_dir}/test_{slug}.cpp"
    else:
        name = f"{tests_dir}/test_{slug}.py"
    n, base = 2, name
    while (repo / name).exists():
        stem, _, ext = base.rpartition(".")
        name, n = f"{stem}_{n}.{ext}", n + 1
    return name


def example_tests(repo: Path, files: list[str], limit: int = 7_000) -> dict[str, str]:
    tests = [p.relative_to(repo).as_posix() for p in sorted(repo.rglob("*"))
             if p.is_file() and ".git" not in p.relative_to(repo).parts and _is_test(p.relative_to(repo).as_posix())]
    stems = [Path(f).stem.lower() for f in files]
    picked = [t for t in tests if any(s and s in Path(t).stem.lower() for s in stems)][:2]
    picked = [t for t in tests if t.endswith("conftest.py")][:1] + (picked or [t for t in tests if "test" in Path(t).name][:1])
    out = {}
    for t in picked:
        text = (repo / t).read_text(encoding="utf-8", errors="replace")
        out[t] = text[:limit] + ("\n# ... (truncated)" if len(text) > limit else "")
    return out


def overlay_diff(repo: Path, overlay: dict[str, str]) -> str:
    """Unified diff of the overlay against the repo (only the added lines count for similarity)."""
    import difflib

    out = []
    for path, new in sorted(overlay.items()):
        old = (repo / path).read_text(encoding="utf-8", errors="replace") if (repo / path).is_file() else ""
        out += difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="")
    return "\n".join(out) + "\n"


def _proposal_text(p: TaskProposal) -> str:
    return json.dumps({"title": p.title, "kind": p.kind, "summary": p.summary, "behaviour": p.behaviour_spec,
                       "expected_files": p.expected_files, "behaviours_to_cover": p.test_plan}, ensure_ascii=False)


# --------------------------------------------------------------------------- prompts


def author_prompt(p: TaskProposal, spec: RepoSpec, context: dict, reference: dict, feedback: str = "",
                  previous: dict | None = None, source: str = "rejected by automated validation") -> str:
    out = (
        f"You are the Author in a coding-task factory, working as a maintainer of {spec.name} "
        f"({spec.language}). Implement the approved proposal below as a benign, self-contained change and "
        "write the task statement (instruction.md) a developer will receive.\n"
        f"Proposal: {_proposal_text(p)}\n"
        "The instruction (Markdown) starts with '# <title>' and fully specifies the observable behaviour: "
        "public names, parameters and defaults, return values, exact error types and message text callers "
        "can see, and the edge cases. For EVERY new public name state exactly how callers reach it (for example "
        "`pkg.name` or `from pkg.module import name`) and make the change provide it exactly there (export it "
        "where the package exports its other public names). A developer must be able to implement it from the "
        "instruction alone; "
        "hidden tests will check exactly what it says. Do not mention tests or the reference change.\n"
        "The reference change touches source files only (no tests, no build or test configuration, no new "
        "dependencies), keeps every existing test passing and follows the code around it. The peer "
        "implementation below is a DESIGN REFERENCE from another project: adapt the approach to this "
        "repository's API and structure, never copy it.\n"
        "Return only JSON shaped like "
        '{"title": "...", "requirements": "# ...", ' + EDIT_FORMAT[:-1] + "}.\n"
        f"Peer reference: {json.dumps(reference, ensure_ascii=False)}\n"
    )
    if feedback:
        out += (f"Your previous version was {source}:\n{feedback}\n"
                f"Previous version: {json.dumps(previous or {}, ensure_ascii=False)}\n"
                "Return the complete corrected version; edits apply to the ORIGINAL source below.\n")
    return out + f"Source (complete files; outlines for large files): {json.dumps(context, ensure_ascii=False)}"


def tests_prompt(spec: RepoSpec, requirements: str, test_path: str, examples: dict, reference: dict, context: dict,
                 feedback: str = "", previous: dict | None = None, source: str = "rejected by automated validation") -> str:
    out = (
        f"You are the Test writer in a coding-task factory. Write the hidden feature tests for this task on "
        f"{spec.name}, with the project's own test framework and conventions (see the example tests).\n"
        f"Instruction:\n{requirements}\n"
        "Rules: test only behaviour the instruction states; reach every new name exactly the way the instruction "
        "writes it (same module path or import), never a path it does not give; use the existing API correctly "
        "(read the code when unsure: a test must not fail because of how it calls existing functions); every "
        "test must FAIL on the current code and PASS "
        "once the instruction is implemented; cover the main rule, the boundaries and each error case with "
        "exact expected values, so a subtly wrong implementation fails at least one test; deterministic, no "
        "network, no sleeping, no reliance on test order. Aim for 5 to 12 focused tests.\n"
        "The peer project's tests, review edge cases and follow-up fixes below show what its maintainers "
        "checked: translate the relevant behaviours to THIS project's API and test framework (never copy "
        "them, never test behaviour the instruction does not state).\n"
        f"Put all tests in ONE new file at {test_path}.\n"
        f'Return only JSON shaped like {{"tests": {{"{test_path}": "<whole file>"}}}}.\n'
        f"Peer reference: {json.dumps(reference, ensure_ascii=False)[:24_000]}\n"
        f"Example tests: {json.dumps(examples, ensure_ascii=False)}\n"
    )
    if feedback:
        out += (f"Your previous tests were {source}:\n{feedback}\n"
                f"Previous tests: {json.dumps(previous or {}, ensure_ascii=False)}\n"
                "Go through every failing test: compare what it asserts with the instruction. Fix a test that "
                "asks for a name, import path, value or message the instruction does not state, or that calls the "
                "existing API wrongly; keep the tests that are right. Return the complete corrected test file.\n")
    return out + f"Source the tests run against (before the change): {json.dumps(context, ensure_ascii=False)}"


REVIEW_SHAPE = ('{"score": 0, "verdict": "ok | fix", "problems": ["a concrete problem, naming the rule, name or line"], '
                '"fixes": ["what exactly to change"]}')


def review_draft_prompt(p: TaskProposal, spec: RepoSpec, sol: dict, context: dict) -> str:
    """Another model reviews an author's draft (instruction + reference change); it never sees tests."""
    draft = {k: sol.get(k) for k in ("title", "requirements", "edits", "new_files")}
    return (
        f"You review a draft in a coding-task factory on {spec.name} ({spec.language}): the task statement a "
        "developer will receive (requirements) and the reference change (search/replace edits) that implements "
        "it. Hidden tests will be written from the statement alone, so it must be exact. Check: (1) the change "
        "implements every rule the statement gives, with the same names, defaults, error types and messages; "
        "(2) the statement gives every new public name with its exact import path, exact messages, defaults and "
        "edge cases, so tests can check it unambiguously; (3) the change is complete (exports, registrations, "
        "every call site) and keeps existing behaviour; (4) the statement does not mention tests or reveal the "
        "implementation. Score 0-10 (10 = ready as is). Return only JSON shaped like " + REVIEW_SHAPE + ".\n"
        f"Proposal: {_proposal_text(p)}\nDraft: {json.dumps(draft, ensure_ascii=False)}\n"
        f"Source (before the change): {json.dumps(context, ensure_ascii=False)}")


def review_tests_prompt(spec: RepoSpec, requirements: str, tests: dict, context: dict) -> str:
    """Another model reviews hidden tests against the statement; it never sees the reference change."""
    return (
        f"You review hidden tests in a coding-task factory on {spec.name} ({spec.language}). You get the task "
        "statement and the tests, never the implementation. Check: (1) every test checks only what the statement "
        "says, reaching names exactly as it writes them (module path, import, parameters) and expecting exactly "
        "its values and messages; (2) tests call the existing API correctly (see the source); (3) each test "
        "fails on the current code and passes with any correct implementation of the statement; (4) "
        "deterministic, no network or timing; (5) the rules, boundaries and error cases are covered. Score 0-10 "
        "(10 = ready as is). Return only JSON shaped like " + REVIEW_SHAPE + ".\n"
        f"Statement:\n{requirements}\nTests: {json.dumps(tests, ensure_ascii=False)}\n"
        f"Source (before the change): {json.dumps(context, ensure_ascii=False)}")


def _review(agent, prompt: str) -> dict:
    try:
        r = agent.ask_json(prompt)
    except ModelProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unusable review counts as no opinion
        return {"model": agent.model, "score": None, "problems": [f"no usable review ({str(exc)[:80]})"]}
    try:
        score = max(0.0, min(10.0, float(r.get("score"))))
    except (TypeError, ValueError):
        score = None
    return {"model": agent.model, "score": score, "verdict": str(r.get("verdict") or ""),
            "problems": [str(x) for x in r.get("problems") or []][:8], "fixes": [str(x) for x in r.get("fixes") or []][:8]}


def _review_text(reviews: list[dict]) -> str:
    rows = []
    for r in reviews:
        if r.get("problems") or r.get("fixes"):
            rows.append(f"- reviewer ({r.get('score')}/10): problems: {'; '.join(r.get('problems') or [])}"
                        f"; fixes: {'; '.join(r.get('fixes') or [])}")
    return "\n".join(rows)


def _mean(xs: list) -> float:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else 0.0


# --------------------------------------------------------------------------- build


@dataclass
class BuildResult:
    verdict: str  # validated | dropped | error
    task_id: str | None = None
    attempts: list[dict] = field(default_factory=list)
    similarity: Similarity | None = None
    reason: str = ""
    panel: list[dict] = field(default_factory=list)  # multi-model drafts, reviews and the choice, per stage


def _owner(code: str, message: str, attempt: int) -> str:
    """Who repairs a failure that names its owner; FEATURE_TESTS_FAIL_WITH_PATCH goes through ``route``."""
    if code in {c.value for c in AUTHOR_CODES}:
        return "author"
    if code in {c.value for c in TESTS_CODES}:
        return "tests"
    if code == "DRAFT_ERROR":
        low = message.lower()
        return "tests" if ("hidden test" in low and "requirements" not in low) or "tests must" in low else "author"
    if code == "NEAR_COPY_PATCH":
        return "author"
    if code == "NEAR_COPY_TESTS":
        return "tests"
    return "tests" if attempt % 2 == 0 else "author"


# A name the tests use that does not exist once the reference change is in.
_MISSING = (
    re.compile(r"AttributeError: module '([\w.]+)' has no attribute '(\w+)'"),
    re.compile(r"AttributeError: (?:type object |')?'?([\w.]+)'? (?:object )?has no attribute '(\w+)'"),
    re.compile(r"ImportError: cannot import name '(\w+)' from '([\w.]+)'"),
    re.compile(r"NameError: name '(\w+)' is not defined"),
    re.compile(r"TypeError: .*got an unexpected keyword argument '(\w+)'"),
    re.compile(r"E\s+AttributeError: (\w+)\s*$", re.M),  # a module __getattr__ that raises AttributeError(name)
)
# Failures in the test file itself, not in the behaviour: always the test writer's to fix.
_TEST_SIDE = re.compile(r"^ERROR (?:collecting )?\S+|fixture '\w+' not found|SyntaxError|IndentationError|"
                        r"ModuleNotFoundError: No module named '(?:test|conftest)", re.M)


def missing_names(output: str) -> list[tuple[str, str, str]]:
    """(name, how the tests reached it, kind) for every name the tests used that does not exist; kind is
    "member" for an attribute of an object or class (``'Choice' object has no attribute 'x'``), else "path"."""
    found: list[tuple[str, str, str]] = []
    for i, rx in enumerate(_MISSING):
        for m in rx.finditer(output):
            groups = m.groups()
            kind = "member" if i == 1 else "path"
            if rx.pattern.startswith("ImportError"):
                name, where = groups[0], f"from {groups[1]} import {groups[0]}"
            elif len(groups) == 2:
                name, where = groups[1], f"{groups[0]}.{groups[1]}"
            else:
                name, where = groups[0], groups[0]
                kind = "member" if rx.pattern.startswith("TypeError") else kind  # an unknown keyword argument
            if where == name and kind == "path":  # how the test reached it: `pkg.name` on a line of the test
                used = re.search(rf"([A-Za-z_][\w.]*)\.{re.escape(name)}\b", output)
                where = f"{used.group(1)}.{name}" if used else name
            if name not in {n for n, _, _ in found}:
                found.append((name, where, kind))
    return found


_RECEIVERS = {"self", "cls", "this", "ctx", "obj", "param", "result", "value", "instance"}


def _stated_path(requirements: str, name: str) -> str:
    """How the instruction says callers IMPORT ``name`` (``pkg.name`` rooted at a module, or ``from x import
    name``), or ''. ``self.name`` or ``Choice.name`` describe a member, not an import path."""
    for m in re.finditer(rf"(from\s+[\w.]+\s+import\s+[^\n`]*\b{re.escape(name)}\b|"
                         rf"\b([A-Za-z_]\w*)(?:\.\w+)*\.{re.escape(name)}\b)", requirements):
        root = m.group(2)
        if root is None or (root not in _RECEIVERS and root[:1].islower()):
            return m.group(1)
    return ""


def route(code: str, feedback: str, attempt: int, requirements: str, history: list[dict]) -> tuple[str, str]:
    """(owner, diagnosis) for a gate failure. FEATURE_TESTS_FAIL_WITH_PATCH is decided on the evidence:
    a name the instruction never promised, an error inside the test file, or a first failure goes to the
    test writer with the gate's reason; a promised name the change does not provide goes to the author;
    the same tests failing again after the test writer's repair go to the author."""
    if code != RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH.value:
        return _owner(code, feedback, attempt), ""
    for name, where, kind in missing_names(feedback):
        if name not in requirements:
            return "tests", (f"`{where}` does not exist and the instruction never mentions `{name}`: test only the "
                             "names and behaviour the instruction states.")
        if kind == "member":  # the instruction promises this attribute / parameter; the change lacks it
            return "author", (f"the instruction promises `{name}`, but with your change `{where}` does not exist: "
                              "provide it exactly as the instruction describes.")
        if where in requirements:
            return "author", (f"the instruction promises `{where}`, but with your change it does not exist: make "
                              f"`{where}` work exactly as the instruction writes it (define and export it).")
        stated = _stated_path(requirements, name)
        if stated:
            return "tests", (f"the tests reach `{name}` as `{where}`, which the instruction does not offer; use "
                             f"`{stated}` as the instruction states.")
        return "author", (f"the instruction never says how callers reach `{name}` (the tests tried `{where}`): "
                          "state the exact import path of every new public name in the instruction, and make it "
                          "work.")
    if _TEST_SIDE.search(feedback):
        return "tests", "the test file itself errors (collection, fixture or syntax): fix the test code."
    failing = _failing_tests(feedback)
    prev = next((a for a in reversed(history) if a.get("code") == code), None)
    if prev and prev.get("role") == "tests" and failing and failing == set(prev.get("failing") or []):
        return "author", ("the same tests still fail after the test writer checked them against the instruction: "
                          "the change does not do what the instruction says.")
    if prev is None or prev.get("role") == "author":
        return "tests", ("these tests fail with the reference change: check each against the instruction; fix a test "
                         "that asks for more or other than the instruction states, keep the ones that are right.")
    return "author", "these tests still fail with the reference change: make the change do what the instruction states."


def _failing_tests(output: str) -> set[str]:
    names = set(re.findall(r"^FAILED (\S+)", output, re.M))
    if not names:
        m = re.search(r"feature tests fail with the reference patch: ([^\n]+)", output)
        names = {s.strip() for s in m.group(1).split(", ")} if m else set()
    return names


def tests_feedback(report, test_paths: list[str], limit: int = 9_000) -> str:
    """The gate's reason plus the failing tests' own frames and error lines, for the test writer.

    Frames inside the library are cut (their lines would show the reference change, which the test
    writer never sees); the error lines are kept: they state what the code did."""
    from ..generation.pipeline import _log_tail

    head = gate_feedback(report).partition("\nTest output (end):")[0]
    failed = [g for g in report.gates if not g.passed]
    out = _log_tail(failed[-1].exec_log_paths[-1], limit=40_000) if failed and failed[-1].exec_log_paths else ""
    if not out:
        return head
    keep: list[str] = []
    chunk: list[str] = []
    sep = re.compile(r"^(?:_{3,} .+ _{3,}|(?:_ ){3,}_?|={3,} .+ ={3,})\s*$")

    def flush() -> None:
        locs = [m.group(1) for ln in chunk if (m := re.match(r"^(\S+?):\d+: ", ln))]
        own = not locs or any(loc.endswith(tp) or tp.endswith(loc) for loc in locs[-1:] for tp in test_paths)
        keep.extend(chunk if own else ["    (a frame inside the library: hidden)"] +
                    [ln for ln in chunk if ln.startswith("E ")][:6])
        chunk.clear()

    for ln in out.splitlines():
        if sep.match(ln):
            flush()
            keep.append(ln)
        else:
            chunk.append(ln)
    flush()
    body = "\n".join(keep)
    return f"{head}\nTest output (your tests' frames and the errors):\n{body[-limit:]}"


def edit_hints(repo: Path, sol: dict | None, language: str | None = None, limit: int = 3_000) -> str:
    """For each search/replace edit whose search text is not in its file: where that text really is, or
    the closest real lines of the file, so a repair can copy them exactly (models repeat the same miss)."""
    import difflib

    from ..generation.ideas import source_files

    if not sol or not isinstance(sol.get("edits"), list):
        return ""
    hints = []
    sources = None
    for i, e in enumerate(sol["edits"], 1):
        if not isinstance(e, dict) or not isinstance(e.get("search"), str) or not isinstance(e.get("path"), str):
            continue
        target = repo / e["path"]
        text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        if e["search"] and e["search"] in text:
            continue
        first = next((ln.strip() for ln in e["search"].splitlines() if ln.strip()), "")
        if not first:
            continue
        if sources is None:
            sources = source_files(repo, language)
        elsewhere = [f for f in sources if f != e["path"]
                     and first in (repo / f).read_text(encoding="utf-8", errors="replace")]
        if not text:
            hints.append(f"edit {i}: {e['path']} does not exist" + (f"; that code is in {', '.join(elsewhere[:3])}"
                                                                    if elsewhere else ""))
            continue
        if elsewhere:
            hints.append(f"edit {i}: {first[:80]!r} is not in {e['path']}; it is in {', '.join(elsewhere[:3])}: "
                         f"put the edit on that path")
            continue
        lines = text.splitlines()
        near = difflib.get_close_matches(first, [ln.strip() for ln in lines], n=1, cutoff=0.5)
        if near:
            at = next(n for n, ln in enumerate(lines) if ln.strip() == near[0])
            snippet = "\n".join(f"{n + 1}: {lines[n]}" for n in range(max(0, at - 3), min(len(lines), at + 8)))
            hints.append(f"edit {i}: the search text is not in {e['path']} exactly as written; the closest real "
                         f"lines are (copy them exactly, without the line numbers):\n{snippet}")
    return "\n".join(hints)[:limit]


def _author_feedback(feedback: str, code: str) -> str:
    """The author never sees the hidden tests' CODE. Output of existing tests is kept whole; for hidden
    tests only the failing test names and their assertion messages are kept: they state the behaviour
    that is missing ("alias not shown in help"), which the instruction already promises."""
    if code in {RejectCode.REGRESSION.value, RejectCode.PATCH_APPLY_FAIL.value}:
        return feedback
    head, _, output = feedback.partition("\nTest output (end):")
    keep = [ln.strip() for ln in output.splitlines()
            if ln.startswith(("FAILED ", "E ")) and not re.search(r"^E\s+(def |assert .*==.*\(|\s*>)", ln)][:20]
    return head + ("\nFailing behaviours (test names and assertion messages, no test code):\n" + "\n".join(keep)
                   if keep else "")


class Builder:
    """Author + test writer + package writer. With several author / test-writer models it builds as a
    panel: every model drafts at once, the other models review each draft, the best-reviewed draft is
    revised with its reviews, and that one package goes to the gates (user choice 2026-11-26)."""

    def __init__(self, author, test_writer, spec: RepoSpec, drafts: Path, validate, repairs: int = 2,
                 flag_similarity: float = 0.5, max_similarity: float = 0.8, make_tools=None,
                 authors: list | None = None, test_writers: list | None = None, workers: int = 1) -> None:
        self.make_tools = make_tools  # repo -> Toolbox: the author reads the exact lines before editing them
        self.authors = list(authors or [author])
        self.test_writers = list(test_writers or [test_writer])
        self.author = self.authors[0]  # the model that repairs (the panel's chosen one once it has run)
        self.test_writer = self.test_writers[0]
        self.workers = max(1, workers)
        self.spec = spec
        self.drafts = Path(drafts)
        self.validate = validate  # draft_dir -> (ValidationReport, task_id | None)
        self.repairs = repairs
        self.flag_similarity = flag_similarity
        self.max_similarity = max_similarity

    def _author(self, prompt: str, tools, agent=None) -> dict:
        """The author's reply; with tools it first reads the code it will edit (search texts must match)."""
        agent = agent or self.author
        if tools is None:
            return agent.ask_json(prompt)
        from .research import run_agent

        run = run_agent(
            agent,
            "You are the Author. Before writing any edit, use repo_search and repo_read to see the EXACT current "
            "lines you will change; every search text must be copied verbatim from repo_read output (without the "
            "line numbers, same indentation). Large files are only outlined below, so read them.",
            prompt, tools, ["repo_search", "repo_read"],
            '{"title": "...", "requirements": "# ...", ' + EDIT_FORMAT[:-1] + "}", max_steps=12)
        if not isinstance(run.final, dict):
            raise PackageError("the author gave no final answer")
        return run.final

    def _tests(self, prompt: str, tools, test_path: str | None = None, agent=None) -> dict | None:
        """The test writer's files; with tools it reads the existing code (before the change) it calls.
        One file put elsewhere (models like to extend an existing test file) is moved to ``test_path``."""
        files = self._tests_raw(prompt, tools, agent or self.test_writer)
        if test_path and isinstance(files, dict) and len(files) == 1 and test_path not in files:
            files = {test_path: next(iter(files.values()))}
        return files

    def _tests_raw(self, prompt: str, tools, agent) -> dict | None:
        if tools is None:
            return _test_files(agent.ask_json(prompt))
        from .research import run_agent

        run = run_agent(
            agent,
            "You are the Test writer. Use repo_search and repo_read to check how the existing API is called "
            "(fixtures, runners, helpers, warnings) before you write a test that calls it. The repository is "
            "the code BEFORE the change.",
            prompt, tools, ["repo_search", "repo_read", "repo_tree"], '{"tests": {"<path>": "<whole file>"}}',
            max_steps=8)
        return _test_files(run.final) if isinstance(run.final, dict) else None

    # ------------------------------------------------------------------ the multi-model panel

    def _panel(self, kind: str, agents: list, make, check, review_prompt, revise, step) -> tuple:
        """Every agent drafts at once; each usable draft is reviewed by the OTHER agents; the best mean
        score wins (ties: agent order) and its author revises it once with the reviews. Returns
        (chosen agent, draft, record). A model that fails (quota, unusable reply) drops out."""
        from .parallel import pmap

        step(f"{kind} panel: {len(agents)} models draft in parallel ({', '.join(a.model for a in agents)})")

        def draft(agent):
            try:
                out = make(agent)
                check(out)
                return {"agent": agent, "draft": out, "error": ""}
            except ModelProviderError as exc:
                return {"agent": agent, "draft": None, "error": f"model: {str(exc)[:160]}"}
            except (PackageError, ValueError, TypeError, KeyError) as exc:
                return {"agent": agent, "draft": locals().get("out"), "error": str(exc)[:300]}

        drafts = pmap(draft, agents, self.workers)
        ok = [d for d in drafts if not d["error"]]
        record = {"stage": kind, "drafts": [{"model": d["agent"].model, "error": d["error"]} for d in drafts]}
        if not ok:
            first = next((d for d in drafts if d["draft"] is not None), drafts[0])
            if all(d["error"].startswith("model:") for d in drafts):
                raise ModelProviderError(f"every {kind} model failed: " + "; ".join(d["error"] for d in drafts))
            raise _PanelFailed(first["agent"], first["draft"], first["error"], record)
        if len(ok) > 1:
            step(f"{kind} panel: cross-review, each of {len(ok)} drafts by the other models")
            jobs = [(d, r) for d in ok for r in agents if r is not d["agent"]]
            reviews = pmap(lambda dr: _review(dr[1], review_prompt(dr[0]["draft"])), jobs, self.workers)
            for d in ok:
                d["reviews"] = [rv for (dd, _), rv in zip(jobs, reviews) if dd is d]
                d["score"] = round(_mean([rv["score"] for rv in d["reviews"]]), 2)
            best = max(ok, key=lambda d: (d["score"], -agents.index(d["agent"])))
        else:
            best = ok[0]
            best.setdefault("reviews", [])
            best.setdefault("score", None)
        for row, d in zip(record["drafts"], drafts):
            row.update({"score": d.get("score"), "reviews": d.get("reviews", [])})
        record["chosen"] = best["agent"].model
        chosen = best["draft"]
        notes = _review_text(best["reviews"])
        if notes and any(r.get("verdict") != "ok" for r in best["reviews"]):
            step(f"{kind} panel: {best['agent'].model} revises its draft with the reviews")
            try:
                revised = revise(best["agent"], chosen, notes)
                check(revised)
                chosen, record["revised"] = revised, True
            except (PackageError, ValueError, TypeError, KeyError) as exc:
                record["revised"] = f"kept the reviewed draft: the revision failed ({str(exc)[:120]})"
            except ModelProviderError as exc:
                record["revised"] = f"kept the reviewed draft: {str(exc)[:120]}"
        return best["agent"], chosen, record

    def _write(self, repo: Path, p: TaskProposal, task_id: str, sol: dict, tests: dict, attempts: list[dict]) -> Path:
        title, requirements = sol.get("title") or p.title, sol.get("requirements")
        if not isinstance(requirements, str) or not requirements.strip():
            raise PackageError("the author must provide the instruction (requirements)")
        if not requirements.lstrip().startswith("# "):
            requirements = f"# {str(title).strip()}\n\n{requirements.lstrip()}"
        overlay = solution_overlay(sol, repo)
        for path in overlay:
            if _is_test(path):
                raise PackageError(f"the reference change edits a test or build file: {path}")
        if not isinstance(tests, dict) or not tests:
            raise PackageError("the hidden tests must be a map of paths to file contents")
        check_requirements(requirements, tests)
        for path, text in tests.items():
            _relative(path)
            if path.endswith(".py"):  # a syntax error needs no Docker run to be found
                try:
                    compile(text, path, "exec")
                except SyntaxError as exc:
                    raise PackageError(f"the hidden test {path} does not compile: {exc.msg} (line {exc.lineno})") from exc
            if (repo / path).exists():
                raise PackageError(f"hidden tests must go in a new file, not replace {path}")
        dest = self.drafts / p.proposal_id
        if dest.exists():
            shutil.rmtree(dest)
        _write_files(dest / "after", overlay, repo, '"edits" (the reference change)')
        _write_files(dest / "tests", tests, repo, '"tests" (the hidden tests)')
        (dest / "requirements.md").write_text(requirements, encoding="utf-8")
        (dest / "task.json").write_text(json.dumps({
            "task_id": task_id, "title": str(title).strip(), "kind": p.kind, "tags": [t for t in p.tags if not t.startswith("new_name:")],
            "idea_id": p.proposal_id, "model": f"author:{self.author.model} tests:{self.test_writer.model}",
            "request": p.summary or p.title, "repair_attempts": [
                {"strategy": a["role"] + "_repair", "reject_code": a["code"], "attempt": a["attempt"]} for a in attempts],
        }, indent=2), encoding="utf-8")
        return dest

    def similarity(self, sol_patch_text: str, tests: dict, feature: PeerFeature | None) -> Similarity:
        if feature is None:
            return Similarity()
        b = feature.reference_bundle
        sp = code_similarity(sol_patch_text, b.impl_diff)
        st = code_similarity("\n".join(tests.values()), b.tests)
        return Similarity(patch=round(sp, 3), tests=round(st, 3),
                          flagged=max(sp, st) >= self.flag_similarity)

    def build(self, p: TaskProposal, feature: PeerFeature | None, on_step=None) -> BuildResult:
        step = on_step or (lambda m: None)
        task_id = task_id_for(self.spec, p)
        res = BuildResult("error", task_id=task_id)
        a_ref = for_author(feature.reference_bundle) if feature else {}
        t_ref = for_test_writer(feature.reference_bundle) if feature else {}
        with tempfile.TemporaryDirectory(prefix="factory-ref-build-") as tmp:
            repo = export_clean(self.spec, Path(tmp) / "repo")
            files = p.expected_files
            tools = self.make_tools(repo) if self.make_tools else None
            # with tools the author reads exact lines itself; the prompt only needs an overview
            context = repo_context(repo, files, budget=30_000 if tools else 80_000)
            test_ctx = repo_context(repo, files, budget=60_000)
            test_path = test_path_for(repo, p, self.spec.language)
            examples = example_tests(repo, files)
            sol: dict | None = None
            tests: dict | None = None
            redo = "both"
            feedback = ""
            last_report = None
            author_agent, tests_agent = self.authors[0], self.test_writers[0]  # the panel's choice repairs
            keys = ("title", "requirements", "edits", "new_files")

            def check_sol(d: dict) -> None:
                if not isinstance(d, dict) or not str(d.get("requirements") or "").strip():
                    raise PackageError("the author must provide the instruction (requirements)")
                solution_overlay(d, repo)  # every edit must apply

            def check_tests(ts) -> None:
                if not isinstance(ts, dict) or not ts:
                    raise PackageError("no hidden tests")
                for path, text in ts.items():
                    if str(path).endswith(".py"):
                        try:
                            compile(str(text), str(path), "exec")
                        except SyntaxError as exc:
                            raise PackageError(f"the hidden test {path} does not compile: {exc.msg}") from exc

            for attempt in range(self.repairs + 1):
                t = {}  # this attempt's timings (seconds)
                panel_mark = len(res.panel)
                try:
                    if redo in {"both", "author"}:
                        t0 = time.monotonic()
                        if redo == "both" and len(self.authors) > 1:
                            def make_sol(agent):
                                return self._author(author_prompt(p, self.spec, context, a_ref), tools, agent)

                            def revise_sol(agent, d, notes):
                                return self._author(author_prompt(
                                    p, self.spec, context, a_ref, notes, {k: d.get(k) for k in keys},
                                    source="reviewed by other models; fix every problem they found that is real"),
                                    tools, agent)
                            try:
                                author_agent, sol, rec = self._panel(
                                    "author", self.authors, make_sol, check_sol,
                                    lambda d: review_draft_prompt(p, self.spec, d, context), revise_sol, step)
                                res.panel.append(rec)
                            except _PanelFailed as exc:
                                author_agent, sol = exc.agent, exc.draft
                                res.panel.append(exc.record)
                                raise PackageError(exc.error) from None
                        else:
                            step(f"author: {'instruction + patch' if attempt == 0 else 'repair'} (attempt {attempt + 1})")
                            prev = {k: sol.get(k) for k in keys} if sol else None
                            prompt = author_prompt(p, self.spec, context, a_ref, feedback if redo == "author" else "",
                                                   prev)
                            sol = self._author(prompt, tools, author_agent)
                        t["author_s"] = round(time.monotonic() - t0, 1)
                    if redo in {"both", "tests"} or tests is None:
                        t0 = time.monotonic()
                        req = str(sol.get("requirements") or "")
                        if (redo == "both" or tests is None) and len(self.test_writers) > 1:
                            def make_tests(agent):
                                return self._tests(tests_prompt(self.spec, req, test_path, examples, t_ref, test_ctx),
                                                   tools, test_path, agent)

                            def revise_tests(agent, ts, notes):
                                return self._tests(tests_prompt(
                                    self.spec, req, test_path, examples, t_ref, test_ctx, notes, {"tests": ts},
                                    source="reviewed by other models (they never saw the implementation); fix "
                                           "every problem they found that is real"), tools, test_path, agent)
                            try:
                                tests_agent, tests, rec = self._panel(
                                    "tests", self.test_writers, make_tests, check_tests,
                                    lambda ts: review_tests_prompt(self.spec, req, ts, test_ctx), revise_tests, step)
                                res.panel.append(rec)
                            except _PanelFailed as exc:
                                tests_agent, tests = exc.agent, exc.draft
                                res.panel.append(exc.record)
                                raise PackageError(f"the hidden tests: {exc.error}") from None
                        else:
                            step(f"test writer: {'tests from the instruction' if attempt == 0 or redo == 'both' else 'repair'}")
                            tests = self._tests(tests_prompt(
                                self.spec, req, test_path, examples, t_ref, test_ctx,
                                feedback if redo == "tests" else "", {"tests": tests} if redo == "tests" else None),
                                tools, test_path, tests_agent)
                        t["tests_s"] = round(time.monotonic() - t0, 1)
                    overlay = solution_overlay(sol, repo)
                    sim = self.similarity(overlay_diff(repo, overlay), tests or {}, feature)
                    res.similarity = sim
                    if sim.patch >= self.max_similarity:
                        raise _NearCopy("NEAR_COPY_PATCH", f"the patch is a near-copy of the peer implementation "
                                        f"({sim.patch:.2f}); adapt the design to this repository instead")
                    if sim.tests >= self.max_similarity:
                        raise _NearCopy("NEAR_COPY_TESTS", f"the tests are a near-copy of the peer tests "
                                        f"({sim.tests:.2f}); translate behaviours to this project's API")
                    draft = self._write(repo, p, task_id, sol, tests, res.attempts)
                    step(f"gates G0-G6 in Docker (attempt {attempt + 1})")
                    t0 = time.monotonic()
                    report, stored = self.validate(draft)
                    t["gates_s"] = round(time.monotonic() - t0, 1)
                    t["gate_s"] = {g.gate: round(g.duration_s, 1) for g in report.gates}
                    if report.verdict == "valid":
                        res.verdict, res.task_id = "validated", stored or task_id
                        res.attempts.append({"attempt": attempt + 1, "code": "VALID", "role": "-",
                                             "mutation_score": report.mutation_score, "timings": t,
                                             "models": {"author": author_agent.model, "tests": tests_agent.model},
                                             "panel": res.panel[panel_mark:]})
                        return res
                    feedback = gate_feedback(report)
                    code = report.reject_code.value if report.reject_code else "REJECTED"
                    last_report = report
                except _NearCopy as exc:
                    code, feedback = exc.code, str(exc)
                except ModelProviderError as exc:
                    res.reason = f"model provider: {exc}"
                    return res
                except PackageError as exc:
                    code, feedback = "DRAFT_ERROR", f"The draft could not be built: {exc}"
                except FactoryError as exc:
                    res.reason = f"error: {exc}"
                    return res
                full = feedback  # the gate's feedback; the author only ever gets it filtered (no test code)
                if last_report is not None and code == RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH.value:
                    full = tests_feedback(last_report, list(tests or {}))
                owner, why = route(code, full, attempt, str((sol or {}).get("requirements") or ""), res.attempts)
                res.attempts.append({"attempt": attempt + 1, "code": code, "role": owner,
                                     "reason": feedback.splitlines()[0][:300], "diagnosis": why,
                                     "failing": sorted(_failing_tests(full)), "timings": t,
                                     "models": {"author": author_agent.model, "tests": tests_agent.model},
                                     "panel": res.panel[panel_mark:]})
                last_report = None
                log.info("%s attempt %d: %s -> %s repair: %s", p.proposal_id, attempt + 1, code, owner,
                         feedback.splitlines()[0][:300])
                if len(res.attempts) >= 2:  # the same model failing the same way twice: the next model repairs
                    before, now = res.attempts[-2], res.attempts[-1]
                    pool = self.authors if owner == "author" else self.test_writers
                    current = author_agent if owner == "author" else tests_agent
                    if (len(pool) > 1 and before["role"] == now["role"] == owner and before["code"] == now["code"]
                            and before["reason"] == now["reason"]):
                        nxt = pool[(next(i for i, a in enumerate(pool) if a is current) + 1) % len(pool)]
                        step(f"{owner}: {current.model} repeated the same failure; {nxt.model} takes over the repair")
                        if owner == "author":
                            author_agent = nxt
                        else:
                            tests_agent = nxt
                redo = owner
                if owner == "tests":
                    feedback = full
                if owner == "author":
                    feedback = _author_feedback(feedback, code)
                    hints = edit_hints(repo, sol, self.spec.language) if code == "DRAFT_ERROR" else ""
                    if hints:
                        feedback += "\nWhere your edits went wrong:\n" + hints
                if why:
                    feedback += "\nDiagnosis: " + why
                if sol is None:
                    redo = "both"
        res.verdict = "dropped"
        res.reason = res.attempts[-1]["reason"] if res.attempts else "no attempt"
        return res


class _PanelFailed(Exception):
    """No panel draft was usable: the first usable-looking one goes on to the ordinary repair."""

    def __init__(self, agent, draft, error: str, record: dict) -> None:
        super().__init__(error)
        self.agent, self.draft, self.error, self.record = agent, draft, error, record


class _NearCopy(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- beside the task folder


def write_side_files(out: Path, task_id: str, p: TaskProposal, feature: PeerFeature | None, snapshot_id: str,
                     declarations: list[dict], harbor: dict | None) -> None:
    """provenance/, reviews/ next to the task folders (the task keeps exactly the five parts)."""
    out = Path(out)
    prov = {
        "task_id": task_id, "proposal_id": p.proposal_id, "snapshot_id": snapshot_id, "kind": p.kind,
        "sources": [x.model_dump(mode="json") for x in p.provenance],
        "novelty": p.novelty.model_dump(mode="json"),
        "similarity_to_peer": p.similarity.model_dump(mode="json") if p.similarity else None,
        "peer_bundle": None if feature is None else {
            "pr": feature.reference_bundle.pr.model_dump(mode="json"), "issue": feature.reference_bundle.issue,
            "followup_fixes": [f.get("url") for f in feature.reference_bundle.followup_fixes]},
        "edge_cases_from_peer": p.edge_cases_from_peer, "drafter": p.drafter, "approved_by": p.approved_by,
        "build_attempts": p.build_attempts, "validation": p.validation, "harbor": harbor,
        "models": declarations,
    }
    (out / "provenance").mkdir(parents=True, exist_ok=True)
    (out / "provenance" / f"{task_id}.json").write_text(json.dumps(prov, indent=2, ensure_ascii=False) + "\n",
                                                        encoding="utf-8")
    (out / "reviews").mkdir(parents=True, exist_ok=True)
    (out / "reviews" / f"{task_id}.json").write_text(json.dumps({
        "task_id": task_id, "reviews": [r.model_dump(mode="json") for r in p.reviews],
        "edits": [e.model_dump(mode="json") for e in p.edits], "history": p.history,
        "manual_edits": len(p.edits)}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
