"""Subprocess entrypoint executed by the *old* DuckDB interpreter.

Reads ``{"schema_sqls": [...], "queries": [...]}`` from stdin, executes
everything on a fresh in-memory database, and writes a JSON array of
normalized per-query results to stdout. Kept free of third-party imports
other than ``duckdb`` so it runs inside the bare old-version venv.

Usage:
    PYTHONPATH=<project> <old_venv>/bin/python -m oracles._remote_exec
"""

from __future__ import annotations

import json
import sys

import duckdb

from oracles.normalize import is_internal_error, normalize_rows


def main() -> int:
    payload = json.load(sys.stdin)
    schema_sqls = payload.get("schema_sqls", [])
    queries = payload.get("queries", [])

    conn = duckdb.connect(":memory:")
    setup_errors = []
    for statement in schema_sqls:
        sql = statement.strip().rstrip(";")
        if not sql:
            continue
        try:
            conn.execute(sql)
        except Exception as exc:  # noqa: BLE001
            setup_errors.append({"statement": statement, "error": str(exc)})

    results = []
    for query in queries:
        record = {"rows": [], "error": None, "is_internal_error": False}
        try:
            cursor = conn.execute(query)
            record["rows"] = normalize_rows(cursor.fetchall())
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["is_internal_error"] = is_internal_error(record["error"])
        results.append(record)

    json.dump(
        {
            "duckdb_version": duckdb.__version__,
            "setup_errors": setup_errors,
            "results": results,
        },
        sys.stdout,
        default=str,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
