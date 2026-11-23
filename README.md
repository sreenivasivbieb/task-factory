<div align="center">

# task-factory

**Turn any software repository into validated coding tasks for evaluating AI coding systems.**

Innodata AI Hackathon 2026 · Track 1: Coding Task Factory

`Python 3.11+` · `Docker` · `181 tests` · `Harbor-compatible`

</div>

---

## The problem in one paragraph

To find out how good an AI coding agent is, you give it a real repository and a
task ("add a bulk discount to the cart") and check whether its change works.
Writing those tasks by hand is slow, and a task is only useful if it is **fair**:
the requirements must be clear, the hidden tests must really need the new
behaviour, and nothing else in the repo may break. task-factory automates this.
It takes a repository, produces tasks, and **proves each task is valid by
running code**, so a weak or broken task never reaches the benchmark.

## How it works in 60 seconds

```mermaid
flowchart LR
    R[("Your repo<br/>path · git URL · zip")] --> L[Load & pin<br/>one commit]
    L --> I[Inspect<br/>language, tests,<br/>Docker image]
    I --> F[Find feature<br/>ideas]
    F --> G[Write task:<br/>requirements +<br/>patch + tests]
    YOU(["You: write<br/>your own task"]) --> V
    PROMPT(["Your prompt +<br/>selected model"]) --> G
    G --> V{{"Validation gates<br/>G0 to G6"}}
    V -- rejected --> X[rejections.jsonl<br/>with the reason]
    X -.->|repair hints| G
    V -- valid --> S[("Task store<br/>(pending)")]
    S --> H{"Human review"}
    H -- delete + reason --> T[".trash<br/>(restorable)"]
    H -- approve --> P[("Published tasks<br/>native · Harbor")]
    P --> B["Coding harness<br/>AI agents scored"]

    classDef done fill:#dcfce7,stroke:#16a34a,color:#14532d;
    classDef next fill:#f1f5f9,stroke:#94a3b8,color:#334155,stroke-dasharray: 4 3;
    classDef human fill:#fef9c3,stroke:#ca8a04,color:#713f12;
    class L,I,G,V,X,S,T,P,B done;
    class F next;
    class YOU,PROMPT,H human;
```

<sub>Green = implemented. Yellow = human input or review. Dashed = planned automatic feature discovery.</sub>

1. **Load** copies the repo into a work folder and pins one exact commit (a 40-character SHA). The original is never touched.
2. **Inspect** works out the language, how to install it and how to run its tests, builds a Docker image, and runs the existing test suite once.
3. **Write tasks**: each task has a requirements file, a reference implementation as a diff, and hidden tests. You can write a task yourself or generate a draft from a user prompt with the selected model.
4. **Validate** runs seven gates in fresh, network-less containers. A task passes only if every gate passes, including the ones you write yourself.
5. **Review**: a person looks at every valid task and approves or deletes it. Deletions need a reason and are disclosed.
6. **Publish** only the approved tasks, in our format or in [Harbor](https://github.com/laude-institute/harbor)'s format.
7. **Harness**: assign tasks to a test model (the Task Factory engine with NVIDIA, TokenRouter, Zen or AWS Bedrock Qwen; Claude Code; Codex; Aider; mini-swe-agent; or any command). It solves them in a sandbox, the hidden tests grade it, and every passing solution becomes a new git branch.

### Task Factory terminal

Task Factory ships its own terminal: a full coding-agent TUI with a command
palette, sessions, model management, agents, repository tools, permissions and
plugins, plus the Task Factory commands. It is a separate program with its own
settings, logins and sessions, so it never starts or changes another coding
tool installed on the same machine.

Build the engine, then install the launcher:

```bash
python3 scripts/build_terminal.py "../Task Factory Engine"   # fetches the pinned source on first run
uv tool install --editable .
```

| What | Where |
|---|---|
| Engine | `~/.local/share/task-factory/bin/task-factory-engine` |
| Settings, logins, sessions | `~/.config/task-factory`, `~/.local/share/task-factory` |
| Project config | `task-factory.json`, `.task-factory/` |
| Environment settings | `TASK_FACTORY_*`, e.g. `TASK_FACTORY_CONFIG_CONTENT` |
| `uninstall` | removes only the Task Factory cache |

Log in to model providers inside the terminal with `/connect`. The engine's
third-party license is installed next to it as `THIRD-PARTY-LICENSE`.

Run `task-factory` inside any project, or use `task-factory chat /path/to/project`.
`task-factory terminal --help` shows the engine's own commands. Run the build
script again to rebuild the engine.

#### Task Factory commands in the terminal

Add a model with `/connect` (provider login or key) and choose it with `/models`;
then, inside any git project:

| Command | What it does |
| --- | --- |
| `/setup` | Pins the project's committed HEAD and builds its Docker test environment (once; again when HEAD moves). |
| `/generate-task <request>` | The selected model drafts requirements, a reference change and hidden tests in two separate requests, in its own **read-only** session ("Generate task: …" in `/sessions`); the gates G0-G6 must pass before it is saved as pending and linked to the session you asked from. |
| `/tasks` | Lists the project's tasks. Pick one to show it, run it, approve it, or delete it with a reason. |
| `/run-task` | Pick tasks (or "all approved"), pick a model (the selected one is first), and watch it solve them in Docker, live. Passing solutions become git branches. |
| `/results` | Runs → attempts → one page with the verdict, hidden-test counts, the **agent's whole session** (what it said, every tool call and its output, tokens), the graded diff and the test output. |
| `/benchmark` | Tasks × models: who solved what, the design doc's difficulty tag (solved by all attempts, some or none; "none ⚠" means check it is solvable), resolve rate, average time and tokens per model, and why attempts failed. `o` opens any attempt. |

The engine's own commands that have nothing to do with tasks (`/pair`, `/stats`,
`/usage`, `/update`, `/init`, `/review`, `/goal`, `/mcps`, `/plugins`, `/skills`,
`/worktrees`, …) are hidden from `/` completion and the command palette; the list is
`HIDDEN_COMMANDS` in `scripts/build_terminal.py` (rebuild the engine after changing it).

Long commands stream their progress into a Task Factory page (`esc` goes back,
`↑ ↓ pgup pgdn home end` scroll). Each project keeps its tasks, runs and solution
branches in one folder outside the repo,
`~/.local/state/task-factory/projects/<repo>-<hash>/`, so the repo is never
changed; the same folder works with the CLI (`factory tasks list --workdir …`).
A model-generated task is recorded with that model as its author (never as
human-authored), with the request and the session it came from.

Every session's sidebar shows a Task Factory panel:

```text
Task Factory
toyshop @ 5cbaceb ✓
Tasks (2)
✓ 1 approved · ⏳ 1 review · ✗ 0
not run yet  1
AI performance
resolved 100% (3/3 attempts)
solved by all 2 · some 0 · none 0
By model
big-pickle            1/1 31s 24k
deepseek-v4.1-flash   2/2 19s 12k
This session
1 generated · 1 valid
✓ toyshop-5cbaceb-add-cart-total-items-002
Next: 1 task(s) wait for review: /tasks
```

Agent errors (an exhausted quota, a bad login) are shown apart and never counted
as a model failing a task. Zen's free models work for every command.

`factory generate` also supports direct use with a compatible model provider.

## What makes a task "valid"

A task is valid only when the tests prove five things. Each gate runs in its own
fresh container. The first gate that fails rejects the task, with an exact code.

```mermaid
flowchart TD
    D[Task draft] --> G0
    G0["G0 · Baseline<br/>the original repo's tests pass"] -->|ok| G1
    G1["G1 · Patch applies<br/>reference diff applies cleanly,<br/>touches no test or test-config file"] -->|ok| G2
    G2["G2 · Feature works<br/>new tests pass WITH the patch"] -->|ok| G3
    G3["G3 · Tests need the feature<br/>new tests fail WITHOUT the patch"] -->|ok| G4
    G4["G4 · No regressions<br/>every old test still passes"] -->|ok| G6
    G6["G6 · Tests are strong<br/>deliberately wrong patches are caught"] -->|ok| G5
    G5["G5 · Stable<br/>same result when repeated"] -->|ok| OK(["VALID → exported"])

    G0 -->|fail| R0[BASELINE_FAIL]
    G1 -->|fail| R1["PATCH_APPLY_FAIL /<br/>SOLUTION_TOUCHES_PROTECTED"]
    G2 -->|fail| R2[FEATURE_TESTS_FAIL_WITH_PATCH]
    G3 -->|fail| R3[FEATURE_TESTS_PASS_WITHOUT_PATCH]
    G4 -->|fail| R4[REGRESSION]
    G6 -->|fail| R6[NEG_CONTROL_PASSED]
    G5 -->|fail| R5[FLAKY]

    classDef gate fill:#eff6ff,stroke:#3b82f6,color:#1e3a8a;
    classDef bad fill:#fef2f2,stroke:#ef4444,color:#7f1d1d;
    classDef good fill:#dcfce7,stroke:#16a34a,color:#14532d;
    class G0,G1,G2,G3,G4,G5,G6 gate;
    class R0,R1,R2,R3,R4,R5,R6 bad;
    class OK good;
```

G3 is the gate most generators skip: a "feature test" that already passes on
the old code tests nothing. G6 goes further. It breaks the reference patch on
purpose (flips `>=` to `>`, deletes an error branch, hard-codes the expected
value…) and demands that the hidden tests notice. If a wrong patch still passes,
the tests are too weak and the task is rejected.

**Which areas fit?** Anything whose behaviour a test can check. One task per area
was written for the toy repo and passed every gate with 10 repeats and all six
mutation operators: a **security** fix (receipt line injection + a SKU
allowlist; an AI model solved it), a **scalability** change (a tag index whose
cost stays flat from 1k to 100k products, measured as a timing ratio so it holds
on any machine) and an **API design** task (pluggable coupon rules). Out of
reach: things that need a live network or many machines, pure refactors (G3
needs a failing test) and visual or architecture design (needs a judge, not a
test). Details in [design §9](docs/design.md#what-kinds-of-tasks-fit).

## A real example, end to end

The bundled toy repo `toyshop` is a tiny shop library. One task asks for a bulk
discount. Here is what the factory produces and checks.

<table>
<tr><th>Requirements (visible to the solver)</th><th>Reference patch (hidden)</th></tr>
<tr valign="top"><td>

> Change `toyshop.pricing.line_total(price_cents, qty)` so that a cart line
> with **10 or more** items gets **10% off** that line.
>
> - Below 10 items the line total is unchanged.
> - The 10% is rounded half up to a whole cent.
> - `subtotal(cart)` and everything built on it use the discounted totals.

</td><td>

```diff
+BULK_MIN_QTY = 10
+BULK_PERCENT = 10

 def line_total(price_cents, qty):
-    return price_cents * qty
+    amount = price_cents * qty
+    if qty >= BULK_MIN_QTY:
+        amount -= (amount * BULK_PERCENT + 50) // 100
+    return amount
```

</td></tr>
<tr><th>Hidden test (one of three)</th><th>Why G6 matters</th></tr>
<tr valign="top"><td>

```python
def test_bulk_discount_boundary():
    assert line_total(100, 9) == 900
    assert line_total(100, 10) == 900
```

</td><td>

The mutant `if qty > BULK_MIN_QTY:` looks right but
is off by one. With 10 items it gives `1000`, so the
boundary test fails and the mutant is **caught**. A
test that only checked 20 items would let it through,
and that task would be rejected with
`NEG_CONTROL_PASSED`.

</td></tr>
</table>

The fixture set has one good task and one deliberately broken task per reject
code. Running them gives exactly the expected verdicts:

```text
$ factory validate --tasks tests/fixtures/tasks/python
rejected  FEATURE_TESTS_FAIL_WITH_PATCH      toyshop-a471ba4-bulk-discount-wrong-threshold-001
rejected  FLAKY                              toyshop-a471ba4-bulk-discount-flaky-test-002
rejected  FEATURE_TESTS_PASS_WITHOUT_PATCH   toyshop-a471ba4-bulk-discount-tests-check-old-be-003
rejected  PATCH_APPLY_FAIL                   toyshop-a471ba4-bulk-discount-stale-patch-004
rejected  REGRESSION                         toyshop-a471ba4-bulk-discount-breaks-money-forma-005
rejected  SOLUTION_TOUCHES_PROTECTED         toyshop-a471ba4-bulk-discount-reference-also-edi-006
rejected  NEG_CONTROL_PASSED                 toyshop-a471ba4-bulk-discount-weak-tests-007
valid                                        toyshop-a471ba4-bulk-discount-on-cart-lines-008
1/8 valid
```

The same set exists for a Node.js repo (`textkit`), with the same results.

## What a task package looks like

```mermaid
flowchart LR
    subgraph PKG["toyshop-a471ba4-bulk-discount-on-cart-lines-007/"]
        direction TB
        A["instruction.md"]
        B["task.toml"]
        C["solution/solution.diff"]
        D["tests/files/… + run_tests.sh"]
        E["environment/Dockerfile + env.json + repo.tar"]
        F["validation/report.json + logs/"]
        G["checksums.txt"]
    end
    S(["AI solver"]) -->|sees only| A
    J(["Judge / harness"]) -->|uses| C & D & E
    Rv(["Reviewer"]) -->|audits| B & F & G

    classDef visible fill:#dcfce7,stroke:#16a34a,color:#14532d;
    classDef hidden fill:#fef9c3,stroke:#ca8a04,color:#713f12;
    classDef meta fill:#eff6ff,stroke:#3b82f6,color:#1e3a8a;
    class A visible;
    class C,D hidden;
    class B,E,F,G meta;
```

| File | What it holds | Who sees it |
| --- | --- | --- |
| `instruction.md` | The requirements | the solver (nothing else) |
| `task.toml` | Repo, pinned commit, test IDs (`fail_to_pass`, `pass_to_pass`), tags, validation summary | judge |
| `solution/solution.diff` | Reference implementation | hidden |
| `tests/` | Hidden feature tests and a script that runs them | hidden |
| `environment/` | Dockerfile, config and `repo.tar` (the pinned commit): `docker build environment/` rebuilds the exact sandbox on any machine, base image pinned by digest | judge |
| `validation/` | Gate-by-gate report and the raw logs of every run | reviewer |
| `checksums.txt` | SHA-256 of every task file; one changed byte fails verification | reviewer |

`factory validate --tasks <package>` rebuilds everything from these files and
runs all gates again, so the evidence can be checked by someone who never saw
the original run, and without access to the original repo.

## Human in the loop

Automatic checks prove a task is *valid*. Only a person can say it is *good*:
clear wording, a realistic change, worth an agent's time. So every valid task
lands in a **task store** as `pending`, and nothing is published until a
reviewer approves it.

```mermaid
stateDiagram-v2
    [*] --> pending : passes all gates
    pending --> approved : factory review → a
    pending --> deleted : d + reason (moved to .trash)
    approved --> deleted : d + reason
    deleted --> pending : factory tasks restore
    approved --> pending : edited (re-validated, disclosed)
    approved --> published : factory tasks publish
```

Review one task at a time in the terminal:

```text
$ factory review
1 pending task(s) to review · reviewer: you
──────────────────────────────── task 1/1 ────────────────────────────────
╭──────────── toyshop-a471ba4-bulk-discount-on-cart-lines-007 ────────────╮
│ status       pending                                                    │
│ repo         toyshop @ a471ba47d091  (python)                           │
│ kind / tags  feature  ·  validation rule, data transformation           │
│ touches      toyshop/pricing.py                                         │
│ tests        3 fail_to_pass · 28 pass_to_pass                           │
│ mutants      2/2 caught (score 1.0)                                     │
│ gates         G0   G1   G2   G3   G4   G6   G5                          │
╰─────────────────────────────────────────────────────────────────────────╯
╭─────────── instruction.md  (the only file the solver sees) ────────────╮
│ Bulk discount on cart lines …                                           │
╰─────────────────────────────────────────────────────────────────────────╯
a=approve  d=delete  s=skip  i=instruction  p=patch  t=tests  g=gates  e=edit  q=quit
choice>
```

| You want to… | Command |
| --- | --- |
| See every task and its status | `factory tasks list [--status pending]` |
| Read one task (summary, instruction, colored diff, tests, gate logs) | `factory tasks show TASK_ID [--section patch]` |
| Step through pending tasks and decide | `factory review` |
| Approve | `factory tasks approve TASK_ID [--note "…"]` |
| Delete (soft, reason required) | `factory tasks delete TASK_ID --reason "requirements are vague"` |
| Undo a delete | `factory tasks restore TASK_ID` |
| Fix a task by hand | `factory tasks edit TASK_ID` then `--apply --reason "…"` (re-validated, marked as a manual edit) |
| Write your own task | `factory tasks new my-task`, fill it in, `factory tasks add workdir/drafts/my-task` |
| Publish approved tasks | `factory tasks publish --out submission/ [--format harbor]` |
| See what humans changed | `factory tasks summary` (counts, deletions with reasons, manual edits) |

Every decision is appended to `tasks/.review/log.jsonl` with who, when and why.
`publish` writes `review_summary.json` next to the tasks, so deletions and manual
edits are disclosed, as the organizers require. A task you write yourself goes
through all seven gates like any other and is marked `human (your name)`.

## Coding harness

The harness answers the question the tasks exist for: *how well does this
model do?* You assign a task to a test model; a worker gives the model a fresh
sandbox at the repo's base commit with every package requirement already
installed, lets it implement the task, then runs the hidden tests. **If every
test passes, the model's solution becomes a new git node**: a commit on its own
branch.

```mermaid
flowchart LR
    A["factory tasks assign TASK<br/>--model nvidia/… · tokenrouter/… ·<br/>Zen · amazon-bedrock/qwen…"] --> W["factory harness work"]
    W --> S1["Agent sandbox<br/>base commit + all deps installed<br/>+ agent CLI · network on"]
    S1 -->|"instruction.md only"| M(["LLM implements<br/>the task"])
    M -->|"its diff"| S2["Grading sandbox<br/>fresh · network off<br/>undo test/config changes · add hidden tests"]
    S2 --> Q{"all hidden +<br/>existing tests pass?"}
    Q -- yes --> N[("new git node<br/>branch ai/AGENT-MODEL/TASK")]
    Q -- no --> F["result recorded<br/>task flagged for review"]

    classDef good fill:#dcfce7,stroke:#16a34a,color:#14532d;
    classDef bad fill:#fef2f2,stroke:#ef4444,color:#7f1d1d;
    class N good;
    class F bad;
```

### Assign a task to a test model and run it

```sh
factory tasks assign TASK_ID --agent task-factory --model nvidia/qwen/qwen3-coder-480b-a35b-instruct
factory tasks assignments                    # what is waiting
factory harness work                         # run every open assignment
factory tasks assignments --all              # results, with the branch of each passing solution
git -C workdir/solutions/toyshop log --oneline --all
```

A passing solution becomes a commit on a new branch,
`ai/<agent>-<model>/<task_id>`, and its message carries the evidence:

```text
Task: <task_id>
Base commit: <40-character SHA>
Agent: task-factory · Model: <model> · Attempt 1
Hidden tests: 3/3 fail_to_pass passed, 28/28 pass_to_pass passed
```

If the model edits an existing test file, the harness puts that file back
before grading, records it under `tests_tampered`, and commits only the source
change to the branch.

### Which models

The default agent, `task-factory`, is the Task Factory engine itself, copied
into the sandbox image (no download inside the container). It talks to many
providers. Pass the model as `provider/model`:

| Provider | Example `--model` | Credentials |
| --- | --- | --- |
| Zen (has free models, rate limited; IDs keep the provider's own prefix) | `opencode/deepseek-v4.1-flash-free`, `opencode/qwen3.6-plus` | none for free models, or `TASK_FACTORY_API_KEY` |
| NVIDIA (build.nvidia.com, free credits) | `nvidia/<model id from task-factory terminal models nvidia>` | `NVIDIA_API_KEY` |
| TokenRouter | `tokenrouter/<model id>` | `TOKENROUTER_API_KEY` |
| AWS Bedrock (Qwen) | `amazon-bedrock/qwen.qwen3-coder-480b-a35b-v1:0` | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + `AWS_REGION`, or `AWS_BEARER_TOKEN_BEDROCK` |

The harness forwards only the provider's environment variables into the
sandbox, and the container is removed after the attempt. Run
`task-factory terminal models` to see the exact model IDs your accounts can
use. A model the provider rejects ("model not found", a used-up free quota) is
reported as `agent_error` and the assignment stays open.

### Other commands and agents

```sh
factory harness agents                                   # list agents
factory harness run --agent oracle                       # sanity check: must resolve 100%
factory harness run --agent noop                         # sanity check: must resolve 0%
factory harness run --agent task-factory --model … --k 3 # without assignments: all tasks in the store
factory harness run --agent command --cmd 'my-agent --task-file {instruction_file}'
```

| Agent | What it runs | State |
| --- | --- | --- |
| `task-factory` | `task-factory-engine run --auto --standalone -m <model>` (the bundled engine) | **verified with a real model** (`opencode/deepseek-v4.1-flash-free` resolved the toy task in 44 s, branch created) |
| `oracle` | applies the reference patch | verified |
| `noop` | changes nothing | verified |
| `command` | any shell command you give it | verified |
| `claude-code` | `claude -p … --output-format json` ([repo](https://github.com/anthropics/claude-code)) | preset, not yet run with a real key |
| `codex` | `codex exec …` ([repo](https://github.com/openai/codex)) | preset, not yet run with a real key |
| `aider` | `aider --message-file …` ([repo](https://github.com/Aider-AI/aider)) | preset, not yet run with a real key |
| `mini-swe-agent` | `mini -y --exit-immediately …`, LiteLLM models such as `bedrock/qwen…` ([repo](https://github.com/SWE-agent/mini-swe-agent)) | preset, not yet run with a real key |
| your own | `[harness.agents.NAME]` in `factory.toml` | |

Each attempt is graded into one category: `resolved`, `wrong_behaviour`,
`regression`, `build_error`, `no_change`, `agent_error` (the agent failed, for
example a bad model ID), `timeout` or `patch_apply_fail`. The summary reports
**resolved@k**, pass rate, time and cost (when the agent reports it), and a
difficulty per task: *easy* (every attempt solved it), *medium* (some) or
*unsolved*. An unsolved task gets a note in the review log, because it may be
unsolvable and deserves a human look.

A failure of the *setup* is not a verdict on the task. If every attempt ends in
`agent_error` (an exhausted free quota, a wrong model ID, a missing key), the
task's difficulty is *error*, no "unsolved" note is written, and `harness work`
leaves the assignment open to retry. A run stops after two agent errors in a
row instead of failing every remaining task the same way.

**Cheating is blocked.** The agent's sandbox never contains the hidden tests or
the reference solution (checked before the agent starts). API keys never
appear on a command line or in logs. Before grading, every change the agent made
to tests or to files that steer the test run (`conftest.py`, `pytest.ini`,
`pyproject.toml`, `package.json`, jest/vitest config, …) is undone: edited and
deleted files are restored, new ones removed. A review found that a six-line
`conftest.py` marking every test "passed" used to count as a solution; it is now
caught by our harness and by the Harbor verifier, and tests keep it that way.

### Harbor compatibility

[Harbor](https://github.com/laude-institute/harbor) (Apache-2.0, from the
Terminal-Bench team) is a popular framework for running agents on tasks.
`factory tasks publish --format harbor` writes each task in Harbor's layout
(`environment/` with the pinned repo, `solution/solve.sh`, `tests/test.sh`
writing `/logs/verifier/reward.txt`), so the same tasks run on Harbor's built-in
agents too. The verifier gives reward 1 only when every hidden test actually ran
and passed and the full suite passes. Checked with the real Harbor CLI:

```text
$ harbor run -p submission/ -a oracle        $ harbor run -p submission/ -a nop
  Trials 1 · Exceptions 0 · Mean 1.000         Trials 1 · Exceptions 0 · Mean 0.000
```

## Quickstart

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and Docker (Podman works too).

```sh
git clone https://github.com/sreenivasivbieb/task-factory && cd task-factory
uv sync
uv run pytest                      # 181 tests; Docker tests skip with a message if Docker is missing
```

Run the whole flow on the bundled toy repo:

```sh
cp -r tests/fixtures/toy_python_repo /tmp/toyshop
git -C /tmp/toyshop init -q && git -C /tmp/toyshop add -A && git -C /tmp/toyshop commit -qm init

uv run factory load --repo /tmp/toyshop            # copy and pin the repo
uv run factory inspect                             # detect env, build image, run baseline (28 passed)
uv run factory validate --tasks tests/fixtures/tasks/python   # 1/8 valid, 7 rejected with exact codes
uv run factory export                              # valid tasks go to workdir/tasks as pending
uv run factory review                              # approve or delete them, one by one
uv run factory tasks publish --out submission/     # only approved tasks
uv run factory validate --tasks submission/        # re-verify the packages from disk alone
uv run factory harness run --agent oracle          # harness sanity check: 100% resolved
```

## Commands

| Command | What it does | State |
| --- | --- | --- |
| `factory load --repo SRC [--commit SHA]` | Copy and pin the repo; writes `repo_spec.json` | ✅ |
| `factory inspect` | Detect environment, build image, run baseline; writes `env_config.json` | ✅ |
| `factory validate --tasks DIR` | Run the gates on task drafts or re-verify packages | ✅ |
| `factory export` | Write every valid task into the task store as `pending` (never overwrites a package already there; `--force` does) | ✅ |
| `factory tasks list · show · approve · delete · restore · edit · new · add · publish · summary` | Human review and authoring | ✅ |
| `factory review` | Interactive review, one task at a time | ✅ |
| `factory tasks assign · unassign · assignments` | Assign tasks to a test model | ✅ |
| `factory harness agents · run · work` (alias `factory bench`) | Run AI coding agents on tasks, score them, branch passing solutions | ✅ |
| `factory ideas` · `generate` · `report` · `run` | Ideas mined from the repo (TODO/FIXME, stubs, skipped tests, bug-fix history), LLM generation, evidence report, one-shot pipeline | 🔜 M6–M10 (labelled "planned" in `--help`) |

Flags on every command: `--workdir`, `--config`, `--seed`, `--force`, `--dry-run`,
`--verbose`, `--backend docker|local`. Each step saves JSON under `workdir/` and
skips work that is already done, so a run can be stopped and resumed.

Exit codes: `0` success · `1` pipeline failure · `2` bad usage or config ·
`3` environment problem (for example Docker missing) · `4` ran but no valid tasks.

## Supported languages

| Language | Detected from | Test runner | State |
| --- | --- | --- | --- |
| Python | `pyproject.toml`, `setup.py`, `requirements.txt` | pytest (also runs unittest suites) | end to end |
| JavaScript / TypeScript | `package.json` | node:test, Jest, Vitest | end to end (node:test) |
| Java | `pom.xml`, `build.gradle` | Maven Surefire, Gradle | report parser only |
| Go | `go.mod` | `go test -json` | report parser only |
| Rust | `Cargo.toml` | `cargo test` | report parser only |

Adding a language means adding one inspector rule and, if needed, one report parser.

## Progress

```mermaid
flowchart LR
    M0[M0 Skeleton] --> M1[M1 Sandbox] --> M2[M2 Loader +<br/>inspector] --> M4[M4 Validation<br/>gates]
    M1 --> M3[M3 Test runner] --> M4
    M4 --> M5[M5 Packages] --> HR[Human review +<br/>own tasks]
    M5 --> HN[Coding harness +<br/>Harbor export]
    M4 --> M6[M6 LLM task<br/>generator]
    M2 --> M7[M7 Feature<br/>source]
    M6 --> M8[M8 Variation] --> M9[M9 Report]
    HR & M7 & M9 --> M10[M10 Wiring] --> M11[M11 Real runs] --> M12[M12 Demo]

    classDef done fill:#dcfce7,stroke:#16a34a,color:#14532d;
    classDef next fill:#f1f5f9,stroke:#94a3b8,color:#334155,stroke-dasharray: 4 3;
    class M0,M1,M2,M3,M4,M5,HR,HN done;
    class M6,M7,M8,M9,M10,M11,M12 next;
```

The coding harness was planned as an optional extra in M12; it was pulled
forward because it shows whether the tasks are fair (the oracle must score
100%, an empty agent 0%) and lets people review tasks with real agent results.

## Learn more

- **[docs/design.md](docs/design.md)**: architecture, data flow, every component, how the gates and mutants work, and the reasoning behind them.
- **[docs/decisions.md](docs/decisions.md)**: every assumption, deviation and known limitation, recorded as the build went.

## Dependencies and licenses

| Dependency | Used for | License |
| --- | --- | --- |
| pydantic | data models | MIT |
| rich | terminal review screens and tables | MIT |
| pytest (dev only) | tests | MIT |
| hatchling (build only) | packaging | MIT |

Everything else (CLI, TOML writer, report parsers, mutation engine, harness) uses only the Python standard library.
Agents and Harbor are optional external tools: they are installed into sandboxes or run by you, never
imported. Their licenses: Harbor Apache-2.0, Aider Apache-2.0, mini-swe-agent MIT, Codex CLI Apache-2.0,
Claude Code under Anthropic's terms.
