"""PostgreSQL server backed by a self-compiled install prefix.

``pgserver`` (the pip package) embeds one fixed PostgreSQL build; testing a
version ladder or an assert-enabled ``--enable-debug`` build needs servers
launched from arbitrary install prefixes such as
``$COEVO_PGBLD/pg162_assert`` (containing ``bin/initdb``,
``bin/pg_ctl``, ``bin/postgres``).

``LocalPgServer`` exposes the duck-type contract ``PostgresRunner`` uses:
``get_uri()`` plus ``cleanup()``, and additionally ``start()``/``stop()``.
Connections go over a private unix-socket dir (no TCP listener); the port is
derived deterministically from the datadir so parallel ladders do not
collide.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import psycopg2

LOGGER = logging.getLogger(__name__)

# Deterministic port pool for socket-only servers. Well clear of the default
# 5432 and of pgserver's own ephemeral range, narrow enough to eyeball.
_PORT_BASE = 54330
_PORT_SPAN = 601  # 54330..54930 inclusive

_START_TIMEOUT_S = 60.0
_READY_TIMEOUT_S = 30.0


def port_for_datadir(datadir: str | Path) -> int:
    """Deterministic port in [_PORT_BASE, _PORT_BASE+_PORT_SPAN).

    Hashes the absolute datadir path so two ladders using the same leaf name
    under different output dirs still land on different ports, while the same
    datadir always reuses the same port across runs. Hashlib (not ``hash()``)
    keeps the value stable regardless of PYTHONHASHSEED.
    """
    key = str(Path(datadir).expanduser().absolute())
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return _PORT_BASE + int.from_bytes(digest[:2], "big") % _PORT_SPAN


def sockdir_for_datadir(datadir: str | Path) -> Path:
    """Private unix-socket dir under /tmp, named after the datadir leaf."""
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(datadir).name) or "pg"
    return Path(f"/tmp/pg_{tag}.sock_dir")


class LocalPgServer:
    """Run PostgreSQL from a self-compiled ``prefix`` against ``datadir``.

    Mirrors the parts of ``pgserver.get_server(...)``'s return value that
    runners rely on. Construction initializes the datadir on first use
    (``initdb -A trust``) and starts a socket-only postmaster.
    """

    def __init__(
        self,
        prefix: str | Path,
        datadir: str | Path,
        port: int | None = None,
        sockdir: str | Path | None = None,
    ):
        self.prefix = Path(prefix)
        self.datadir = Path(datadir)
        self.port = port if port is not None else port_for_datadir(self.datadir)
        self.sockdir = Path(sockdir) if sockdir else sockdir_for_datadir(
            self.datadir
        )
        self._bin = self.prefix / "bin"
        for tool in ("initdb", "pg_ctl", "postgres"):
            if not (self._bin / tool).exists():
                raise FileNotFoundError(
                    f"{self._bin / tool} missing — {self.prefix} is not a "
                    "PostgreSQL install prefix"
                )
        self.datadir.mkdir(parents=True, exist_ok=True)
        self.sockdir.mkdir(parents=True, exist_ok=True)
        # Only this user may drop sockets here; a world-writable dir would let
        # a local process squat on .s.PGSQL.<port> before postmaster starts.
        self.sockdir.chmod(0o700)
        self.start()

    # ------------------------------------------------------------ helpers
    def _run(self, args: list[str], **kw) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=120, **kw
        )

    def _pg_ctl(self, *args: str) -> subprocess.CompletedProcess:
        return self._run([str(self._bin / "pg_ctl"), "-D", str(self.datadir),
                          *args])

    def _ready(self) -> bool:
        try:
            conn = psycopg2.connect(self.get_uri())
        except psycopg2.Error:
            return False
        try:
            conn.autocommit = True
            conn.cursor().execute("SELECT 1")
            return True
        except psycopg2.Error:
            return False
        finally:
            conn.close()

    def _initdb(self) -> None:
        if (self.datadir / "PG_VERSION").exists():
            return
        LOGGER.info("initdb %s (prefix %s)", self.datadir, self.prefix)
        proc = self._run(
            [str(self._bin / "initdb"), "-D", str(self.datadir), "-A", "trust"]
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"initdb failed for {self.datadir}: {proc.stderr.strip()}"
            )

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        """initdb if needed, then start a socket-only postmaster."""
        if self._ready():
            return  # a postmaster is already serving this datadir
        self._initdb()
        opts = (
            f"-k {self.sockdir} -p {self.port} "
            "-c listen_addresses='' "
            "-c fsync=off -c autovacuum=off -c max_connections=20"
            # Extra postmaster -c options for runs that need a tuned
            # server (e.g. COEVO_PG_EXTRA_OPTS="-c
            # max_pred_locks_per_transaction=16" for SSI churn).
            + " " + os.environ.get("COEVO_PG_EXTRA_OPTS", "").strip()
        )
        proc = self._pg_ctl(
            "-w", "-t", str(int(_START_TIMEOUT_S)),
            "-l", str(self.datadir / "local_pg.log"),
            "-o", opts,
            "start",
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pg_ctl start failed for {self.datadir}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        # pg_ctl -w waits for its own readiness probe; still poll SELECT 1 so
        # callers recovering from an immediate-mode stop are safe.
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._ready():
                return
            time.sleep(0.2)
        raise RuntimeError(
            f"postmaster for {self.datadir} did not accept connections "
            f"within {_READY_TIMEOUT_S}s (see {self.datadir}/local_pg.log)"
        )

    def stop(self, mode: str = "fast") -> None:
        """Stop the postmaster (``-m fast``); no-op when not running."""
        proc = self._pg_ctl("-w", "-m", mode, "stop")
        if proc.returncode != 0:
            LOGGER.debug("pg_ctl stop for %s: %s", self.datadir,
                         proc.stderr.strip() or proc.stdout.strip())

    def cleanup(self) -> None:
        """pgserver-compatible teardown: stop the server, drop the sockdir."""
        self.stop()
        try:
            shutil.rmtree(self.sockdir, ignore_errors=True)
        except Exception:  # noqa: BLE001 - teardown must never raise
            LOGGER.debug("sockdir removal failed", exc_info=True)

    def get_uri(self, database: str | None = None) -> str:
        """psycopg2-compatible URI targeting the unix socket dir."""
        db = database or "postgres"
        return (
            f"postgresql:///{db}?host={self.sockdir}&port={self.port}"
        )
