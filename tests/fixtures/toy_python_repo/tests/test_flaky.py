"""A test that is flaky on purpose when TOY_FLAKY=1.

It then fails on every second run in the same environment (a counter file
in the temp folder), so flaky detection can be tested deterministically.
"""

import os
import tempfile
from pathlib import Path


def test_sometimes_flaky():
    if os.environ.get("TOY_FLAKY") != "1":
        return
    counter = Path(tempfile.gettempdir()) / "toyshop_flaky_counter"
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    assert n % 2 == 0, "flaky failure on purpose"
