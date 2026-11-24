import json
import shutil
import zipfile

import pytest

from factory.cli import main
from factory.errors import RepoLoadError
from factory.models import RepoSpec
from factory.repo.inspector import inspect_repo, smoke_check
from factory.repo.loader import export_clean, load_repo, repo_name
from helpers import NODE_REPO, PY_REPO, git


def _zip(src, out, prefix="toyshop/", include_git=False):
    with zipfile.ZipFile(out, "w") as zf:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if p.is_file() and (include_git or ".git" not in rel.parts) and "__pycache__" not in rel.parts:
                zf.write(p, prefix + str(rel))
    return out


# --------------------------------------------------------------------------- loader


def test_load_local_path(py_repo, tmp_path):
    head = git(py_repo, "rev-parse", "HEAD")
    spec = load_repo(str(py_repo), tmp_path / "wd")
    assert spec.commit_sha == head and len(spec.commit_sha) == 40
    assert spec.language == "python" and not spec.synthetic_history
    assert spec.local_path != str(py_repo)
    assert (tmp_path / "wd" / "repos" / "toyshop" / "toyshop" / "cart.py").exists()


def test_load_specific_commit(py_repo, tmp_path):
    first = git(py_repo, "rev-parse", "HEAD")
    (py_repo / "NEW.txt").write_text("x")
    git(py_repo, "add", "-A")
    git(py_repo, "commit", "-qm", "second")
    spec = load_repo(str(py_repo), tmp_path / "wd", commit=first[:10])
    assert spec.commit_sha == first
    assert not (tmp_path / "wd" / "repos" / "toyshop" / "NEW.txt").exists()


def test_load_git_url(py_repo, tmp_path):
    spec = load_repo(f"file://{py_repo}", tmp_path / "wd")
    assert spec.commit_sha == git(py_repo, "rev-parse", "HEAD")
    assert spec.name == "toyshop"


def test_dirty_tree_refused_and_original_untouched(py_repo, tmp_path):
    (py_repo / "toyshop" / "cart.py").write_text("changed\n")
    with pytest.raises(RepoLoadError, match="uncommitted"):
        load_repo(str(py_repo), tmp_path / "wd")
    assert (py_repo / "toyshop" / "cart.py").read_text() == "changed\n"


def test_zip_without_history_is_deterministic(tmp_path):
    z = _zip(PY_REPO, tmp_path / "toyshop.zip")
    a = load_repo(str(z), tmp_path / "wd1")
    b = load_repo(str(z), tmp_path / "wd2")
    assert a.synthetic_history and a.commit_sha == b.commit_sha
    assert (tmp_path / "wd1" / "repos" / "toyshop" / "toyshop" / "cart.py").exists()


def test_zip_with_history(py_repo, tmp_path):
    z = _zip(py_repo, tmp_path / "toyshop.zip", include_git=True)
    spec = load_repo(str(z), tmp_path / "wd")
    assert not spec.synthetic_history
    assert spec.commit_sha == git(py_repo, "rev-parse", "HEAD")


def test_plain_folder_gets_synthetic_history(tmp_path):
    src = tmp_path / "plain"
    shutil.copytree(NODE_REPO, src)
    spec = load_repo(str(src), tmp_path / "wd")
    assert spec.synthetic_history and spec.language == "node"


def test_missing_source(tmp_path):
    with pytest.raises(RepoLoadError):
        load_repo(str(tmp_path / "nope"), tmp_path / "wd")


def test_export_clean(py_repo, tmp_path):
    spec = load_repo(str(py_repo), tmp_path / "wd")
    out = export_clean(spec, tmp_path / "clean")
    assert (out / "toyshop" / "money.py").exists() and not (out / ".git").exists()


def test_repo_name():
    assert repo_name("https://github.com/acme/Big-Repo.git") == "big-repo"
    assert repo_name("/x/y/thing.zip") == "thing"


# --------------------------------------------------------------------------- inspector


def _spec(path, lang=None):
    return RepoSpec(name=path.name, source=str(path), commit_sha="0" * 40, local_path=str(path), language=lang)


def test_inspect_python():
    r = inspect_repo(_spec(PY_REPO))
    assert r.status == "ok"
    assert r.env.base_image == "python:3.11-slim"
    assert r.env.setup_commands == ["pip install -q -e '.[test]'", "pip install -q pytest"]
    assert r.env.framework == "pytest" and "--junitxml={report}" in r.env.test_command


def test_inspect_node():
    r = inspect_repo(_spec(NODE_REPO))
    assert r.status == "ok"
    assert r.env.setup_commands == ["npm ci --no-audit --no-fund"]
    assert r.env.framework == "node" and "node-junit.mjs" in r.env.test_command


def test_inspect_jest(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"jest": "29"}, "scripts": {"test": "jest"}}))
    r = inspect_repo(_spec(tmp_path))
    assert r.env.framework == "jest" and r.env.report_format == "jest-json"


def test_inspect_unknown(tmp_path):
    (tmp_path / "main.c").write_text("int main(){}")
    r = inspect_repo(_spec(tmp_path))
    assert r.status == "needs_manual_config" and r.reasons


def test_inspect_unknown_node_runner(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "mocha"}}))
    r = inspect_repo(_spec(tmp_path))
    assert r.status == "needs_manual_config"
    assert "jest, vitest, node:test" in r.reasons[0] and "package.json" in r.reasons[0]
    assert "[ecosystems.node]" in r.reasons[0]  # says how to add the missing setup


def test_overrides(tmp_path):
    from factory.config import EnvOverrides

    (tmp_path / "main.c").write_text("int main(){}")
    ov = EnvOverrides(base_image="gcc:13", test_command="make test", framework="pytest", report_format="junit")
    assert inspect_repo(_spec(tmp_path), ov).status == "ok"
    r = inspect_repo(_spec(PY_REPO), EnvOverrides(base_image="python:3.12-slim"))
    assert r.env.base_image == "python:3.12-slim" and r.env.framework == "pytest"


@pytest.mark.docker
@pytest.mark.parametrize("src,passed", [(PY_REPO, 28), (NODE_REPO, 22)])
def test_smoke_check_docker(src, passed, tmp_path, docker_factory):
    from helpers import make_git_repo

    spec = load_repo(str(make_git_repo(src, tmp_path / src.name)), tmp_path / "wd")
    result = smoke_check(spec, inspect_repo(spec), docker_factory)
    assert result.status == "ok", result.reasons
    assert result.baseline.counts["passed"] == passed and result.baseline.counts["failed"] == 0


@pytest.mark.docker
def test_cli_load_and_inspect(py_repo, tmp_path):
    wd = tmp_path / "wd"
    assert main(["load", "--repo", str(py_repo), "--workdir", str(wd)]) == 0
    assert main(["inspect", "--workdir", str(wd)]) == 0
    assert json.loads((wd / "inspect.json").read_text())["baseline"]["counts"]["passed"] == 28
    assert main(["inspect", "--workdir", str(wd)]) == 0  # resumable: skipped


def test_cli_inspect_without_load(tmp_path):
    assert main(["inspect", "--workdir", str(tmp_path)]) == 2
