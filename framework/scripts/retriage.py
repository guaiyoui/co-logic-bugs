"""Re-triage all candidates of a past run under the current oracle rules.

Reads ``iter_<k>.json`` records (candidates + stored minimal cases), applies
the determinism gate and the current ``BugFixer.triage`` logic — no hunter
calls, no re-minimization — and writes ``retriage/{bugs.jsonl,
version_divergences.jsonl, summary.json}`` inside the run directory.

Usage: ``python scripts/retriage.py results/run_20260913_233129``
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from agents.bug_fixer import BugFixer  # noqa: E402
from oracles.differential import DifferentialOracle  # noqa: E402
from oracles.models import (  # noqa: E402
    VERDICT_TRUE_BUG,
    VERDICT_VERSION_DIVERGENCE,
    Candidate,
    reproducible_script,
)

LOGGER = logging.getLogger("retriage")


def load_candidates(run_dir: Path) -> list[dict]:
    """All triaged candidate records across iter_<k>.json files."""
    records = []
    for path in sorted(run_dir.glob("iter_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload.get("candidates", []):
            record["_iteration"] = payload.get("iteration")
            records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--config", type=Path, default=PROJECT_DIR / "config.yml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with args.config.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    old_venv = Path(os.environ.get("COEVO_OLD_VENV")
                    or config.get("database", {}).get("old_venv", ""))
    differential = DifferentialOracle(old_python=old_venv / "bin" / "python")
    fixer = BugFixer(
        config["agents"].get("bug_fixer", config["agents"]["bug_hunter"]),
        differential=differential,
    )

    out_dir = args.run_dir / "retriage"
    out_dir.mkdir(parents=True, exist_ok=True)

    records = load_candidates(args.run_dir)
    LOGGER.info("re-triaging %d candidates from %s", len(records), args.run_dir)

    verdicts: Counter[str] = Counter()
    bug_index: dict[str, dict] = {}
    divergences: list[dict] = []
    llm_calls_before = fixer.performance_metrics["total_calls"]

    for record in records:
        candidate = Candidate.from_dict(record["candidate"])
        minimal = record.get("minimal_case") or {
            "schema_sqls": candidate.schema_sqls,
            "inserts": candidate.inserts,
            "q1": candidate.q1,
            "q2": candidate.q2,
        }
        try:
            verdict, extra = fixer.triage(candidate, minimal)
        except Exception as exc:  # noqa: BLE001 - one bad record must not abort
            LOGGER.error("triage failed for %s: %s", candidate.id, exc)
            verdict, extra = "undetermined", {"triage_error": str(exc)}
        verdicts[verdict] += 1
        LOGGER.info("%s kind=%s -> %s", candidate.id, candidate.kind, verdict)

        output = {
            "candidate": candidate.to_dict(),
            "previous_verdict": record.get("verdict"),
            "verdict": verdict,
            "minimal_case": minimal,
            "analysis": {**record.get("analysis", {}), **extra},
            "iteration": record.get("_iteration"),
        }
        if verdict == VERDICT_TRUE_BUG:
            key = candidate.root_key(
                q1=minimal.get("q1"), q2=minimal.get("q2")
            )
            if key in bug_index:
                bug_index[key]["occurrences"] += 1
                bug_index[key]["case_ids"].append(candidate.id)
                continue
            bug_index[key] = {
                **output,
                "root_key": key,
                "occurrences": 1,
                "case_ids": [candidate.id],
                "repro_sql": reproducible_script(
                    Candidate(
                        id=candidate.id,
                        kind=candidate.kind,
                        schema_sqls=minimal.get("schema_sqls", candidate.schema_sqls),
                        inserts=minimal.get("inserts", candidate.inserts),
                        q1=minimal.get("q1", candidate.q1),
                        q2=minimal.get("q2", candidate.q2),
                    )
                ),
            }
        elif verdict == VERDICT_VERSION_DIVERGENCE:
            divergences.append(output)

    (out_dir / "bugs.jsonl").write_text(
        "".join(json.dumps(b, default=str) + "\n" for b in bug_index.values()),
        encoding="utf-8",
    )
    (out_dir / "version_divergences.jsonl").write_text(
        "".join(json.dumps(d, default=str) + "\n" for d in divergences),
        encoding="utf-8",
    )
    summary = {
        "run_dir": str(args.run_dir),
        "raw_candidates": len(records),
        "verdicts": dict(verdicts),
        "deduped_root_causes": len(bug_index),
        "raw_true_bug_occurrences": sum(
            b["occurrences"] for b in bug_index.values()
        ),
        "version_divergences": len(divergences),
        "llm_calls": fixer.performance_metrics["total_calls"] - llm_calls_before,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nRetriage artifacts written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
