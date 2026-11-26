import subprocess
import sys

from factory.cli import main


def test_help_works():
    r = subprocess.run([sys.executable, "-m", "factory.cli", "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "Coding Task Factory" in r.stdout


def test_unknown_command_exits_2(tmp_path, capsys):
    import pytest

    with pytest.raises(SystemExit) as info:  # argparse: "invalid choice"
        main(["frobnicate", "--workdir", str(tmp_path)])
    assert info.value.code == 2 and "invalid choice" in capsys.readouterr().err
