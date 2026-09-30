"""plan_cache_mode oracle for PostgreSQL.

A prepared statement runs custom plans for its first executions and may
switch to a generic (parameter-agnostic) plan afterwards. The two plan
classes must return identical results; comparing
``plan_cache_mode=force_custom_plan`` vs ``force_generic_plan`` on the
same PREPAREd query is a sound differential oracle that ordinary
plan-variant sweeps never reach.

Usage:
    python scripts/pg_plan_cache.py --out results/pg_cache_1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.normalize import loose_bag  # noqa: E402
from seeds.pg_targets import as_seeds  # noqa: E402
from seeds.store import Seed, SeedCorpus  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_plan_cache")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_cache")
    ap.add_argument("--corpus", default="seeds/corpus.json")
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--param-seeds", default=None,
                    help="python module with PARAM_SEEDS: parameterized "
                         "PREPARE/EXECUTE cases — the only seeds that make "
                         "generic-vs-custom a real differential")
    ap.add_argument("--out", default="results/pg_cache_1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    corpus = SeedCorpus.load(args.corpus)
    seeds = [s for s in corpus.seeds
             if s.query and "SELECT" in s.query.upper()]
    seeds += [Seed(**d) for d in as_seeds()]
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("plan-cache oracle over %d seeds", len(seeds))

    pg = PostgresRunner(args.pg_datadir)
    hits: list[dict] = []
    stats = {"seeds": 0, "exec_ok": 0, "prepare_ok": 0, "hits": 0,
             "skipped_nondet": 0}
    t0 = time.time()
    try:
        for i, seed in enumerate(seeds):
            stats["seeds"] += 1
            outcomes = pg.setup(seed.setup_sqls)
            if any(err for _, err in outcomes):
                continue
            q = seed.query.strip().rstrip(";")
            # PREPARE requires plain SELECT with $ params; skip queries
            # with placeholders or non-SELECT heads.
            if not q.upper().lstrip().startswith(("SELECT", "WITH", "TABLE",
                                                  "VALUES")):
                continue
            stats["exec_ok"] += 1
            eff.count("queries_executed")
            prep = pg.run(f"PREPARE coevo_q AS {q}", timeout_s=10.0)
            if not prep.ok:
                continue
            stats["prepare_ok"] += 1
            try:
                pg.run("SET plan_cache_mode = force_custom_plan")
                custom = pg.run("EXECUTE coevo_q", timeout_s=10.0)
                pg.run("SET plan_cache_mode = force_generic_plan")
                generic = pg.run("EXECUTE coevo_q", timeout_s=10.0)
                pg.run("RESET plan_cache_mode")
            finally:
                pg.run("DEALLOCATE coevo_q")
            if not custom.ok or not generic.ok:
                continue
            if loose_bag(custom.rows) != loose_bag(generic.rows):
                stats["hits"] += 1
                rec = {
                    "source": seed.source,
                    "query": q,
                    "setup_sqls": list(seed.setup_sqls),
                    "custom_rows": [list(r) for r in custom.rows][:20],
                    "generic_rows": [list(r) for r in generic.rows][:20],
                }
                hits.append(rec)
                LOGGER.info("DIVERGENCE %s", seed.source)
            if (i + 1) % 40 == 0:
                LOGGER.info("seed %d/%d hits=%d", i + 1, len(seeds),
                            stats["hits"])
    finally:
        pg.cleanup()

    if args.param_seeds:
        stats["param_seeds"] = 0
        stats["param_execs"] = 0
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "coevo_param_seeds", args.param_seeds)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pg2 = PostgresRunner(args.pg_datadir + "_param")
        try:
            for case in mod.PARAM_SEEDS:
                stats["param_seeds"] += 1
                outcomes = pg2.setup(case["setup_sqls"])
                if any(err for _, err in outcomes):
                    continue
                types = ",".join(case["types"])
                prep = pg2.run(
                    f"PREPARE coevo_p({types}) AS {case['query']}",
                    timeout_s=10.0)
                if not prep.ok:
                    continue
                try:
                    for params in case["exec_params"]:
                        args_s = ",".join(
                            "NULL" if p is None else repr(p)
                            for p in params)
                        ex = f"EXECUTE coevo_p({args_s})"
                        pg2.run("SET plan_cache_mode = force_custom_plan")
                        custom = pg2.run(ex, timeout_s=10.0)
                        pg2.run("SET plan_cache_mode = force_generic_plan")
                        generic = pg2.run(ex, timeout_s=10.0)
                        pg2.run("RESET plan_cache_mode")
                        if not custom.ok or not generic.ok:
                            continue
                        stats["param_execs"] += 1
                        if loose_bag(custom.rows) != loose_bag(generic.rows):
                            stats["hits"] += 1
                            hits.append({
                                "source": f"param:{case['name']}",
                                "query": case["query"],
                                "params": params,
                                "custom_rows": [list(r)
                                                for r in custom.rows][:20],
                                "generic_rows": [list(r)
                                                 for r in generic.rows][:20],
                            })
                            LOGGER.info("PARAM DIVERGENCE %s %s",
                                        case["name"], params)
                finally:
                    pg2.run("DEALLOCATE coevo_p")
        finally:
            pg2.cleanup()

    with open(os.path.join(args.out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1)
    summary = {**stats, "elapsed_s": time.time() - t0,
               "efficiency": eff.snapshot()}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
