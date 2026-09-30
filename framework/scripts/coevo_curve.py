#!/usr/bin/env python3
"""Co-evolution curve experiment — does accumulated knowledge compound?

Arms (paired seeds, identical budgets):
  full         — σ-conditioned DCE only (status quo)
  coevo        — full + accumulating playbook (distill -> inject -> cite)
  coevo_warm   — coevo pre-seeded with cross-engine distilled lessons
  coevo_frozen — warm playbook, distillation disabled (frozen knowledge)

Curve metrics per iteration (metrics.jsonl): new_true_bugs,
inspired_queries/candidates/families, playbook_size. The co-evolution
claim is supported if (a) the coevo arms' cumulative-family curve
separates above full, and (b) within coevo, inspired queries out-hit
uninspired ones — the direct diagnosis->query causal edge.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARM_TO_MODE = {
    "full": "full",
    "coevo": "coevo",
    "coevo_warm": "coevo_warm",
    "coevo_frozen": "coevo_frozen",
}


def _metrics(run_dir: str) -> list[dict]:
    path = os.path.join(run_dir, "metrics.jsonl")
    try:
        return [json.loads(l) for l in open(path)]
    except Exception:
        return []


def run_one(arm: str, seed: int, args) -> dict:
    mode = ARM_TO_MODE[arm]
    cmd = [sys.executable, os.path.join(ROOT, "main.py"),
           "--engine", args.engine, "--mode", mode,
           "--iterations", str(args.iterations),
           "--queries-per-iter", str(args.queries_per_iter),
           "--seed", str(seed),
           "--stagnation-limit", str(args.stagnation_limit),
           "--results-root", args.results_root,
           "--label", f"curve_{arm}_s{seed}"]
    if arm in ("coevo_warm", "coevo_frozen") and args.playbook_seed:
        cmd += ["--playbook-seed", args.playbook_seed]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    wall = time.time() - t0
    cand = sorted(
        (d for d in os.listdir(args.results_root)
         if d.endswith(f"curve_{arm}_s{seed}")),
        key=lambda d: os.path.getmtime(os.path.join(args.results_root, d)),
        reverse=True)
    run_dir = os.path.join(args.results_root, cand[0]) if cand else ""
    rec = {"arm": arm, "seed": seed, "mode": mode, "run_dir": run_dir,
           "wall_s": round(wall, 1), "rc": proc.returncode}
    summary_path = os.path.join(run_dir, "summary.json")
    if os.path.isfile(summary_path):
        s = json.load(open(summary_path))
        counts = ((s.get("efficiency") or {}).get("counts") or {})
        rec.update({
            "execs": counts.get("sql_executions")
                     or s.get("exec_ok") or 0,
            "families": s.get("total_families")
                        or s.get("distinct_families")
                        or len(s.get("families") or []),
            "true_bugs": s.get("total_true_bugs", 0),
            "llm_calls": s.get("total_llm_calls", 0),
            "duplicates": sum(i.get("duplicates", 0)
                              for i in s.get("per_iteration", [])),
        })
        pb = s.get("playbook") or {}
        rec["playbook"] = pb
        iters = _metrics(run_dir)
        rec["families_by_iter"] = [i.get("new_true_bugs", 0) for i in iters]
        rec["inspired_by_iter"] = [
            {"queries": i.get("inspired_queries", 0),
             "candidates": i.get("inspired_candidates", 0),
             "families": i.get("inspired_families", 0),
             "playbook_size": i.get("playbook_size", 0)}
            for i in iters
        ]
        rec["queries_by_iter"] = [len(i.get("queries", []))
                                  for i in s.get("per_iteration", [])]
    else:
        rec["error"] = (proc.stderr or "")[-500:]
    return rec


def bootstrap_ci(xs, n=1000, seed=0):
    if not xs:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.mean(
        rng.choices(xs, k=len(xs))) for _ in range(n))
    return (round(means[int(0.025 * n)], 3),
            round(means[int(0.975 * n)], 3))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="+",
                    default=["full", "coevo", "coevo_warm"])
    ap.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2, 3, 4])
    ap.add_argument("--iterations", type=int, default=12)
    ap.add_argument("--queries-per-iter", type=int, default=8)
    ap.add_argument("--engine", default="duckdb")
    ap.add_argument("--playbook-seed",
                    default=os.path.join(ROOT, "results",
                                         "playbook_seed_xeng.jsonl"))
    ap.add_argument("--stagnation-limit", type=int, default=3)
    ap.add_argument("--results-root",
                    default=os.path.join(ROOT, "results", "coevo_curve"))
    args = ap.parse_args()
    os.makedirs(args.results_root, exist_ok=True)
    out = os.path.join(args.results_root, "curve_summary.json")

    records = []
    for seed in args.seeds:
        for arm in args.arms:
            print(f"[curve] seed={seed} arm={arm}", flush=True)
            rec = run_one(arm, seed, args)
            records.append(rec)
            with open(out, "w") as fh:
                json.dump({"args": vars(args), "records": records},
                          fh, indent=1, default=str)
            print(f"  -> fams={rec.get('families')} "
                  f"execs={rec.get('execs')} "
                  f"inspired_fams={sum(i['families'] for i in rec.get('inspired_by_iter', []))} "
                  f"rc={rec['rc']}", flush=True)

    # ---- aggregate ---------------------------------------------------
    agg = {}
    for arm in args.arms:
        rs = [r for r in records if r["arm"] == arm and "families" in r]
        if not rs:
            continue
        fams = [r["families"] for r in rs]
        execs = [r["execs"] for r in rs]
        insp_q = sum(sum(i["queries"] for i in r.get("inspired_by_iter", []))
                     for r in rs)
        insp_c = sum(sum(i["candidates"] for i in r.get("inspired_by_iter", []))
                     for r in rs)
        insp_f = sum(sum(i["families"] for i in r.get("inspired_by_iter", []))
                     for r in rs)
        tot_q = sum(sum(r.get("queries_by_iter", [])) for r in rs)
        # per-iteration mean cumulative families (the "curve")
        maxit = max(len(r.get("families_by_iter", [])) for r in rs)
        cum_curve = []
        for it in range(maxit):
            cum = sum(sum(r.get("families_by_iter", [])[:it + 1])
                      for r in rs) / len(rs)
            cum_curve.append(round(cum, 3))
        agg[arm] = {
            "n": len(rs),
            "families_mean": round(statistics.mean(fams), 2),
            "families_ci95": bootstrap_ci(fams),
            "execs_mean": round(statistics.mean(execs)),
            "fam_per_1k": round(
                statistics.mean(fams) / max(statistics.mean(execs), 1) * 1000,
                3),
            "duplicates_mean": round(
                statistics.mean([r["duplicates"] for r in rs]), 2),
            "cum_families_by_iter": cum_curve,
            "inspired": {"queries": insp_q, "candidates": insp_c,
                         "families": insp_f, "total_queries": tot_q},
        }
    report = {"args": vars(args), "arms": agg}
    with open(out, "w") as fh:
        json.dump({"args": vars(args), "records": records, "arms": agg},
                  fh, indent=1, default=str)
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
