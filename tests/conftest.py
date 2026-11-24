import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers import NODE_REPO, PY_REPO, docker_available, make_git_repo  # noqa: E402


@pytest.fixture
def py_repo(tmp_path):
    return make_git_repo(PY_REPO, tmp_path / "toyshop")


@pytest.fixture
def node_repo(tmp_path):
    return make_git_repo(NODE_REPO, tmp_path / "textkit")


def pytest_collection_modifyitems(config, items):
    if docker_available():
        return
    skip = pytest.mark.skip(reason="Docker is not available on this machine (docker info failed)")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


# --------------------------------------------------------------------------- shared session fixtures

from factory.config import SandboxConfig  # noqa: E402
from factory.models import EnvConfig, RepoSpec  # noqa: E402
from helpers import git  # noqa: E402

PY_TEST_CMD = ("python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 "
               "--junitxml={report} {select}")


def _spec_for(path, name):
    return RepoSpec(name=name, source=str(path), commit_sha=git(path, "rev-parse", "HEAD"),
                    local_path=str(path), language=None)


@pytest.fixture(scope="session")
def py_spec(tmp_path_factory):
    repo = make_git_repo(PY_REPO, tmp_path_factory.mktemp("spec") / "toyshop")
    return _spec_for(repo, "toyshop")


@pytest.fixture(scope="session")
def node_spec(tmp_path_factory):
    repo = make_git_repo(NODE_REPO, tmp_path_factory.mktemp("spec") / "textkit")
    return _spec_for(repo, "textkit")


@pytest.fixture(scope="session")
def py_env_docker():
    return EnvConfig(base_image="python:3.11-slim", setup_commands=["pip install -q pytest"],
                     test_command=PY_TEST_CMD, framework="pytest", report_format="junit",
                     test_timeout_s=120)


@pytest.fixture(scope="session")
def py_env_local():
    return EnvConfig(base_image="host", setup_commands=[], test_command=PY_TEST_CMD,
                     framework="pytest", report_format="junit", test_timeout_s=120)


@pytest.fixture(scope="session")
def node_env_docker():
    """What the inspector derives from ecosystems.toml for the toy Node repo (node:test variant)."""
    from factory.ecosystems import registry
    from factory.repo.inspector import env_for

    env, reasons = env_for(registry().ecosystems["node"], NODE_REPO)
    assert env is not None, reasons
    return env.model_copy(update={"test_timeout_s": 120})


@pytest.fixture(scope="session")
def docker_factory():
    from factory.sandbox.docker_backend import DockerSandboxFactory

    return DockerSandboxFactory(SandboxConfig())


@pytest.fixture(scope="session")
def local_factory(tmp_path_factory):
    from factory.sandbox.local_backend import LocalSandboxFactory

    return LocalSandboxFactory(tmp_path_factory.mktemp("cache"))
