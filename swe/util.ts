// Small helpers shared by the SWE pipeline. Node built-ins only.
import { execFile } from "node:child_process"
import { createHash } from "node:crypto"

export type Exec = { code: number; stdout: string; stderr: string }
export type Runner = (cmd: string, args: string[], opts?: { cwd?: string; input?: string | Buffer; timeoutMs?: number }) => Promise<Exec>

/** Run a program (no shell). Never throws for a non-zero exit; a missing program gives code 127. */
export const run: Runner = (cmd, args, opts = {}) =>
  new Promise((resolve) => {
    const child = execFile(
      cmd,
      args,
      {
        cwd: opts.cwd,
        maxBuffer: 256 * 1024 * 1024,
        timeout: opts.timeoutMs ?? 0,
        killSignal: "SIGKILL",
        encoding: "utf8",
        env: { ...process.env, GIT_TERMINAL_PROMPT: "0", GH_PROMPT_DISABLED: "1", GH_NO_UPDATE_NOTIFIER: "1" },
      },
      (error: any, stdout, stderr) => {
        const code = !error ? 0 : typeof error.code === "number" ? error.code : error.code === "ENOENT" ? 127 : 1
        resolve({ code, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") + (error?.killed ? "\n(timed out)" : "") })
      },
    )
    if (opts.input !== undefined) child.stdin?.end(opts.input)
  })

export async function must(r: Promise<Exec>, what: string): Promise<string> {
  const out = await r
  if (out.code !== 0) throw new Error(`${what} failed (exit ${out.code}): ${(out.stderr || out.stdout).trim().slice(-800)}`)
  return out.stdout
}

export const sha256 = (data: string | Buffer) => createHash("sha256").update(data).digest("hex")

/** Same as factory.models.slugify. */
export function slugify(text: string, maxLen = 32): string {
  const slug = text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")
  return slug.slice(0, maxLen).replace(/-+$/, "") || "task"
}

/** Python's json.dumps(value) with default settings (", " and ": " separators, ASCII only). */
export function pyJson(value: unknown): string {
  if (value === null || value === undefined) return "null"
  if (typeof value === "boolean") return value ? "true" : "false"
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : String(value)
  if (typeof value === "string")
    return JSON.stringify(value).replace(/[\u007f-￿]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"))
  if (Array.isArray(value)) return "[" + value.map(pyJson).join(", ") + "]"
  return "{" + Object.entries(value as object).map(([k, v]) => `${pyJson(k)}: ${pyJson(v)}`).join(", ") + "}"
}

/** POSIX shell quoting, like Python's shlex.quote. */
export function shQuote(s: string): string {
  if (s === "") return "''"
  return /^[\w@%+=:,./-]+$/.test(s) ? s : "'" + s.replace(/'/g, "'\"'\"'") + "'"
}

export const clip = (text: string, n: number) => (text.length <= n ? text : text.slice(0, n) + `\n… (${text.length - n} more characters)`)
