"""Sandbox backends. ``make_factory`` picks one from the config."""

from __future__ import annotations

from pathlib import Path

from ..config import FactoryConfig
from ..errors import ConfigError
from .base import Sandbox, SandboxFactory

__all__ = ["Sandbox", "SandboxFactory", "make_factory"]


def make_factory(cfg: FactoryConfig) -> SandboxFactory:
    backend = cfg.sandbox.backend
    if backend == "docker":
        from .docker_backend import DockerSandboxFactory

        return DockerSandboxFactory(cfg.sandbox)
    if backend == "local":
        from .local_backend import LocalSandboxFactory

        return LocalSandboxFactory(Path(cfg.workdir) / "cache", cfg.sandbox)
    raise ConfigError(f"unknown sandbox backend {backend!r} (use docker or local)")
