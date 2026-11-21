import subprocess
import sys

from factory.cli import main


def test_help_works():
    r = subprocess.run([sys.executable, "-m", "factory.cli", "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "Coding Task Factory" in r.stdout


def test_unimplemented_command_exits_2(tmp_path):
    assert main(["report", "--workdir", str(tmp_path)]) == 2
