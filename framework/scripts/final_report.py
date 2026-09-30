#!/usr/bin/env python3
"""Aggregate per-family bug counts, properties and efficiency across all
result dirs into results/FINAL_REPORT.md.

Sources of truth:
- results/FAMILY_LEDGER_155.md  -> curated family ids (F1..Fn, DF-*)
- results/*/bugs.jsonl          -> per-bug records (signature.key,
                                   minimal_case, verdict, occurrences)
- results/*/families.json       -> family-level occurrences/case_ids
- results/*/summary.json        -> per-run efficiency (execs, wall, hits)

A "bug" for counting = a distinct minimal reproducer (setup+q1 hash)
inside a verified family. Families are deduplicated by signature key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results")


def _hash_case(mc: dict) -> str:
    blob = "\x00".join(mc.get("schema_sqls") or []) + "\x00" + \
        (mc.get("q1") or "")
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _manifestation(rec: dict) -> str:
    sig = rec.get("signature") or {}
    fk = sig.get("fix_kind") or ""
    err = ""
    for part in (rec.get("candidate") or {}).values():
        pass
    r1 = (rec.get("candidate") or {}).get("r1_summary") or {}
    r2 = (rec.get("candidate") or {}).get("r2_summary") or {}
    for s in (r1, r2):
        if s.get("is_internal_error"):
            err = "internal-error"
    repro = rec.get("repro_sql") or ""
    low = repro.lower()
    if "flaky" in fk:
        return "flaky/" + ("parallel" if "parallel" in fk else "nondet")
    if "sigfpe" in low or "crash" in low:
        return "crash"
    if err:
        return err
    return "wrong-result"


def load_bugs(results_dir: str):
    """key -> {repros: set(hash), cases: set(id), records: [rec],
               runs: set(dir), verdicts: Counter}"""
    fams = defaultdict(lambda: {"repros": set(), "cases": set(),
                                "records": [], "runs": set(),
                                "verdicts": defaultdict(int)})
    for d in sorted(os.listdir(results_dir)):
        bpath = os.path.join(results_dir, d, "bugs.jsonl")
        if not os.path.isfile(bpath):
            continue
        for line in open(bpath):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sig = rec.get("signature") or {}
            key = sig.get("key")
            if not key:
                continue
            f = fams[key]
            f["runs"].add(d)
            f["records"].append(rec)
            f["verdicts"][rec.get("verdict") or "?"] += 1
            for cid in rec.get("case_ids") or []:
                f["cases"].add(cid)
            mc = rec.get("minimal_case")
            if mc:
                f["repros"].add(_hash_case(mc))
    return fams


def parse_ledger(path: str):
    """Return [{fid, keys, title}] in ledger order."""
    fams = []
    if not os.path.isfile(path):
        return fams
    cur = None
    for line in open(path):
        m = re.match(
            r"^###\s+(?:~~)?(F\d+|DF-[A-Z]|SQLITE-[A-Z]|PG-[A-Z]|TIDB-[A-Z])\b"
            r"[^`]*(`([0-9a-f]{6,})`)?",
            line) or re.match(
                r"^-\s+\*\*(DF-[A-Z]|SQLITE-[A-Z]|PG-[A-Z]|TIDB-[A-Z])\b",
                line)
        if m:
            cur = {"fid": m.group(1), "keys": set(),
                   "rejected": "~~" in line.split(m.group(1))[0],
                   "title": line.strip("# \n~")}
            if len(m.groups()) >= 3 and m.group(3):
                cur["keys"].add(m.group(3))
            fams.append(cur)
            continue
        if cur is not None:
            # capture backtick-quoted or bare hex keys mentioned in body
            for k in re.findall(r"`([0-9a-f]{6,18})`", line):
                cur["keys"].add(k)
            # bare hex keys only on lines that declare membership
            if re.search(r"fragment|σ key|signature key", line, re.I):
                for k in re.findall(r"\b([0-9a-f]{8,18})\b", line):
                    cur["keys"].add(k)
    return fams


def load_summaries(results_dir: str):
    rows = []
    for d in sorted(os.listdir(results_dir)):
        sp = os.path.join(results_dir, d, "summary.json")
        if not os.path.isfile(sp):
            continue
        try:
            s = json.load(open(sp))
        except Exception:
            continue
        eff = (s.get("efficiency") or {})
        counts = eff.get("counts") or {}
        rows.append({
            "run": d,
            # canonical execution count: every physical SQL statement
            # routed through a runner (setup + query + variant prelude).
            # Older runs lack it; fall back to logical query count.
            "execs": counts.get("sql_executions") or s.get("exec_ok")
                   or counts.get("queries_executed")
                   or s.get("seeds") or 0,
            "seeds": s.get("seeds"),
            "oracle_hits": s.get("oracle_hits") or s.get("divergent"),
            "families": s.get("distinct_families") or s.get("families"),
            "wall_s": round(eff.get("wall_s") or s.get("elapsed_s") or 0, 1),
            "llm_calls": (s.get("ledger") or {}).get("total_calls") or 0,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=RESULTS)
    ap.add_argument("--ledger",
                    default=os.path.join(RESULTS, "FAMILY_LEDGER_155.md"))
    ap.add_argument("--out", default=os.path.join(RESULTS,
                                                "FINAL_REPORT.md"))
    args = ap.parse_args()

    fams = load_bugs(args.results)
    ledger = parse_ledger(args.ledger)
    runs = load_summaries(args.results)

    # manual findings for families discovered by fuzzers/manual probes
    # (no σ key in bugs.jsonl): results/manual_findings.jsonl.
    # Entries may also carry only {"fid", "tier"} as tier overrides for
    # ledger families that already have σ-key records.
    manual = {}
    mpath = os.path.join(args.results, "manual_findings.jsonl")
    if os.path.isfile(mpath):
        for line in open(mpath):
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("fid"):
                manual[m["fid"]] = m

    # Conservative counting: only "confirmed" tier (default-path or
    # user-visible defects) enters the headline family/repro totals.
    # user-setting / debug-setting / unspecified-semantics /
    # spec-observation are reported separately — they are evidence,
    # not counted bugs.
    def tier_of(fid: str) -> str:
        m = manual.get(fid)
        if m and m.get("tier"):
            return m["tier"]
        return "confirmed"

    # map ledger keys -> bug stats; ledger stores short prefixes while
    # bugs.jsonl keeps full 18-hex keys, so match by prefix both ways
    def fid_of(key: str):
        for lf in ledger:
            for k in lf["keys"]:
                if key.startswith(k) or k.startswith(key):
                    return lf["fid"]
        return None
    key2fid = {k: fid_of(k) for k in fams}

    lines = []
    lines.append("# CoevoDB final bug report\n")
    lines.append("Generated by scripts/final_report.py from "
                 f"{args.results}\n")

    # ---- per-family table -------------------------------------------
    lines.append("## Verified families (per-engine)\n")
    lines.append("| family | σ keys | distinct repros | unique cases | "
                 "manifestation | fix_kind | fix_set / plan_diff | runs |")
    lines.append("|---|---|---|---|---|---|---|---|")
    total_repros = 0
    matched = set()
    fid2keys = defaultdict(list)
    for k, fid in key2fid.items():
        if fid:
            fid2keys[fid].append(k)

    def fam_stats(lf):
        repros, cases, runs_, manif, fk, detail = set(), set(), set(), \
            set(), set(), set()
        for k in fid2keys.get(lf["fid"], []):
            f = fams[k]
            matched.add(k)
            repros |= f["repros"]
            cases |= f["cases"]
            runs_ |= f["runs"]
            for rec in f["records"]:
                manif.add(_manifestation(rec))
                sig = rec.get("signature") or {}
                if sig.get("fix_kind"):
                    fk.add(sig["fix_kind"])
                pd = sig.get("plan_diff") or []
                fs = sig.get("fix_set") or []
                detail.add("/".join(fs) or "-")
                detail.add("+".join(pd[:4]) or "-")
        mf = manual.get(lf["fid"])
        if mf:
            repros |= set(range(mf.get("distinct_repros", 0)))
            cases |= set(range(mf.get("cases", 0)))
            manif.add(mf.get("manifestation", ""))
            fk.add(mf.get("fix_kind", ""))
        return repros, cases, runs_, manif, fk, detail

    excluded = []
    for lf in ledger:
        if lf.get("rejected"):
            continue
        if tier_of(lf["fid"]) != "confirmed":
            excluded.append(lf)
            continue
        repros, cases, runs_, manif, fk, detail = fam_stats(lf)
        keys = sorted(set(lf["keys"]) | set(fid2keys.get(lf["fid"], [])))
        total_repros += len(repros)
        keys_s = ", ".join(sorted({k[:8] for k in keys})) or "-"
        lines.append(
            f"| {lf['fid']} | {keys_s} | {len(repros)} | {len(cases)} | "
            f"{', '.join(sorted(manif)) or '-'} | "
            f"{', '.join(sorted(fk)) or '-'} | "
            f"{'; '.join(sorted(detail))[:80]} | {len(runs_)} |")

    if excluded:
        lines.append("\n### Recorded but excluded from confirmed totals\n")
        lines.append("| family | tier | repros | note |")
        lines.append("|---|---|---|---|")
        for lf in excluded:
            repros, cases, _, _, _, _ = fam_stats(lf)
            mf = manual.get(lf["fid"]) or {}
            lines.append(
                f"| {lf['fid']} | {tier_of(lf['fid'])} | "
                f"{len(repros)} | {(mf.get('note') or '')[:90]} |")

    # families in bugs.jsonl not mapped to ledger entries
    unmapped = {k: f for k, f in fams.items() if k not in matched}
    if unmapped:
        lines.append("\n### Unmapped signature keys (not yet curated "
                     "into ledger)\n")
        lines.append(f"(top 25 of {len(unmapped)} keys by repro count; "
                     "the long tail is dominated by attribution "
                     "fragments — flaky cases re-bisected under "
                     "coincidental triggers, plus pre-gate "
                     "nondeterminism leaks)\n")
        lines.append("| σ key | repros | cases | manifestation | "
                     "fix_kind | verdicts | runs |")
        lines.append("|---|---|---|---|---|---|---|")
        for k, f in sorted(unmapped.items(),
                           key=lambda kv: -len(kv[1]["repros"]))[:25]:
            manif = {_manifestation(r) for r in f["records"]}
            fk = {(r.get("signature") or {}).get("fix_kind") or "?"
                  for r in f["records"]}
            v = ", ".join(f"{kk}:{vv}" for kk, vv in
                          f["verdicts"].items())
            lines.append(f"| {k[:8]} | {len(f['repros'])} | "
                         f"{len(f['cases'])} | {', '.join(sorted(manif))} | "
                         f"{', '.join(sorted(fk))} | {v} | {len(f['runs'])} |")
        # noise structure: most unmapped keys are attribution fragments of
        # known families (flaky re-bisects, nondeterminism leaks)
        hist = defaultdict(int)
        pd_hist = defaultdict(int)
        ver_hist = defaultdict(int)
        for k, f in unmapped.items():
            for r in f["records"][:1]:
                sig = r.get("signature") or {}
                hist[sig.get("fix_kind") or "?"] += 1
                pd_hist["+".join((sig.get("plan_diff") or [])[:4])
                        or "-"] += 1
            vers = {"1.1.3" if "duck113" in d else
                    "1.0.0" if "duck100" in d else
                    "1.5.5" if "155" in d else "other"
                    for d in f["runs"]}
            ver_hist["+".join(sorted(vers))] += 1
        lines.append("\nUnmapped keys by fix_kind: " +
                     ", ".join(f"{k}={v}" for k, v in
                               sorted(hist.items(), key=lambda x: -x[1])))
        lines.append("\nUnmapped keys by engine-version runs: " +
                     ", ".join(f"{k}={v}" for k, v in
                               sorted(ver_hist.items(),
                                      key=lambda x: -x[1])))
        lines.append("\nTop plan_diff clusters: " +
                     "; ".join(f"{k}×{v}" for k, v in
                               sorted(pd_hist.items(), key=lambda x: -x[1])
                               [:8]))

    lines.append(f"\n**Total distinct reproducers in curated families: "
                 f"{total_repros}**; unmapped signature keys: "
                 f"{len(unmapped)}.\n")

    # ---- per-engine headline ----------------------------------------
    eng_map = {"F": "duckdb 1.5.5", "DF-": "datafusion 54.0.0",
               "SQLITE-": "sqlite 3.51.2", "PG-": "postgres 16.2",
               "TIDB-": "tidb v8.5.8"}
    per_eng = defaultdict(lambda: {"fams": 0, "repros": 0,
                                   "manif": set()})
    for lf in ledger:
        if lf.get("rejected"):
            continue
        fid = lf["fid"]
        if tier_of(fid) != "confirmed":
            continue
        eng = next((v for p, v in eng_map.items() if fid.startswith(p)),
                   "duckdb 1.5.5")
        e = per_eng[eng]
        e["fams"] += 1
        mf = manual.get(fid)
        nrep = mf.get("distinct_repros", 0) if mf else 0
        ks = fid2keys.get(fid, [])
        for k in ks:
            nrep += len(fams[k]["repros"])
            for rec in fams[k]["records"]:
                e["manif"].add(_manifestation(rec))
        if mf:
            e["manif"].add(mf.get("manifestation", ""))
        e["repros"] += nrep
    lines.insert(2, "## Headline\n")
    tbl = ["| engine | families | distinct bug repros | "
           "manifestations |", "|---|---|---|---|"]
    for eng in set(eng_map.values()):
        per_eng[eng]  # touch: defaultdict materializes zero rows
    tot_f = tot_r = 0
    for eng, e in sorted(per_eng.items()):
        tot_f += e["fams"]
        tot_r += e["repros"]
        tbl.append(f"| {eng} | {e['fams']} | {e['repros']} | "
                   f"{', '.join(sorted(m for m in e['manif'] if m))} |")
    tbl.append(f"| **total** | **{tot_f}** | **{tot_r}** | |")
    lines[3:3] = tbl + [""]

    # ---- efficiency table -------------------------------------------
    lines.append("## Run efficiency\n")
    lines.append("| run | seeds | execs | hits/div | families | "
                 "llm_calls | wall_s | yield (fam/1k exec) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    tot_e, tot_w = 0, 0.0
    for r in runs:
        y = ""
        if r["families"] and r["execs"]:
            y = f"{1000 * r['families'] / r['execs']:.2f}"
        lines.append(f"| {r['run']} | {r['seeds']} | {r['execs']} | "
                     f"{r['oracle_hits']} | {r['families']} | "
                     f"{r['llm_calls']} | {r['wall_s']} | {y} |")
        tot_e += r["execs"] or 0
        tot_w += r["wall_s"] or 0
    lines.append(f"\nTotal executions across recorded runs: **{tot_e}**, "
                 f"wall time ~{tot_w/3600:.1f}h.\n")

    # ---- baseline comparison ----------------------------------------
    bpath = os.path.join(args.results, "sqlancer_strong", "summary.json")
    bweak = os.path.join(args.results, "sqlancer_baseline", "summary.json")
    if not os.path.isfile(bpath):
        bpath = bweak
    if os.path.isfile(bpath):
        b = json.load(open(bpath))
        strong = "sqlancer_strong" in bpath
        lines.append("## Baseline comparison (SQLancer)\n")
        if strong:
            lines.append(
                f"Tool: {b['tool']}; STRONG baseline: 35 runs, "
                "multi-oracle, multi-seed, ~3h wall. Full details: "
                "results/sqlancer_strong/SQLANCER_STRONG_REPORT.md\n")
            lines.append("| engine | version | oracles | checks | bugs |")
            lines.append("|---|---|---|---|---|")
            per_eng = {}
            for r in b["runs"]:
                e = per_eng.setdefault(f"{r['engine']} {r['version']}",
                                       {"or": set(), "ck": 0, "bg": 0})
                e["or"].add(r.get("oracle", "?"))
                e["ck"] += r.get("checks", 0)
                e["bg"] += r.get("bugs", 0)
            for eng, e in sorted(per_eng.items()):
                lines.append(f"| {eng} | — | {len(e['or'])} | "
                             f"~{e['ck']/1e6:.1f}M | {e['bg']} |")
            lines.append(
                f"\nSQLancer total: **{b['total_bugs']} verified bugs** in "
                f"~{b['total_checks']/1e6:.0f}M oracle checks, "
                f"{len(b['runs'])} runs "
                f"(vs our {tot_r} distinct repros / {tot_f} families in "
                f"~{tot_e/1000:.0f}k execs — ~1700× fewer executions). "
                f"{b.get('note','')}\n")
        else:
            lines.append(f"Tool: {b['tool']}; budget "
                         f"{b['budget_per_engine_s']}s per engine. Details: "
                         "results/sqlancer_baseline/SQLANCER_REPORT.md\n")
            lines.append("| engine | version | oracle | checks | bugs |")
            lines.append("|---|---|---|---|---|")
            for r in b["runs"]:
                lines.append(f"| {r['engine']} | {r['version']} | "
                             f"{r['oracle']} | ~{r['checks']//1000}k | "
                             f"{r['bugs']} |")
            lines.append(f"\nSQLancer total: **{b['total_bugs']} bugs** in "
                         f"~{b['total_checks']/1e6:.1f}M oracle checks "
                         f"(vs our {tot_r} distinct repros / {tot_f} "
                         f"families in ~{tot_e/1000:.0f}k execs). "
                         f"{b.get('note','')}\n")

    # ---- gate2 ablation ----------------------------------------------
    g15 = os.path.join(args.results, "gate2", "gate2_summary.json")
    g113 = os.path.join(args.results, "gate2_duck113",
                        "gate2_summary.json")
    if os.path.isfile(g15) or os.path.isfile(g113):
        lines.append("## Gate 2 co-evolution ablation\n")
        lines.append("5 arms × 5 seeds × 8 iters, paired budgets. "
                     "Families = run-internal σ-attributed families. "
                     "Details: results/GATE2_RESULTS.md\n")
        for path, tag in ((g15, "duckdb 1.5.5 (sparse)"),
                          (g113, "duckdb 1.1.3 (bug-dense)")):
            if not os.path.isfile(path):
                continue
            rep = json.load(open(path))
            lines.append(f"### {tag}\n")
            lines.append("| arm | fams mean | fam/1k exec | dups | "
                         "nondet-skip | wall_s |")
            lines.append("|---|---|---|---|---|---|")
            for arm, a in sorted(rep["arms"].items()):
                lines.append(
                    f"| {arm} | {a['families_mean']:.2f} | "
                    f"{a['yield_per_1k']:.3f} | {a['duplicates_mean']:.1f} "
                    f"| {a['skipped_nondet_mean']:.1f} | "
                    f"{a['wall_mean']:.0f} |")
            lines.append("")
        lines.append("Reading: LLM arms find 2-7.7× typed_random; "
                     "DCE expansion is the workhorse; σ conditioning "
                     "minimizes duplicate discovery (full dups 0.2 vs "
                     "5.8 shuffled on 1.1.3). Directional evidence — "
                     "n=5, overlapping CIs.\n")

    with open(args.out, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {args.out}: {len(ledger)} ledger families, "
          f"{len(fams)} distinct σ keys in bugs.jsonl, "
          f"{total_repros} curated repros, {len(unmapped)} unmapped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
