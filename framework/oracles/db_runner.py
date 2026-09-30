"""DuckDB execution wrapper used by every oracle.

The runner owns a single in-memory connection. Queries execute on a worker
thread so a wall-clock timeout can interrupt them via ``Connection.interrupt``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

import duckdb

from .normalize import is_internal_error, normalize_rows
from util.efficiency import eff

LOGGER = logging.getLogger(__name__)


@dataclass
class QueryResult:
    """Normalized outcome of one SQL execution."""

    rows: list[list[Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error: str | None = None
    is_internal_error: bool = False
    elapsed: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    def bag(self) -> Counter:
        """Row multiset; ordering is ignored on purpose."""
        return Counter(
            json.dumps(row, sort_keys=True, default=str) for row in self.rows
        )

    def summary(self, max_rows: int = 8) -> dict[str, Any]:
        """Compact, JSON-safe description used in candidate records."""
        return {
            "ok": self.ok,
            "error": self.error,
            "is_internal_error": self.is_internal_error,
            "timed_out": self.timed_out,
            "row_count": len(self.rows),
            "columns": self.columns,
            "sample_rows": self.rows[:max_rows],
            "elapsed": round(self.elapsed, 4),
        }


def bags_equal(left: QueryResult, right: QueryResult) -> bool:
    """Compare two results as row multisets, ignoring order."""
    return left.bag() == right.bag()


class DuckDBRunner:
    """Execute setup and queries against a fresh in-memory DuckDB."""

    def __init__(self, version_tag: str = "local", memory_limit: str = "1GB"):
        self.version_tag = version_tag
        self.memory_limit = memory_limit
        self._conn: duckdb.DuckDBPyConnection | None = None

    @property
    def engine_version(self) -> str:
        return duckdb.__version__

    def connect(self) -> duckdb.DuckDBPyConnection:
        """(Re)create the in-memory connection."""
        self.close()
        self._conn = duckdb.connect(":memory:")
        try:
            self._conn.execute(f"SET memory_limit = '{self.memory_limit}'")
        except duckdb.Error:
            # Older versions may reject the setting; not fatal.
            LOGGER.debug("memory_limit setting rejected", exc_info=True)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                LOGGER.debug("connection close failed", exc_info=True)
            self._conn = None

    def setup(self, schema_sqls: Iterable[str]) -> list[tuple[str, str | None]]:
        """Run schema/insert statements on a fresh connection.

        Returns a list of ``(statement, error_or_None)`` pairs so callers can
        drop the statements that failed while keeping the rest.
        """
        conn = self.connect()
        outcomes: list[tuple[str, str | None]] = []
        for statement in schema_sqls:
            sql = statement.strip().rstrip(";")
            if not sql:
                continue
            try:
                eff.count("sql_executions")
                conn.execute(sql)
                outcomes.append((statement, None))
            except Exception as exc:  # noqa: BLE001 - record and continue
                outcomes.append((statement, str(exc)))
                LOGGER.debug("setup statement failed: %s", exc)
        return outcomes

    def run(self, sql: str, timeout_s: float = 10.0) -> QueryResult:
        """Execute one query with a wall-clock timeout.

        The query runs on a worker thread sharing the connection; on timeout
        ``interrupt()`` cancels the execution and the result is marked.
        """
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        conn = self._conn
        result = QueryResult()
        finished = threading.Event()

        def _worker() -> None:
            try:
                eff.count("sql_executions")
                cursor = conn.execute(sql)
                rows = cursor.fetchall()
                result.rows = normalize_rows(rows)
                result.columns = [item[0] for item in (cursor.description or [])]
            except Exception as exc:  # noqa: BLE001 - any engine error recorded
                result.error = f"{type(exc).__name__}: {exc}"
                result.is_internal_error = is_internal_error(result.error)
            finally:
                finished.set()

        thread = threading.Thread(target=_worker, daemon=True)
        start = time.monotonic()
        thread.start()
        thread.join(timeout_s)
        result.elapsed = time.monotonic() - start
        if thread.is_alive():
            try:
                conn.interrupt()
            except Exception:  # noqa: BLE001
                LOGGER.debug("interrupt failed", exc_info=True)
            thread.join(min(timeout_s, 5.0))
            result.timed_out = True
            result.error = result.error or f"TimeoutError: exceeded {timeout_s}s"
            return result
        finished.wait(timeout=1.0)
        return result

    def explain_plan(self, sql: str) -> dict | None:
        """Return the parsed JSON plan for a query, or None on failure."""
        result = self.run(f"EXPLAIN (FORMAT JSON) {sql}", timeout_s=10.0)
        if not result.ok or not result.rows:
            return None
        try:
            payload = result.rows[0][1] if len(result.rows[0]) > 1 else result.rows[0][0]
            if isinstance(payload, str):
                payload = json.loads(payload)
            return payload
        except (json.JSONDecodeError, TypeError, IndexError):
            return None
