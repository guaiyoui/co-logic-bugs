"""Flaky-bug miner: repeat plan-variant pairs N times per query and keep
probabilistic inconsistencies (0 < hit rate < 1).

The pervasive/CTE family proved this class exists and is under-detected:
deterministic fuzzers never see a bug that only fires ~2-20% of runs.
Each flaky candidate is then pushed through the normal pipeline (fixer
triage -> sigma -> family archive) and flagged for --repeat verification.

    python scripts/flaky_mine.py --engine duckdb --repeat 10 \
        --max-seeds 200 --out results/flaky_run
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.bug_fixer import BugFixer  # noqa: E402
from diagnosis.signature import SignatureEngine  # noqa: E402
from evolution.co_evolution import CoEvolution  # noqa: E402
from evolution.dce import FamilyExpander  # noqa: E402
from llm.ledger import ledger  # noqa: E402
from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.determinism import is_usable_for_oracles  # noqa: E402
from oracles.plan_variant import PlanVariantOracle  # noqa: E402
from seeds.mutate import SeedMutator  # noqa: E402
from seeds.store import SeedCorpus  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("flaky_mine")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["duckdb", "postgres"], default="duckdb")
    ap.add_argument("--corpus", default="seeds/corpus.json")
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--max-per-seed", type=int, default=4)
    ap.add_argument("--repeat", type=int, default=10)
    ap.add_argument("--dce-budget", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rule-targets", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    out_dir = args.out or os.path.join(
        "results", f"flaky_{args.engine}_{time.strftime('%Y%m%d_%H%M%S')}"
    )
    os.makedirs(out_dir, exist_ok=True)
    ledger.configure(os.path.join(out_dir, "llm_ledger.jsonl"))

    runner_factory = lambda: DuckDBRunner(version_tag="flaky")  # noqa: E731
    fixer = BugFixer(
        {"model": "none", "api_key": "", "disable_llm": True},
        runner_factory=runner_factory, reference_factory=None,
        target_engine=args.engine,
    )
    signature = SignatureEngine(
        runner_factory=runner_factory, checker=fixer.checker,
        engine=args.engine,
    )
    expander = FamilyExpander(
        runner_factory=runner_factory, engine=args.engine,
        max_probes=150, max_executed=80,
    )
    coevo = CoEvolution(
        hunter=None, fixer=fixer, guideline_generator=None,
        results_dir=out_dir, signature_engine=signature,
        expander=expander, dce_budget=args.dce_budget,
    )

    corpus = SeedCorpus.load(args.corpus)
    seeds = corpus.for_engine("duckdb").seeds
    if args.rule_targets:
        from seeds.rule_targets import as_seeds
        from seeds.store import Seed
        seeds += [
            Seed(setup_sqls=s["setup_sqls"], query=s["query"],
                 source=s["source"], engine=s["engine"], tags=s["tags"])
            for s in as_seeds()
        ]
    import random
    rng = random.Random(args.seed)
    rng.shuffle(seeds)
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("flaky mining %d seeds x%d repeats", len(seeds), args.repeat)

    mutator = SeedMutator(random.Random(args.seed + 7))
    pv = PlanVariantOracle(args.engine)
    stats = collections.Counter()
    t0 = time.time()
    runner = runner_factory()
    try:
        for i, seed in enumerate(seeds):
            muts = mutator.mutations_for(
                seed, max_per_seed=args.max_per_seed)
            stats["seeds"] += 1
            for mut in muts:
                outcomes = runner.setup(mut.setup_sqls)
                if any(err for _, err in outcomes):
                    stats["setup_fail"] += 1
                    continue
                full = mut.select_from if mut.predicate is None else mut.select_from
                q = mut.select_from
                if mut.predicate:
                    from oracles.tlp import inject_predicate
                    q = inject_predicate(mut.select_from, mut.predicate)
                if not is_usable_for_oracles(q, runner, mut.setup_sqls):
                    stats["skipped_nondet"] += 1
                    continue
                stats["exec_ok"] += 1
                hits = 0
                first_hit = None
                with eff.phase("flaky_repeat"):
                    for _ in range(args.repeat):
                        h = pv.check(runner, q, mut.setup_sqls, [],
                                     category="flaky:" + mut.category)
                        if h is not None:
                            hits += 1
                            if first_hit is None:
                                first_hit = h
                if hits == 0:
                    continue
                rate = hits / args.repeat
                stats["flaky_cases"] += 1
                stats[f"rate_bucket_{int(rate * 10)}"] += 1
                LOGGER.info(
                    "flaky %d/%d rate=%.2f seed=%s cat=%s",
                    hits, args.repeat, rate, seed.source, mut.category)
                if first_hit is not None:
                    verdict, key = coevo.ingest_hit(
                        first_hit, iteration=f"flaky{i}")
                    stats[f"verdict_{verdict}"] += 1
                    if verdict == "true_bug":
                        LOGGER.info("  -> family %s", key)
            if (i + 1) % 25 == 0:
                LOGGER.info(
                    "seed %d/%d exec=%d flaky=%d (%s)",
                    i + 1, len(seeds), stats["exec_ok"],
                    stats["flaky_cases"],
                    time.strftime("%H:%M:%S"),
                )
                coevo._write_families()
    finally:
        runner.close()
        coevo._write_families()
        with open(os.path.join(out_dir, "bugs.jsonl"), "w") as fh:
            for b in coevo.true_bugs:
                fh.write(json.dumps(b, default=str) + "\n")

    summary = {
        **dict(stats),
        "distinct_families": len(coevo.bug_index),
        "dce_executed": coevo.dce_executed,
        "efficiency": eff.snapshot(),
        "ledger": ledger.totals(),
        "elapsed_s": time.time() - t0,
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    print(f"Artifacts in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
