# Terminal

The Task Factory terminal is a full coding-agent TUI (a rebranded OpenCode build) with the Task Factory commands added.

## Install and start

```sh
python3 scripts/build_terminal.py "../Task Factory Engine"   # builds the engine from pinned source
uv tool install --editable .
cd /path/to/a/git/project && task-factory
```

| Item | Location |
|---|---|
| Engine | `~/.local/share/task-factory/bin/task-factory-engine` |
| Settings, logins, sessions | `~/.config/task-factory`, `~/.local/share/task-factory` |
| Project state | `~/.local/state/task-factory/projects/<repo>-<hash>/` (outside the repository) |

- Log in to providers with `/connect`; choose a model with `/models`.
- The terminal never changes another coding tool's settings on the same machine.
- The engine's own commands unrelated to tasks are hidden (`HIDDEN_COMMANDS` in `scripts/build_terminal.py`).

## Commands

| Command | What it does |
|---|---|
| `/setup` | Pins the project's committed HEAD and builds its Docker environment (again when HEAD moves) |
| `/generate-task <request>` | The selected model drafts a task in its own read-only session; the gates must pass before it is saved as pending |
| `/tasks` | Lists the project's tasks; show, run, approve or delete one |
| `/run-task` | Pick tasks and a model; watch it solve them in Docker; passing solutions become branches |
| `/results` | Runs → attempts → verdict, the agent's whole session, the graded diff, the test output |
| `/solutions` | **This session's** attempts only, as files changed; `r` toggles the raw agent diff |
| `/rerun` | Runs this session's tasks on another model, here or in a copy of the session |
| `/benchmark` | Tasks × models: who solved what, difficulty, resolve rate, time, tokens, failures |
| `/swe <url> [n]` | Mines SWE-bench style tasks from a GitHub repository, with a live funnel |
| `/swe-tasks` | Mined tasks: issue, reference patch, hidden tests, quality flags; `x` run, `a` approve, `d` delete |

## Pages and keys

| Key | Action |
|---|---|
| `esc` | Back |
| `↑ ↓ pgup pgdn home end` | Scroll |
| `r` | `/solutions`: switch between graded and raw diff |
| `o` | `/benchmark`: open an attempt |
| `x`, `a`, `d` | `/swe-tasks`: run, approve, delete |

## The solution page

- **Summary:** verdict, model, hidden tests passed, broken existing tests, branch.
- **Network note:**
  - 🛡 "only the model API; refused …" — the attempt ran with the egress allowlist.
  - ⚠ "used the internet" — an older attempt with open network looked something up; its result is not trustworthy.
- **Files changed:** each file with `+`/`−` counts, then its colored hunks.
- **Raw diff (`r`):** everything the agent changed, including test edits that grading undid.

## Sessions

- `/solutions` and the sidebar show only attempts started from the current session.
- `/rerun` → "In a copy of this session" creates a new session that starts with the same tasks; its `/solutions` shows only the new model.
- Runs started from the CLI or the home screen belong to no session; see them in `/results`.

## Sidebar

```text
Task Factory
toyshop @ 5cbaceb ✓
Tasks (2)
✓ 1 approved · ⏳ 1 review · ✗ 0
AI performance
resolved 100% (3/3 attempts)
By model
big-pickle            1/1 31s 24k
AI solutions (this session)
✓ 1 resolved · ✗ 0 · /solutions
Next: 1 task(s) wait for review: /tasks
```

- Agent errors (quota, login) are shown apart and never counted as a model failing a task.
