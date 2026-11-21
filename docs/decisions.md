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

## Limitations

- L1 LocalSandbox has no isolation and allows network; it logs a warning on every use.
