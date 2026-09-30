"""Tests for LocalPgServer, the self-compiled-prefix PostgreSQL launcher.

Port/sockdir derivation and prefix validation run without any build. The
launch test needs a real install prefix; it is skipped unless one exists at
$COEVO_PG_TEST_PREFIX or the default pgbld location.
"""

import os

from pathlib import Path

from util.paths import pg_build_prefix

import pytest

from targets.pg_local_server import (
    LocalPgServer,
    port_for_datadir,
    sockdir_for_datadir,
)

_DEFAULT_PREFIX = Path(pg_build_prefix("pg162_assert"))


def _usable_prefix() -> Path | None:
    prefix = Path(os.environ.get("COEVO_PG_TEST_PREFIX", _DEFAULT_PREFIX))
    return prefix if (prefix / "bin" / "postgres").exists() else None


def test_port_is_deterministic_and_in_range():
    a = port_for_datadir("/tmp/ladder/data_pg162")
    assert a == port_for_datadir("/tmp/ladder/data_pg162")
    assert 54330 <= a <= 54930
    # different datadirs almost surely land on different ports
    assert a != port_for_datadir("/tmp/ladder/data_pg171")


def test_sockdir_named_after_datadir():
    assert sockdir_for_datadir("/x/data_pg162") == Path(
        "/tmp/pg_data_pg162.sock_dir")
    assert sockdir_for_datadir("/x/a b") == Path("/tmp/pg_a_b.sock_dir")


def test_missing_prefix_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalPgServer(tmp_path / "no_such_prefix", tmp_path / "data")


def test_live_server_roundtrip(tmp_path):
    prefix = _usable_prefix()
    if prefix is None:
        pytest.skip("no built PostgreSQL prefix available")
    import psycopg2

    server = LocalPgServer(prefix, tmp_path / "data")
    try:
        conn = psycopg2.connect(server.get_uri())
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
        cur.execute("SHOW server_version")
        assert cur.fetchone()[0]
        conn.close()
    finally:
        server.cleanup()
    proc = server._pg_ctl("status")
    assert proc.returncode != 0  # postmaster no longer running
