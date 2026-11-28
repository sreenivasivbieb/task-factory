"""Docker sandbox backend: real isolation. Works with Podman via ``docker_binary``."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ..config import SandboxConfig
from ..errors import BuildError, EnvironmentProblem, SandboxRuntimeError, SandboxStartError
from ..models import EnvConfig, ExecResult, ImageRef, RepoSpec
from .base import FIXED_ENV, Sandbox, SandboxFactory, git_archive, image_tag, render_dockerfile, truncate_output

log = logging.getLogger("factory.sandbox.docker")

EGRESS_NET = "factory-egress"  # internal Docker network for agents (see _egress_proxy)
EGRESS_PORT = 3128



def _file_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()

class DockerSandboxFactory(SandboxFactory):
    backend_name = "docker"

    def __init__(self, cfg: SandboxConfig | None = None) -> None:
        self.cfg = cfg or SandboxConfig()
        self.bin = self.cfg.docker_binary
        self._users: dict[str, str | None] = {}
        self._proxies: set[str] = set()
        self._volumes: set[str] = set()
        self._lock = threading.Lock()  # G6 mutants and harness attempts start sandboxes in parallel
        self._check_available()

    def _check_available(self) -> None:
        if shutil.which(self.bin) is None:
            raise EnvironmentProblem(f"'{self.bin}' not found on PATH; install Docker or set sandbox.docker_binary")
        r = subprocess.run([self.bin, "info"], capture_output=True, text=True)
        if r.returncode != 0:
            raise EnvironmentProblem(f"'{self.bin} info' failed; is the daemon running? {r.stderr.strip()[:300]}")

    def _run(self, args: list[str], **kw) -> subprocess.CompletedProcess:
        return subprocess.run([self.bin, *args], capture_output=True, text=True, **kw)

    def image_exists(self, tag: str) -> bool:
        return self._run(["image", "inspect", tag]).returncode == 0

    def build_image(self, spec: RepoSpec, env: EnvConfig) -> ImageRef:
        tag = image_tag(spec, env)
        if self.image_exists(tag):
            log.info("reusing image %s", tag)
            return ImageRef(tag=tag, backend="docker")
        with tempfile.TemporaryDirectory(prefix="factory-build-") as ctx:
            ctx_path = Path(ctx)
            git_archive(spec, ctx_path / "repo.tar")
            (ctx_path / "Dockerfile").write_text(render_dockerfile(env), encoding="utf-8")
            start = time.monotonic()
            try:
                r = self._run(["build", "-t", tag, str(ctx_path)], timeout=self.cfg.build_timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise BuildError(f"image build timed out after {self.cfg.build_timeout_s}s",
                                 str(exc.output or "")) from exc
            log.info("built image %s", tag, extra={"data": {"duration_s": round(time.monotonic() - start, 2)}})
        if r.returncode != 0:
            raise BuildError(f"image build failed for {spec.name}@{spec.short_sha}", r.stdout + r.stderr)
        return ImageRef(tag=tag, backend="docker")

    def pin_image(self, image: str) -> str:
        """Resolve a tag to its registry digest so a later rebuild uses exactly the same base image."""
        import json

        if "@sha256:" in image or not self.cfg.pin_base_image:
            return image
        inspect = ["image", "inspect", "--format", "{{json .RepoDigests}}", image]
        r = self._run(inspect)
        if r.returncode != 0:
            pulled = self._run(["pull", "-q", image], timeout=self.cfg.build_timeout_s)
            if pulled.returncode != 0:
                log.warning("could not pull %s to pin its digest; keeping the tag", image)
                return image
            r = self._run(inspect)
        try:
            digests = json.loads(r.stdout or "[]") or []
        except json.JSONDecodeError:
            digests = []
        last = image.rsplit("/", 1)[-1]
        repo = image[: len(image) - len(last)] + last.split(":", 1)[0]
        match = [d for d in digests if d.split("@")[0] in (repo, f"docker.io/library/{repo}", f"docker.io/{repo}")
                 or d.split("@")[0].endswith("/" + repo)]
        chosen = (match or digests or [None])[0]
        if not chosen or "@" not in chosen:
            log.warning("%s has no registry digest (a local image?); keeping the tag", image)
            return image
        pinned = f"{image}@{chosen.split('@', 1)[1]}"
        log.info("pinned base image %s", pinned)
        return pinned

    def build_derived_image(self, base: ImageRef, name: str, commands: list[str]) -> ImageRef:
        if not commands:
            return base
        import hashlib
        import json

        digest = hashlib.sha256(json.dumps([base.tag, commands]).encode()).hexdigest()[:16]
        tag = f"{base.tag.split(':')[0]}-{name}:{digest}"
        if self.image_exists(tag):
            return ImageRef(tag=tag, backend="docker")
        lines = [f"FROM {base.tag}", "USER root", *(f"RUN {c}" for c in commands),
                 "RUN (id factory >/dev/null 2>&1 && chown -R factory /repo) || true"]
        with tempfile.TemporaryDirectory(prefix="factory-derived-") as ctx:
            (Path(ctx) / "Dockerfile").write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                r = self._run(["build", "-t", tag, ctx], timeout=self.cfg.build_timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise BuildError(f"derived image build timed out ({name})", str(exc.output or "")) from exc
        if r.returncode != 0:
            raise BuildError(f"derived image build failed ({name})", r.stdout + r.stderr)
        log.info("built derived image %s", tag)
        return ImageRef(tag=tag, backend="docker")

    def _user_for(self, tag: str) -> str | None:
        """Run as the non-root 'factory' user when the image has one."""
        with self._lock:  # one probe per image, even when sandboxes start in parallel
            if tag not in self._users:
                r = self._run(["run", "--rm", "--network", "none", "--entrypoint", "id", tag, "-u", "factory"])
                self._users[tag] = "factory" if r.returncode == 0 else None
                if r.returncode != 0:
                    log.warning("image %s has no non-root user; running as root", tag)
            return self._users[tag]

    # ------------------------------------------------------------------ allowlisted egress
    # The agent's container joins an internal network (no route out). The only other member is a
    # small proxy container, also on the default bridge, that tunnels HTTPS to allowed hosts only.

    def _egress_proxy(self, hosts: list[str], image: str) -> str:
        """Start (or reuse) the proxy for this allowlist; returns its name, which resolves on EGRESS_NET."""
        import hashlib

        allow = ",".join(sorted({h.strip().lower() for h in hosts if h.strip()}))
        name = f"factory-egress-{hashlib.sha256(allow.encode()).hexdigest()[:12]}"
        if self._run(["network", "inspect", EGRESS_NET]).returncode != 0:
            r = self._run(["network", "create", "--internal", "--label", "factory-egress=1", EGRESS_NET])
            if r.returncode != 0 and "already exists" not in r.stderr:
                raise SandboxStartError(f"could not create the network {EGRESS_NET}: {r.stderr.strip()}")
        state = self._run(["inspect", "-f", "{{.State.Running}}", name])
        if state.returncode == 0 and state.stdout.strip() == "true":
            return name
        self._run(["rm", "-f", name])
        script = (Path(__file__).resolve().parent.parent / "harness" / "egress_proxy.py").read_text(encoding="utf-8")
        r = self._run(["run", "-d", "--name", name, "--label", "factory-egress=1", "--restart", "unless-stopped",
                       "--user", "65534:65534", "--read-only", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges", "--memory", "128m", "--pids-limit", "64",
                       "--entrypoint", "python", image, "-c", script, str(EGRESS_PORT), allow])
        if r.returncode != 0:
            raise SandboxStartError(f"could not start the egress proxy from {image}: {r.stderr.strip()}")
        r = self._run(["network", "connect", "--alias", name, EGRESS_NET, name])
        if r.returncode != 0 and "already exists" not in r.stderr:
            raise SandboxStartError(f"could not attach the egress proxy to {EGRESS_NET}: {r.stderr.strip()}")
        self._proxies.add(name)
        log.info("egress proxy %s allows %s", name, allow)
        return name

    def egress_blocked(self, since: float, sandbox: "DockerSandbox | None" = None) -> list[str]:
        ip = sandbox.egress_ip() if sandbox is not None else None
        out: list[str] = []
        for name in sorted(self._proxies):
            r = self._run(["logs", "--since", str(int(since)), name])
            for ln in (r.stdout + r.stderr).splitlines():
                if not ln.startswith("DENY "):
                    continue
                what, _, peer = ln[5:].partition(" from ")
                if ip is None or peer.strip() == ip:
                    out.append(what.strip())
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------------ read-only file mounts
    # An agent's files (the 330 MB engine) used to be copied into a derived image per task image
    # (~780 MB each). Now each file lives once in a Docker volume, mounted read-only.

    def _file_volume(self, src: Path, filename: str, image: str) -> str:
        """A volume holding ``src`` as ``filename`` (named by content), filled once through ``image``'s shell."""
        name = f"factory-file-{_file_digest(src)[:16]}-{re.sub(r'[^a-z0-9]+', '-', filename.lower()).strip('-')}"
        if name in self._volumes:
            return name
        # create it labelled first: `docker run -v` would create it without the label prune looks for
        if self._run(["volume", "inspect", name]).returncode != 0:
            self._run(["volume", "create", "--label", "factory-file=1", name])
        check = self._run(["run", "--rm", "--network", "none", "-v", f"{name}:/v", "--entrypoint", "sh", image,
                           "-c", "test -f /v/.complete"])
        if check.returncode != 0:
            with src.open("rb") as fh:
                r = subprocess.run([self.bin, "run", "--rm", "-i", "--network", "none", "-u", "0", "-v", f"{name}:/v",
                                    "--entrypoint", "sh", image, "-c",
                                    f'cat > /v/{shlex.quote(filename)} && chmod 755 /v/{shlex.quote(filename)} '
                                    '&& touch /v/.complete'],
                                   stdin=fh, capture_output=True, check=False)
            if r.returncode != 0:
                raise SandboxStartError(f"could not fill volume {name} with {src}: {r.stderr.decode()[-300:]}")
        self._volumes.add(name)
        return name

    def new_sandbox(self, image: ImageRef, *, network: bool = False, allow_hosts: list[str] | None = None,
                    egress_image: str | None = None, files: dict[str, Path] | None = None) -> "DockerSandbox":

        name = f"factory-sb-{uuid.uuid4().hex[:12]}"
        args = ["run", "-d", "--name", name, "--memory", self.cfg.memory, "--cpus", str(self.cfg.cpus),
                "--pids-limit", str(self.cfg.pids_limit)]
        if not network:
            args += ["--network", "none"]
        elif allow_hosts is not None:
            proxy = f"http://{self._egress_proxy(allow_hosts, egress_image or 'python:3.13-slim')}:{EGRESS_PORT}"
            args += ["--network", EGRESS_NET]
            for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY"):
                args += ["-e", f"{k}={proxy}"]
            args += ["-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1"]
        for k, v in FIXED_ENV.items():
            args += ["-e", f"{k}={v}"]
        for dest, src in (files or {}).items():
            # the volume is mounted as dest's folder (read-only), so dest must have a folder of its own
            folder, _, filename = dest.rpartition("/")
            vol = self._file_volume(Path(src), filename, image.tag)
            args += ["--mount", f"type=volume,src={vol},dst={folder or '/'},readonly"]
        args += ["--entrypoint", "sleep", image.tag, "infinity"]
        r = self._run(args)
        if r.returncode != 0:
            self._run(["rm", "-f", name])
            raise SandboxStartError(f"could not start sandbox from {image.tag}: {r.stderr.strip()}")
        return DockerSandbox(self, r.stdout.strip(), self._user_for(image.tag))


class DockerSandbox(Sandbox):
    def __init__(self, factory: DockerSandboxFactory, container_id: str, user: str | None) -> None:
        self.factory = factory
        self.bin = factory.bin
        self.cid = container_id
        self.user = user
        self.repo_root = "/repo"
        self.limit = factory.cfg.output_limit_bytes
        self._closed = False

    def egress_ip(self) -> str | None:
        """This sandbox's address on the egress network (the proxy logs refusals by client address)."""
        fmt = '{{with index .NetworkSettings.Networks "' + EGRESS_NET + '"}}{{.IPAddress}}{{end}}'
        r = subprocess.run([self.bin, "inspect", "-f", fmt, self.cid], capture_output=True, text=True, check=False)
        return r.stdout.strip() or None

    def _exec_prefix(self, *, interactive: bool = False, env: dict | None = None,
                     cwd: str | None = None, secret_names: list[str] | None = None) -> list[str]:
        cmd = [self.bin, "exec"]
        if interactive:
            cmd.append("-i")
        if self.user:
            cmd += ["--user", self.user]
        if cwd:
            cmd += ["-w", cwd]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        for k in secret_names or []:
            cmd += ["-e", k]  # value comes from the docker CLI's own environment
        return cmd + [self.cid]

    def exec(self, argv: list[str], *, timeout_s: int, env: dict | None = None,
             cwd: str | None = None, secret_env: dict | None = None) -> ExecResult:
        self._ensure_open()
        secret_env = secret_env or {}
        cmd = self._exec_prefix(env=env, cwd=cwd, secret_names=sorted(secret_env)) + list(argv)
        start = time.monotonic()
        proc_env = {**os.environ, **secret_env} if secret_env else None
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=proc_env)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_all()
            proc.kill()
            out, err = proc.communicate()
        duration = time.monotonic() - start
        code = proc.returncode if not timed_out else -9
        result = ExecResult(argv=list(argv), exit_code=code, stdout=truncate_output(out, self.limit),
                            stderr=truncate_output(err, self.limit), duration_s=round(duration, 3),
                            timed_out=timed_out, oom_killed=(code == 137 and not timed_out))
        log.debug("exec %s -> %s", argv[:3], code,
                  extra={"data": {"argv": argv, "exit_code": code, "duration_s": result.duration_s,
                                  "timed_out": timed_out}})
        return result

    def _kill_all(self) -> None:
        """Kill every process in the sandbox except PID 1 (``kill -9 -1``)."""
        subprocess.run(self._exec_prefix() + ["sh", "-c", "kill -9 -1"], capture_output=True)

    def write_file(self, path: str, content: str | bytes) -> None:
        self._ensure_open()
        data = content.encode("utf-8") if isinstance(content, str) else content
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
        r = subprocess.run(self._exec_prefix(interactive=True) + ["sh", "-c", script, "sh", path],
                           input=data, capture_output=True)
        if r.returncode != 0:
            raise SandboxRuntimeError(f"write_file({path}) failed: {r.stderr.decode(errors='replace')}")

    def read_file(self, path: str) -> bytes:
        self._ensure_open()
        r = subprocess.run(self._exec_prefix() + ["sh", "-c", 'test -f "$1" && cat "$1"', "sh", path],
                           capture_output=True)
        if r.returncode != 0:
            raise FileNotFoundError(path)
        return r.stdout

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxRuntimeError("sandbox is closed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        r = subprocess.run([self.bin, "rm", "-f", self.cid], capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("could not remove container %s: %s", self.cid[:12], r.stderr.strip())
