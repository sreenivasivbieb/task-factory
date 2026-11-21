import time

import pytest

from factory.config import SandboxConfig
from factory.errors import EnvironmentProblem
from factory.sandbox.base import image_tag, render_dockerfile

GOOD_PATCH = """\
--- a/toyshop/__init__.py
+++ b/toyshop/__init__.py
@@ -1,3 +1,3 @@
 \"\"\"toyshop: products, carts, pricing and receipts.\"\"\"
 
-__version__ = "0.3.0"
+__version__ = "0.4.0"
"""

BAD_PATCH = GOOD_PATCH.replace('-__version__ = "0.3.0"', '-__version__ = "9.9.9"')


@pytest.fixture(params=["local", pytest.param("docker", marks=pytest.mark.docker)])
def env_and_factory(request, py_spec, py_env_local, py_env_docker):
    if request.param == "local":
        return py_spec, py_env_local, request.getfixturevalue("local_factory")
    return py_spec, py_env_docker, request.getfixturevalue("docker_factory")


@pytest.fixture
def image(env_and_factory):
    spec, env, factory = env_and_factory
    return factory.build_image(spec, env)


def test_dockerfile_contents(py_spec, py_env_docker):
    text = render_dockerfile(py_env_docker)
    assert text.startswith("FROM python:3.11-slim\n")
    assert "ADD repo.tar /repo/" in text and "RUN pip install -q pytest" in text
    assert image_tag(py_spec, py_env_docker).startswith("factory-toyshop:")


def test_image_is_cached(env_and_factory):
    spec, env, factory = env_and_factory
    first = factory.build_image(spec, env)
    start = time.monotonic()
    second = factory.build_image(spec, env)
    assert first == second
    assert time.monotonic() - start < 5


def test_exec_exit_code_and_output(env_and_factory, image):
    _, _, factory = env_and_factory
    with factory.new_sandbox(image) as sb:
        r = sb.exec(["sh", "-c", "echo out; echo err >&2; exit 3"], timeout_s=30)
        assert (r.exit_code, r.stdout, r.stderr) == (3, "out\n", "err\n")
        assert sb.exec(["cat", "toyshop/__init__.py"], timeout_s=30).ok
        assert b"__version__" in sb.read_file("toyshop/__init__.py")


def test_env_is_fixed(env_and_factory, image):
    _, _, factory = env_and_factory
    with factory.new_sandbox(image) as sb:
        r = sb.exec(["sh", "-c", "echo $TZ $PYTHONHASHSEED $EXTRA"], timeout_s=30, env={"EXTRA": "x"})
        assert r.stdout.strip() == "UTC 0 x"


def test_timeout_kills_process(env_and_factory, image):
    _, _, factory = env_and_factory
    with factory.new_sandbox(image) as sb:
        start = time.monotonic()
        r = sb.exec(["sh", "-c", "sleep 30 & sleep 30"], timeout_s=2)
        assert r.timed_out and not r.ok
        assert time.monotonic() - start < 15
        # the sandbox is still usable and nothing is left running
        ps = sb.exec(["sh", "-c", "ls /proc | grep -c '^[0-9]' || true"], timeout_s=10)
        assert ps.ok


def test_output_truncated(env_and_factory, image):
    _, _, factory = env_and_factory
    with factory.new_sandbox(image) as sb:
        sb.limit = 1000
        r = sb.exec(["python", "-c", "print('x' * 5000)"], timeout_s=30)
        assert "truncated by factory" in r.stdout
        assert len(r.stdout) < 1200


def test_good_and_bad_patch(env_and_factory, image):
    _, _, factory = env_and_factory
    with factory.new_sandbox(image) as sb:
        bad = sb.apply_patch(BAD_PATCH)
        assert bad.exit_code != 0 and "patch" in (bad.stderr + bad.stdout).lower()
        good = sb.apply_patch(GOOD_PATCH)
        assert good.ok, good.stderr
        assert b'"0.4.0"' in sb.read_file("toyshop/__init__.py")
        status = sb.exec(["git", "status", "--porcelain"], timeout_s=30)
        assert status.stdout.strip() == "M toyshop/__init__.py"  # .factory/ is ignored


def test_sandboxes_share_nothing(env_and_factory, image):
    _, _, factory = env_and_factory
    with factory.new_sandbox(image) as a, factory.new_sandbox(image) as b:
        a.write_file("notes/new.txt", "hello")
        a.exec(["sh", "-c", "echo x > /tmp/state 2>/dev/null || true"], timeout_s=10)
        with pytest.raises(FileNotFoundError):
            b.read_file("notes/new.txt")
        assert a.read_file("notes/new.txt") == b"hello"


@pytest.mark.docker
def test_network_blocked(docker_factory, py_spec, py_env_docker):
    image = docker_factory.build_image(py_spec, py_env_docker)
    with docker_factory.new_sandbox(image) as sb:
        code = "import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)"
        r = sb.exec(["python", "-c", code], timeout_s=30)
        assert r.exit_code != 0


@pytest.mark.docker
def test_runs_as_non_root(docker_factory, py_spec, py_env_docker):
    image = docker_factory.build_image(py_spec, py_env_docker)
    with docker_factory.new_sandbox(image) as sb:
        assert sb.exec(["id", "-u"], timeout_s=10).stdout.strip() != "0"


@pytest.mark.docker
def test_container_removed_after_exception(docker_factory, py_spec, py_env_docker):
    import subprocess

    image = docker_factory.build_image(py_spec, py_env_docker)
    with pytest.raises(RuntimeError):
        with docker_factory.new_sandbox(image) as sb:
            cid = sb.cid
            raise RuntimeError("boom")
    r = subprocess.run(["docker", "inspect", cid], capture_output=True)
    assert r.returncode != 0


def test_missing_docker_binary():
    from factory.sandbox.docker_backend import DockerSandboxFactory

    with pytest.raises(EnvironmentProblem):
        DockerSandboxFactory(SandboxConfig(docker_binary="definitely-not-docker"))
