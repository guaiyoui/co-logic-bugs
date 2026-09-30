"""CLI entrypoint for the co-evolution DBMS bug-finding system.

Pipeline per iteration: guidelines -> BugHunter (LLM generates db, queries,
rewrites + zero-LLM seed mutations; deterministic oracles screen) ->
BugFixer (delta-debug + triage) -> feedback into both agents (UCB1 bandit +
root-cause arms). Artifacts land in ``results/<engine>_<mode>_<ts>/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import yaml

from agents.bug_fixer import BugFixer
from agents.bug_hunter import BugHunter
from coverage.store import CoverageStore
from diagnosis.signature import SignatureEngine
from evolution.bandit import CategoryBandit
from evolution.co_evolution import CoEvolution
from evolution.dce import FamilyExpander
from guidelines.llm_guideline import GuidelineGenerator
from llm.ledger import ledger
from oracles.cross_engine import CrossEngineOracle
from oracles.db_runner import DuckDBRunner
from oracles.differential import DifferentialOracle

PROJECT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Co-evolutionary LLM+oracle DBMS bug finder"
    )
    parser.add_argument("--config", type=Path, default=PROJECT_DIR / "config.yml")
    parser.add_argument("--engine",
                        choices=["duckdb", "postgres", "sqlite", "datafusion"],
                        default="duckdb")
    parser.add_argument(
        "--mode",
        choices=["full", "random_category", "no_llm", "coverage_only",
                 "dce_only", "shuffled_diag", "coevo", "coevo_warm",
                 "coevo_frozen"],
        default="full",
        help="full=bandit+feedback; random_category=ablation; "
             "no_llm=seed-only; coverage_only=bandit sees novelty only; "
             "dce_only=expansion without σ diagnosis; "
             "shuffled_diag=DCE conditioned on shuffled σ; "
             "coevo=full+accumulating playbook; coevo_warm=coevo seeded "
             "from --playbook-seed; coevo_frozen=seeded playbook, no "
             "new distillation",
    )
    parser.add_argument(
        "--playbook-seed",
        type=Path,
        default=None,
        help="jsonl of pre-distilled playbook rules (coevo_warm/frozen)",
    )
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--queries-per-iter", type=int, default=8)
    parser.add_argument("--seed-mutations-per-iter", type=int, default=30)
    parser.add_argument("--stagnation-limit", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--seed-corpus", type=Path,
                        default=PROJECT_DIR / "seeds" / "corpus.json")
    parser.add_argument("--no-seeds", action="store_true")
    parser.add_argument(
        "--cross-engine",
        action="store_true",
        help="also check every query on the other engine (adjudicated)",
    )
    parser.add_argument(
        "--pg-datadir",
        type=Path,
        default=Path("/tmp/coevo_pg"),
        help="pgserver datadir for the postgres target",
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=PROJECT_DIR / "results",
        help="directory where run artifacts are written",
    )
    parser.add_argument("--old-duckdb-venv", type=Path, default=None)
    parser.add_argument(
        "--label",
        type=str,
        default="",
        help="extra label appended to the run directory name",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.config.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    label = f"_{args.label}" if args.label else ""
    run_dir = args.results_root / f"{args.engine}_{args.mode}_{stamp}{label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger.configure(run_dir / "llm_ledger.jsonl")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(run_dir / "run.log", encoding="utf-8"),
        ],
    )
    logger = logging.getLogger("main")
    rng = random.Random(args.seed)

    # --- runner factory --------------------------------------------------
    if args.engine == "duckdb":
        runner_factory = lambda: DuckDBRunner(version_tag="local")  # noqa: E731
    elif args.engine == "sqlite":
        from targets.sqlite_runner import SQLiteRunner
        runner_factory = lambda: SQLiteRunner(None)  # noqa: E731
    elif args.engine == "datafusion":
        from targets.datafusion_runner import DataFusionRunner
        runner_factory = lambda: DataFusionRunner(None)  # noqa: E731
    else:
        from targets.postgres_runner import PostgresRunner

        pg_dir = args.pg_datadir / f"run_{stamp}"
        runner_factory = lambda: PostgresRunner(pg_dir)  # noqa: E731

    # --- optional oracles -------------------------------------------------
    differential = None
    if args.engine == "duckdb":
        old_python = (
            args.old_duckdb_venv / "bin" / "python"
            if args.old_duckdb_venv
            else Path(os.environ.get("COEVO_OLD_VENV")
                      or config.get("database", {}).get("old_venv", ""))
            / "bin"
            / "python"
        )
        differential = DifferentialOracle(old_python=old_python)
        if differential.available:
            logger.info("differential oracle enabled: %s", old_python)
        else:
            logger.warning("differential disabled (no old venv at %s)", old_python)

    cross_engine = None
    reference_factory = None
    if args.engine == "postgres":
        # DuckDB doubles as the cross-engine reference for a PG target.
        reference_factory = lambda: DuckDBRunner(version_tag="reference")  # noqa: E731
        cross_engine = CrossEngineOracle(
            reference_factory=reference_factory, reference_name="duckdb"
        )
        logger.info("cross-engine oracle enabled: postgres vs duckdb")
    elif args.engine in ("sqlite", "datafusion"):
        # DuckDB is the adjudication reference for the embedded engines —
        # dialect mismatches land as errors and stay unconfirmed.
        reference_factory = lambda: DuckDBRunner(version_tag="reference")  # noqa: E731
        cross_engine = CrossEngineOracle(
            reference_factory=reference_factory, reference_name="duckdb"
        )
        logger.info("cross-engine oracle enabled: %s vs duckdb",
                    args.engine)
    elif args.cross_engine:
        from targets.postgres_runner import PostgresRunner

        ref_dir = args.pg_datadir / f"ref_{stamp}"
        reference_factory = lambda: PostgresRunner(ref_dir)  # noqa: E731
        cross_engine = CrossEngineOracle(
            reference_factory=reference_factory, reference_name="postgres"
        )
        logger.info("cross-engine oracle enabled: duckdb vs postgres")

    # --- seeds / coverage / bandit ---------------------------------------
    seed_corpus = None
    seed_mutator = None
    if not args.no_seeds and args.seed_corpus.exists():
        from seeds.mutate import SeedMutator
        from seeds.store import SeedCorpus

        seed_corpus = SeedCorpus.load(args.seed_corpus)
        seed_mutator = SeedMutator(rng=random.Random(args.seed + 1))
        logger.info("seed corpus loaded: %d seeds", len(seed_corpus))
    elif not args.no_seeds:
        logger.warning("seed corpus %s missing; run scripts/fetch_seeds.py",
                       args.seed_corpus)

    coverage = CoverageStore(run_dir / "coverage.json")
    bandit = CategoryBandit(c=1.0) if args.mode in (
        "full", "coverage_only", "dce_only", "shuffled_diag",
        "coevo", "coevo_warm", "coevo_frozen") else None

    # no_llm mode must be a hard zero-call guarantee: disable_llm blocks
    # every call_llm at the agent level and the guideline generator gets no
    # agent at all.
    hunter_config = dict(config["agents"]["bug_hunter"])
    fixer_config = dict(
        config["agents"].get("bug_fixer", config["agents"]["bug_hunter"])
    )
    if args.mode == "no_llm":
        hunter_config["disable_llm"] = True
        fixer_config["disable_llm"] = True

    hunter = BugHunter(
        hunter_config,
        runner_factory=runner_factory,
        engine=args.engine,
        differential=differential,
        cross_engine=cross_engine,
        bandit=bandit,
        coverage=coverage,
        seed_mutator=seed_mutator,
        seed_corpus=seed_corpus,
        mode=args.mode,
        seed_mutations_per_iter=args.seed_mutations_per_iter,
        rng=rng,
    )
    fixer = BugFixer(
        fixer_config,
        differential=differential,
        runner_factory=runner_factory,
        reference_factory=reference_factory,
        target_engine=args.engine,
        reference_name=("postgres" if args.engine == "duckdb" else "duckdb"),
    )
    guidelines = GuidelineGenerator(
        llm_agent=None if args.mode == "no_llm" else hunter
    )

    # --- diagnosis signature + DCE family expansion ----------------------
    diag_cfg = config.get("diagnosis", {})
    signature_engine = None
    expander = None
    if diag_cfg.get("enabled", True):
        # dce_only ablation: expansion runs, σ diagnosis does not —
        # membership/identity falls back to the root_key path.
        if args.mode != "dce_only":
            signature_engine = SignatureEngine(
                runner_factory=runner_factory,
                checker=fixer.checker,
                engine=args.engine,
                differential=differential,
                max_bisect_executions=int(
                    diag_cfg.get("max_bisect_executions", 120)),
            )
        expander = FamilyExpander(
            runner_factory=runner_factory,
            engine=args.engine,
            max_probes=int(diag_cfg.get("max_probes", 250)),
            max_executed=int(diag_cfg.get("max_probes_executed", 150)),
        )
        logger.info("diagnosis engine + DCE expander enabled"
                    + (" (dce_only: no σ)" if args.mode == "dce_only"
                       else ""))

    # --- co-evolution playbook: distilled lessons accumulate across
    # iterations (and optionally across runs via --playbook-seed).
    playbook = None
    if args.mode.startswith("coevo"):
        from evolution.playbook import Playbook
        if args.playbook_seed and args.playbook_seed.exists():
            playbook = Playbook.load(args.playbook_seed, max_rules=20)
            logger.info("playbook seeded with %d rules from %s",
                        len(playbook.rules), args.playbook_seed)
        else:
            playbook = Playbook(max_rules=20)
        if args.mode == "coevo_frozen":
            # freeze: seed knowledge only, no new distillation
            playbook.distill = lambda *a, **k: None
        logger.info("coevo playbook enabled (%s)", args.mode)

    coevo = CoEvolution(
        hunter=hunter,
        fixer=fixer,
        guideline_generator=guidelines,
        results_dir=run_dir,
        stagnation_limit=args.stagnation_limit,
        signature_engine=signature_engine,
        expander=expander,
        dce_budget=int(diag_cfg.get("dce_budget", 2000)),
        shuffle_sigma=(args.mode == "shuffled_diag"),
        playbook=playbook,
        rng=rng,
    )
    try:
        summary = coevo.run(args.iterations, args.queries_per_iter)
    finally:
        coverage.save()
        hunter.close_client()
        fixer.close_client()

    summary["engine"] = args.engine
    summary["mode"] = args.mode
    summary["seed"] = args.seed
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nArtifacts written to {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
