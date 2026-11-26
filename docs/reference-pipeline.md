# Reference-guided pipeline (`factory run`)

The v2.2 change proposal ("Reference-Guided Task Generation") built into the factory, redesigned as
an agentic flow. One model no longer invents tasks: the factory understands the starter repo, finds
the closest open-source peer repos, lets research agents look for features a maintainer could
accept, lets a critic agent check each idea against the real code (and run its example), writes a
precise spec, builds the package with one model or a panel of models, and proves it valid with the
gates G0-G6 and Harbor. An AI under test can then be benchmarked in the background.

```
starter repo ─► P1 profile ─► P2 peers ─► P5 research (parallel lanes) ─► P6 critic + spec
             ─► P7 review (optional) ─► P8 build (author / test writer, or a model panel)
             ─► gates G0-G6 ─► P9 Harbor nop + oracle ─► P10 benchmark ─► report
```

## Quick start

```bash
docker compose up -d mongo                      # state lives in MongoDB
export TASK_FACTORY_MONGO_URI=mongodb://127.0.0.1:27017/task_factory
export AWS_BEARER_TOKEN_BEDROCK=...             # bedrock/ models; any provider in model_client works
export TAVILY_API_KEY=...                       # optional: turns on the web research lane
uv run factory load    --repo https://github.com/pallets/click --commit cf19179e8e2fd54c601378cec9e0d21bb12b6fb1 --config examples/click/factory.toml
uv run factory inspect --config examples/click/factory.toml
uv run factory run     --config examples/click/factory.toml --count 2 --once \
    --model bedrock/qwen.qwen3-coder-next --model bedrock/moonshotai.kimi-k2.5
uv run factory report --print
```

Set models per role in `[reference.models]` (see [Configuration](#configuration-reference-in-factorytoml)).
`--model` is used for every role left empty; when a model hits its quota, the next `--model` takes over.
In the container, `scripts/factory-docker run ...` does the same (MongoDB, Harbor, `gh` and Docker
are already there).

## Stages

| Stage | Module | What it does | Guard |
|---|---|---|---|
| P1 Understand repo | `reference/profiler.py` | Static detection (language, test runner, dependencies, docs, test names, source outline), then the profiler writes a `RepoProfile`: purpose, capability map, design traits, boundaries | Every capability cites files; each citation is checked against the code |
| P2 Match peers | `reference/scout.py` | Recall by capability (GitHub search, named alternatives, topics), cheap cut, deep profiles of the shortlist (in parallel), capability-map comparison, top K with a score breakdown | Permissive licence, tests, recent activity, not archived or a fork, not the same organisation, not built on the starter; only capability text leaves the machine |
| P5 Research | `reference/research.py` | `research_lanes` research agents at once, each with tools: **peers** (README, docs, capabilities of the selected peers, then the other vetted ones), **starter** (its own code, docs and upstream issues / PRs), **web** (seeded web results, then follow-up searches; only with `TAVILY_API_KEY`). Ideas merge in lane order; the same title is kept once | Every lane reads the starter's code before it proposes; web queries use the profile and capability text, never source code |
| P6 Critic + spec | `reference/research.py`, `reference/drafting.py` | The critic agent (tools: repo, upstream, existing tasks, peers, web) decides whether the idea already works today, is one focused change, and is deterministically testable. Its example runs in a sandbox (Python: `ExampleRunner`); a result that contradicts the critic makes it decide again. "Fix" gives one refinement round. The spec writer then turns an accepted idea into a proposal with exact names, defaults and messages | Deterministic checks (bug fix, network, credentials, files exist, names not already defined, spec size); near-duplicates merged; an idea an existing package already asks for is rejected |
| P7 Review | `reference/review.py` | Optional (`--review`): approve, edit, reject; edits logged and re-checked | Off by default: the auto-approve policy approves |
| P8 Build | `reference/building.py` | Author writes the instruction and the reference patch (reading the code with tools); test writer writes the hidden tests from the instruction (reading the existing code with tools). With several models per role, a **panel** builds (below). The package writer assembles the draft | Python test files must compile before any Docker run; a test file put elsewhere lands at the new test path |
| Gates | `validation/gates.py` | G0 baseline, G1 patch applies, G2 tests pass with the patch, G3 tests fail without it, G4 no existing test breaks, G6 mutants are caught, G5 stable on repeats, all in fresh offline containers | Failures go back to the role that owns them (below); `[reference] repairs` repairs at most |
| P9 Harbor | `reference/harbor.py` | `harbor run -a nop` (0.0) and `-a oracle` (1.0, repeated for stability), plus format checks | A null reward is an infrastructure error, retried, never a verdict |
| P10 Benchmark | `reference/benchmark.py` | One job per (task, agent, attempt) in the MongoDB queue; workers lease jobs with heartbeats | Never changes validity; infra errors retried and reported apart |

## The builder panel

Give `[reference.models] author` and/or `test_writer` several models and P8 builds as a panel:

1. Every model drafts at once (the author: instruction + patch; the test writer: tests from the
   chosen instruction).
2. A draft whose edits do not apply, whose tests do not compile, or whose model fails (quota) drops out.
3. Each remaining draft is reviewed by the OTHER models (score 0-10, problems, fixes). Patch
   reviewers never see tests; test reviewers never see the patch.
4. The best mean score wins (ties: the order in the config); its model revises it once with the reviews.
5. That one package goes to the gates. Repairs are made by the chosen model; if it fails the same
   way twice, the next model of the panel takes over.

Every attempt records the drafts, scores, reviews, the chosen models and whether a revision was made
(`build_attempts[*].panel` in MongoDB and in `provenance/`).

## Repair routing

| Failure | Goes to |
|---|---|
| Patch does not apply, breaks existing tests (G4), edits test files | Author |
| Tests pass without the patch (G3), mutants survive (G6), flaky (G5), test file does not compile | Test writer |
| Tests fail WITH the patch (G2), first time | Test writer, with the gate's reason and its own failing frames (library frames are hidden: they show the patch) |
| … the tests use a name the instruction never mentions | Test writer |
| … a name or member the instruction promises does not exist | Author |
| … the instruction gives another import path than the tests used | Test writer, with the stated path |
| … the instruction gives no import path | Author, who must state one |
| … the same tests still fail after the test writer's repair | Author |
| An edit's search text was already rewritten by an earlier edit | Author, told which edit to merge it into |

The author always states the import path of every new public name; the author never sees test code,
only the failing behaviours.

## Who sees what

| Role | Gets | Never gets |
|---|---|---|
| Profiler | repo tree, README, manifests, tests | other repos |
| Scout | capability descriptions, candidate profiles | the starter's source code |
| Research agents | profile, starter code, peer docs and capabilities, upstream, web results (per lane) | peer pull-request diffs |
| Critic | idea, repo, upstream, existing task packages, peers, its example's sandbox output | the researcher's reasoning |
| Author (and patch reviewers) | approved proposal, repo, peer implementation diff when there is one | any tests |
| Test writer (and test reviewers) | instruction, repo before the change, peer tests, review edge cases | our patch, peer implementation |
| AI under test | repo at the base commit, `instruction.md` | tests, solution, proposal, provenance, the web |

Every sub-agent call is a fresh, single-message context.

## Proposal states

`DRAFTED → CHECKED → (PENDING_REVIEW) → APPROVED → BUILDING → VALIDATED → QUEUED → RUNNING → BENCHMARKED`,
with `REJECTED` (check or critic objection, user reject), `MERGED` (near-duplicate), `DROPPED`
(repairs exhausted, Harbor failed) and `RUN_FAILED` (benchmark retries exhausted). An edit sends a
proposal back to `DRAFTED`; a changed package invalidates older benchmark results (checksum).
Transitions are compare-and-set in MongoDB, so a rerun or a second worker never moves a proposal twice.

## Evidence snapshots and replay

Every research result (GitHub responses, peer profiles, web results) and every LLM reply (by role,
model and prompt hash) is saved under `<workdir>/reference/snapshots/<snapshot_id>/`. The default
snapshot id is `<repo>-<sha>`, so a rerun reuses what it already knows. `--replay <snapshot_id>` runs
with no web and no new model calls and gives the same proposal ids, states and task ids (verified on
click: a live run and its replay ended identically). A snapshot only replays with the prompts it was
recorded with: after a prompt or model change, record a new snapshot. The run id depends only on the
repo, the commit and the snapshot.

## Output

```
<out>/                       default <workdir>/reference/harbor
  <task_id>/                 exactly the five official parts: instruction.md solution/ tests/ environment/ task.toml
  provenance/<task_id>.json  sources, novelty evidence, similarity, build attempts (panel), models
  reviews/<task_id>.json     critic and user reviews, manual edits, state history
  benchmark/results.json     every benchmark result
  report.md, report.json     yield, peers, tasks, rejections, latency per stage and role, declarations
```

## Benchmark

- The AI under test always runs through `harness`: web tools are off and its Docker sandbox reaches
  only configured model API hosts. Benchmarking refuses the local sandbox, open egress, and Harbor's
  public-network agent backend. Harbor remains the package-validation runner for `nop` and `oracle`.
- `factory benchmark worker [--run RUN] [--exit-when-empty]`, `status`, `kill [--off]`,
  `enqueue --agent AGENT[:MODEL]`. A worker killed mid-job is resumed by the next one when its lease
  runs out. `[reference.benchmark] cost_cap_usd` trips the kill switch.
- Outcomes: solved (1.0), partial (between), failed (0.0 after a clean run), infra error (null:
  retried with backoff, then `RUN_FAILED`, reported apart).

## Configuration (`[reference]` in factory.toml)

```toml
[reference]
target = 5              # finite-run target; continuous mode uses this to size each research batch
workers = 4             # sub-agent calls at the same time (lanes, critics, panel drafts and reviews)
research_lanes = 3      # research agents at once: peers, starter + upstream, web (1 = one agent, every source)
research_rounds = 3     # research rounds at most while too few ideas survive the critic
repairs = 3             # repair attempts after the first build
peers_k = 3
peers = []              # manual peer override, e.g. ["fastapi/typer"]
licences = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", ...]
auto_approve = true     # false = review step (same as --review)
harbor = "required"     # required | if_available | off
harbor_repeats = 2      # oracle runs (stability)
similarity_flag = 0.5
similarity_max = 0.8    # sent back to the author / test writer as a near-copy of the peer

[reference.models]      # lists; empty roles use --model
researcher  = ["bedrock/moonshotai.kimi-k2.5"]
critic      = ["bedrock/deepseek.v3.2"]           # another family than the researcher
drafter     = ["bedrock/qwen.qwen3-coder-next"]    # the spec writer
author      = ["bedrock/us.anthropic.claude-opus-4-6-v1", "bedrock/moonshotai.kimi-k2.5", "bedrock/deepseek.v3.2"]
test_writer = ["bedrock/us.anthropic.claude-opus-4-6-v1", "bedrock/moonshotai.kimi-k2.5", "bedrock/deepseek.v3.2"]

[reference.benchmark]
agents = ["task-factory:opencode/hy4-flash-free"]
k = 1
max_retries = 3
cost_cap_usd = 5.0
```

Several models under `author` or `test_writer` turn on the [builder panel](#the-builder-panel).

## Compared with v1 (`factory produce`)

`factory produce` (one model invents ideas from a repo digest) stays as the v1 baseline for the A/B
comparison the proposal asks for; `factory run` is the default pipeline. Both write the same task
packages through the same gates.

`factory run` keeps researching by default: after each round it saves proposals, builds valid packages
and queues benchmarks, then begins another round. Stop it with Ctrl-C; the saved state resumes on the
next run. Use `--once` for one finite cycle. `[reference] research_pause_s` controls the wait between
rounds.

## Known limits

- The critic's example runner and the compile check before the gates are Python only; other
  languages rely on the critic reading the code and on the gates.
- The hidden test file name follows Python, JavaScript/TypeScript and C++ conventions; other
  ecosystems still get a Python-style name (open item).
- On click, single weaker models (qwen3-coder-next) could not get a package past G2 in four
  attempts; Claude Opus 4.6 alone got within one failing test; the three-model panel passed G0-G3
  on its first attempt (2026-11-26). No click package has passed all gates yet.
