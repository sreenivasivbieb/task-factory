"""Launch the Task Factory terminal engine with the bundled Task Factory extension.

The engine is a separate program. It keeps its own config, credentials and sessions
under the task-factory XDG directories. Inherited ``OPENCODE_*`` variables are dropped
because they belong to other installs; the engine's settings are ``TASK_FACTORY_*``, e.g.
``TASK_FACTORY_CONFIG_CONTENT`` or ``TASK_FACTORY_CLI_CONFIG_CONTENT``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from .errors import ConfigError

PREFIX = "TASK_FACTORY_"
ENGINE_PREFIX = "OPENCODE_"
# Provider credential for the Zen model service, kept so its models keep working.
SHARED = {"OPENCODE_API_KEY"}


def engine_environment(source: dict[str, str]) -> dict[str, str]:
    """Drop inherited engine settings and map ``TASK_FACTORY_*`` onto the engine's names."""
    env = {key: value for key, value in source.items()
           if not key.startswith(ENGINE_PREFIX) or key in SHARED}
    for key, value in source.items():
        if key.startswith(PREFIX) and len(key) > len(PREFIX):
            env[ENGINE_PREFIX + key[len(PREFIX):]] = value
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    return env


def engine_path() -> str:
    override = os.environ.get("FACTORY_TERMINAL")
    data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    name = "task-factory-engine.exe" if os.name == "nt" else "task-factory-engine"
    path = shutil.which(override or str(data / "task-factory" / "bin" / name))
    if path is None:
        raise ConfigError("Task Factory terminal engine is missing. See README.md for the build command.")
    return path


def _saved_plugins(env: dict[str, str]) -> list:
    """Read the on-disk plugin list before applying the inline override."""
    root = Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    try:
        source = (root / "task-factory/cli.json").read_text(encoding="utf-8")
        without_comments = re.sub(
            r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/',
            lambda token: token[0] if token[0].startswith('"') else "",
            source,
        )
        json_text = re.sub(
            r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])',
            lambda token: token[0] if token[0].startswith('"') else "",
            without_comments,
        )
        config = json.loads(json_text)
    except (OSError, ValueError):
        return []
    plugins = config.get("plugins") if isinstance(config, dict) else None
    return plugins if isinstance(plugins, list) else []


def _move_legacy_database(env: dict[str, str]) -> None:
    """Keep sessions and logins from builds that named the database after the engine."""
    data = Path(env.get("XDG_DATA_HOME") or (Path.home() / ".local/share")) / "task-factory"
    old, new = data / "opencode-task-factory.db", data / "task-factory.db"
    if not old.exists() or new.exists():
        return
    try:
        for suffix in ("-wal", "-shm", ""):  # the main file last, so a partial move is retried
            if Path(f"{old}{suffix}").exists():
                os.replace(f"{old}{suffix}", f"{new}{suffix}")
    except OSError:
        pass


def launch(args: list[str], *, integrate: bool = True) -> int:
    program = engine_path()
    env = engine_environment(dict(os.environ))
    _move_legacy_database(env)
    args = ["--continue" if arg == "--resume" else arg for arg in args]
    if integrate:
        try:
            config = json.loads(env.get("OPENCODE_CLI_CONFIG_CONTENT") or "{}")
        except ValueError as exc:
            raise ConfigError("TASK_FACTORY_CLI_CONFIG_CONTENT must be a JSON object.") from exc
        if not isinstance(config, dict) or not isinstance(config.get("plugins", []), list):
            raise ConfigError("TASK_FACTORY_CLI_CONFIG_CONTENT has an invalid plugin list.")
        plugin = Path(__file__).with_name("terminal_plugin").resolve().as_uri()
        plugins = config.get("plugins")
        if plugins is None:
            plugins = _saved_plugins(env)
        config["plugins"] = [
            *plugins,
            {"package": plugin, "options": {"python": sys.executable}},
        ]
        env["OPENCODE_CLI_CONFIG_CONTENT"] = json.dumps(config)
    os.execvpe(program, [program, *args], env)
