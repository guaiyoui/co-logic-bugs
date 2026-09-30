"""Post-hoc family collapse for runs that predated the ``pervasive`` fix.

Re-keys every record in ``families.json``: signatures whose fix_set is
wider than max(6, basis//4) are reclassified ``pervasive`` and re-keyed
by (engine, pervasive, plan_diff, nu) — matching the new signature.py
behavior. Writes ``families_collapsed.json`` and prints a summary.

    python scripts/collapse_families.py results/RUN_DIR [--basis 33]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def rekey(sig: dict, engine: str, basis: int) -> dict:
    sig = dict(sig)
    fix_kind = sig.get("fix_kind", "")
    fix_set = sig.get("fix_set") or []
    if fix_kind in ("single", "minimal_set") and len(fix_set) > max(6, basis // 4):
        fix_kind = "pervasive"
        fix_set = []
    canonical = {
        "engine": engine,
        "fix_kind": fix_kind,
        "fix_set": sorted(fix_set),
        "plan_diff": sorted(sig.get("plan_diff") or []),
        "nu": sig.get("nu", "unknown"),
    }
    if fix_kind in ("single", "minimal_set") and fix_set:
        ident = {"engine": engine, "fix_kind": fix_kind,
                 "fix_set": sorted(fix_set)}
    else:
        ident = canonical
    key = hashlib.sha256(
        json.dumps(ident, sort_keys=True).encode()
    ).hexdigest()[:20]
    return {**canonical, "key": key}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--basis", type=int, default=33)
    ap.add_argument("--engine", default="duckdb")
    args = ap.parse_args()
    for run in args.runs:
        path = os.path.join(run, "families.json")
        if not os.path.exists(path):
            print(f"{run}: no families.json")
            continue
        fams = json.load(open(path))
        merged: dict[str, dict] = {}
        for rec in fams:
            sig = rec.get("signature") or {}
            new_sig = rekey(sig, args.engine, args.basis)
            k = new_sig["key"]
            if k not in merged:
                merged[k] = {
                    "key": k,
                    "signature": new_sig,
                    "occurrences": 0,
                    "case_ids": [],
                    "origin": rec.get("origin", "hunter"),
                    "members": list(rec.get("key") for _ in [0]),
                }
                merged[k]["members"] = [rec.get("key")]
            else:
                merged[k]["members"].append(rec.get("key"))
            merged[k]["occurrences"] += rec.get("occurrences", 1)
            merged[k]["case_ids"] += rec.get("case_ids", [])
        out = list(merged.values())

        # Flaky-fragment merge: a probabilistic case lands in a different
        # narrow fix_set on each bisect attempt (avoidance != attribution).
        # Families that share the exact same plan_diff signature and whose
        # kinds include at least one nondeterministic verdict are merged —
        # the shared plan_diff marks the same underlying fault surface.
        nondet_kinds = {"flaky", "flaky_parallel", "unstable", "unresolved",
                        "pervasive"}
        groups: dict[tuple, list[dict]] = {}
        keep: list[dict] = []
        for rec in out:
            pd = tuple(sorted((rec.get("signature") or {}).get("plan_diff") or []))
            kind = (rec.get("signature") or {}).get("fix_kind")
            if not pd or kind not in nondet_kinds:
                # No shared plan_diff signal (cannot group) or a stable
                # record — never merge stable families into flaky clusters.
                keep.append(rec)
                continue
            groups.setdefault(pd, []).append(rec)
        for pd, recs in groups.items():
            if len(recs) > 1:
                anchor = next(
                    (r for r in recs
                     if (r.get("signature") or {}).get("fix_kind")
                     in ("flaky_parallel", "flaky")),
                    recs[0])
                for r in recs:
                    if r is anchor:
                        continue
                    anchor["occurrences"] += r.get("occurrences", 0)
                    anchor["case_ids"] += r.get("case_ids", [])
                    anchor["members"] += r.get("members", [])
                anchor.setdefault("signature", {})["merged_fragments"] = [
                    (r.get("key") or "")[:12] for r in recs if r is not anchor]
                keep.append(anchor)
            else:
                keep.extend(recs)
        keyed = {r["key"]: r for r in keep}
        out = [r for r in merged.values() if r["key"] in keyed] + [
            r for r in keep if r["key"] not in
            {m["key"] for m in merged.values()}]
        # dedupe preserving order
        seen = set()
        deduped = []
        for r in out:
            if r["key"] in seen:
                continue
            seen.add(r["key"])
            deduped.append(r)
        out = deduped
        with open(os.path.join(run, "families_collapsed.json"), "w") as fh:
            json.dump(out, fh, indent=1)
        uniq_cases = len({c for v in out for c in v["case_ids"]})
        print(f"{run}: {len(fams)} -> {len(out)} families, "
              f"{uniq_cases} unique member cases")
        for v in out:
            s = v["signature"]
            print(f"  {v['key'][:12]} {s['fix_kind']:>12} "
                  f"set={s['fix_set'][:4]}{'...' if len(s['fix_set'])>4 else ''} "
                  f"pd={s['plan_diff']} nu={s['nu']} occ={v['occurrences']}")
        _report_query_collisions(run)
    return 0


def _report_query_collisions(run: str) -> None:
    """Different family keys over the SAME minimized query indicate
    attribution noise (e.g. a by-spec nondeterministic query where any
    intervention coincidentally 'resolves' the run)."""
    bugs_path = os.path.join(run, "bugs.jsonl")
    if not os.path.exists(bugs_path):
        return
    by_query: dict[str, set] = {}
    for line in open(bugs_path):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        minimal = rec.get("minimal_case") or {}
        q1 = (minimal.get("q1") or "").strip()
        key = (rec.get("signature") or {}).get("key", "")[:12]
        if q1 and key:
            by_query.setdefault(q1, set()).add(key)
    collisions = {q: ks for q, ks in by_query.items() if len(ks) > 1}
    if collisions:
        print(f"  WARNING: {len(collisions)} queries attributed to "
              f"multiple family keys (attribution noise)")
        for q, ks in list(collisions.items())[:5]:
            print(f"    {q[:90]!r} -> {sorted(ks)}")


if __name__ == "__main__":
    raise SystemExit(main())
