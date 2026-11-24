// Step 3, environment: a Docker image of the repo at the PR's exact base commit, with its
// dependencies as they were when the PR merged ("time-travel" install with uv --exclude-newer),
// built with the same Dockerfile and tag as the Python side, so the harness reuses it as is.
import { copyFileSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { type EnvConfig, FIXED_ENV, imageTag, renderDockerfile } from "./formats.ts"
import { run, shQuote, type Runner } from "./util.ts"

export const PY_FILES = ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt",
  "requirements-test.txt", "requirements_dev.txt", "requirements_test.txt", "dev-requirements.txt",
  "test-requirements.txt", "tox.ini", "pytest.ini"]

// Paths a solver may not change: grading undoes them (written into env.json, so the package is graded
// the same way everywhere). Python's test paths, the shared test folders and its build files.
export const PROTECTED =
  "(^|/)(tests?|__tests__|specs?|testing)/|(^|/)test_[^/]*\\.py$|_test\\.py$|(^|/)conftest\\.py$|\\.pth$|" +
  "(^|/)(pyproject\\.toml|pytest\\.ini|setup\\.cfg|sitecustomize\\.py|tox\\.ini|usercustomize\\.py)$"

export const PYTEST_COMMAND =
  "python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 --junitxml={report} " +
  `$(for f in {select}; do case "\${f##*/}" in test_*.py|*_test.py) printf '%s\\n' "$f";; esac; done)`

/** The newest Python that was current at `date` (old code rarely runs on a newer one). */
export function pythonFor(date: string, requiresPython?: string): string {
  const t = Date.parse(date)
  const eras: [string, string][] = [["2020-10-05", "3.8"], ["2021-10-04", "3.9"], ["2022-10-24", "3.10"], ["2023-10-02", "3.11"], ["2024-10-07", "3.12"]]
  let minor = Number((eras.find(([d]) => t < Date.parse(d)) ?? ["", "3.13"])[1].split(".")[1])
  const m = /(?:>=|~=|==)\s*3\.(\d+)/.exec(requiresPython ?? "")
  if (m && Number(m[1]) > minor) minor = Number(m[1])
  return `3.${minor}`
}

/** The test extra a project declares (pyproject or setup.py), if any. */
export function testExtra(files: Record<string, string>): string | undefined {
  const names = ["test", "tests", "testing", "dev"]
  const py = files["pyproject.toml"] ?? ""
  const section = /\[project\.optional-dependencies\]([\s\S]*?)(\n\[|$)/.exec(py)?.[1] ?? ""
  for (const n of names) if (new RegExp(`^\\s*["']?${n}["']?\\s*=`, "m").test(section)) return n
  for (const n of names) if (new RegExp(`^\\s*optional-dependencies\\.["']?${n}["']?\\s*=`, "m").test(py)) return n
  const inline = /optional-dependencies\s*=\s*\{([^}]*)\}/.exec(py)?.[1] ?? ""
  for (const n of names) if (new RegExp(`\\b${n}\\s*=`).test(inline)) return n
  const setup = (files["setup.py"] ?? "") + "\n" + (files["setup.cfg"] ?? "")
  for (const n of names) if (new RegExp(`["']${n}["']\\s*:|^\\s*${n}\\s*=`, "m").test(setup) && /extras_require/.test(setup)) return n
  return undefined
}

/** A PEP 735 dependency group with the test tools ([dependency-groups] tests = [...]), if any. */
export function testGroup(files: Record<string, string>): string | undefined {
  const section = /\[dependency-groups\]([\s\S]*?)(\n\[|$)/.exec(files["pyproject.toml"] ?? "")?.[1] ?? ""
  return ["tests", "test", "testing"].find((n) => new RegExp(`^\\s*["']?${n}["']?\\s*=`, "m").test(section))
}

/** EnvConfig for a Python repo at one commit. `until`: install packages released before this moment. */
export function pythonEnv(files: Record<string, string>, until: string, baseImage?: string): EnvConfig {
  const requires = /requires-python\s*=\s*["']([^"']+)["']/.exec(files["pyproject.toml"] ?? "")?.[1]
    ?? /python_requires\s*=\s*["']?([^"'\n,)]+)/.exec((files["setup.py"] ?? "") + "\n" + (files["setup.cfg"] ?? ""))?.[1]
  const version = pythonFor(until, requires)
  const stamp = new Date(Date.parse(until)).toISOString().replace(/\.\d{3}Z$/, "Z")
  const uv = `uv pip install --system --exclude-newer ${stamp} -q`
  const setup = ["pip install -q uv"]
  const project = files["pyproject.toml"] !== undefined || files["setup.py"] !== undefined
  if (project) {
    const extra = testExtra(files)
    setup.push(`${uv} -e ${shQuote(extra ? `.[${extra}]` : ".")}`)
  }
  const group = testGroup(files)
  if (group) setup.push(`${uv} --group ${group}`)
  for (const req of PY_FILES.filter((f) => f.endsWith(".txt"))) if (files[req] !== undefined) setup.push(`${uv} -r ${req}`)
  setup.push(`${uv} pytest`)
  return {
    schema_version: 1,
    base_image: baseImage ?? `python:${version}-slim`,
    setup_commands: setup,
    // Hidden test files can include conftest.py, data files and helpers; pytest aborts the whole run
    // when such a file is a command-line argument, so only test modules are passed (the rest is
    // still written into the repo). The harness passes every hidden file: the filter lives here.
    test_command: PYTEST_COMMAND,
    test_timeout_s: 900,
    env_vars: {},
    workdir: "/repo",
    framework: "pytest",
    report_format: "junit",
    report_path: ".factory/report.xml",
    select: "ids",
    select_all: "",
    deselect: "--deselect {id}",
    helper_files: {},
    artifacts: [".factory/", "*.egg-info/", "__pycache__/", "*.pyc", ".pytest_cache/", ".venv/"],
    protected_paths: PROTECTED,
  }
}

const pinned = new Map<string, string>()

/** Resolve an image tag to its registry digest, so a rebuild anywhere uses the same base. */
export async function pinImage(image: string, exec: Runner = run): Promise<string> {
  if (image.includes("@sha256:")) return image
  if (pinned.has(image)) return pinned.get(image)!
  const inspect = async () => exec("docker", ["image", "inspect", "--format", "{{json .RepoDigests}}", image])
  let r = await inspect()
  if (r.code !== 0) {
    const pull = await exec("docker", ["pull", "-q", image], { timeoutMs: 1_800_000 })
    if (pull.code !== 0) throw new Error(`docker pull ${image} failed: ${pull.stderr.trim().slice(-400)}`)
    r = await inspect()
  }
  const digests: string[] = JSON.parse(r.stdout || "[]") ?? []
  const repo = image.split(":")[0]
  const match = digests.find((d) => [repo, `docker.io/library/${repo}`, `docker.io/${repo}`].includes(d.split("@")[0])) ?? digests[0]
  const out = match?.includes("@") ? `${image}@${match.split("@")[1]}` : image
  pinned.set(image, out)
  return out
}

export async function imageExists(tag: string, exec: Runner = run) {
  return (await exec("docker", ["image", "inspect", tag])).code === 0
}

/** Build (or reuse) the image for `repoName` at `commit` from a `git archive` tarball. */
export async function buildImage(repoName: string, commit: string, env: EnvConfig, repoTar: string, exec: Runner = run) {
  const tag = imageTag(repoName, commit, env)
  if (await imageExists(tag, exec)) return { tag, log: "reused" }
  const ctx = mkdtempSync(join(tmpdir(), "swe-build-"))
  try {
    writeFileSync(join(ctx, "Dockerfile"), renderDockerfile(env))
    copyFileSync(repoTar, join(ctx, "repo.tar")) // not `cp`: it does not exist on Windows
    const r = await exec("docker", ["build", "-q", "-t", tag, ctx], { timeoutMs: 3_600_000 })
    if (r.code !== 0) throw new BuildFailed((r.stderr + "\n" + r.stdout).trim().slice(-4000))
    return { tag, log: "built" }
  } finally {
    rmSync(ctx, { recursive: true, force: true })
  }
}

export class BuildFailed extends Error {
  readonly output: string
  constructor(output: string) {
    super("docker build failed")
    this.output = output
  }
}

/** One isolated container: no network, CPU/memory/process limits, the non-root `factory` user. */
export class Container {
  readonly id: string
  private user: string | undefined
  private exec: Runner
  private constructor(id: string, user: string | undefined, exec: Runner) {
    this.id = id
    this.user = user
    this.exec = exec
  }

  static async start(image: string, exec: Runner = run, memory = "2g"): Promise<Container> {
    const args = ["run", "-d", "--network", "none", "--memory", memory, "--cpus", "2", "--pids-limit", "512"]
    for (const [k, v] of Object.entries(FIXED_ENV)) args.push("-e", `${k}=${v}`)
    args.push("--entrypoint", "sleep", image, "infinity")
    const r = await exec("docker", args)
    if (r.code !== 0) throw new Error(`could not start a container from ${image}: ${r.stderr.trim()}`)
    const hasUser = (await exec("docker", ["run", "--rm", "--network", "none", "--entrypoint", "id", image, "-u", "factory"])).code === 0
    return new Container(r.stdout.trim(), hasUser ? "factory" : undefined, exec)
  }

  sh(script: string, timeoutMs = 600_000, input?: string) {
    const user = this.user ? ["-u", this.user] : []
    return this.exec("docker", ["exec", ...(input !== undefined ? ["-i"] : []), ...user, "-w", "/repo", this.id, "sh", "-c", script], { timeoutMs, input })
  }

  async write(path: string, content: string) {
    const q = shQuote(path)
    const r = await this.sh(`mkdir -p "$(dirname ${q})" && cat > ${q}`, 60_000, content)
    if (r.code !== 0) throw new Error(`write ${path}: ${r.stderr.trim()}`)
  }

  async read(path: string): Promise<string | undefined> {
    const r = await this.sh(`cat ${shQuote(path)}`, 60_000)
    return r.code === 0 ? r.stdout : undefined
  }

  async applyPatch(patch: string) {
    await this.write(".factory/patch.diff", patch.endsWith("\n") ? patch : patch + "\n")
    return this.sh("git apply --whitespace=nowarn .factory/patch.diff", 120_000)
  }

  async stop() {
    // a timeout kills the docker exec, not the processes inside: removing the container stops them all
    await this.exec("docker", ["rm", "-f", this.id])
  }
}
