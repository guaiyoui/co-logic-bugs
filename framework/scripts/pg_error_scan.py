"""Internal-error / crash scan for PostgreSQL.

Runs every seed query under each POSTGRES_VARIANTS planner-GUC prelude
(imported at call time — the list may gain entries between runs) and
under a forced-parallel prelude repeated N times. Any *internal-class*
error — server crash, XX000, assertion/PANIC, planner bookkeeping
failures ("cache lookup failed", "unrecognized node", "variable not
found in subplan", "no relation entry", "could not find pathkey", ...)
— raised while the DEFAULT run succeeds is a hit. Ordinary SQL errors
(syntax, type, constraint, value-dependent) are NOT hits.

Two failure classes are recorded separately:
- ``variant_internal``: internal error under a planner variant.
- ``parallel_flaky``: internal error under forced-parallel GUCs on a
  repeat — parallel-scheduling bugs are intermittent, single runs miss
  them.
- ``baseline_internal`` (not counted as hits, kept in
  baseline_internal.jsonl): the default run itself errors internally —
  ambiguous validity, triage only.

Usage:
    python scripts/pg_error_scan.py --out results/pg_err_1 --repeat 5
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

import oracles.plan_variant as plan_variant  # noqa: E402
from oracles.normalize import is_internal_error  # noqa: E402
from seeds.pg_targets import as_seeds  # noqa: E402
from seeds.store import Seed, SeedCorpus  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_error_scan")

# force_parallel_mode was removed in PG 16; force parallelism through
# the cost knobs instead.
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

# See pg_cursor_oracle.looks_internal for why the stock is_internal_error
# alone is insufficient (psycopg2 names XX000 errors "InternalError").
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_errscan")
    ap.add_argument("--corpus", default="seeds/corpus.json")
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--repeat", type=int, default=5,
                    help="repetitions under forced-parallel GUCs")
    ap.add_argument("--out", default="results/pg_err_1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    # Imported/reloaded at call time: POSTGRES_VARIANTS may gain entries
    # from another agent between runs; never hardcode a copy. A reload
    # failure (e.g. mid-edit file) falls back to the imported module.
    try:
        variants = list(importlib.reload(plan_variant).POSTGRES_VARIANTS)
    except Exception:  # noqa: BLE001
        variants = list(plan_variant.POSTGRES_VARIANTS)
    LOGGER.info("sweeping %d plan variants + %d parallel repeats",
                len(variants), args.repeat)

    corpus = SeedCorpus.load(args.corpus)
    seeds = [s for s in corpus.seeds
             if s.query and "SELECT" in s.query.upper()]
    seeds += [Seed(**d) for d in as_seeds()]
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("error scan over %d seeds", len(seeds))

    pg = PostgresRunner(args.pg_datadir)
    hits: list[dict] = []
    baseline_internal: list[dict] = []
    stats = {"seeds": 0, "exec_ok": 0, "variant_runs": 0,
             "parallel_runs": 0, "hits": 0,
             "baseline_internal": 0, "variant_timeouts": 0}
    t0 = time.time()
    try:
        for i, seed in enumerate(seeds):
            stats["seeds"] += 1
            q = seed.query.strip().rstrip(";")
            if not q.upper().lstrip().startswith(("SELECT", "WITH", "TABLE",
                                                  "VALUES")):
                continue
            outcomes = pg.setup(seed.setup_sqls)
            if any(err for _, err in outcomes):
                continue
            baseline = pg.run(q, timeout_s=10.0)
            if _is_internal(baseline):
                stats["baseline_internal"] += 1
                baseline_internal.append({
                    "source": seed.source, "query": q,
                    "setup_sqls": list(seed.setup_sqls),
                    "error": baseline.error,
                })
                continue
            if not baseline.ok or baseline.timed_out:
                continue
            stats["exec_ok"] += 1
            eff.count("queries_executed")

            # --- pass 1: each planner variant, internal errors only ---
            hit_found = False
            for label, setup_stmts, teardown_stmts in variants:
                for stmt in setup_stmts:
                    pg.run(stmt, timeout_s=5.0)
                variant = pg.run(q, timeout_s=10.0)
                stats["variant_runs"] += 1
                eff.count("variant_executions")
                for stmt in teardown_stmts:
                    pg.run(stmt, timeout_s=5.0)
                if variant.timed_out:
                    stats["variant_timeouts"] += 1
                    continue
                if _is_internal(variant):
                    stats["hits"] += 1
                    hits.append({
                        "kind": "variant_internal",
                        "variant": label,
                        "source": seed.source,
                        "query": q,
                        "setup_sqls": list(seed.setup_sqls),
                        "error": variant.error,
                        "baseline_rows":
                            [list(r) for r in baseline.rows][:20],
                    })
                    LOGGER.info("INTERNAL ERROR %s variant=%s",
                                seed.source, label)
                    hit_found = True
                    break

            # --- pass 2: forced-parallel repeat (flaky scheduler bugs) ---
            if not hit_found:
                for stmt in PARALLEL_PRELUDE:
                    pg.run(stmt, timeout_s=5.0)
                for attempt in range(args.repeat):
                    res = pg.run(q, timeout_s=10.0)
                    stats["parallel_runs"] += 1
                    eff.count("parallel_executions")
                    if res.timed_out:
                        stats["variant_timeouts"] += 1
                        break
                    if _is_internal(res):
                        stats["hits"] += 1
                        hits.append({
                            "kind": "parallel_flaky",
                            "variant": "force_parallel",
                            "attempt": attempt,
                            "source": seed.source,
                            "query": q,
                            "setup_sqls": list(seed.setup_sqls),
                            "error": res.error,
                            "baseline_rows":
                                [list(r) for r in baseline.rows][:20],
                        })
                        LOGGER.info("FLAKY INTERNAL %s attempt=%d",
                                    seed.source, attempt)
                        break
                    if not res.ok:
                        break  # ordinary error: no point repeating
                for stmt in PARALLEL_TEARDOWN:
                    pg.run(stmt, timeout_s=5.0)
            if (i + 1) % 40 == 0:
                LOGGER.info("seed %d/%d hits=%d", i + 1, len(seeds),
                            stats["hits"])
    finally:
        pg.cleanup()
    with open(os.path.join(args.out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1)
    with open(os.path.join(args.out, "baseline_internal.jsonl"), "w") as fh:
        for rec in baseline_internal:
            fh.write(json.dumps(rec, default=str) + "\n")
    summary = {**stats, "repeat": args.repeat, "elapsed_s": time.time() - t0,
               "efficiency": eff.snapshot()}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
