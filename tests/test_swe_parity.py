"""The TypeScript SWE miner writes files the Python side reads back: they must match byte for byte.

Dockerfile and image tag (so the harness reuses the miner's image), env.json, task.toml
(a valid TaskPackage that Python would write the same way) and JUnit test IDs.
"""
import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.models import EnvConfig, TaskPackage
from factory.packaging import toml_writer
from factory.runner.parsers import parse_junit
from factory.sandbox.base import image_tag, render_dockerfile

from test_ts import _node

ROOT = Path(__file__).resolve().parent.parent
REPORTS = [ROOT / "tests/fixtures/reports/pytest_junit.xml", ROOT / "tests/fixtures/reports/surefire.xml"]


@pytest.fixture(scope="module")
def ts():
    node = _node()
    if node is None:
        pytest.skip("needs Node 22.18+ (runs TypeScript directly)")
    out = subprocess.run([node, str(ROOT / "tests/ts/parity_dump.ts"), *map(str, REPORTS)],
                         capture_output=True, text=True, timeout=120, check=True).stdout
    return json.loads(out)


def test_env_json_and_dockerfile_match(ts):
    env = EnvConfig.model_validate(ts["env"])
    assert env.to_json() == ts["env_json"]
    assert render_dockerfile(env) == ts["dockerfile"]


def test_image_tag_matches_so_the_harness_reuses_the_image(ts):
    env = EnvConfig.model_validate(ts["env"])
    meta = tomllib.loads(ts["toml"])
    spec = SimpleNamespace(name=meta["repo_name"], commit_sha=meta["commit_sha"])
    assert image_tag(spec, env) == ts["tag"]


def test_task_toml_is_a_task_package_python_writes_the_same_way(ts):
    meta = TaskPackage.model_validate(tomllib.loads(ts["toml"]))
    assert meta.kind == "bugfix" and meta.fail_to_pass == ["tests/test_a.py::test_x"]
    assert toml_writer.dumps(meta.model_dump(mode="json")) == ts["toml"]


def test_junit_ids_match(ts):
    for path, ts_results in zip(REPORTS, ts["junit"]):
        py = [{"test_id": r.test_id, "status": r.status} for r in parse_junit(path.read_text(encoding="utf-8"))]
        assert py == ts_results, path.name
