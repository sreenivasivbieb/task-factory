// node --test tests/ts   (Node 22.18+ runs TypeScript directly)
import assert from "node:assert/strict"
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"
import {
  attemptDiff,
  copyTasks,
  filesChanged,
  linkRuns,
  newRuns,
  runIDs,
  sessionAttempts,
  readAttempts as readAttemptsFor,
  sessionTaskIDs,
  taskTitle,
  verdictOf,
  webUse,
} from "../../factory/terminal_plugin/solutions.ts"

const DIFF = `diff --git a/shop/cart.py b/shop/cart.py
index 1111111..2222222 100644
--- a/shop/cart.py
+++ b/shop/cart.py
@@ -1,3 +1,4 @@
 def total(cart):
-    return 0
+    amount = sum(cart)
+    return amount
diff --git a/shop/new.py b/shop/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/shop/new.py
@@ -0,0 +1 @@
+X = 1
`

function workdir() {
  const wd = mkdtempSync(join(tmpdir(), "tf-sol-"))
  return wd
}

function addRun(wd: string, runID: string, rows: any[]) {
  mkdirSync(join(wd, "harness", runID), { recursive: true })
  writeFileSync(join(wd, "harness", runID, "results.jsonl"), rows.map((r) => JSON.stringify(r)).join("\n") + "\n")
  for (const r of rows) {
    const dir = join(wd, "harness", runID, r.task_id, `attempt-${r.attempt}`)
    mkdirSync(dir, { recursive: true })
    writeFileSync(join(dir, "solution.diff"), DIFF)
    writeFileSync(join(dir, "agent.diff"), DIFF + "diff --git a/tests/test_x.py b/tests/test_x.py\n@@ -1 +1 @@\n-a\n+b\n")
  }
}

function addTask(wd: string, id: string) {
  mkdirSync(join(wd, "tasks", id), { recursive: true })
  writeFileSync(join(wd, "tasks", id, "task.toml"), "")
  writeFileSync(join(wd, "tasks", id, "instruction.md"), `# Title of ${id}\n\nbody\n`)
}

const row = (task_id: string, resolved: boolean, attempt = 1) => ({
  task_id, attempt, agent: "task-factory", model: "zen/m", resolved,
  category: resolved ? "resolved" : "wrong_behaviour", f2p_passed: resolved ? 2 : 1, f2p_total: 2,
  p2p_failed: [], tests_tampered: [],
})

test("filesChanged splits a diff per file with counts and status", () => {
  const files = filesChanged(DIFF)
  assert.deepEqual(files.map((f) => [f.path, f.status, f.added, f.removed]), [
    ["shop/cart.py", "modified", 2, 1],
    ["shop/new.py", "added", 1, 0],
  ])
  assert.equal(files[0].lines[0], "@@ -1,3 +1,4 @@")
  assert.ok(!files[0].lines.some((l) => l.startsWith("+++") || l.startsWith("index")))
})

test("filesChanged handles renames, deletions and an empty diff", () => {
  assert.deepEqual(filesChanged(""), [])
  const renamed = filesChanged("diff --git a/a.py b/b.py\nsimilarity index 90%\nrename from a.py\nrename to b.py\n")
  assert.deepEqual([renamed[0].path, renamed[0].status], ["b.py", "renamed"])
  const gone = filesChanged("diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
  assert.deepEqual([gone[0].status, gone[0].removed], ["deleted", 1])
})

test("only runs started from a session are its attempts", () => {
  const wd = workdir()
  addRun(wd, "20261124-100000-task-factory", [row("t-001", true)]) // someone else's older run
  const before = new Set(runIDs(wd))
  addRun(wd, "20261124-110000-task-factory", [row("t-001", true), row("t-002", false)])
  addRun(wd, "20261124-110001-task-factory", [row("other-task", true)]) // another terminal, same moment
  const fresh = newRuns(wd, before, ["t-001", "t-002"])
  assert.deepEqual(fresh, ["20261124-110000-task-factory"])
  linkRuns(wd, "ses_A", fresh, "zen/m", ["t-001", "t-002"])
  const mine = sessionAttempts(wd, "ses_A")
  assert.deepEqual(mine.map((a) => [a.task_id, a.resolved]), [["t-001", true], ["t-002", false]])
  assert.deepEqual(sessionAttempts(wd, "ses_B"), [])
  assert.equal(verdictOf(mine[1]), "WRONG BEHAVIOUR")
})

test("graded and raw diffs come from the attempt folder", () => {
  const wd = workdir()
  addRun(wd, "20261124-120000-task-factory", [row("t-001", true)])
  linkRuns(wd, "ses_A", ["20261124-120000-task-factory"])
  const [a] = sessionAttempts(wd, "ses_A")
  assert.equal(filesChanged(attemptDiff(wd, a)).length, 2)
  assert.equal(filesChanged(attemptDiff(wd, a, true)).length, 3)
  assert.throws(() => attemptDiff(wd, { ...a, run_id: "../x" }))
})

test("a session's tasks: generated, run or copied, still in the store", () => {
  const wd = workdir()
  for (const id of ["t-gen", "t-run", "t-copy"]) addTask(wd, id)
  writeFileSync(join(wd, "generations.jsonl"),
    [{ session_id: "ses_A", verdict: "valid", task_id: "t-gen" }, { session_id: "ses_A", verdict: "rejected" },
     { session_id: "ses_B", verdict: "valid", task_id: "t-copy" }, { session_id: "ses_A", verdict: "valid", task_id: "t-gone" }]
      .map((g) => JSON.stringify(g)).join("\n") + "\n")
  addRun(wd, "20261124-130000-task-factory", [row("t-run", true)])
  linkRuns(wd, "ses_A", ["20261124-130000-task-factory"])
  assert.deepEqual(sessionTaskIDs(wd, "ses_A"), ["t-gen", "t-run"])
  copyTasks(wd, "ses_C", sessionTaskIDs(wd, "ses_A"), "ses_A")
  assert.deepEqual(sessionTaskIDs(wd, "ses_C"), ["t-gen", "t-run"])
})

test("taskTitle takes the first heading", () => {
  assert.equal(taskTitle("intro\n# Bulk discount\n# Other", "fallback"), "Bulk discount")
  assert.equal(taskTitle("no heading", "fallback"), "fallback")
})

test("webUse lists web tools and download commands from the agent's session", () => {
  const wd = workdir()
  addRun(wd, "20261124-140000-task-factory", [row("t-001", true)])
  const [a] = readAttemptsFor(wd, "20261124-140000-task-factory")
  const events = [
    { part: { type: "tool", tool: "webfetch", state: { input: { url: "https://github.com/o/r/pull/7.diff" } } } },
    { part: { type: "tool", tool: "shell", state: { input: { command: "pip install -e ." } } } },
    { part: { type: "tool", tool: "shell", state: { input: { command: "pip install click==9.0" } } } },
    { part: { type: "tool", tool: "read", state: { input: { filePath: "x.py" } } } },
  ]
  writeFileSync(join(wd, "harness", a.run_id, a.task_id, "attempt-1", "agent.log.json"),
    JSON.stringify({ stdout: events.map((e) => JSON.stringify(e)).join("\n") }))
  assert.deepEqual(webUse(wd, a), ["webfetch https://github.com/o/r/pull/7.diff", "shell pip install click==9.0"])
})
