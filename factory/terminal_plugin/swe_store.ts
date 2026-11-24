// The SWE task stores (written by swe/cli.ts), read for /swe-tasks: one folder per GitHub repo,
//   <state>/task-factory/swe/<owner>-<repo>/tasks/<task_id>/   (the factory's package layout + swe.json)
// Review status comes from the same append-only log the Python task store keeps.
import { existsSync, readdirSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { readJsonl, safeName } from "./solutions.ts"

export type SweTask = {
  task_id: string
  store: string // <owner>-<repo> folder: the workdir for the harness and /solutions
  pkg: string
  status: "pending" | "approved" | "deleted"
  meta: {
    repo: string
    instance_id: string
    base_commit: string
    pull_request: string
    issues: string[]
    created_at: string
    FAIL_TO_PASS: string
    PASS_TO_PASS: string
    quality?: { flags: string[]; unmentioned_names: string[]; patch_lines: number }
  }
}

export function sweRoot(): string {
  return join(process.env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "task-factory", "swe")
}

const AFTER: Record<string, SweTask["status"]> = { add: "pending", restore: "pending", edit: "pending", approve: "approved", delete: "deleted" }

/** factory.review.store: the latest status-changing event wins; no event = pending. */
export function reviewStatus(tasksDir: string, taskID: string): SweTask["status"] {
  let status: SweTask["status"] = "pending"
  for (const e of readJsonl(join(tasksDir, ".review", "log.jsonl"))) if (e.task_id === taskID && AFTER[e.action]) status = AFTER[e.action]
  return status === "deleted" ? "pending" : status // a deleted package is in .trash, not listed here
}

/** The store folders that have tasks (cheap: no package is read). */
export function sweStores(root = sweRoot()): string[] {
  if (!existsSync(root)) return []
  return readdirSync(root).filter((d) => safeName(d) && existsSync(join(root, d, "tasks"))).map((d) => join(root, d))
}

export function sweTasks(root = sweRoot()): SweTask[] {
  if (!existsSync(root)) return []
  const out: SweTask[] = []
  for (const store of readdirSync(root).filter(safeName).sort()) {
    const tasksDir = join(root, store, "tasks")
    if (!existsSync(tasksDir)) continue
    for (const id of readdirSync(tasksDir).filter((d) => safeName(d) && !d.startsWith(".")).sort()) {
      const pkg = join(tasksDir, id)
      if (!existsSync(join(pkg, "swe.json")) || !existsSync(join(pkg, "task.toml"))) continue
      try {
        out.push({ task_id: id, store: join(root, store), pkg, status: reviewStatus(tasksDir, id), meta: JSON.parse(readFileSync(join(pkg, "swe.json"), "utf8")) })
      } catch {
        // a half-written package: skip it
      }
    }
  }
  return out
}

export const listOf = (json: string): string[] => {
  try {
    const v = JSON.parse(json)
    return Array.isArray(v) ? v.map(String) : []
  } catch {
    return []
  }
}

export function readPart(t: SweTask, rel: string): string {
  const file = join(t.pkg, rel)
  return existsSync(file) ? readFileSync(file, "utf8") : ""
}
