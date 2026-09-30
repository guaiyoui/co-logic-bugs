#!/usr/bin/env python3
"""Adjudicate auto-run families: replay each family's minimal case on the
target engine, baseline vs the recorded variant, N repeats.

Usage: python scripts/adjudicate_run.py <run_dir> [--repeat 5]
Emits per-family verdicts: confirmed / flaky / dead + observed evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.plan_variant import _ENGINE_VARIANTS  # noqa: E402

VARIANT_SETUP = {
    label: (setup, teardown)
    for label, setup, teardown in _ENGINE_VARIANTS.get("duckdb", [])
}


def replay(runner, schema, inserts, query, variant_label):
    """Run query at baseline and under the recorded variant."""
    runner.setup(schema + inserts)
    base = runner.run(query)
    diffs = []
    if variant_label and variant_label in VARIANT_SETUP:
        setup, teardown = VARIANT_SETUP[variant_label]
        for s in setup:
            runner.run(s)
        var = runner.run(query)
        for s in teardown:
            runner.run(s)
        diffs.append((variant_label, var))
    return base, diffs


def bags_differ(a, b) -> bool:
    if a is None or b is None or not a.ok or not b.ok:
        return False
    return a.bag() != b.bag()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--repeat", type=int, default=5)
    args = ap.parse_args()
    rd = Path(args.run_dir)

    fams = json.load(open(rd / "families.json"))
    bugs = [json.loads(l) for l in open(rd / "bugs.jsonl")]
    by_key = {b.get("root_key"): b for b in bugs}

    runner = DuckDBRunner(version_tag="adjudicate")
    out = []
    for f in fams:
        key = f["key"]
        rec = by_key.get(key) or {}
        mc = rec.get("minimal_case") or {}
        cand = rec.get("candidate", {})
        q1 = mc.get("q1") or cand.get("q1")
        q2 = mc.get("q2") or cand.get("q2")
        schema = mc.get("schema_sqls") or cand.get("schema_sqls") or []
        inserts = mc.get("inserts") or cand.get("inserts") or []
        variant = (cand.get("r2_summary") or {}).get("variant")
        kind = cand.get("kind")

        hits = {"default": 0, "variant": 0, "q2_diff": 0}
        err = None
        for _ in range(args.repeat):
            try:
                if kind in ("tlp", "norec", "equiv") and q2:
                    runner.setup(schema + inserts)
                    a = runner.run(q1)
                    b = runner.run(q2)
                    if bags_differ(a, b):
                        hits["q2_diff"] += 1
                else:
                    base, diffs = replay(runner, schema, inserts, q1,
                                         variant)
                    for lbl, var in diffs:
                        if bags_differ(base, var):
                            hits["variant"] += 1
            except Exception as e:  # noqa: BLE001
                err = str(e)[:200]
        n = args.repeat
        if hits["q2_diff"] == n or hits["variant"] == n:
            verdict = "confirmed"
        elif any(hits.values()):
            verdict = "flaky"
        else:
            verdict = "dead"
        out.append({"key": key, "kind": kind, "variant": variant,
                    "inspired_by": cand.get("inspired_by"),
                    "verdict": verdict, "hits": hits, "err": err,
                    "occurrences": f.get("occurrences")})
        print(f"{key[:14]} {verdict:>9} kind={kind} variant={variant} "
              f"insp={cand.get('inspired_by') or '-'} hits={hits}")
    runner.close()
    (rd / "adjudication.json").write_text(json.dumps(out, indent=1))
    print(f"\nwrote {rd}/adjudication.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
