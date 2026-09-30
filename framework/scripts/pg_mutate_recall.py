#!/usr/bin/env python3
"""Mutate verbatim recall reproducers and differential-run on two PG builds.

For each recall case, mechanical variants are generated:
  - constant sweep: integer literals in setup/query -> -1, 0, 1, x10, 65535
  - planner GUC variants: force alternative plan shapes
  - frame variants: window EXCLUDE / frame-bound swaps

Each variant runs on BOTH builds (e.g. pg166 buggy-window, pg186 fixed).
The oracle is differential, not absolute — mutated constants change the
correct answer, so we compare the two builds' behavior:

  crash_A_only     -> fires only on the buggy build (recall confirmation)
  crash_B_only     -> fires only on the newer build (CANDIDATE LIVE BUG)
  divergent_rows   -> both succeed, different results (investigate)
  crash_both       -> both crash (compare server logs)
  clean            -> identical behavior

Usage:
  pg_mutate_recall.py --prefix-a /p/pg166_assert --prefix-b /p/pg186_assert
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from seeds.pg_recall import PG_RECALL
from targets.postgres_runner import PostgresRunner


def load_cases(module: str) -> list[dict]:
    if module == "pg_live_probes":
        from seeds.pg_live_probes import PG_LIVE_PROBES
        return PG_LIVE_PROBES
    if module == "pg_deferred_probes":
        from seeds.pg_deferred_probes import PG_DEFERRED_PROBES
        return PG_DEFERRED_PROBES
    if module == "pg18_targets":
        from seeds.pg18_targets import as_seeds
        return as_seeds()
    if module.endswith(".json"):
        raw = json.load(open(module))
        out = []
        for i, c in enumerate(raw):
            out.append({"name": c.get("name") or c.get("id")
                        or c.get("source") or f"corpus#{i}",
                        "setup_sqls": c.get("setup_sqls", c.get("setup", [])),
                        "pre_sqls": c.get("pre_sqls", c.get("pre", [])),
                        "query": c["query"]})
        return out
    return PG_RECALL


_INT = re.compile(r"(?<![\w.])(\d+)(?![\w.])")

_GUC_VARIANTS = [
    ("seqscan_off", ["set enable_seqscan to off"]),
    ("no_hashmerge", ["set enable_hashjoin to off",
                      "set enable_mergejoin to off"]),
    ("no_memoize", ["set enable_memoize to off"]),
    ("tiny_workmem", ["set work_mem to '64kB'"]),
    ("no_parallel", ["set max_parallel_workers_per_gather to 0"]),
    ("sje_off", ["set enable_self_join_elimination to off"]),
    ("no_distinct_reorder", ["set enable_distinct_reordering to off"]),
    ("generic_plan", ["set plan_cache_mode to force_generic_plan"]),
    ("async_off", ["set enable_async_append to off"]),
]

_FRAME_SWAPS = [
    ("exclude_current_row", "exclude group"),
    ("exclude_current_row", "exclude ties"),
    ("exclude group", "exclude current row"),
    ("exclude ties", "exclude current row"),
    ("unbounded preceding", "1 preceding"),
    ("current row", "unbounded following"),
]

# Mechanism-adjacent structural swaps, applied per occurrence when the
# needle is present — preserves executability while probing whether a fix
# covered sibling shapes.
_STRUCT_SWAPS = [
    (" union all ", " union "),
    (" union all ", " except all "),
    (" union all ", " intersect all "),
    ("inner join lateral", "left join lateral"),
    ("left join", "join"),
    (" in (select", " = any (select"),
    ("is not null", "is null"),
    ("is not null", "is not distinct from null"),
    ("on true", "on false"),
    ("with viewer as (", "with viewer as materialized ("),
    ("primary key", "unique"),
    ("count(*)", "count(1)"),
    ("select count(*)", "select sum(1)"),
]


def _int_variants(sql: str) -> list[tuple[str, str]]:
    """Replace each integer literal (one at a time) with perturbations."""
    out = []
    for m in _INT.finditer(sql):
        orig = m.group(1)
        for rep in ("0", "-1", str(int(orig) + 1), str(int(orig) * 10), "65535"):
            if rep == orig:
                continue
            v = sql[: m.start(1)] + rep + sql[m.end(1):]
            out.append((f"{orig}->{rep}@{m.start(1)}", v))
    return out


def mutate_case(case: dict) -> list[dict]:
    """Return variant dicts: {label, setup_sqls, pre_sqls, query}."""
    variants = []
    base_q = case["query"]
    for label, vq in _int_variants(base_q)[:24]:
        variants.append({"label": f"const:{label}", "query": vq})
    for s_idx, s in enumerate(case.get("setup_sqls", [])):
        for label, vs in _int_variants(s)[:8]:
            setup = list(case["setup_sqls"])
            setup[s_idx] = vs
            variants.append({"label": f"setup{s_idx}:{label}", "setup": setup})
    for gname, gsqls in _GUC_VARIANTS:
        variants.append({"label": f"guc:{gname}", "pre": gsqls})
    for old, new in _FRAME_SWAPS:
        if old in base_q:
            variants.append({
                "label": f"frame:{old}->{new}",
                "query": base_q.replace(old, new),
            })
    low = base_q.lower()
    for old, new in _STRUCT_SWAPS:
        if old in low:
            variants.append({
                "label": f"struct:{old.strip()}->{new.strip()}",
                "query": re.sub(re.escape(old), new, base_q, flags=re.IGNORECASE),
            })
    return variants


def outcome_of(res) -> tuple[str, object]:
    if res.is_internal_error:
        return ("crash", (res.error or "")[:160])
    if res.timed_out:
        return ("timeout", None)
    if not res.ok:
        return ("error", (res.error or "")[:160])
    return ("rows", res.rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix-a", required=True, help="buggy-window build")
    ap.add_argument("--prefix-b", required=True, help="fixed/newer build")
    ap.add_argument("--cases", default="", help="comma-separated case filter")
    ap.add_argument("--seed-module", default="pg_recall",
                    help="seeds.<module> providing the case list")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    only = {c.strip() for c in args.cases.split(",") if c.strip()}
    cases = [c for c in load_cases(args.seed_module)
             if not only or c["name"] in only]

    runners = {}
    for tag, prefix in (("A", args.prefix_a), ("B", args.prefix_b)):
        runners[tag] = PostgresRunner(
            tempfile.mkdtemp(prefix=f"mut_{tag}_"), pg_prefix=prefix)

    findings = []
    try:
        for case in cases:
            variants = mutate_case(case)
            for vi, var in enumerate(variants):
                results = {}
                log_hits = {"A": [], "B": []}
                for tag, pg in runners.items():
                    setup = var.get("setup", case.get("setup_sqls", []))
                    pre = list(case.get("pre_sqls", [])) + var.get("pre", [])
                    pg.setup([])
                    setup_ok = True
                    for s in setup:
                        r = pg.run(s, timeout_s=args.timeout)
                        if not r.ok:
                            setup_ok = False
                            break
                    if setup_ok:
                        for p in pre:
                            pg.run(p, timeout_s=args.timeout)
                        results[tag] = outcome_of(
                            pg.run(var.get("query", case["query"]),
                                   timeout_s=args.timeout))
                        # Teardown: pre-GUCs persist on the shared session —
                        # without RESET each variant runs with every previous
                        # variant's settings (a leaked sje=off masks later
                        # crashes and mislabels attribution).
                        for p in pre:
                            m = re.match(
                                r"\s*set\s+(?:local\s+|session\s+)?"
                                r"([\w.]+)", p, re.IGNORECASE)
                            if m:
                                pg.run(f"reset {m.group(1)}",
                                       timeout_s=args.timeout)
                            elif re.match(r"\s*prepare\b", p, re.IGNORECASE):
                                pg.run("deallocate all",
                                       timeout_s=args.timeout)
                        # Server-log scan: background-process deaths (parallel
                        # / io workers, autovacuum) never surface on the client.
                        fatal = pg.log_fatal_lines(pg.log_new_lines())
                        if fatal:
                            log_hits[tag].extend(fatal[:2])
                    else:
                        results[tag] = ("setup_fail", None)
                        pg.log_new_lines()  # keep offset fresh
                a, b = results["A"], results["B"]
                if a[0] == "setup_fail" or b[0] == "setup_fail":
                    continue
                expected = case.get("expected_rows")
                fired = {}
                if expected is not None:
                    for tag, o in (("A", a), ("B", b)):
                        fired[tag] = (o[0] == "rows"
                                      and o[1] != expected)
                if a == b and not (log_hits["A"] or log_hits["B"]):
                    if not (expected is not None and fired.get("A")):
                        continue
                cls = "divergent"
                if a[0] == "crash" and b[0] != "crash":
                    cls = "crash_A_only"
                elif b[0] == "crash" and a[0] != "crash":
                    cls = "crash_B_only"
                elif a[0] == "crash" and b[0] == "crash":
                    cls = "crash_both"
                elif a[0] == "rows" and b[0] == "rows":
                    cls = "divergent_rows"
                if a == b and (log_hits["A"] or log_hits["B"]):
                    cls = "log_anomaly"  # identical client output, dead process
                if a == b and expected is not None and fired.get("A"):
                    cls = "fired_both"  # bug reproduces identically on both
                rec = {"case": case["name"], "variant": var["label"],
                       "cls": cls,
                       "A": [a[0], str(a[1])[:300]],
                       "B": [b[0], str(b[1])[:300]],
                       "query": var.get("query", case["query"])}
                if log_hits["A"] or log_hits["B"]:
                    rec["log"] = {k: v[:4] for k, v in log_hits.items() if v}
                if fired.get("A") or fired.get("B"):
                    rec["fired"] = [t for t in ("A", "B") if fired.get(t)]
                findings.append(rec)
                print(f"{cls:<16} {case['name']} [{var['label']}]")
    finally:
        for pg in runners.values():
            pg.cleanup()

    order = {"crash_B_only": 0, "divergent_rows": 1, "crash_A_only": 2,
             "crash_both": 3, "divergent": 4}
    findings.sort(key=lambda f: order.get(f["cls"], 9))
    print(f"\n== {len(findings)} divergent variants ==")
    for f in findings:
        print(f"{f['cls']:<16} {f['case']:<42} {f['variant']:<28}")
        print(f"    A: {f['A'][0]} {f['A'][1][:100]}")
        print(f"    B: {f['B'][0]} {f['B'][1][:100]}")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(findings, fh, indent=2, default=str)


if __name__ == "__main__":
    main()
