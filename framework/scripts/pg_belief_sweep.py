"""Belief census sweep: run the belief extractor + auto-generated audits
over a whole query corpus.

For every corpus entry (setup_sqls + query): EXPLAIN (FORMAT JSON,
VERBOSE), oracles.pg_plan_beliefs.discover_beliefs harvests every
asserted planner belief, mechanically generated audits are executed
(inner_unique, partition_pruned), and any counterexample row => a
false_belief candidate — a wrong planner assertion regardless of whether
the query result happens to be right.

Usage:
    python scripts/pg_belief_sweep.py \
        --prefix $COEVO_PGBLD/pgmaster_assert \
        --corpus seeds/pg18_corpus.json,seeds/pg_corpus.json \
        --out results/belief_sweep/master_a
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner  # noqa: E402
from oracles.pg_plan_beliefs import discover_beliefs  # noqa: E402


def plan_json(pg: PostgresRunner, q: str):
    r = pg.run(f"EXPLAIN (FORMAT JSON, VERBOSE) {q}")
    if not r.ok or not r.rows:
        return None
    payload = r.rows[0][0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def children_of(pg: PostgresRunner, parent: str) -> list[str]:
    r = pg.run("SELECT inhrelid::regclass::text FROM pg_inherits "
               f"WHERE inhparent = '{parent}'::regclass")
    return [row[0].split(".")[-1] for row in r.rows] if r.ok else []


def parent_of(pg: PostgresRunner, rel: str) -> str | None:
    r = pg.run("SELECT inhparent::regclass::text FROM pg_inherits "
               f"WHERE inhrelid = '{rel}'::regclass")
    return r.rows[0][0].split(".")[-1] if r.ok and r.rows else None


def nonnull_col(pg: PostgresRunner, rel: str) -> str | None:
    """Any NOT NULL / PK column of rel — null-extension sentinel."""
    r = pg.run(
        "SELECT attname FROM pg_attribute WHERE attrelid = "
        f"'{rel}'::regclass AND attnum > 0 AND attnotnull "
        "AND NOT attisdropped ORDER BY attnum LIMIT 1")
    if r.ok and r.rows:
        return r.rows[0][0]
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--corpus", required=True,
                    help="comma-separated corpus JSON files")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    entries = []
    for f in args.corpus.split(","):
        entries += json.loads(Path(f.strip()).read_text())

    tag = Path(args.prefix).name
    pg = PostgresRunner(out / f"data_{tag}", pg_prefix=args.prefix)
    ver = pg.engine_version
    print(f"== {tag} (server {ver}), {len(entries)} corpus entries")

    stats = Counter()
    report = {"version": ver, "prefix": tag, "cases": []}
    for i, e in enumerate(entries):
        q = e["query"]
        pg.setup(e["setup_sqls"])
        pjson = plan_json(pg, q)
        if pjson is None:
            stats["explain_failed"] += 1
            continue
        recs = discover_beliefs(
            q, pjson,
            lambda p: children_of(pg, p),
            lambda r: parent_of(pg, r),
            nonnull_col=lambda r: nonnull_col(pg, r))
        case = {"idx": i, "source": e.get("source", ""),
                "query": q, "beliefs": []}
        for rec in recs:
            stats[f"asserted_{rec['kind']}"] += 1
            # audits: single SQL str, or {child: sql} for partitions
            audits = rec.get("audits")
            single = rec.get("audit")
            if audits:
                todo = list(audits.items())
            elif single:
                todo = [("", single)]
            else:
                rec["verdict"] = "asserted_no_audit"
                stats["no_audit"] += 1
                case["beliefs"].append(rec)
                continue
            stats["audited"] += 1
            rec["audit_results"] = {}
            false_hit = False
            for label, sql in todo:
                r = pg.run(sql, timeout_s=args.timeout)
                if not r.ok:
                    rec["verdict"] = "audit_error"
                    rec["audit_results"][label or rec["id"]] = {
                        "error": r.error}
                    stats["audit_error"] += 1
                    break
                rec["audit_results"][label or rec["id"]] = {
                    "n": len(r.rows), "rows": r.rows[:5]}
                if r.rows:
                    false_hit = True
            else:
                rec["verdict"] = ("false_belief" if false_hit else "holds")
                stats["false_belief" if false_hit else "holds"] += 1
            case["beliefs"].append(rec)
        if case["beliefs"]:
            report["cases"].append(case)

    (out / "belief_sweep.json").write_text(
        json.dumps(report, indent=2, default=str))
    print("\n".join(f"   {k:<28} {v}" for k, v in sorted(stats.items())))
    print(f"\n-> {out}/belief_sweep.json")
    pg.cleanup()


if __name__ == "__main__":
    main()
