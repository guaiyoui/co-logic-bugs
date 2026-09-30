"""Backfill coverage_size into gate2 summary records from per-run coverage.json,
and print a merged per-arm table across one or more gate2 result roots."""
import json, os, sys, statistics

def backfill(root):
    sp = os.path.join(root, "gate2_summary.json")
    if not os.path.isfile(sp):
        return []
    rep = json.load(open(sp))
    for r in rep.get("records", []):
        if r.get("coverage_size") is None:
            cov = os.path.join(r.get("run_dir", ""), "coverage.json")
            if os.path.isfile(cov):
                try: r["coverage_size"] = len(json.load(open(cov)))
                except Exception: pass
    json.dump(rep, open(sp, "w"), indent=1, default=str)
    return rep.get("records", [])

def table(records, label):
    from collections import defaultdict
    per = defaultdict(list)
    for r in records:
        if r.get("rc") == 0 and "families" in r:
            per[r["arm"]].append(r)
    print(f"\n== {label} ==")
    print(f"{'arm':15s} {'n':>3} {'fams':>18} {'mean':>5} {'fam/1k':>7} "
          f"{'execs~':>7} {'cov~':>5} {'llm~':>5} {'wall~':>6}")
    for arm, rs in sorted(per.items()):
        fams = [r["families"] for r in rs]
        y = [1000*r["families"]/r["execs"] for r in rs if r.get("execs")]
        cov = [r["coverage_size"] for r in rs if r.get("coverage_size") is not None]
        print(f"{arm:15s} {len(rs):>3} {str(fams):>18} "
              f"{statistics.mean(fams):>5.2f} "
              f"{(statistics.mean(y) if y else 0):>7.3f} "
              f"{statistics.mean([r.get('execs',0) for r in rs]):>7.0f} "
              f"{(statistics.mean(cov) if cov else 0):>5.0f} "
              f"{statistics.mean([r.get('llm_calls',0) for r in rs]):>5.0f} "
              f"{statistics.mean([r.get('wall_s',0) for r in rs]):>6.0f}")
    return per

if __name__ == "__main__":
    allrecs = []
    for root in sys.argv[1:]:
        allrecs += backfill(root)
    if allrecs:
        table(allrecs, "merged")
