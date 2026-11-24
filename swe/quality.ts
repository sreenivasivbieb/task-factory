// Quality flags for a mined task, for the reviewer (nothing is dropped because of them).
// The known SWE-bench flaws they look for: hidden tests that call a new function or class the
// issue never names (the solver cannot guess the name), a problem statement too short to act on,
// and very large changes.

export type Quality = {
  flags: string[]
  unmentioned_names: string[] // new public names the hidden tests use, not in the problem statement
  patch_lines: number // added + removed lines in the reference patch
  statement_chars: number
}

const DEF = /^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)/

/** Public functions and classes the reference patch adds (names starting with _ are private). */
export function newNames(patch: string): string[] {
  const added = new Set<string>()
  const removed = new Set<string>()
  for (const line of patch.split("\n")) {
    const plus = DEF.exec(line)
    if (plus) added.add(plus[1])
    const minus = /^-\s*(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)/.exec(line)
    if (minus) removed.add(minus[1])
  }
  return [...added].filter((n) => !removed.has(n)).sort() // a moved or re-indented definition is not new
}

export function qualityOf(statement: string, patch: string, testPatch: string): Quality {
  const testAdded = testPatch.split("\n").filter((l) => l.startsWith("+") && !l.startsWith("+++")).join("\n")
  const used = newNames(patch).filter((n) => new RegExp(`\\b${n}\\b`).test(testAdded))
  const unmentioned = used.filter((n) => !new RegExp(`\\b${n}\\b`).test(statement))
  const patchLines = patch.split("\n").filter((l) => /^[+-]/.test(l) && !/^(\+\+\+|---) /.test(l)).length
  const body = statement.replace(/^# .*$/m, "").trim()
  const flags: string[] = []
  if (unmentioned.length) flags.push("tests_use_names_not_in_issue")
  if (body.length < 120 || body === "(no description)") flags.push("short_problem_statement")
  if (patchLines > 400) flags.push("large_change")
  return { flags, unmentioned_names: unmentioned, patch_lines: patchLines, statement_chars: statement.length }
}
