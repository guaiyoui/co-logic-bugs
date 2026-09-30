"""Catalog/cache-invalidation stress oracle for PostgreSQL.

Historical PostgreSQL bug clusters (relcache/syscache invalidation,
stale cached plans, aborted-DDL catalog leaks) are unreachable for a
SELECT-only corpus over static schemas. This oracle interleaves DDL
with reads and prepared statements on deterministic data and checks
the two sound signals:

- an *internal-class* error (assert/PANIC/XX000, "cache lookup failed",
  planner bookkeeping failures, connection loss) on the probing
  session, or
- a bag divergence between the post-DDL read and a freshly planned
  baseline on the same catalog state.

Deadlock / serialization / statement-timeout errors under concurrency
are expected and are NOT hits. Internal errors raised on background
writer/builder sessions (patterns 4-5, where the spec bounds hits to
the reader/verifier side) are still real anomalies: they are written
to ``anomalies.json`` and counted in ``stats["anomalies"]``.

Probe patterns (each: setup -> interleave -> probe, ``--iters`` bounds
repetition):

1. ``prepare_ddl``       PREPARE p AS q($1); ALTER TABLE ...; EXECUTE p
                         x iters; compare each EXECUTE bag against the
                         same query text re-planned fresh post-DDL.
                         Sweeps ADD/DROP/RENAME COLUMN, SET DATA TYPE,
                         ADD CONSTRAINT, ATTACH/DETACH PARTITION, and a
                         failed DDL (invalidation must not fire).
2. ``prepare_ddl`` under ``plan_cache_mode = force_generic_plan`` and
   ``force_custom_plan`` (same scenario table, mode dimension).
3. ``partition_churn``   loop {ATTACH spare; SELECT counts; DETACH;
                         SELECT counts} on a range-partitioned parent.
4. ``concurrent_ddl``    writer thread loops ALTER/CREATE+DROP INDEX/
                         DROP+recreate TABLE on its own connection;
                         the main connection reads a fixed bag.
5. ``cic_dml``           CREATE INDEX CONCURRENTLY on a second session
                         while the main session inserts+selects; then
                         verify index-path bag == forced-seqscan bag.
6. ``savepoint_ddl``     BEGIN; SAVEPOINT; DDL; ROLLBACK TO SAVEPOINT;
                         SELECT — post-abort catalog consistency.
7. ``temp_shadow``       CREATE TEMP TABLE shadowing a permanent one;
                         reads must hit temp; DROP; permanent visible.

All probes are deterministic (fixed literals / generate_series only).
The runner picks up ``COEVO_PG_PREFIX`` for the build under test.

Usage:
    COEVO_PG_PREFIX=/path/to/prefix \
        python scripts/pg_ddl_stress.py --iters 20 --out results/pg_ddlstress_1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402

from oracles.normalize import (  # noqa: E402
    is_internal_error,
    loose_bag,
    normalize_rows,
)
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_ddl_stress")

# PG internal-class error markers beyond normalize.is_internal_error —
# identical to pg_error_scan._INTERNAL_MARKERS (kept as a copy: scripts
# is not an importable package, and the cursor oracle copies it too).
_INTERNAL_MARKERS = (
    "xx000",
    "assert",
    "panic",
    "unexpected",
    "cache lookup failed",
    "unrecognized node",
    "variable not found in subplan",
    "no relation entry",
    "cannot compare",
    "could not find pathkey",
    "server closed the connection",
    "terminating connection",
    "connection not open",
    "could not receive data from server",
    "internalerror",
)


def looks_internal(error: str | None) -> bool:
    """True when an error string indicates a DBMS-internal failure."""
    if not error:
        return False
    if is_internal_error(error):
        return True
    low = error.lower()
    return any(marker in low for marker in _INTERNAL_MARKERS)


def _is_internal(result) -> bool:
    return result.is_internal_error or looks_internal(result.error)


# Concurrent sessions legitimately fail with these under lock contention
# or statement_timeout; they are never hits (only tracked as stats).
_BENIGN_CONCURRENT_MARKERS = (
    "deadlock detected",
    "could not serialize",
    "canceling statement",
    "lock not available",
    "lock timeout",
    "tuple concurrently updated",
    "40p01",
    "40001",
    "55p03",
    "57014",
    "55p02",
)


def benign_concurrent(error: str | None) -> bool:
    """True for expected cross-session failures (deadlock/cancel/...)."""
    if not error:
        return False
    low = error.lower()
    return any(marker in low for marker in _BENIGN_CONCURRENT_MARKERS)


def _exec_conn(conn, sql: str):
    """Run ``sql`` on a raw psycopg2 connection.

    Returns ``(normalized_rows, None)`` or ``(None, error_str)``. Never
    raises; caller owns transaction state on error.
    """
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall() if cur.description is not None else []
        return normalize_rows(rows), None
    except Exception as exc:  # noqa: BLE001 - any engine error recorded
        return None, f"{type(exc).__name__}: {exc}"


def _second_conn(pg: PostgresRunner, timeout: float):
    """Open a sibling psycopg2 connection to the runner's server."""
    conn = psycopg2.connect(pg._server.get_uri())
    conn.autocommit = True
    conn.cursor().execute(
        f"SET statement_timeout = {int(timeout * 1000)}")
    return conn


# ---------------------------------------------------------------- patterns
# Pattern 1+2: PREPARE -> DDL -> EXECUTE under each plan_cache_mode.
#
# Each case: setup_sqls, a $1-parameterized query, its param type and
# arg literal, and a deterministic list of invalidating DDL statements
# (each scenario re-runs setup, so every DDL applies to a fresh schema).
PREPARE_MODES = [
    ("auto", None),
    ("generic", "force_generic_plan"),
    ("custom", "force_custom_plan"),
]

_PREPARE_CASES = [
    {
        "name": "cols",
        "setup": [
            "CREATE TABLE t(a INT, b INT)",
            "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
        ],
        "query": "SELECT a, b FROM t WHERE a >= $1",
        "ptype": "int",
        "arg": "2",
        "ddls": [
            "ALTER TABLE t ADD COLUMN c INT DEFAULT 7",
            "ALTER TABLE t ALTER COLUMN a TYPE BIGINT",
            "ALTER TABLE t ALTER COLUMN b SET NOT NULL",
            "ALTER TABLE t ADD CONSTRAINT t_a_pos CHECK (a > 0)",
            "ALTER TABLE t RENAME COLUMN a TO a2",
            "ALTER TABLE t DROP COLUMN b",
        ],
    },
    {
        "name": "star",
        "setup": [
            "CREATE TABLE t(a INT, b INT)",
            "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
        ],
        "query": "SELECT * FROM t WHERE a >= $1",
        "ptype": "int",
        "arg": "2",
        "ddls": [
            "ALTER TABLE t ADD COLUMN c INT",
            "ALTER TABLE t DROP COLUMN b",
            "ALTER TABLE t ALTER COLUMN a TYPE BIGINT",
        ],
    },
    {
        "name": "part",
        "setup": [
            "CREATE TABLE tp(a INT, b INT) PARTITION BY RANGE (a)",
            "CREATE TABLE tp_lo PARTITION OF tp "
            "FOR VALUES FROM (0) TO (100)",
            "CREATE TABLE tp_hi PARTITION OF tp "
            "FOR VALUES FROM (100) TO (200)",
            "INSERT INTO tp VALUES (10,1),(50,2),(150,3)",
            "CREATE TABLE tp_spare(a INT, b INT)",
            "INSERT INTO tp_spare VALUES (210,4),(250,5)",
        ],
        "query": "SELECT count(*)::int, COALESCE(sum(b),0)::int "
                 "FROM tp WHERE a >= $1",
        "ptype": "int",
        "arg": "0",
        "ddls": [
            "ALTER TABLE tp ATTACH PARTITION tp_spare "
            "FOR VALUES FROM (200) TO (300)",
            "ALTER TABLE tp DETACH PARTITION tp_lo",
            # failed DDL: tp_spare is not a partition yet — the error
            # path must not corrupt invalidation state.
            "ALTER TABLE tp DETACH PARTITION tp_spare",
            "ALTER TABLE tp ADD COLUMN c INT DEFAULT 1",
        ],
    },
    {
        "name": "join",
        "setup": [
            "CREATE TABLE t(a INT, b INT)",
            "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
            "CREATE TABLE u(a INT, w INT)",
            "INSERT INTO u VALUES (1,100),(2,200),(4,400)",
        ],
        "query": "SELECT t.a, u.w FROM t JOIN u ON t.a = u.a "
                 "WHERE t.a >= $1",
        "ptype": "int",
        "arg": "1",
        "ddls": [
            "ALTER TABLE u ADD COLUMN v INT",
            "ALTER TABLE u ALTER COLUMN w TYPE BIGINT",
            "ALTER TABLE t RENAME COLUMN b TO b2",
            "CREATE INDEX u_a_idx ON u(a)",
            "ALTER TABLE u DROP COLUMN w",
        ],
    },
]


def run_prepare_ddl(pg: PostgresRunner, args, hits: list[dict],
                    stats: dict) -> None:
    """Patterns 1+2: PREPARE/EXECUTE across cache-invalidating DDL."""
    for case in _PREPARE_CASES:
        q = case["query"]
        fresh_sql = q.replace("$1", case["arg"])
        exec_sql = f"EXECUTE coevo_p({case['arg']})"
        for ddl in case["ddls"]:
            for mode_label, mode_val in PREPARE_MODES:
                with eff.phase("prepare_ddl"):
                    stats["scenarios"] += 1
                    outcomes = pg.setup(case["setup"])
                    if any(err for _, err in outcomes):
                        stats["setup_fail"] += 1
                        continue
                    pg.run("DEALLOCATE ALL", timeout_s=5.0)
                    if mode_val:
                        pg.run(f"SET plan_cache_mode = {mode_val}",
                               timeout_s=5.0)
                    try:
                        prep = pg.run(
                            f"PREPARE coevo_p({case['ptype']}) AS {q}",
                            timeout_s=args.timeout)
                        if not prep.ok:
                            stats["prepare_fail"] += 1
                            if _is_internal(prep):
                                stats["hits"] += 1
                                hits.append({
                                    "pattern": "prepare_ddl",
                                    "case": case["name"],
                                    "mode": mode_label,
                                    "sql": prep_summary(case, ddl),
                                    "stage": "prepare",
                                    "error": prep.error,
                                })
                            continue
                        # Sanity: pre-DDL EXECUTE bag == fresh bag.
                        pre_exec = pg.run(exec_sql,
                                          timeout_s=args.timeout)
                        pre_fresh = pg.run(fresh_sql,
                                           timeout_s=args.timeout)
                        eff.count("prepare_ddl_execs", 2)
                        for tag, res in (("pre_exec", pre_exec),
                                         ("pre_fresh", pre_fresh)):
                            if _is_internal(res):
                                stats["hits"] += 1
                                hits.append({
                                    "pattern": "prepare_ddl",
                                    "case": case["name"],
                                    "mode": mode_label,
                                    "sql": exec_sql if tag == "pre_exec"
                                         else fresh_sql,
                                    "stage": tag, "error": res.error,
                                })
                        if _is_internal(pre_exec) or \
                                _is_internal(pre_fresh):
                            continue
                        if (pre_exec.ok and pre_fresh.ok
                                and loose_bag(pre_exec.rows)
                                != loose_bag(pre_fresh.rows)):
                            stats["hits"] += 1
                            hits.append({
                                "pattern": "prepare_ddl",
                                "case": case["name"], "mode": mode_label,
                                "sql": prep_summary(case, ddl),
                                "stage": "pre_ddl_baseline",
                                "exec_rows":
                                    [list(r) for r in pre_exec.rows][:20],
                                "fresh_rows":
                                    [list(r) for r in pre_fresh.rows][:20],
                            })
                        # The invalidating DDL itself may crash/error.
                        dres = pg.run(ddl, timeout_s=args.timeout)
                        eff.count("ddl_executions")
                        if _is_internal(dres):
                            stats["hits"] += 1
                            hits.append({
                                "pattern": "prepare_ddl",
                                "case": case["name"], "mode": mode_label,
                                "sql": ddl, "stage": "ddl",
                                "error": dres.error,
                            })
                            continue
                        # Post-DDL fresh baseline on the same state.
                        post_fresh = pg.run(fresh_sql,
                                            timeout_s=args.timeout)
                        eff.count("prepare_ddl_execs")
                        if _is_internal(post_fresh):
                            stats["hits"] += 1
                            hits.append({
                                "pattern": "prepare_ddl",
                                "case": case["name"], "mode": mode_label,
                                "sql": fresh_sql, "stage": "fresh_post",
                                "error": post_fresh.error,
                            })
                            continue
                        for i in range(args.iters):
                            ex = pg.run(exec_sql, timeout_s=args.timeout)
                            eff.count("prepare_ddl_execs")
                            stats["execs"] += 1
                            if ex.timed_out:
                                stats["timeouts"] += 1
                                break
                            if _is_internal(ex):
                                stats["hits"] += 1
                                hits.append({
                                    "pattern": "prepare_ddl",
                                    "case": case["name"],
                                    "mode": mode_label,
                                    "iteration": i, "sql": exec_sql,
                                    "stage": "execute",
                                    "error": ex.error,
                                    "ddl": ddl,
                                })
                                break
                            if not ex.ok:
                                # Clean EXECUTE error (e.g. "cached plan
                                # must not change result type") is fine.
                                stats["clean_errors"] += 1
                                break
                            if not post_fresh.ok:
                                # EXECUTE succeeds where the identical
                                # fresh query errors: stale cached plan.
                                stats["hits"] += 1
                                hits.append({
                                    "pattern": "prepare_ddl",
                                    "case": case["name"],
                                    "mode": mode_label,
                                    "iteration": i, "sql": exec_sql,
                                    "stage": "stale_plan_success",
                                    "exec_rows":
                                        [list(r) for r in ex.rows][:20],
                                    "fresh_error": post_fresh.error,
                                    "ddl": ddl,
                                })
                                break
                            if (loose_bag(ex.rows)
                                    != loose_bag(post_fresh.rows)):
                                stats["hits"] += 1
                                hits.append({
                                    "pattern": "prepare_ddl",
                                    "case": case["name"],
                                    "mode": mode_label,
                                    "iteration": i, "sql": exec_sql,
                                    "stage": "wrong_bag",
                                    "exec_rows":
                                        [list(r) for r in ex.rows][:20],
                                    "fresh_rows":
                                        [list(r)
                                         for r in post_fresh.rows][:20],
                                    "ddl": ddl,
                                })
                                break
                    finally:
                        pg.run("DEALLOCATE ALL", timeout_s=5.0)
                        if mode_val:
                            pg.run("RESET plan_cache_mode",
                                   timeout_s=5.0)


def prep_summary(case: dict, ddl: str) -> str:
    return (f"PREPARE coevo_p({case['ptype']}) AS {case['query']} "
            f"/* then: {ddl} */")


# ------------------------------------------------------- pattern 3
def run_partition_churn(pg: PostgresRunner, args, hits: list[dict],
                        stats: dict) -> None:
    """ATTACH/DETACH loop with count checks after each catalog change."""
    setup = [
        "CREATE TABLE pp(a INT, b INT) PARTITION BY RANGE (a)",
        "CREATE TABLE pp_lo PARTITION OF pp FOR VALUES FROM (0) TO (100)",
        "CREATE TABLE pp_hi PARTITION OF pp "
        "FOR VALUES FROM (100) TO (200)",
        "INSERT INTO pp SELECT i, i * 10 FROM generate_series(5, 195, 10) i",
        "CREATE TABLE pp_spare(a INT, b INT)",
        "INSERT INTO pp_spare VALUES (210, 1), (250, 2)",
    ]
    outcomes = pg.setup(setup)
    if any(err for _, err in outcomes):
        stats["setup_fail"] += 1
        return
    base = pg.run("SELECT count(*) FROM pp", timeout_s=args.timeout)
    spare = pg.run("SELECT count(*) FROM pp_spare",
                   timeout_s=args.timeout)
    if not base.ok or not spare.ok:
        stats["setup_fail"] += 1
        return
    detached_n = int(base.rows[0][0])
    attached_n = detached_n + int(spare.rows[0][0])
    attach = ("ALTER TABLE pp ATTACH PARTITION pp_spare "
              "FOR VALUES FROM (200) TO (300)")
    detach = "ALTER TABLE pp DETACH PARTITION pp_spare"
    for i in range(args.iters):
        with eff.phase("partition_churn"):
            stats["scenarios"] += 1
            steps = [
                (attach, None),
                ("SELECT count(*) FROM pp", attached_n),
                ("SELECT count(*) FROM pp WHERE a >= 200",
                 int(spare.rows[0][0])),
                (detach, None),
                ("SELECT count(*) FROM pp", detached_n),
                ("SELECT count(*) FROM pp_spare",
                 int(spare.rows[0][0])),
            ]
            for sql, expected in steps:
                res = pg.run(sql, timeout_s=args.timeout)
                eff.count("partition_churn_execs")
                stats["execs"] += 1
                if _is_internal(res):
                    stats["hits"] += 1
                    hits.append({
                        "pattern": "partition_churn", "iteration": i,
                        "sql": sql, "error": res.error,
                    })
                    break
                if not res.ok:
                    # Clean DDL error means state drifted (e.g. a prior
                    # DETACH silently failed) — not an internal error.
                    stats["clean_errors"] += 1
                    break
                if expected is not None:
                    got = int(res.rows[0][0]) if res.rows else -1
                    if got != expected:
                        stats["hits"] += 1
                        hits.append({
                            "pattern": "partition_churn", "iteration": i,
                            "sql": sql, "expected_count": expected,
                            "got_count": got,
                        })
                        break
            else:
                continue
            # Broke out of the step loop: re-stabilize for next iter.
            pg.run("ALTER TABLE pp DETACH PARTITION IF EXISTS pp_spare",
                   timeout_s=5.0)


# ------------------------------------------------------- pattern 4
# Writer ops keep the reader's bag invariant: cx comes and goes, b only
# changes type int<->bigint (loose_bag treats numerics as equal).
_WRITER_OPS = [
    "ALTER TABLE cr ADD COLUMN IF NOT EXISTS cx INT DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS cr_b_idx ON cr(b)",
    "ALTER TABLE cr DROP COLUMN IF EXISTS cx",
    "DROP INDEX IF EXISTS cr_b_idx",
    "DROP TABLE IF EXISTS cw",
    "CREATE TABLE cw(x INT, y INT)",
    "INSERT INTO cw VALUES (1, 2)",
    "ALTER TABLE cr ALTER COLUMN b TYPE BIGINT",
    "ALTER TABLE cr ALTER COLUMN b TYPE INT",
]
_READ_SQL = "SELECT a, b FROM cr WHERE a >= 25"


def run_concurrent_ddl(pg: PostgresRunner, args, hits: list[dict],
                       stats: dict, anomalies: list[dict]) -> None:
    """One writer thread churns DDL while the main connection reads."""
    setup = [
        "CREATE TABLE cr(a INT, b INT)",
        "INSERT INTO cr SELECT i, i * 10 FROM generate_series(1, 100) i",
    ]
    outcomes = pg.setup(setup)
    if any(err for _, err in outcomes):
        stats["setup_fail"] += 1
        return
    baseline = pg.run(_READ_SQL, timeout_s=args.timeout)
    if not baseline.ok:
        stats["setup_fail"] += 1
        return
    base_bag = loose_bag(baseline.rows)

    writer_errors: list[str] = []
    stop_flag = {"stop": False}
    try:
        wconn = _second_conn(pg, args.timeout)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("second connection failed: %s", exc)
        stats["setup_fail"] += 1
        return

    def _writer() -> None:
        try:
            for i in range(args.iters):
                if stop_flag["stop"]:
                    break
                sql = _WRITER_OPS[i % len(_WRITER_OPS)]
                _, err = _exec_conn(wconn, sql)
                if err is not None:
                    writer_errors.append(f"{sql} :: {err}")
                    eff.count("concurrent_ddl_writer_errors")
        finally:
            stop_flag["stop"] = True

    thread = threading.Thread(target=_writer, daemon=True)
    with eff.phase("concurrent_ddl"):
        stats["scenarios"] += 1
        thread.start()
        for i in range(args.iters):
            res = pg.run(_READ_SQL, timeout_s=args.timeout)
            eff.count("concurrent_ddl_reads")
            stats["execs"] += 1
            if res.timed_out:
                stats["timeouts"] += 1
                continue
            if _is_internal(res):
                stats["hits"] += 1
                hits.append({
                    "pattern": "concurrent_ddl", "side": "reader",
                    "iteration": i, "sql": _READ_SQL,
                    "error": res.error,
                })
                continue
            if not res.ok:
                if benign_concurrent(res.error):
                    stats["benign_concurrent"] += 1
                else:
                    stats["clean_errors"] += 1
                continue
            if loose_bag(res.rows) != base_bag:
                stats["hits"] += 1
                hits.append({
                    "pattern": "concurrent_ddl", "side": "reader",
                    "iteration": i, "sql": _READ_SQL,
                    "expected_rows":
                        [list(r) for r in baseline.rows][:20],
                    "got_rows": [list(r) for r in res.rows][:20],
                })
        thread.join(timeout=max(30.0, args.timeout * args.iters))
        if thread.is_alive():
            stats["anomalies"] += 1
            anomalies.append({
                "pattern": "concurrent_ddl", "stage": "writer_join",
                "detail": "writer thread did not finish; cancelling",
            })
            try:
                wconn.cancel()
            except Exception:  # noqa: BLE001
                pass
            thread.join(timeout=5.0)
    for entry in writer_errors:
        sql, _, err = entry.partition(" :: ")
        if looks_internal(err):
            stats["anomalies"] += 1
            anomalies.append({
                "pattern": "concurrent_ddl", "side": "writer",
                "sql": sql, "error": err,
            })
            LOGGER.info("WRITER INTERNAL (anomaly): %s", err)
        elif benign_concurrent(err):
            stats["benign_concurrent"] += 1
        else:
            stats["clean_errors"] += 1
    # Post-churn catalog consistency: the reader bag must be unchanged.
    post = pg.run(_READ_SQL, timeout_s=args.timeout)
    if _is_internal(post):
        stats["hits"] += 1
        hits.append({
            "pattern": "concurrent_ddl", "side": "reader",
            "sql": _READ_SQL, "stage": "post_churn",
            "error": post.error,
        })
    elif post.ok and loose_bag(post.rows) != base_bag:
        stats["hits"] += 1
        hits.append({
            "pattern": "concurrent_ddl", "side": "reader",
            "sql": _READ_SQL, "stage": "post_churn",
            "expected_rows": [list(r) for r in baseline.rows][:20],
            "got_rows": [list(r) for r in post.rows][:20],
        })
    try:
        wconn.close()
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------- pattern 5
def run_cic_dml(pg: PostgresRunner, args, hits: list[dict],
                stats: dict, anomalies: list[dict]) -> None:
    """CREATE INDEX CONCURRENTLY under concurrent inserts, then verify.

    After the build, the index-served bag must equal the forced-seqscan
    bag for probes covering both pre-existing and mid-build rows.
    """
    setup = [
        "CREATE TABLE ci(a INT PRIMARY KEY, b INT)",
        "INSERT INTO ci SELECT i, i * 7 FROM generate_series(1, 500) i",
    ]
    outcomes = pg.setup(setup)
    if any(err for _, err in outcomes):
        stats["setup_fail"] += 1
        return
    try:
        bconn = _second_conn(pg, args.timeout)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("second connection failed: %s", exc)
        stats["setup_fail"] += 1
        return

    build_err: list[str] = []

    def _builder() -> None:
        _, err = _exec_conn(
            bconn, "CREATE INDEX CONCURRENTLY ci_b_idx ON ci(b)")
        if err is not None:
            build_err.append(err)

    thread = threading.Thread(target=_builder, daemon=True)
    with eff.phase("cic_dml"):
        stats["scenarios"] += 1
        thread.start()
        inserted = 0
        for i in range(args.iters):
            ins = pg.run(
                f"INSERT INTO ci VALUES ({1000 + i}, {(1000 + i) * 7})",
                timeout_s=args.timeout)
            eff.count("cic_dml_execs")
            stats["execs"] += 1
            if _is_internal(ins):
                stats["hits"] += 1
                hits.append({
                    "pattern": "cic_dml", "side": "inserter",
                    "iteration": i, "sql": "INSERT INTO ci ...",
                    "error": ins.error,
                })
                break
            if not ins.ok:
                if benign_concurrent(ins.error):
                    stats["benign_concurrent"] += 1
                else:
                    stats["clean_errors"] += 1
                continue
            inserted += 1
            # Read-your-writes under a concurrent index build.
            chk = pg.run(
                "SELECT count(*) FROM ci WHERE a >= 1000",
                timeout_s=args.timeout)
            stats["execs"] += 1
            if _is_internal(chk):
                stats["hits"] += 1
                hits.append({
                    "pattern": "cic_dml", "side": "inserter",
                    "iteration": i,
                    "sql": "SELECT count(*) FROM ci WHERE a >= 1000",
                    "error": chk.error,
                })
                break
            if chk.ok and int(chk.rows[0][0]) != inserted:
                stats["hits"] += 1
                hits.append({
                    "pattern": "cic_dml", "side": "inserter",
                    "iteration": i,
                    "sql": "SELECT count(*) FROM ci WHERE a >= 1000",
                    "expected_count": inserted,
                    "got_count": int(chk.rows[0][0]),
                })
                break
        thread.join(timeout=max(30.0, args.timeout * 4))
        if thread.is_alive():
            stats["anomalies"] += 1
            anomalies.append({
                "pattern": "cic_dml", "stage": "builder_join",
                "detail": "CREATE INDEX CONCURRENTLY did not finish",
            })
            try:
                bconn.cancel()
            except Exception:  # noqa: BLE001
                pass
            thread.join(timeout=5.0)

    for err in build_err:
        if looks_internal(err):
            stats["anomalies"] += 1
            anomalies.append({
                "pattern": "cic_dml", "side": "builder",
                "sql": "CREATE INDEX CONCURRENTLY ci_b_idx ON ci(b)",
                "error": err,
            })
            LOGGER.info("CIC INTERNAL (anomaly): %s", err)
        elif benign_concurrent(err):
            stats["benign_concurrent"] += 1
        else:
            stats["clean_errors"] += 1

    # Verify only a VALID built index; a cleanly failed/invalid CIC is
    # not a bug (invalid indexes are dropped, not compared).
    valid = pg.run(
        "SELECT i.indisvalid FROM pg_class c JOIN pg_index i "
        "ON c.oid = i.indexrelid WHERE c.relname = 'ci_b_idx'",
        timeout_s=args.timeout)
    idx_valid = bool(valid.ok and valid.rows
                     and str(valid.rows[0][0]) in ("True", "t", "true"))
    if not idx_valid:
        stats["cic_invalid_or_missing"] += 1
        pg.run("DROP INDEX IF EXISTS ci_b_idx", timeout_s=5.0)
    else:
        last_b = (1000 + max(inserted - 1, 0)) * 7
        probes = [350, last_b]  # pre-existing row, mid-build row
        seq_pre = ["SET enable_indexscan = off",
                   "SET enable_bitmapscan = off",
                   "SET enable_indexonlyscan = off"]
        idx_pre = ["SET enable_seqscan = off"]
        for pv in probes:
            q = f"SELECT a FROM ci WHERE b = {pv}"
            for stmt in seq_pre:
                pg.run(stmt, timeout_s=5.0)
            seq = pg.run(q, timeout_s=args.timeout)
            pg.run("RESET enable_indexscan", timeout_s=5.0)
            pg.run("RESET enable_bitmapscan", timeout_s=5.0)
            pg.run("RESET enable_indexonlyscan", timeout_s=5.0)
            for stmt in idx_pre:
                pg.run(stmt, timeout_s=5.0)
            idx = pg.run(q, timeout_s=args.timeout)
            pg.run("RESET enable_seqscan", timeout_s=5.0)
            eff.count("cic_dml_execs", 2)
            stats["execs"] += 2
            for tag, res in (("seqscan", seq), ("indexpath", idx)):
                if _is_internal(res):
                    stats["hits"] += 1
                    hits.append({
                        "pattern": "cic_dml", "side": "verifier",
                        "sql": q, "stage": tag, "error": res.error,
                    })
            if seq.ok and idx.ok and \
                    loose_bag(seq.rows) != loose_bag(idx.rows):
                stats["hits"] += 1
                hits.append({
                    "pattern": "cic_dml", "side": "verifier",
                    "sql": q, "stage": "index_vs_seqscan",
                    "seqscan_rows": [list(r) for r in seq.rows][:20],
                    "index_rows": [list(r) for r in idx.rows][:20],
                })
    try:
        bconn.close()
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------- pattern 6
_SAVEPOINT_OPS = [
    ("addcol", ["ALTER TABLE sp ADD COLUMN sx INT DEFAULT 9"], None),
    ("dropcol", ["ALTER TABLE sp DROP COLUMN b"], None),
    ("create_index", ["CREATE INDEX sp_b_i ON sp(b)"], None),
    ("rename", ["ALTER TABLE sp RENAME COLUMN a TO ar"], None),
    # In-transaction check: TRUNCATE must already show 0 rows.
    ("truncate", ["TRUNCATE sp"], ("SELECT count(*) FROM sp", 0)),
]


def run_savepoint_ddl(pg: PostgresRunner, args, hits: list[dict],
                      stats: dict) -> None:
    """DDL inside a rolled-back savepoint must leave no catalog trace."""
    setup = [
        "CREATE TABLE sp(a INT, b INT)",
        "INSERT INTO sp VALUES (1,10),(2,20),(3,30)",
    ]
    outcomes = pg.setup(setup)
    if any(err for _, err in outcomes):
        stats["setup_fail"] += 1
        return
    conn = pg._conn or pg.connect()
    _, err = _exec_conn(conn, "SELECT a, b FROM sp")
    base_rows, _ = _exec_conn(conn, "SELECT a, b FROM sp")
    base_bag = loose_bag(base_rows or [])
    for i in range(args.iters):
        op_name, stmts, intx = _SAVEPOINT_OPS[i % len(_SAVEPOINT_OPS)]
        with eff.phase("savepoint_ddl"):
            stats["scenarios"] += 1
            hit_rec: dict | None = None
            _, err = _exec_conn(conn, "BEGIN")
            if err is None:
                _, err = _exec_conn(conn, "SAVEPOINT coevo_s")
            if err is None:
                for sql in stmts:
                    _, err = _exec_conn(conn, sql)
                    eff.count("savepoint_ddl_execs")
                    stats["execs"] += 1
                    if err is not None:
                        break
            if err is None and intx is not None:
                rows, err = _exec_conn(conn, intx[0])
                if err is None and int(rows[0][0]) != intx[1]:
                    hit_rec = {
                        "pattern": "savepoint_ddl", "iteration": i,
                        "sql": intx[0], "op": op_name,
                        "stage": "in_tx_check",
                        "expected_count": intx[1],
                        "got_count": int(rows[0][0]),
                    }
            if err is not None:
                if looks_internal(err):
                    hit_rec = {
                        "pattern": "savepoint_ddl", "iteration": i,
                        "sql": stmts[-1], "op": op_name,
                        "stage": "ddl_in_tx", "error": err,
                    }
                else:
                    stats["clean_errors"] += 1
            # Roll the savepoint back (or the whole tx on error paths —
            # ROLLBACK TO SAVEPOINT also clears the aborted state).
            _, rerr = _exec_conn(conn, "ROLLBACK TO SAVEPOINT coevo_s")
            if rerr is not None:
                _exec_conn(conn, "ROLLBACK")
            if hit_rec is not None:
                stats["hits"] += 1
                hits.append(hit_rec)
                _exec_conn(conn, "ROLLBACK")
                if not pg._alive():
                    try:
                        conn = pg.connect()
                    except Exception:  # noqa: BLE001
                        pass
                continue
            # Post-abort checks: bag restored, phantom column gone.
            rows, perr = _exec_conn(conn, "SELECT a, b FROM sp")
            stats["execs"] += 1
            if perr is not None and looks_internal(perr):
                stats["hits"] += 1
                hits.append({
                    "pattern": "savepoint_ddl", "iteration": i,
                    "sql": "SELECT a, b FROM sp", "op": op_name,
                    "stage": "post_rollback", "error": perr,
                })
            elif perr is None and loose_bag(rows) != base_bag:
                stats["hits"] += 1
                hits.append({
                    "pattern": "savepoint_ddl", "iteration": i,
                    "sql": "SELECT a, b FROM sp", "op": op_name,
                    "stage": "post_rollback",
                    "expected_rows": [list(r) for r in base_rows][:20],
                    "got_rows": [list(r) for r in rows][:20],
                })
            # sx must not leak out of the aborted subtransaction.
            leak_rows, lerr = _exec_conn(conn, "SELECT sx FROM sp")
            if lerr is not None and looks_internal(lerr):
                stats["hits"] += 1
                hits.append({
                    "pattern": "savepoint_ddl", "iteration": i,
                    "sql": "SELECT sx FROM sp", "op": op_name,
                    "stage": "catalog_leak_probe", "error": lerr,
                })
            elif lerr is None:
                stats["hits"] += 1
                hits.append({
                    "pattern": "savepoint_ddl", "iteration": i,
                    "sql": "SELECT sx FROM sp", "op": op_name,
                    "stage": "catalog_leak",
                    "got_rows": [list(r) for r in leak_rows][:20],
                })
            _exec_conn(conn, "COMMIT")
    # If the connection died mid-pattern, leave it usable.
    if not pg._alive():
        try:
            pg.connect()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------- pattern 7
def run_temp_shadow(pg: PostgresRunner, args, hits: list[dict],
                    stats: dict) -> None:
    """TEMP table shadows permanent; DROP restores the permanent one."""
    setup = [
        "CREATE TABLE sh(a INT, b INT)",
        "INSERT INTO sh VALUES (1,10),(2,20)",
    ]
    outcomes = pg.setup(setup)
    if any(err for _, err in outcomes):
        stats["setup_fail"] += 1
        return
    perm = pg.run("SELECT a, b FROM public.sh", timeout_s=args.timeout)
    if not perm.ok:
        stats["setup_fail"] += 1
        return
    perm_bag = loose_bag(perm.rows)
    temp_bag = loose_bag([[999, 1]])
    for i in range(args.iters):
        with eff.phase("temp_shadow"):
            stats["scenarios"] += 1
            steps = [
                ("CREATE TEMP TABLE sh(a INT, b INT)", None, None),
                ("INSERT INTO sh VALUES (999, 1)", None, None),
                ("SELECT a, b FROM sh", temp_bag, "temp_visible"),
                ("SELECT a, b FROM pg_temp.sh", temp_bag, "temp_qual"),
                ("SELECT a, b FROM public.sh", perm_bag, "perm_qual"),
                ("DROP TABLE sh", None, None),  # drops the TEMP one
                ("SELECT a, b FROM sh", perm_bag, "perm_restored"),
            ]
            for sql, expected, stage in steps:
                res = pg.run(sql, timeout_s=args.timeout)
                eff.count("temp_shadow_execs")
                stats["execs"] += 1
                if _is_internal(res):
                    stats["hits"] += 1
                    hits.append({
                        "pattern": "temp_shadow", "iteration": i,
                        "sql": sql, "stage": stage or "ddl",
                        "error": res.error,
                    })
                    break
                if not res.ok:
                    stats["clean_errors"] += 1
                    break
                if expected is not None \
                        and loose_bag(res.rows) != expected:
                    stats["hits"] += 1
                    hits.append({
                        "pattern": "temp_shadow", "iteration": i,
                        "sql": sql, "stage": stage,
                        "got_rows": [list(r) for r in res.rows][:20],
                    })
                    break
            # Never let a leftover temp table shadow later iterations.
            res = pg.run("SELECT a, b FROM pg_temp.sh",
                         timeout_s=5.0)
            if res.ok:
                pg.run("DROP TABLE pg_temp.sh", timeout_s=5.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_ddl")
    ap.add_argument("--out", default="results/pg_ddlstress_1")
    ap.add_argument("--iters", type=int, default=20,
                    help="repetitions per probe pattern")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="per-statement timeout in seconds")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    pg = PostgresRunner(args.pg_datadir,
                        statement_timeout_ms=int(args.timeout * 1000))
    hits: list[dict] = []
    anomalies: list[dict] = []
    stats = {
        "scenarios": 0, "execs": 0, "hits": 0, "anomalies": 0,
        "clean_errors": 0, "benign_concurrent": 0, "timeouts": 0,
        "setup_fail": 0, "prepare_fail": 0, "cic_invalid_or_missing": 0,
    }
    t0 = time.time()
    try:
        LOGGER.info("pg version: %s", pg.engine_version)
        patterns = [
            ("prepare_ddl", lambda: run_prepare_ddl(pg, args, hits, stats)),
            ("partition_churn",
             lambda: run_partition_churn(pg, args, hits, stats)),
            ("concurrent_ddl",
             lambda: run_concurrent_ddl(pg, args, hits, stats, anomalies)),
            ("cic_dml",
             lambda: run_cic_dml(pg, args, hits, stats, anomalies)),
            ("savepoint_ddl",
             lambda: run_savepoint_ddl(pg, args, hits, stats)),
            ("temp_shadow", lambda: run_temp_shadow(pg, args, hits, stats)),
        ]
        for name, fn in patterns:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - isolate patterns
                stats["anomalies"] += 1
                anomalies.append({
                    "pattern": name, "stage": "harness",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                LOGGER.exception("pattern %s crashed", name)
                if not pg._alive():
                    try:
                        pg.connect()
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        pg.cleanup()
    with open(os.path.join(args.out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1)
    with open(os.path.join(args.out, "anomalies.json"), "w") as fh:
        json.dump(anomalies, fh, indent=1)
    summary = {**stats, "iters": args.iters,
               "pg_version": getattr(pg, "_version", "unknown"),
               "elapsed_s": time.time() - t0,
               "efficiency": eff.snapshot()}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
