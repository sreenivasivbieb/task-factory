// Step 5, package: the same layout factory/packaging/writer.py writes, so the existing task
// store, review commands and coding harness use SWE tasks unchanged, plus swe.json (where the
// task came from) and a SWE-bench style record for the JSONL export.
import { copyFileSync, linkSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { type EnvConfig, renderDockerfile, tomlDumps } from "./formats.ts"
import type { PullRequest, RepoRef } from "./github.ts"
import type { Split } from "./repo.ts"
import { sha256, shQuote, slugify } from "./util.ts"
import { testCommand, type Extracted, type RunResult } from "./extract.ts"
import { qualityOf } from "./quality.ts"

const CHECKSUMMED = ["instruction.md", "task.toml", "solution/", "tests/", "environment/"]

export type TaskInput = {
  repo: RepoRef
  pr: PullRequest
  split: Split
  env: EnvConfig
  extracted: Extracted
  statement: string
  kind: "bugfix" | "feature"
  runs: { before: RunResult; after: RunResult[]; full?: RunResult }
  repoTar: string
  image: string
}

export const instanceID = (repo: RepoRef, pr: number) => `${repo.owner}__${repo.name}-${pr}`

export function taskID(t: Pick<TaskInput, "repo" | "pr" | "split">): string {
  const title = t.pr.issues[0]?.title || t.pr.title
  return `${slugify(t.repo.name, 24)}-${t.split.base.slice(0, 7)}-${slugify(title, 28)}-pr${t.pr.number}`
}

/** task.toml as a TaskPackage (factory/models.py), field order as Python dumps it. */
export function taskMeta(t: TaskInput) {
  return {
    schema_version: 1,
    task_id: taskID(t),
    repo: t.repo.url,
    repo_name: t.repo.name,
    commit_sha: t.split.base,
    language: "python",
    test_command: t.env.test_command,
    kind: t.kind,
    tags: ["swe", `pr-${t.pr.number}`],
    idea_id: `pr-${t.pr.number}`,
    touched_files: t.split.codeFiles,
    test_files: [...t.split.testFiles].sort(),
    fail_to_pass: t.extracted.fail_to_pass,
    pass_to_pass: t.extracted.pass_to_pass,
    known_failures: [],
    manual_edit: false,
    environment: t.env,
    generated_by: {
      schema_version: 1,
      model: "swe-miner",
      author: t.pr.author,
      request: t.pr.url,
      repair_attempts: [],
    },
    validation: {
      schema_version: 1,
      verdict: "valid",
      backend: "docker",
      gates: ["SWE-before", "SWE-after", "SWE-after-repeat"],
      mutants_run: 0,
      mutants_caught: 0,
    },
  }
}

/** factory.packaging.writer._run_script */
function runScript(env: EnvConfig, testFiles: string[]): string {
  const reportDir = env.report_path.replace(/\/+$/, "").split("/").slice(0, -1).join("/") || "."
  return [
    "#!/bin/sh",
    "# Runs the hidden feature tests against a solver's working copy.",
    "# Usage: REPO_DIR=/repo sh tests/run_tests.sh   (run from the package folder)",
    "set -eu",
    'PKG_DIR=$(cd "$(dirname "$0")/.." && pwd)',
    "REPO_DIR=${REPO_DIR:-/repo}",
    'cp -R "$PKG_DIR/tests/files/." "$REPO_DIR/"',
    'cd "$REPO_DIR"',
    `mkdir -p ${shQuote(reportDir)}`,
    testCommand(env, testFiles),
    "",
  ].join("\n")
}

function walk(dir: string, rel = ""): string[] {
  return readdirSync(join(dir, rel), { withFileTypes: true }).flatMap((d) => {
    const r = rel ? `${rel}/${d.name}` : d.name
    return d.isDirectory() ? walk(dir, r) : [r]
  })
}

/** Path order of Python's sorted(Path.rglob()): compare part by part. */
const byParts = (a: string, b: string) => {
  const pa = a.split("/"), pb = b.split("/")
  for (let i = 0; i < Math.min(pa.length, pb.length); i++) if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1
  return pa.length - pb.length
}

export function checksums(dir: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const rel of walk(dir).sort(byParts))
    if (CHECKSUMMED.some((p) => rel.startsWith(p)) && statSync(join(dir, rel)).isFile()) out[rel] = sha256(readFileSync(join(dir, rel)))
  return out
}

/** Write the package atomically into `storeDir/<task_id>`; returns its path. */
export function writePackage(t: TaskInput, storeDir: string): string {
  const meta = taskMeta(t)
  mkdirSync(storeDir, { recursive: true })
  const tmp = mkdtempSync(join(storeDir, `.${meta.task_id}-`))
  const put = (rel: string, content: string) => {
    mkdirSync(dirname(join(tmp, rel)), { recursive: true })
    writeFileSync(join(tmp, rel), content, "utf8")
  }
  try {
    put("instruction.md", t.statement.endsWith("\n") ? t.statement : t.statement + "\n")
    put("task.toml", tomlDumps(meta))
    put("solution/solution.diff", t.split.patch)
    for (const [rel, content] of Object.entries(t.split.tests).sort()) put(`tests/files/${rel}`, content)
    put("tests/run_tests.sh", runScript(t.env, meta.test_files))
    put("environment/Dockerfile", renderDockerfile(t.env))
    put("environment/env.json", JSON.stringify(t.env, null, 2) + "\n")
    try {
      linkSync(t.repoTar, join(tmp, "environment/repo.tar")) // no second copy of the archive on disk
    } catch {
      copyFileSync(t.repoTar, join(tmp, "environment/repo.tar"))
    }
    put("validation/swe-runs.json", JSON.stringify({ image: t.image, ...t.runs, extracted: t.extracted }, null, 2) + "\n")
    put("swe.json", JSON.stringify(sweRecord(t), null, 2) + "\n")
    const sums = checksums(tmp)
    put("checksums.txt", Object.entries(sums).map(([rel, h]) => `${h}  ${rel}\n`).join(""))
    const final = join(storeDir, meta.task_id)
    rmSync(final, { recursive: true, force: true })
    renameSync(tmp, final)
    return final
  } catch (e) {
    rmSync(tmp, { recursive: true, force: true })
    throw e
  }
}

/** A SWE-bench dataset row (FAIL_TO_PASS / PASS_TO_PASS as JSON strings, like the published sets). */
export function sweRecord(t: TaskInput) {
  return {
    repo: `${t.repo.owner}/${t.repo.name}`,
    instance_id: instanceID(t.repo, t.pr.number),
    base_commit: t.split.base,
    patch: t.split.patch,
    test_patch: t.split.testPatch,
    problem_statement: t.statement,
    hints_text: "",
    created_at: t.pr.createdAt,
    merged_at: t.pr.mergedAt,
    version: t.env.base_image.split("@")[0],
    environment_setup_commit: t.split.base,
    FAIL_TO_PASS: JSON.stringify(t.extracted.fail_to_pass),
    PASS_TO_PASS: JSON.stringify(t.extracted.pass_to_pass),
    pull_request: t.pr.url,
    issues: t.pr.issues.map((i) => i.url),
    task_id: taskID(t),
    quality: qualityOf(t.statement, t.split.patch, t.split.testPatch),
  }
}
