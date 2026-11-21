# task-factory

A reusable pipeline that turns a software repository into **validated coding
tasks** for evaluating AI coding systems. Built for the Innodata AI Hackathon
2026, Track 1 (Coding Task Factory).

Each task package holds requirements, a reference implementation (a diff), hidden
tests and metadata. A task is exported only after **executed tests** prove it is
valid. No model opinion decides pass or fail.

## Status

| Milestone | Component | State |
| --- | --- | --- |
| M0 | Skeleton, models, CLI, toy Python + Node fixtures | done |
| M1 | Sandbox (Docker + local dev backend) | done |
| M2 | Repo loader (path, git URL, zip) + environment inspector | done |
| M3 | Test runner + parsers (JUnit, Jest JSON, go test -json, cargo) | done |
| M4 | Validation runner, gates G0-G6 | done |
| M5 | Package writer + verifier | done |
| M6-M12 | LLM client, task generator, feature source, variation, report, wiring, real runs | next |

## Setup

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and Docker (or Podman).

```sh
uv sync
uv run pytest            # all tests; Docker tests skip with a message if Docker is missing
uv run pytest -m "not docker"
```

## Commands

```sh
uv run factory load --repo PATH_OR_URL_OR_ZIP [--commit SHA]   # copy + pin the repo
uv run factory inspect                                          # detect env, build image, run baseline
uv run factory validate --tasks DIR                             # run gates on drafts or packages
uv run factory export --out tasks/                              # write valid tasks as packages
```

Common flags: `--workdir`, `--config`, `--seed`, `--force`, `--dry-run`,
`--verbose`, `--backend docker|local`. Every step reads and writes JSON under
`workdir/` and skips work already done unless `--force` is given.

Try it on the bundled fixture:

```sh
cp -r tests/fixtures/toy_python_repo /tmp/toyshop
git -C /tmp/toyshop init -q && git -C /tmp/toyshop add -A && git -C /tmp/toyshop commit -qm init
uv run factory load --repo /tmp/toyshop
uv run factory inspect
uv run factory validate --tasks tests/fixtures/tasks/python   # 1/7 valid; 6 rejected with exact codes
uv run factory export --out tasks/
uv run factory validate --tasks tasks/                        # re-verifies the package from disk
```

## Validation gates

| Gate | What runs | Pass condition | Reject code |
| --- | --- | --- | --- |
| G0 | Full suite on the starting repo | passes (known failures recorded, limited) | `BASELINE_FAIL` |
| G1 | Reference patch | `git apply --check` succeeds | `PATCH_APPLY_FAIL` |
| G2 | Patch + feature tests | every new test passes | `FEATURE_TESTS_FAIL_WITH_PATCH` |
| G3 | Feature tests, no patch | every new test fails | `FEATURE_TESTS_PASS_WITHOUT_PATCH` |
| G4 | Patch + full suite | nothing that passed at baseline breaks | `REGRESSION` |
| G6 | Mutants of the patch (empty, flipped comparison, dropped error branch, ...) | feature tests catch every mutant | `NEG_CONTROL_PASSED` |
| G5 | G2 and G4 repeated | same result every time | `FLAKY` |

Each gate runs in a fresh container with no network. Logs for every gate are
kept and copied into the package under `validation/logs/`.

## Package layout

```
<task_id>/
  instruction.md          requirements (the only file the solver sees)
  task.toml               metadata: repo, commit, tests, tags, validation summary
  solution/solution.diff  reference patch (hidden)
  tests/files/...         hidden feature tests; tests/run_tests.sh runs them
  environment/            Dockerfile + env.json to rebuild the sandbox
  validation/             report.json + gate logs
  checksums.txt           SHA-256 of every task file
```

The layout is assumed (Harbor-style) until the organizers publish the template;
it lives in `factory/packaging/writer.py` only.

## Dependencies and licenses

| Dependency | Use | License |
| --- | --- | --- |
| pydantic | data models | MIT |
| pytest (dev) | tests | MIT |
| hatchling (build) | packaging | MIT |

Assumptions, decisions and known limitations: [docs/decisions.md](docs/decisions.md).
