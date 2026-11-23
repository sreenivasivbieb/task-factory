# Decisions, assumptions and limitations

Recorded as the build goes (design doc section 20). Newest last within each section.

## Decisions

- **D1 Python 3.11+, uv for dev.** Runtime dependency: pydantic v2 (MIT). Dev: pytest (MIT).
  CLI uses `argparse` from the standard library, so no extra dependency.
- **D2 LLM provider not chosen yet.** `LLMClient` stays provider-agnostic; only a mock adapter
  exists until hackathon credits are known (M6).
- **D3 EnvConfig has extra fields** beyond section 6: `framework`, `report_format` and
  `report_path`. `test_command` is a shell template with `{report}` and `{select}` placeholders.
  Needed so one runner can drive pytest, node:test, Jest, Go, Maven and Cargo.
- **D4 Fixture tasks** (one good task and one bad task per reject code) are added with the
  validation runner (M4), because their on-disk format is defined there.
- **D5 git inside every image.** The generated Dockerfile installs git when missing (apt, apk or
  yum) and commits the archive as a `base` commit, so `apply_patch` and diff capture work the
  same in every language image. `.factory/` (patches, reports, scratch) is git-ignored there.
- **D6 Non-root.** The image gets a `factory` user (uid 10001) when `useradd`/`adduser` exists.
  The container's PID 1 (`sleep infinity`) runs as root; every `exec` runs as `factory`.
- **D7 Timeout kill.** On timeout the backend runs `kill -9 -1` as the exec user inside the
  container (kills every process except PID 1), then reaps the host `docker exec` process.
  LocalSandbox kills the process group (`start_new_session` + `killpg`).
- **D8 OOM detection is a heuristic:** exit code 137 without a timeout sets `oom_killed`.
- **D9 LocalSandbox sets `TMPDIR`** to a per-sandbox folder, so `/tmp` state does not leak
  between local sandboxes.
- **D10 `run_repeated(sb, env, n, select)` repeats inside one sandbox** (the doc's signature
  took a sandbox factory). This catches random flakiness and tests that depend on state left by
  an earlier run, and keeps fixtures deterministic. G5 starts a fresh sandbox for each repeated
  gate, so the "fresh sandbox per gate" rule still holds.
- **D11 Own node:test reporter.** Node's built-in junit reporter has no `file` attribute before
  v23, so test IDs were ambiguous. The runner writes `.factory/node-junit.mjs` into the sandbox
  and node:test projects use it (`NODE_TEST_COMMAND`). Works on Node 20+.
- **D12 Selection.** pytest, node:test and Jest select files/IDs on the command line. Go selects
  package folders. Maven, Gradle and Cargo run the full suite and results are filtered.
- **D13 Loader dirty check** refuses changes to tracked files; untracked files only log a
  warning, because the loader clones committed content and never copies them anyway.
- **D14 Synthetic history is deterministic**: fixed author and date, so the same zip gives the
  same commit SHA on every run (needed for repeatable task IDs).
- **D15 Inspector defaults**: Python -> `python:3.11-slim` (raised by `requires-python`), pytest
  for every Python repo (it also runs unittest suites); Node -> `node:22-slim`, Jest, Vitest or
  node:test; anything else -> `needs_manual_config` with reasons. LLM help for setup commands is
  not built (section 8 marks it optional).
- **D16 Gate order** is G0, G1, G2, G3, G4, G6, G5: G5 (stability) is the most expensive and
  the doc says to run it only on tasks that passed every other gate.
- **D17 Feature tests = new test IDs.** G2 runs the task's test files; the feature tests are the
  test IDs not present in the baseline (so a task may extend an existing test file).
  `fail_to_pass` = those IDs, `pass_to_pass` = every test that passed at baseline.
- **D18 A feature test that is missing from the G3 run counts as failing** only when the run
  exited non-zero (e.g. ImportError because the feature does not exist yet).
- **D19 G4 also checks the feature tests inside the full suite**; if they fail there only, the
  reject code is FEATURE_TESTS_FAIL_WITH_PATCH (test-order or pollution problem).
- **D20 G6 empty-patch control reuses G3** (same run, no extra sandbox). Mutants change only
  lines the patch adds, so they apply wherever the reference applies. Operators skip comments
  and operators inside string literals, to avoid mutants that cannot change behaviour.
- **D21 Hand-written drafts** (`task.json`, `requirements.md`, `after/`, `tests/`, optional
  `solution.diff`) compute their patch with git against the pinned commit. Fixtures use this
  format; `factory validate --tasks DIR` accepts it too.
- **D22 The local backend skips setup commands** unless `sandbox.local_run_setup = true`: it
  would install packages into the developer's own environment. The host must already have the
  test tools (e.g. pytest).
- **D23 `checksums.txt` covers the task files** (instruction, task.toml, solution, tests,
  environment) but not `validation/`: gate logs hold timings, so they differ on every run and
  would break "same seed, same checksums". `verify_package` regenerates the evidence instead.
- **D24 `factory validate` saves each draft** next to its report in `workdir/runs/<task_id>/`;
  `factory export` writes packages from the valid ones.
- **D25 `verify_package` reloads the repo** from `task.toml`'s `repo` + `commit_sha`, so the
  source must still be reachable (URL or path). The repo itself is not copied into packages.
- **D26 Human review is a first-class step** (user decision, 2026-11-21). Valid tasks enter a
  task store as `pending`; only `approved` tasks are published. The review state lives in an
  append-only `tasks/.review/log.jsonl`, not in the packages, so review never changes checksums.
- **D27 Soft delete with a required reason.** Deleted packages move to `tasks/.trash/` and can be
  restored. `publish` writes `review_summary.json` (deletions with reasons, human-authored tasks,
  manual edits) because the organizers require manual changes to be disclosed. `factory export`
  does not re-export a task a reviewer deleted.
- **D28 Human-authored tasks get no shortcut**: `factory tasks add` runs every gate and marks the
  task `generated_by.model = "human"` with the author. Added tasks are approved by default
  (`--pending` to opt out), since their author already reviewed them.
- **D29 Edits are re-validated and disclosed.** `tasks edit --apply --reason` replaces a package
  only if the edited draft still passes every gate; it sets `manual_edit` and sends the task back
  to `pending`.
- **D30 Terminal review UI with `rich`** (MIT) instead of a web UI (the design doc kept web UIs out
  of scope). Every interactive view also exists as a non-interactive `factory tasks show`.
- **D31 Coding harness pulled forward from M12** and named `factory harness` (`factory bench` kept
  as an alias). Agents are data (install + run command + secrets); presets for Claude Code,
  Codex CLI, Aider and mini-swe-agent are marked experimental until run with real API keys.
- **D32 Two sandboxes per attempt.** The agent runs with network (it must reach its model API) in
  a container that has no hidden files; grading runs in a fresh offline container. Existing test
  files the agent changed are restored before grading (`harness/protect.py`).
- **D33 Secrets via environment only.** API keys are passed as `docker exec -e NAME` with the value
  in the docker CLI's environment, so they never appear in a command line, `ps` output or logs.
- **D34 Every image tags its starting commit `factory-base`** (prelude version 3), so diffs are
  taken against the right commit even if an agent commits.
- **D35 Harbor export.** `tasks publish --format harbor` writes Harbor's layout
  (`/solution/solve.sh`, `/tests/test.sh` writing `/logs/verifier/reward.txt`, `environment/` with
  the repo archive). Verified with the real Harbor CLI: oracle 1.000, nop 0.000.
- **D36 Assignments live in the review log** (user decision, 2026-11-21): `assign` events with
  agent + model, closed by the worker's `result` event or by `unassign`. `factory harness work`
  runs every open assignment. Assignments never change review status.
- **D37 A passing AI solution becomes a new git node** (user decision): a commit on branch
  `ai/<agent>-<model>/<task_id>` in `workdir/solutions/<repo>` (a clone, never the supplied repo).
  The commit holds exactly the graded diff (restored test files removed; hidden tests never
  included). Only real agents get branches, not `oracle`/`noop`.
- **D38 The Task Factory engine is the default test agent** (user wanted AWS Qwen, Zen,
  TokenRouter or NVIDIA free models): one engine covers all four through `provider/model`.
  It is our own bundled binary, copied into the agent image, so the harness installs no
  third-party coding CLI and the benchmark runs the same engine as the terminal.
- **D39 `--use-host-auth` is opt-in** and copies only the model provider's entry of the agent's
  login file into the sandbox, through stdin, mode 600.
- **D40 `agent_error` category**: an agent that exits non-zero without changing anything is not
  reported as `no_change`, so a wrong model ID or key is not mistaken for a weak model.
- **D41 Custom agents may run on the local backend** (they are the user's own commands, e.g. in
  tests); built-in presets that install third-party agents are refused there.

- **D42 Grading undoes every change to protected files** (review round 4, 2026-11-21). Found by
  running a "cheating agent": a six-line root `conftest.py` that marks every test passed got a task
  `resolved` with no implementation. Protected now = test paths + test/build config
  (`conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `package.json`,
  jest/vitest/mocha config, `sitecustomize.py`, `*.pth`, `go.mod`, `Cargo.toml`, `pom.xml`, Gradle).
  Changed or deleted ones are restored, added ones removed, in the harness and the Harbor verifier
  alike. Safe because dependencies are installed at image build time, never while grading.
- **D43 The reference solution may not touch protected files** (new reject code
  `SOLUTION_TOUCHES_PROTECTED`, gate G1): otherwise the grader's restore would make the task
  unsolvable. Test helpers go in the hidden test files, which are written after the restore.
- **D44 The Harbor verifier checks that the hidden tests ran**: it runs the feature test files
  first and, for JUnit reports, requires at least `len(fail_to_pass)` test cases with no failure,
  error or skip, before the full-suite exit code decides the reward.
- **D45 Packages carry `environment/repo.tar`** (user decision): self-contained, offline, buildable
  with `docker build environment/`. `package_repo()` reads it for verify, edit, Harbor export and
  the harness; the solutions repo falls back to it (tag `base/<sha>`) when the original repo is gone.
- **D46 Base images are pinned by digest at inspect time** (user decision; `sandbox.pin_base_image`,
  default on). `python:3.11-slim` becomes `python:3.11-slim@sha256:…`, so a rebuild months later
  uses the same base. Images without a registry digest keep their tag, with a warning.
- **D47 Agent errors are not results**: when every attempt is `agent_error`/`leak` the worker keeps
  the assignment open and notes it (no "unsolved" note); difficulty is `error`. A run stops after
  two agent errors in a row (an exhausted quota fails every task alike). Seen live: the Zen free
  quota returned HTTP 429 and earlier runs wrongly closed the assignments as unsolved.
- **D48 The workflow never discards a human decision**: `export` skips packages already in the store
  (`--force` overwrites); `tasks publish` removes task folders that are no longer approved; `tasks
  add` picks the next free task number and refuses a draft whose `task_id` already exists.
- **D49 Smaller fixes**: the baseline cache key includes the test settings; G6 says when no code
  mutant could be made; `--task` accepts the same partial IDs as `tasks show`; planned commands
  (`ideas`, `generate`, `report`, `run`) are labelled with their milestone in `--help`; tags
  `security`, `scalability` and `api design` were added.
- **D50 Fixes after trying the terminal end to end** (2026-11-23, after PR #1):
  `build_terminal.py` installs the engine's dependencies *before* branding (the rebrand step
  needs the checkout's `typescript`, so a first build from a fresh clone failed);
  `/generate-task` shows the gate and reject reason when a draft is rejected (it only said
  "Draft rejected"); the generator's prompts show the exact JSON shape and also accept a bare
  `{path: text}` map for tests, because a real model dropped the `{"tests": ...}` wrapper;
  `factory`/`task-factory` open the terminal only with no arguments, `chat`/`terminal`, an engine
  flag or a project folder, so a typo such as `factory taks` is an error again; the
  `task-factory` agent declares the engine's login file so `--use-host-auth` works with logins
  made in the terminal.
- **D51 Terminal commands for the whole loop** (user decisions, 2026-11-23): `/setup`, `/tasks`,
  `/run-task`, `/results` next to `/generate-task`. One stable workdir per project *outside* the
  repo (`~/.local/state/task-factory/projects/<repo>-<hash>`), replacing a new temporary workdir
  per generation. `/run-task` uses the selected model by default with a picker to change it,
  assigns the tasks and runs only those (`harness work` gained an internal filter), and uses the
  terminal's stored login for that provider when there is one. "Session outputs" are a Task
  Factory page built from `agent.log.json` (messages, tool calls with output, tokens), not an
  import into the engine's session list. Python does the work (`factory/terminal_ops.py`); the
  plugin only renders and streams progress. Found on the way: `tasks add` recorded every draft
  as human-authored, so `/generate-task` tasks were disclosed as human ones; a draft's `model`
  is now kept as its author.

## Limitations

- L1 LocalSandbox has no isolation and allows network; it logs a warning on every use.
- L2 Go, Maven, Gradle and Cargo support is parser-level only so far (unit-tested on sample
  reports); only Python and Node run end to end on fixtures.
- L3 Mutation testing is heuristic. An equivalent mutant (one that cannot change behaviour,
  e.g. swapping the arguments of a symmetric function) survives and rejects a good task. The
  operator set is configurable (`gates.mutations`) and every survivor is named in the report.
- L4 `gates.task_timeout_s` is not enforced yet; each test run has its own timeout.
- L5 Known baseline failures are deselected in the Harbor verifier for pytest only; for other
  frameworks a repo with known failures would score 0. The "every hidden test ran" check (D44)
  needs JUnit reports; for Jest JSON, go test and cargo the verifier relies on exit codes.
- L6 Grading protection (D42) covers files that are known to steer test runs. A determined agent
  could still special-case the tests from inside source code (for example detect pytest and
  return canned answers); hidden tests, mutation controls and human review are the defence there.
- L7 Agent presets other than `oracle`, `noop` and `command` have not been run with real API
  keys in this environment; their command lines follow each tool's documentation.
- L8 Test protection cannot tell a harmless edit (an agent adding its own tests to an existing
  test file) from a harmful one; both are restored before grading and listed in
  `tests_tampered`.
- L9 AWS Bedrock Qwen is configured but not run here (no AWS credentials on this machine); the
  engine path itself was verified with a Zen free model.
- L10 Every hidden test must fail without the change (G3), so a task cannot ship separate
  "guard" tests that already pass (e.g. "the old behaviour still works"); such assertions go
  inside a failing test. Existing tests already act as guards (pass_to_pass).
- L11 Performance tasks rely on timing ratios; G5 repeats catch flaky ones, but a heavily loaded
  machine can still make them fail. Mutation operators rarely apply to a pure data-structure
  change, so such tasks need correctness assertions in the timing test.
- L12 Ideas mined from the repo (TODO/FIXME, `NotImplementedError` stubs, skipped tests, bug-fix
  commits, stale TODOs) are planned (M7, plus a history miner for bug fixes) but not built yet;
  today tasks are written by people or drafted from a prompt (`/generate-task`, `factory generate`).
- L13 A task can only depend on what the base image has: dependencies are installed at build
  time, and the grader restores build files, so a task that needs a new dependency cannot pass.
- L14 `/generate-task` cannot use Zen's free models: the engine's `generate.text` is a stateless
  request without the client/session headers of a normal session, and Zen answers "Free usage
  limit reached" (the same models work in `/run-task`). We do not fake those headers.

