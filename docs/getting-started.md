# Getting started

## Requirements

| Tool | Needed for | Version |
|---|---|---|
| Python + [uv](https://docs.astral.sh/uv/) | Everything | Python 3.11+ |
| Docker (or Podman) | Sandboxes, environments | Any recent |
| git | Pinning repositories, solution branches | Any recent |
| Node | SWE miner, TypeScript tests | 22.18+ (runs TypeScript directly) |
| [GitHub CLI](https://cli.github.com) | SWE miner (reads pull requests) | Any recent, logged in |
| Bun | Building the Task Factory terminal | Any recent |

## Install

```sh
git clone https://github.com/sreenivasivbieb/task-factory && cd task-factory
uv sync
```

- Check the install:

```sh
uv run factory --help
uv run pytest -m "not docker"      # seconds
uv run pytest                      # full suite, needs Docker
```

## Your first task (command line)

- Make a toy repository:

```sh
cp -r tests/fixtures/toy_python_repo /tmp/toyshop
git -C /tmp/toyshop init -q && git -C /tmp/toyshop add -A && git -C /tmp/toyshop commit -qm init
```

- Pin it and build its environment:

```sh
uv run factory load --repo /tmp/toyshop     # copies the repo, pins the commit
uv run factory inspect                      # detects Python + pytest, builds the image, runs 28 baseline tests
```

- Add a hand-written task (every gate runs):

```sh
uv run factory tasks add tests/fixtures/tasks/python/good_bulk_discount
uv run factory tasks list
uv run factory tasks show TASK_ID --section patch
```

- Check the task with the built-in agents:

```sh
uv run factory harness run --agent oracle   # applies the reference: must resolve 100%
uv run factory harness run --agent noop     # changes nothing: must resolve 0%
```

- Publish it:

```sh
uv run factory tasks publish --out submission/              # native format
uv run factory tasks publish --out harbor/ --format harbor  # Harbor format
uv run factory validate --tasks submission/                 # re-verify from the files alone
```

- Next: [Gated pipeline](gated-pipeline.md) explains every step.

## Use the terminal

- Build the engine and install the launcher:

```sh
python3 scripts/build_terminal.py "../Task Factory Engine"   # fetches the pinned source on first run
uv tool install --editable .
```

- Open it in any git project:

```sh
cd /path/to/your/repo
task-factory
```

- Typical session:
  - `/connect` and `/models` — log in to a provider, choose a model
  - `/setup` — pin the project and build its environment
  - `/generate-task add a bulk discount` — the model drafts a task; the gates check it
  - `/tasks` — review, approve or delete
  - `/run-task` — solve tasks with a model in Docker
  - `/solutions` — see what the model changed
- Next: [Terminal](terminal.md) lists every command.

## Mine tasks from GitHub

```sh
gh auth login
node swe/cli.ts mine https://github.com/pallets/click --max 5
node swe/cli.ts funnel https://github.com/pallets/click
uv run factory tasks list --workdir ~/.local/state/task-factory/swe/pallets-click
```

- Next: [SWE miner](swe-miner.md).

## Run everything in containers

```sh
docker compose up -d mongo                                       # records database
scripts/factory-docker factory --help                            # any factory command
scripts/factory-docker swe mine https://github.com/pallets/click --max 5
```

- Next: [Deployment](deployment.md).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Exit code `3`, "docker info failed" | Docker is not running | Start Docker, or set `sandbox.docker_binary = "podman"` |
| `needs_manual_config` after `inspect` | Language or test runner not detected | Add an `[env]` override or an `[ecosystems.NAME]` entry ([Configuration](configuration.md)) |
| Every attempt is `agent_error` | Bad model ID, missing key, or an exhausted free quota | Check `agent.log.json`; try another model or provider |
| `Free usage limit reached` (HTTP 429) | Zen free-model quota used up | Wait for the reset, or connect a provider with credits |
| SWE miner drops every PR with `env_failed` | Dependencies do not install at that commit | Run `/swe` from the terminal (the setup agent helps), or try newer PRs with `--since` |
| Port 27017 already in use | Another MongoDB is running | Stop it, or change the port in `docker-compose.yml` |
