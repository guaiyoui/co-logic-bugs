"""Deterministic corpus hunt: sweep every seed's rule-mutations through the
sound oracles; each hit goes through the full pipeline — fixer minimize +
verdict, sigma signature, family dedup, then DCE fault-surface expansion.

Zero LLM calls by default (fixer runs with disable_llm). Usage:

    python scripts/corpus_hunt.py --engine duckdb --max-per-seed 6
    python scripts/corpus_hunt.py --engine duckdb --max-seeds 200 --out X
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
from util.paths import OLD_VENV  # noqa: E402

from agents.bug_fixer import BugFixer  # noqa: E402
from diagnosis.signature import SignatureEngine  # noqa: E402
from evolution.co_evolution import CoEvolution  # noqa: E402
from evolution.dce import FamilyExpander  # noqa: E402
from llm.ledger import ledger  # noqa: E402
from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.determinism import is_usable_for_oracles  # noqa: E402
from oracles.differential import DifferentialOracle  # noqa: E402
from oracles.norec import NoRECOracle  # noqa: E402
from oracles.plan_variant import PlanVariantOracle  # noqa: E402
from oracles.tlp import TLPOracle, inject_predicate  # noqa: E402
from seeds.mutate import Mutation, SeedMutator  # noqa: E402
from util.efficiency import eff  # noqa: E402
from seeds.store import SeedCorpus  # noqa: E402

LOGGER = logging.getLogger("corpus_hunt")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["duckdb", "postgres"], default="duckdb")
    ap.add_argument("--corpus", default="seeds/corpus.json")
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--start-seed", type=int, default=0)
    ap.add_argument("--max-per-seed", type=int, default=6)
    ap.add_argument("--include-original", action="store_true",
                    help="also screen each seed's raw query via plan_variant")
    ap.add_argument("--rule-targets", action="store_true",
                    help="append per-optimizer-rule trigger templates as seeds")
    ap.add_argument("--rule-targets-only", action="store_true",
                    help="screen ONLY the rule-target templates")
    ap.add_argument("--dce-budget", type=int, default=6000)
    ap.add_argument("--max-bisect", type=int, default=120)
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_hunt")
    ap.add_argument("--old-duckdb-venv",
                    default=str(OLD_VENV))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    out_dir = args.out or os.path.join(
        "results", f"hunt_{args.engine}_{time.strftime('%Y%m%d_%H%M%S')}"
    )
    os.makedirs(out_dir, exist_ok=True)
    ledger.configure(os.path.join(out_dir, "llm_ledger.jsonl"))

    if args.engine == "postgres":
        from targets.postgres_runner import PostgresRunner

        runner_factory = lambda: PostgresRunner(args.pg_datadir)  # noqa: E731
    else:
        runner_factory = lambda: DuckDBRunner(version_tag="hunt")  # noqa: E731

    differential = None
    if args.engine == "duckdb":
        old_python = os.path.join(args.old_duckdb_venv, "bin", "python")
        if os.path.exists(old_python):
            differential = DifferentialOracle(old_python=old_python)
            LOGGER.info("version ladder enabled: %s", old_python)

    fixer = BugFixer(
        {"model": "none", "api_key": "", "disable_llm": True},
        differential=differential,
        runner_factory=runner_factory,
        reference_factory=None,
        target_engine=args.engine,
    )
    signature = SignatureEngine(
        runner_factory=runner_factory,
        checker=fixer.checker,
        engine=args.engine,
        differential=differential,
        max_bisect_executions=args.max_bisect,
    )
    expander = FamilyExpander(
        runner_factory=runner_factory, engine=args.engine,
        max_probes=250, max_executed=150,
    )
    coevo = CoEvolution(
        hunter=None, fixer=fixer, guideline_generator=None,
        results_dir=out_dir, signature_engine=signature,
        expander=expander, dce_budget=args.dce_budget,
    )

    corpus = SeedCorpus.load(args.corpus)
    seeds = corpus.for_engine(
        "duckdb" if args.engine == "duckdb" else "postgres"
    ).seeds
    if args.rule_targets or args.rule_targets_only:
        if args.engine == "postgres":
            from seeds.pg_targets import as_seeds
        else:
            from seeds.rule_targets import as_seeds
        from seeds.store import Seed
        rt = [
            Seed(setup_sqls=s["setup_sqls"], query=s["query"],
                 source=s["source"], engine=s["engine"], tags=s["tags"])
            for s in as_seeds()
        ]
        LOGGER.info("rule-target seeds: %d", len(rt))
        seeds = rt if args.rule_targets_only else seeds + rt
    rng = random.Random(args.seed)
    rng.shuffle(seeds)
    seeds = seeds[args.start_seed:]
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("hunting %d seeds", len(seeds))

    mutator = SeedMutator(random.Random(args.seed + 7))
    tlp, norec, pv = TLPOracle(), NoRECOracle(), PlanVariantOracle(args.engine)

    stats = {
        "seeds": 0, "mutations": 0, "exec_ok": 0, "skipped_nondet": 0,
        "oracle_hits": 0, "families": 0, "duplicates": 0,
        "nonbug_verdicts": {},
    }
    t0 = time.time()
    runner = runner_factory()
    try:
        for i, seed in enumerate(seeds):
            muts = mutator.mutations_for(seed, max_per_seed=args.max_per_seed)
            stats["seeds"] += 1
            if args.include_original and seed.query:
                muts = [Mutation(
                    setup_sqls=list(seed.setup_sqls),
                    select_from=seed.query, predicate=None,
                    category="seed_original", source=seed.source,
                )] + list(muts)
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
                eff.count("queries_executed")
                with eff.phase("screen"):
                    hits = [
                        h for h in (
                            tlp.check(runner, mut.select_from, mut.predicate,
                                      mut.setup_sqls, [], category=mut.category),
                            norec.check(runner, mut.select_from, mut.predicate,
                                        mut.setup_sqls, [], category=mut.category),
                            pv.check(runner, full, mut.setup_sqls, [],
                                     category=mut.category),
                        )
                        if h is not None
                    ]
                for hit in hits:
                    stats["oracle_hits"] += 1
                    eff.count("oracle_hits")
                    with eff.phase("ingest"):
                        verdict, key = coevo.ingest_hit(
                            hit, iteration=f"seed{i}"
                        )
                    if verdict == "true_bug":
                        stats["families"] += 1
                        LOGGER.info(
                            "NEW FAMILY %s via seed %s (%s)",
                            key, seed.source, mut.category,
                        )
                    elif verdict == "duplicate":
                        stats["duplicates"] += 1
                    else:
                        stats["nonbug_verdicts"][verdict] = (
                            stats["nonbug_verdicts"].get(verdict, 0) + 1
                        )
            if (i + 1) % 50 == 0:
                rate = stats["exec_ok"] / max(time.time() - t0, 0.01)
                LOGGER.info(
                    "seed %d/%d exec=%d hits=%d families=%d dce=%d (%.1f q/s)",
                    i + 1, len(seeds), stats["exec_ok"], stats["oracle_hits"],
                    stats["families"], coevo.dce_executed, rate,
                )
            # Checkpoint families incrementally.
            if (i + 1) % 25 == 0:
                coevo._write_families()
    finally:
        runner.close()
        coevo._write_families()
        bugs_path = os.path.join(out_dir, "bugs.jsonl")
        with open(bugs_path, "w", encoding="utf-8") as fh:
            for b in coevo.true_bugs:
                fh.write(json.dumps(b, default=str) + "\n")

    summary = {
        **stats,
        "distinct_families": len(coevo.bug_index),
        "dce_executed": coevo.dce_executed,
        "efficiency": eff.snapshot(),
        "ledger": ledger.totals(),
        "elapsed_s": time.time() - t0,
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    LOGGER.info("done: %s", summary)
    print(json.dumps(summary, indent=2))
    print(f"Artifacts in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
