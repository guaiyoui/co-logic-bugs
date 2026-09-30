#!/usr/bin/env python3
"""Gate 2: paired ablation of the co-evolution framework.

Runs each arm on the same seeds with identical per-run budgets, then
compares new-family yield per execution, time-to-first-family, duplicate
rate and bandit allocation quality, with bootstrap CIs.

Arms:
  typed_random   — no_llm: seed mutations only, no LLM, no feedback
  coverage_only  — bandit scheduled on coverage novelty alone
  dce_only       — family expansion without σ diagnosis
  shuffled_diag  — full pipeline, DCE conditioned on shuffled σ
  full           — the complete co-evolution loop

Usage:
    python scripts/gate2.py --seeds 0 1 2 3 4 --iterations 8 \
        --queries-per-iter 8 --arms typed_random coverage_only full
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
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARM_TO_MODE = {
    "typed_random": "no_llm",
    "coverage_only": "coverage_only",
    "dce_only": "dce_only",
    "shuffled_diag": "shuffled_diag",
    "full": "full",
    "random_category": "random_category",
}


def _families_by_iter(metrics_path: str) -> list[int]:
    """new_true_bugs per iteration -> iteration index of first family."""
    try:
        return [json.loads(l).get("new_true_bugs", 0)
                for l in open(metrics_path)]
    except Exception:
        return []


def run_one(arm: str, seed: int, args) -> dict:
    mode = ARM_TO_MODE[arm]
    out = os.path.join(args.results_root, f"gate2_{arm}_s{seed}")
    cmd = [sys.executable, os.path.join(ROOT, "main.py"),
           "--engine", args.engine, "--mode", mode,
           "--iterations", str(args.iterations),
           "--queries-per-iter", str(args.queries_per_iter),
           "--seed", str(seed),
           "--results-root", args.results_root,
           "--label", f"gate2_{arm}_s{seed}"]
    env = dict(os.environ)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=ROOT, env=env)
    wall = time.time() - t0
    # locate the run dir main.py created (timestamped name + label)
    cand = sorted(
        (d for d in os.listdir(args.results_root)
         if d.endswith(f"gate2_{arm}_s{seed}")),
        key=lambda d: os.path.getmtime(
            os.path.join(args.results_root, d)),
        reverse=True)
    run_dir = (os.path.join(args.results_root, cand[0])
               if cand else out)
    summary_path = os.path.join(run_dir, "summary.json")
    rec = {"arm": arm, "seed": seed, "mode": mode, "run_dir": run_dir,
           "wall_s": round(wall, 1), "rc": proc.returncode}
    if os.path.isfile(summary_path):
        s = json.load(open(summary_path))
        counts = ((s.get("efficiency") or {}).get("counts") or {})
        rec.update({
            "execs": counts.get("sql_executions")
                     or s.get("exec_ok")
                     or counts.get("queries_executed") or 0,
            "families": s.get("total_families")
                        or s.get("distinct_families")
                        or len(s.get("families") or []),
            "true_bugs": s.get("total_true_bugs", 0),
            "llm_calls": s.get("total_llm_calls", 0),
            "duplicates": sum(i.get("duplicates", 0)
                              for i in s.get("per_iteration", [])),
            "skipped_nondet": s.get("total_skipped_nondeterministic", 0),
            "false_positives": s.get("total_false_positives", 0),
            "efficiency": s.get("efficiency"),
        })
        per_iter = _families_by_iter(os.path.join(run_dir,
                                                "metrics.jsonl"))
        rec["first_family_iter"] = (
            per_iter.index(next(x for x in per_iter if x > 0))
            if any(per_iter) else None)
        cov_path = os.path.join(run_dir, "coverage.json")
        if os.path.isfile(cov_path):
            try:
                rec["coverage_size"] = len(json.load(open(cov_path)))
            except Exception:
                pass
    else:
        rec["error"] = (proc.stderr or "")[-500:]
    return rec


def bootstrap_ci(xs, n=1000, seed=0):
    if not xs:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.mean(
        rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="+",
                    default=["typed_random", "coverage_only", "dce_only",
                             "shuffled_diag", "full"])
    ap.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2, 3, 4])
    ap.add_argument("--iterations", type=int, default=8)
    ap.add_argument("--queries-per-iter", type=int, default=8)
    ap.add_argument("--engine", default="duckdb")
    ap.add_argument("--results-root",
                    default=os.path.join(ROOT, "results", "gate2"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    os.makedirs(args.results_root, exist_ok=True)
    out = args.out or os.path.join(args.results_root, "gate2_summary.json")

    records = []
    for seed in args.seeds:
        for arm in args.arms:
            print(f"[gate2] seed={seed} arm={arm}", flush=True)
            rec = run_one(arm, seed, args)
            records.append(rec)
            with open(out, "w") as fh:
                json.dump({"args": vars(args), "records": records},
                          fh, indent=1, default=str)
            print(f"  -> fams={rec.get('families')} "
                  f"execs={rec.get('execs')} rc={rec['rc']}", flush=True)

    # ---- aggregate per arm -------------------------------------------
    per_arm = defaultdict(list)
    for r in records:
        per_arm[r["arm"]].append(r)
    report = {"arms": {}, "records": records, "args": vars(args)}
    for arm, rs in per_arm.items():
        ok = [r for r in rs if r.get("rc") == 0 and "families" in r]
        fams = [r["families"] for r in ok]
        execs = [r["execs"] for r in ok]
        yields = [1000 * r["families"] / r["execs"]
                  for r in ok if r.get("execs")]
        ttf = [r["first_family_iter"] for r in ok
               if r.get("first_family_iter") is not None]
        report["arms"][arm] = {
            "n_ok": len(ok), "n_total": len(rs),
            "families_mean": statistics.mean(fams) if fams else 0,
            "families_all": fams,
            "families_ci95": bootstrap_ci(fams),
            "execs_mean": statistics.mean(execs) if execs else 0,
            "yield_per_1k": statistics.mean(yields) if yields else 0,
            "yield_ci95": bootstrap_ci(yields),
            "time_to_first_family_iter":
                statistics.mean(ttf) if ttf else None,
            "duplicates_mean": statistics.mean(
                [r.get("duplicates", 0) for r in ok]) if ok else 0,
            "skipped_nondet_mean": statistics.mean(
                [r.get("skipped_nondet", 0) for r in ok]) if ok else 0,
            "wall_mean": statistics.mean(
                [r.get("wall_s", 0) for r in ok]) if ok else 0,
            "llm_calls_mean": statistics.mean(
                [r.get("llm_calls", 0) for r in ok]) if ok else 0,
            "coverage_mean": statistics.mean(
                [r["coverage_size"] for r in ok
                 if r.get("coverage_size") is not None])
            if any(r.get("coverage_size") is not None for r in ok)
            else None,
        }
    with open(out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)

    # ---- markdown -----------------------------------------------------
    md = ["# Gate 2 ablation\n",
          f"engine={args.engine} iterations={args.iterations} "
          f"queries/iter={args.queries_per_iter} seeds={args.seeds}\n",
          "| arm | runs | families (mean) | fam/1k exec | "
          "1st-family iter | dups | nondet-skip | wall_s |",
          "|---|---|---|---|---|---|---|---|"]
    for arm, a in sorted(report["arms"].items()):
        ci = a["families_ci95"]
        cis = f"[{ci[0]:.1f},{ci[1]:.1f}]" if ci else "-"
        md.append(
            f"| {arm} | {a['n_ok']}/{a['n_total']} | "
            f"{a['families_mean']:.2f} {cis} | {a['yield_per_1k']:.3f} | "
            f"{a['time_to_first_family_iter'] or '-'} | "
            f"{a['duplicates_mean']:.1f} | {a['skipped_nondet_mean']:.1f} |"
            f" {a['wall_mean']:.0f} |")
    md_path = os.path.splitext(out)[0] + ".md"
    with open(md_path, "w") as fh:
        fh.write("\n".join(md))
    print(f"wrote {out} and {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
