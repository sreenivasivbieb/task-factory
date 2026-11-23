import json
from pathlib import Path

from factory import terminal, terminal_bridge
from factory.config import FactoryConfig


def test_launcher_preserves_upstream_arguments_and_adds_plugin(monkeypatch):
    monkeypatch.setenv("TASK_FACTORY_CLI_CONFIG_CONTENT", json.dumps({
        "plugins": ["my-plugin"], "commands": {"example": {"template": "Hello"}},
    }))
    monkeypatch.setattr(terminal, "engine_path", lambda: "/engine/task-factory-engine")
    launch = []
    monkeypatch.setattr(terminal.os, "execvpe", lambda *args: launch.append(args))
    terminal.launch(["/path with spaces", "--model", "provider/model", "--resume"])
    program, command, env = launch[0]
    assert program == "/engine/task-factory-engine"
    assert command[1:] == ["/path with spaces", "--model", "provider/model", "--continue"]
    config = json.loads(env["OPENCODE_CLI_CONFIG_CONTENT"])
    assert config["commands"] == {"example": {"template": "Hello"}}
    assert config["plugins"][0] == "my-plugin"
    assert config["plugins"][1]["package"].endswith("/factory/terminal_plugin")


def test_launcher_ignores_inherited_engine_settings(monkeypatch):
    """Another install's settings must never configure the Task Factory engine."""
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/home/user/.config/other")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"plugin": ["their-plugin"]}')
    monkeypatch.setenv("OPENCODE_CLI_CONFIG_CONTENT", '{"plugins": ["their-tui-plugin"]}')
    monkeypatch.setenv("OPENCODE_DB", "/home/user/other.db")
    monkeypatch.setenv("OPENCODE_API_KEY", "zen-key")
    monkeypatch.setenv("TASK_FACTORY_LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("TASK_FACTORY_CLI_CONFIG_CONTENT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent")
    monkeypatch.setattr(terminal, "engine_path", lambda: "/engine/task-factory-engine")
    launch = []
    monkeypatch.setattr(terminal.os, "execvpe", lambda *args: launch.append(args))
    terminal.launch([])
    _, _, env = launch[0]
    for name in ("OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "OPENCODE_DB"):
        assert name not in env
    assert env["OPENCODE_API_KEY"] == "zen-key"
    assert env["OPENCODE_LOG_LEVEL"] == "DEBUG"
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    plugins = json.loads(env["OPENCODE_CLI_CONFIG_CONTENT"])["plugins"]
    assert len(plugins) == 1 and plugins[0]["package"].endswith("/factory/terminal_plugin")


def test_bridge_generates_valid_pending_task_in_current_repo(py_repo, tmp_path, monkeypatch, capsys):
    fixture = Path(__file__).parent / "fixtures/tasks/python/good_bulk_discount"
    requirements = (fixture / "requirements.md").read_text()
    after = (fixture / "after/toyshop/pricing.py").read_text()
    hidden = (fixture / "tests/tests/test_bulk_discount.py").read_text()
    prompts = []

    def reply(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps({"title": "Bulk discount", "requirements": requirements,
                               "after": {"toyshop/pricing.py": after}})
        return json.dumps({"tests": {"tests/test_bulk_discount.py": hidden}})

    def local_config(**options):
        cfg = FactoryConfig(**options)
        cfg.sandbox.backend = "local"
        return cfg

    monkeypatch.setattr(terminal_bridge, "FactoryConfig", local_config)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(py_repo)
    terminal_bridge.generate("Add a discount to pricing.py", "test/model", reply)
    assert "(pending)" in capsys.readouterr().out
    assert "toyshop/pricing.py" in prompts[0]
    assert requirements in prompts[1]
    assert after not in prompts[1]


def test_launcher_keeps_sessions_from_the_old_database_name(monkeypatch, tmp_path):
    data = tmp_path / "task-factory"
    data.mkdir()
    (data / "opencode-task-factory.db").write_text("db")
    (data / "opencode-task-factory.db-wal").write_text("wal")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(terminal, "engine_path", lambda: "/engine/task-factory-engine")
    monkeypatch.setattr(terminal.os, "execvpe", lambda *args: None)
    terminal.launch([])
    assert (data / "task-factory.db").read_text() == "db"
    assert (data / "task-factory.db-wal").read_text() == "wal"
    assert not (data / "opencode-task-factory.db").exists()


def test_bridge_reports_why_a_draft_was_rejected(monkeypatch, capsys):
    import io

    from factory.errors import PackageError

    def rejected(request, model, reply):
        print("NOT added: FEATURE_TESTS_FAIL_WITH_PATCH: feature tests fail with the reference patch")
        raise PackageError("Draft rejected by Task Factory.")

    monkeypatch.setattr(terminal_bridge, "generate", rejected)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"request": "x", "model": "m/x"}) + "\n"))
    terminal_bridge.main()
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "FEATURE_TESTS_FAIL_WITH_PATCH" in result["text"] and "Draft rejected" in result["text"]


def test_only_flags_folders_or_no_args_open_the_terminal(tmp_path, monkeypatch):
    from factory.cli import _launches_terminal

    monkeypatch.chdir(tmp_path)
    (tmp_path / "tasks").mkdir()  # a folder named like a subcommand is still the subcommand
    assert _launches_terminal([]) and _launches_terminal(["chat"]) and _launches_terminal(["terminal", "models"])
    assert _launches_terminal(["--continue"]) and _launches_terminal([str(tmp_path)])
    assert not _launches_terminal(["taks", "list"])  # typo: argparse reports it
    assert not _launches_terminal(["tasks", "list"]) and not _launches_terminal(["--help"])
