// Prints what the TypeScript side writes, for tests/test_swe_parity.py to compare with Python.
import { readFileSync } from "node:fs"
import { pythonEnv } from "../../swe/env.ts"
import { imageTag, parseJunit, renderDockerfile, tomlDumps } from "../../swe/formats.ts"
import { parseRepo } from "../../swe/github.ts"
import { taskMeta } from "../../swe/package.ts"

const [reportA, reportB] = process.argv.slice(2)
const env = pythonEnv({ "pyproject.toml": "[project]\nname='x'\nrequires-python='>=3.8'\n[project.optional-dependencies]\ntest=['pytest']\n", "requirements.txt": "" }, "2023-05-06T07:08:09Z", "python:3.11-slim@sha256:" + "a".repeat(64))
const meta = taskMeta({
  repo: parseRepo("https://github.com/owner/Some.Repo"),
  pr: { number: 42, title: "PR", body: "", url: "https://github.com/owner/Some.Repo/pull/42", author: "dev", createdAt: "", mergedAt: "",
    baseRefOid: "", headRefOid: "", labels: [], files: [], issues: [{ number: 1, title: "Crash on “unicode” input", body: "", url: "", labels: [] }] },
  split: { base: "0123456789abcdef0123456789abcdef01234567", head: "", baseDate: "", codeFiles: ["pkg/a.py"], testFiles: ["tests/test_b.py", "tests/test_a.py"], patch: "", testPatch: "", tests: {} },
  env, extracted: { fail_to_pass: ["tests/test_a.py::test_x"], pass_to_pass: ["tests/test_a.py::test_y", "tests/test_b.py::t[1]", "tests/test_b.py::t[2]", "tests/test_b.py::u"], flaky: [], still_failing: [] },
  statement: "", kind: "bugfix", runs: { before: {} as any, after: [] }, repoTar: "", image: "",
} as any)
process.stdout.write(JSON.stringify({
  env, env_json: JSON.stringify(env, null, 2) + "\n", dockerfile: renderDockerfile(env),
  tag: imageTag("Some.Repo", meta.commit_sha, env), toml: tomlDumps(meta),
  junit: [reportA, reportB].map((p) => parseJunit(readFileSync(p, "utf8"))),
}))
