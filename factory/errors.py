"""Typed exceptions. Components raise these; the CLI maps them to exit codes."""

from __future__ import annotations


class FactoryError(Exception):
    """Base class for all factory errors."""

    exit_code = 1


class ConfigError(FactoryError):
    """Bad usage or configuration (exit code 2)."""

    exit_code = 2


class EnvironmentProblem(FactoryError):
    """The host environment is missing something, e.g. Docker (exit code 3)."""

    exit_code = 3


class RepoLoadError(FactoryError):
    """The supplied repository cannot be loaded or pinned."""


class BuildError(FactoryError):
    """A sandbox image failed to build. ``log`` holds the build output."""

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


class SandboxStartError(FactoryError):
    """A sandbox container could not be started."""


class SandboxRuntimeError(FactoryError):
    """A sandbox operation failed for a reason other than the command's own exit code."""


class ParseError(FactoryError):
    """A test report could not be parsed."""


class PackageError(FactoryError):
    """A task package is malformed or fails verification."""
