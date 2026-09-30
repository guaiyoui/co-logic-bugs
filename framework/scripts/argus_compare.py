"""Compare Argus output pairs on DuckDB with our EquivalenceOracle.

Reads an Argus ``guided`` results file (list of {original_query, sqls:[...]}),
executes every ``provable=True`` (original, rewritten) pair on the database
described by the SQLancer valid-schema file, and counts how many pairs the
deterministic oracle flags as divergent — i.e. how many *candidate bugs*
the Argus baseline would report on the same data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.equivalence import EquivalenceOracle  # noqa: E402
from util.paths import ARGUS_DIR  # noqa: E402


def load_schema(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [s.strip() for s in text.split(";") if s.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus baseline comparison")
    parser.add_argument("--argus-results", type=Path, required=True)
    parser.add_argument(
        "--schema-file",
        type=Path,
        default=Path(
            str(ARGUS_DIR / "data" / "sqlancer" / "valid" / "database0_0-schema.sql")
        ),
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    results = json.loads(args.argus_results.read_text(encoding="utf-8"))
    statements = load_schema(args.schema_file)

    runner = DuckDBRunner(version_tag="local")
    runner.setup(statements)
    oracle = EquivalenceOracle()

    total_pairs = 0
    provable_pairs = 0
    mismatches = []
    per_query = []
    for entry in results:
        original = entry.get("original_query", "")
        stats = {"id": entry.get("id"), "generated": 0, "provable": 0, "mismatched": 0}
        for item in entry.get("sqls", []):
            rewritten = item.get("sql1") or item.get("sql") or ""
            if not rewritten or rewritten.strip() == original.strip():
                continue
            stats["generated"] += 1
            total_pairs += 1
            if not item.get("provable"):
                continue
            provable_pairs += 1
            stats["provable"] += 1
            hit = oracle.check(
                runner,
                original,
                rewritten,
                statements,
                [],
                category="argus_baseline",
                rewrite_kind="argus",
            )
            if hit is not None:
                stats["mismatched"] += 1
                mismatches.append(
                    {
                        "query_id": entry.get("id"),
                        "kind": hit.kind,
                        "original": original,
                        "rewritten": rewritten,
                        "r1_summary": hit.r1_summary,
                        "r2_summary": hit.r2_summary,
                    }
                )
        per_query.append(stats)

    runner.close()
    report = {
        "queries_evaluated": len(results),
        "generated_pairs": total_pairs,
        "provable_pairs": provable_pairs,
        "mismatching_pairs": len(mismatches),
        "per_query": per_query,
        "mismatches": mismatches,
    }
    text = json.dumps(report, indent=2, default=str)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
