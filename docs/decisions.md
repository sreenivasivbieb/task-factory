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

## Limitations

- L1 LocalSandbox has no isolation and allows network; it logs a warning on every use.
- L2 Go, Maven, Gradle and Cargo support is parser-level only so far (unit-tested on sample
  reports); only Python and Node run end to end on fixtures.
