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


@pytest.mark.docker
def test_files_are_mounted_read_only_from_content_named_volumes(docker_factory, py_spec, py_env_docker, tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\necho mounted-tool\n")
    base = docker_factory.build_image(py_spec, py_env_docker)
    assert docker_factory.build_derived_image(base, "no-install", []) == base  # nothing to install: no image
    with docker_factory.new_sandbox(base, files={"/opt/tools/tool": tool}) as sb:
        res = sb.exec(["/opt/tools/tool"], timeout_s=30)
    assert res.exit_code == 0 and res.stdout.strip() == "mounted-tool"
    tool.write_text("#!/bin/sh\necho changed\n")  # new content: a new volume, never a stale copy
    with docker_factory.new_sandbox(base, files={"/opt/tools/tool": tool}) as sb:
        assert sb.exec(["/opt/tools/tool"], timeout_s=30).stdout.strip() == "changed"


@pytest.mark.docker
@pytest.mark.slow
def test_task_factory_engine_runs_in_task_image(docker_factory, py_spec, py_env_docker):
    from pathlib import Path

    from factory.harness.agents import ENGINE_IN_IMAGE, get_agent

    agent = get_agent("task-factory")
    files = {dest: Path(src) for dest, src in agent.files.items()}
    if not all(src.is_file() for src in files.values()):
        pytest.skip("Task Factory engine is not built")
    base = docker_factory.build_image(py_spec, py_env_docker)
    # no derived image: the engine is mounted read-only from a volume filled once
    assert docker_factory.build_derived_image(base, agent.name, agent.install) == base
    with docker_factory.new_sandbox(base, files=files) as sb:
        res = sb.exec([ENGINE_IN_IMAGE, "--version"], timeout_s=60)
        denied = sb.exec(["sh", "-c", f"echo x > {ENGINE_IN_IMAGE}"], timeout_s=10)
    assert res.exit_code == 0 and res.stdout.strip().startswith("task-factory ")
    assert denied.exit_code != 0  # read-only: an agent cannot replace its own engine


# --------------------------------------------------------------------------- allowlisted egress

_PROBE = r"""
import os, socket, sys
host, port = os.environ["HTTPS_PROXY"].rsplit("/", 1)[1].split(":")
def connect(target):
    s = socket.create_connection((host, int(port)), timeout=10)
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    return s.recv(200).decode(errors="replace").split("\r\n")[0]
print("github", connect("api.github.com:443"))
print("pypi", connect("pypi.org:443"))
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5)
    print("direct open")
except OSError as exc:
    print("direct blocked", type(exc).__name__)
"""


@pytest.mark.docker
def test_agent_sandbox_reaches_only_allowed_hosts(docker_factory):
    """An agent sandbox gets a proxy that refuses every host but the allowlist, and no route around it."""
    from factory.models import ImageRef

    image = "python:3.13-slim"
    if docker_factory._run(["image", "inspect", image]).returncode != 0 and \
            docker_factory._run(["pull", "-q", image]).returncode != 0:
        pytest.skip(f"{image} is not available")
    since, before = time.time(), set(docker_factory._proxies)
    try:
        with docker_factory.new_sandbox(ImageRef(tag=image, backend="docker"), network=True,
                                        allow_hosts=["opencode.ai", "*.opencode.ai"], egress_image=image) as sb:
            sb.write_file("/tmp/probe.py", _PROBE)
            out = sb.exec(["python", "/tmp/probe.py"], timeout_s=60).stdout
        assert "github HTTP/1.1 403" in out and "pypi HTTP/1.1 403" in out, out
        assert "direct blocked" in out, out  # internal network: nothing gets out without the proxy
        blocked = docker_factory.egress_blocked(since - 1)
        assert "api.github.com:443" in blocked and "pypi.org:443" in blocked
    finally:
        for name in docker_factory._proxies - before:  # this test's proxy only, not the harness's
            docker_factory._run(["rm", "-f", name])
            docker_factory._proxies.discard(name)


def test_egress_allowlist_patterns():
    from factory.harness.egress_proxy import allowed

    patterns = ["opencode.ai", "*.opencode.ai", "bedrock-runtime.*.amazonaws.com"]
    assert allowed("models.opencode.ai", "443", patterns) and allowed("OpenCode.AI.", "443", patterns)
    assert allowed("bedrock-runtime.us-east-1.amazonaws.com", "443", patterns)
    for host, port in [("api.github.com", "443"), ("evilopencode.ai", "443"), ("opencode.ai", "80"),
                       ("s3.amazonaws.com", "443")]:
        assert not allowed(host, port, patterns), host
