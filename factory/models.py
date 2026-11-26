"""Shared data models (design doc section 6).

Every component exchanges these models, never raw dicts. Each model carries
``schema_version`` and serializes to JSON through pydantic.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1


class Model(BaseModel):
    """Base for all factory models: strict fields, JSON round trip helpers."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        """Serialize to pretty JSON with a trailing newline."""
        return self.model_dump_json(indent=2) + "\n"

    def save(self, path: Path) -> Path:
        """Write the model as JSON to ``path`` (parent folders are created)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path):
        """Read a model from a JSON file."""
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- repo + env


class RepoSpec(Model):
    """A supplied repository, copied and pinned to one commit."""

    name: str
    source: str  # local path, git URL or zip path as supplied
    commit_sha: str
    local_path: str
    language: str | None = None
    synthetic_history: bool = False
    # A tar of the pinned commit (e.g. a package's environment/repo.tar). When set, images and
    # clean copies are made from it, so the original repo does not need to be reachable.
    archive: str | None = None

    @field_validator("commit_sha")
    @classmethod
    def _full_sha(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", v):
            raise ValueError(f"commit_sha must be a full 40-character SHA, got {v!r}")
        return v

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:7]


SelectMode = Literal["ids", "files", "dirs", "none"]
# Older env.json files have no select fields: derive them from the framework label they carry.
_LEGACY_SELECT = {"pytest": ("ids", "", "--deselect {id}"), "node": ("files", "", None),
                  "jest": ("files", "", None), "go": ("dirs", "./...", None)}
_LEGACY_NODE_REPORTER = ".factory/node-junit.mjs"
_LEGACY_ARTIFACTS = ("*.egg-info/", "node_modules/", "__pycache__/", ".pytest_cache/")  # older node:test packages wrote this file implicitly


class EnvConfig(Model):
    """How to build and test a repo inside the sandbox.

    ``test_command`` is a shell template. ``{report}`` is replaced by
    ``report_path`` and ``{select}`` by the quoted test selectors (``select_all`` for
    the full suite). ``select`` says what a selector is: test ``ids``, test ``files``,
    the ``dirs`` holding them, or ``none`` (run everything, keep the wanted results).
    ``helper_files`` (path -> content) are written into the repo before tests run.
    Paths are relative to the repo root. Nothing here is specific to one language:
    the values come from ``ecosystems.toml`` or ``factory.toml``.
    """

    base_image: str
    setup_commands: list[str] = Field(default_factory=list)
    test_command: str
    test_timeout_s: int = 600
    env_vars: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/repo"
    framework: str  # a label, e.g. "pytest"
    report_format: str  # a registered report parser, see runner/parsers
    report_path: str = ".factory/report.xml"
    select: SelectMode | None = None
    select_all: str = ""
    deselect: str | None = None  # template to skip one test id, e.g. "--deselect {id}"
    helper_files: dict[str, str] = Field(default_factory=dict)
    # build output never part of a change (git exclude in the sandbox); older packages: the old fixed list
    artifacts: list[str] = Field(default_factory=lambda: list(_LEGACY_ARTIFACTS))
    # ERE of the test and test-config paths a solver may not change, recorded at inspect time so a
    # package is graded the same way on a machine without this factory.toml (None: older package)
    protected_paths: str | None = None

    @field_validator("report_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        from .runner.parsers import PARSERS

        if value not in PARSERS:
            raise ValueError(f"unknown report_format {value!r}; known: {sorted(PARSERS)}")
        return value

    @model_validator(mode="after")
    def _legacy_select(self) -> "EnvConfig":
        if not self.helper_files and _LEGACY_NODE_REPORTER in self.test_command:
            from .ecosystems import helper_text

            self.helper_files = {_LEGACY_NODE_REPORTER: helper_text("node_junit_reporter.mjs")}
        if self.select is None:
            select, select_all, deselect = _LEGACY_SELECT.get(self.framework, ("none", "", None))
            self.select = select
            self.select_all = self.select_all or select_all
            if self.deselect is None:
                self.deselect = deselect
        return self


class InspectResult(Model):
    """Inspector output: an EnvConfig, or the reasons manual config is needed."""

    status: Literal["ok", "needs_manual_config"]
    env: EnvConfig | None = None
    reasons: list[str] = Field(default_factory=list)
    baseline: RunSummary | None = None


class ImageRef(Model):
    """A built sandbox image (a Docker tag, or a prepared folder for LocalSandbox)."""

    tag: str
    backend: Literal["docker", "local"]
    location: str | None = None  # folder for the local backend


# --------------------------------------------------------------------------- execution


class ExecResult(Model):
    argv: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    oom_killed: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


TestStatus = Literal["passed", "failed", "skipped", "error"]


class TestResult(Model):
    __test__ = False  # not a pytest test class
    test_id: str
    status: TestStatus
    duration_s: float = 0.0


class RunSummary(Model):
    results: list[TestResult] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    raw_exit_code: int = 0
    timed_out: bool = False
    error: str | None = None  # set when the run crashed before tests ran

    @classmethod
    def build(cls, results: list[TestResult], exit_code: int, *, timed_out: bool = False,
              error: str | None = None) -> "RunSummary":
        counts = {s: 0 for s in ("passed", "failed", "skipped", "error")}
        for r in results:
            counts[r.status] += 1
        return cls(results=results, counts=counts, raw_exit_code=exit_code,
                   timed_out=timed_out, error=error)

    def status_of(self) -> dict[str, str]:
        return {r.test_id: r.status for r in self.results}

    def ids_with(self, *statuses: str) -> set[str]:
        return {r.test_id for r in self.results if r.status in statuses}

    @property
    def crashed(self) -> bool:
        """True when the command failed and no tests were parsed (build/collection error)."""
        return self.error is not None


# --------------------------------------------------------------------------- tasks


TaskKind = Literal["feature", "bugfix", "behavior_change"]




class RepairAttempt(Model):
    strategy: str
    reject_code: str | None = None
    attempt: int


class GeneratedBy(Model):
    model: str = "human"  # "human" for hand-written tasks
    author: str | None = None  # who wrote or added the task, for human-authored tasks
    session_id: str | None = None  # terminal session the task was generated in (/generate-task)
    request: str | None = None  # the user's request that the model turned into this task
    prompt_hash: str | None = None
    seed: int | None = None
    repair_attempts: list[RepairAttempt] = Field(default_factory=list)


class TaskDraft(Model):
    task_id: str
    idea_id: str
    kind: TaskKind
    tags: list[str] = Field(default_factory=list)
    requirements_md: str
    solution_patch: str  # unified diff against the starting commit
    test_files: dict[str, str] = Field(default_factory=dict)  # repo-relative path -> content
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)
    touched_files: list[str] = Field(default_factory=list)
    generated_by: GeneratedBy = Field(default_factory=GeneratedBy)
    manual_edit: bool = False
    manual_edit_reason: str | None = None


class RejectCode(str, Enum):
    BASELINE_FAIL = "BASELINE_FAIL"
    PATCH_APPLY_FAIL = "PATCH_APPLY_FAIL"
    FEATURE_TESTS_FAIL_WITH_PATCH = "FEATURE_TESTS_FAIL_WITH_PATCH"
    FEATURE_TESTS_PASS_WITHOUT_PATCH = "FEATURE_TESTS_PASS_WITHOUT_PATCH"
    REGRESSION = "REGRESSION"
    FLAKY = "FLAKY"
    NEG_CONTROL_PASSED = "NEG_CONTROL_PASSED"
    SOLUTION_TOUCHES_PROTECTED = "SOLUTION_TOUCHES_PROTECTED"
    UNDERSPECIFIED = "UNDERSPECIFIED"  # G7: the tests call names the requirements never give


class GateResult(Model):
    gate: str
    passed: bool
    details: str = ""
    exec_log_paths: list[str] = Field(default_factory=list)
    duration_s: float = 0.0


class ValidationReport(Model):
    task_id: str
    gates: list[GateResult] = Field(default_factory=list)
    verdict: Literal["valid", "rejected"]
    reject_code: RejectCode | None = None
    reject_reason: str | None = None
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)
    known_failures: list[str] = Field(default_factory=list)
    flaky_tests: list[str] = Field(default_factory=list)
    mutation_score: float | None = None
    mutants_run: int = 0
    mutants_caught: int = 0
    surviving_mutants: list[str] = Field(default_factory=list)  # names of mutants the tests did not catch
    backend: str | None = None  # sandbox the gates ran in: "docker" (isolated) or "local" (no isolation)


class ValidationSummary(Model):
    verdict: Literal["valid", "rejected"]
    backend: str | None = None
    gates: list[str] = Field(default_factory=list)
    mutation_score: float | None = None
    mutants_run: int = 0
    mutants_caught: int = 0


class TaskPackage(Model):
    """Contents of ``task.toml``: the metadata of one exported task package (section 13)."""

    task_id: str
    repo: str  # URL or path the repo was loaded from
    repo_name: str
    commit_sha: str  # the starting version
    language: str | None = None
    test_command: str
    kind: TaskKind
    tags: list[str] = Field(default_factory=list)
    idea_id: str
    touched_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)
    known_failures: list[str] = Field(default_factory=list)
    manual_edit: bool = False
    manual_edit_reason: str | None = None
    difficulty: str | None = None
    solver_results: dict[str, float] | None = None
    environment: EnvConfig
    generated_by: GeneratedBy = Field(default_factory=GeneratedBy)
    validation: ValidationSummary


# --------------------------------------------------------------------------- helpers


def slugify(text: str, max_len: int = 32) -> str:
    """Lower-case, dash-separated slug used in task IDs."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def make_task_id(repo_name: str, commit_sha: str, title: str, n: int) -> str:
    """Task ID format ``<repo>-<short_sha>-<slug>-<nnn>`` (deterministic)."""
    return f"{slugify(repo_name, 24)}-{commit_sha[:7]}-{slugify(title)}-{n:03d}"


InspectResult.model_rebuild()
