// The SWE pipeline: GitHub URL -> merged PRs with issues -> split at the exact base commit ->
// Docker environment -> before/after test runs -> task packages (pending review) + SWE-bench JSONL.
// Every PR's outcome is saved in <out>/prs/<n>.json, so a rerun continues where it stopped.
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { BuildFailed, buildImage, Container, pinImage, PY_FILES, pythonEnv } from "./env.ts"
import { decide, runTests, type RunResult } from "./extract.ts"
import type { EnvConfig } from "./formats.ts"
import { minePullRequests, parseRepo, problemStatement, type PullRequest, type RepoRef, taskKind } from "./github.ts"
import { writePackage } from "./package.ts"
import { qualityOf } from "./quality.ts"
import { syncRecords } from "./records.ts"
import { archive, ensureClone, listFiles, readFiles, SkipError, splitPullRequest, type Split } from "./repo.ts"
import { clip, run, type Runner } from "./util.ts"

export type Options = {
  max: number // tasks to try to make (candidates processed)
  scan: number // PRs to look at, newest first
  since?: string
  out?: string
  retry?: boolean // try PRs that were dropped before again
  memory?: string // the sandbox's memory limit (docker --memory), default 2g
  workers?: number // PRs processed at the same time (default 1; more is opt-in)
  progress?: (line: string) => void
  ask?: (prompt: string) => Promise<string> // the setup agent: a model, when the plain setup fails
  exec?: Runner
}

export type Outcome = { pr: number; status: "task" | "dropped"; reason?: string; detail?: string; task_id?: string; ts: string }

export function defaultOut(repo: RepoRef): string {
  const state = process.env.XDG_STATE_HOME || join(homedir(), ".local", "state")
  return join(state, "task-factory", "swe", `${repo.owner}-${repo.name}`)
}

export function outcomes(out: string): Outcome[] {
  const dir = join(out, "prs")
  if (!existsSync(dir)) return []
  return readdirSync(dir).filter((f) => /^\d+\.json$/.test(f)).map((f) => JSON.parse(readFileSync(join(dir, f), "utf8")))
}

/** The funnel: how many PRs reached each step, and why the others stopped. */
export function funnel(out: string) {
  const all = outcomes(out)
  const reasons: Record<string, number> = {}
  for (const o of all) if (o.status === "dropped") reasons[o.reason ?? "error"] = (reasons[o.reason ?? "error"] ?? 0) + 1
  const mined = existsSync(join(out, "mined.json")) ? JSON.parse(readFileSync(join(out, "mined.json"), "utf8")) : {}
  return { scanned: mined.scanned ?? 0, mining_skipped: mined.skipped ?? {}, processed: all.length, tasks: all.filter((o) => o.status === "task").length, dropped: reasons }
}

/** Rewrite <out>/swe-bench.jsonl from every package's swe.json (deleted tasks in .trash are left out). */
export function exportJsonl(out: string): number {
  const tasks = join(out, "tasks")
  const rows = existsSync(tasks)
    ? readdirSync(tasks).filter((d) => !d.startsWith(".") && existsSync(join(tasks, d, "swe.json"))).sort()
        .map((d) => readFileSync(join(tasks, d, "swe.json"), "utf8").trim())
        .map((s) => JSON.stringify(JSON.parse(s)))
    : []
  writeFileSync(join(out, "swe-bench.jsonl"), rows.map((r) => r + "\n").join(""))
  return rows.length
}

const SETUP_PROMPT = (repo: RepoRef, pr: PullRequest, env: EnvConfig, files: Record<string, string>, tree: string[], output: string) => `\
You prepare a Docker test environment for the Python repository ${repo.owner}/${repo.name} at the commit
just before pull request #${pr.number} (merged ${pr.mergedAt}). The image is built from
python:<version>-slim with the repository in /repo, then each setup command runs as root (network on).
The tests then run offline with: ${env.test_command.replace("{report}", "<report>").replace("{select}", "<test files>")}

The current setup failed. Setup commands:
${env.setup_commands.map((c) => "  " + c).join("\n")}
Output (tail):
${clip(output, 3500)}

Top-level files: ${tree.filter((p) => !p.includes("/")).slice(0, 80).join(", ")}
${Object.entries(files).map(([p, c]) => `--- ${p}\n${clip(c, 2500)}`).join("\n")}

Reply with ONLY a JSON object: {"python": "3.X", "setup_commands": ["...", "..."]}
Use at most 8 commands. Keep "uv pip install --system --exclude-newer <date>" for Python packages so
versions match the PR's time, install system packages with apt-get when needed, and end with pytest installed.`

function parseSetup(reply: string): { python: string; setup_commands: string[] } | undefined {
  const text = /```(?:json)?\s*\n([\s\S]*?)```/.exec(reply)?.[1] ?? reply.slice(reply.indexOf("{"))
  try {
    const data = JSON.parse(text.slice(0, text.lastIndexOf("}") + 1))
    const cmds = data.setup_commands
    if (!/^3\.\d{1,2}$/.test(String(data.python))) return undefined
    if (!Array.isArray(cmds) || !cmds.length || cmds.length > 8 || !cmds.every((c) => typeof c === "string" && c.trim() && c.length < 600 && !c.includes("\n")))
      return undefined
    return { python: String(data.python), setup_commands: cmds.map((c: string) => c.trim()) }
  } catch {
    return undefined
  }
}

/** No test result, or none passing even with the PR's own change: the environment is the problem. */
export const envBroken = (r: RunResult) => r.crashed || !Object.values(r.statuses).includes("passed")

async function testRuns(image: string, env: EnvConfig, split: Split, exec: Runner, memory?: string) {
  const select = [...split.testFiles].sort()
  let full: RunResult | undefined
  const once = async (patched: boolean, wholeSuite = false): Promise<RunResult> => {
    const box = await Container.start(image, exec, memory)
    try {
      for (const [path, content] of Object.entries(split.tests)) await box.write(path, content)
      if (patched) {
        const applied = await box.applyPatch(split.patch)
        if (applied.code !== 0) throw new SkipError("patch_apply_fail", applied.stderr.trim().slice(0, 500))
      }
      const result = await runTests(box, env, select)
      if (wholeSuite && !envBroken(result)) full = await runTests(box, env, [])
      return result
    } finally {
      await box.stop()
    }
  }
  const before = await once(false)
  const after = [await once(true, true)]
  if (!envBroken(after[0])) after.push(await once(true))
  return { before, after, full }
}

export async function mine(url: string, opts: Options) {
  const exec = opts.exec ?? run
  const sayAll = opts.progress ?? (() => {})
  const say = sayAll
  const repo = parseRepo(url)
  const out = opts.out ?? defaultOut(repo)
  mkdirSync(join(out, "prs"), { recursive: true })
  mkdirSync(join(out, "cache"), { recursive: true })
  const done = new Set(outcomes(out).filter((o) => o.status === "task" || !opts.retry).map((o) => o.pr))

  say(`1/5 mining ${repo.owner}/${repo.name}: merged PRs that close an issue and change tests and code`)
  const mined = await minePullRequests(repo, { scan: opts.scan, max: opts.max, since: opts.since, skip: done, progress: say }, exec)
  writeFileSync(join(out, "mined.json"), JSON.stringify({ scanned: mined.scanned, skipped: mined.skipped, at: new Date().toISOString() }, null, 2))
  say(`  scanned ${mined.scanned} PR(s): ${mined.candidates.length} candidate(s); skipped ${JSON.stringify(mined.skipped)}`)
  if (!mined.candidates.length) return { out, repo, made: [] as string[], funnel: funnel(out) }

  say(`  cloning ${repo.url}`)
  const clone = join(out, "repo")
  await ensureClone(repo.url, clone, exec)

  const made: string[] = []
  // Workers process PRs at the same time (build + tests); git on the shared clone runs one at a time.
  let gitChain: Promise<unknown> = Promise.resolve()
  const withGit = <T,>(fn: () => Promise<T>): Promise<T> => {
    const p = gitChain.then(fn, fn)
    gitChain = p.catch(() => undefined)
    return p
  }
  const processOne = async (pr: PullRequest, i: number) => {
    // with parallel workers, every line says which PR it is about
    const say = (line: string) => sayAll(line.startsWith("  ") ? `  #${pr.number} ${line.trimStart()}` : line)
    const tag = `[${i + 1}/${mined.candidates.length}] PR #${pr.number}`
    const record = (o: Omit<Outcome, "pr" | "ts">) =>
      writeFileSync(join(out, "prs", `${pr.number}.json`), JSON.stringify({ pr: pr.number, url: pr.url, ...o, ts: new Date().toISOString() }, null, 2))
    try {
      say(`${tag} 2/5 split at the base commit`)
      const split = await withGit(() => splitPullRequest(clone, pr.number, pr.baseRefOid, exec))
      say(`  base ${split.base.slice(0, 12)} · code: ${split.codeFiles.length} file(s) · hidden tests: ${split.testFiles.join(", ")}`)
      const files = await withGit(() => readFiles(clone, split.base, PY_FILES, exec))
      if (!["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"].some((f) => files[f] !== undefined))
        throw new SkipError("not_python", "no pyproject.toml, setup.py, setup.cfg or requirements.txt at the base commit (only Python repos for now)")
      const repoTar = join(out, "cache", `${split.base}.tar`)
      await withGit(async () => existsSync(repoTar) || archive(clone, split.base, repoTar, exec))

      let env = pythonEnv(files, pr.mergedAt)
      env.base_image = await pinImage(env.base_image, exec)
      let image = ""
      let runs: Awaited<ReturnType<typeof testRuns>> | undefined
      let tree: string[] | undefined
      for (let attempt = 1; attempt <= (opts.ask ? 2 : 1); attempt++) {
        let failure = ""
        try {
          say(`${tag} 3/5 environment ${env.base_image.split("@")[0]}${attempt > 1 ? " (setup agent's commands)" : ""}`)
          image = (await buildImage(repo.name, split.base, env, repoTar, exec)).tag
          say(`${tag} 4/5 tests before and after the change (offline sandbox)`)
          runs = await testRuns(image, env, split, exec, opts.memory)
          if (runs.after.some((r) => r.oom) || runs.before.oom)
            throw new SkipError("oom", `a test run was killed (exit 137) at the ${opts.memory ?? "2g"} memory limit; try --memory 4g`)
          if (!envBroken(runs.after[0])) break
          failure = runs.after[0].output
        } catch (e) {
          if (!(e instanceof BuildFailed)) throw e
          failure = e.output
        }
        runs = undefined
        if (!opts.ask || attempt > 1) throw new SkipError("env_failed", clip(failure, 1500))
        say(`  the environment does not run the tests; asking the setup agent`)
        tree ??= await withGit(() => listFiles(clone, split.base, exec))
        const reply = parseSetup(await opts.ask(SETUP_PROMPT(repo, pr, env, files, tree, failure)))
        if (!reply) throw new SkipError("env_failed", "the setup agent did not return usable commands; " + clip(failure, 1200))
        env = { ...env, base_image: await pinImage(`python:${reply.python}-slim`, exec), setup_commands: reply.setup_commands }
      }
      if (!runs) throw new SkipError("env_failed", "no test run")
      const extracted = decide(runs.before, runs.after, runs.full)
      say(`  FAIL_TO_PASS ${extracted.fail_to_pass.length} · PASS_TO_PASS ${extracted.pass_to_pass.length}` +
        (extracted.flaky.length ? ` · flaky ${extracted.flaky.length} (left out)` : "") +
        (extracted.still_failing.length ? ` · failing after the change ${extracted.still_failing.length} (left out)` : "") +
        (extracted.skipped.length ? ` · skipped ${extracted.skipped.length}` : "") +
        (extracted.not_green_in_full_suite?.length ? ` · not green in the full suite ${extracted.not_green_in_full_suite.length} (left out)` : ""))
      if (!extracted.fail_to_pass.length) throw new SkipError("no_fail_to_pass", "no PR test fails before the change and passes after it")

      const q = qualityOf(problemStatement(pr), split.patch, split.testPatch)
      if (q.flags.length)
        say(`  ⚠ for the reviewer: ${q.flags.join(", ")}${q.unmentioned_names.length ? ` (${q.unmentioned_names.join(", ")})` : ""}`)
      say(`${tag} 5/5 package`)
      const dir = writePackage({ repo, pr, split, env, extracted, statement: problemStatement(pr), kind: taskKind(pr),
        runs, repoTar, image }, join(out, "tasks"))
      const id = dir.split("/").pop()!
      made.push(id)
      record({ status: "task", task_id: id })
      say(`  ✓ task ${id} (pending review)`)
    } catch (e) {
      const reason = e instanceof SkipError ? e.code : "error"
      record({ status: "dropped", reason, detail: String(e instanceof Error ? e.message : e).slice(0, 2000) })
      say(`  ✗ dropped: ${reason}: ${String(e instanceof Error ? e.message : e).split("\n")[0].slice(0, 200)}`)
    }
  }
  const workers = Math.max(1, Math.min(opts.workers ?? 1, mined.candidates.length))
  if (workers > 1) sayAll(`  ${workers} workers`)
  let next = 0
  await Promise.all(Array.from({ length: workers }, async () => {
    while (next < mined.candidates.length) {
      const i = next++
      await processOne(mined.candidates[i], i)
    }
  }))
  made.sort()
  // every package holds its own repo.tar; the cache only served this run
  rmSync(join(out, "cache"), { recursive: true, force: true })
  const rows = exportJsonl(out)
  const warning = await syncRecords(out, exec)
  if (warning) say(`  ⚠ ${warning}`)
  say(`done: ${made.length} new task(s); ${rows} in ${join(out, "swe-bench.jsonl")}`)
  return { out, repo, made, funnel: funnel(out) }
}
