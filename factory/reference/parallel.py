"""Run independent sub-agent calls at the same time (``[reference] workers``; 1 = one after another)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def pmap(fn: Callable[[T], R], items: Iterable[T], workers: int = 1) -> list[R]:
    """``[fn(x) for x in items]`` with up to ``workers`` calls at once; order kept, first error raised."""
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items)), thread_name_prefix="factory-ref") as ex:
        return list(ex.map(fn, items))
