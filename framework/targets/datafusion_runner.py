"""DataFusion runner — Apache Arrow-native columnar engine, embedded via
``pip install datafusion`` (datafusion-python bindings).

Same interface as DuckDBRunner/PostgresRunner: setup + run + normalized
bags. DataFusion's SQL coverage is narrower (Postgres-ish dialect); a
dialect mismatch lands as an error, not a divergence — only shared-shape
queries produce comparable bags.
"""
from __future__ import annotations

import logging
import time

from util.efficiency import eff

from oracles.db_runner import QueryResult  # noqa: F401  (shared interface)

LOGGER = logging.getLogger("datafusion_runner")


def _norm(v):
    import datetime
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return int(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    return v


class DataFusionRunner:
    def __init__(self, _datadir: str | None = None):
        import datafusion
        self._ctx = datafusion.SessionContext()

    def connect(self):
        import datafusion
        self._ctx = datafusion.SessionContext()

    def setup(self, stmts):
        """Fresh session per case; returns (stmt, error|None) pairs so
        dialect-mismatched statements are dropped, not fatal."""
        self.connect()
        outcomes = []
        for s in stmts:
            try:
                eff.count("sql_executions")
                self._ctx.sql(s).collect()
                outcomes.append((s, None))
            except Exception as exc:  # noqa: BLE001 - record and continue
                outcomes.append((s, str(exc)))
        return outcomes

    def run(self, sql: str, timeout_s: float = 10.0) -> QueryResult:
        r = QueryResult()
        t0 = time.monotonic()
        try:
            eff.count("sql_executions")
            df = self._ctx.sql(sql)
            rows = df.collect()
            if rows:
                r.columns = list(rows[0].schema.names)
            out = []
            for batch in rows:
                # read by column index — to_pydict() collapses same-named
                # columns (e.g. self-joins returning a.x and b.x)
                cols = [batch.column(i).to_pylist()
                        for i in range(batch.num_columns)]
                for i in range(batch.num_rows):
                    out.append([_norm(c[i]) for c in cols])
            r.rows = out
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"
        r.elapsed = time.monotonic() - t0
        return r

    def cleanup(self):
        pass

    def close(self):
        self.cleanup()
