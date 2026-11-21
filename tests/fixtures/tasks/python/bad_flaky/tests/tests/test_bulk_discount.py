import tempfile
from pathlib import Path

from toyshop.pricing import line_total


def test_bulk_discount_stateful():
    counter = Path(tempfile.gettempdir()) / "bulk_discount_counter"
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    assert line_total(100, 9) == 900
    assert line_total(100, 10) == 900
    assert line_total(45, 11) == 445
    assert n == 0, "fails when run twice in one environment"
