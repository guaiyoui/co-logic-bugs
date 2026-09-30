"""Optimizer belief auditor — third oracle class.

Where TLP/NoREC/DQP compare outputs of two executions and PQS/TQS compare
an output to an expected value, this oracle audits the planner's
*intermediate semantic assertions* (beliefs) visible in EXPLAIN: dropped
quals, outer-join reductions, Inner Unique, Run Conditions, partition
pruning, Memoize cache keys, reduced Group Keys.  An asserted belief is a
universal claim about all data satisfying the schema; one counterexample
row in the concrete data falsifies it — catching faults at infection time
(RIPR), including latent wrong beliefs whose result happens to be right.

Per case the driver: EXPLAIN (VERBOSE + FORMAT JSON) the query, extract
asserted beliefs, run each belief's counterexample audit, and run the
query itself for the optional absolute answer.

Verdicts: false_belief | holds | not_asserted | asserted_no_audit |
audit_error; selftest cases additionally require not-asserted + non-empty
audit (proves the auditor would catch the violation if it were claimed).

Usage:
    python scripts/pg_belief_audit.py \
        --prefixes $COEVO_PGBLD/pg180_assert,\
$COEVO_PGBLD/pg186_assert,\
$COEVO_PGBLD/pgmaster_assert \
        --out results/belief_audit/run_a
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner  # noqa: E402
from oracles.pg_plan_beliefs import extract_beliefs  # noqa: E402
from seeds.pg_belief_cases import PG_BELIEF_CASES  # noqa: E402


def bag(rows) -> Counter:
    return Counter(repr(tuple(r)) for r in (rows or []))


def plan_text(pg: PostgresRunner, q: str) -> str:
    r = pg.run(f"EXPLAIN (VERBOSE, COSTS OFF) {q}")
    if not r.ok:
        return f"<explain_error: {r.error}>"
    return "\n".join(str(row[0]) for row in r.rows)


def plan_json(pg: PostgresRunner, q: str):
    r = pg.run(f"EXPLAIN (FORMAT JSON, VERBOSE) {q}")
    if not r.ok or not r.rows:
        return None
    payload = r.rows[0][0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def children_lookup(pg: PostgresRunner, parent: str) -> list[str]:
    r = pg.run(
        "SELECT inhrelid::regclass::text FROM pg_inherits "
        f"WHERE inhparent = '{parent}'::regclass")
    return [row[0].split(".")[-1] for row in r.rows] if r.ok else []


def audit_belief(pg: PostgresRunner, rec: dict) -> dict:
    """Run the counterexample audit for an asserted belief."""
    if not rec.get("asserted"):
        rec["verdict"] = "not_asserted"
        return rec
    audit = rec.get("audit")
    if audit is None:
        rec["verdict"] = "asserted_no_audit"
        return rec
    r = pg.run(audit)
    if not r.ok:
        rec.update(verdict="audit_error", audit_error=r.error)
        return rec
    rec["audit_rows"] = r.rows[:10]
    rec["audit_n"] = len(r.rows)
    rec["verdict"] = "false_belief" if r.rows else "holds"
    return rec


def run_case(pg: PostgresRunner, case: dict, timeout: float) -> dict:
    pg.setup(case["setup_sqls"])
    for g in case.get("gucs", []):
        pg.run(g, timeout_s=timeout)

    ptxt = plan_text(pg, case["q"])
    pjson = plan_json(pg, case["q"])
    recs = extract_beliefs(case, ptxt, pjson,
                           lambda p: children_lookup(pg, p))

    result = {"name": case["name"], "arm": case["arm"],
              "source": case.get("source", ""), "beliefs": []}

    for rec in recs:
        if case.get("selftest"):
            # fabricated claim: audit must find counterexamples even
            # though the plan rightly never asserted the belief
            audit = rec.get("audit")
            r = pg.run(audit) if audit else None
            n = len(r.rows) if (r and r.ok) else 0
            rec["audit_n"] = n
            if rec["asserted"]:
                rec["verdict"] = "unexpected_assertion"
            elif audit is None:
                rec["verdict"] = "selftest_no_audit"
            elif not (r and r.ok):
                rec["verdict"] = "audit_error"
            elif n > 0:
                rec["verdict"] = "selftest_pass"
            else:
                rec["verdict"] = "selftest_weak"
        else:
            audit_belief(pg, rec)
        result["beliefs"].append(rec)

    r = pg.run(case["q"], timeout_s=timeout)
    if not r.ok:
        result["result"] = {"outcome": "error", "error": r.error}
    elif "expected" in case:
        ok = bag(r.rows) == bag(case["expected"])
        result["result"] = {
            "outcome": "ok" if ok else "wrong_result",
            "rows": r.rows[:10], "expected": case["expected"]}
    else:
        result["result"] = {"outcome": "ok", "rows": r.rows[:10]}

    for g in case.get("resets", []):
        pg.run(g, timeout_s=timeout)
    return result


def verdict_line(case: dict, result: dict) -> str:
    bits = []
    for b in result["beliefs"]:
        bits.append(f'{b["id"]}={b["verdict"]}')
    res = result["result"]["outcome"]
    return f'{res:<13} | {"; ".join(bits) or "no beliefs":<60} | {case["name"]}'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefixes", required=True,
                    help="comma-separated PG install prefixes")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--only", default=None,
                    help="run only cases whose name matches")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]

    cases = [c for c in PG_BELIEF_CASES
             if args.only is None or args.only in c["name"]]

    report = {"versions": {}, "cases": []}
    for prefix in prefixes:
        tag = Path(prefix).name
        pg = PostgresRunner(out / f"data_{tag}", pg_prefix=prefix)
        ver = pg.engine_version
        report["versions"][tag] = ver
        print(f"== {tag} (server {ver})")
        for case in cases:
            res = run_case(pg, case, args.timeout)
            res["prefix"] = tag
            res["version"] = ver
            report["cases"].append(res)
            print("   " + verdict_line(case, res), flush=True)
        pg.cleanup()

    (out / "belief_audit.json").write_text(
        json.dumps(report, indent=2, default=str))
    print(f"\n-> {out}/belief_audit.json")


if __name__ == "__main__":
    main()
