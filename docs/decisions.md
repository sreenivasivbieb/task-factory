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
- **D52 Engine commands unrelated to tasks are hidden** (user request, 2026-11-23): the build hides
  them from `/` completion and the command palette (`HIDDEN_COMMANDS` in
  `scripts/build_terminal.py`: btw, cd, debug, goal, highlights, init, mcps, pair, plugins, reload,
  restart, review, skills, stats, terminal, update, usage, worktrees). Kept: models and providers,
  sessions, agents, diff, editor, settings, status, themes, help, exit, and the Task Factory
  commands. Hidden, not deleted: a typed command or its keybind still works, and the engine's
  source stays a plain patch of upstream. `/plugins` is hidden because turning the Task Factory
  plugin off from there would remove our commands.
- **D53 Sessions show the project's state; generation runs in its own session** (user decisions,
  2026-11-23). The sidebar of every session shows tasks (by status, not run yet, never solved),
  AI performance (resolve rate over graded attempts, the doc's difficulty tag, failure
  categories), per model (resolved/attempts, mean time, mean tokens, agent errors apart), what
  this session generated (valid or rejected with the gate), and the next step. `/benchmark` is
  the tasks × models matrix (the doc says no leaderboard, so it is framed as task difficulty, not
  a ranking). `/generate-task` runs in a separate, top-level, read-only session: tried first as a
  child session, the engine delivered the child's failure to the parent session, whose Build
  agent then implemented the task in the user's repo by itself. Each task records the request
  and the session it was made for (`generated_by.session_id`, `request`); every attempt, valid
  or not, goes to `generations.jsonl`. `/setup` pins and inspects a new commit in a staging
  folder and keeps the working setup when that fails (seen live: PyPI unreachable). Generated
  requirements always open with `# <title>`.
- **D54 Review of a teammate's fixes** (2026-11-24, commits 1e3bc30..d3fe292): Windows support
  (editor quoting, path parsing, no `killpg`, `shutil.move` for drafts) and the crash fixes were
  kept. Their publish fix stopped an empty store from wiping a submission folder, but also undid
  D48 (a republish left deleted tasks behind). Both now hold: publish removes only stale folders
  of tasks the store knows, even when nothing is approved, and keeps unknown ones. The tar
  safety filter is chosen by feature (`tarfile.data_filter`), not by version, so Python 3.11.4+
  keeps it. Validation reports and `task.toml` now record the sandbox backend (`docker` or
  `local`): the two packages in `submission/` were validated on Windows without it being
  recorded, and were re-verified here in Docker (2/2 valid).
- **D55 Whole-pipeline run on hand-written tasks** (2026-11-24, `examples/toyshop/`): three drafts
  (end-to-end feature, security fix, scalability fix with a load test) through strict gates,
  oracle/noop, four free models with k=2, publish and re-verification from disk (5/5 valid).
  What it found: G6 caught a test gap (a zero price) and an equivalent mutant (swapped arguments
  of a multiplication; the reference now uses keyword arguments). A model that failed 1 of 9
  tests (flint-40b, twice) was right: the requirements said "an int" while a test rejected
  `True`; the task was fixed through `tasks edit` (disclosed as a manual edit) and the model then
  solved it 2/2. Performance views now leave out the sanity agents (oracle, noop) and attempts
  made before a task's last manual edit; the harness difficulty uses the doc's words (`all`,
  `some`, `none`) everywhere.
- **D56 Languages are data, not code** (user request: "kisi bhi language pe chalna chahiye", no
  hard-coding; 2026-11-24). `factory/ecosystems.toml` holds every language rule that used to be
  code: inspector rules (markers, image, setup, test command; Node test tools and Maven/Gradle as
  `variants`, lock files as `install`), how tests are selected (`select`, replacing branches on
  the framework name), helper files (the node:test reporter), build output for git excludes and
  copies (per ecosystem, so `build/` is never dropped from a Python repo), the protected test and
  config paths, and G6's error-branch, value-return and expected-value rules (per file type: a
  Bash `return 1` is a status, not a value). `factory.toml` adds or overrides ecosystems under
  `[ecosystems.<name>]`. `EnvConfig.framework` is now a label and `report_format` any registered
  parser. A package carries what it needs (`select`, `helper_files`, `artifacts`,
  `protected_paths`), so grading does not depend on the grader's config; older packages load
  with the old values. Proof: a Bash repo defined only in factory.toml goes through inspect,
  every gate (including Bash mutation rules), export, oracle/noop, and a cheat that rewrites
  the test helper is blocked with no Bash config present (`tests/test_any_language.py`).
  `harness.stop_after_agent_errors` and the terminal's agent (`harness.default_agent`) are config.
- **D57 Dead code removed**: CLI placeholders `ideas`, `report`, `run`; empty `report/` and
  `variation/` packages; the unused `[generation]` config and `gates.task_timeout_s`;
  `config_hash`, `FeatureIdea`, `SOLVER_VISIBLE`, `AgentSpec.homepage`, `is_test_path`,
  `run_repeated`. Fields stored in existing `task.toml` files (`repair_attempts`,
  `solver_results`) stay, or older packages would stop loading. Mutation names in
  `gates.mutations` are now checked (a typo used to switch a mutant off silently).
- **D58 Session-scoped solutions, written in TypeScript** (user request 2026-11-24: "particular
  session tasks implementation should be shown with in that session only", and "JS/TS zyada").
  `/solutions` and `/rerun` live in the terminal plugin (`factory/terminal_plugin/solutions.ts`) and
  read the workdir directly; no Python was added. The plugin links each harness run it starts to the
  session in `<workdir>/session-runs.jsonl` (new run folders that appeared during the run and hold
  only the chosen tasks, so a run from another terminal is not taken). A copied session gets its task
  list in `session-tasks.jsonl`. Runs started from the CLI or from the home screen belong to no
  session and show only in `/results`. Agent errors (quota, login) are shown apart, as elsewhere.
  Tests: `tests/ts/*.test.ts` with Node's built-in runner (Node 22.18+ runs TypeScript directly),
  called from pytest (`tests/test_ts.py`, skipped on older Node).
- **D59 SWE-bench style miner in TypeScript, beside the gated pipeline** (user request 2026-11-24:
  "github link do, AI solved PRs with issues extract kare, tests hide kare, exact base node par
  sandbox", "isme koi gates nahi", "naya SWE pipeline poora TS", "abhi ka implementation mat
  badlo"). `swe/` (Node 22.18+, built-ins only, run with `node swe/cli.ts`) writes the factory's
  package layout, so the Python store, review, harness and Harbor export use SWE tasks unchanged.
  Open questions answered with the recommended defaults (the user asked to implement without
  answering them): one before run + two after runs (the minimum that yields FAIL_TO_PASS /
  PASS_TO_PASS; not the gates), problem statement = the issue text as written (only HTML template
  comments removed), tasks land as `pending`, a linked issue is required, the setup agent's model
  call is made by the terminal (host side; no credential enters a container), images are not
  layered (the harness is unchanged; the tag and Dockerfile are byte-identical to Python's, so the
  harness reuses the miner's image), dependencies are time-travelled with
  `uv --exclude-newer <merge date>`, Python version = the one current at the PR's date (or higher
  if `requires-python` says so). `tests/test_swe_parity.py` checks Dockerfile, image tag, env.json,
  task.toml and JUnit IDs against the Python originals.
- **D60 PASS_TO_PASS is filtered by a full-suite run.** The harness grades PASS_TO_PASS in the full
  suite, not in the PR's test files (SWE-bench's way). On humanize an unrelated module that did not
  import made every oracle attempt a "regression"; the miner now runs the whole suite once after the
  change and keeps only PASS_TO_PASS tests that are green there (`not_green_in_full_suite` in
  `validation/swe-runs.json`). An after-run with no passing test is an environment failure
  (`env_failed`, and the setup agent is asked), not "no FAIL_TO_PASS".
- **D61 Review round on the SWE miner** (2026-11-24, after a real run on pallets/click: 8/8 tasks,
  oracle 8/8, empty patch 0/8). Fixed: a test file that contains U+FFFD was taken for binary (now a
  strict UTF-8 decode of the raw bytes); hidden files that are not test modules (a mypy typing file,
  data, helpers) were passed to pytest, which aborts the whole run, so the test command now passes
  only `test_*.py` / `*_test.py` (the harness passes every hidden file; the filter lives in the
  command); skipped tests were counted as failing; PEP 735 `[dependency-groups]` test groups are
  installed; a run killed with exit 137 is reported as `oom` (`--memory`); `cp` replaced (Windows);
  the package hardlinks the cached `repo.tar` and the cache is removed after a run; the sidebar no
  longer reads every package. Dead code: `models.sha256_text`, an unused import in the plugin.
  Quality flags per task (`swe.json` `quality`): hidden tests that call a new name the issue never
  mentions, a too-short problem statement, a very large change.
- **D62 Harness agents run without web tools** (user choice 2026-11-24, after the click leak). The
  engine gets `OPENCODE_CONFIG_CONTENT` with deny rules for `webfetch` and `websearch` (the built
  binary still reads the upstream name; checked by hand: with the rules, `run --auto` has no web
  tool and says so; without them it fetched a page). Claude Code gets
  `--disallowedTools WebFetch,WebSearch`. Network limits and the login proxy were not chosen yet.
- **D63 Agent sandboxes reach only the model APIs** (user request 2026-11-24, "sahi API tak simit").
  With web tools off, a model still ran `curl https://api.github.com/search/issues?...` from its
  shell. Now the agent's container is on an internal Docker network (`factory-egress`, no route
  out) and gets `HTTPS_PROXY` pointing at a small proxy container (`factory/harness/egress_proxy.py`,
  standard library, non-root, read-only, 128 MB) that is the network's only way out and tunnels
  HTTPS `CONNECT` to `harness.allowed_hosts` only (fnmatch patterns; defaults: Zen
  `opencode.ai` + `*.opencode.ai`, found by routing the engine through the proxy, and the NVIDIA,
  TokenRouter, OpenRouter, Anthropic, OpenAI, Groq and Bedrock APIs). Plain HTTP and other ports
  are refused. Each attempt records the refused hosts (`egress_blocked` in `grade.json`,
  `egress-blocked.txt`), shown in `/solutions`. The proxy is reused across runs (one per allowlist);
  remove it with `docker rm -f $(docker ps -aq --filter label=factory-egress=1)`. Agents whose tools
  need other hosts (e.g. LiteLLM's price list) may warn; add hosts in factory.toml.
- **D64 Records in MongoDB by dual write** (user choices 2026-11-24: records in Mongo, packages stay
  files; dual write). `factory/records.py`: with `TASK_FACTORY_MONGO_URI`, the review store,
  `/generate-task` log, rejections, package writer and harness (attempts, runs) also upsert into
  MongoDB (`tasks`, `review_events`, `generations`, `rejections`, `attempts`, `runs`, `session_runs`,
  `session_tasks`, `swe_prs`, `swe_mining`). Fixed `_id`s, so a live write and `factory db sync`
  never duplicate. The TypeScript side writes through `python -m factory.records sync <folder>` (no
  npm driver). A failed write warns once and turns MongoDB off for the process; files are always
  written. The user stopped Folio's MongoDB (`folio-mongo`, not removed) so ours uses 27017.
- **D65 The pipeline in containers** (user request: "kahi bhi chale"). `docker-compose.yml`: `mongo`
  (mongo:8.0, healthcheck, named volume) and `factory` (profile `cli`, image from `Dockerfile`).
  Sandboxes are created on the host's Docker through its socket (user choice over Docker-in-Docker:
  no privileged container, shared image cache). `scripts/factory-docker` runs as the host user with
  the socket's group, passes `gh auth token`, mounts the state folder, the engine (read-only) and
  the working repo. The terminal UI stays on the host.
- **D66 Optimizations** (user choices). (1) Agent files (the engine) are mounted read-only from a
  content-named volume (`factory-file-<digest>-<name>`, labelled for prune) instead of a derived image
  per task image; the engine moved to `/opt/task-factory/` because the volume is mounted as the
  file's folder. (2) `factory images prune`. (3) Parallel attempts (`harness.workers`, `--workers`)
  with locks for the run's files, the solutions repo and image builds; the egress proxy logs the
  client address so each attempt gets its own refused hosts. (4) Parallel SWE candidates
  (`--workers`), git on the shared clone serialized. Both default to 1 (user: no extra risk by
  default).
- **D67 Dead code removed** (2026-11-24): the empty placeholder packages `factory/features`,
  `factory/benchmark` and `factory/llm` (never imported; the model client is `model_client.py`, the
  benchmark is the harness), `Registry.test_re` (only a test used it), and the file copying in
  `build_derived_image` (agent files are mounted from volumes since D66; derived images now only run
  install commands). Kept on purpose: framework hooks (`redirect_request`, `writable`, pydantic
  validators) and `submission/` (a teammate's deliverable). Design doc module map updated.
- **D68 The SWE miner follows the project's pytest `python_files`** (open issue from the review: a
  project that names its tests `check_*.py` lost them). `swe/testfiles.ts` reads `python_files` from
  `[tool.pytest.ini_options]`, `pytest.ini`, `tox.ini` `[pytest]` or `setup.cfg` `[tool:pytest]`
  (pytest's own order; `pytest.ini` wins even without the option). The patterns decide which files
  of a PR are hidden tests (at the base commit; at the default branch for the mining filter) and
  which files the test command passes to pytest. Patterns with characters other than plain globs, or
  not ending in `.py`, fall back to the defaults, because they are written into a shell command. The
  repository is now cloned before mining.

## Limitations

- **The agent sandbox has the internet** (it must reach the model), and the engine runs with every
  tool allowed. On pallets/click the free model looked up the public fix in 3 of 7 attempts (web
  search, GitHub issues, once `pull/3902.diff`). Such attempts are marked "used the internet" in
  `/solutions` and after a run. Fixed by D62 (web tools off) and D63 (the sandbox reaches only the
  model APIs). Left: an allowed model API could itself browse (e.g. a provider's server-side web
  search); the provider login copied into the sandbox is readable by the repository's own code
  there (proposed: a host-side proxy for model calls); `harness.egress = "open"` brings the old
  behaviour back.

- **SWE miner** (D59): Python repositories only; one Docker image per base commit (the Python
  side's Dockerfile copies the repo before installing, so layers are not shared: several hundred MB
  per task); public PRs may be in a model's training data (use `--since` for recent PRs only); no
  mutation or regression gates, so a task is only as strong as the project's tests; PRs that delete
  a test file or ship non-UTF-8 test files are dropped; the setup agent's commands run in the
  container build with network on.

- L1 LocalSandbox has no isolation and allows network; it logs a warning on every use.
- L2 Python, Node (node:test) and Bash (defined only in factory.toml) run end to end on
  fixtures. The built-in Go, Maven, Gradle and Cargo entries are data that no real repo has
  gone through yet; their report parsers are unit-tested on sample reports.
- L3 Mutation testing is heuristic. An equivalent mutant (one that cannot change behaviour,
  e.g. swapping the arguments of a symmetric function) survives and rejects a good task. The
  operator set is configurable (`gates.mutations`) and every survivor is named in the report.
- L4 There is no per-task time limit across gates (the unenforced `gates.task_timeout_s` was
  removed, D57); each test run has its own timeout (`env.test_timeout_s`).
- L5 Known baseline failures are deselected in the Harbor verifier only for ecosystems with a
  `deselect` template (pytest has one); for others a repo with known failures would score 0. The "every hidden test ran" check (D44)
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
- L14 (resolved by D53) `/generate-task` first used the engine's stateless `generate.text`, which
  Zen's free models refuse ("Free usage limit reached"); a session with every tool denied is
  refused the same way. A read-only agent session works. We never fake client headers.

