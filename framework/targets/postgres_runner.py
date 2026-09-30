"""PostgreSQL execution wrapper backed by an embedded ``pgserver`` instance.

The runner owns a private server datadir (no root required). Each ``setup``
call resets the ``public`` schema so every test case runs on a clean database.
Queries run on a worker thread; a wall-clock timeout is enforced both by
``statement_timeout`` and by abandoning the thread.

Passing ``pg_prefix`` swaps the embedded build for a self-compiled install
prefix (``bin/initdb``, ``bin/pg_ctl``, ``bin/postgres``) via
``targets.pg_local_server.LocalPgServer`` — used by the version-ladder
differential to run assert/older builds.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import psycopg2

from oracles.db_runner import QueryResult
from oracles.normalize import is_internal_error, normalize_rows
from util.efficiency import eff

LOGGER = logging.getLogger(__name__)

_CRASH_MARKERS = (
    "server closed the connection",
    "terminating connection",
    "connection not open",
    "could not receive data from server",
)

# Server-log signatures for deaths that never surface on the client
# connection (parallel workers, io workers, autovacuum, checkpointer).
_LOG_FATAL_RE = re.compile(
    r"TRAP:|PANIC|was terminated by signal|server process.*(?:exited|"
    r"terminated)|segmentation fault|terminating any other active|"
    r"AddressSanitizer|LeakSanitizer|runtime error:|SUMMARY: "
    r"(?:Address|UndefinedBehavior|Leak)Sanitizer",
    re.IGNORECASE)


class PostgresRunner:
    """Execute setup and queries against an embedded PostgreSQL server."""

    def __init__(
        self,
        datadir: str | Path,
        statement_timeout_ms: int = 15000,
        version_tag: str = "local",
        pg_prefix: str | Path | None = None,
    ):
        import os

        pg_prefix = pg_prefix or os.environ.get("COEVO_PG_PREFIX")
        self.datadir = Path(datadir)
        self.statement_timeout_ms = statement_timeout_ms
        self.version_tag = version_tag
        self.datadir.mkdir(parents=True, exist_ok=True)
        if pg_prefix is not None:
            from targets.pg_local_server import LocalPgServer

            self._server = LocalPgServer(pg_prefix, self.datadir)
        else:
            import pgserver

            self._server = pgserver.get_server(str(self.datadir))
        self._conn: psycopg2.extensions.connection | None = None
        self._version: str | None = None

    # ----------------------------------------------------------- lifecycle
    @property
    def engine_version(self) -> str:
        if self._version is None:
            result = self.run("SHOW server_version")
            self._version = (
                str(result.rows[0][0]) if result.ok and result.rows else "unknown"
            )
        return self._version

    def connect(self) -> psycopg2.extensions.connection:
        self.close()
        # After an assert-crash the postmaster enters crash recovery and
        # briefly refuses connections; retry until it accepts them.
        last_exc: Exception | None = None
        for _ in range(30):
            try:
                self._conn = psycopg2.connect(self._server.get_uri())
                self._conn.autocommit = True
                self._conn.cursor().execute(
                    f"SET statement_timeout = {self.statement_timeout_ms}"
                )
                return self._conn
            except psycopg2.OperationalError as exc:
                last_exc = exc
                time.sleep(0.5)
        assert last_exc is not None
        raise last_exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                LOGGER.debug("pg connection close failed", exc_info=True)
            self._conn = None

    def cleanup(self) -> None:
        """Close the connection and stop the embedded server."""
        self.close()
        try:
            self._server.cleanup()
        except Exception:  # noqa: BLE001
            LOGGER.debug("pgserver cleanup failed", exc_info=True)

    # ------------------------------------------------------------- execute
    def setup(self, schema_sqls: Iterable[str]) -> list[tuple[str, str | None]]:
        """Reset the public schema, then run each statement."""
        conn = self._conn or self.connect()
        outcomes: list[tuple[str, str | None]] = []
        try:
            cur = conn.cursor()
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        except psycopg2.Error as exc:
            # The previous case may have left the session in an aborted
            # transaction (or a dropped role) — reconnect and retry the reset
            # on the fresh connection, otherwise the schema_sqls below run on
            # the OLD schema and silently contaminate this case.
            LOGGER.warning("schema reset failed, reconnecting: %s", exc)
            conn = self.connect()
            try:
                cur = conn.cursor()
                cur.execute("DROP SCHEMA public CASCADE")
                cur.execute("CREATE SCHEMA public")
            except psycopg2.Error as exc2:
                LOGGER.warning("schema reset retry failed: %s", exc2)
        for statement in schema_sqls:
            sql = statement.strip().rstrip(";")
            if not sql:
                continue
            try:
                eff.count("sql_executions")
                conn.cursor().execute(sql)
                outcomes.append((statement, None))
            except Exception as exc:  # noqa: BLE001 - record and continue
                outcomes.append((statement, str(exc)))
                LOGGER.debug("pg setup statement failed: %s", exc)
                if not self._alive():
                    self.connect()
        return outcomes

    def log_new_lines(self) -> list[str]:
        """Lines appended to the server log since the previous call."""
        path = self.datadir / "local_pg.log"
        try:
            data = path.read_bytes()
        except OSError:
            return []
        off = getattr(self, "_log_off", 0)
        if len(data) <= off:
            return []
        self._log_off = len(data)
        return data[off:].decode("utf-8", "replace").splitlines()

    def log_fatal_lines(self, lines: Iterable[str]) -> list[str]:
        """Filter for crash signatures invisible to the client session."""
        return [ln for ln in lines if _LOG_FATAL_RE.search(ln)]

    def _alive(self) -> bool:
        try:
            self._conn.cursor().execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    def run(self, sql: str, timeout_s: float = 10.0) -> QueryResult:
        """Execute one query; crashes/timeouts are captured, never raised."""
        if self._conn is None or not self._alive():
            self.connect()
        assert self._conn is not None
        conn = self._conn
        result = QueryResult()
        finished = threading.Event()

        def _worker() -> None:
            try:
                eff.count("sql_executions")
                cur = conn.cursor()
                cur.execute(sql)
                if cur.description is not None:
                    rows = cur.fetchall()
                    result.rows = normalize_rows(rows)
                    result.columns = [d.name for d in cur.description]
            except Exception as exc:  # noqa: BLE001 - any engine error recorded
                result.error = f"{type(exc).__name__}: {exc}"
                low = result.error.lower()
                result.is_internal_error = is_internal_error(
                    result.error
                ) or any(m in low for m in _CRASH_MARKERS)
            finally:
                finished.set()

        thread = threading.Thread(target=_worker, daemon=True)
        start = time.monotonic()
        thread.start()
        thread.join(timeout_s)
        result.elapsed = time.monotonic() - start
        if thread.is_alive():
            result.timed_out = True
            result.error = result.error or f"TimeoutError: exceeded {timeout_s}s"
            try:
                conn.cancel()
            except Exception:  # noqa: BLE001
                LOGGER.debug("pg cancel failed", exc_info=True)
            return result
        finished.wait(timeout=1.0)
        if result.is_internal_error:
            # A crashed backend kills the connection; reconnect for next call.
            try:
                self.connect()
            except Exception:  # noqa: BLE001
                LOGGER.debug("reconnect after crash failed", exc_info=True)
        return result

    def explain_plan(self, sql: str) -> dict[str, Any] | None:
        """Return the parsed JSON plan for a query, or None on failure."""
        result = self.run(f"EXPLAIN (FORMAT JSON) {sql}", timeout_s=10.0)
        if not result.ok or not result.rows:
            return None
        try:
            payload = result.rows[0][0]
            if isinstance(payload, str):
                payload = json.loads(payload)
            return payload
        except (json.JSONDecodeError, TypeError, IndexError):
            return None
