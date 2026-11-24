// node --test tests/ts   (Node 22.18+ runs TypeScript directly)
import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"
import { pythonEnv, pythonFor, testExtra, testGroup } from "../../swe/env.ts"
import { decide, testCommand, type RunResult } from "../../swe/extract.ts"
import { parseJunit, tomlDumps } from "../../swe/formats.ts"
import { isTestPath, parseRepo, problemStatement, type PullRequest, rejectReason, taskKind } from "../../swe/github.ts"
import { checksums, taskID } from "../../swe/package.ts"
import { newNames, qualityOf } from "../../swe/quality.ts"
import { ensureClone, SkipError, splitPullRequest } from "../../swe/repo.ts"
import { pyJson, shQuote, slugify } from "../../swe/util.ts"

const pr = (over: Partial<PullRequest> = {}): PullRequest => ({
  number: 7, title: "Fix rounding", body: "", url: "https://github.com/o/r/pull/7", author: "dev",
  createdAt: "2023-03-01T00:00:00Z", mergedAt: "2023-03-02T00:00:00Z", baseRefOid: "b", headRefOid: "h", labels: [],
  files: [{ path: "pkg/money.py", changeType: "MODIFIED" }, { path: "tests/test_money.py", changeType: "MODIFIED" }],
  issues: [{ number: 3, title: "Rounding is wrong", body: "<!-- template -->\r\nSteps:\r\n1. x", url: "u", labels: [] }],
  ...over,
})

test("parseRepo accepts the usual GitHub forms and refuses others", () => {
  for (const s of ["https://github.com/pallets/click", "https://github.com/pallets/click.git", "git@github.com:pallets/click.git",
    "github.com/pallets/click/pulls", "pallets/click"])
    assert.deepEqual(parseRepo(s), { owner: "pallets", name: "click", url: "https://github.com/pallets/click.git" })
  for (const s of ["https://gitlab.com/a/b", "not a url", "../x/y"]) assert.throws(() => parseRepo(s))
})

test("candidate rules: a linked issue, tests and code both changed", () => {
  assert.equal(rejectReason(pr()), null)
  assert.equal(rejectReason(pr({ issues: [] })), "no_linked_issue")
  assert.equal(rejectReason(pr({ files: [{ path: "pkg/a.py", changeType: "MODIFIED" }] })), "no_test_change")
  assert.equal(rejectReason(pr({ files: [{ path: "tests/test_a.py", changeType: "ADDED" }] })), "only_test_change")
  assert.equal(rejectReason(pr(), 500), "too_many_files")
  assert.ok(isTestPath("src/pkg/tests/data/x.json") && isTestPath("conftest.py") && isTestPath("pkg/a_test.py"))
  assert.ok(!isTestPath("pkg/testing_utils.py") && !isTestPath("docs/index.md"))
})

test("problem statement is the issue text without template comments; kind from labels", () => {
  assert.equal(problemStatement(pr()), "# Rounding is wrong\n\nSteps:\n1. x\n")
  assert.equal(taskKind(pr()), "bugfix")
  assert.equal(taskKind(pr({ labels: ["Enhancement"] })), "feature")
})

test("Python environment: era version, test extra, time-travel installs", () => {
  assert.equal(pythonFor("2021-05-01T00:00:00Z"), "3.9")
  assert.equal(pythonFor("2025-01-01T00:00:00Z"), "3.13")
  assert.equal(pythonFor("2019-01-01T00:00:00Z", ">=3.10"), "3.10")
  assert.equal(testExtra({ "pyproject.toml": "[project]\nname='x'\n[project.optional-dependencies]\ntests = ['pytest']\n" }), "tests")
  assert.equal(testExtra({ "setup.py": "setup(extras_require={'testing': ['pytest']})" }), "testing")
  assert.equal(testExtra({ "pyproject.toml": "[project]\noptional-dependencies.tests = [\n  'freezegun',\n]\n" }), "tests")
  assert.equal(testExtra({ "setup.py": "setup()" }), undefined)
  const pep735 = { "pyproject.toml": "[project]\nname='click'\n[dependency-groups]\ndev = ['ruff']\ntests = [\n  'pytest',\n]\n[tool.x]\n" }
  assert.equal(testGroup(pep735), "tests")
  assert.ok(pythonEnv(pep735, "2025-01-01T00:00:00Z").setup_commands.includes("uv pip install --system --exclude-newer 2025-01-01T00:00:00Z -q --group tests"))
  const env = pythonEnv({ "setup.py": "setup(extras_require={'test': []})", "requirements-dev.txt": "" }, "2022-06-01T10:20:30.000Z")
  assert.equal(env.base_image, "python:3.10-slim")
  assert.deepEqual(env.setup_commands, [
    "pip install -q uv",
    "uv pip install --system --exclude-newer 2022-06-01T10:20:30Z -q -e '.[test]'",
    "uv pip install --system --exclude-newer 2022-06-01T10:20:30Z -q -r requirements-dev.txt",
    "uv pip install --system --exclude-newer 2022-06-01T10:20:30Z -q pytest",
  ])
  // only test modules reach pytest: a data file or helper as an argument aborts the whole run
  assert.equal(testCommand(env, ["tests/test_b.py", "tests/test_a.py::t", "tests/data x.json"]),
    "python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 --junitxml=.factory/report.xml " +
    `$(for f in tests/test_b.py tests/test_a.py 'tests/data x.json'; do case "\${f##*/}" in test_*.py|*_test.py) printf '%s\\n' "$f";; esac; done)`)
})

const result = (statuses: Record<string, any>, crashed = false): RunResult => ({ command: "", exit_code: 0, statuses, crashed, oom: false, output: "" })

test("FAIL_TO_PASS / PASS_TO_PASS from one run before and two after", () => {
  const before = result({ "t::a": "passed", "t::b": "failed", "t::flaky": "passed", "t::broken": "passed" })
  const after = [
    result({ "t::a": "passed", "t::b": "passed", "t::new": "passed", "t::flaky": "passed", "t::broken": "failed" }),
    result({ "t::a": "passed", "t::b": "passed", "t::new": "passed", "t::flaky": "failed", "t::broken": "failed" }),
  ]
  assert.deepEqual(decide(before, after), {
    fail_to_pass: ["t::b", "t::new"], pass_to_pass: ["t::a"], flaky: ["t::flaky"], still_failing: ["t::broken"], skipped: [],
  })
  const full = result({ "t::a": "error", "t::b": "passed", "t::new": "passed" })
  assert.deepEqual(decide(before, after, full).pass_to_pass, [])
  assert.deepEqual(decide(before, after, full).not_green_in_full_suite, ["t::a"])
  // before crashed (e.g. ImportError: the feature does not exist yet): every green test flips
  assert.deepEqual(decide(result({}, true), [after[0], after[0]]).fail_to_pass, ["t::a", "t::b", "t::flaky", "t::new"])
})

test("JUnit IDs like factory/runner/parsers (pytest file attribute, classes, entities, repeats)", () => {
  const xml = `<?xml version="1.0"?><testsuites><testsuite>
    <testcase classname="tests.test_a" name="test_x" file="tests/test_a.py" time="0.1"/>
    <testcase classname="tests.test_a.TestK" name="test_y[a&amp;b]" file="/repo/tests/test_a.py"><failure message="x">boom</failure></testcase>
    <testcase classname="tests.test_a" name="test_x" file="tests/test_a.py"><skipped/></testcase>
    <testcase classname="Pkg.Cls" name="m"><error message="e"/></testcase>
  </testsuite></testsuites>`
  assert.deepEqual(parseJunit(xml), [
    { test_id: "tests/test_a.py::test_x", status: "passed" },
    { test_id: "tests/test_a.py::TestK::test_y[a&b]", status: "failed" },
    { test_id: "tests/test_a.py::test_x[2]", status: "skipped" },
    { test_id: "Pkg.Cls::m", status: "error" },
  ])
  assert.throws(() => parseJunit("not xml"))
})

test("helpers match their Python twins", () => {
  assert.equal(slugify("Hello, World -- Task!", 32), "hello-world-task")
  assert.equal(slugify("***"), "task")
  assert.equal(pyJson(["a", "é", ["x"], "3"]), '["a", "\\u00e9", ["x"], "3"]')
  assert.equal(shQuote("a b'c"), "'a b'\"'\"'c'")
  assert.equal(tomlDumps({ a: 1, l: ["x", "y", "z", "w"], t: { k: true, e: {} } }),
    'a = 1\nl = [\n  "x",\n  "y",\n  "z",\n  "w",\n]\n\n[t]\nk = true\n\n[t.e]\n')
  assert.equal(taskID({ repo: parseRepo("o/My.Repo"), pr: pr(), split: { base: "abcdef1234" } as any }),
    "my-repo-abcdef1-rounding-is-wrong-pr7")
})

function git(cwd: string, ...args: string[]) {
  return execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", ...args], { cwd, encoding: "utf8" }).trim()
}

test("split: code patch and hidden tests at the PR's merge-base, from refs/pull/N/head", async () => {
  const root = mkdtempSync(join(tmpdir(), "swe-split-"))
  const work = join(root, "work")
  mkdirSync(join(work, "pkg"), { recursive: true })
  mkdirSync(join(work, "tests"))
  writeFileSync(join(work, "pkg", "money.py"), "def r(x):\n    return x\n")
  writeFileSync(join(work, "tests", "test_money.py"), "def test_old():\n    assert True\n")
  git(work, "init", "-q")
  git(work, "add", "-A")
  git(work, "commit", "-qm", "base")
  const base = git(work, "rev-parse", "HEAD")
  git(work, "checkout", "-qb", "fix")
  writeFileSync(join(work, "pkg", "money.py"), "def r(x):\n    return round(x)\n")
  writeFileSync(join(work, "tests", "test_money.py"), "def test_old():\n    assert True\n\ndef test_round():\n    assert True\n")
  git(work, "commit", "-qam", "fix")
  git(work, "checkout", "-q", "main")
  writeFileSync(join(work, "README.md"), "later work on main\n")
  git(work, "add", "-A")
  git(work, "commit", "-qm", "main moves on")
  const tip = git(work, "rev-parse", "HEAD")
  const bare = join(root, "remote.git")
  git(root, "clone", "-q", "--bare", work, bare)
  git(work, "push", "-q", bare, "fix:refs/pull/1/head")
  const clone = join(root, "clone")
  await ensureClone(bare, clone)
  const split = await splitPullRequest(clone, 1, tip)
  assert.equal(split.base, base)
  assert.deepEqual([split.codeFiles, split.testFiles], [["pkg/money.py"], ["tests/test_money.py"]])
  assert.match(split.patch, /\+    return round\(x\)/)
  assert.doesNotMatch(split.patch, /test_round|README/)
  assert.match(split.tests["tests/test_money.py"], /def test_round/)
  // a test file may contain U+FFFD itself: still UTF-8 text
  git(work, "checkout", "-qb", "fffd", base)
  writeFileSync(join(work, "pkg", "money.py"), "y = 2\n")
  writeFileSync(join(work, "tests", "test_money.py"), "def test_fffd():\n    assert '\ufffd'\n")
  git(work, "commit", "-qam", "fffd")
  git(work, "push", "-q", bare, "fffd:refs/pull/3/head")
  assert.match((await splitPullRequest(clone, 3, tip)).tests["tests/test_money.py"], /\ufffd/)
  git(work, "checkout", "-q", "main")
  // a PR that deletes a test file cannot be a task
  git(work, "checkout", "-qb", "del", base)
  git(work, "rm", "-q", "tests/test_money.py")
  writeFileSync(join(work, "pkg", "money.py"), "x = 1\n")
  mkdirSync(join(work, "tests"), { recursive: true })
  writeFileSync(join(work, "tests", "test_new.py"), "def test_n():\n    pass\n")
  git(work, "add", "-A")
  git(work, "commit", "-qm", "del")
  git(work, "push", "-q", bare, "del:refs/pull/2/head")
  await assert.rejects(splitPullRequest(clone, 2, tip), (e: any) => e instanceof SkipError && e.code === "deletes_test_file")
})

test("quality flags: tests that call a new name the issue never mentions", () => {
  const patch = "+def make_context(x):\n+    return x\n+def _helper():\n-def moved():\n+def moved():\n+class Shell:\n"
  assert.deepEqual(newNames(patch), ["Shell", "make_context"])
  const tests = "+from pkg import make_context, Shell\n+def test_it():\n+    assert make_context(1)\n"
  const long = "x".repeat(200)
  assert.deepEqual(qualityOf(`# Bug\n\nCalling Shell() fails. ${long}`, patch, tests).unmentioned_names, ["make_context"])
  assert.deepEqual(qualityOf(`# Bug\n\nAdd make_context; Shell() fails. ${long}`, patch, tests).flags, [])
  assert.deepEqual(qualityOf("# Bug\n\n(no description)\n", "", "").flags, ["short_problem_statement"])
})

test("checksums cover the task files in Python's path order", () => {
  const dir = mkdtempSync(join(tmpdir(), "swe-sum-"))
  for (const rel of ["tests/files/a-b/x.py", "tests/files/a/x.py", "instruction.md", "validation/log.json", "swe.json"]) {
    mkdirSync(join(dir, rel, ".."), { recursive: true })
    writeFileSync(join(dir, rel), rel)
  }
  assert.deepEqual(Object.keys(checksums(dir)), ["instruction.md", "tests/files/a/x.py", "tests/files/a-b/x.py"])
  assert.equal(readFileSync(join(dir, "instruction.md"), "utf8"), "instruction.md")
})
