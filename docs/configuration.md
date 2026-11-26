# Configuration

- Settings live in `factory.toml` in the current folder (or `--config PATH`).
- Command-line flags override the file.
- Unknown keys are an error (a typo never switches a setting off silently).
- Start from [`factory.example.toml`](../factory.example.toml).

## Top level

| Key | Default | Meaning |
|---|---|---|
| `seed` | `0` | Seed for anything random |
| `workdir` | `"workdir"` | Work folder (the terminal uses `~/.local/state/task-factory/projects/…`) |

## `[sandbox]`

| Key | Default | Meaning |
|---|---|---|
| `backend` | `"docker"` | `docker`, or `local` (no isolation, development only) |
| `docker_binary` | `"docker"` | Set to `"podman"` to use Podman |
| `memory` | `"2g"` | Memory limit per sandbox |
| `cpus` | `2.0` | CPU limit per sandbox |
| `pids_limit` | `512` | Process limit per sandbox |
| `build_timeout_s` | `1800` | Image build time limit |
| `output_limit_bytes` | `1000000` | Command output kept per run |
| `local_run_setup` | `false` | Local backend: run setup commands on the host |
| `pin_base_image` | `true` | Record the base image by digest at inspect time |

## `[gates]`

| Key | Default | Meaning |
|---|---|---|
| `stability_repeats` | `1` | G5 repeats (use 3 for final runs) |
| `mutations` | `["empty", "flip_comparison", "drop_error_branch"]` | G6 operators; full set: `empty`, `revert_half`, `flip_comparison`, `swap_arguments`, `drop_error_branch`, `hardcode_expected`, `off_by_one`, `swap_arithmetic`, `negate_condition`, `swap_boolean` |
| `min_mutation_score` | `1.0` | Share of mutants (the empty patch included) the hidden tests must catch |
| `spec_check` | `true` | G7: hidden tests may only import names that exist or that the requirements give |
| `max_known_failure_ratio` | `0.2` | Share of baseline tests allowed to fail already |

- Pipeline files (`factory pipeline FILE`) have their own `[pipeline]` section: see `configs/hackathon.toml`
  and the [Track 1 guide](hackathon.md).

## `[harness]`

| Key | Default | Meaning |
|---|---|---|
| `k` | `1` | Attempts per task |
| `workers` | `1` | Attempts at the same time |
| `default_agent` | `"task-factory"` | Agent used by `harness work` and the terminal |
| `default_model` | — | Model when none is given |
| `use_host_auth` | `false` | Copy your stored login for the model's provider into the sandbox |
| `branches` | `true` | Commit every resolved attempt to a git branch |
| `stop_after_agent_errors` | `2` | Stop a run after this many agent errors in a row |
| `egress` | `"allowlist"` | `allowlist`: the agent reaches only `allowed_hosts`; `open`: the whole internet |
| `allowed_hosts` | Model APIs (see below) | Hosts the agent may reach (fnmatch patterns, HTTPS only) |
| `egress_image` | `"python:3.13-slim"` | Image that runs the egress proxy |
| `agents` | `{}` | Custom agents (see below) |

- Default `allowed_hosts`:
  - `opencode.ai`, `*.opencode.ai` (Zen)
  - `integrate.api.nvidia.com`
  - `api.tokenrouter.com`
  - `openrouter.ai`
  - `api.anthropic.com`, `api.openai.com`, `api.groq.com`
  - `bedrock-runtime.*.amazonaws.com`

### Custom agents

```toml
[harness.agents.my-agent]
description = "My agent"
install = ["pip install my-agent"]           # run once into a derived image
run = "my-agent --task {instruction_file} {model_args}"
model_flag = "--model {model}"
secrets = ["MY_AGENT_KEY"]                   # forwarded only when set
```

## `[reference]`

- The reference-guided pipeline (`factory run`): targets, workers, research lanes, repairs, peers,
  licences, Harbor, and models per role (`[reference.models]`: several `author` / `test_writer`
  models turn on the builder panel). Every key: [Reference-guided pipeline](reference-pipeline.md#configuration-reference-in-factorytoml).

## `[env]` overrides

- Override what `inspect` detects; any key left out keeps the detected value.

| Key | Example |
|---|---|
| `base_image` | `"python:3.12-slim"` |
| `setup_commands` | `["pip install -e '.[test]'"]` |
| `test_command` | `"python -m pytest -q --junitxml={report} {select}"` |
| `test_timeout_s` | `900` |
| `env_vars` | `{ DJANGO_SETTINGS_MODULE = "tests.settings" }` |
| `framework`, `report_format`, `report_path` | `"pytest"`, `"junit"`, `".factory/report.xml"` |

- `{report}` is replaced by the report path; `{select}` by the selected tests.

## Languages

- Every language is data in [`factory/ecosystems.toml`](../factory/ecosystems.toml); none is written into the code.
- Add or change one under `[ecosystems.NAME]` in `factory.toml`.

| Key | Meaning |
|---|---|
| `markers` | Files in the repository root that identify the language |
| `framework` | A label for reports |
| `base_image`, `setup_commands`, `test_command`, `test_timeout_s` | The environment |
| `select` | How tests are chosen: `ids`, `files`, `dirs` or `none` |
| `select_all`, `deselect` | Full-suite selector; template to skip a test |
| `report_format`, `report_path` | `junit`, `jest-json`, `go-json` or `cargo-text` |
| `install` | `[{if_file, command}]`: the first match runs before setup |
| `variants` | `[{name, if_file, if_contains, …}]`: override keys for a matching setup (e.g. Jest vs Vitest) |
| `test_paths`, `build_files`, `build_patterns` | Test and test-config files (protected from solvers) |
| `artifacts` | Build output, never part of a change |
| `error_branch`, `expected_literal`, `comment_prefixes`, `value_return` | Mutation rules for G6 |

- Example (a language with no built-in support):

```toml
[ecosystems.bash]
markers = ["run_tests.sh"]
framework = "bash"
base_image = "bash:5.2"
test_command = "bash run_tests.sh {report} {select}"
select = "files"
report_format = "junit"
test_paths = ['(^|/)t/']
build_files = ["run_tests.sh"]
```

| Ecosystem | Detected from | Test runner | State |
|---|---|---|---|
| Python | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt` | pytest (also runs unittest) | End to end |
| JavaScript / TypeScript | `package.json` | node:test, Jest, Vitest | End to end (node:test) |
| Bash | `factory.toml` only | A script writing JUnit XML | End to end |
| Java | `pom.xml`, `build.gradle` | Maven Surefire, Gradle | Built in, not yet run on a real repository |
| Go | `go.mod` | `go test -json` | Built in, not yet run on a real repository |
| Rust | `Cargo.toml` | `cargo test` | Built in, not yet run on a real repository |

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `TASK_FACTORY_MONGO_URI` | Records | Write records to this MongoDB as well |
| `TASK_FACTORY_PYTHON` | SWE miner | Python used to write MongoDB records |
| `GH_TOKEN` | SWE miner | GitHub login (otherwise `gh auth login`) |
| `NVIDIA_API_KEY`, `TOKENROUTER_API_KEY`, `TASK_FACTORY_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, AWS variables | Harness | Model provider credentials |
| `XDG_STATE_HOME` | Terminal, SWE miner | Where project state and SWE stores live |
