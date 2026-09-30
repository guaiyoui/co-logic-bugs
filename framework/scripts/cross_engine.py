"""Cross-engine differential: run the same case on N engines, compare
result bags. With >=3 engines a divergence becomes a *majority vote* —
the odd engine out is the prime suspect, which dramatically narrows
manual adjudication.

Supported engines: duckdb, postgres, sqlite, datafusion.

    python scripts/cross_engine.py --out results/xengine_feat \
        --engines duckdb,postgres,sqlite,datafusion
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.determinism import is_usable_for_oracles  # noqa: E402
from oracles.normalize import loose_bag  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("cross_engine")


def classify_divergence(canon: dict) -> tuple[list, str]:
    """Adjudicate a divergent case among engines that succeeded.

    Returns (odd_engines, class). An "odd engine" verdict requires a
    real quorum: >=3 engines succeeding AND a plurality of >=2 agreeing.
    With only two engines, Counter.most_common picks by insertion order
    — a tie is a pairwise divergence, not a majority verdict.
    """
    from collections import Counter
    votes = Counter(canon.values())
    top, top_n = votes.most_common(1)[0]
    if len(canon) >= 3 and top_n >= 2:
        return [n for n, b in canon.items() if b != top], "majority"
    return [], "pairwise"


def make_runner(engine: str):
    if engine == "duckdb":
        from oracles.db_runner import DuckDBRunner
        return DuckDBRunner(version_tag="xengine")
    if engine == "postgres":
        from targets.postgres_runner import PostgresRunner
        return PostgresRunner("/tmp/coevo_pg_xengine")
    if engine == "sqlite":
        from targets.sqlite_runner import SQLiteRunner
        return SQLiteRunner()
    if engine == "datafusion":
        from targets.datafusion_runner import DataFusionRunner
        return DataFusionRunner()
    raise ValueError(engine)


def run_case(runner, setup: list[str], query: str):
    try:
        errs = runner.setup(setup)
    except Exception:
        return None, "setup_error"
    if errs and any(e for _, e in errs):
        return None, "setup_error"
    res = runner.run(query, timeout_s=15.0)
    if not res.ok:
        return None, "exec_error"
    return loose_bag(res.rows), None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/xengine_feat")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--engines", default="duckdb,postgres",
                    help="comma list: duckdb,postgres,sqlite,datafusion")
    ap.add_argument("--pg-targets", action="store_true",
                    help="include pg-native templates (portable subset)")
    ap.add_argument("--corpus", default=None,
                    help="also sweep SELECT seeds from this corpus json")
    ap.add_argument("--seed-file", default=None,
                    help="python file exposing as_seeds() -> list of "
                         "{setup_sqls, query, source}")
    ap.add_argument("--no-rule-targets", action="store_true",
                    help="skip the default rule_targets seed set")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    engine_names = [e.strip() for e in args.engines.split(",") if e.strip()]
    runners = {e: make_runner(e) for e in engine_names}

    seeds = []
    if not args.no_rule_targets:
        from seeds.rule_targets import as_seeds
        seeds += as_seeds()
    if args.pg_targets:
        from seeds.pg_targets import as_seeds as pg_seeds
        seeds += pg_seeds()
    if args.corpus:
        from seeds.store import SeedCorpus
        seeds += [
            {"setup_sqls": s.setup_sqls, "query": s.query,
             "source": s.source}
            for s in SeedCorpus.load(args.corpus).seeds
            if s.query and s.query.lstrip().upper().startswith(
                ("SELECT", "WITH"))
        ]
    if args.seed_file:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "custom_seeds", args.seed_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        seeds += mod.as_seeds()
    seeds = seeds[: args.limit or None]

    stats = {"seeds": len(seeds), "engines": engine_names,
             "all_ok": 0, "divergent": 0, "skipped_nondet": 0,
             "div_majority": 0, "div_pairwise": 0,
             "exec_err": {e: 0 for e in engine_names}}
    divergences = []
    t0 = time.time()
    gate = runners.get("duckdb") or next(iter(runners.values()))
    try:
        for i, seed in enumerate(seeds):
            setup, query = seed["setup_sqls"], seed["query"]
            with eff.phase("screen"):
                if not is_usable_for_oracles(query, gate, setup):
                    stats["skipped_nondet"] += 1
                    continue
                bags, errs_ = {}, {}
                for name, runner in runners.items():
                    bag, err = run_case(runner, setup, query)
                    eff.count("queries_executed")
                    if err:
                        errs_[name] = err
                        stats["exec_err"][name] += 1
                    else:
                        bags[name] = bag
                if len(bags) < 2:
                    continue
                stats["all_ok"] += 1
                canon = {n: tuple(sorted(b.items())) for n, b in bags.items()}
                if len(set(canon.values())) > 1:
                    stats["divergent"] += 1
                    odd, cls = classify_divergence(canon)
                    stats[f"div_{cls}"] += 1
                    divergences.append({
                        "seed": seed["source"], "setup": setup,
                        "query": query,
                        "bags": {n: repr(b)[:300] for n, b in bags.items()},
                        "errors": errs_, "odd_engines": odd,
                        "n_engines_ok": len(bags), "class": cls,
                    })
                    LOGGER.info("DIVERGENT %s odd=%s", seed["source"], odd)
            if (i + 1) % 20 == 0:
                LOGGER.info("%d/%d div=%d", i + 1, len(seeds),
                            stats["divergent"])
    finally:
        for r in runners.values():
            try:
                r.cleanup()
            except Exception:
                pass

    with open(os.path.join(args.out, "divergences.jsonl"), "w") as fh:
        for d in divergences:
            fh.write(json.dumps(d, default=str) + "\n")
    summary = {**stats, "efficiency": eff.snapshot(),
               "elapsed_s": time.time() - t0}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
