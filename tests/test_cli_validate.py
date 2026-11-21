import json
from pathlib import Path

from factory.cli import main

TASKS = Path(__file__).parent / "fixtures" / "tasks" / "python"


def test_validate_drafts_local(py_repo, tmp_path, capsys):
    wd = tmp_path / "wd"
    assert main(["load", "--repo", str(py_repo), "--workdir", str(wd)]) == 0
    assert main(["inspect", "--workdir", str(wd), "--backend", "local"]) == 0
    code = main(["validate", "--tasks", str(TASKS), "--workdir", str(wd), "--backend", "local"])
    out = capsys.readouterr().out
    assert code == 0
    assert "1/8 valid" in out
    rejections = [json.loads(line) for line in (wd / "rejections.jsonl").read_text().splitlines()]
    assert len(rejections) == 7
    assert list((wd / "runs").glob("*/report.json"))
