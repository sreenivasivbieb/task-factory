// What a terminal session did with tasks, read straight from the project's workdir (no Python):
//   - which harness runs the session started (session-runs.jsonl, written here),
//   - which tasks belong to it (made by /generate-task, run in it, or copied into it),
//   - each attempt's change as files changed (graded solution.diff or the raw agent.diff).
// Only node:fs and node:path, so `node --test` can test it without the engine.
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync } from "node:fs"
import { join } from "node:path"

export type Attempt = {
  run_id: string
  task_id: string
  attempt: number
  agent?: string
  model?: string | null
  resolved: boolean
  category: string
  f2p_passed: number
  f2p_total: number
  p2p_failed: string[]
  tests_tampered: string[]
  agent_duration_s?: number | null
  diff_lines?: number
  branch?: string | null
  commit?: string | null
  egress_blocked?: string[] // set when the agent's network was the allowlist: what it was refused
}

export type FileChange = {
  path: string
  status: "added" | "deleted" | "modified" | "renamed" | "binary"
  added: number
  removed: number
  lines: string[] // the hunks: "@@ ..." headers and +/-/space lines
}

export const SESSION_RUNS = "session-runs.jsonl" // {session_id, run_id, model, task_ids, ts}
export const SESSION_TASKS = "session-tasks.jsonl" // {session_id, task_ids, from_session, ts}
const SAFE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/

export const safeName = (name: string) => SAFE.test(name) && !name.includes("..")

export function readJsonl(file: string): any[] {
  if (!existsSync(file)) return []
  const out: any[] = []
  for (const line of readFileSync(file, "utf8").split("\n")) {
    if (!line.trim()) continue
    try {
      out.push(JSON.parse(line))
    } catch {
      // a half-written line from a crash: skip it
    }
  }
  return out
}

function appendJsonl(file: string, value: unknown) {
  mkdirSync(join(file, ".."), { recursive: true })
  appendFileSync(file, JSON.stringify(value) + "\n", "utf8")
}

const unique = <T,>(items: T[]) => [...new Set(items)]

/** Harness runs in the workdir that have results, oldest first. */
export function runIDs(workdir: string): string[] {
  const dir = join(workdir, "harness")
  if (!existsSync(dir)) return []
  return readdirSync(dir, { withFileTypes: true })
    .filter((d) => d.isDirectory() && safeName(d.name) && existsSync(join(dir, d.name, "results.jsonl")))
    .map((d) => d.name)
    .sort()
}

export function readAttempts(workdir: string, runID: string): Attempt[] {
  if (!safeName(runID)) return []
  return readJsonl(join(workdir, "harness", runID, "results.jsonl"))
    .filter((r) => r && typeof r.task_id === "string" && safeName(r.task_id))
    .map((r) => ({
      ...r,
      run_id: runID,
      attempt: Number(r.attempt) || 1,
      p2p_failed: r.p2p_failed ?? [],
      tests_tampered: r.tests_tampered ?? [],
    }))
}

/** Runs that appeared since `before` and only hold the requested tasks (another terminal's run is left out). */
export function newRuns(workdir: string, before: Set<string>, taskIDs: string[]): string[] {
  const wanted = new Set(taskIDs)
  return runIDs(workdir).filter((id) => {
    if (before.has(id)) return false
    const rows = readAttempts(workdir, id)
    return rows.length > 0 && rows.every((r) => wanted.has(r.task_id))
  })
}

export function linkRuns(workdir: string, sessionID: string, ids: string[], model?: string, taskIDs: string[] = []) {
  for (const run_id of ids)
    appendJsonl(join(workdir, SESSION_RUNS), { session_id: sessionID, run_id, model, task_ids: taskIDs, ts: Date.now() / 1000 })
}

/** A copied session starts with the tasks of the session it was copied from. */
export function copyTasks(workdir: string, toSession: string, taskIDs: string[], fromSession?: string) {
  appendJsonl(join(workdir, SESSION_TASKS), { session_id: toSession, task_ids: taskIDs, from_session: fromSession, ts: Date.now() / 1000 })
}

export function sessionRunIDs(workdir: string, sessionID: string): string[] {
  return unique(readJsonl(join(workdir, SESSION_RUNS)).filter((l) => l.session_id === sessionID).map((l) => String(l.run_id)))
}

/** Every attempt from the runs this session started, newest run first. */
export function sessionAttempts(workdir: string, sessionID: string): Attempt[] {
  return sessionRunIDs(workdir, sessionID)
    .flatMap((id) => readAttempts(workdir, id))
    .sort((a, b) => b.run_id.localeCompare(a.run_id) || a.task_id.localeCompare(b.task_id) || a.attempt - b.attempt)
}

/** The session's tasks: made in it (/generate-task), run in it, or copied into it; only ones still in the store. */
export function sessionTaskIDs(workdir: string, sessionID: string): string[] {
  const made = readJsonl(join(workdir, "generations.jsonl"))
    .filter((g) => g.session_id === sessionID && g.verdict === "valid" && g.task_id)
    .map((g) => String(g.task_id))
  const run = sessionAttempts(workdir, sessionID).map((a) => a.task_id)
  const copied = readJsonl(join(workdir, SESSION_TASKS))
    .filter((l) => l.session_id === sessionID)
    .flatMap((l) => (Array.isArray(l.task_ids) ? l.task_ids.map(String) : []))
  return unique([...made, ...run, ...copied]).filter((id) => safeName(id) && existsSync(join(workdir, "tasks", id, "task.toml")))
}

export function attemptDir(workdir: string, a: Attempt): string {
  if (!safeName(a.run_id) || !safeName(a.task_id)) throw new Error(`bad attempt ${a.run_id}/${a.task_id}`)
  return join(workdir, "harness", a.run_id, a.task_id, `attempt-${a.attempt}`)
}

/** The graded change (test/config edits undone), or with `raw` the agent's whole change. */
export function attemptDiff(workdir: string, a: Attempt, raw = false): string {
  const file = join(attemptDir(workdir, a), raw ? "agent.diff" : "solution.diff")
  return existsSync(file) ? readFileSync(file, "utf8") : ""
}

const unquote = (p: string) => (p.startsWith('"') && p.endsWith('"') ? p.slice(1, -1) : p)

/** Split a `git diff` into one entry per file, with +/- counts and only the hunks. */
export function filesChanged(diff: string): FileChange[] {
  const files: FileChange[] = []
  let cur: FileChange | undefined
  let inHunk = false
  for (const line of diff.split("\n")) {
    const head = /^diff --git (?:"?a\/)(.+?)"? (?:"?b\/)(.+?)"?$/.exec(line)
    if (head) {
      cur = { path: unquote(head[2]), status: "modified", added: 0, removed: 0, lines: [] }
      files.push(cur)
      inHunk = false
      continue
    }
    if (!cur) continue
    if (!inHunk) {
      if (line.startsWith("new file mode")) cur.status = "added"
      else if (line.startsWith("deleted file mode")) cur.status = "deleted"
      else if (line.startsWith("rename to ")) {
        cur.status = "renamed"
        cur.path = line.slice("rename to ".length)
      } else if (line.startsWith("Binary files ") || line === "GIT binary patch") cur.status = "binary"
    }
    if (line.startsWith("@@")) {
      inHunk = true
      cur.lines.push(line)
    } else if (inHunk && /^[+\- \\]/.test(line)) {
      cur.lines.push(line)
      if (line.startsWith("+")) cur.added++
      else if (line.startsWith("-")) cur.removed++
    }
  }
  return files
}

export function taskInstruction(workdir: string, taskID: string): string {
  if (!safeName(taskID)) return ""
  const file = join(workdir, "tasks", taskID, "instruction.md")
  return existsSync(file) ? readFileSync(file, "utf8") : ""
}

/** The first "# " heading of the instruction, like the harness does. */
export function taskTitle(instruction: string, fallback: string): string {
  const line = instruction.split("\n").find((l) => l.startsWith("# "))
  return line ? line.slice(2).trim() : fallback
}

export function verdictOf(a: Attempt): string {
  return a.resolved ? "RESOLVED" : a.category.toUpperCase().replace(/_/g, " ")
}

const FETCH_CMD = /\b(curl|wget|git\s+(clone|fetch|pull)|pip3?\s+(download|install)\s+(?!-e\b|-r\b|\.)|uv\s+pip\s+install\s+(?!-e\b|-r\b|\.)|npm\s+(view|pack))\b/

/** What an attempt took from the internet: web tools and download commands in the agent's session.
 *  The agent sandbox has network access (to reach the model), so a public fix can be looked up:
 *  an attempt that did this may be contaminated. */
export function webUse(workdir: string, a: Attempt): string[] {
  const file = join(attemptDir(workdir, a), "agent.log.json")
  if (!existsSync(file)) return []
  let stdout = ""
  try {
    stdout = String(JSON.parse(readFileSync(file, "utf8")).stdout ?? "")
  } catch {
    return []
  }
  const out: string[] = []
  for (const line of stdout.split("\n")) {
    if (!line.startsWith("{")) continue
    let part: any
    try {
      part = JSON.parse(line).part
    } catch {
      continue
    }
    if (part?.type !== "tool") continue
    const input = part.state?.input ?? {}
    if (part.tool === "webfetch" || part.tool === "websearch")
      out.push(`${part.tool} ${String(input.url ?? input.query ?? "").slice(0, 160)}`)
    else if (part.tool === "shell" || part.tool === "bash") {
      const cmd = String(input.command ?? "")
      if (FETCH_CMD.test(cmd)) out.push(`shell ${cmd.replace(/\s+/g, " ").slice(0, 160)}`)
    }
  }
  return unique(out)
}
