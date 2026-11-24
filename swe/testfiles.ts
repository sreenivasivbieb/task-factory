// Which files are tests: the shared test folders, conftest.py, and pytest's `python_files`
// patterns, read from the project's own pytest configuration (default: test_*.py, *_test.py).
// A project that names its tests differently (check_*.py, *_tests.py) keeps its tests: they are
// split into the hidden tests and passed to pytest, instead of being mistaken for code.

export const DEFAULT_PYTHON_FILES = ["test_*.py", "*_test.py"]
export const PYTEST_CONFIG_FILES = ["pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"]

const TEST_DIRS = /(^|\/)(tests?|__tests__|specs?|testing)\/|(^|\/)conftest\.py$/
// Patterns end up in a shell `case` and in regular expressions: plain glob characters only.
const SAFE = /^[A-Za-z0-9_.*?[\]-]+$/

const clean = (patterns: string[]) => {
  const ok = patterns.map((p) => p.trim()).filter(Boolean)
  return ok.length && ok.every((p) => SAFE.test(p) && p.endsWith(".py")) ? ok : undefined
}

function iniSection(text: string, name: string): string | undefined {
  const m = new RegExp(`^\\[${name.replace(/[.:]/g, "\\$&")}\\][^\\n]*\\n([\\s\\S]*?)(?=^\\[|(?![\\s\\S]))`, "m").exec(text)
  return m?.[1]
}

/** pytest's `python_files` for this project, or the defaults when it does not set one (or sets odd ones). */
export function pythonFiles(files: Record<string, string>): string[] {
  const toml = iniSection(files["pyproject.toml"] ?? "", "tool.pytest.ini_options")
  if (toml !== undefined) {
    const m = /^\s*python_files\s*=\s*(\[[^\]]*\]|"[^"]*"|'[^']*')/m.exec(toml)
    if (m) {
      const value = m[1].startsWith("[") ? [...m[1].matchAll(/["']([^"']*)["']/g)].map((x) => x[1]) : m[1].slice(1, -1).split(/\s+/)
      return clean(value) ?? DEFAULT_PYTHON_FILES
    }
  }
  for (const [file, section] of [["pytest.ini", "pytest"], ["tox.ini", "pytest"], ["setup.cfg", "tool:pytest"]]) {
    const body = iniSection(files[file] ?? "", section)
    if (body === undefined) continue
    // ini values may continue on indented lines
    const m = /^python_files\s*[=:]\s*(.*(?:\n[ \t]+.*)*)/m.exec(body)
    if (m) return clean(m[1].split(/\s+/)) ?? DEFAULT_PYTHON_FILES
    if (file === "pytest.ini") break // pytest.ini wins even without python_files (pytest's own rule)
  }
  return DEFAULT_PYTHON_FILES
}

const globRe = (glob: string) =>
  new RegExp("^" + glob.replace(/[.+^${}()|\\]/g, "\\$&").replace(/\*/g, "[^/]*").replace(/\?/g, "[^/]") + "$")

/** A test file under these patterns (by its name), a test folder, or conftest.py. */
export function isTestFile(path: string, patterns: string[] = DEFAULT_PYTHON_FILES): boolean {
  if (TEST_DIRS.test(path)) return true
  const name = path.split("/").pop() ?? path
  return patterns.some((p) => globRe(p).test(name))
}

/** The pytest command: only files matching the patterns reach pytest (a data file or helper as an
 *  argument aborts the whole run); the harness passes every hidden file, so the filter lives here. */
export function pytestCommand(patterns: string[] = DEFAULT_PYTHON_FILES): string {
  return "python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 --junitxml={report} " +
    `$(for f in {select}; do case "\${f##*/}" in ${patterns.join("|")}) printf '%s\\n' "$f";; esac; done)`
}
