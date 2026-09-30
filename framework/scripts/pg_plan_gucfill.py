#!/usr/bin/env python3
"""Plan-variant GUC-fill sweep for PostgreSQL.

Runs every arm of ``oracles.plan_variant.POSTGRES_VARIANTS`` over the
SELECT-only seed corpus on one or more builds; under each arm the same
query must return the same row bag.  Any bag divergence, variant-only
(non value-dependent) error, or internal error is a sound wrong-result /
crash candidate — no LLM in the verdict path.

Coverage honesty: planner GUCs can be vacuous (``enable_tidscan=off``
without a ctid query, ``enable_gathermerge=off`` without a parallel
ordered plan).  For every (case, variant) pair the driver captures the
EXPLAIN (FORMAT JSON) node fingerprint under baseline and under the arm
and records ``plan_changed`` plus axis-specific markers (aggregate
Strategy, Gather Merge, parallel Append/Hash, Tid [Range] Scan,
EXPLAIN ANALYZE spill markers for the work_mem arm).  ``fired`` arms
provably altered plan shape; ``noop`` arms never did — reported per
variant as the no-op rate.

Session-GUC caveat: a variant's RESET restores the *config-file* value,
not a SET issued by the case's own setup.  Setup statements matching
``SET ...`` are therefore re-applied after each variant teardown.

Usage:
    python scripts/pg_plan_gucfill.py \
        --build master=/p/pgmaster_assert --build b186=/p/pg186_assert \
        --out results/pg_plan_variant_gucfill
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections import Counter
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.db_runner import QueryResult  # noqa: E402
from oracles.determinism import (  # noqa: E402
    has_intrinsic_nondeterminism,
    has_nondeterministic_tiebreak,
    is_result_order_sensitive,
)
from oracles.errors import is_guard_rail_error, is_value_dependent_error  # noqa: E402
from oracles.plan_variant import POSTGRES_VARIANTS  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402

LOGGER = logging.getLogger("pg_plan_gucfill")

_SELECT_ONLY = re.compile(r"^\s*(select|with|table|values|\()", re.IGNORECASE)
_NEEDS_TIEBREAK = re.compile(
    r"\b(limit|offset|fetch)\b|\bover\s*\(", re.IGNORECASE)
_SESSION_SET = re.compile(r"^\s*set\s+(?!local\b)", re.IGNORECASE)

# Forced-parallel context for probes whose baseline plan must contain
# parallel Append/GatherMerge/ParallelHash for the matching no_* arm to
# be load-bearing.  These SETs ride inside setup_sqls, so they shape the
# baseline plan without widening the variant list.
_FORCE_PAR = [
    "SET max_parallel_workers_per_gather=2",
    "SET min_parallel_table_scan_size=0",
    "SET min_parallel_index_scan_size=0",
    "SET parallel_setup_cost=0",
    "SET parallel_tuple_cost=0",
]

_CT_SETUP = [
    "CREATE TABLE ctf(id int, v int)",
    "INSERT INTO ctf SELECT i, i*3 FROM generate_series(1,400) i",
    "VACUUM ctf",
]
_GEQO_SETUP = [
    "CREATE TABLE j1(a int)", "CREATE TABLE j2(a int)",
    "CREATE TABLE j3(a int)", "CREATE TABLE j4(a int)",
    "CREATE TABLE j5(a int)", "CREATE TABLE j6(a int)",
    "INSERT INTO j1 VALUES (1),(2),(3)",
    "INSERT INTO j2 VALUES (1),(2),(4)",
    "INSERT INTO j3 VALUES (1),(3),(5)",
    "INSERT INTO j4 VALUES (1),(4)",
    "INSERT INTO j5 VALUES (1),(2)",
    "INSERT INTO j6 VALUES (1),(3)",
]
_BIG_SETUP = [
    "CREATE TABLE big(a int, t text)",
    "INSERT INTO big SELECT i, 'tag'||i FROM generate_series(1,30000) i",
    "ANALYZE big",
]
_SRT_SETUP = [
    "CREATE TABLE srt(a int, b int)",
    "INSERT INTO srt SELECT i%300, i FROM generate_series(1,6000) i",
    "CREATE INDEX srt_a ON srt(a)",
    "ANALYZE srt",
]

# Supplemental probes for axes the corpus cannot reach (no ctid queries,
# too few >=4-rel joins, no large fixtures, no parallel-eligible scans).
SUPPLEMENTAL: list[dict] = [
    # -- enable_tidscan arms --
    {"source": "gucfill:ctid_eq", "setup_sqls": _CT_SETUP,
     "query": "SELECT id, v FROM ctf WHERE ctid = '(0,7)'::tid"},
    {"source": "gucfill:ctid_range", "setup_sqls": _CT_SETUP,
     "query": "SELECT count(*) FROM ctf WHERE ctid > '(0,5)'::tid "
              "AND ctid < '(0,300)'::tid"},
    {"source": "gucfill:ctid_selfjoin", "setup_sqls": _CT_SETUP,
     "query": "SELECT count(*) FROM ctf a JOIN ctf b ON a.ctid = b.ctid "
              "WHERE a.ctid < '(0,50)'::tid"},
    # -- geqo arms (>=4 FROM items) --
    {"source": "gucfill:geqo6", "setup_sqls": _GEQO_SETUP,
     "query": "SELECT count(*) FROM j1 JOIN j2 ON j1.a=j2.a "
              "JOIN j3 ON j2.a=j3.a JOIN j4 ON j3.a=j4.a "
              "JOIN j5 ON j4.a=j5.a JOIN j6 ON j5.a=j6.a"},
    {"source": "gucfill:geqo5_star", "setup_sqls": [
        "CREATE TABLE f(a int, b int, c int, d int)",
        "CREATE TABLE d1(a int)", "CREATE TABLE d2(b int)",
        "CREATE TABLE d3(c int)", "CREATE TABLE d4(d int)",
        "INSERT INTO f SELECT i%5, i%4, i%3, i%2 "
        "FROM generate_series(1,200) i",
        "INSERT INTO d1 VALUES (1),(2),(3)",
        "INSERT INTO d2 VALUES (1),(2)",
        "INSERT INTO d3 VALUES (1),(3)",
        "INSERT INTO d4 VALUES (0),(1)"],
     "query": "SELECT count(*) FROM f JOIN d1 ON f.a=d1.a "
              "JOIN d2 ON f.b=d2.b JOIN d3 ON f.c=d3.c "
              "JOIN d4 ON f.d=d4.d"},
    # -- work_mem spill / optimize_bounded_sort / groupagg --
    {"source": "gucfill:spill_hashagg", "setup_sqls": _BIG_SETUP,
     "query": "SELECT a, count(*) FROM big GROUP BY a ORDER BY a"},
    {"source": "gucfill:spill_topn", "setup_sqls": _BIG_SETUP,
     "query": "SELECT t FROM big ORDER BY t LIMIT 10"},
    {"source": "gucfill:spill_fullsort", "setup_sqls": _BIG_SETUP,
     "query": "SELECT a, t FROM big ORDER BY t, a OFFSET 29990"},
    {"source": "gucfill:groupagg_idx", "setup_sqls": _SRT_SETUP,
     "query": "SELECT a, count(*), sum(b) FROM srt GROUP BY a ORDER BY a"},
    {"source": "gucfill:groupagg_distinct", "setup_sqls": _SRT_SETUP,
     "query": "SELECT DISTINCT a FROM srt ORDER BY a"},
    # -- parallel append / gather merge / parallel hash --
    {"source": "gucfill:parappend_part", "setup_sqls": _FORCE_PAR + [
        "CREATE TABLE pp(k int, v int) PARTITION BY RANGE(k)",
        "CREATE TABLE pp0 PARTITION OF pp FOR VALUES FROM (0) TO (1000)",
        "CREATE TABLE pp1 PARTITION OF pp FOR VALUES FROM (1000) TO (2000)",
        "CREATE TABLE pp2 PARTITION OF pp FOR VALUES FROM (2000) TO (3000)",
        "INSERT INTO pp SELECT i, i*2 FROM generate_series(1,2999) i",
        "ANALYZE pp"],
     "query": "SELECT k/1000 AS g, count(*) FROM pp GROUP BY 1 ORDER BY 1"},
    {"source": "gucfill:gathermerge_idx", "setup_sqls": _FORCE_PAR + [
        "CREATE TABLE gm(a int)",
        "INSERT INTO gm SELECT i FROM generate_series(1,20000) i",
        "CREATE INDEX gm_a ON gm(a)", "ANALYZE gm"],
     "query": "SELECT a FROM gm WHERE a > 50 ORDER BY a LIMIT 20"},
    {"source": "gucfill:parhash_join", "setup_sqls": _FORCE_PAR + [
        "CREATE TABLE h1(a int, b int)", "CREATE TABLE h2(a int, c int)",
        "INSERT INTO h1 SELECT i, i*2 FROM generate_series(1,8000) i",
        "INSERT INTO h2 SELECT i, i*3 FROM generate_series(1,8000) i",
        "ANALYZE h1", "ANALYZE h2"],
     "query": "SELECT count(*) FROM h1 JOIN h2 ON h1.a=h2.a"},
]

# Axis markers: variant label -> marker names whose presence in the
# baseline plan makes the arm load-bearing (and whose disappearance in
# the variant plan proves the flip fired).  Arms not listed fire via the
# generic plan-fingerprint diff only.
VARIANT_MARKERS: dict[str, set[str]] = {
    "no_groupagg": {"agg:Sorted", "agg:Mixed"},
    "no_hashagg": {"agg:Hashed"},
    "no_presorted_agg": {"agg:Sorted", "agg:Mixed"},
    "no_gathermerge": {"Gather Merge"},
    "no_par_append": {"Parallel Append", "par_append_aware"},
    "no_async_append": {"async_capable"},
    "no_par_hash": {"Parallel Hash", "Parallel Hash Join"},
    "no_tidscan": {"Tid Scan", "Tid Range Scan"},
    "no_sort": {"Sort", "Incremental Sort"},
    "no_index": {"Index Scan", "Index Only Scan", "Bitmap Heap Scan"},
    "no_seqscan": {"Seq Scan"},
    "no_memoize": {"Memoize"},
    "no_material": {"Materialize"},
    "no_hashjoin": {"Hash Join"},
    "no_mergejoin": {"Merge Join"},
    "no_nestloop": {"Nested Loop"},
    "no_parallel": {"Gather", "Gather Merge"},
    "force_parallel": {"Gather", "Gather Merge"},
    "debug_par": {"Gather", "Gather Merge"},
}

# EXPLAIN ANALYZE text markers proving the tiny-work_mem arm spilled.
_SPILL_RE = re.compile(
    r"Disk Usage|external merge|external sort|Method: external|"
    r"Batches:\s*(?:[2-9]|\d\d+)|temp\s+(?:read|written)=\s*[1-9]",
    re.IGNORECASE)
# Arms that warrant the extra EXPLAIN ANALYZE probe.
_ANALYZE_ARMS = {"tiny_workmem"}


def _walk_plan(node: dict[str, Any], out: list[tuple]) -> None:
    out.append((
        node.get("Node Type"),
        node.get("Strategy"),
        node.get("Partial Mode"),
        node.get("Join Type"),
        bool(node.get("Parallel Aware")),
        bool(node.get("Async Capable")),
    ))
    for child in node.get("Plans") or []:
        _walk_plan(child, out)


def _root_plan(plan_json: Any) -> dict | None:
    """EXPLAIN (FORMAT JSON) yields a top-level [{Plan: {...}}] list."""
    if isinstance(plan_json, list) and plan_json:
        plan_json = plan_json[0]
    if isinstance(plan_json, dict):
        return plan_json.get("Plan")
    return None


def plan_fingerprint(plan_json: Any) -> list[tuple]:
    """Multiset of shape-relevant node tuples; ignores costs/rows."""
    root = _root_plan(plan_json)
    if root is None:
        return []
    out: list[tuple] = []
    _walk_plan(root, out)
    return sorted(out, key=repr)


def plan_markers(plan_json: Any) -> set[str]:
    """Feature markers present in a JSON plan (axis evidence)."""
    markers: set[str] = set()
    nodes: list[tuple] = []
    root = _root_plan(plan_json)
    if root is not None:
        _walk_plan(root, nodes)
    for nt, strategy, _pmode, _jtype, par, async_cap in nodes:
        if nt:
            markers.add(nt)
        if nt in {"Aggregate", "HashAggregate", "GroupAggregate"} and strategy:
            markers.add(f"agg:{strategy}")
        if nt == "Append" and par:
            markers.add("par_append_aware")
        if async_cap:
            markers.add("async_capable")
    return markers


def spill_markers(pg: PostgresRunner, query: str,
                  timeout_s: float) -> list[str]:
    """EXPLAIN ANALYZE text lines proving sort/hash spilled to disk."""
    res = pg.run(
        f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {query}",
        timeout_s=timeout_s)
    if not res.ok:
        return []
    hits = []
    for row in res.rows:
        line = str(row[0])
        if _SPILL_RE.search(line):
            hits.append(line.strip()[:120])
    return hits


def load_corpus(modules: list[str]) -> list[dict]:
    seeds: list[dict] = []
    for name in modules:
        mod = importlib.import_module(name)
        seeds.extend(mod.as_seeds())
    seeds.extend(SUPPLEMENTAL)
    seen: set[str] = set()
    out = []
    for s in seeds:
        q = s["query"].strip().rstrip(";")
        if not _SELECT_ONLY.match(q):
            continue
        key = " ".join(s.get("setup_sqls", [])) + "|" + q
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": s.get("source", "?"),
                    "setup_sqls": list(s.get("setup_sqls", [])),
                    "query": q,
                    "tags": s.get("tags", [])})
    return out


def _hit_record(build: str, seed: dict, label: str, kind: str,
                variant: tuple, base: QueryResult,
                var: QueryResult, extra: dict) -> dict:
    missing = extra_rows = []
    if base.ok and var.ok:
        missing = [list(r) for r in
                   list((base.bag() - var.bag()).elements())[:8]]
        extra_rows = [list(r) for r in
                      list((var.bag() - base.bag()).elements())[:8]]
    return {
        "build": build, "kind": kind, "variant": label,
        "source": seed["source"], "query": seed["query"],
        "setup_sqls": seed["setup_sqls"],
        "variant_setup": variant[1], "variant_teardown": variant[2],
        "baseline": base.summary(), "variant_result": var.summary(),
        "rows_missing_in_variant": missing,
        "rows_extra_in_variant": extra_rows,
        "repro_sql": "\n".join(
            [s.rstrip(";") + ";" for s in seed["setup_sqls"]]
            + ["-- baseline"] + [seed["query"] + ";"]
            + [f"-- variant: {label}"]
            + [s.rstrip(";") + ";" for s in variant[1]]
            + [seed["query"] + ";"]
            + [s.rstrip(";") + ";" for s in variant[2]]),
        **extra,
    }


def run_build(tag: str, prefix: str, seeds: list[dict],
              variants: list[tuple], out_dir: str, timeout_s: float,
              flush_every: int = 25) -> dict:
    bdir = os.path.join(out_dir, tag)
    os.makedirs(bdir, exist_ok=True)
    runs_path = os.path.join(bdir, "runs.jsonl")
    pg = PostgresRunner(tempfile.mkdtemp(prefix=f"gucfill_{tag}_"),
                        pg_prefix=prefix)
    LOGGER.info("[%s] server %s (%s)", tag, pg.engine_version, prefix)

    # Runtime context audit: every GUC touched by an arm must be
    # session-settable (PGC_USERSET) — a postmaster-context SET inside
    # autocommit is a silent no-op hazard per AGENTS.md.
    gucs = sorted({m.group(1).lower() for _l, ss, ts in variants
                   for s in ss + ts
                   for m in [re.match(
                       r"\s*(?:set|reset)\s+([\w.]+)", s, re.IGNORECASE)]
                   if m})
    lst = ",".join(f"'{g}'" for g in gucs)
    ctx = pg.run(f"SELECT name, context FROM pg_settings WHERE name IN ({lst})")
    context_map = {r[0]: r[1] for r in ctx.rows}
    bad = {g: c for g, c in context_map.items()
           if c not in {"user", "superuser"}}
    if bad:
        LOGGER.warning("[%s] non-userset GUCs in variant table: %s", tag, bad)
    missing_gucs = [g for g in gucs if g not in context_map]
    if missing_gucs:
        LOGGER.info("[%s] GUCs absent on this build: %s", tag, missing_gucs)

    stats = Counter()
    var_stats: dict[str, Counter] = {v[0]: Counter() for v in variants}
    hits: list[dict] = []
    fatal_seen: list[str] = []
    runs_fh = open(runs_path, "w")

    def emit_run(rec: dict) -> None:
        runs_fh.write(json.dumps(rec, default=str) + "\n")

    def drain_log(seed: dict, phase: str) -> None:
        new = [ln for ln in pg.log_fatal_lines(pg.log_new_lines())
               if ln not in fatal_seen]
        if new:
            fatal_seen.extend(new)
            stats["log_fatal"] += 1
            hits.append({"build": tag, "kind": "log_fatal",
                         "source": seed["source"], "query": seed["query"],
                         "phase": phase, "log_lines": new[:20]})
            LOGGER.warning("[%s] LOG FATAL %s: %s", tag, seed["source"],
                           new[0][:120])

    t0 = time.time()
    try:
        for i, seed in enumerate(seeds):
            stats["cases"] += 1
            q = seed["query"]
            setup = seed["setup_sqls"]
            session_sets = [s for s in setup if _SESSION_SET.match(s)]

            # -- false-positive screening (lexical, then data-dependent) --
            if (has_intrinsic_nondeterminism(q)
                    or is_result_order_sensitive(q)):
                stats["fp_lexical"] += 1
                emit_run({"case": i, "source": seed["source"],
                          "status": "fp_skip", "reason": "lexical"})
                continue
            outcomes = pg.setup(setup)
            setup_errs = [e for _, e in outcomes if e]
            drain_log(seed, "setup")
            if setup_errs:
                stats["setup_fail"] += 1
                emit_run({"case": i, "source": seed["source"],
                          "status": "setup_fail",
                          "errors": setup_errs[:3]})
                continue
            if _NEEDS_TIEBREAK.search(q):
                try:
                    if has_nondeterministic_tiebreak(q, pg, setup):
                        stats["fp_tiebreak"] += 1
                        emit_run({"case": i, "source": seed["source"],
                                  "status": "fp_skip",
                                  "reason": "tiebreak"})
                        continue
                    # has_nondeterministic_tiebreak re-ran setup(); the
                    # session SETs above still hold.
                except Exception as exc:  # noqa: BLE001
                    stats["fp_tiebreak_err"] += 1
                    LOGGER.debug("tiebreak probe failed for %s: %s",
                                 seed["source"], exc)

            base = pg.run(q, timeout_s=timeout_s)
            drain_log(seed, "baseline")
            if base.is_internal_error:
                stats["baseline_internal"] += 1
                hits.append({"build": tag, "kind": "baseline_internal",
                             "source": seed["source"], "query": q,
                             "setup_sqls": setup,
                             "error": base.error})
                continue
            if not base.ok:
                stats["baseline_err"] += 1
                emit_run({"case": i, "source": seed["source"],
                          "status": "baseline_err",
                          "error": (base.error or "")[:200]})
                continue
            stats["exec_ok"] += 1
            base_plan = pg.explain_plan(q)
            base_fp = plan_fingerprint(base_plan)
            base_marks = plan_markers(base_plan)

            for label, set_stmts, teardown in variants:
                vstats = var_stats[label]
                vstats["runs"] += 1
                rec = {"case": i, "source": seed["source"],
                       "variant": label}
                setup_err = None
                for s in set_stmts:
                    r = pg.run(s, timeout_s=5.0)
                    if not r.ok:
                        setup_err = (r.error or "")[:200]
                        break
                try:
                    if setup_err is not None:
                        vstats["setup_fail"] += 1
                        rec.update(status="setup_fail", error=setup_err)
                        emit_run(rec)
                        continue
                    vplan = pg.explain_plan(q)
                    v_fp = plan_fingerprint(vplan)
                    v_marks = plan_markers(vplan)
                    changed = bool(base_fp) and v_fp != base_fp
                    markers = VARIANT_MARKERS.get(label, set())
                    marker_fired = bool(
                        (markers & base_marks)
                        and not (markers & v_marks))
                    spill = []
                    if label in _ANALYZE_ARMS:
                        spill = spill_markers(pg, q, timeout_s)
                    rec.update(
                        plan_changed=changed,
                        marker_fired=marker_fired,
                        markers_base=sorted(markers & base_marks),
                        markers_var=sorted(markers & v_marks))
                    if spill:
                        rec["spill_markers"] = spill[:6]
                    fired = changed or marker_fired or bool(spill)
                    vstats["fired" if fired else "noop"] += 1

                    var = pg.run(q, timeout_s=timeout_s)
                    drain_log(seed, label)
                    if var.timed_out:
                        vstats["timeout"] += 1
                        rec["status"] = "timeout"
                        emit_run(rec)
                        continue
                    if var.is_internal_error:
                        vstats["hit_internal"] += 1
                        rec["status"] = "hit_internal"
                        emit_run(rec)
                        hits.append(_hit_record(
                            tag, seed, label, "variant_internal",
                            (label, set_stmts, teardown), base, var,
                            {"plan_changed": changed}))
                        LOGGER.warning("[%s] INTERNAL %s/%s: %s", tag,
                                       seed["source"], label,
                                       (var.error or "")[:120])
                        continue
                    if not var.ok:
                        if (is_value_dependent_error(var.error)
                                or is_guard_rail_error(var.error, label)):
                            vstats["fp_skip"] += 1
                            rec.update(status="fp_skip",
                                       error=(var.error or "")[:160])
                            emit_run(rec)
                            continue
                        vstats["hit_err"] += 1
                        rec["status"] = "hit_err"
                        emit_run(rec)
                        hits.append(_hit_record(
                            tag, seed, label, "variant_error",
                            (label, set_stmts, teardown), base, var,
                            {"plan_changed": changed}))
                        LOGGER.warning("[%s] ERR %s/%s: %s", tag,
                                       seed["source"], label,
                                       (var.error or "")[:120])
                        continue
                    if var.bag() != base.bag():
                        vstats["hit_bag"] += 1
                        rec["status"] = "hit_bag"
                        emit_run(rec)
                        hits.append(_hit_record(
                            tag, seed, label, "bag_divergence",
                            (label, set_stmts, teardown), base, var,
                            {"plan_changed": changed,
                             "marker_fired": marker_fired}))
                        LOGGER.warning("[%s] BAG DIFF %s/%s base=%d var=%d",
                                       tag, seed["source"], label,
                                       len(base.rows), len(var.rows))
                        continue
                    vstats["same"] += 1
                    rec["status"] = "same_fired" if fired else "same_noop"
                    emit_run(rec)
                finally:
                    for s in teardown:
                        pg.run(s, timeout_s=5.0)
                    # RESET restores the config value, wiping case-level
                    # session SETs (e.g. forced-parallel probes) — re-apply.
                    for s in session_sets:
                        pg.run(s, timeout_s=5.0)
            if (i + 1) % flush_every == 0:
                runs_fh.flush()
                LOGGER.info("[%s] %d/%d cases ok=%d hits=%d noop_vs_fired=%s",
                            tag, i + 1, len(seeds), stats["exec_ok"],
                            len(hits),
                            {k: v["noop"] for k, v in var_stats.items()
                             if v["runs"] and v["fired"] == 0})
    finally:
        runs_fh.close()
        pg.cleanup()

    summary = {
        "build": tag, "prefix": prefix,
        "server_version": pg._version,
        "stats": dict(stats),
        "guc_context": context_map,
        "non_userset": bad,
        "missing_gucs": missing_gucs,
        "variants": {k: dict(v) for k, v in var_stats.items()},
        "hits": len(hits),
        "elapsed_s": round(time.time() - t0, 1),
    }
    with open(os.path.join(bdir, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1, default=str)
    with open(os.path.join(bdir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1, default=str)
    repro_dir = os.path.join(bdir, "repro")
    os.makedirs(repro_dir, exist_ok=True)
    for n, h in enumerate(hits):
        if "repro_sql" in h:
            with open(os.path.join(
                    repro_dir, f"hit{n}_{h['variant']}.sql"), "w") as fh:
                fh.write(h["repro_sql"] + "\n")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="append", required=True,
                    help="tag=/path/to/pg_prefix (repeatable)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed-modules", default=(
        "seeds.pg_targets,seeds.pg18_targets,seeds.pg_edge_targets"))
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--source-regex", default="",
                    help="keep only cases whose source matches")
    ap.add_argument("--variants", default="",
                    help="comma-separated label filter (default: all)")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    seeds = load_corpus([m.strip() for m in args.seed_modules.split(",")])
    if args.source_regex:
        rx = re.compile(args.source_regex)
        seeds = [s for s in seeds if rx.search(s["source"])]
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    only = {v.strip() for v in args.variants.split(",") if v.strip()}
    variants = [v for v in POSTGRES_VARIANTS if not only or v[0] in only]
    LOGGER.info("%d cases x %d variants", len(seeds), len(variants))

    summaries = []
    for spec in args.build:
        tag, _, prefix = spec.partition("=")
        summaries.append(run_build(tag, prefix, seeds, variants,
                                   out, args.timeout))

    with open(os.path.join(out, "summary_all.json"), "w") as fh:
        json.dump(summaries, fh, indent=1, default=str)

    lines = ["# pg_plan_gucfill report", ""]
    for s in summaries:
        lines.append(f"## {s['build']} ({s['server_version']})")
        st = s["stats"]
        lines.append(
            f"- cases={st.get('cases', 0)} exec_ok={st.get('exec_ok', 0)} "
            f"baseline_err={st.get('baseline_err', 0)} "
            f"fp_skip={st.get('fp_lexical', 0) + st.get('fp_tiebreak', 0)} "
            f"setup_fail={st.get('setup_fail', 0)} "
            f"elapsed={s['elapsed_s']}s")
        lines.append(f"- hits={s['hits']}")
        if s["missing_gucs"]:
            lines.append(f"- missing GUCs: {', '.join(s['missing_gucs'])}")
        lines.append("")
        lines.append("| variant | runs | fired | noop | setup_fail | "
                     "timeout | fp_skip | hits |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for label, v in s["variants"].items():
            n_hits = (v.get("hit_bag", 0) + v.get("hit_err", 0)
                      + v.get("hit_internal", 0))
            lines.append(
                f"| {label} | {v.get('runs', 0)} | {v.get('fired', 0)} | "
                f"{v.get('noop', 0)} | {v.get('setup_fail', 0)} | "
                f"{v.get('timeout', 0)} | {v.get('fp_skip', 0)} | "
                f"{n_hits} |")
        lines.append("")
    report = "\n".join(lines)
    with open(os.path.join(out, "report.md"), "w") as fh:
        fh.write(report)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
