"""Minimal deterministic TOML writer (the standard library only reads TOML).

Supports str, int, float, bool, lists of scalars, nested tables and arrays of
tables. ``None`` values are left out. Key order is preserved.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN/inf not supported")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # TOML basic strings use JSON-compatible escapes
    raise TypeError(f"unsupported TOML value: {type(v).__name__}")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else json.dumps(k)


def _is_table_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _emit(data: dict, prefix: str, out: list[str]) -> None:
    tables, arrays = [], []
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, dict):
            tables.append((k, v))
        elif _is_table_list(v):
            arrays.append((k, v))
        elif isinstance(v, list):
            if len(v) <= 3:
                out.append(f"{_key(k)} = [{', '.join(_scalar(x) for x in v)}]")
            else:
                out.append(f"{_key(k)} = [")
                out += [f"  {_scalar(x)}," for x in v]
                out.append("]")
        else:
            out.append(f"{_key(k)} = {_scalar(v)}")
    for k, v in tables:
        name = f"{prefix}{_key(k)}"
        out += ["", f"[{name}]"]
        _emit(v, name + ".", out)
    for k, items in arrays:
        name = f"{prefix}{_key(k)}"
        for item in items:
            out += ["", f"[[{name}]]"]
            _emit(item, name + ".", out)


def dumps(data: dict) -> str:
    out: list[str] = []
    _emit(data, "", out)
    return "\n".join(out).lstrip("\n") + "\n"
