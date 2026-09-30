"""TiDB execution wrapper for a running TiDB instance (e.g. ``tiup playground``).

TiDB speaks the MySQL wire protocol and self-declares MySQL 8.0
compatibility, so ``mysql-connector-python`` (or PyMySQL) talks to it
directly. The runner owns a dedicated test database (default ``coevotest``);
each ``setup`` call drops and recreates it so every test case runs on a
clean schema. Queries run on a worker thread with a wall-clock timeout —
on timeout the runner issues ``KILL QUERY`` from a side connection.

DSN form: ``mysql://user:pass@host:port`` — all parts optional.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlparse

import mysql.connector
from mysql.connector import MySQLConnection

from oracles.db_runner import QueryResult
from oracles.normalize import is_internal_error, normalize_rows
from util.efficiency import eff

LOGGER = logging.getLogger(__name__)

_CRASH_MARKERS = (
    "Lost connection",
    "server has gone away",
    "connection not available",
    "Can't connect to MySQL server",
)


def _parse_dsn(dsn: str | None) -> dict[str, Any]:
    if not dsn:
        return {}
    parsed = urlparse(dsn if "://" in dsn else f"mysql://{dsn}")
    cfg: dict[str, Any] = {}
    if parsed.hostname:
        cfg["host"] = parsed.hostname
    if parsed.port:
        cfg["port"] = parsed.port
    if parsed.username:
        cfg["user"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    path = (parsed.path or "").lstrip("/")
    if path:
        cfg["database"] = path
    return cfg


class TiDBRunner:
    """Execute setup and queries against a live TiDB server."""

    def __init__(
        self,
        dsn: str | None = None,
        database: str = "coevotest",
        statement_timeout_ms: int = 15000,
        version_tag: str = "local",
    ):
        cfg = {
            "host": "127.0.0.1",
            "port": 4000,
            "user": "root",
            "password": "",
        }
        cfg.update(_parse_dsn(dsn))
        self.database = cfg.pop("database", None) or database
        self._cfg = cfg
        self.statement_timeout_ms = statement_timeout_ms
        self.version_tag = version_tag
        self._conn: MySQLConnection | None = None
        self._conn_id: int | None = None
        self._version: str | None = None

    # ----------------------------------------------------------- lifecycle
    @property
    def engine_version(self) -> str:
        if self._version is None:
            result = self.run("SELECT VERSION()")
            self._version = (
                str(result.rows[0][0]) if result.ok and result.rows else "unknown"
            )
        return self._version

    def connect(self) -> MySQLConnection:
        self.close()
        self._conn = mysql.connector.connect(
            autocommit=True, connection_timeout=10, **self._cfg
        )
        cur = self._conn.cursor()
        cur.execute(
            f"SET SESSION max_execution_time = {self.statement_timeout_ms}"
        )
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
        cur.execute(f"USE `{self.database}`")
        cur.execute("SELECT CONNECTION_ID()")
        self._conn_id = cur.fetchone()[0]
        cur.close()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                LOGGER.debug("tidb connection close failed", exc_info=True)
            self._conn = None

    def cleanup(self) -> None:
        """Drop the test database and close the connection."""
        try:
            admin = mysql.connector.connect(
                autocommit=True, connection_timeout=10, **self._cfg
            )
            admin.cursor().execute(f"DROP DATABASE IF EXISTS `{self.database}`")
            admin.close()
        except Exception:  # noqa: BLE001
            LOGGER.debug("tidb cleanup drop failed", exc_info=True)
        self.close()

    # ------------------------------------------------------------- execute
    def setup(self, schema_sqls: Iterable[str]) -> list[tuple[str, str | None]]:
        """Drop/recreate the test database, then run each statement."""
        try:
            admin = self._conn or self.connect()
            cur = admin.cursor()
            cur.execute(f"DROP DATABASE IF EXISTS `{self.database}`")
            cur.execute(f"CREATE DATABASE `{self.database}`")
            cur.execute(f"USE `{self.database}`")
            cur.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("database reset failed, reconnecting: %s", exc)
            self.connect()
        conn = self._conn
        assert conn is not None
        outcomes: list[tuple[str, str | None]] = []
        for statement in schema_sqls:
            sql = statement.strip().rstrip(";")
            if not sql:
                continue
            try:
                eff.count("sql_executions")
                cur = conn.cursor()
                cur.execute(sql)
                cur.close()
                outcomes.append((statement, None))
            except Exception as exc:  # noqa: BLE001 - record and continue
                outcomes.append((statement, str(exc)))
                LOGGER.debug("tidb setup statement failed: %s", exc)
                if not self._alive():
                    self.connect()
                    conn = self._conn
        return outcomes

    def _alive(self) -> bool:
        try:
            if self._conn is None:
                return False
            self._conn.ping(reconnect=False, attempts=1, delay=0)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _kill_conn_id(self, conn_id: int) -> None:
        try:
            killer = mysql.connector.connect(
                autocommit=True, connection_timeout=5, **self._cfg
            )
            killer.cursor().execute(f"KILL QUERY {conn_id}")
            killer.close()
        except Exception:  # noqa: BLE001
            LOGGER.debug("kill query failed", exc_info=True)

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
                    result.columns = [d[0] for d in cur.description]
                cur.close()
            except Exception as exc:  # noqa: BLE001 - any engine error recorded
                result.error = f"{type(exc).__name__}: {exc}"
                low = result.error.lower()
                result.is_internal_error = is_internal_error(
                    result.error
                ) or any(m.lower() in low for m in _CRASH_MARKERS)
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
                self._kill_conn_id(self._conn_id)
            except Exception:  # noqa: BLE001
                LOGGER.debug("tidb kill failed", exc_info=True)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
            return result
        finished.wait(timeout=1.0)
        if result.is_internal_error:
            try:
                self.connect()
            except Exception:  # noqa: BLE001
                LOGGER.debug("reconnect after crash failed", exc_info=True)
        return result

    def run_with_vars(
        self, sql: str, session_vars: dict[str, str], timeout_s: float = 10.0
    ) -> QueryResult:
        """Run ``sql`` after applying ``SET SESSION`` variables on this conn."""
        if self._conn is None or not self._alive():
            self.connect()
        assert self._conn is not None
        for var, val in session_vars.items():
            setter = self.run(f"SET SESSION {var} = {val}", timeout_s=timeout_s)
            if not setter.ok:
                return setter
        return self.run(sql, timeout_s=timeout_s)

    def explain_plan(self, sql: str) -> Any | None:
        """Return EXPLAIN output rows, or None on failure."""
        result = self.run(f"EXPLAIN {sql}", timeout_s=10.0)
        if not result.ok or not result.rows:
            return None
        return result.rows


class MySQLRunner(TiDBRunner):
    """Reference runner for a real MySQL/MariaDB server (same wire protocol)."""

    def connect(self) -> MySQLConnection:
        self.close()
        self._conn = mysql.connector.connect(
            autocommit=True, connection_timeout=10, **self._cfg
        )
        cur = self._conn.cursor()
        cur.execute(
            f"SET SESSION max_execution_time = {self.statement_timeout_ms}"
        )
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
        cur.execute(f"USE `{self.database}`")
        cur.execute("SELECT CONNECTION_ID()")
        self._conn_id = cur.fetchone()[0]
        cur.close()
        return self._conn
