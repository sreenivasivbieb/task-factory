# CLI reference

- `factory` — the Python CLI (also installed as `task-factory`).
- `node swe/cli.ts` — the SWE miner (`swe` inside the container).
- Generated from the argument parser; run `factory COMMAND --help` for the same text.

## Options on every `factory` command

| Option | Meaning |
|---|---|
| `--workdir DIR` | Work folder (default: `workdir/`, or `workdir` in `factory.toml`) |
| `--config FILE` | Path to `factory.toml` |
| `--seed N` | Seed for anything random |
| `--no-cache` | Ignore cached model responses and images |
| `--force` | Rerun a step that already finished |
| `--dry-run` | Show what would run without running it |
| `--verbose`, `-v` | More logging |
| `--backend docker|local` | Sandbox backend (`local` = no isolation, development only) |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Pipeline failure |
| `2` | Bad usage or configuration |
| `3` | Environment problem (for example, Docker missing) |
| `4` | Ran, but no valid tasks |

## `factory` commands

### `factory load`

- load and pin the repo; writes RepoSpec

| Option | Meaning |
|---|---|
| `--repo` `REPO` | local path, git URL or zip |
| `--commit` `COMMIT` | commit SHA (default HEAD) |

### `factory inspect`

- detect environment, build image, run baseline

### `factory generate`

- draft and validate a task using a configured model

| Option | Meaning |
|---|---|
| `--prompt` `PROMPT` | describe the coding task |
| `--model` `MODEL` | model ID (defaults to FACTORY_MODEL) |
| `--file` | repo-relative context file (repeatable) |

### `factory chat`

- launch the Task Factory terminal

### `factory terminal`

- use the bundled terminal engine’s native commands

### `factory validate`

- run the gates on task drafts, or re-verify packages

| Option | Meaning |
|---|---|
| `--tasks` `TASKS` | a package folder, a folder of packages, or a draft folder |

### `factory export`

- write valid tasks as packages into the task store

| Option | Meaning |
|---|---|
| `--out` `OUT` | folder (default: the task store) |

### `factory tasks list`

- table of tasks with their review status

| Option | Meaning |
|---|---|
| `--status` `all|pending|approved|deleted` | Show only tasks with this status |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks show TASK_ID`

- show one task: summary, instruction, patch, tests, gates

| Option | Meaning |
|---|---|
| `--section` `all|summary|instruction|patch|tests|gates` | Show one part only |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks approve TASK_IDS`

- approve a task for publishing

| Option | Meaning |
|---|---|
| `--note` `NOTE` | Free-text note kept in the review log |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks delete TASK_IDS`

- move a task to the trash (restorable); a reason is required

| Option | Meaning |
|---|---|
| `--reason` `REASON` | why it is not good enough (disclosed in the report) |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks restore TASK_IDS`

- bring a deleted task back as pending

| Option | Meaning |
|---|---|
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks new NAME`

- create a draft folder to write your own task

| Option | Meaning |
|---|---|
| `--title` `TITLE` | Task title |
| `--kind` `feature|bugfix|behavior_change` | Task kind |
| `--tags` `TAGS` | comma-separated tags |
| `--dir` `DIR` | where to create it (default: <workdir>/drafts) |

### `factory tasks add DRAFT`

- validate your own draft with every gate and add it to the store

| Option | Meaning |
|---|---|
| `--author` `AUTHOR` | Recorded as the task's author (default: your git name) |
| `--pending` | add as pending instead of approved |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks edit TASK_ID`

- edit a package: unpack to a draft, then --apply with a reason

| Option | Meaning |
|---|---|
| `--apply` | re-validate the edited draft and replace the package |
| `--reason` `REASON` | what was changed and why (required with --apply) |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks publish`

- copy approved tasks to a submission folder

| Option | Meaning |
|---|---|
| `--out` `OUT` | Destination folder |
| `--format` `native|harbor` | Package layout (default `native`) |
| `--include-pending` | Also publish pending tasks |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks summary`

- counts, deletions with reasons, manual edits

| Option | Meaning |
|---|---|
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks assign TASK_IDS`

- assign tasks to a test model (run them with: factory harness work)

| Option | Meaning |
|---|---|
| `--agent` `AGENT` | agent (default: harness.default_agent = task-factory) |
| `--model` `MODEL` | model, e.g. nvidia/..., tokenrouter/..., amazon-bedrock/qwen... |
| `--note` `NOTE` | Free-text note kept in the review log |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks unassign TASK_IDS`

- cancel open assignments

| Option | Meaning |
|---|---|
| `--agent` `AGENT` | Agent name |
| `--model` `MODEL` | `provider/model` |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory tasks assignments`

- list assignments (open by default)

| Option | Meaning |
|---|---|
| `--all` | include finished ones with their result |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory review`

- interactive review: approve or delete tasks one by one

| Option | Meaning |
|---|---|
| `--status` `pending|approved|all` | Which tasks to review |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory harness agents`

- list available agents

### `factory harness run`

- run an agent on tasks

| Option | Meaning |
|---|---|
| `--agent` `AGENT` | oracle, noop, task-factory, claude-code, codex, aider, mini-swe-agent, command, or one from [harness.agents] |
| `--model` `MODEL` | `provider/model` |
| `--k` `K` | attempts per task (default: harness.k = 1) |
| `--workers` `WORKERS` | attempts at the same time (default: harness.workers = 1) |
| `--cmd` `CMD` | shell command for --agent command; use {instruction_file} |
| `--tasks` `TASKS` | a package or folder of packages (default: store) |
| `--task` | only this task ID (repeatable) |
| `--status` `approved|pending|all` | which tasks from the store (default: all not deleted) |
| `--timeout` `TIMEOUT` | agent timeout in seconds |
| `--use-host-auth` | copy your stored agent login (that provider only) into the sandbox |
| `--no-branch` | do not create git branches for passing solutions |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory harness work`

- run every open assignment, record results, branch passing solutions

| Option | Meaning |
|---|---|
| `--k` `K` | attempts per task (default: harness.k = 1) |
| `--workers` `WORKERS` | attempts at the same time (default: harness.workers = 1) |
| `--timeout` `TIMEOUT` | agent timeout in seconds |
| `--use-host-auth` | copy your stored agent login (that provider only) into the sandbox |
| `--no-branch` | do not create git branches for passing solutions |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

### `factory db sync`

- write every record of the work folders to MongoDB (idempotent)

| Option | Meaning |
|---|---|
| `--path` | a work folder or SWE store (repeatable; default: every one this machine knows) |

### `factory db status`

- documents per collection

### `factory images prune`

- remove agent images (rebuilt on demand); --all also task images

| Option | Meaning |
|---|---|
| `--all` | also every factory-* task image (rebuilt from packages) |

### `factory bench` (alias of `harness run`)

- alias of 'harness run'

| Option | Meaning |
|---|---|
| `--agent` `AGENT` | oracle, noop, task-factory, claude-code, codex, aider, mini-swe-agent, command, or one from [harness.agents] |
| `--model` `MODEL` | `provider/model` |
| `--k` `K` | attempts per task (default: harness.k = 1) |
| `--workers` `WORKERS` | attempts at the same time (default: harness.workers = 1) |
| `--cmd` `CMD` | shell command for --agent command; use {instruction_file} |
| `--tasks` `TASKS` | a package or folder of packages (default: store) |
| `--task` | only this task ID (repeatable) |
| `--status` `approved|pending|all` | which tasks from the store (default: all not deleted) |
| `--timeout` `TIMEOUT` | agent timeout in seconds |
| `--use-host-auth` | copy your stored agent login (that provider only) into the sandbox |
| `--no-branch` | do not create git branches for passing solutions |
| `--store` `STORE` | task store folder (default: <workdir>/tasks) |

## SWE miner (`node swe/cli.ts`)

### `node swe/cli.ts mine URL`

- Mine merged pull requests that close an issue into tasks.

| Option | Meaning |
|---|---|
| `--max N` | Candidates to process (default 10) |
| `--scan N` | Pull requests to look at, newest first (default 200) |
| `--since DATE` | Only PRs created after this date |
| `--out DIR` | Store folder (default `~/.local/state/task-factory/swe/<owner>-<repo>`) |
| `--retry` | Process PRs dropped before again |
| `--memory SIZE` | Sandbox memory limit (default `2g`) |
| `--workers N` | PRs processed at the same time (default 1) |
| `--json` | JSON events on stdout (terminal protocol) |

### `node swe/cli.ts funnel URL`

- Print how many PRs reached each step, and the drop reasons. Takes `--out`.

### `node swe/cli.ts export URL`

- Rewrite `swe-bench.jsonl` from the store's packages. Takes `--out`.

## Helpers

| Command | What it does |
|---|---|
| `python -m factory.records sync DIR` | Write one folder's records to MongoDB |
| `python -m factory.records put COLLECTION` | Upsert JSON documents (one per line, with `_id`) from stdin |
| `scripts/factory-docker COMMAND…` | Run any command in the factory container |
| `python3 scripts/build_terminal.py ENGINE_DIR` | Build the terminal engine from pinned source |
