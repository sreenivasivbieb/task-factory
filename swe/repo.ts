// Step 2, split: the pull request's change at its exact base commit, cut into the
// reference patch (code) and the hidden tests (test files), like SWE-bench's patch / test_patch.
import { execFileSync } from "node:child_process"
import { isTestPath } from "./github.ts"
import { must, run, type Runner } from "./util.ts"

export type Split = {
  base: string // merge-base of the PR head and its base branch: where the change starts
  head: string
  baseDate: string
  codeFiles: string[]
  testFiles: string[]
  patch: string // code change, applied by the reference solution
  testPatch: string // test change, for the SWE-bench export
  tests: Record<string, string> // hidden test files: path -> content after the PR
}

const git = (repo: string, args: string[], exec: Runner = run, input?: string) =>
  exec("git", ["-C", repo, "-c", "core.quotepath=off", ...args], { input, timeoutMs: 600_000 })

/** A full clone without a checkout (git archive needs every blob), reused across runs. */
export async function ensureClone(url: string, dir: string, exec: Runner = run): Promise<void> {
  const has = await exec("git", ["-C", dir, "rev-parse", "--git-dir"])
  if (has.code === 0) {
    await git(dir, ["fetch", "-q", "origin"], exec)
    return
  }
  await must(exec("git", ["clone", "-q", "--no-checkout", url, dir], { timeoutMs: 1_800_000 }), `git clone ${url}`)
}

export async function splitPullRequest(dir: string, prNumber: number, baseRefOid: string, exec: Runner = run): Promise<Split> {
  const ref = `refs/swe/pr-${prNumber}`
  await must(git(dir, ["fetch", "-q", "origin", `+refs/pull/${prNumber}/head:${ref}`], exec), `fetch PR #${prNumber}`)
  const head = (await must(git(dir, ["rev-parse", ref], exec), "rev-parse")).trim()
  if ((await git(dir, ["cat-file", "-e", `${baseRefOid}^{commit}`], exec)).code !== 0)
    await git(dir, ["fetch", "-q", "origin", baseRefOid], exec)
  const base = (await must(git(dir, ["merge-base", baseRefOid, head], exec), "merge-base (base branch was rewritten?)")).trim()
  const baseDate = (await must(git(dir, ["show", "-s", "--format=%cI", base], exec), "commit date")).trim()
  const status = await must(git(dir, ["diff", "--no-renames", "--name-status", base, head], exec), "diff")
  const codeFiles: string[] = []
  const testFiles: string[] = []
  for (const line of status.split("\n").filter(Boolean)) {
    const [st, path] = line.split("\t")
    if (isTestPath(path)) {
      if (st.startsWith("D")) throw new SkipError("deletes_test_file", `the PR deletes ${path}; hidden tests can only add or change files`)
      testFiles.push(path)
    } else codeFiles.push(path)
  }
  if (!testFiles.length) throw new SkipError("no_test_change", "no test file changed between base and head")
  if (!codeFiles.length) throw new SkipError("only_test_change", "only test files changed")
  const patch = await must(git(dir, ["diff", "--binary", "--no-renames", base, head, "--", ...codeFiles], exec), "diff code")
  const testPatch = await must(git(dir, ["diff", "--no-renames", base, head, "--", ...testFiles], exec), "diff tests")
  const tests: Record<string, string> = {}
  for (const path of testFiles) {
    // raw bytes: the harness writes hidden tests as UTF-8 text, so anything else cannot be one
    // (a test may contain U+FFFD itself, so decoding leniently and searching for it is wrong)
    const bytes = execFileSync("git", ["-C", dir, "show", `${head}:${path}`], { maxBuffer: 256 * 1024 * 1024 })
    try {
      tests[path] = new TextDecoder("utf-8", { fatal: true }).decode(bytes)
    } catch {
      throw new SkipError("binary_test_file", `${path} is not UTF-8 text`)
    }
    if (tests[path].includes("\u0000")) throw new SkipError("binary_test_file", `${path} contains NUL bytes`)
  }
  return { base, head, baseDate, codeFiles, testFiles, patch, testPatch, tests }
}

/** Files at the base commit, for environment detection: path -> content (missing files are left out). */
export async function readFiles(dir: string, commit: string, paths: string[], exec: Runner = run): Promise<Record<string, string>> {
  const out: Record<string, string> = {}
  for (const path of paths) {
    const r = await exec("git", ["-C", dir, "show", `${commit}:${path}`], { timeoutMs: 120_000 })
    if (r.code === 0) out[path] = r.stdout
  }
  return out
}

export async function listFiles(dir: string, commit: string, exec: Runner = run): Promise<string[]> {
  return (await must(git(dir, ["ls-tree", "-r", "--name-only", commit], exec), "ls-tree")).split("\n").filter(Boolean)
}

export async function archive(dir: string, commit: string, outTar: string, exec: Runner = run): Promise<void> {
  await must(git(dir, ["archive", "--format=tar", "-o", outTar, commit], exec), "git archive")
}

/** A candidate that is not a task, with a stable reason code (shown in the funnel). */
export class SkipError extends Error {
  readonly code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}
