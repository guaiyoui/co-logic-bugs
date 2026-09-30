"""Consolidated run report: families + verification + efficiency.

Reads a run directory (bugs.jsonl / families*.json / verified_bugs.jsonl /
summary.json / llm_ledger.jsonl) and prints a compact report:

    python scripts/report_run.py results/RUN_DIR [--collapse-basis 33]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(run: str, name: str):
    p = os.path.join(run, name)
    if not os.path.exists(p):
        return None
    if name.endswith(".jsonl"):
        return [json.loads(l) for l in open(p) if l.strip()]
    return json.load(open(p))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--basis", type=int, default=33)
    args = ap.parse_args()
    run = args.run

    summary = _load(run, "summary.json") or {}
    bugs = _load(run, "bugs.jsonl") or []
    verified = _load(run, "verified_bugs.jsonl")
    fams = (_load(run, "families_collapsed.json")
            or _load(run, "families.json") or [])
    members = _load(run, "family_members.jsonl") or []

    print(f"# Report: {os.path.basename(run.rstrip('/'))}\n")

    # ---- headline
    eff = summary.get("efficiency") or {}
    ledger = summary.get("ledger") or {}
    headline = {
        "families": len(fams),
        "bug_records": len(bugs),
        "oracle_hits": summary.get("oracle_hits"),
        "distinct_families": summary.get("distinct_families"),
        "iterations": summary.get("iterations"),
    }
    print("## Headline")
    for k, v in headline.items():
        if v is not None:
            print(f"- {k}: {v}")

    # ---- families
    print("\n## Families")
    for v in fams:
        s = v.get("signature") or {}
        n_members = len(set(v.get("case_ids") or []))
        print(
            f"- `{v['key'][:12]}` fix_kind={s.get('fix_kind')} "
            f"fix_set={s.get('fix_set')} nu={s.get('nu')} "
            f"occ={v.get('occurrences')} uniq_cases={n_members} "
            f"bisect_exec={s.get('bisect_executions')}"
        )

    # ---- verification
    if verified is not None:
        stab = collections.Counter(r.get("stability", "?") for r in verified)
        print("\n## Verification (verify_bugs)")
        for k, c in stab.most_common():
            print(f"- {k}: {c}")

    # ---- efficiency
    print("\n## Efficiency")
    if eff:
        print(f"- wall_s: {eff.get('wall_s')}")
        for k, v in (eff.get("phase_s") or {}).items():
            print(f"- phase {k}: {v}s")
        for k, v in (eff.get('counts') or {}).items():
            print(f"- count {k}: {v}")
    else:
        el = summary.get("elapsed_s")
        if el:
            print(f"- elapsed_s: {el}")
        for k in ("exec_ok", "mutations", "dce_executed"):
            if summary.get(k) is not None:
                print(f"- {k}: {summary[k]}")
    print(f"- llm_calls: {ledger.get('total_calls', 0)} "
          f"(ok={ledger.get('successful_calls', 0)} "
          f"fail={ledger.get('failed_calls', 0)} "
          f"blocked={ledger.get('blocked_calls', 0)})")
    print(f"- llm_tokens: {ledger.get('total_tokens', 0)}")
    for ev, n in (ledger.get("by_event") or {}).items():
        print(f"  - {ev}: {n}")

    # ---- derived yield rates
    n_fam = len(fams)
    execs = (eff.get("counts") or {}).get("queries_executed") \
        or summary.get("exec_ok")
    print("\n## Yield")
    if execs:
        print(f"- families per 1k executions: {1000.0 * n_fam / execs:.3f}")
    toks = ledger.get("total_tokens", 0)
    if toks and n_fam:
        print(f"- tokens per family: {toks / n_fam:.0f}")
    wall = eff.get("wall_s") or summary.get("elapsed_s")
    if wall and n_fam:
        print(f"- wall per family: {wall / n_fam:.1f}s")
    if members:
        print(f"- dce family members archived: {len(members)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
