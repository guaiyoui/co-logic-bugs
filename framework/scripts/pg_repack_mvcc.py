#!/usr/bin/env python3
"""Verify the documented non-MVCC-safe behavior of REPACK (CONCURRENTLY):
a REPEATABLE READ snapshot taken before the rewrite commits sees the
table as empty afterwards (mvcc-caveats)."""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from util.paths import pg_build_prefix  # noqa: E402
from targets.postgres_runner import PostgresRunner

INJ = "repack-concurrently-before-lock"
os.environ["COEVO_PG_EXTRA_OPTS"] = "-c wal_level=logical"
dd = tempfile.mkdtemp(prefix="pg_mvcc_")
pg = PostgresRunner(
    dd,
    pg_prefix=pg_build_prefix("pgmaster_inject"),
    statement_timeout_ms=30000,
)
pg.connect()
uri = pg._server.get_uri()
s1 = psycopg2.connect(uri)
s1.autocommit = True
s2 = psycopg2.connect(uri)
s2.autocommit = True
s3 = psycopg2.connect(uri)
s3.autocommit = False
c1, c2, c3 = s1.cursor(), s2.cursor(), s3.cursor()

c1.execute("CREATE EXTENSION IF NOT EXISTS injection_points")
c1.execute("CREATE TABLE mv(i int PRIMARY KEY, j int)")
c1.execute("INSERT INTO mv SELECT g, g FROM generate_series(1,50) g")

# RR snapshot BEFORE repack, without touching mv and without assigning
# an xid (a running xid would stall the repack decode worker's snapbuild)
c3.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
c3.execute("SELECT count(*) FROM pg_class")
c3.fetchall()

c1.execute("SELECT injection_points_set_local()")
c1.execute(f"SELECT injection_points_attach('{INJ}','wait')")
err = []


def rep():
    try:
        c1.execute("REPACK (CONCURRENTLY) mv")
    except Exception as e:  # noqa: BLE001
        err.append(str(e))


t = threading.Thread(target=rep)
t.start()
for _ in range(600):
    c2.execute(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE wait_event_type='InjectionPoint'"
    )
    if c2.fetchone()[0]:
        break
    if not t.is_alive():
        break
    time.sleep(0.1)
c2.execute("INSERT INTO mv VALUES (999, 999)")
deadline = time.monotonic() + 60
while t.is_alive() and time.monotonic() < deadline:
    try:
        c2.execute(f"SELECT injection_points_wakeup('{INJ}')")
    except psycopg2.Error:
        pass
    time.sleep(0.2)
t.join(30)
print("repack errors:", err)

c3.execute("SELECT count(*) FROM mv")
print("RR snapshot after repack sees:", c3.fetchall(),
      "(upstream docs: expect 0 — non-MVCC-safe rewrite)")
c3.execute("ROLLBACK")
c1.execute("SELECT count(*), sum(i) FROM mv")
print("fresh read:", c1.fetchall(), "(expect 51 rows incl. concurrent ins)")
fat = pg.log_fatal_lines(pg.log_new_lines())
print("fatal:", fat[:3])
s1.close(); s2.close(); s3.close()
pg.cleanup()
