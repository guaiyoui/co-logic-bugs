#!/usr/bin/env python3
"""PostgreSQL index-presence sweep: run every seed query under storage
state variants (index on/off, covering/index-only-scan, partial,
expression, BRIN, GIN, HOT-update chains, ANALYZE shifts) and report
divergences.

    python scripts/pg_index_hunt.py --out results/pg_idx --limit 200 \
        [--seed-file seeds/pg_targets.py] [--datlab x]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.determinism import is_usable_for_oracles  # noqa: E402
from oracles.index_variant import IndexVariantOracle  # noqa: E402
from seeds.store import SeedCorpus  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_index_hunt")


def load_seeds(args) -> list[dict]:
    seeds = []
    if not args.no_corpus and os.path.isfile(args.corpus):
        seeds += [s.to_dict()
                  for s in SeedCorpus.load(args.corpus).seeds]
    if not args.no_rule_targets:
        try:
            from seeds import pg_targets
            seeds += pg_targets.as_seeds()
        except Exception as exc:
            LOGGER.warning("pg_targets load failed: %s", exc)
    if args.seed_file:
        spec = importlib.util.spec_from_file_location(
            "custom_seeds", args.seed_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        seeds += mod.as_seeds()
    # drop seeds with no SELECT-ish query
    seeds = [s for s in seeds if s.get("query", "").lstrip().lower()
             .startswith(("select", "with", "values", "table"))]
    return seeds[: args.limit or None]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--corpus",
                    default=os.path.join(ROOT := os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__))),
                        "seeds", "corpus.json"))
    ap.add_argument("--seed-file", default=None)
    ap.add_argument("--no-corpus", action="store_true")
    ap.add_argument("--no-rule-targets", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--datadir", default="/tmp/coevo_pg_idxhunt")
    ap.add_argument("--max-variants", type=int, default=12)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    seeds = load_seeds(args)
    LOGGER.info("index-presence sweep: %d seeds on postgres", len(seeds))

    runner = PostgresRunner(args.datadir)
    oracle = IndexVariantOracle(max_variants=args.max_variants)
    stats = {"seeds": len(seeds), "screened": 0, "skipped": 0,
             "setup_err": 0, "divergent": 0}
    divergences = []
    try:
        for i, seed in enumerate(seeds):
            setup, query = seed["setup_sqls"], seed["query"]
            outcomes = runner.setup(setup)
            if any(err for _, err in outcomes):
                stats["setup_err"] += 1
                continue
            if not is_usable_for_oracles(query, runner, setup):
                stats["skipped"] += 1
                continue
            stats["screened"] += 1
            hit = oracle.check(
                runner, query, setup, [],
                category=seed.get("category", "seed"),
            )
            if hit:
                stats["divergent"] += 1
                divergences.append({
                    "seed": seed.get("source", "?"),
                    "query": query, "setup": setup,
                    "r1": hit.r1_summary, "r2": hit.r2_summary,
                    "notes": hit.notes,
                })
                LOGGER.info("DIVERGENT %s: %s",
                            seed.get("source"), hit.notes)
            if (i + 1) % 25 == 0:
                LOGGER.info("%d/%d div=%d", i + 1, len(seeds),
                            stats["divergent"])
    finally:
        runner.cleanup()

    with open(os.path.join(args.out, "divergences.jsonl"), "w") as fh:
        for d in divergences:
            fh.write(json.dumps(d, default=str) + "\n")
    summary = {**stats, "efficiency": eff.snapshot(),
               "variants_applied": oracle._applied,
               "variants_noop": oracle._noop,
               "elapsed_s": None}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
