// Step 4, extract: run the PR's test files before and after its code change and keep the
// test IDs that flip (FAIL_TO_PASS) or stay green (PASS_TO_PASS), as SWE-bench does.
// Not the factory's gates: no mutants, no full-suite regression gate, just one run before
// and two after (a test that differs between the two "after" runs is flaky and left out).
import { type Container } from "./env.ts"
import { type EnvConfig, parseJunit, type TestStatus } from "./formats.ts"
import { clip, shQuote } from "./util.ts"

export type RunResult = {
  command: string
  exit_code: number
  statuses: Record<string, TestStatus>
  crashed: boolean // no test results at all (collection or build failure, timeout)
  oom: boolean // killed with exit 137: the container's memory limit (or an outside kill)
  output: string // the command's output tail, for the logs and the setup agent
}

const fileOf = (id: string) => id.split("::")[0]
const matches = (id: string, select: string[]) => select.some((s) => id === s || id.startsWith(s + "::") || fileOf(id) === s)

/** factory.runner.test_runner.build_command for `ids`/`files` selection. */
export function testCommand(env: EnvConfig, select: string[]): string {
  if (!select.length) return env.test_command.replace("{report}", shQuote(env.report_path)).replace("{select}", env.select_all).trim()
  const sel = env.select === "dirs" ? [...new Set(select.map((s) => "./" + (fileOf(s).split("/").slice(0, -1).join("/") || ".")))] : select.map(fileOf)
  return env.test_command.replace("{report}", shQuote(env.report_path)).replace("{select}", [...new Set(sel)].map(shQuote).join(" ")).trim()
}

export async function runTests(box: Container, env: EnvConfig, select: string[]): Promise<RunResult> {
  const dir = env.report_path.split("/").slice(0, -1).join("/") || "."
  await box.sh(`rm -rf ${shQuote(env.report_path)}; mkdir -p ${shQuote(dir)}`, 30_000)
  const command = testCommand(env, select)
  const r = await box.sh(command, env.test_timeout_s * 1000)
  const report = await box.read(env.report_path)
  const statuses: Record<string, TestStatus> = {}
  let parseError = ""
  if (report) {
    try {
      for (const t of parseJunit(report)) if (!select.length || matches(t.test_id, select)) statuses[t.test_id] = t.status
    } catch (e) {
      parseError = String(e)
    }
  }
  const output = clip((r.stdout + "\n" + r.stderr + (parseError ? "\n" + parseError : "")).trim().slice(-6000), 6000)
  return { command, exit_code: r.code, statuses, crashed: Object.keys(statuses).length === 0, oom: r.code === 137, output }
}

export type Extracted = {
  fail_to_pass: string[]
  pass_to_pass: string[]
  flaky: string[]
  still_failing: string[]
  skipped: string[] // skipped after the change (e.g. Windows-only tests): neither list
  not_green_in_full_suite?: string[] // left out of PASS_TO_PASS: the harness grades them in the full suite
}

/** Decide the hidden test lists from one run before the change and two after it.
 *  `full`: the whole suite after the change; the harness checks PASS_TO_PASS there, so a test
 *  that is not green in it (an unrelated module that does not import, test-order effects) is left out. */
export function decide(before: RunResult, after: RunResult[], full?: RunResult): Extracted {
  const ids = [...new Set(after.flatMap((a) => Object.keys(a.statuses)))].sort()
  const flaky = ids.filter((id) => new Set(after.map((a) => a.statuses[id] ?? "missing")).size > 1)
  const green = ids.filter((id) => !flaky.includes(id) && after.every((a) => a.statuses[id] === "passed"))
  const p2p = green.filter((id) => before.statuses[id] === "passed")
  const out: Extracted = {
    fail_to_pass: green.filter((id) => before.statuses[id] !== "passed"),
    pass_to_pass: full ? p2p.filter((id) => full.statuses[id] === "passed") : p2p,
    flaky,
    still_failing: ids.filter((id) => !flaky.includes(id) && !green.includes(id) && after[0].statuses[id] !== "skipped"),
    skipped: ids.filter((id) => !flaky.includes(id) && after[0].statuses[id] === "skipped"),
  }
  if (full) out.not_green_in_full_suite = p2p.filter((id) => full.statuses[id] !== "passed")
  return out
}
