// Step 1, mine: merged pull requests that close an issue and change both tests and code.
// Uses the GitHub CLI (`gh api graphql`), so the user's own `gh auth login` is the only setup.
import { run, type Runner } from "./util.ts"

export type Issue = { number: number; title: string; body: string; url: string; labels: string[] }
export type PullRequest = {
  number: number
  title: string
  body: string
  url: string
  author: string
  createdAt: string
  mergedAt: string
  baseRefOid: string
  headRefOid: string
  labels: string[]
  files: { path: string; changeType: string }[]
  issues: Issue[]
}
export type RepoRef = { owner: string; name: string; url: string }

/** https://github.com/o/r(.git), git@github.com:o/r(.git), ssh://git@github.com/o/r, or plain o/r. */
export function parseRepo(input: string): RepoRef {
  const s = input.trim().replace(/\/+$/, "").replace(/\.git$/, "")
  const m =
    /^(?:https?:\/\/)?(?:[^@/]+@)?github\.com[/:]([\w.-]+)\/([\w.-]+)(?:\/.*)?$/.exec(s) ??
    /^ssh:\/\/git@github\.com\/([\w.-]+)\/([\w.-]+)$/.exec(s) ??
    /^([\w.-]+)\/([\w.-]+)$/.exec(s)
  if (!m || m[1] === "." || m[2] === "." || m[1].includes("..") || m[2].includes(".."))
    throw new Error(`not a GitHub repository: ${input} (use https://github.com/OWNER/REPO)`)
  return { owner: m[1], name: m[2], url: `https://github.com/${m[1]}/${m[2]}.git` }
}

// Test files: the Python ecosystem's test_paths plus the shared test folders (factory/ecosystems.toml).
const TEST_PATH = /(^|\/)(tests?|__tests__|specs?|testing)\/|(^|\/)test_[^/]*\.py$|_test\.py$|(^|\/)conftest\.py$/
export const isTestPath = (path: string) => TEST_PATH.test(path)

const QUERY = `query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, first: 40, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body url createdAt mergedAt baseRefOid headRefOid
        author { login }
        labels(first: 10) { nodes { name } }
        files(first: 100) { totalCount nodes { path changeType } }
        closingIssuesReferences(first: 5) { nodes { number title body url labels(first: 10) { nodes { name } } } }
      }
    }
  }
}`

/** Why a pull request is not a task candidate, or null when it is one (the SWE-bench rules). */
export function rejectReason(pr: PullRequest, totalFiles = pr.files.length): string | null {
  if (!pr.issues.length) return "no_linked_issue"
  if (totalFiles > pr.files.length) return "too_many_files"
  const tests = pr.files.filter((f) => isTestPath(f.path))
  if (!tests.length) return "no_test_change"
  if (tests.length === pr.files.length) return "only_test_change"
  if (!pr.issues.some((i) => (i.body ?? "").trim() || i.title.trim())) return "empty_issue"
  return null
}

export type MineOptions = { scan: number; max: number; since?: string; skip?: Set<number>; progress?: (line: string) => void }

/** Newest merged pull requests first; stops after `scan` PRs, `max` candidates or the `since` date. */
export async function minePullRequests(repo: RepoRef, opts: MineOptions, exec: Runner = run) {
  const candidates: PullRequest[] = []
  const skipped: Record<string, number> = {}
  let cursor: string | undefined
  let scanned = 0
  const since = opts.since ? Date.parse(opts.since) : undefined
  while (scanned < opts.scan && candidates.length < opts.max) {
    const args = ["api", "graphql", "-f", `query=${QUERY}`, "-F", `owner=${repo.owner}`, "-F", `name=${repo.name}`]
    if (cursor) args.push("-f", `cursor=${cursor}`)
    const r = await exec("gh", args, { timeoutMs: 120_000 })
    if (r.code === 127) throw new Error("the GitHub CLI (gh) is not installed: https://cli.github.com")
    if (r.code !== 0) throw new Error(`GitHub query failed: ${(r.stderr || r.stdout).trim().slice(0, 600)} (try: gh auth login)`)
    const page = JSON.parse(r.stdout)?.data?.repository?.pullRequests
    if (!page) throw new Error(`repository ${repo.owner}/${repo.name} not found or not visible to your gh login`)
    let old = false
    for (const n of page.nodes ?? []) {
      if (scanned >= opts.scan || candidates.length >= opts.max) break
      if (since !== undefined && Date.parse(n.createdAt) < since) {
        old = true
        break
      }
      scanned++
      const pr: PullRequest = {
        number: n.number, title: n.title ?? "", body: n.body ?? "", url: n.url, author: n.author?.login ?? "ghost",
        createdAt: n.createdAt, mergedAt: n.mergedAt, baseRefOid: n.baseRefOid, headRefOid: n.headRefOid,
        labels: (n.labels?.nodes ?? []).map((l: any) => l.name),
        files: (n.files?.nodes ?? []).map((f: any) => ({ path: f.path, changeType: f.changeType })),
        issues: (n.closingIssuesReferences?.nodes ?? []).map((i: any) => ({
          number: i.number, title: i.title ?? "", body: i.body ?? "", url: i.url,
          labels: (i.labels?.nodes ?? []).map((l: any) => l.name),
        })),
      }
      if (opts.skip?.has(pr.number)) {
        skipped.already_done = (skipped.already_done ?? 0) + 1
        continue
      }
      const why = rejectReason(pr, n.files?.totalCount ?? pr.files.length)
      if (why) skipped[why] = (skipped[why] ?? 0) + 1
      else {
        candidates.push(pr)
        opts.progress?.(`  candidate PR #${pr.number}: ${pr.title} (closes ${pr.issues.map((i) => "#" + i.number).join(", ")})`)
      }
    }
    if (old || !page.pageInfo?.hasNextPage) break
    cursor = page.pageInfo.endCursor
  }
  return { candidates, scanned, skipped }
}

/** The problem statement: the linked issues as written (SWE-bench), minus HTML comments from issue templates. */
export function problemStatement(pr: PullRequest): string {
  const parts = pr.issues.map((i) => {
    const body = (i.body ?? "").replace(/<!--[\s\S]*?-->/g, "").replace(/\r\n/g, "\n").trim()
    return `# ${i.title.trim()}\n\n${body || "(no description)"}\n`
  })
  return parts.join("\n")
}

/** bugfix unless the issue or PR is labelled as a feature or enhancement. */
export function taskKind(pr: PullRequest): "bugfix" | "feature" {
  const labels = [...pr.labels, ...pr.issues.flatMap((i) => i.labels)].map((l) => l.toLowerCase())
  return labels.some((l) => /feature|enhancement|new/.test(l)) ? "feature" : "bugfix"
}
