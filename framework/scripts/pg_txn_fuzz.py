"""Interleaved multi-session transaction fuzzer for PostgreSQL.

Property-based transaction interleaving against one embedded server
(``PostgresRunner`` datadir; ``COEVO_PG_PREFIX`` selects the build). Each
trial spawns two (optionally three) sessions, each with a pre-generated
op list over a small deterministic workload (tables ``t1``/``t2``/``t3``,
``k int primary key, v int``). Op kinds: predicate SELECTs, paired SELECTs
(same query twice), SELECT ... FOR UPDATE / FOR NO KEY UPDATE / FOR KEY
SHARE, UPDATE/DELETE/INSERT on small key ranges, INSERT ... ON CONFLICT
upserts, MERGE upserts, DELETE ... FOR UPDATE SKIP LOCKED (journaled via
RETURNING so replay stays exact), SAVEPOINT / ROLLBACK TO SAVEPOINT,
SET TRANSACTION ISOLATION LEVEL, COMMIT. With ``--maint-ops`` the pool
also emits autocommit-only maintenance verbs at transaction boundaries
(VACUUM / VACUUM ANALYZE / VACUUM FREEZE / VACUUM FULL / ANALYZE /
CLUSTER USING idx_t1_v / REINDEX TABLE / REFRESH MATERIALIZED VIEW /
CHECKPOINT — none journaled — plus rare TRUNCATE t1, which IS journaled
as a single-statement committed transaction whose replay clears t1).

Determinism: everything is a function of ``--seed`` and the trial index.
Op lists are generated from ``Random(seed*K+trial)``; the interleave
schedule is a seeded shuffle of session ids — sessions are stepped on
real threads through a synchronous rendezvous (only the scheduled session
executes; the others wait), so the schedule — not wall-clock racing —
decides the interleaving. A blocked op waits at most ``lock_timeout``
(there is no way for the lock holder to run while another session has the
rendezvous token), which keeps waits bounded and outcomes reproducible.

Oracles per trial:
- serializable: the observed final table state must equal the replayed
  state of SOME serial order of the committed transactions' write ops
  (journal-replayed on a Python model; INSERT-dup-invalid orders are not
  valid witnesses). A committed state reachable under no serial order is
  a hit. Serialization failures / deadlocks / lock timeouts are normal
  outcomes — counted, never hits. Maintenance-verb failures (lock
  contention, deadlock, serialization) are likewise expected aborts.
- snapshot stability (all levels): two consecutive identical SELECTs in
  one transaction with no interleaving gap must return the same bag —
  sound even under READ COMMITTED since no other session can commit
  between them.
- ``--crash-probe`` (default off): every ``--crash-every`` trials a
  backend is terminated mid-trial via ``pg_terminate_backend``; internal
  errors on *clean* sessions afterwards, a server crash, or a final state
  inconsistent with serial replay are hits.
- internal-class errors (XX000/assert/PANIC/connection loss on a session
  we did not kill) are always hits.

``--inject-lost-update`` is a harness SELF-TEST, not a real bug hunt: each
trial's workload is replaced by a deterministic non-atomic
read-modify-write on a shared hot key with a schedule that forces a lost
update. Under ``read_committed`` the oracle flags every trial (hits>0 —
proves the oracle works); under ``serializable`` SSI normally aborts one
transaction, which is a legal outcome.

Usage:
    python scripts/pg_txn_fuzz.py --trials 100 --out results/pg_txn_1
    COEVO_PG_PREFIX=$COEVO_PGBLD/pg162_assert \
        python scripts/pg_txn_fuzz.py --trials 10 --out /tmp/txn_smoke
    python scripts/pg_txn_fuzz.py --inject-lost-update \
        --iso read_committed --trials 3 --out /tmp/txn_selftest
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import random
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

LOGGER = logging.getLogger("pg_txn_fuzz")

ISO_SQL = {
    "serializable": "SERIALIZABLE",
    "repeatable_read": "REPEATABLE READ",
    "read_committed": "READ COMMITTED",
}
ISO_RANK = {"read_committed": 0, "repeatable_read": 1, "serializable": 2}
ISO_BY_RANK = {0: "READ COMMITTED", 1: "REPEATABLE READ",
               2: "SERIALIZABLE"}

TABLES = ("t1", "t2", "t3")
KEY_LO, KEY_HI = 1, 10      # select/update/delete key space (1..8 exist)
INS_LO, INS_HI = 5, 14      # insert key space overlaps the delete range
MAX_SERIAL_ORDERS = 50_000  # beyond this a trial is marked unverifiable

SETUP_SQLS = [
    "CREATE TABLE t1 (k int primary key, v int)",
    "INSERT INTO t1 SELECT g, g * 10 FROM generate_series(1, 8) g",
    "CREATE TABLE t2 (k int primary key, v int)",
    "INSERT INTO t2 SELECT g, g * 7 + 3 FROM generate_series(1, 8) g",
    "CREATE TABLE t3 (k int primary key, v int)",
    "INSERT INTO t3 SELECT g, g * 3 + 1 FROM generate_series(1, 8) g",
]

# Extra objects only --maint-ops needs: a secondary index so CLUSTER has
# a USING target, and a materialized view for REFRESH. Gated on the flag
# so non-maintenance runs keep byte-identical setup.
MAINT_SETUP_SQLS = [
    "CREATE INDEX idx_t1_v ON t1(v)",
    "CREATE MATERIALIZED VIEW mv_t1 AS SELECT k, v FROM t1",
]

# Autocommit-only maintenance verbs; weights are pick rates INSIDE the
# maintenance class. TRUNCATE stays rare (~1% of all ops): it is the
# only verb that writes, and it serializes against every open txn on t1.
MAINT_VERBS: list[tuple[str, int]] = [
    ("VACUUM t1", 6),
    ("VACUUM (ANALYZE) t1", 4),
    ("VACUUM (FREEZE) t1", 4),
    ("VACUUM FULL t1", 2),
    ("ANALYZE t1", 5),
    ("CLUSTER t1 USING idx_t1_v", 4),
    ("REINDEX TABLE t1", 4),
    ("CHECKPOINT", 3),
    ("REFRESH MATERIALIZED VIEW mv_t1", 2),
    ("TRUNCATE t1", 2),
]
# Fraction of transaction-boundary op slots spent on a maintenance verb.
# Boundaries are ~1 in 4-5 generated ops (and a maintenance op leaves the
# session at another boundary, so picks chain), which lands maintenance
# at roughly ~15% of the emitted op stream.
MAINT_BOUNDARY_P = 0.55

# Internal-class markers beyond normalize.is_internal_error — same list as
# pg_cursor_oracle/pg_error_scan (psycopg2 names XX000 "InternalError").
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

_CONN_DEAD_MARKERS = (
    "server closed the connection",
    "terminating connection",
    "connection not open",
    "could not receive data from server",
    "could not send data to server",
    "closed the connection unexpectedly",
    "no connection to the server",
    "connection already closed",
)

# error class -> stats counter name
_ERR_COUNTERS = {
    "serialization_failure": "serialization_failures",
    "deadlock": "deadlocks",
    "lock_timeout": "lock_timeouts",
    "query_canceled": "query_canceled",
    "unique_violation": "unique_violations",
    "in_failed_txn": "in_failed_txn_errors",
    "schema_missing": "schema_missing_errors",
    "txn_state_error": "txn_state_errors",
    "conn_dead": "conn_deaths",
    "internal": "internal_errors",
    "other": "other_errors",
}


def looks_internal(error: str | None) -> bool:
    """True when an error string indicates a DBMS-internal failure."""
    if not error:
        return False
    if is_internal_error(error):
        return True
    low = error.lower()
    return any(marker in low for marker in _INTERNAL_MARKERS)


def classify_error(exc: Exception) -> tuple[str, str]:
    """Map a psycopg2 failure to a normal-outcome or hit-relevant class.

    pgcode checks run FIRST: psycopg2 raises OperationalError (the same
    class used for connection death) for 55P03/40P01/57014, so isinstance
    checks alone would misclassify lock timeouts and deadlocks.
    """
    err = f"{type(exc).__name__}: {exc}"
    pgcode = getattr(exc, "pgcode", None) or ""
    if pgcode == "40001":
        return "serialization_failure", err
    if pgcode == "40P01":
        return "deadlock", err
    if pgcode == "55P03":
        return "lock_timeout", err
    if pgcode == "57014":
        return "query_canceled", err
    if pgcode == "23505":
        return "unique_violation", err
    if pgcode in ("42P01", "3F000"):
        # undefined_table / undefined_schema: the shared workload vanished
        # under a session — on an exclusive server this is an engine bug,
        # but it also fires when two fuzzer instances share one datadir
        return "schema_missing", err
    if pgcode == "25P02":
        return "in_failed_txn", err
    if pgcode.startswith("25P"):
        return "txn_state_error", err
    low = err.lower()
    if (isinstance(exc, (psycopg2.OperationalError,
                         psycopg2.InterfaceError))
            or any(m in low for m in _CONN_DEAD_MARKERS)):
        return "conn_dead", err
    if looks_internal(err):
        return "internal", err
    return "other", err


def _bag(rows) -> dict:
    """Loose multiset of a small row set as a JSON-safe dict."""
    return dict(loose_bag(normalize_rows(rows)))


# ---------------------------------------------------------------- model
def initial_model() -> dict[str, dict[int, int]]:
    """Deterministic initial contents mirroring SETUP_SQLS."""
    return {
        "t1": {k: k * 10 for k in range(1, 9)},
        "t2": {k: k * 7 + 3 for k in range(1, 9)},
        "t3": {k: k * 3 + 1 for k in range(1, 9)},
    }


def freeze_model(model: dict[str, dict[int, int]]):
    return tuple((t, tuple(sorted(model[t].items())))
                 for t in sorted(model))


def freeze_rows(rows_by_table: dict[str, list]):
    return tuple((t, tuple(sorted((int(k), int(v)) for k, v in rows)))
                 for t, rows in sorted(rows_by_table.items()))


class _InvalidOrder(Exception):
    """A committed op cannot legally apply in this serial order."""


def apply_mut(state: dict[str, dict[int, int]], mut: list) -> None:
    """Apply one journaled write to the Python model.

    ``rmw`` re-reads the model state: the recorded op is "read k, write
    k=read+d", so a serial replay reads whatever the serial state holds —
    a lost update in the real execution shows up as an unreachable state.
    """
    kind = mut[0]
    tbl = state[mut[1]]
    if kind == "insert":
        _, _, k, v = mut
        if k in tbl:
            raise _InvalidOrder(f"insert dup k={k}")
        tbl[k] = v
    elif kind == "update_add":
        _, _, a, b, d = mut
        for k in list(tbl):
            if a <= k <= b:
                tbl[k] += d
    elif kind == "update_set":
        _, _, k, v = mut
        if k in tbl:
            tbl[k] = v
    elif kind == "delete":
        _, _, a, b = mut
        for k in [k for k in tbl if a <= k <= b]:
            del tbl[k]
    elif kind == "rmw":
        _, _, k, d = mut
        if k in tbl:
            tbl[k] += d
    elif kind == "upsert_add":
        _, _, k, v, d = mut
        if k in tbl:
            tbl[k] += d
        else:
            tbl[k] = v
    elif kind == "delete_exact":
        # RETURNING journaled the realized write set, so replay is exact
        # even though SKIP LOCKED picked the rows nondeterministically.
        _, _, keys = mut
        for k in keys:
            tbl.pop(k, None)
    elif kind == "truncate":
        # TRUNCATE removes every row in the table; in a serial order the
        # replay clears every row committed into the dict up to this
        # position. Sound because a committed TRUNCATE implies no open
        # txn still held a lock on the table (it would have blocked).
        tbl.clear()
    elif kind == "cond_set":
        # The op wrote k=v only because NOT EXISTS(v >= cv) held on its
        # snapshot. Replay re-evaluates the condition at each serial
        # position: a position where v >= cv exists cannot have produced
        # this write, so the order is invalid. This is what makes
        # write-skew visible to the final-state oracle.
        _, _, k, v, cv = mut
        if any(x >= cv for x in tbl.values()):
            raise _InvalidOrder(f"cond_set blocked: v>={cv} present")
        if k in tbl:
            tbl[k] = v
    else:  # pragma: no cover - generation keeps this closed
        raise ValueError(f"unknown mut {mut}")


def serial_state_space(committed: list[dict], max_orders: int):
    """All final states reachable by serial orders of committed txns.

    Returns (states:set, valid_orders:int, checked:int, complete:bool).
    An order is invalid when a committed INSERT collides in replay — that
    order can never be the true serialization. ``complete`` is False when
    the order space was truncated at ``max_orders``.
    """
    states: set = set()
    valid = 0
    checked = 0
    for perm in itertools.permutations(range(len(committed))):
        checked += 1
        if checked > max_orders:
            return states, valid, checked - 1, False
        state = initial_model()
        ok = True
        for ti in perm:
            for mut in committed[ti]["writes"]:
                try:
                    apply_mut(state, mut)
                except _InvalidOrder:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            valid += 1
            states.add(freeze_model(state))
    return states, valid, checked, True


# ------------------------------------------------------- op generation
def _pick(rng: random.Random, pool: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in pool)
    r = rng.uniform(0, total)
    acc = 0.0
    for name, w in pool:
        acc += w
        if r <= acc:
            return name
    return pool[-1][0]


def _range(rng: random.Random, lo: int, hi: int, max_span: int):
    a = rng.randint(lo, hi)
    b = min(hi, a + rng.randint(0, max_span))
    return a, b


def gen_maint_op(rng: random.Random) -> dict:
    """One standalone maintenance op: a single autocommit statement.

    ``autocommit`` tells the worker to skip BEGIN — these verbs cannot
    run inside a transaction block. Only TRUNCATE carries a journal mut;
    every other verb is content-neutral so replay ignores it.
    """
    verb = _pick(rng, MAINT_VERBS)
    op = {"kind": "maint", "sql": [verb], "autocommit": True}
    if verb.startswith("TRUNCATE"):
        op["mut"] = ["truncate", "t1"]
    return op


def gen_session_ops(rng: random.Random, n_ops: int, iso_rank: int,
                    max_commits: int, maint: bool = False) -> list[dict]:
    """One session's op list; valid savepoint/txn refs by construction."""
    ops: list[dict] = []
    in_txn = False
    txn_pos = 0
    sps: list[str] = []
    sp_ctr = 0
    commits = 0
    for _ in range(n_ops):
        # Maintenance verbs are standalone autocommit statements: they
        # can only occupy a transaction boundary, never an open txn.
        if maint and not in_txn and rng.random() < MAINT_BOUNDARY_P:
            ops.append(gen_maint_op(rng))
            continue
        if not in_txn:           # next op implicitly begins a transaction
            in_txn = True
            txn_pos = 0
            sps = []
        pool = [("select", 20), ("select_pair", 12), ("update_add", 16),
                ("update_range", 10), ("update_set", 8), ("delete", 9),
                ("insert", 10), ("savepoint", 5),
                # SSI/lock-pressure ops: row locks, upserts, MERGE, and
                # SKIP LOCKED (journals its realized write set via RETURNING).
                ("select_for_update", 8), ("upsert", 8),
                ("merge_upsert", 6), ("delete_skip_locked", 5)]
        if commits < max_commits:
            pool.append(("commit", 10))
        if sps:
            pool.append(("rollback_to", 6))
        if txn_pos == 0:
            pool.append(("set_iso", 5))
        kind = _pick(rng, pool)
        t = rng.choice(TABLES)

        if kind == "select":
            a, b = _range(rng, KEY_LO, KEY_HI, 4)
            q = f"SELECT k, v FROM {t} WHERE k BETWEEN {a} AND {b}"
            op = {"kind": kind, "sql": [q]}
        elif kind == "select_pair":
            a, b = _range(rng, KEY_LO, KEY_HI, 4)
            q = f"SELECT k, v FROM {t} WHERE k BETWEEN {a} AND {b}"
            op = {"kind": kind, "sql": [q, q]}
        elif kind == "update_add":
            k = rng.randint(KEY_LO, KEY_HI)
            d = rng.choice([-7, -5, -3, -2, -1, 1, 2, 3, 5, 7])
            op = {"kind": kind,
                  "sql": [f"UPDATE {t} SET v = v + {d} WHERE k = {k}"],
                  "mut": ["update_add", t, k, k, d]}
        elif kind == "update_range":
            a, b = _range(rng, KEY_LO, KEY_HI, 4)
            d = rng.choice([-5, -3, -1, 1, 3, 5])
            op = {"kind": kind,
                  "sql": [f"UPDATE {t} SET v = v + {d} "
                          f"WHERE k BETWEEN {a} AND {b}"],
                  "mut": ["update_add", t, a, b, d]}
        elif kind == "update_set":
            k = rng.randint(KEY_LO, KEY_HI)
            v = rng.randint(0, 99)
            op = {"kind": kind,
                  "sql": [f"UPDATE {t} SET v = {v} WHERE k = {k}"],
                  "mut": ["update_set", t, k, v]}
        elif kind == "delete":
            a, b = _range(rng, KEY_LO, KEY_HI, 2)
            op = {"kind": kind,
                  "sql": [f"DELETE FROM {t} WHERE k BETWEEN {a} AND {b}"],
                  "mut": ["delete", t, a, b]}
        elif kind == "insert":
            k = rng.randint(INS_LO, INS_HI)
            v = rng.randint(0, 99)
            op = {"kind": kind,
                  "sql": [f"INSERT INTO {t} VALUES ({k}, {v})"],
                  "mut": ["insert", t, k, v]}
        elif kind == "select_for_update":
            a, b = _range(rng, KEY_LO, KEY_HI, 4)
            mode = rng.choice(
                ["FOR UPDATE", "FOR NO KEY UPDATE", "FOR KEY SHARE"])
            op = {"kind": kind,
                  "sql": [f"SELECT k, v FROM {t} "
                          f"WHERE k BETWEEN {a} AND {b} {mode}"]}
        elif kind == "upsert":
            k = rng.randint(INS_LO, INS_HI)
            v = rng.randint(0, 99)
            d = rng.choice([1, 2, 5])
            op = {"kind": kind,
                  "sql": [f"INSERT INTO {t} VALUES ({k}, {v}) "
                          f"ON CONFLICT (k) DO UPDATE SET v = {t}.v + {d}"],
                  "mut": ["upsert_add", t, k, v, d]}
        elif kind == "merge_upsert":
            k = rng.randint(INS_LO, INS_HI)
            v = rng.randint(0, 99)
            d = rng.choice([1, 2, 5])
            op = {"kind": kind,
                  "sql": [f"MERGE INTO {t} USING (VALUES ({k}, {v})) "
                          f"AS s(k, v) ON {t}.k = s.k "
                          f"WHEN MATCHED THEN UPDATE SET v = {t}.v + {d} "
                          f"WHEN NOT MATCHED THEN INSERT VALUES (s.k, s.v)"],
                  "mut": ["upsert_add", t, k, v, d]}
        elif kind == "delete_skip_locked":
            # The mut is filled in at dispatch from RETURNING: the realized
            # write set is nondeterministic, the journal makes replay exact.
            a, b = _range(rng, KEY_LO, KEY_HI, 3)
            op = {"kind": kind, "table": t,
                  "sql": [f"DELETE FROM {t} WHERE k IN (SELECT k FROM {t} "
                          f"WHERE k BETWEEN {a} AND {b} "
                          f"FOR UPDATE SKIP LOCKED) RETURNING k"]}
        elif kind == "savepoint":
            sp_ctr += 1
            name = f"sp{sp_ctr}"
            sps.append(name)
            op = {"kind": kind, "sql": [f"SAVEPOINT {name}"], "sp": name}
        elif kind == "rollback_to":
            target = rng.choice(sps)
            # savepoints declared after the target stop existing
            del sps[sps.index(target) + 1:]
            op = {"kind": kind,
                  "sql": [f"ROLLBACK TO SAVEPOINT {target}"],
                  "sp": target}
        elif kind == "set_iso":
            lvl = ISO_BY_RANK[rng.randint(iso_rank, 2)]
            op = {"kind": kind,
                  "sql": [f"SET TRANSACTION ISOLATION LEVEL {lvl}"]}
        else:  # commit
            op = {"kind": "commit", "sql": ["COMMIT"]}
            commits += 1
            in_txn = False
        ops.append(op)
        txn_pos += 1
    return ops


def gen_inject_trial(rng: random.Random, sessions: int):
    """Self-test scenario: two non-atomic read-modify-writes, one hot key.

    Schedule forces read/read then write/commit/write/commit so the lost
    update commits under READ COMMITTED; the serial oracle must flag it.
    """
    tbl = rng.choice(TABLES)
    k = rng.randint(1, 4)
    d0, d1 = rng.randint(2, 9), rng.randint(2, 9)
    lead = rng.randrange(2)
    other = 1 - lead
    ops: list[list[dict]] = [[], []]
    for sid, d in ((lead, d0), (other, d1)):
        ops[sid] = [
            {"kind": "rmw_read",
             "sql": [f"SELECT v FROM {tbl} WHERE k = {k}"],
             "table": tbl, "key": k},
            {"kind": "rmw_write",
             "sql": [f"UPDATE {tbl} SET v = {{VAL}} WHERE k = {k}"],
             "table": tbl, "key": k, "d": d},
            {"kind": "commit", "sql": ["COMMIT"]},
        ]
    schedule = [lead, other, lead, lead, other, other]
    for sid in range(2, sessions):  # extra sessions: benign tail ops
        t = rng.choice(TABLES)
        ops.append([
            {"kind": "select",
             "sql": [f"SELECT k, v FROM {t} WHERE k BETWEEN 1 AND 5"]},
            {"kind": "commit", "sql": ["COMMIT"]},
        ])
        schedule += [sid, sid]
    return ops, schedule


def gen_ssi_trial(rng: random.Random, sessions: int, scenario: str,
                  churn: int):
    """Scripted SSI write-skew scenarios (upstream-reported, still live).

    tidrange_skew — Brazeal 2026-07: SIREAD predicate locks are not taken
    for Tid Range Scans, so the rw-antidependency between the two
    serializable txns is missed and both commit. The schedule forces
    set/set, read/read, write/write, commit/commit; each UPDATE is
    conditioned on NOT EXISTS so both-writes-committed is unreachable in
    any serial order (cond_set mut re-evaluates per position).

    summarize_skew — Brazeal 2026-08: once OldCommittedSxact
    summarization kicks in (~(MaxBackends+max_prepared)*10 committed
    serializable txns), CheckTargetForConflictsIn ignores summarized
    locks (no finishedBefore) — a SIREAD held across the churn loses
    its conflict, letting the long txn commit. Run with
    COEVO_PG_EXTRA_OPTS="-c max_connections=10" to shrink the churn
    threshold ~10x.
    """
    CTID_RANGE = "ctid BETWEEN '(0,0)'::tid AND '(0,100)'::tid"
    COND_C = "(SELECT 1 FROM t1 WHERE {scan} AND v >= 300)"

    def cond_update(k: int, v: int, scan: str) -> dict:
        return {
            "kind": "cond_update",
            "sql": [f"UPDATE t1 SET v = {v} WHERE k = {k} AND NOT EXISTS "
                    f"{COND_C.format(scan=scan)}"],
            "mut": ["cond_set", "t1", k, v, 300],
        }

    def sel(sql: str) -> dict:
        return {"kind": "select", "sql": [sql]}

    commit = {"kind": "commit", "sql": ["COMMIT"]}
    ops: list[list[dict]] = [[], []]
    if scenario == "tidrange_skew":
        ctid = f"{CTID_RANGE}"
        for sid, (k, v) in enumerate(((1, 400), (2, 500))):
            ops[sid] = [
                {"kind": "session_set",
                 "sql": ["SET enable_seqscan = off"]},
                sel(f"SELECT sum(v) FROM t1 WHERE {ctid}"),
                cond_update(k, v, ctid),
                commit,
            ]
        # set,set / read,read / write,write / commit,commit
        schedule = [0, 1, 0, 1, 0, 1, 0, 1]
    else:  # summarize_skew
        plain = "TRUE"
        ops[0] = [
            sel("SELECT count(*) FROM t1 WHERE v >= 300"),
            cond_update(2, 500, plain),
            sel("SELECT count(*) FROM t1 WHERE v >= 300"),
            commit,
        ]
        ops[1] = [
            sel("SELECT count(*) FROM t1 WHERE v >= 300"),
            cond_update(1, 400, plain),
            commit,
        ]
        # session-2 tail: churn of tiny serializable txns that pushes
        # OldCommittedSxact past its summarization threshold while S1's
        # SIREAD lock is still held.
        for _ in range(churn):
            ops[1] += [sel("SELECT 1"), commit]
        schedule = [0] + [1] * (3 + 2 * churn) + [0, 0, 0]
    for sid in range(2, sessions):  # extra sessions: benign tail ops
        t = rng.choice(TABLES)
        ops.append([
            sel(f"SELECT k, v FROM {t} WHERE k BETWEEN 1 AND 5"),
            commit,
        ])
        schedule += [sid, sid]
    return ops, schedule


# ------------------------------------------------------------ sessions
class TxnSession(threading.Thread):
    """One database session driven one op at a time by the scheduler.

    The scheduler grants the rendezvous token (``_turn``); the worker
    executes exactly one op, records the outcome, and signals ``_done``.
    Blocking ops can only end via lock_timeout/deadlock detection — no
    other session may run while this one holds the token — which is what
    makes every outcome a deterministic function of the schedule.
    """

    def __init__(self, uri: str, sid: int, ops: list[dict], iso_sql: str,
                 lock_ms: int, deadlock_ms: int, stmt_ms: int,
                 stats: dict):
        super().__init__(daemon=True, name=f"txn_sess_{sid}")
        self.uri = uri
        self.sid = sid
        self.ops = ops
        self.iso_sql = iso_sql
        self.lock_ms = lock_ms
        self.deadlock_ms = deadlock_ms
        self.stmt_ms = stmt_ms
        self.stats = stats

        self.conn = None
        self.pid = None
        self.connect_error = None
        self.dead = False
        self.probe_victim = False

        self.in_txn = False
        self.txn_seq = 0
        self.txn: dict | None = None      # journal of the open txn
        self.committed: list[dict] = []   # txns that truly committed
        self.rmw_val = None               # last rmw_read result

        self.trace: list[dict] = []
        self.last_rec: dict | None = None
        self._req = None
        self._turn = threading.Event()
        self._done = threading.Event()
        self._ready = threading.Event()

    # --------------------------------------------------- thread body
    def run(self) -> None:
        try:
            self.conn = psycopg2.connect(self.uri)
            self.conn.autocommit = True
            cur = self.conn.cursor()
            for stmt in (f"SET lock_timeout = {self.lock_ms}",
                         f"SET deadlock_timeout = {self.deadlock_ms}",
                         f"SET statement_timeout = {self.stmt_ms}"):
                try:
                    cur.execute(stmt)
                except Exception:  # noqa: BLE001 - best-effort GUCs
                    LOGGER.debug("session %d GUC failed: %s", self.sid, stmt)
            self.pid = self.conn.get_backend_pid()
        except Exception as exc:  # noqa: BLE001
            self.connect_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        self._ready.set()
        while True:
            self._turn.wait()
            self._turn.clear()
            act, payload = self._req
            try:
                if act == "stop":
                    break
                if act == "op":
                    self._step(*payload)
                elif act == "finish":
                    self._finish()
            except Exception as exc:  # noqa: BLE001 - never wedge scheduler
                LOGGER.exception("worker %d harness error", self.sid)
                self.stats["harness_errors"] += 1
                self.last_rec = {"outcome": "harness_error",
                                 "err": f"{type(exc).__name__}: {exc}"}
                self.trace.append(self.last_rec)
                self.dead = True
            self._done.set()
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------ scheduler API
    def request(self, act: str, payload=None) -> None:
        self._done.clear()
        self._req = (act, payload)
        self._turn.set()

    def wait_done(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    # ------------------------------------------------------- internals
    def _begin(self, rec: dict) -> None:
        try:
            self.conn.cursor().execute(
                f"BEGIN ISOLATION LEVEL {self.iso_sql}")
        except psycopg2.Error as exc:
            cls, err = classify_error(exc)
            rec["begin_error"] = err
            rec["err_class"] = cls
            self.stats[_ERR_COUNTERS.get(cls, "other_errors")] += 1
            if cls == "conn_dead":
                self.dead = True
            return
        self.txn_seq += 1
        self.in_txn = True
        self.txn = {"seq": self.txn_seq, "writes": [], "sp": {},
                    "sp_order": []}
        rec["txn"] = self.txn_seq

    def _step(self, op_idx: int) -> None:
        op = self.ops[op_idx]
        rec = {"i": op_idx, "kind": op["kind"], "sql": list(op["sql"])}
        if self.dead or self.conn is None:
            rec["outcome"] = "skipped_dead"
            self.trace.append(rec)
            self.last_rec = rec
            return
        if not self.in_txn:
            if op.get("autocommit"):
                # standalone autocommit statement — no BEGIN (VACUUM/
                # CLUSTER/CHECKPOINT etc. cannot run in a txn block)
                rec["txn"] = None
            else:
                self._begin(rec)
                if not self.in_txn:
                    rec["outcome"] = "begin_failed"
                    self.trace.append(rec)
                    self.last_rec = rec
                    return
        else:
            rec["txn"] = self.txn_seq
        try:
            self._dispatch(op, rec)
        except psycopg2.Error as exc:
            self._record_error(exc, rec)
        self.trace.append(rec)
        self.last_rec = rec

    def _dispatch(self, op: dict, rec: dict) -> None:
        kind = op["kind"]
        cur = self.conn.cursor()
        eff.count("sql_executions", len(op["sql"]))
        if kind == "select":
            cur.execute(op["sql"][0])
            rec["bag"] = _bag(cur.fetchall())
            rec["outcome"] = "ok"
        elif kind == "select_pair":
            cur.execute(op["sql"][0])
            rec["bag1"] = _bag(cur.fetchall())
            cur.execute(op["sql"][1])
            rec["bag2"] = _bag(cur.fetchall())
            rec["outcome"] = "ok"
        elif kind == "rmw_read":
            cur.execute(op["sql"][0])
            rows = cur.fetchall()
            self.rmw_val = rows[0][0] if rows else None
            rec["read"] = self.rmw_val
            rec["outcome"] = "ok"
        elif kind == "rmw_write":
            k, d, t = op["key"], op["d"], op["table"]
            if self.rmw_val is None:
                # row absent at read time: degrade to a plain update_add
                cur.execute(
                    f"UPDATE {t} SET v = v + {d} WHERE k = {k}")
                mut = ["update_add", t, k, k, d]
            else:
                cur.execute(
                    f"UPDATE {t} SET v = {self.rmw_val + d} WHERE k = {k}")
                mut = ["rmw", t, k, d]
                rec["wrote"] = self.rmw_val + d
            self.txn["writes"].append(mut)
            rec["outcome"] = "ok"
        elif kind == "select_for_update":
            cur.execute(op["sql"][0])
            rec["bag"] = _bag(cur.fetchall())
            rec["outcome"] = "ok"
        elif kind == "delete_skip_locked":
            cur.execute(op["sql"][0])
            keys = [row[0] for row in cur.fetchall()]
            self.txn["writes"].append(
                ["delete_exact", op["table"], keys])
            rec["outcome"] = "ok"
        elif kind == "session_set":
            cur.execute(op["sql"][0])
            rec["outcome"] = "ok"
        elif kind == "cond_update":
            cur.execute(op["sql"][0])
            # journal the conditional mut only when a row was written —
            # replay re-evaluates the NOT EXISTS per serial position
            if cur.rowcount and cur.rowcount > 0:
                self.txn["writes"].append(list(op["mut"]))
            rec["outcome"] = "ok"
        elif kind in ("update_add", "update_range", "update_set",
                      "delete", "insert", "upsert", "merge_upsert"):
            cur.execute(op["sql"][0])
            self.txn["writes"].append(list(op["mut"]))
            rec["outcome"] = "ok"
        elif kind == "maint":
            # Autocommit utility verb: no BEGIN was issued, so a success
            # commits by itself. TRUNCATE is the one verb that writes —
            # journal it as its own single-statement committed txn so
            # serial replay stays sound (all other verbs are
            # content-neutral and need no journal entry).
            cur.execute(op["sql"][0])
            self.stats["maint_ops"] += 1
            tag = f"maint_{op['sql'][0].split()[0].lower()}"
            self.stats[tag] = self.stats.get(tag, 0) + 1
            if op.get("mut"):
                self.committed.append({"sid": self.sid,
                                       "txn_seq": self.txn_seq,
                                       "writes": [list(op["mut"])]})
                self.stats["commits"] += 1
            rec["outcome"] = "ok"
        elif kind == "savepoint":
            cur.execute(op["sql"][0])
            self.txn["sp"][op["sp"]] = len(self.txn["writes"])
            self.txn["sp_order"].append(op["sp"])
            self.stats["savepoints"] += 1
            rec["outcome"] = "ok"
        elif kind == "rollback_to":
            cur.execute(op["sql"][0])
            keep = self.txn["sp"][op["sp"]]
            del self.txn["writes"][keep:]
            order = self.txn["sp_order"]
            for name in order[order.index(op["sp"]) + 1:]:
                del self.txn["sp"][name]
            del order[order.index(op["sp"]) + 1:]
            self.stats["rollback_to_savepoints"] += 1
            rec["outcome"] = "ok"
        elif kind == "set_iso":
            cur.execute(op["sql"][0])
            self.stats["set_iso_ops"] += 1
            rec["outcome"] = "ok"
        elif kind == "commit":
            self._commit(rec)
        else:  # pragma: no cover
            rec["outcome"] = "unknown_op"

    def _commit(self, rec: dict) -> None:
        try:
            cur = self.conn.cursor()
            cur.execute("COMMIT")
        except psycopg2.Error as exc:
            # e.g. 40001 raised at COMMIT: the txn is gone either way
            self._record_error(exc, rec, during="commit")
            self.in_txn = False
            self.txn = None
            return
        if cur.statusmessage == "COMMIT":
            self.committed.append({"sid": self.sid,
                                   "txn_seq": self.txn["seq"],
                                   "writes": self.txn["writes"]})
            self.stats["commits"] += 1
            rec["outcome"] = "ok"
            rec["commit"] = "committed"
        else:
            # tag "ROLLBACK": the txn was aborted; COMMIT rolled it back
            self.stats["aborts"] += 1
            rec["outcome"] = "ok"
            rec["commit"] = "rolled_back"
        self.in_txn = False
        self.txn = None

    def _record_error(self, exc: psycopg2.Error, rec: dict,
                      during: str | None = None) -> None:
        cls, err = classify_error(exc)
        rec["outcome"] = "error"
        rec["err"] = err.strip().split("\n")[0]
        rec["pgcode"] = getattr(exc, "pgcode", None)
        rec["err_class"] = cls
        if during:
            rec["during"] = during
        self.stats[_ERR_COUNTERS.get(cls, "other_errors")] += 1
        if rec.get("kind") == "maint":
            # a failed autocommit utility statement is an abort of its
            # own single-statement transaction — expected, and only a
            # hit when the class itself is internal/conn-dead (the
            # driver decides that from err_class, not from this count)
            self.stats["aborts"] += 1
            self.stats["maint_aborts"] += 1
        if cls == "conn_dead":
            self.dead = True
            self.in_txn = False
            self.txn = None
        elif during == "commit":
            # an error at COMMIT aborts the txn (e.g. serialization failure)
            self.stats["aborts"] += 1

    def _finish(self) -> None:
        rec = {"i": None, "kind": "final_commit", "sql": ["COMMIT"],
               "txn": self.txn_seq}
        if self.dead or self.conn is None:
            rec["outcome"] = "skipped_dead"
        elif not self.in_txn:
            rec["outcome"] = "no_open_txn"
        else:
            try:
                self._commit(rec)
            except Exception as exc:  # noqa: BLE001
                self._record_error(exc, rec)
        self.trace.append(rec)
        self.last_rec = rec


# -------------------------------------------------------------- driver
def _observe_state(pg: PostgresRunner):
    """Final table contents; (frozen_state, None) or (None, error)."""
    rows_by_table = {}
    for t in TABLES:
        res = pg.run(f"SELECT k, v FROM {t} ORDER BY k", timeout_s=10.0)
        if not res.ok:
            return None, res.error
        rows_by_table[t] = res.rows
    return freeze_rows(rows_by_table), None


def _jsonable_state(frozen) -> dict:
    return {t: [list(r) for r in rows] for t, rows in frozen}


def _hit_context(trial: int, args, ops_lists, schedule, events,
                 committed) -> dict:
    return {
        "trial": trial,
        "iso": args.iso,
        "seed": args.seed,
        "sessions": args.sessions,
        "ops": [[o["sql"] for o in ops] for ops in ops_lists],
        "schedule": schedule,
        "events": events,
        "committed_writes": committed,
    }


def run_trial(pg: PostgresRunner, uri: str, trial: int, args,
              stats: dict, hits: list[dict]) -> None:
    rng = random.Random(args.seed * 1_000_003 + trial)
    setup_sqls = (SETUP_SQLS + MAINT_SETUP_SQLS if args.maint_ops
                  else SETUP_SQLS)
    outcomes = pg.setup(setup_sqls)
    if any(err for _, err in outcomes):
        stats["setup_failures"] += 1
        LOGGER.warning("trial %d setup failed: %s", trial,
                       [e for _, e in outcomes if e][:2])
        return

    sessions = args.sessions
    if args.inject_lost_update:
        ops_lists, schedule = gen_inject_trial(rng, sessions)
    elif args.ssi_scenario != "off":
        scen = args.ssi_scenario
        if scen == "all":
            scen = ("tidrange_skew", "summarize_skew")[trial % 2]
        ops_lists, schedule = gen_ssi_trial(
            rng, sessions, scen, args.ssi_churn)
    else:
        iso_rank = ISO_RANK[args.iso]
        commit_cap = 2 if sessions == 2 else 1  # bounds serial orders
        ops_lists = [gen_session_ops(rng, args.ops_per_session, iso_rank,
                                     commit_cap, maint=args.maint_ops)
                     for _ in range(sessions)]
        schedule = [sid for sid in range(sessions)
                    for _ in ops_lists[sid]]
        rng.shuffle(schedule)

    workers = [
        TxnSession(uri, sid, ops_lists[sid], ISO_SQL[args.iso],
                   args.lock_timeout_ms, args.deadlock_timeout_ms,
                   args.statement_timeout_ms, stats)
        for sid in range(sessions)
    ]
    for w in workers:
        w.start()
    for w in workers:
        if not w._ready.wait(30.0) or w.connect_error:
            stats["connect_failures"] += 1
            w.dead = True

    cursors = [0] * sessions
    events: list[dict] = []
    compromised = False  # workload objects vanished mid-trial
    probe_at = None
    if args.crash_probe and trial % args.crash_every == args.crash_every - 1:
        probe_at = rng.randrange(len(schedule))
        stats["crash_probes"] += 1

    for step, sid in enumerate(schedule):
        w = workers[sid]
        idx = cursors[sid]
        cursors[sid] += 1
        if w.dead or w.connect_error:
            events.append({"step": step, "sid": sid, "i": idx,
                           "kind": ops_lists[sid][idx]["kind"],
                           "sql": ops_lists[sid][idx]["sql"],
                           "outcome": "skipped_dead"})
        else:
            w.request("op", (idx,))
            if not w.wait_done(args.step_timeout_s):
                stats["hung_backends"] += 1
                stats["hits"] += 1
                rec = {"step": step, "sid": sid, "i": idx,
                       "kind": ops_lists[sid][idx]["kind"],
                       "sql": ops_lists[sid][idx]["sql"],
                       "outcome": "hung"}
                events.append(rec)
                hits.append({
                    "kind": "hung_backend",
                    **_hit_context(trial, args, ops_lists, schedule,
                                   events, []),
                })
                LOGGER.info("HUNG backend trial=%d sid=%d op=%s",
                            trial, sid, ops_lists[sid][idx]["sql"])
                w.dead = True
                if w.pid:
                    pg.run(f"SELECT pg_terminate_backend({w.pid})",
                           timeout_s=5.0)
            else:
                rec = dict(w.last_rec)
                rec["step"] = step
                rec["sid"] = sid
                events.append(rec)
                cls = rec.get("err_class")
                if cls == "schema_missing":
                    compromised = True
                if cls == "internal" or (
                        cls == "conn_dead" and not w.probe_victim):
                    stats["hits"] += 1
                    hits.append({
                        "kind": ("internal_error" if cls == "internal"
                                 else "unexpected_conn_death"),
                        "bad_event": rec,
                        **_hit_context(trial, args, ops_lists, schedule,
                                       events, []),
                    })
                    LOGGER.info("INTERNAL/CONN hit trial=%d sid=%d "
                                "err=%s", trial, sid, rec.get("err"))
        if probe_at is not None and step == probe_at:
            # deterministic victim choice: seeded first pick, then scan
            victim = None
            first = rng.randrange(sessions)
            for off in range(sessions):
                cand = workers[(first + off) % sessions]
                if not cand.dead and cand.pid and not cand.probe_victim:
                    victim = cand
                    break
            if victim is not None:
                victim.probe_victim = True  # its conn death is expected
                res = pg.run(
                    f"SELECT pg_terminate_backend({victim.pid})",
                    timeout_s=5.0)
                stats["terminated_backends"] += 1
                events.append({"step": step, "sid": victim.sid,
                               "kind": "probe_terminate",
                               "pid": victim.pid,
                               "outcome": "ok" if res.ok else "failed"})
                # wait for the backend to actually disappear so the rest
                # of the schedule sees a deterministic post-kill world
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    alive = pg.run(
                        "SELECT count(*) FROM pg_stat_activity "
                        f"WHERE pid = {victim.pid}", timeout_s=5.0)
                    if alive.ok and alive.rows \
                            and int(alive.rows[0][0]) == 0:
                        break
                    time.sleep(0.15)

    # final commits, still rendezvous'd one session at a time; every
    # worker gets "stop" so its thread exits and the conn closes (for a
    # hung worker the queued stop is consumed whenever its step returns)
    for w in workers:
        if w.connect_error or w.conn is None:
            continue
        if not w.dead:
            w.request("finish")
            if w.wait_done(args.step_timeout_s) and w.last_rec:
                rec = dict(w.last_rec)
                rec["step"] = f"final_{w.sid}"
                rec["sid"] = w.sid
                events.append(rec)
        w.request("stop")
    for w in workers:
        w.join(timeout=10.0)

    # write-empty committed txns are identity in any serial order; keeping
    # them only explodes the permutation space (summarize_skew churns 300+).
    committed = [c for w in workers for c in w.committed if c["writes"]]
    if compromised:
        # Somebody dropped the workload objects mid-trial. On an
        # exclusive server that is an engine bug worth a hit; when two
        # fuzzer runs share one datadir it is expected noise — either way
        # the serial/pair oracles cannot reason about this trial.
        stats["compromised_trials"] += 1
        stats["hits"] += 1
        bad = next((e for e in events
                    if e.get("err_class") == "schema_missing"), None)
        hits.append({
            "kind": "workload_vanished",
            "bad_event": bad,
            **_hit_context(trial, args, ops_lists, schedule,
                           events, committed),
        })
        eff.count("trials")
        return
    observed, obs_err = _observe_state(pg)
    if observed is None:
        stats["observe_failures"] += 1
        if looks_internal(obs_err):
            stats["hits"] += 1
            hits.append({
                "kind": "observe_internal_error",
                "error": obs_err,
                **_hit_context(trial, args, ops_lists, schedule,
                               events, committed),
            })
    else:
        stats["trials_completed"] += 1

    # ---- oracle: snapshot stability of paired selects (all levels) ----
    for w in workers:
        for rec in w.trace:
            if rec.get("outcome") == "ok" and "bag1" in rec \
                    and rec["bag1"] != rec["bag2"]:
                stats["hits"] += 1
                hits.append({
                    "kind": "unstable_repeat_read",
                    "bad_event": rec,
                    **_hit_context(trial, args, ops_lists, schedule,
                                   events, committed),
                })
                LOGGER.info("UNSTABLE READ trial=%d sid=%d op=%s",
                            trial, w.sid, rec["sql"][0])
            if "bag1" in rec:
                stats["pair_reads_checked"] += 1

    # ---- oracle: serializability of the committed final state --------
    if observed is not None:
        if not committed:
            if observed != freeze_model(initial_model()):
                stats["hits"] += 1
                hits.append({
                    "kind": "state_changed_without_commits",
                    "observed_state": _jsonable_state(observed),
                    **_hit_context(trial, args, ops_lists, schedule,
                                   events, committed),
                })
                LOGGER.info("PHANTOM STATE trial=%d", trial)
        else:
            states, valid, checked, complete = serial_state_space(
                committed, MAX_SERIAL_ORDERS)
            stats["serial_orders_checked"] += checked
            stats["serial_orders_valid"] += valid
            if not complete:
                stats["unverifiable_trials"] += 1
            elif observed not in states:
                serializable_required = (
                    args.iso == "serializable"
                    or args.inject_lost_update
                    or args.ssi_scenario != "off")
                rec = {
                    "kind": ("no_valid_serial_order" if valid == 0
                             else "nonserializable_final_state"),
                    "observed_state": _jsonable_state(observed),
                    "serial_states": [_jsonable_state(s)
                                      for s in sorted(states)][:64],
                    "valid_orders": valid,
                    "orders_checked": checked,
                    **_hit_context(trial, args, ops_lists, schedule,
                                   events, committed),
                }
                if serializable_required:
                    stats["hits"] += 1
                    hits.append(rec)
                    LOGGER.info(
                        "NONSERIALIZABLE trial=%d valid_orders=%d",
                        trial, valid)
                else:
                    # legal under weak isolation: counted, not a hit
                    stats["nonserializable_states"] += 1
    eff.count("trials")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_txn",
                    help="server datadir; must be exclusive — two runs on "
                         "one datadir drop each other's tables mid-trial")
    ap.add_argument("--pg-prefix", default=None,
                    help="install prefix; COEVO_PG_PREFIX env also works")
    ap.add_argument("--out", default="results/pg_txn_1")
    ap.add_argument("--trials", type=int, default=100)
    ap.add_argument("--ops-per-session", type=int, default=8)
    ap.add_argument("--sessions", type=int, default=2, choices=(2, 3))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--iso", default="serializable",
                    choices=tuple(ISO_SQL))
    ap.add_argument("--lock-timeout-ms", type=int, default=800,
                    help="bounds each rendezvous lock wait")
    ap.add_argument("--deadlock-timeout-ms", type=int, default=150,
                    help="deadlock check fires before lock_timeout")
    ap.add_argument("--statement-timeout-ms", type=int, default=20000)
    ap.add_argument("--step-timeout-s", type=float, default=25.0,
                    help="rendezvous watchdog: a longer op is a hung "
                         "backend hit")
    ap.add_argument("--crash-probe", action="store_true",
                    help="terminate one backend mid-trial every "
                         "--crash-every trials")
    ap.add_argument("--crash-every", type=int, default=10)
    ap.add_argument("--inject-lost-update", action="store_true",
                    help="SELF-TEST: plant a non-atomic read-modify-"
                         "write the serial oracle must catch")
    ap.add_argument("--ssi-scenario", default="off",
                    choices=("off", "tidrange_skew", "summarize_skew",
                             "all"),
                    help="scripted SSI write-skew schedules (upstream "
                         "repros); 'all' alternates scenarios per trial")
    ap.add_argument("--maint-ops", action="store_true",
                    help="mix autocommit maintenance verbs (VACUUM/"
                         "ANALYZE/CLUSTER/REINDEX/CHECKPOINT/REFRESH MV,"
                         " rare TRUNCATE) into the op stream at txn "
                         "boundaries; verb failures count as aborts")
    ap.add_argument("--ssi-churn", type=int, default=900,
                    help="committed serializable txns pushed onto session "
                         "2 for summarize_skew; summarization observed "
                         "past ~800 even at max_connections=10")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    pg = PostgresRunner(args.pg_datadir, pg_prefix=args.pg_prefix)
    uri = pg._server.get_uri()  # both server types expose get_uri()
    LOGGER.info("txn fuzz: uri=%s trials=%d sessions=%d iso=%s seed=%d",
                uri, args.trials, args.sessions, args.iso, args.seed)

    hits: list[dict] = []
    stats = {
        "trials": args.trials, "trials_completed": 0,
        "setup_failures": 0, "connect_failures": 0,
        "commits": 0, "aborts": 0,
        "serialization_failures": 0, "deadlocks": 0,
        "lock_timeouts": 0, "query_canceled": 0,
        "unique_violations": 0, "in_failed_txn_errors": 0,
        "txn_state_errors": 0, "conn_deaths": 0,
        "schema_missing_errors": 0, "compromised_trials": 0,
        "internal_errors": 0, "other_errors": 0,
        "savepoints": 0, "rollback_to_savepoints": 0, "set_iso_ops": 0,
        "maint_ops": 0, "maint_aborts": 0,
        "pair_reads_checked": 0, "serial_orders_checked": 0,
        "serial_orders_valid": 0, "nonserializable_states": 0,
        "unverifiable_trials": 0, "observe_failures": 0,
        "crash_probes": 0, "terminated_backends": 0,
        "hung_backends": 0, "harness_errors": 0, "hits": 0,
    }
    t0 = time.time()
    try:
        for trial in range(args.trials):
            run_trial(pg, uri, trial, args, stats, hits)
            if (trial + 1) % 20 == 0:
                LOGGER.info("trial %d/%d hits=%d ser_fail=%d deadlocks=%d",
                            trial + 1, args.trials, stats["hits"],
                            stats["serialization_failures"],
                            stats["deadlocks"])
    finally:
        pg.cleanup()
    with open(os.path.join(args.out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1, default=str)
    summary = {
        **stats,
        "config": {
            "iso": args.iso, "sessions": args.sessions,
            "ops_per_session": args.ops_per_session, "seed": args.seed,
            "inject_lost_update": args.inject_lost_update,
            "maint_ops": args.maint_ops,
            "ssi_scenario": args.ssi_scenario,
            "ssi_churn": args.ssi_churn,
            "crash_probe": args.crash_probe,
            "crash_every": args.crash_every,
            "lock_timeout_ms": args.lock_timeout_ms,
        },
        "elapsed_s": time.time() - t0,
        "efficiency": eff.snapshot(),
    }
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
