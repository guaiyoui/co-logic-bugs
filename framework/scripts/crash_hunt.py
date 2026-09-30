#!/usr/bin/env python3
"""Crash/assertion oracle: replay seed setup+query through a DuckDB debug
shell (build/debug/duckdb). Assertion failures and aborts are invisible to
result oracles — this pass catches internal invariant violations.

Usage:
    python scripts/crash_hunt.py --duckdb-bin /path/to/build/debug/duckdb \
        --corpus seeds/corpus.json --rule-targets --out results/crash_155dbg
"""
import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from seeds.store import SeedCorpus  # noqa: E402
from oracles.determinism import has_intrinsic_nondeterminism  # noqa: E402

LOGGER = logging.getLogger("crash_hunt")

# Exit code alone is not a crash signal: the duckdb shell returns 1 for
# ordinary SQL errors (catalog/binder/etc). A real crash is a signal death
# (returncode < 0), a timeout, or an assertion/internal-error marker.
CRASH_MARKERS = ("Assertion", "assertion", "SIGABRT", "Segmentation",
                 "INTERNAL Error", "Internal Exception", "Fatal")


def run_shell(duckdb_bin: str, sql: str, timeout: float = 20.0):
    """Run sql via the debug shell; return (exit_code, stderr_text)."""
    try:
        p = subprocess.run(
            [duckdb_bin, "-batch", "-noheader", ":memory:"],
            input=sql, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stderr + p.stdout
    except subprocess.TimeoutExpired:
        return -9, "TIMEOUT_MARKER"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duckdb-bin", required=True)
    ap.add_argument("--corpus", default="seeds/corpus.json")
    ap.add_argument("--rule-targets", action="store_true")
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--mutations", type=int, default=0,
                    help="also try N simple mutations per seed (reserved)")
    ap.add_argument("--prelude", action="append", default=[],
                    help="SQL statement executed before each seed "
                         "(repeatable, e.g. executor debug settings)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    corpus = SeedCorpus.load(args.corpus)
    seeds = corpus.for_engine("duckdb").seeds
    if args.rule_targets:
        from seeds.rule_targets import as_seeds
        from seeds.store import Seed
        seeds = seeds + [
            Seed(setup_sqls=s["setup_sqls"], query=s["query"],
                 source=s["source"], engine=s["engine"], tags=s["tags"])
            for s in as_seeds()
        ]
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("crash hunting %d seeds via %s", len(seeds), args.duckdb_bin)

    hits = []
    executed = 0
    skipped = 0
    t0 = time.time()
    for i, seed in enumerate(seeds):
        query = seed.query
        if has_intrinsic_nondeterminism(query):
            skipped += 1
            continue
        pre = ";\n".join(args.prelude) + ";\n" if args.prelude else ""
        sql = pre + ";\n".join(seed.setup_sqls) + ";\n" + query + ";\n"
        code, out = run_shell(args.duckdb_bin, sql, args.timeout)
        executed += 1
        is_crash = code < 0 or any(m in out for m in CRASH_MARKERS)
        if is_crash:
            if code == -9:
                marker = "timeout"
            else:
                marker = next((m for m in CRASH_MARKERS if m in out),
                              f"signal={-code}")
            hits.append({
                "seed": seed.source,
                "prelude": args.prelude,
                "query": query[:500],
                "setup": seed.setup_sqls,
                "exit": code,
                "marker": marker,
                "out_tail": out[-400:],
            })
            LOGGER.info("CRASH %s marker=%s", seed.source, marker)
        if (i + 1) % 100 == 0:
            LOGGER.info("seed %d/%d exec=%d crashes=%d (%.1f q/s)",
                        i + 1, len(seeds), executed, len(hits),
                        executed / max(1e-9, time.time() - t0))

    out_dir = Path(args.out or "results/crash_run")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "crashes.jsonl").write_text(
        "\n".join(json.dumps(h) for h in hits))
    summary = {"seeds": len(seeds), "executed": executed,
               "skipped_nondet": skipped, "crashes": len(hits),
               "elapsed_s": time.time() - t0}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
