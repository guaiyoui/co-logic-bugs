"""Shared result normalization helpers.

Kept dependency-free (stdlib only) so the same canonicalization can run inside
the old-DuckDB subprocess interpreter used by the differential oracle.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable

# Scalar encodings keep the value type so that e.g. the integer 1 and the
# string "1" never compare equal across versions.
def normalize_value(value: Any) -> Any:
    """Normalize a single cell to a JSON-safe, order-stable value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "float:nan"
        if math.isinf(value):
            return "float:+inf" if value > 0 else "float:-inf"
        return round(value, 6)
    if isinstance(value, Decimal):
        as_float = float(value)
        if math.isnan(as_float):
            return "float:nan"
        return round(as_float, 6)
    if isinstance(value, (datetime, date, time)):
        return f"temporal:{value.isoformat()}"
    if isinstance(value, timedelta):
        return f"interval:{value.total_seconds()}"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"bytes:{bytes(value).hex()}"
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_value(val) for key, val in value.items()}
    return str(value)


def normalize_rows(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    """Normalize a row sequence into a JSON-safe list of lists."""
    return [[normalize_value(value) for value in row] for row in rows]


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def loose_value(value: Any) -> Any:
    """Cross-version loose key for a *normalized* cell value.

    Numerics collapse to float (DECIMAL 1.5 == DOUBLE 1.5 == INTEGER 1) and a
    DATE equals the same TIMESTAMP at midnight. Everything else keeps its
    strict normalized tag.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return ["num", round(float(value), 6)]
    if isinstance(value, str):
        if value.startswith("temporal:"):
            stamp = value[len("temporal:") :]
            if _DATE_ONLY.match(stamp):
                stamp += "T00:00:00"
            return ["temporal", stamp]
        return value
    if isinstance(value, list):
        return [loose_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): loose_value(val) for key, val in value.items()}
    return value


def loose_bag(rows: Iterable[Any]) -> Any:
    """Row multiset under loose_value keys; ``rows`` are normalized rows."""
    import json
    from collections import Counter

    return Counter(
        json.dumps(loose_value(row), sort_keys=True, default=str) for row in rows
    )


def loose_rows_equal(left: Iterable[Any], right: Iterable[Any]) -> bool:
    """Loose multiset equality between two normalized row sets."""
    return loose_bag(left) == loose_bag(right)


INTERNAL_ERROR_MARKERS = (
    "INTERNAL",
    "InternalError",  # psycopg2 XX000 typename
    "XX000",
    "Assertion",
    "assertion",
    "Segmentation",
    "SEGV",
    "SIGSEGV",
    "FATAL",
    "PANIC",
    "sanitizer",
    "cache lookup failed",
    "unrecognized node",
    "variable not found in subplan",
    "could not find pathkey",
)


def is_internal_error(error: str | None) -> bool:
    """Return True when an error string looks like a DBMS internal failure."""
    if not error:
        return False
    return any(marker in error for marker in INTERNAL_ERROR_MARKERS)
