"""Run the TypeScript unit tests (tests/ts/*.test.ts) with Node's built-in test runner.

Node 22.18+ runs TypeScript files directly, so there is nothing to install.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _node() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout
    m = re.match(r"v(\d+)\.(\d+)", out.strip())
    return node if m and (int(m[1]), int(m[2])) >= (22, 18) else None


@pytest.mark.skipif(_node() is None, reason="needs Node 22.18+ (runs TypeScript directly)")
def test_typescript_unit_tests():
    files = sorted(str(p) for p in (ROOT / "tests" / "ts").glob("*.test.ts"))
    r = subprocess.run([_node(), "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
