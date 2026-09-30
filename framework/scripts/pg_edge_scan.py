#!/usr/bin/env python3
"""Edge-construct internal-error scan for PostgreSQL.

Runs the pg_edge_targets corpus (or another --seed-module) and records
*internal-class* failures only:

- ``internal``: the probe itself raised XX000/assert/PANIC/crash-class
  errors (via ``looks_internal``).
- ``log_fatal``: TRAP/PANIC/signal/ASan lines appended to the server log
  — catches deaths invisible to the client session (parallel workers,
  checkpointer, bgwriter) after EVERY statement, unlike pg_error_scan.
- ``variant_internal``: internal error under a cheap planner variant
  while the default run is clean.
- ``baseline_internal``: the default run errors internally (ambiguous
  validity; kept for triage).

Variants (best-effort; a SET that fails skips that variant):
- ``parallel``: forced-parallel GUCs, probe repeated --repeat times
  (parallel-worker asserts are flaky).
- ``debug_par``: debug_parallel_query=regress (18+/master only).
- ``tiny_workmem``: work_mem=64kB stresses sort/hash/recheck paths.

Usage:
    python scripts/pg_edge_scan.py \
        --prefix $COEVO_PGBLD/pgmaster_assert \
        --out results/pg_edge_master --repeat 3
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.normalize import is_internal_error  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_edge_scan")

PARALLEL_PRELUDE = [
    "SET max_parallel_workers_per_gather = 4",
    "SET parallel_setup_cost = 0",
    "SET parallel_tuple_cost = 0",
    "SET min_parallel_table_scan_size = 0",
    "SET min_parallel_index_scan_size = 0",
]
PARALLEL_TEARDOWN = [
    "RESET max_parallel_workers_per_gather",
    "RESET parallel_setup_cost",
    "RESET parallel_tuple_cost",
    "RESET min_parallel_table_scan_size",
    "RESET min_parallel_index_scan_size",
]

# label -> (prelude, teardown, repetitions)
VARIANTS = [
    ("parallel", PARALLEL_PRELUDE, PARALLEL_TEARDOWN, 3),
    ("debug_par", ["SET debug_parallel_query = regress"],
     ["RESET debug_parallel_query"], 1),
    ("tiny_workmem", ["SET work_mem = '64kB'"], ["RESET work_mem"], 1),
]

_INTERNAL_MARKERS = (
    "xx000", "assert", "panic", "unexpected", "cache lookup failed",
    "unrecognized node", "variable not found in subplan",
    "no relation entry", "cannot compare", "could not find pathkey",
    "server closed the connection", "terminating connection",
    "connection not open", "could not receive data from server",
    "internalerror", "segv", "signal",
)


def looks_internal(error: str | None) -> bool:
    if not error:
        return False
    if is_internal_error(error):
        return True
    low = error.lower()
    return any(m in low for m in _INTERNAL_MARKERS)


def is_internal(result) -> bool:
    return result.is_internal_error or looks_internal(result.error)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--pg-datadir", default=None)
    ap.add_argument("--seed-module", default="seeds.pg_edge_targets")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    mod = importlib.import_module(args.seed_module)
    seeds = mod.as_seeds()
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("%d seeds from %s", len(seeds), args.seed_module)

    pg = PostgresRunner(args.pg_datadir or os.path.join(out, "datadir"),
                        pg_prefix=args.prefix)
    LOGGER.info("server %s", pg.engine_version)

    hits: list[dict] = []
    baseline_internal: list[dict] = []
    stats = {"seeds": 0, "setup_fail": 0, "exec_ok": 0, "baseline_err": 0,
             "baseline_internal": 0, "variant_runs": 0, "hits": 0,
             "log_fatal": 0, "timeouts": 0, "variant_skip": 0}
    t0 = time.time()
    fatal_seen: list[str] = []

    def drain_log(rec: dict, phase: str) -> bool:
        """Append new fatal log lines to hits; True when new fatals seen."""
        lines = pg.log_new_lines()
        fatal = pg.log_fatal_lines(lines)
        if not fatal:
            return False
        new = [ln for ln in fatal if ln not in fatal_seen]
        fatal_seen.extend(new)
        if new:
            stats["log_fatal"] += 1
            hits.append({"kind": "log_fatal", "phase": phase, **rec,
                         "log_lines": new[:40]})
            LOGGER.warning("LOG FATAL %s phase=%s: %s",
                           rec["source"], phase, new[0][:120])
        return bool(new)

    try:
        for i, seed in enumerate(seeds):
            stats["seeds"] += 1
            q = seed["query"].strip().rstrip(";")
            rec = {"source": seed["source"], "query": q,
                   "setup_sqls": list(seed["setup_sqls"])}
            pg.log_new_lines()  # sync offset; attribute lines correctly
            outcomes = pg.setup(seed["setup_sqls"])
            setup_errs = [e for _, e in outcomes if e]
            drain_log(rec, "setup")
            if setup_errs:
                stats["setup_fail"] += 1
                rec2 = {**rec, "setup_errors": setup_errs[:3]}
                if any(looks_internal(e) for e in setup_errs):
                    stats["hits"] += 1
                    hits.append({"kind": "setup_internal", **rec2})
                with open(os.path.join(out, "cases.jsonl"), "a") as fh:
                    fh.write(json.dumps(
                        {"verdict": "setup_fail", **rec2}, default=str) + "\n")
                continue

            baseline = pg.run(q, timeout_s=10.0)
            drain_log(rec, "baseline")
            if baseline.timed_out:
                stats["timeouts"] += 1
            if is_internal(baseline):
                stats["baseline_internal"] += 1
                baseline_internal.append({**rec, "error": baseline.error})
                LOGGER.warning("BASELINE INTERNAL %s: %s",
                               seed["source"], (baseline.error or "")[:120])
                continue
            baseline_ordinary_err = not baseline.ok
            if baseline_ordinary_err:
                stats["baseline_err"] += 1
            else:
                stats["exec_ok"] += 1
            eff.count("queries_executed")

            verdict = "ok"
            variant_notes = []
            for label, prelude, teardown, reps in VARIANTS:
                skip = False
                for stmt in prelude:
                    r = pg.run(stmt, timeout_s=5.0)
                    if not r.ok:
                        skip = True
                        break
                if skip:
                    stats["variant_skip"] += 1
                    continue
                hit = False
                for attempt in range(
                        args.repeat if label == "parallel" else reps):
                    res = pg.run(q, timeout_s=10.0)
                    stats["variant_runs"] += 1
                    eff.count("variant_executions")
                    drain_log(rec, f"{label}#{attempt}")
                    if res.timed_out:
                        stats["timeouts"] += 1
                        break
                    if is_internal(res):
                        stats["hits"] += 1
                        hits.append({
                            "kind": "variant_internal",
                            "variant": label, "attempt": attempt, **rec,
                            "error": res.error,
                            "baseline_rows":
                                [list(r) for r in baseline.rows][:20]})
                        LOGGER.warning(
                            "INTERNAL %s variant=%s attempt=%d: %s",
                            seed["source"], label, attempt,
                            (res.error or "")[:120])
                        verdict = "internal"
                        hit = True
                        break
                    if not res.ok:
                        break  # ordinary error: pointless to repeat
                for stmt in teardown:
                    pg.run(stmt, timeout_s=5.0)
                if hit:
                    variant_notes.append(label)
                    break
            rec_out = {"verdict": ("ordinary_error" if
                                   baseline_ordinary_err and
                                   verdict == "ok" else verdict), **rec}
            if baseline_ordinary_err:
                rec_out["baseline_error"] = baseline.error
            if variant_notes:
                rec_out["variant"] = variant_notes
            with open(os.path.join(out, "cases.jsonl"), "a") as fh:
                fh.write(json.dumps(rec_out, default=str) + "\n")
            if (i + 1) % 40 == 0:
                LOGGER.info("seed %d/%d exec_ok=%d hits=%d log_fatal=%d",
                            i + 1, len(seeds), stats["exec_ok"],
                            stats["hits"], stats["log_fatal"])
    finally:
        pg.cleanup()

    with open(os.path.join(out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1, default=str)
    with open(os.path.join(out, "baseline_internal.jsonl"), "w") as fh:
        for r in baseline_internal:
            fh.write(json.dumps(r, default=str) + "\n")
    summary = {**stats, "elapsed_s": round(time.time() - t0, 1),
               "prefix": args.prefix,
               "efficiency": eff.snapshot()}
    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
