"""Exhaustive zero-LLM seed sweep: run every corpus seed's rule-mutations
through the sound oracles (TLP, NoREC, plan-variant) on one engine.

Usage:
    python scripts/seed_sweep.py --engine duckdb [--max-seeds N] [--out DIR]
    python scripts/seed_sweep.py --engine postgres --pg-datadir /tmp/pg_sweep
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.determinism import is_usable_for_oracles  # noqa: E402
from oracles.norec import NoRECOracle  # noqa: E402
from oracles.plan_variant import PlanVariantOracle  # noqa: E402
from oracles.tlp import TLPOracle, inject_predicate  # noqa: E402
from seeds.mutate import SeedMutator  # noqa: E402
from seeds.store import SeedCorpus  # noqa: E402

LOGGER = logging.getLogger("seed_sweep")


def make_runner(engine: str, datadir: str | None):
    if engine == "postgres":
        from targets.postgres_runner import PostgresRunner

        return PostgresRunner(datadir or "/tmp/coevo_pg_sweep")
    return DuckDBRunner()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["duckdb", "postgres"], default="duckdb")
    ap.add_argument("--corpus", default="seeds/corpus.json")
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--max-per-seed", type=int, default=6)
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_sweep")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    out_dir = args.out or os.path.join(
        "results", f"sweep_{args.engine}_{time.strftime('%Y%m%d_%H%M%S')}"
    )
    os.makedirs(out_dir, exist_ok=True)

    corpus = SeedCorpus.load(args.corpus)
    seeds = corpus.for_engine(
        "duckdb" if args.engine == "duckdb" else "postgres"
    ).seeds
    rng = random.Random(args.seed)
    rng.shuffle(seeds)
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("%d %s seeds to sweep", len(seeds), args.engine)

    mutator = SeedMutator(rng)
    tlp, norec, pv = TLPOracle(), NoRECOracle(), PlanVariantOracle()
    runner = make_runner(args.engine, args.pg_datadir)

    hits_path = os.path.join(out_dir, "hits.jsonl")
    stats = {
        "seeds": 0, "mutations": 0, "exec_ok": 0,
        "skipped_nondet": 0, "hits": 0,
    }
    t0 = time.time()
    try:
        with open(hits_path, "a") as hits:
            for i, seed in enumerate(seeds):
                muts = mutator.mutations_for(seed, max_per_seed=args.max_per_seed)
                stats["seeds"] += 1
                for mut in muts:
                    stats["mutations"] += 1
                    outcomes = runner.setup(mut.setup_sqls)
                    if any(err for _, err in outcomes):
                        continue
                    full = (
                        inject_predicate(mut.select_from, mut.predicate)
                        if mut.predicate
                        else mut.select_from
                    )
                    if not is_usable_for_oracles(full, runner, mut.setup_sqls):
                        stats["skipped_nondet"] += 1
                        continue
                    stats["exec_ok"] += 1
                    for name, hit in (
                        ("tlp", tlp.check(
                            runner, mut.select_from, mut.predicate,
                            mut.setup_sqls, [], category=mut.category)),
                        ("norec", norec.check(
                            runner, mut.select_from, mut.predicate,
                            mut.setup_sqls, [], category=mut.category)),
                        ("plan_variant", pv.check(
                            runner, full, mut.setup_sqls, [],
                            category=mut.category)),
                    ):
                        if hit is not None:
                            stats["hits"] += 1
                            hits.write(json.dumps(
                                {"oracle": name, "source": mut.source,
                                 "seed_query": seed.query[:300],
                                 "candidate": hit.to_dict()},
                                default=str) + "\n")
                            hits.flush()
                if (i + 1) % 50 == 0:
                    rate = stats["mutations"] / max(time.time() - t0, 0.01)
                    LOGGER.info(
                        "seed %d/%d mutations=%d exec=%d hits=%d (%.1f mut/s)",
                        i + 1, len(seeds), stats["mutations"],
                        stats["exec_ok"], stats["hits"], rate,
                    )
    finally:
        runner.close()
        if hasattr(runner, "cleanup"):
            try:
                runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
    with open(os.path.join(out_dir, "sweep_summary.json"), "w") as fh:
        json.dump({**stats, "elapsed_s": time.time() - t0}, fh, indent=1)
    LOGGER.info("done: %s", stats)
    print(f"Hits written to {hits_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
