"""Independent final adjudication of archived bugs — deterministic, no LLM.

Re-executes every bugs.jsonl record under the *current* oracle code and
re-decides the verdict:
  - sound kinds (tlp/norec/plan_variant/crash): must still reproduce and pass
    the determinism gate; plan-variant timeouts no longer count.
  - equiv/error_mismatch: the pair must differ on the target AND not produce
    identical per-side results on the reference engine (per-side agreement =
    non-equivalent rewrite = false positive).
  - differential: kept as version_divergence unless the record already shows
    an adjudicated regression.
  - cross_engine: never a true bug (dialect evidence only).

Usage:
    python scripts/verify_bugs.py --engine duckdb results/RUN_DIR [RUN_DIR...]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.models import Candidate, candidate_from_dict  # noqa: E402
from oracles.normalize import loose_bag  # noqa: E402
from oracles.reproduce import CaseChecker  # noqa: E402

LOGGER = logging.getLogger("verify_bugs")
SOUND = {"tlp", "norec", "plan_variant", "crash"}


def make_factory(engine: str, pg_dir: str):
    if engine == "postgres":
        from targets.postgres_runner import PostgresRunner

        return lambda: PostgresRunner(pg_dir)  # noqa: E731
    return lambda: DuckDBRunner()  # noqa: E731


def ref_factory(engine: str, pg_dir: str):
    def make():
        if engine == "postgres":
            return DuckDBRunner()
        from targets.postgres_runner import PostgresRunner

        return PostgresRunner(pg_dir)

    return make


def verify_equiv(cand: Candidate, run_f, ref_f) -> str:
    """Per-side cross-engine check for equivalence candidates."""
    setup = list(cand.schema_sqls) + list(cand.inserts)
    tgt = run_f()
    try:
        if any(e for _, e in tgt.setup(setup)):
            return "unverifiable_setup"
        t1 = tgt.run(cand.q1)
        t2 = tgt.run(cand.q2) if cand.q2 else None
    finally:
        tgt.close()
    if t2 is None or not t1.ok or not t2.ok:
        return "unverifiable"
    if loose_bag(t1.rows) == loose_bag(t2.rows):
        return "false_positive"  # no longer reproduces
    ref = ref_f()
    try:
        if any(e for _, e in ref.setup(setup)):
            return "true_bug_unverified"  # differs on target; no reference
        r1 = ref.run(cand.q1)
        r2 = ref.run(cand.q2) if cand.q2 else None
    finally:
        ref.close()
    if r2 is None or not r1.ok or not r2.ok:
        return "true_bug_unverified"
    same1 = loose_bag(r1.rows) == loose_bag(t1.rows)
    same2 = loose_bag(r2.rows) == loose_bag(t2.rows)
    if same1 and same2:
        return "false_positive"  # reference agrees per-side: not equivalent
    return "true_bug"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--engine", default="duckdb")
    ap.add_argument("--pg-datadir", default="/tmp/coevo_verify_pg")
    ap.add_argument("--repeat", type=int, default=1,
                    help="re-run sound-kind reproduction N times and "
                         "classify stable_true (all N) / flaky_true (>=1) / "
                         "false_positive (0)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    run_f = make_factory(args.engine, args.pg_datadir + "/tgt")
    ref_f = ref_factory(args.engine, args.pg_datadir + "/ref")
    checker = CaseChecker(runner_factory=run_f, engine=args.engine)

    totals: dict[str, int] = {}
    for run_dir in args.runs:
        path = os.path.join(run_dir, "bugs.jsonl")
        if not os.path.exists(path):
            continue
        counts: dict[str, int] = {}
        out_path = os.path.join(run_dir, "verified_bugs.jsonl")
        with open(path) as fin, open(out_path, "w") as fout:
            for line in fin:
                rec = json.loads(line)
                cand = candidate_from_dict(rec["candidate"])
                verdict = "unknown"
                if cand.kind in SOUND:
                    if args.repeat > 1:
                        hits = sum(
                            checker.reproduces(
                                cand, cand.schema_sqls, cand.inserts
                            )
                            for _ in range(args.repeat)
                        )
                        if hits == args.repeat:
                            verdict = "stable_true"
                        elif hits > 0:
                            verdict = "flaky_true"
                        else:
                            verdict = "false_positive"
                    else:
                        verdict = (
                            "true_bug"
                            if checker.reproduces(
                                cand, cand.schema_sqls, cand.inserts
                            )
                            else "false_positive"
                        )
                elif cand.kind in ("equiv", "error_mismatch"):
                    verdict = verify_equiv(cand, run_f, ref_f)
                elif cand.kind == "differential":
                    verdict = "version_divergence"
                elif cand.kind == "cross_engine":
                    verdict = "cross_engine_divergence"
                counts[verdict] = counts.get(verdict, 0) + 1
                if verdict in ("true_bug", "stable_true", "flaky_true"):
                    rec = dict(rec)
                    rec["stability"] = verdict
                    fout.write(json.dumps(rec, default=str) + "\n")
        totals[os.path.basename(run_dir)] = counts
        LOGGER.info("%s: %s", run_dir, counts)
    print(json.dumps(totals, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
