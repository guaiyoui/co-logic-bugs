"""SQLite runner — stdlib sqlite3, in-memory, zero cost.

Oracle semantics: same as other engines — run setup, run query,
normalized row bags. SQLite is the most-fuzzed DBMS in existence; its
value here is as an *adjudication reference* and an honest "clean engine"
data point, not as a bug mine.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from util.efficiency import eff
from dataclasses import dataclass, field

from oracles.db_runner import QueryResult  # noqa: F401  (shared interface)

LOGGER = logging.getLogger("sqlite_runner")


def _norm(v):
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return int(v)
    return v


class SQLiteRunner:
    def __init__(self, _datadir: str | None = None):
        self._con = sqlite3.connect(":memory:")

    def connect(self):
        self._con = sqlite3.connect(":memory:")

    def setup(self, stmts):
        """Fresh in-memory db per case; returns (stmt, error|None) pairs
        so dialect-mismatched statements are dropped, not fatal."""
        self._con.close()
        self._con = sqlite3.connect(":memory:")
        outcomes = []
        for s in stmts:
            sql = s.strip().rstrip(";")
            if not sql:
                continue
            try:
                eff.count("sql_executions")
                self._con.executescript(sql + ";")
                outcomes.append((s, None))
            except Exception as exc:  # noqa: BLE001 - record and continue
                outcomes.append((s, str(exc)))
        self._con.commit()
        return outcomes

    def run(self, sql: str, timeout_s: float = 10.0) -> QueryResult:
        r = QueryResult()
        t0 = time.monotonic()
        try:
            eff.count("sql_executions")
            cur = self._con.execute(sql)
            r.rows = [[_norm(v) for v in row] for row in cur.fetchall()]
            r.columns = ([d[0] for d in cur.description]
                         if cur.description else [])
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"
        r.elapsed = time.monotonic() - t0
        return r

    def cleanup(self):
        try:
            self._con.close()
        except Exception:
            pass

    def close(self):
        self.cleanup()
