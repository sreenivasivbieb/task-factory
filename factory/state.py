"""Where each pipeline step keeps its JSON output under the workdir."""

from __future__ import annotations

from pathlib import Path


class Workdir:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def repo_spec(self) -> Path:
        return self.root / "repo_spec.json"

    @property
    def inspect_result(self) -> Path:
        return self.root / "inspect.json"

    @property
    def env_config(self) -> Path:
        return self.root / "env_config.json"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def rejections(self) -> Path:
        return self.root / "rejections.jsonl"
