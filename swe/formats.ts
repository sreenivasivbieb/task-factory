// Formats the Python side reads back, written byte for byte the same way:
// the generated Dockerfile and image tag (so the harness reuses the image the miner built),
// task.toml (factory/packaging/toml_writer.py) and JUnit test IDs (factory/runner/parsers).
// tests/test_swe_parity.py compares each one with the Python original.
import { pyJson, sha256, shQuote, slugify } from "./util.ts"

export type EnvConfig = {
  schema_version: 1
  base_image: string
  setup_commands: string[]
  test_command: string
  test_timeout_s: number
  env_vars: Record<string, string>
  workdir: string
  framework: string
  report_format: string
  report_path: string
  select: "ids" | "files" | "dirs" | "none"
  select_all: string
  deselect: string | null
  helper_files: Record<string, string>
  artifacts: string[]
  protected_paths: string | null
}

export const PRELUDE_VERSION = "3" // factory/sandbox/base.py
export const SCRATCH_DIR = ".factory"
export const BASE_TAG = "factory-base"
export const FIXED_ENV: Record<string, string> = {
  TZ: "UTC", LANG: "C.UTF-8", LC_ALL: "C.UTF-8", PYTHONHASHSEED: "0", PYTHONDONTWRITEBYTECODE: "1", CI: "1",
}

/** factory.sandbox.base.image_tag */
export function imageTag(repoName: string, commitSha: string, env: Pick<EnvConfig, "base_image" | "setup_commands">): string {
  const digest = sha256(pyJson([commitSha, env.base_image, env.setup_commands, PRELUDE_VERSION])).slice(0, 16)
  return `factory-${slugify(repoName, 40)}:${digest}`
}

/** factory.sandbox.base.base_commit_sh */
export function baseCommitSh(env: Pick<EnvConfig, "artifacts">): string {
  const excludes = [...new Set([`${SCRATCH_DIR}/`, ...env.artifacts])].map(shQuote).join(" ")
  return (
    "git init -q && git add -A && " +
    "git -c user.name=factory -c user.email=factory@localhost -c commit.gpgsign=false " +
    `commit -q --allow-empty -m base && git tag ${BASE_TAG} && ` +
    `mkdir -p ${SCRATCH_DIR} && printf '%s\\n' ${excludes} >> .git/info/exclude`
  )
}

/** factory.sandbox.base.render_dockerfile */
export function renderDockerfile(env: EnvConfig): string {
  const lines = [
    `FROM ${env.base_image}`,
    "USER root",
    "RUN command -v git >/dev/null 2>&1 || " +
      "(apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates " +
      "&& rm -rf /var/lib/apt/lists/*) || apk add --no-cache git || yum install -y git",
    "RUN id factory >/dev/null 2>&1 || useradd -m -u 10001 factory 2>/dev/null " +
      "|| adduser -D -u 10001 factory 2>/dev/null || true",
    `WORKDIR ${env.workdir}`,
    `ADD repo.tar ${env.workdir}/`,
    `RUN ${baseCommitSh(env)}`,
    ...env.setup_commands.map((c) => `RUN ${c}`),
    `RUN (id factory >/dev/null 2>&1 && chown -R factory ${env.workdir}) || true`,
  ]
  return lines.join("\n") + "\n"
}

// ---------------------------------------------------------------- task.toml (toml_writer.py)

const tomlScalar = (v: unknown): string => {
  if (typeof v === "boolean") return v ? "true" : "false"
  if (typeof v === "number") {
    if (!Number.isFinite(v)) throw new Error("NaN/inf not supported")
    return String(v) // only integers are written (floats are left out of SWE packages)
  }
  if (typeof v === "string") return JSON.stringify(v)
  throw new Error(`unsupported TOML value: ${typeof v}`)
}
const tomlKey = (k: string) => (/^[A-Za-z0-9_-]+$/.test(k) && /[A-Za-z0-9]/.test(k) ? k : JSON.stringify(k))
const isPlainObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v)

function emit(data: Record<string, unknown>, prefix: string, out: string[]) {
  const tables: [string, Record<string, unknown>][] = []
  for (const [k, v] of Object.entries(data)) {
    if (v === null || v === undefined) continue
    if (isPlainObject(v)) tables.push([k, v])
    else if (Array.isArray(v)) {
      if (v.some(isPlainObject)) throw new Error("arrays of tables are not used in task.toml")
      if (v.length <= 3) out.push(`${tomlKey(k)} = [${v.map(tomlScalar).join(", ")}]`)
      else out.push(`${tomlKey(k)} = [`, ...v.map((x) => `  ${tomlScalar(x)},`), "]")
    } else out.push(`${tomlKey(k)} = ${tomlScalar(v)}`)
  }
  for (const [k, v] of tables) {
    const name = `${prefix}${tomlKey(k)}`
    out.push("", `[${name}]`)
    emit(v, name + ".", out)
  }
}

/** factory.packaging.toml_writer.dumps */
export function tomlDumps(data: Record<string, unknown>): string {
  const out: string[] = []
  emit(data, "", out)
  return out.join("\n").replace(/^\n+/, "") + "\n"
}

// ---------------------------------------------------------------- JUnit XML (parsers.parse_junit)

export type TestStatus = "passed" | "failed" | "error" | "skipped"
export type TestResult = { test_id: string; status: TestStatus }

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" }
const decode = (s: string) =>
  s.replace(/&(#x[0-9a-fA-F]+|#\d+|\w+);/g, (m, e: string) =>
    e[0] === "#" ? String.fromCodePoint(e[1] === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10)) : ENTITIES[e] ?? m,
  )

function attrs(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of text.matchAll(/([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) out[m[1]] = decode(m[2] ?? m[3] ?? "")
  return out
}

/** Make an absolute path relative to the first matching repo root (parsers._rel, without realpath). */
export function relPath(path: string, roots: string[]): string {
  if (!path.startsWith("/")) return path.replace(/\\/g, "/")
  for (const root of roots) {
    const r = root.replace(/\/+$/, "")
    if (path.startsWith(r + "/")) return path.slice(r.length + 1)
  }
  return path.replace(/^\/+/, "")
}

function dedupe(results: TestResult[]): TestResult[] {
  const seen = new Map<string, number>()
  return results.map((r) => {
    const n = seen.get(r.test_id) ?? 0
    seen.set(r.test_id, n + 1)
    return n === 0 ? r : { ...r, test_id: `${r.test_id}[${n + 1}]` }
  })
}

export function parseJunit(text: string, roots: string[] = ["/repo"]): TestResult[] {
  if (!/<testsuites?\b|<testcase\b/.test(text)) throw new Error("invalid JUnit XML: no test suite")
  const body = text.replace(/<!\[CDATA\[[\s\S]*?\]\]>/g, "")
  const results: TestResult[] = []
  for (const m of body.matchAll(/<testcase\b((?:[^>"']|"[^"]*"|'[^']*')*?)(\/>|>([\s\S]*?)<\/testcase>)/g)) {
    const a = attrs(m[1])
    const inner = m[3] ?? ""
    const name = a.name ?? ""
    const classname = a.classname ?? ""
    let testID: string
    if (a.file) {
      const rel = relPath(a.file, roots)
      const dot = rel.lastIndexOf(".")
      const module = (dot >= 0 ? rel.slice(0, dot) : rel).replace(/\//g, ".")
      const parts = [rel]
      if (classname.startsWith(module + ".")) parts.push(classname.slice(module.length + 1).replace(/\./g, "::"))
      parts.push(name)
      testID = parts.join("::")
    } else testID = classname ? `${classname}::${name}` : name
    const status: TestStatus = /<error\b/.test(inner) ? "error" : /<failure\b/.test(inner) ? "failed" : /<skipped\b/.test(inner) ? "skipped" : "passed"
    results.push({ test_id: testID, status })
  }
  return dedupe(results)
}
