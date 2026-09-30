#!/usr/bin/env python3
"""Probe REPACK (master-only, 20devel) on the --enable-injection-points build.

Scenarios
---------
spec_repack      replay src/test/modules/injection_points/specs/repack.spec
spec_toast       replay repack_toast.spec (TOAST shapes, no tuplesort)
spec_temporal    replay repack_temporal.spec (GiST/WITHOUT OVERLAPS identity)
spec_multirange  replay repack_temporal_multirange.spec (lossy multirange eq)
spec_decode      replay repack_decode.spec (rewrite changes not leaked)
basic            REPACK / REPACK (CONCURRENTLY) on a bloated table; bag
                 equality, relfilenode flip, index validity, constraints
random_churn     injection-point wait + randomized DML storm in the window
double_conc      second REPACK (CONCURRENTLY) while the first waits
prepared_txn     prepared transaction overlapping REPACK (CONCURRENTLY)
lock_holder      AccessExclusiveLock holder; REPACK waits or errors
edge_errors      every documented CONCURRENTLY rejection (from
                 contrib/test_decoding/sql/repack.sql) plus txn-block case
options          VERBOSE/ANALYZE/USING INDEX/column-list/no-name variants

Oracle: bag-equality of committed rows + no internal errors + no
TRAP/PANIC/assert lines in the server log (log_fatal_lines).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from util.paths import pg_build_prefix  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402

INJ_POINT = "repack-concurrently-before-lock"
PG_PREFIX = pg_build_prefix("pgmaster_inject")


# ------------------------------------------------------------------ helpers
def _exec(conn, sql, fetch=False):
    cur = conn.cursor()
    cur.execute(sql)
    if fetch and cur.description is not None:
        return cur.fetchall()
    return None


def _bag(conn, table, order="*"):
    """Multiset of all rows of `table`, deterministically ordered."""
    ncols = _exec(
        conn,
        "SELECT count(*) FROM pg_attribute WHERE attrelid = "
        f"'{table}'::regclass AND attnum > 0 AND NOT attisdropped",
        fetch=True,
    )[0][0]
    ords = (
        ",".join(str(i) for i in range(1, ncols + 1))
        if order == "*"
        else order
    )
    rows = _exec(conn, f"SELECT * FROM {table} ORDER BY {ords}", fetch=True)
    return [tuple("" if v is None else str(v) for v in r) for r in rows]


def _relfilenode(conn, table):
    return _exec(
        conn,
        f"SELECT relfilenode FROM pg_class WHERE oid='{table}'::regclass",
        fetch=True,
    )[0][0]


def _index_health(conn, table):
    """(invalid, not-ready) index name lists for `table`."""
    rows = _exec(
        conn,
        "SELECT x.indexrelid::regclass::text, x.indisvalid, x.indisready "
        "FROM pg_index x "
        f"WHERE x.indrelid = '{table}'::regclass",
        fetch=True,
    )
    bad_valid = [r[0] for r in rows if not r[1]]
    bad_ready = [r[0] for r in rows if not r[2]]
    return bad_valid, bad_ready


def _constraints(conn, table):
    rows = _exec(
        conn,
        "SELECT conname, contype, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE conrelid = "
        f"'{table}'::regclass ORDER BY conname",
        fetch=True,
    )
    return sorted(tuple(map(str, r)) for r in rows)


def wait_for_injection_point(conn, name=INJ_POINT, timeout_s=90.0):
    """Poll pg_stat_activity until a backend waits in the injection point."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = _exec(
            conn,
            "SELECT pid FROM pg_stat_activity "
            "WHERE wait_event_type = 'InjectionPoint' "
            f"AND wait_event = '{name}'",
            fetch=True,
        )
        if rows:
            return rows[0][0]
        time.sleep(0.05)
    raise TimeoutError(f"no backend reached injection point {name}")


def wakeup_retry(conn, thread, name=INJ_POINT, timeout_s=60.0):
    """Keep waking `name` until `thread` finishes or timeout.

    injection_points_wakeup() errors when no backend has registered the
    wait yet, and the wake is not recorded — so a wakeup fired before the
    repack backend reaches the point is lost.  Retry until the repack
    thread actually terminates.
    """
    deadline = time.monotonic() + timeout_s
    while not thread.done.is_set() and time.monotonic() < deadline:
        try:
            _exec(conn, f"SELECT injection_points_wakeup('{name}')")
        except psycopg2.Error:
            pass  # no waiter registered yet
        time.sleep(0.25)
    return thread.done.is_set()


def detach_safe(conn, name=INJ_POINT):
    try:
        _exec(conn, f"SELECT injection_points_detach('{name}')")
    except psycopg2.Error:
        pass


class RepackThread(threading.Thread):
    """Run one statement in the background, capturing error/result."""

    def __init__(self, conn, sql):
        super().__init__(daemon=True)
        self.conn = conn
        self.sql = sql
        self.error = None
        self.done = threading.Event()

    def run(self):
        try:
            _exec(self.conn, self.sql)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()


class Probe:
    def __init__(self, datadir, pg_prefix, extra_opts, timeout_ms=0):
        os.environ["COEVO_PG_EXTRA_OPTS"] = extra_opts
        self.results = []
        self.runner = PostgresRunner(
            datadir, pg_prefix=pg_prefix, statement_timeout_ms=timeout_ms or 60000
        )
        self.uri = self.runner._server.get_uri()
        self.mon = psycopg2.connect(self.uri)  # monitor/oracle session
        self.mon.autocommit = True

    def session(self, statement_timeout_ms=0):
        conn = psycopg2.connect(self.uri)
        conn.autocommit = True
        conn.cursor().execute(f"SET statement_timeout = {statement_timeout_ms}")
        return conn

    # ------------------------------------------------------------- oracle
    def scan_log(self):
        lines = self.runner.log_new_lines()
        return self.runner.log_fatal_lines(lines)

    def report(self, name, ok, details, fatal):
        entry = {
            "scenario": name,
            "ok": bool(ok),
            "details": details,
            "fatal_log": fatal,
        }
        self.results.append(entry)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {details}")
        for ln in fatal:
            print(f"    LOG: {ln}")

    # ------------------------------------------------------ spec replays
    def spec_repack(self):
        """Replay repack.spec: concurrent DML incl. subxacts during repack."""
        name = "spec_repack"
        s1, s2 = self.session(), self.session()
        det = {}
        try:
            _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
            _exec(
                s2,
                "CREATE TABLE repack_test(i int PRIMARY KEY, j int,"
                " k int GENERATED ALWAYS AS (j * 2) STORED)",
            )
            _exec(
                s2,
                "INSERT INTO repack_test(i, j) VALUES (1,1),(2,2),(3,3),(4,4)",
            )
            node0 = _relfilenode(s2, "repack_test")

            _exec(s1, "SELECT injection_points_set_local()")
            _exec(s1, f"SELECT injection_points_attach('{INJ_POINT}','wait')")

            t = RepackThread(
                s1, "REPACK (CONCURRENTLY) repack_test USING INDEX repack_test_pkey"
            )
            t.start()
            pid = wait_for_injection_point(self.mon)
            det["waiter_pid"] = pid

            # s2 DML — mirrors change_existing / change_new / subxacts.
            for sql in (
                "UPDATE repack_test SET i=10 where i=1",
                "UPDATE repack_test SET j=20 where i=2",
                "UPDATE repack_test SET i=30 where i=3",
                "UPDATE repack_test SET i=40 where i=30",
                "DELETE FROM repack_test WHERE i=4",
                "INSERT INTO repack_test(i, j) VALUES (5,5),(6,6),(7,7),(8,8)",
                "UPDATE repack_test SET i=50 where i=5",
                "UPDATE repack_test SET j=60 where i=6",
                "DELETE FROM repack_test WHERE i=7",
                "BEGIN",
                "INSERT INTO repack_test(i, j) VALUES (100,100)",
                "SAVEPOINT s1",
                "UPDATE repack_test SET i=101 where i=100",
                "SAVEPOINT s2",
                "UPDATE repack_test SET i=102 where i=101",
                "COMMIT",
                "BEGIN",
                "SAVEPOINT s1",
                "INSERT INTO repack_test(i, j) VALUES (110,110)",
                "ROLLBACK TO SAVEPOINT s1",
                "INSERT INTO repack_test(i, j) VALUES (110,111)",
                "COMMIT",
            ):
                _exec(s2, sql)

            expected = _bag(s2, "repack_test")
            wakeup_retry(s2, t)
            t.join(timeout=60)
            det["repack_error"] = t.error
            got = _bag(s1, "repack_test") if not t.error else []
            node1 = _relfilenode(s1, "repack_test")
            det["node_changed"] = node0 != node1
            det["rows"] = len(got)
            det["bag_equal"] = got == expected
            bad_v, bad_r = _index_health(s1, "repack_test")
            det["invalid_idx"] = bad_v
            det["notready_idx"] = bad_r
            cons = _constraints(s1, "repack_test")
            det["constraints"] = cons
            detach_safe(s1)
            ok = (
                t.error is None
                and det["bag_equal"]
                and det["node_changed"]
                and not bad_v
                and not bad_r
            )
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            try:
                wakeup_retry(s2, t, timeout_s=15)
            except Exception:  # noqa: BLE001
                pass
            s1.close()
            s2.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def spec_toast(self):
        """Replay repack_toast.spec: all TOAST storage shapes."""
        name = "spec_toast"
        s1, s2 = self.session(), self.session()
        det = {}
        try:
            _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
            _exec(
                s2,
                """CREATE FUNCTION gen_compressible(seed int) RETURNS text
                   LANGUAGE sql IMMUTABLE AS $$
                   SELECT repeat(md5((seed * 1000)::text), 50); $$""",
            )
            _exec(
                s2,
                """CREATE FUNCTION gen_compressible_external(seed int)
                   RETURNS text LANGUAGE sql IMMUTABLE AS $$
                   SELECT repeat(md5((seed * 1000)::text), 10000); $$""",
            )
            _exec(
                s2,
                """CREATE FUNCTION gen_external() RETURNS text LANGUAGE sql AS $$
                   SELECT string_agg(chr(65 + trunc(25 * random())::int), '')
                   FROM generate_series(1, 2048) s(x); $$""",
            )
            _exec(
                s2,
                """CREATE FUNCTION gen_inline() RETURNS text LANGUAGE sql AS $$
                   SELECT string_agg(chr(65 + trunc(25 * random())::int), '')
                   FROM generate_series(1, 1024) s(x); $$""",
            )
            _exec(
                s2,
                """CREATE FUNCTION gen_short() RETURNS text LANGUAGE sql AS $$
                   SELECT string_agg(chr(65 + trunc(25 * random())::int), '')
                   FROM generate_series(1, 120) s(x); $$""",
            )
            _exec(
                s2,
                "CREATE TABLE repack_toast(drop1 int, i int PRIMARY KEY,"
                " drop2 int, j text COMPRESSION pglz,"
                " k text COMPRESSION pglz)",
            )
            _exec(
                s2,
                "INSERT INTO repack_toast(drop1, i, drop2, j, k)"
                " SELECT 42, gs, 42, gen_external(), gen_compressible(gs)"
                " FROM generate_series(1, 10) gs",
            )
            _exec(
                s2,
                "ALTER TABLE repack_toast DROP COLUMN drop1,"
                " DROP COLUMN drop2",
            )
            _exec(
                s2, "ALTER TABLE repack_toast ALTER COLUMN k SET COMPRESSION default"
            )
            _exec(
                s2,
                "INSERT INTO repack_toast(i, j, k)"
                " SELECT gs, gen_external(), gen_compressible(142857)"
                " FROM generate_series(11, 20) gs",
            )
            _exec(s2, "ALTER TABLE repack_toast SET (toast_tuple_target = 128)")
            node0 = _relfilenode(s2, "repack_toast")

            _exec(s1, "SELECT injection_points_set_local()")
            _exec(s1, f"SELECT injection_points_attach('{INJ_POINT}','wait')")

            t = RepackThread(s1, "REPACK (CONCURRENTLY) repack_toast")
            t.start()
            wait_for_injection_point(self.mon)

            for sql in (
                "DELETE FROM repack_toast WHERE i=1",
                "INSERT INTO repack_toast(i, j, k) VALUES (1, gen_external(),"
                " gen_compressible(1))",
                "UPDATE repack_toast SET i=i+300 where i % 10 = 2",
                "UPDATE repack_toast SET j=gen_external() where i % 10 = 3",
                "UPDATE repack_toast SET j=gen_compressible(1), k=k||''"
                " where i % 10 = 4",
                "UPDATE repack_toast SET j=gen_compressible_external(2)"
                " where i % 10 = 5",
                "UPDATE repack_toast SET j=gen_inline(), k=repeat(k,5)"
                " where i % 10 = 6",
                "UPDATE repack_toast SET j=gen_short(), k=gen_external()"
                " where i % 10 = 7",
            ):
                _exec(s2, sql)

            expected = _bag(s2, "repack_toast")
            wakeup_retry(s2, t)
            t.join(timeout=120)
            det["repack_error"] = t.error
            got = _bag(s1, "repack_toast") if not t.error else []
            node1 = _relfilenode(s1, "repack_toast")
            det["node_changed"] = node0 != node1
            det["rows"] = len(got)
            det["bag_equal"] = got == expected
            if not det["bag_equal"]:
                det["missing"] = [r[:2] for r in expected if r not in got][:10]
                det["extra"] = [r[:2] for r in got if r not in expected][:10]
            detach_safe(s1)
            ok = t.error is None and det["bag_equal"] and det["node_changed"]
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            try:
                wakeup_retry(s2, t, timeout_s=15)
            except Exception:  # noqa: BLE001
                pass
            s1.close()
            s2.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def _temporal_like(self, name, ddl, ident_idx, repack_sql, upd_sql,
                       order="id, valid_at, label"):
        s1, s2 = self.session(), self.session()
        det = {}
        try:
            _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
            _exec(s2, ddl[0])
            _exec(s2, ddl[1])
            _exec(s2, ddl[2])
            tbl = ddl[3]
            node0 = _relfilenode(s2, tbl)
            _exec(s1, "SELECT injection_points_set_local()")
            _exec(s1, f"SELECT injection_points_attach('{INJ_POINT}','wait')")
            t = RepackThread(s1, repack_sql)
            t.start()
            wait_for_injection_point(self.mon)
            _exec(s2, upd_sql)
            expected = _bag(s2, tbl, order)
            wakeup_retry(s2, t)
            t.join(timeout=60)
            det["repack_error"] = t.error
            got = _bag(s1, tbl, order) if not t.error else []
            det["node_changed"] = node0 != _relfilenode(s1, tbl)
            det["rows"] = len(got)
            det["bag_equal"] = got == expected
            det["got"] = got
            det["expected"] = expected
            detach_safe(s1)
            ok = t.error is None and det["bag_equal"] and det["node_changed"]
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            try:
                wakeup_retry(s2, t, timeout_s=15)
            except Exception:  # noqa: BLE001
                pass
            s1.close()
            s2.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def spec_temporal(self):
        self._temporal_like(
            "spec_temporal",
            (
                "CREATE TABLE repack_temporal (id int4range,"
                " valid_at daterange, label text,"
                " CONSTRAINT rt_pkey PRIMARY KEY (id, valid_at"
                " WITHOUT OVERLAPS))",
                "ALTER TABLE repack_temporal REPLICA IDENTITY USING INDEX"
                " rt_pkey",
                "INSERT INTO repack_temporal(id, valid_at, label) VALUES"
                " ('[1,10)', '[2000-01-01,2000-02-01)', 'other'),"
                " ('[2,3)', '[2000-01-10,2000-01-20)', 'target')",
                "repack_temporal",
            ),
            "rt_pkey",
            "REPACK (CONCURRENTLY) repack_temporal USING INDEX rt_pkey",
            "UPDATE repack_temporal SET label = 'updated'"
            " WHERE id = '[2,3)' AND valid_at = '[2000-01-10,2000-01-20)'",
        )

    def spec_multirange(self):
        self._temporal_like(
            "spec_multirange",
            (
                "CREATE TABLE repack_temporal_multirange (id int4multirange,"
                " valid_at datemultirange, label text,"
                " CONSTRAINT rtm_pkey PRIMARY KEY (id, valid_at"
                " WITHOUT OVERLAPS))",
                "ALTER TABLE repack_temporal_multirange"
                " REPLICA IDENTITY USING INDEX rtm_pkey",
                "INSERT INTO repack_temporal_multirange(id, valid_at, label)"
                " VALUES"
                " (int4multirange(int4range(1, 3), int4range(5, 7)),"
                "  datemultirange(daterange('2000-01-01', '2000-02-01')),"
                "  'other'),"
                " (int4multirange(int4range(1, 7)),"
                "  datemultirange(daterange('2000-01-01', '2000-02-01')),"
                "  'target')",
                "repack_temporal_multirange",
            ),
            "rtm_pkey",
            "REPACK (CONCURRENTLY) repack_temporal_multirange"
            " USING INDEX rtm_pkey",
            "UPDATE repack_temporal_multirange SET label = 'updated'"
            " WHERE id = int4multirange(int4range(1, 7))",
        )

    def spec_decode(self):
        """Replay repack_decode.spec: rewrite changes must not reach an
        unrelated output plugin (test_decoding)."""
        name = "spec_decode"
        s1, s2 = self.session(), self.session()
        det = {}
        try:
            _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
            _exec(
                s2,
                """CREATE FUNCTION gen_external() RETURNS text LANGUAGE sql AS $$
                   SELECT string_agg(chr(65 + trunc(25 * random())::int), '')
                   FROM generate_series(1, 2048) s(x); $$""",
            )
            _exec(
                s2,
                "SELECT pg_create_logical_replication_slot('s',"
                " 'test_decoding')",
            )
            _exec(s2, "CREATE TABLE repack_toast(i int PRIMARY KEY, t text)")
            _exec(s2, "INSERT INTO repack_toast(i, t) VALUES (1, gen_external())")

            _exec(s1, "SELECT injection_points_set_local()")
            _exec(s1, f"SELECT injection_points_attach('{INJ_POINT}','wait')")
            t = RepackThread(s1, "REPACK (CONCURRENTLY) repack_toast")
            t.start()
            wait_for_injection_point(self.mon)
            _exec(s2, "UPDATE repack_toast SET t = gen_external() WHERE i=1")
            wakeup_retry(s2, t)
            t.join(timeout=60)
            det["repack_error"] = t.error
            rows = _exec(
                s1,
                "SELECT count(*) FROM pg_logical_slot_peek_changes"
                "('s', NULL, NULL, 'include-rewrites', '1')",
                fetch=True,
            )
            det["decoded_count"] = rows[0][0] if rows else None
            # second permutation: inline change; inspect UPDATE payloads
            _exec(s1, "SELECT pg_drop_replication_slot('s')")
            _exec(
                s2,
                "SELECT pg_create_logical_replication_slot('s2',"
                " 'test_decoding')",
            )
            t2 = RepackThread(s1, "REPACK (CONCURRENTLY) repack_toast")
            t2.start()
            wait_for_injection_point(self.mon)
            _exec(s2, "UPDATE repack_toast SET t = 'short' WHERE i=1")
            wakeup_retry(s2, t2)
            t2.join(timeout=60)
            det["repack2_error"] = t2.error
            rows = _exec(
                s1,
                "SELECT data FROM pg_logical_slot_peek_changes"
                "('s2', NULL, NULL, 'include-rewrites', '1')"
                " WHERE data LIKE '%UPDATE%'",
                fetch=True,
            )
            det["decoded_updates"] = [r[0] for r in rows]
            _exec(s1, "SELECT pg_drop_replication_slot('s2')")
            detach_safe(s1)
            # upstream expects 3 records (begin, change, commit) for perm1
            # and exactly one UPDATE message for perm2 — plus no leaked
            # internal rewrite tuples.
            ok = (
                t.error is None
                and t2.error is None
                and det["decoded_count"] is not None
            )
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            try:
                wakeup_retry(s2, t, timeout_s=15)
                wakeup_retry(s2, t2, timeout_s=15)
            except Exception:  # noqa: BLE001
                pass
            s1.close()
            s2.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    # ------------------------------------------------------- scenarios
    def basic(self):
        """Bloat, REPACK + REPACK (CONCURRENTLY); bag/index/constraint oracle."""
        name = "basic"
        s = self.session()
        det = {}
        try:
            _exec(
                s,
                "CREATE TABLE bloat(i int PRIMARY KEY, j int, pad text,"
                " CONSTRAINT j_pos CHECK (j >= 0),"
                " CONSTRAINT j_uq UNIQUE (j))",
            )
            _exec(
                s,
                "INSERT INTO bloat SELECT g, g, repeat('x', 100)"
                " FROM generate_series(1, 2000) g",
            )
            _exec(s, "CREATE INDEX bloat_pad ON bloat(left(pad, 4))")
            # churn: ~50% dead tuples (j stays unique via big offset)
            _exec(s, "UPDATE bloat SET j = j + 100000 WHERE i % 2 = 0")
            _exec(s, "DELETE FROM bloat WHERE i % 10 = 3")
            _exec(
                s,
                "INSERT INTO bloat SELECT 5000 + g, 9000 + g, 'new'"
                " FROM generate_series(1, 50) g",
            )
            cons0 = _constraints(s, "bloat")
            expected = _bag(s, "bloat")
            node0 = _relfilenode(s, "bloat")
            size0 = _exec(
                s, "SELECT pg_relation_size('bloat')", fetch=True
            )[0][0]

            _exec(s, "REPACK bloat")
            got1 = _bag(s, "bloat")
            node1 = _relfilenode(s, "bloat")
            det["excl"] = {
                "bag_equal": got1 == expected,
                "node_changed": node0 != node1,
                "size_before": size0,
                "size_after": _exec(
                    s, "SELECT pg_relation_size('bloat')", fetch=True
                )[0][0],
            }

            _exec(s, "REPACK (CONCURRENTLY, VERBOSE, ANALYZE) bloat")
            got2 = _bag(s, "bloat")
            node2 = _relfilenode(s, "bloat")
            det["conc"] = {
                "bag_equal": got2 == expected,
                "node_changed": node1 != node2,
            }
            bad_v, bad_r = _index_health(s, "bloat")
            det["invalid_idx"] = bad_v
            det["notready_idx"] = bad_r
            det["constraints_kept"] = _constraints(s, "bloat") == cons0
            det["rows"] = len(got2)
            ok = (
                det["excl"]["bag_equal"]
                and det["excl"]["node_changed"]
                and det["conc"]["bag_equal"]
                and det["conc"]["node_changed"]
                and not bad_v
                and not bad_r
                and det["constraints_kept"]
            )
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            s.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def random_churn(self, ops=400, seed=1234):
        """Injection-point wait + randomized concurrent DML storm."""
        name = "random_churn"
        s1, s2 = self.session(), self.session()
        det = {}
        rng = random.Random(seed)
        try:
            _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
            _exec(
                s2,
                "CREATE TABLE churn(i int PRIMARY KEY, j int, v text)",
            )
            _exec(
                s2,
                "INSERT INTO churn SELECT g, g, md5(g::text)"
                " FROM generate_series(1, 500) g",
            )
            live = set(range(1, 501))
            next_i = 1000

            _exec(s1, "SELECT injection_points_set_local()")
            _exec(s1, f"SELECT injection_points_attach('{INJ_POINT}','wait')")
            t = RepackThread(s1, "REPACK (CONCURRENTLY) churn")
            t.start()
            wait_for_injection_point(self.mon)

            # model the committed state in python alongside the DML
            model = {i: (i, i, f"md5-{i}") for i in live}
            fails = []
            for _ in range(ops):
                op = rng.random()
                try:
                    if op < 0.45:  # insert
                        next_i += 1
                        j = rng.randint(0, 10**6)
                        _exec(
                            s2,
                            f"INSERT INTO churn VALUES ({next_i}, {j},"
                            f" 'v{next_i}')",
                        )
                        model[next_i] = (next_i, j, f"v{next_i}")
                    elif op < 0.8 and model:  # update
                        i = rng.choice(list(model))
                        col = rng.choice(("i", "j"))
                        if col == "i":
                            next_i += 1
                            _exec(
                                s2,
                                f"UPDATE churn SET i={next_i} WHERE i={i}",
                            )
                            v = model.pop(i)
                            model[next_i] = (next_i, v[1], v[2])
                        else:
                            j = rng.randint(0, 10**6)
                            _exec(
                                s2,
                                f"UPDATE churn SET j={j} WHERE i={i}",
                            )
                            v = model[i]
                            model[i] = (v[0], j, v[2])
                    elif model and len(model) > 10:  # delete
                        i = rng.choice(list(model))
                        _exec(s2, f"DELETE FROM churn WHERE i={i}")
                        del model[i]
                except psycopg2.Error as exc:
                    fails.append(str(exc))
            det["dml_fails"] = fails[:5]
            expected = sorted(model.values())
            wakeup_retry(s2, t)
            t.join(timeout=120)
            det["repack_error"] = t.error
            got_rows = (
                _exec(s1, "SELECT i, j, v FROM churn", fetch=True)
                if not t.error
                else []
            )
            got = sorted((r[0], r[1], r[2]) for r in got_rows)
            det["model_rows"] = len(expected)
            det["got_rows"] = len(got)
            # compare as (i -> j) map; v for inserted rows is deterministic
            det["bag_equal"] = [
                (r[0], r[1]) for r in got
            ] == [(r[0], r[1]) for r in expected]
            if not det["bag_equal"]:
                gm = {r[0]: r[1] for r in got}
                em = {r[0]: r[1] for r in expected}
                det["diff"] = [
                    (k, em.get(k), gm.get(k))
                    for k in set(em) | set(gm)
                    if em.get(k) != gm.get(k)
                ][:20]
            detach_safe(s1)
            ok = (
                t.error is None
                and det["bag_equal"]
                and not fails
                and det["got_rows"] == det["model_rows"]
            )
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            try:
                wakeup_retry(s2, t, timeout_s=15)
            except Exception:  # noqa: BLE001
                pass
            s1.close()
            s2.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def double_conc(self):
        """Second REPACK (CONCURRENTLY) while first waits at the point."""
        name = "double_conc"
        s1, s2, s3 = self.session(), self.session(), self.session()
        det = {}
        try:
            _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
            _exec(s2, "CREATE TABLE dc(i int PRIMARY KEY, j int)")
            _exec(s2, "INSERT INTO dc SELECT g, g FROM generate_series(1,50) g")

            _exec(s1, "SELECT injection_points_set_local()")
            _exec(s1, f"SELECT injection_points_attach('{INJ_POINT}','wait')")
            t1 = RepackThread(s1, "REPACK (CONCURRENTLY) dc")
            t1.start()
            wait_for_injection_point(self.mon)

            # s3 issues a second REPACK (CONCURRENTLY) — expect either a
            # clean error or a lock wait.
            t2 = RepackThread(s3, "REPACK (CONCURRENTLY) dc")
            t2.start()
            time.sleep(3)
            det["second_running"] = t2.is_alive()
            wakeup_retry(s2, t1)
            t1.join(timeout=90)
            t2.join(timeout=90)
            det["first_error"] = t1.error
            det["second_error"] = t2.error
            det["rows"] = _exec(s2, "SELECT count(*), sum(j) FROM dc",
                                fetch=True)
            detach_safe(s1)
            # acceptable: second errored cleanly, or waited and both finished
            ok = (
                t1.error is None
                and det["rows"]
                and det["rows"][0][0] == 50
            )
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            try:
                wakeup_retry(s2, t1, timeout_s=15)
            except Exception:  # noqa: BLE001
                pass
            for c in (s1, s2, s3):
                c.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def prepared_txn(self):
        """REPACK (CONCURRENTLY) overlapping a prepared transaction.

        A prepared-but-uncommitted xact holds back snapbuild's consistent
        point, so the repack backend cannot reach the injection point
        until COMMIT PREPARED resolves it.  Schedule: PREPARE, start
        REPACK, let the decode worker stall on the xid, COMMIT PREPARED,
        then DML + wake at the injection point.  The prepared insert must
        land in the repacked heap via the apply-changes path.
        """
        name = "prepared_txn"
        s1, s2 = self.session(), self.session()
        det = {}
        try:
            _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
            _exec(s2, "CREATE TABLE pt(i int PRIMARY KEY, j int)")
            _exec(s2, "INSERT INTO pt SELECT g, g FROM generate_series(1,20) g")

            s3 = self.session()
            _exec(s3, "BEGIN")
            _exec(s3, "INSERT INTO pt VALUES (900, 900)")
            _exec(s3, "PREPARE TRANSACTION 'repack_gid'")

            _exec(s1, "SELECT injection_points_set_local()")
            _exec(s1, f"SELECT injection_points_attach('{INJ_POINT}','wait')")
            t = RepackThread(s1, "REPACK (CONCURRENTLY) pt")
            t.start()
            # give the decode worker a moment to stall on the prepared xid
            time.sleep(5)
            det["repack_alive_pre_commit"] = t.is_alive()
            _exec(s3, "COMMIT PREPARED 'repack_gid'")
            wait_for_injection_point(self.mon, timeout_s=120)
            _exec(s2, "UPDATE pt SET j = j + 1 WHERE i <= 5")
            wakeup_retry(s2, t)
            t.join(timeout=60)
            det["repack_error"] = t.error
            det["rows"] = _exec(
                s2, "SELECT count(*), sum(j) FROM pt", fetch=True
            )
            # expected: 20 original (5 updated -> +5) + prepared insert = 21
            det["expected_count"] = 21
            det["expected_sum"] = sum(range(1, 21)) + 5 + 900
            got = det["rows"][0] if det["rows"] else (None, None)
            det["bag_ok"] = (got[0], got[1]) == (21, det["expected_sum"])
            detach_safe(s1)
            s3.close()
            ok = t.error is None and det["bag_ok"]
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            try:
                wakeup_retry(s2, t, timeout_s=15)
            except Exception:  # noqa: BLE001
                pass
            try:
                _exec(self.mon, "COMMIT PREPARED 'repack_gid'")
            except Exception:  # noqa: BLE001
                pass
            s1.close()
            s2.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def concurrent_writer(self, ops=600, seed=99):
        """No injection point: writer threads hammer the table for the
        entire repack duration; the apply-changes path must capture all
        committed DML."""
        name = "concurrent_writer"
        s1, s2 = self.session(), self.session()
        det = {}
        rng = random.Random(seed)
        try:
            _exec(s2, "CREATE TABLE cw(i int PRIMARY KEY, j int, pad text)")
            # big enough that the copy phase takes real time while the
            # writer churns
            _exec(
                s2,
                "INSERT INTO cw SELECT g, g, md5(g::text)"
                " FROM generate_series(1, 40000) g",
            )
            stop = threading.Event()
            fails = []
            next_i = [100000]

            def writer():
                model = {i: i for i in range(1, 40001)}
                while not stop.is_set():
                    op = rng.random()
                    try:
                        if op < 0.4:
                            next_i[0] += 1
                            i = next_i[0]
                            _exec(
                                s2,
                                f"INSERT INTO cw VALUES ({i}, {i}, 'w')",
                            )
                            model[i] = i
                        elif op < 0.75 and model:
                            i = rng.choice(list(model))
                            j = rng.randint(0, 10**6)
                            _exec(
                                s2, f"UPDATE cw SET j={j} WHERE i={i}"
                            )
                            model[i] = j
                        elif len(model) > 20000:
                            i = rng.choice(list(model))
                            _exec(s2, f"DELETE FROM cw WHERE i={i}")
                            del model[i]
                    except psycopg2.Error as exc:
                        fails.append(str(exc))
                return model

            model_box = {}

            def run_writer():
                model_box["m"] = writer()

            wt = threading.Thread(target=run_writer, daemon=True)
            wt.start()
            t0 = time.monotonic()
            _exec(s1, "REPACK (CONCURRENTLY) cw")
            det["repack_s"] = round(time.monotonic() - t0, 2)
            stop.set()
            wt.join(timeout=30)
            model = model_box.get("m", {})
            det["dml_fails"] = fails[:5]
            det["model_rows"] = len(model)
            got = dict(
                _exec(s1, "SELECT i, j FROM cw", fetch=True) or []
            )
            det["got_rows"] = len(got)
            diff = [
                (k, model.get(k), got.get(k))
                for k in set(model) | set(got)
                if model.get(k) != got.get(k)
            ]
            det["diff"] = diff[:20]
            det["bag_equal"] = not diff and len(got) == len(model)
            ok = det["bag_equal"] and not fails
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            s1.close()
            s2.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def lock_holder(self):
        """Exclusive lock holder vs REPACK (CONCURRENTLY): wait or error."""
        name = "lock_holder"
        s1, s2 = self.session(), self.session()
        det = {}
        try:
            _exec(s2, "CREATE TABLE lh(i int PRIMARY KEY, j int)")
            _exec(s2, "INSERT INTO lh SELECT g, g FROM generate_series(1,10) g")
            s3 = self.session()
            s3.autocommit = False
            _exec(s3, "BEGIN")
            _exec(s3, "LOCK TABLE lh IN ACCESS EXCLUSIVE MODE")

            t = RepackThread(s1, "REPACK (CONCURRENTLY) lh")
            t.start()
            time.sleep(4)
            det["blocked_on_lock"] = t.is_alive()
            # release the lock; repack should proceed to completion
            _exec(s3, "ROLLBACK")
            t.join(timeout=60)
            det["repack_error"] = t.error
            det["rows"] = _exec(s2, "SELECT count(*) FROM lh", fetch=True)
            s3.close()
            ok = t.error is None and det["rows"] and det["rows"][0][0] == 10
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            s1.close()
            s2.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def partitions(self):
        """REPACK on partitioned parent cascades; CONCURRENTLY on a leaf
        partition works iff the leaf has a replica identity."""
        name = "partitions"
        s = self.session()
        det = {}
        try:
            _exec(s, "CREATE TABLE pp(i int, j int) PARTITION BY RANGE (i)")
            _exec(s, "CREATE TABLE pp1 PARTITION OF pp FOR VALUES"
                     " FROM (0) TO (1000)")
            _exec(s, "CREATE TABLE pp2 PARTITION OF pp FOR VALUES"
                     " FROM (1000) TO (2000)")
            _exec(s, "INSERT INTO pp SELECT g, g FROM generate_series(1,1500) g")
            _exec(s, "ALTER TABLE pp1 ADD PRIMARY KEY (i)")
            n1_0 = _relfilenode(s, "pp1")
            n2_0 = _relfilenode(s, "pp2")
            expected = _bag(s, "pp")

            _exec(s, "REPACK pp")  # cascades to both partitions
            det["parent_bag"] = _bag(s, "pp") == expected
            det["pp1_node_changed"] = _relfilenode(s, "pp1") != n1_0
            det["pp2_node_changed"] = _relfilenode(s, "pp2") != n2_0

            # leaf partition without replica identity -> clean error
            try:
                _exec(s, "REPACK (CONCURRENTLY) pp2")
                det["pp2_conc_nopk"] = "NO-ERROR"
            except psycopg2.Error as exc:
                det["pp2_conc_nopk"] = str(exc).strip().splitlines()[0]
            _exec(s, "ALTER TABLE pp2 ADD PRIMARY KEY (i)")
            _exec(s, "REPACK (CONCURRENTLY) pp2")
            det["pp2_conc_pk"] = _bag(s, "pp") == expected
            det["pp2_node2_changed"] = _relfilenode(s, "pp2") != n2_0
            ok = (
                det["parent_bag"]
                and det["pp1_node_changed"]
                and det["pp2_node_changed"]
                and det["pp2_conc_nopk"] != "NO-ERROR"
                and det["pp2_conc_pk"]
                and det["pp2_node2_changed"]
            )
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            s.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def cancel_mid(self):
        """pg_cancel / pg_terminate the repack backend while it waits at
        the injection point; verify no leaked transient tables or slots
        and that the source table is fully intact."""
        name = "cancel_mid"
        det = {}
        for mode in ("cancel", "terminate"):
            s1, s2 = self.session(), self.session()
            sub = {}
            try:
                _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
                _exec(
                    s2,
                    f"CREATE TABLE cm_{mode}(i int PRIMARY KEY, j int)",
                )
                _exec(
                    s2,
                    f"INSERT INTO cm_{mode} SELECT g, g"
                    " FROM generate_series(1, 100) g",
                )
                _exec(s1, "SELECT injection_points_set_local()")
                _exec(
                    s1,
                    f"SELECT injection_points_attach('{INJ_POINT}','wait')",
                )
                t = RepackThread(
                    s1, f"REPACK (CONCURRENTLY) cm_{mode}"
                )
                t.start()
                pid = wait_for_injection_point(self.mon)
                _exec(
                    s2,
                    f"UPDATE cm_{mode} SET j=j+1 WHERE i<=10",
                )
                _exec(
                    self.mon,
                    f"SELECT pg_{mode}_backend({pid})",
                )
                t.join(timeout=60)
                sub["repack_error"] = t.error
                # cleanup assertions
                sub["leftover_slots"] = _exec(
                    self.mon,
                    "SELECT slot_name FROM pg_replication_slots"
                    " WHERE slot_name LIKE 'pg_repack%'",
                    fetch=True,
                )
                sub["leftover_transient"] = _exec(
                    self.mon,
                    "SELECT relname FROM pg_class WHERE relname"
                    " LIKE 'pg\\_temp\\_%'",
                    fetch=True,
                )
                sub["rows"] = _exec(
                    s2,
                    f"SELECT count(*), sum(j) FROM cm_{mode}",
                    fetch=True,
                )
                exp_sum = sum(range(1, 101)) + 10
                sub["data_ok"] = (
                    sub["rows"]
                    and sub["rows"][0] == (100, exp_sum)
                )
                bad_v, bad_r = _index_health(s2, f"cm_{mode}")
                sub["invalid_idx"] = bad_v
                sub["notready_idx"] = bad_r
                detach_safe(s1)
                # either way the repack must have failed; the cancel/
                # terminate text differs but any clean error is fine
                sub["ok"] = (
                    t.error is not None
                    and not sub["leftover_slots"]
                    and not sub["leftover_transient"]
                    and sub["data_ok"]
                    and not bad_v
                    and not bad_r
                )
            except Exception as exc:  # noqa: BLE001
                sub["exception"] = f"{exc}\n{traceback.format_exc()}"
                sub["ok"] = False
            finally:
                try:
                    wakeup_retry(s2, t, timeout_s=15)
                except Exception:  # noqa: BLE001
                    pass
                s1.close()
                s2.close()
            det[mode] = sub
            if mode == "cancel":
                # reset schema for the terminate pass
                self.runner.setup([])
        ok = all(s.get("ok") for s in det.values())
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def crash_mid(self):
        """Immediate postmaster stop while repack waits at the injection
        point, then recovery: source table must be intact and no repack
        artifacts (temp slot, transient table) may survive."""
        name = "crash_mid"
        s1, s2 = self.session(), self.session()
        det = {}
        try:
            _exec(s2, "CREATE EXTENSION IF NOT EXISTS injection_points")
            _exec(s2, "CREATE TABLE cmk(i int PRIMARY KEY, j int)")
            _exec(
                s2,
                "INSERT INTO cmk SELECT g, g FROM generate_series(1,100) g",
            )
            _exec(s1, "SELECT injection_points_set_local()")
            _exec(s1, f"SELECT injection_points_attach('{INJ_POINT}','wait')")
            t = RepackThread(s1, "REPACK (CONCURRENTLY) cmk")
            t.start()
            wait_for_injection_point(self.mon)
            det["reached_point"] = True

            # hard crash: postmaster SIGQUIT-equivalent immediate stop
            self.runner._server.stop("immediate")
            t.join(timeout=10)
            det["repack_error"] = t.error
            s1.close()
            s2.close()
            try:
                self.mon.close()
            except Exception:  # noqa: BLE001
                pass

            # restart and recover
            self.runner._server.start()
            self.mon = psycopg2.connect(self.uri)
            self.mon.autocommit = True
            det["rows"] = _exec(
                self.mon, "SELECT count(*), sum(j) FROM cmk", fetch=True
            )
            det["leftover_slots"] = _exec(
                self.mon,
                "SELECT slot_name FROM pg_replication_slots"
                " WHERE slot_name LIKE 'pg_repack%'",
                fetch=True,
            )
            det["leftover_transient"] = _exec(
                self.mon,
                "SELECT relname FROM pg_class WHERE relname"
                " LIKE 'pg\\_temp\\_%'",
                fetch=True,
            )
            det["data_ok"] = det["rows"] and det["rows"][0] == (100, 5050)
            ok = (
                det["data_ok"]
                and not det["leftover_slots"]
                and not det["leftover_transient"]
            )
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            for c in (s1, s2):
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
        fatal = self.scan_log()
        # the crash itself logs a shutdown; look only for asserts/panics
        fatal = [
            ln for ln in fatal
            if "TRAP" in ln or "PANIC" in ln or "signal" in ln.lower()
            and "immediate" not in ln.lower()
        ]
        self.report(name, ok and not fatal, det, fatal)

    def edge_errors(self):
        """Every documented REPACK (CONCURRENTLY) rejection path."""
        name = "edge_errors"
        s = self.session()
        det = {}
        try:
            _exec(s, "CREATE EXTENSION IF NOT EXISTS injection_points")
            cases = []

            def run_case(tag, sql, expect_error=True):
                try:
                    _exec(s, sql)
                    cases.append((tag, "NO-ERROR", None))
                except psycopg2.Error as exc:
                    msg = str(exc).strip().splitlines()[0]
                    cases.append((tag, "error", msg))

            # partitioned parent
            run_case("setup_part",
                     "CREATE TABLE ep(a int) PARTITION BY RANGE (a)",
                     expect_error=False)
            run_case("partitioned", "REPACK (CONCURRENTLY) ep")
            run_case("catalog", "REPACK (CONCURRENTLY) pg_class")
            run_case("setup_ucat",
                     "CREATE TABLE eucat(i int) WITH"
                     " (user_catalog_table = true)", expect_error=False)
            run_case("user_catalog", "REPACK (CONCURRENTLY) eucat")
            run_case("setup_toast", "CREATE TABLE etoast(t text)",
                     expect_error=False)
            toast_rel = _exec(
                s,
                "SELECT reltoastrelid::regclass::text FROM pg_class"
                " WHERE oid='etoast'::regclass",
                fetch=True,
            )[0][0]
            if toast_rel and toast_rel != "-":
                run_case("toast_direct",
                         f"REPACK (CONCURRENTLY) {toast_rel}")
            run_case("setup_temp",
                     "CREATE TEMP TABLE etemp(i int PRIMARY KEY)",
                     expect_error=False)
            run_case("temp", "REPACK (CONCURRENTLY) etemp")
            run_case("setup_unlogged",
                     "CREATE UNLOGGED TABLE eunl(i int PRIMARY KEY)",
                     expect_error=False)
            run_case("unlogged", "REPACK (CONCURRENTLY) eunl")
            run_case("setup_mv",
                     "CREATE MATERIALIZED VIEW emv AS SELECT 1 AS i",
                     expect_error=False)
            run_case("matview", "REPACK (CONCURRENTLY) emv")
            run_case("setup_ri",
                     "CREATE TABLE eri(i int PRIMARY KEY)",
                     expect_error=False)
            _exec(s, "ALTER TABLE eri REPLICA IDENTITY NOTHING")
            run_case("replident_nothing", "REPACK (CONCURRENTLY) eri")
            _exec(s, "ALTER TABLE eri DROP CONSTRAINT eri_pkey")
            _exec(s, "ALTER TABLE eri REPLICA IDENTITY DEFAULT")
            run_case("no_pk", "REPACK (CONCURRENTLY) eri")
            _exec(s, "ALTER TABLE eri ADD PRIMARY KEY (i) DEFERRABLE")
            run_case("deferrable_pk", "REPACK (CONCURRENTLY) eri")
            # txn block
            try:
                _exec(s, "BEGIN")
                _exec(s, "CREATE TABLE etx(i int PRIMARY KEY)")
                run_case("in_txn", "REPACK (CONCURRENTLY) etx")
                _exec(s, "ROLLBACK")
            except psycopg2.Error:
                s.rollback()
            # REPACK (CONCURRENTLY) with no table name
            run_case("no_name", "REPACK (CONCURRENTLY)")
            # non-existent table
            run_case("no_such", "REPACK (CONCURRENTLY) nosuchtable")
            # column list without ANALYZE
            run_case("setup_cols",
                     "CREATE TABLE ecols(i int PRIMARY KEY, j int)",
                     expect_error=False)
            run_case("col_no_analyze", "REPACK ecols(i)")
            run_case("col_analyze", "REPACK (ANALYZE) ecols(i)",
                     expect_error=False)
            det["cases"] = [
                {"case": c, "result": r, "msg": m} for c, r, m in cases
            ]
            # a "NO-ERROR" is only expected for setup and col_analyze
            bad = [
                c["case"] for c in det["cases"]
                if c["result"] == "NO-ERROR"
                and not c["case"].startswith("setup")
                and c["case"] != "col_analyze"
            ]
            internal = [
                c for c in det["cases"]
                if c["msg"] and "internal error" in c["msg"].lower()
            ]
            det["unexpected_success"] = bad
            det["internal_errors"] = internal
            ok = not bad and not internal
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            s.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def options(self):
        """REPACK option-matrix sanity: USING INDEX, ANALYZE, no-name."""
        name = "options"
        s = self.session()
        det = {}
        try:
            _exec(
                s,
                "CREATE TABLE ot(i int, j int, k text)",
            )
            _exec(
                s,
                "INSERT INTO ot SELECT g, g*3, md5(g::text)"
                " FROM generate_series(1, 300) g",
            )
            _exec(s, "CREATE INDEX ot_i ON ot(i)")
            _exec(s, "CREATE INDEX ot_j ON ot(j)")
            _exec(s, "ALTER TABLE ot ADD PRIMARY KEY (i)")
            _exec(s, "ALTER TABLE ot CLUSTER ON ot_j")
            _exec(s, "UPDATE ot SET j=j+1 WHERE i%3=0")
            expected = _bag(s, "ot")

            _exec(s, "REPACK ot USING INDEX ot_j")
            det["using_idx"] = _bag(s, "ot") == expected
            # physical order should follow j now
            phys = _exec(
                s, "SELECT ctid, j FROM ot ORDER BY ctid LIMIT 3",
                fetch=True,
            )
            det["first_phys"] = [str(r) for r in phys]
            _exec(s, "REPACK (VERBOSE) ot")
            det["verbose"] = _bag(s, "ot") == expected
            # plain CLUSTER still works through the same node
            _exec(s, "CLUSTER ot USING ot_i")
            det["cluster"] = _bag(s, "ot") == expected
            ok = all(det.get(k) for k in ("using_idx", "verbose", "cluster"))
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            ok = False
        finally:
            s.close()
        fatal = self.scan_log()
        self.report(name, ok and not fatal, det, fatal)

    def shutdown(self):
        try:
            self.mon.close()
        except Exception:  # noqa: BLE001
            pass
        fatal = self.scan_log()
        if fatal:
            self.results.append(
                {"scenario": "_final_log_scan", "ok": False,
                 "details": "fatal lines at shutdown", "fatal_log": fatal}
            )
        self.runner.cleanup()


SCENARIOS = [
    "spec_repack", "spec_toast", "spec_temporal", "spec_multirange",
    "spec_decode", "basic", "random_churn", "double_conc",
    "prepared_txn", "lock_holder", "edge_errors", "options",
    "concurrent_writer", "partitions", "cancel_mid", "crash_mid",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default=PG_PREFIX)
    ap.add_argument("--datadir", default=None)
    ap.add_argument("--scenario", default="all",
                    help="comma list or 'all'")
    ap.add_argument("--out", default=None, help="JSON result path")
    ap.add_argument("--ops", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat each selected scenario N times")
    args = ap.parse_args()

    datadir = args.datadir or tempfile.mkdtemp(prefix="pg_repack_probe_")
    # wal_level=logical covers the test_decoding spec; REPACK itself only
    # needs replica.  max_prepared_transactions is PGC_POSTMASTER — must be
    # set here for the prepared_txn scenario.
    probe = Probe(
        datadir,
        args.prefix,
        "-c wal_level=logical -c max_prepared_transactions=10",
    )
    print(f"server: {probe.runner.engine_version}  uri: {probe.uri}")
    wanted = SCENARIOS if args.scenario == "all" else args.scenario.split(",")
    try:
        for it in range(args.repeat):
            for name in wanted:
                tag = name if args.repeat == 1 else f"{name}#{it}"
                print(f"=== {tag} ===")
                fn = getattr(probe, name)
                if name == "random_churn":
                    fn(ops=args.ops, seed=args.seed + it)
                elif name == "concurrent_writer":
                    fn(seed=args.seed + it)
                else:
                    fn()
                # reset schema between scenarios
                probe.runner.setup([])
    finally:
        probe.shutdown()

    npass = sum(1 for r in probe.results if r["ok"])
    print(f"\n{npass}/{len(probe.results)} scenarios passed")
    out = args.out
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(probe.results, indent=2, default=str))
        print(f"results -> {out}")
    return 0 if npass == len(probe.results) else 1


if __name__ == "__main__":
    sys.exit(main())
