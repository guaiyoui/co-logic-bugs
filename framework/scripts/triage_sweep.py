"""Triage seed-sweep hits into deduplicated confirmed bugs — no LLM calls.

Only sound-oracle kinds are confirmed (tlp/norec/plan_variant/crash); each
hit is re-checked for determinism, minimized by delta debugging, and deduped
by root signature. Produces bugs.jsonl + triage_summary.json.

Usage:
    python scripts/triage_sweep.py --engine duckdb results/sweep_duckdb_*/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from agents.bug_fixer import BugFixer  # noqa: E402
from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.models import (  # noqa: E402
    KIND_CRASH,
    KIND_NOREC,
    KIND_PLAN_VARIANT,
    KIND_TLP,
    Candidate,
    candidate_from_dict,
    reproducible_script,
)

LOGGER = logging.getLogger("triage_sweep")
SOUND_KINDS = {KIND_TLP, KIND_NOREC, KIND_PLAN_VARIANT, KIND_CRASH}


def make_factory(engine: str, datadir: str | None):
    if engine == "postgres":
        from targets.postgres_runner import PostgresRunner

        return lambda: PostgresRunner(datadir or "/tmp/coevo_pg_triage")  # noqa: E731
    return lambda: DuckDBRunner()  # noqa: E731


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hits", help="sweep result dir or hits.jsonl path")
    ap.add_argument("--engine", choices=["duckdb", "postgres"], default="duckdb")
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_triage")
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    hits_path = args.hits
    if os.path.isdir(hits_path):
        hits_path = os.path.join(hits_path, "hits.jsonl")
    out_dir = args.out or os.path.join(
        os.path.dirname(hits_path), "triaged"
    )
    os.makedirs(out_dir, exist_ok=True)

    config = yaml.safe_load(open(args.config))
    fixer = BugFixer(
        config.get("fixer", {}),
        runner_factory=make_factory(args.engine, args.pg_datadir),
        target_engine=args.engine,
        reference_name="duckdb" if args.engine == "postgres" else "postgres",
    )

    stats = {"hits": 0, "skipped_kind": 0, "nondet": 0,
             "not_reproduced": 0, "confirmed": 0, "dupe_root": 0}
    seen_roots: set[str] = set()
    seen_dedup: set[str] = set()
    t0 = time.time()
    bugs_path = os.path.join(out_dir, "bugs.jsonl")
    with open(hits_path) as fin, open(bugs_path, "w") as fout:
        for line in fin:
            hit = json.loads(line)
            stats["hits"] += 1
            candidate: Candidate = candidate_from_dict(hit["candidate"])
            if candidate.kind not in SOUND_KINDS:
                stats["skipped_kind"] += 1
                continue
            if candidate.dedup_key() in seen_dedup:
                stats["dupe_root"] += 1
                continue
            seen_dedup.add(candidate.dedup_key())
            minimal = {
                "schema_sqls": list(candidate.schema_sqls),
                "inserts": list(candidate.inserts),
                "q1": candidate.q1,
                "q2": candidate.q2,
            }
            if not fixer._deterministic(minimal):
                stats["nondet"] += 1
                continue
            shrunk = fixer._minimize(candidate)
            fixer.checker.executions = 0
            if not fixer.checker.reproduces(
                candidate,
                shrunk["schema_sqls"],
                shrunk["inserts"],
                shrunk.get("q1"),
                shrunk.get("q2"),
            ):
                stats["not_reproduced"] += 1
                continue
            root = candidate.root_key(shrunk.get("q1"), shrunk.get("q2"))
            if root in seen_roots:
                stats["dupe_root"] += 1
                continue
            seen_roots.add(root)
            stats["confirmed"] += 1
            record = {
                "candidate": candidate.to_dict(),
                "verdict": "true_bug",
                "oracle": hit.get("oracle"),
                "seed_source": hit.get("source"),
                "minimal_case": shrunk,
                "root_key": root,
                "repro_sql": reproducible_script(candidate),
            }
            fout.write(json.dumps(record, default=str) + "\n")
            fout.flush()
            LOGGER.info("confirmed #%d root=%s", stats["confirmed"], root)

    summary = {**stats, "elapsed_s": round(time.time() - t0, 1)}
    with open(os.path.join(out_dir, "triage_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    LOGGER.info("done: %s", summary)
    print(json.dumps(summary, indent=1))
    print(f"Bugs written to {bugs_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
