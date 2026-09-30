"""Argus-lite baseline.

The full Argus pipeline needs SQLSolver, which returns UNKNOWN on schemas
using types it cannot parse (DECIMAL/TIMESTAMP here). This fallback keeps the
Argus generation model (``ModelAPI.generate_one`` with the stock Argus
prompt) but replaces the SMT prover with our deterministic
``EquivalenceOracle`` — matching the handoff's prescribed fallback.

Usage (from the Argus repo root):
    python <this>/argus_lite.py --queries-per-input 3 \
        --output results/argus_lite.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

COEVO_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(COEVO_DIR))
from util.paths import ARGUS_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus-lite baseline")
    parser.add_argument("--queries-per-input", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ARGUS_DIR / "results/argus_lite.json")
    args = parser.parse_args()

    # Argus's ModelAPI reads ./models/*.txt relative to the cwd.
    os.chdir(ARGUS_DIR)
    sys.path.insert(0, str(ARGUS_DIR))
    sys.path.insert(0, str(COEVO_DIR))

    from evaluate.helper import get_db_init_sqls, get_schema, get_supported_queries, read_query
    from models.apis import ModelAPI
    from oracles.db_runner import DuckDBRunner
    from oracles.equivalence import EquivalenceOracle

    data_path = str(ARGUS_DIR / "data" / "sqlancer")
    schema = get_schema(data_path)
    init_sqls = get_db_init_sqls(data_path, "SQLancer")
    supported = get_supported_queries(data_path, "sqlsolver")

    runner = DuckDBRunner(version_tag="local")
    runner.setup(init_sqls)
    oracle = EquivalenceOracle()
    model = ModelAPI("deepseek-chat")
    # Argus's prompt.txt ships "<-SCHEMA->"/"<-INPUT->" markers while
    # generate_one substitutes "<SCHEMA>"/"<INPUT>", so the schema is never
    # injected. Patch the in-memory prompt only; the repo file is untouched.
    if "<-SCHEMA->" in model.prompt:
        model.prompt = model.prompt.replace("<-SCHEMA->", "<SCHEMA>").replace(
            "<-INPUT->", "<INPUT>"
        )

    report = {"queries": [], "total_pairs": 0, "mismatching_pairs": 0, "mismatches": []}
    for query_id, is_supported in enumerate(supported, start=1):
        if not is_supported:
            continue
        original = read_query(query_id, data_path)
        entry = {"id": query_id, "original_query": original, "rewrites": []}
        for _ in range(args.queries_per_input):
            try:
                rewritten = model.generate_one(schema, original, temperature=1.0)
            except Exception as exc:  # noqa: BLE001
                entry["rewrites"].append({"error": str(exc)})
                continue
            if not rewritten or rewritten.strip() == original.strip():
                continue
            report["total_pairs"] += 1
            hit = oracle.check(
                runner,
                original,
                rewritten,
                init_sqls,
                [],
                category="argus_lite",
                rewrite_kind="argus_lite",
            )
            entry["rewrites"].append(
                {
                    "sql": rewritten,
                    "oracle_hit": hit.kind if hit else None,
                }
            )
            if hit is not None:
                report["mismatching_pairs"] += 1
                report["mismatches"].append(
                    {
                        "query_id": query_id,
                        "kind": hit.kind,
                        "original": original,
                        "rewritten": rewritten,
                        "r1_summary": hit.r1_summary,
                        "r2_summary": hit.r2_summary,
                    }
                )
        report["queries"].append(entry)
        print(f"query {query_id}: {len(entry['rewrites'])} rewrites")

    runner.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "mismatches"}, indent=2))
    print(f"mismatching_pairs={report['mismatching_pairs']} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
