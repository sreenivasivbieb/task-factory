// node --test tests/ts   (Node 22.18+ runs TypeScript directly)
import assert from "node:assert/strict"
import { test } from "node:test"
import { pythonEnv } from "../../swe/env.ts"
import { DEFAULT_PYTHON_FILES, isTestFile, pytestCommand, pythonFiles } from "../../swe/testfiles.ts"

test("python_files from each pytest config file", () => {
  assert.deepEqual(pythonFiles({}), DEFAULT_PYTHON_FILES)
  assert.deepEqual(pythonFiles({ "pyproject.toml": "[project]\nname='x'\n[tool.pytest.ini_options]\naddopts = '-q'\npython_files = [\"check_*.py\", \"*_tests.py\"]\n[tool.ruff]\nx=1\n" }),
    ["check_*.py", "*_tests.py"])
  assert.deepEqual(pythonFiles({ "pyproject.toml": "[tool.pytest.ini_options]\npython_files = \"check_*.py test_*.py\"\n" }), ["check_*.py", "test_*.py"])
  assert.deepEqual(pythonFiles({ "pytest.ini": "[pytest]\npython_files = check_*.py\n" }), ["check_*.py"])
  assert.deepEqual(pythonFiles({ "setup.cfg": "[metadata]\nname=x\n[tool:pytest]\npython_files =\n    test_*.py\n    *_spec.py\n[flake8]\nx=1\n" }),
    ["test_*.py", "*_spec.py"])
  assert.deepEqual(pythonFiles({ "tox.ini": "[tox]\nenvlist=py\n[pytest]\npython_files = it_*.py\n" }), ["it_*.py"])
  // pytest.ini is pytest's config even without python_files: a later file is not consulted
  assert.deepEqual(pythonFiles({ "pytest.ini": "[pytest]\naddopts=-q\n", "setup.cfg": "[tool:pytest]\npython_files = x_*.py\n" }), DEFAULT_PYTHON_FILES)
})

test("odd or unsafe patterns fall back to the defaults (they go into a shell command)", () => {
  assert.deepEqual(pythonFiles({ "pytest.ini": "[pytest]\npython_files = $(rm -rf /)*.py\n" }), DEFAULT_PYTHON_FILES)
  assert.deepEqual(pythonFiles({ "pytest.ini": "[pytest]\npython_files = check_*\n" }), DEFAULT_PYTHON_FILES)
})

test("test files by folder, conftest and the patterns", () => {
  const custom = ["check_*.py"]
  assert.ok(isTestFile("pkg/check_money.py", custom) && !isTestFile("pkg/check_money.py"))
  assert.ok(isTestFile("tests/data/x.json", custom) && isTestFile("conftest.py", custom))
  assert.ok(!isTestFile("pkg/test_money.py", custom)) // the project replaced the defaults
  assert.ok(!isTestFile("pkg/checker.py", custom) && !isTestFile("pkg/sub/check_x.txt", custom))
})

test("the test command passes only files matching the project's patterns", () => {
  assert.match(pytestCommand(["check_*.py", "*_spec.py"]), /case "\$\{f##\*\/\}" in check_\*\.py\|\*_spec\.py\)/)
  assert.equal(pythonEnv({}, "2025-01-01T00:00:00Z").test_command, pytestCommand())
  assert.equal(pythonEnv({}, "2025-01-01T00:00:00Z", undefined, ["check_*.py"]).test_command, pytestCommand(["check_*.py"]))
})
