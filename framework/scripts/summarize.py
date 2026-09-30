"""Aggregate campaign metrics across result directories.

Usage: python scripts/summarize.py [results_root] [glob]
Prints one line per run plus a running iteration table for live runs.
"""

from __future__ import annotations

import glob
import json
import os
import sys


def latest_per_run(root: str, pattern: str = "*") -> list[str]:
    dirs = sorted(
        d
        for d in glob.glob(os.path.join(root, pattern))
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "metrics.jsonl"))
        or os.path.exists(os.path.join(d, "summary.json"))
    )
    return dirs


def summarize_dir(d: str) -> dict:
    out = {"run": os.path.basename(d)}
    metrics_path = os.path.join(d, "metrics.jsonl")
    rows = []
    if os.path.exists(metrics_path):
        with open(metrics_path) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    out["iters_done"] = len(rows)
    for key in (
        "n_candidates", "new_true_bugs", "false_positives",
        "version_divergences", "cross_engine_divergences",
        "skipped_nondeterministic", "duplicates",
    ):
        out[key] = sum(r.get(key, 0) or 0 for r in rows)
    out["coverage"] = rows[-1].get("coverage_size") if rows else None
    out["llm_calls"] = sum(r.get("llm_calls", 0) or 0 for r in rows)
    summary_path = os.path.join(d, "summary.json")
    if os.path.exists(summary_path):
        s = json.load(open(summary_path))
        out["finished"] = s.get("finished_at")
        out["true_bugs"] = s.get("total_true_bugs")
        out["deduped_root_causes"] = s.get("deduped_root_causes")
    return out


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "results"
    pattern = sys.argv[2] if len(sys.argv) > 2 else "*"
    for d in latest_per_run(root, pattern):
        row = summarize_dir(d)
        print(
            f"{row['run'][:52]:<52} iters={row['iters_done']:>3} "
            f"cand={row['n_candidates']:>3} bugs={row['new_true_bugs']:>3} "
            f"fp={row['false_positives']:>3} vdiv={row['version_divergences']:>2} "
            f"xeng={row['cross_engine_divergences']:>2} "
            f"ndet={row['skipped_nondeterministic']:>3} cov={row['coverage']} "
            f"calls={row['llm_calls']} "
            f"{'FINISHED bugs=' + str(row.get('true_bugs')) if row.get('finished') else 'running'}"
        )


if __name__ == "__main__":
    main()
