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
