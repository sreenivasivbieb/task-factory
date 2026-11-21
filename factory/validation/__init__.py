"""Validation runner: ``validate_task`` for drafts, ``validate_package`` for packages on disk."""

from __future__ import annotations

from .gates import Validator, validate_task


def validate_package(pkg_dir, factory, cfg=None, log_dir=None):
    """Re-validate an exported package from its files alone (see packaging.verify)."""
    from ..packaging.verify import verify_package

    return verify_package(pkg_dir, factory, cfg, log_dir)


__all__ = ["Validator", "validate_task", "validate_package"]
