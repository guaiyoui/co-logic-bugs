"""Sibling schedules around the summarize_skew SSI write-skew bug
(Brazeal 2026-08): once OldCommittedSxact summarization kicks in,
CheckTargetForConflictsIn ignores summarized SIREAD locks -> a SIREAD
held across the churn loses its rw-antidependency and the long txn
commits where it must abort.

This driver reuses scripts/pg_txn_fuzz.py's rendezvous machinery
(TxnSession + run_trial + serial oracle) but substitutes custom op
schedules via a monkeypatched gen_ssi_trial:

  baseline      verbatim schedule (control)
  swap_roles    session0 = early-committer+churn, session1 = long txn
  pred_low      SIREAD predicate v < 300 (initial rows match)
  pred_none     SIREAD predicate v >= 10000 (nothing ever matches;
                seqscan still takes relation-level SIREAD)
  churn_writes  churn = committed WRITE txns on t2 (UPDATE v=v) —
                do write-only commits count toward summarization?
                (server-side real writes, journaled as session_set so
                the 2-txn serial oracle stays small)
  churn_reads_t1 churn = SELECT count(*) FROM t1 — reads on the
                conflicted table instead of a bare SELECT 1
  churn_300 / churn_600  verbatim shape at lower churn (threshold map)

A pg_locks probe op is appended to S1 right after the churn: it records
(total SIReadLock count, summarized -1/0 SIReadLock count) inside the
hit context so we can see whether OldCommittedSxact summarization
actually ran before S1's commit.

Usage:
    python scripts/pg_ssi_sibling.py \
        --pg-prefix $COEVO_PGBLD/pg186_assert \
        --variant swap_roles --trials 3 --out results/x
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.pg_txn_fuzz as fuzz  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402

PRED_HI = "v >= 300"
PRED_LO = "v < 300"
PRED_NONE = "v >= 10000"

LOCKS_PROBE = (
    "SELECT (SELECT count(*) FROM pg_locks WHERE mode='SIReadLock'), "
    "(SELECT count(*) FROM pg_locks WHERE mode='SIReadLock' "
    "AND virtualtransaction='-1/0')"
)


def sel(sql):
    return {"kind": "select", "sql": [sql]}


def sset(sql):
    """session_set: runs arbitrary SQL with no mut journaling — used for
    churn write-txns that must be real commits server-side but must not
    enter the 2-txn serial replay."""
    return {"kind": "session_set", "sql": [sql]}


def cond_update(k, v):
    return {
        "kind": "cond_update",
        "sql": [f"UPDATE t1 SET v = {v} WHERE k = {k} AND NOT EXISTS "
                f"(SELECT 1 FROM t1 WHERE TRUE AND v >= 300)"],
        "mut": ["cond_set", "t1", k, v, 300],
    }


COMMIT = {"kind": "commit", "sql": ["COMMIT"]}


def _churn_ops(kind):
    if kind == "select1":
        return [sel("SELECT 1"), COMMIT]
    if kind == "write_t2":
        return [sset("UPDATE t2 SET v = v WHERE k = 1"), COMMIT]
    if kind == "read_t1":
        return [sel("SELECT count(*) FROM t1"), COMMIT]
    raise ValueError(kind)


def build(variant, churn):
    """Return (ops_lists, schedule) for a 2-session summarize_skew variant."""
    if variant == "baseline":
        pred, swap, churnkind = PRED_HI, False, "select1"
    elif variant == "swap_roles":
        pred, swap, churnkind = PRED_HI, True, "select1"
    elif variant == "pred_low":
        pred, swap, churnkind = PRED_LO, False, "select1"
    elif variant == "pred_none":
        pred, swap, churnkind = PRED_NONE, False, "select1"
    elif variant == "churn_writes":
        pred, swap, churnkind = PRED_HI, False, "write_t2"
    elif variant == "churn_reads_t1":
        pred, swap, churnkind = PRED_HI, False, "read_t1"
    elif variant in ("churn_300", "churn_600"):
        pred, swap, churnkind = PRED_HI, False, "select1"
        churn = int(variant.split("_")[1])
    else:
        raise ValueError(f"unknown variant {variant}")

    readq = f"SELECT count(*) FROM t1 WHERE {pred}"
    # long-txn session: read, [probe], cond_update, read, commit
    long_ops = [sel(readq), sel(LOCKS_PROBE), cond_update(2, 500),
                sel(readq), COMMIT]
    # early-committer session: read, cond_update, commit, then churn
    early_ops = [sel(readq), cond_update(1, 400), COMMIT]
    for _ in range(churn):
        early_ops += _churn_ops(churnkind)

    if not swap:
        ops = [long_ops, early_ops]
        # S1 op0 first, then ALL of S2 (incl. churn), then S1 rest
        schedule = [0] + [1] * len(early_ops) + [0] * (len(long_ops) - 1)
    else:
        ops = [early_ops, long_ops]
        schedule = [1] + [0] * len(early_ops) + [1] * (len(long_ops) - 1)
    return ops, schedule


def patched_gen(rng, sessions, scen, churn):
    ops, schedule = build(scen, churn)
    for sid in range(2, sessions):  # benign tail sessions (unused)
        t = "t1"
        ops.append([sel(f"SELECT k, v FROM {t} WHERE k BETWEEN 1 AND 5"),
                    COMMIT])
        schedule += [sid, sid]
    return ops, schedule


def stats_template():
    return {
        "trials": 0, "trials_completed": 0,
        "setup_failures": 0, "connect_failures": 0,
        "commits": 0, "aborts": 0,
        "serialization_failures": 0, "deadlocks": 0,
        "lock_timeouts": 0, "query_canceled": 0,
        "unique_violations": 0, "in_failed_txn_errors": 0,
        "txn_state_errors": 0, "conn_deaths": 0,
        "schema_missing_errors": 0, "compromised_trials": 0,
        "internal_errors": 0, "other_errors": 0,
        "savepoints": 0, "rollback_to_savepoints": 0, "set_iso_ops": 0,
        "pair_reads_checked": 0, "serial_orders_checked": 0,
        "serial_orders_valid": 0, "nonserializable_states": 0,
        "unverifiable_trials": 0, "observe_failures": 0,
        "crash_probes": 0, "terminated_backends": 0,
        "hung_backends": 0, "harness_errors": 0, "hits": 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-prefix", required=True)
    ap.add_argument("--pg-datadir", required=True)
    ap.add_argument("--variant", required=True)
    ap.add_argument("--churn", type=int, default=900)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    fuzz.gen_ssi_trial = patched_gen
    targs = argparse.Namespace(
        iso="serializable", sessions=2, ops_per_session=8,
        seed=args.seed, inject_lost_update=False,
        ssi_scenario=args.variant, ssi_churn=args.churn,
        crash_probe=False, crash_every=10, maint_ops=False,
        lock_timeout_ms=800, deadlock_timeout_ms=150,
        statement_timeout_ms=20000, step_timeout_s=60.0)

    pg = PostgresRunner(args.pg_datadir, pg_prefix=args.pg_prefix)
    uri = pg._server.get_uri()
    stats = stats_template()
    stats["trials"] = args.trials
    hits: list[dict] = []
    t0 = time.time()
    try:
        for trial in range(args.trials):
            fuzz.run_trial(pg, uri, trial, targs, stats, hits)
    finally:
        pg.cleanup()

    # surface the lock-probe evidence from each hit/trial context
    for h in hits:
        for e in h.get("events", []):
            if e.get("sql") and LOCKS_PROBE in e["sql"]:
                h["locks_probe"] = e.get("bag")
    summary = {**stats, "variant": args.variant, "churn": args.churn,
               "elapsed_s": round(time.time() - t0, 2),
               "hit_kinds": [h["kind"] for h in hits]}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    with open(os.path.join(args.out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1, default=str)
    print(json.dumps(summary, indent=1))
    for h in hits:
        print("  HIT", h["kind"], "locks_probe=", h.get("locks_probe"),
              "observed_t1=", (h.get("observed_state") or {}).get("t1"))


if __name__ == "__main__":
    main()
