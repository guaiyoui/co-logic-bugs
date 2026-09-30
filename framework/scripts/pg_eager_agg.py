#!/usr/bin/env python3
"""Differential oracle for 20devel eager aggregation (enable_eager_aggregate).

Eager aggregation pushes a *partial* aggregate below a join and finalizes
on top.  It is an optimization: query results with the feature on and off
must be identical.  This script generates agg-over-join workloads across
the known-risky arms (semi/anti joins, outer joins, appendrels, cross-type
join quals, FILTER/DISTINCT/ordered aggregates, grouping sets, lateral,
threshold edges) and bag-compares on/off results.

A case only counts as ``fired`` when the ON-plan actually places a
``Partial`` aggregate below a Join node (checked on EXPLAIN FORMAT JSON
with parallel workers disabled, so 'Partial' means eager aggregation,
not parallel partial aggregation).  Vacuous cases are reported as
``not_fired`` so coverage is measurable.

Determinism: all data and shapes derive from ``--seed``; rows are
compared as repr-keyed multisets (order-insensitive).  Aggregates are
restricted to exact-type results (int/numeric/text) — no float4/float8,
no volatile functions — so bag equality is sound.

Usage: python scripts/pg_eager_agg.py --prefix pgbld/pgmaster_assert \
         --datadir results/pg_eager_agg/dd --seed 1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
LOGGER = logging.getLogger("pg_eager_agg")

# -------------------------------------------------------------------
# schema shapes
# -------------------------------------------------------------------
# r1: a int pk-ish, b int (group/join col, domain small), c numeric, d text
# r2: x (int or bigint), y int (group col), z int
# r3: w int, u int      (3-table arm)
# Partitioned variants reuse the same column names.

SETUP_PLAIN = [
    "CREATE TABLE r1 (a int NOT NULL, b int, c numeric, d text)",
    "CREATE TABLE r2 (x {xtype} NOT NULL, y int, z int)",
    "CREATE TABLE r3 (w int NOT NULL, u int)",
]

SETUP_R1_PART = [
    "CREATE TABLE r1 (a int NOT NULL, b int, c numeric, d text)"
    " PARTITION BY RANGE (a)",
    "CREATE TABLE r1p1 PARTITION OF r1 FOR VALUES FROM (0) TO (400)",
    "CREATE TABLE r1p2 PARTITION OF r1 FOR VALUES FROM (400) TO (800)",
    "CREATE TABLE r1p3 PARTITION OF r1 FOR VALUES FROM (800) TO (1200)",
    "CREATE TABLE r1p4 PARTITION OF r1 FOR VALUES FROM (1200) TO (1600)",
    "CREATE TABLE r2 (x {xtype} NOT NULL, y int, z int)",
    "CREATE TABLE r3 (w int NOT NULL, u int)",
]

SETUP_R2_PART = [
    "CREATE TABLE r1 (a int NOT NULL, b int, c numeric, d text)",
    "CREATE TABLE r2 (x {xtype} NOT NULL, y int, z int)"
    " PARTITION BY RANGE (x)",
    "CREATE TABLE r2p1 PARTITION OF r2 FOR VALUES FROM (0) TO (300)",
    "CREATE TABLE r2p2 PARTITION OF r2 FOR VALUES FROM (300) TO (600)",
    "CREATE TABLE r2p3 PARTITION OF r2 FOR VALUES FROM (600) TO (1000000)",
    "CREATE TABLE r3 (w int NOT NULL, u int)",
]

SETUP_R1_INHERIT = [
    "CREATE TABLE r1p (a int NOT NULL, b int, c numeric, d text)",
    "CREATE TABLE r1c1 () INHERITS (r1p)",
    "CREATE TABLE r1c2 () INHERITS (r1p)",
    "CREATE TABLE r1 (a int NOT NULL, b int, c numeric, d text)",  # unused alias slot
    "DROP TABLE r1",
    "ALTER TABLE r1p RENAME TO r1",
    "CREATE TABLE r2 (x {xtype} NOT NULL, y int, z int)",
    "CREATE TABLE r3 (w int NOT NULL, u int)",
]

TABLE_SHAPES = {
    "plain": SETUP_PLAIN,
    "r1part": SETUP_R1_PART,
    "r2part": SETUP_R2_PART,
    "r1inh": SETUP_R1_INHERIT,
}


def data_sqls(rng: random.Random, n1: int, n2: int, g1: int, g2: int,
              nulls: bool, shape: str) -> list[str]:
    """Deterministic INSERT rows; dense groups make eager agg attractive."""
    r1_rows = []
    for i in range(1, n1 + 1):
        b = "NULL" if (nulls and i % 17 == 0) else str(i % g1)
        r1_rows.append(f"({i},{b},{i * 3 % 97},'t{i % 11}')")
    r2_rows = []
    for i in range(1, n2 + 1):
        r2_rows.append(f"({i},{i % g2},{i % 13})")
    r3_rows = [f"({i},{i % 9})" for i in range(1, 60)]
    if shape == "r1inh":
        sqls = [
            "INSERT INTO r1c1 VALUES " + ",".join(
                r for j, r in enumerate(r1_rows) if j % 2 == 0),
            "INSERT INTO r1c2 VALUES " + ",".join(
                r for j, r in enumerate(r1_rows) if j % 2 == 1),
            "INSERT INTO r2 VALUES " + ",".join(r2_rows),
            "INSERT INTO r3 VALUES " + ",".join(r3_rows),
        ]
    else:
        sqls = [
            "INSERT INTO r1 VALUES " + ",".join(r1_rows),
            "INSERT INTO r2 VALUES " + ",".join(r2_rows),
            "INSERT INTO r3 VALUES " + ",".join(r3_rows),
        ]
    sqls += ["CREATE UNIQUE INDEX r1a_uq ON r1(a)",
             "CREATE INDEX r1b_ix ON r1(b)",
             "CREATE INDEX r2x_ix ON r2(x)",
             "ANALYZE r1", "ANALYZE r2", "ANALYZE r3"]
    return sqls


# -------------------------------------------------------------------
# query arms.  {R1}/{R2}/{R3} are table names; agg cols tagged by side.
# -------------------------------------------------------------------

AGGS = [
    ("count(*)", "cnt"),
    ("count(r1.c)", "cntc"),
    ("sum(r1.c)", "sumc"),
    ("sum(r1.a)", "suma"),
    ("min(r1.c)", "minc"),
    ("max(r1.b)", "maxb"),
    ("avg(r1.a)", "avga"),
    ("count(DISTINCT r1.b)", "cntdb"),
    ("sum(DISTINCT r1.c)", "sumdc"),
    ("sum(r1.c) FILTER (WHERE r1.a % 3 = 0)", "sumflt"),
    ("string_agg(r1.d, ',' ORDER BY r1.d)", "sagg"),
    ("array_agg(r1.a ORDER BY r1.a)", "aagg"),
    ("count(*), sum(r1.c), min(r1.b)", "multi"),
    # expression-valued agg args — agg_eval_at / partial-agg tlist path
    ("sum(r1.c * (r1.a % 3))", "expragg"),
    ("count(CASE WHEN r1.a % 2 = 0 THEN 1 END)", "caseagg"),
    ("sum(r1.c) + count(*)", "compagg"),
    ("max(COALESCE(r1.c, 0))", "coalesceagg"),
]

# Gkeys are (expr, source cols side): 'r1' / 'r2' / 'mixed'
GKEYS = [
    ("r2.y", "r2"),
    ("r1.b", "r1"),
    ("r1.b, r2.y", "mixed"),
    ("r1.b % 5", "r1expr"),
    ("r2.y % 3", "r2expr"),
]

# Arms whose FROM clause exposes only r2: group keys must be r2-side.
R2_ONLY_ARMS = {"semi_exists", "semi_in", "semi_in_dup",
                "anti_notexists", "anti_notin", "lateral"}
# Arms that alias r1 as 'a': rewrite r1.* column refs post-substitution.
ALIAS_A_ARMS = {"selfjoin"}


def join_queries(shape: str) -> list[tuple[str, str]]:
    """(arm_name, query_template) pairs. {AGG} {G} substituted later."""
    q = []
    # --- binary joins, agg cols on r1 ---
    q.append(("inner", "SELECT {G}, {AGG} FROM r1 JOIN r2 ON r1.b = r2.x "
             "GROUP BY {G}"))
    q.append(("inner_exprj", "SELECT {G}, {AGG} FROM r1 JOIN r2 "
              "ON r1.b + 0 = r2.x GROUP BY {G}"))
    q.append(("inner_extraqual", "SELECT {G}, {AGG} FROM r1 JOIN r2 "
              "ON r1.b = r2.x AND r1.a > 5 GROUP BY {G}"))
    q.append(("left", "SELECT {G}, {AGG} FROM r1 LEFT JOIN r2 "
              "ON r1.b = r2.x GROUP BY {G}"))
    q.append(("right_r1agg", "SELECT {G}, {AGG} FROM r1 RIGHT JOIN r2 "
              "ON r1.b = r2.x GROUP BY {G}"))
    q.append(("full", "SELECT {G}, {AGG} FROM r1 FULL JOIN r2 "
              "ON r1.b = r2.x GROUP BY {G}"))
    # --- agg cols on r2 (left_rev puts r2 on non-nullable side) ---
    q.append(("left_r2agg", "SELECT {G}, {AGG2} FROM r2 LEFT JOIN r1 "
              "ON r1.b = r2.x GROUP BY {G}"))
    q.append(("right_r2agg", "SELECT {G}, {AGG2} FROM r1 RIGHT JOIN r2 "
              "ON r1.b = r2.x GROUP BY {G}"))
    # --- semi / anti (agg cols from outer side r2) ---
    q.append(("semi_exists", "SELECT {G}, {AGG2} FROM r2 WHERE EXISTS "
              "(SELECT 1 FROM r1 WHERE r1.b = r2.x) GROUP BY {G}"))
    q.append(("semi_in", "SELECT {G}, {AGG2} FROM r2 WHERE r2.x IN "
              "(SELECT r1.b FROM r1) GROUP BY {G}"))
    q.append(("semi_in_dup", "SELECT {G}, {AGG2} FROM r2 WHERE r2.x IN "
              "(SELECT r1.b FROM r1 WHERE r1.a > 3) GROUP BY {G}"))
    q.append(("anti_notexists", "SELECT {G}, {AGG2} FROM r2 WHERE NOT EXISTS "
              "(SELECT 1 FROM r1 WHERE r1.b = r2.x) GROUP BY {G}"))
    q.append(("anti_notin", "SELECT {G}, {AGG2} FROM r2 WHERE r2.x NOT IN "
              "(SELECT r1.b FROM r1) GROUP BY {G}"))
    # --- 3-table / nested ---
    q.append(("three_inner", "SELECT {G}, {AGG} FROM r1 JOIN r2 "
              "ON r1.b = r2.x JOIN r3 ON r2.y = r3.w GROUP BY {G}"))
    q.append(("three_mixed", "SELECT {G}, {AGG} FROM r1 JOIN r2 "
              "ON r1.b = r2.x LEFT JOIN r3 ON r2.y = r3.w GROUP BY {G}"))
    # --- lateral ---
    q.append(("lateral", "SELECT r2.y, sum(s.v) FROM r2 JOIN LATERAL "
              "(SELECT sum(r1.c) v, r1.b FROM r1 WHERE r1.b = r2.x "
              "GROUP BY r1.b) s ON true GROUP BY r2.y"))
    # --- having / grouping sets ---
    q.append(("having", "SELECT {G}, {AGG} FROM r1 JOIN r2 ON r1.b = r2.x "
              "GROUP BY {G} HAVING sum(r1.c) > 10"))
    # HAVING references aggregates/vars absent from the tlist — exercises
    # make_partial_grouping_target's hidden-column path
    q.append(("having_hidden", "SELECT {G} FROM r1 JOIN r2 ON r1.b = r2.x "
              "GROUP BY {G} HAVING count(*) > 5 AND min(r1.a) < 500"))
    q.append(("having_onlyagg", "SELECT r2.y FROM r1 JOIN r2 "
              "ON r1.b = r2.x GROUP BY r2.y HAVING sum(r1.c) > 50"))
    q.append(("gsets", "SELECT r1.b, r2.y, {AGG} FROM r1 JOIN r2 "
              "ON r1.b = r2.x GROUP BY GROUPING SETS ((r1.b),(r2.y))"))
    q.append(("rollup", "SELECT r1.b, r2.y, {AGG} FROM r1 JOIN r2 "
              "ON r1.b = r2.x GROUP BY ROLLUP (r1.b, r2.y)"))
    # --- distinct-as-group (agg over distinct join output) ---
    q.append(("distinct_g", "SELECT {G}, {AGG} FROM "
              "(SELECT DISTINCT r1.b, r1.c, r1.a, r1.d FROM r1) r1 "
              "JOIN r2 ON r1.b = r2.x GROUP BY {G}"))
    # --- self-join on UNIQUE col: SJE eliminates one copy, then eager ---
    q.append(("selfjoin", "SELECT {G}, {AGG} FROM r1 a JOIN r1 b "
              "ON a.a = b.a JOIN r2 ON a.b = r2.x GROUP BY {G}"))
    # --- cross-side grouping: agg cols r1, group key only r2 ---
    q.append(("xside", "SELECT r2.y, {AGG} FROM r1 JOIN r2 "
              "ON r1.b = r2.x GROUP BY r2.y ORDER BY r2.y"))
    # --- agg cols on r1 but join key is r2 expr ---
    q.append(("inner_jexpr2", "SELECT {G}, {AGG} FROM r1 JOIN r2 "
              "ON r1.b = r2.x * 1 GROUP BY {G}"))
    return q


# Arms observed to fire eager aggregation on 20devel get 75% weight;
# boundary arms (semi/anti/full/grouping-sets) stay as coverage evidence.
FIRED_PRONE = {"inner", "inner_exprj", "inner_extraqual", "left",
               "three_inner", "three_mixed", "having", "distinct_g",
               "selfjoin", "selfjoin_simple", "xside", "inner_jexpr2",
               "having_hidden", "having_onlyagg"}


# agg variants that draw columns from r2 (for the *_r2agg / semi arms)
AGGS2 = [
    ("count(*)", "cnt"),
    ("count(r2.z)", "cntz"),
    ("sum(r2.z)", "sumz"),
    ("min(r2.y)", "miny"),
    ("avg(r2.x)", "avgx"),
    ("count(DISTINCT r2.y)", "cntdy"),
    ("sum(r2.z) FILTER (WHERE r2.y % 2 = 0)", "sumflt"),
]

# Decomposable aggregates (the only ones eager agg can fire on) get 75%
# of picks; DISTINCT/ordered-set aggs stay as not_fired coverage.
_NONDECOMP = {"cntdb", "sumdc", "sagg", "aagg", "cntdy"}


def gen_cases(seed: int, per_shape: int) -> list[dict]:
    rng = random.Random(seed)
    cases: list[dict] = []
    for shape, setup in TABLE_SHAPES.items():
        for i in range(per_shape):
            nulls = rng.random() < 0.35
            xtype = "bigint" if rng.random() < 0.3 else "int"
            n1 = rng.choice([120, 300, 800])
            n2 = rng.choice([80, 200, 500])
            g1 = rng.choice([4, 7, 40])
            g2 = rng.choice([3, 11, 60])
            pool = join_queries(shape)
            if rng.random() < 0.75:
                pool = [p for p in pool if p[0] in FIRED_PRONE] or pool
            arm, tmpl = rng.choice(pool)
            uses_r2 = "AGG2" in tmpl
            aggpool = AGGS2 if uses_r2 else AGGS
            if rng.random() < 0.75:
                decomp = [a for a in aggpool if a[1] not in _NONDECOMP]
                agg, atag = rng.choice(decomp or aggpool)
            else:
                agg, atag = rng.choice(aggpool)
            gkey, gside = rng.choice(GKEYS)
            if arm in R2_ONLY_ARMS and gside not in ("r2", "r2expr"):
                gkey, gside = rng.choice(
                    [g for g in GKEYS if g[1] in ("r2", "r2expr")])
            # skip obviously-unfirable combos: agg side ≠ gkey source is
            # fine (composite R1' model), keep all.
            q = tmpl.replace("{AGG2}", agg).replace("{AGG}", agg)
            if "{G}" in q:
                q = q.replace("{G}", gkey)
            if arm in ALIAS_A_ARMS:
                q = q.replace("r1.", "a.")
            cases.append({
                "name": f"{shape}_{arm}_{atag}_{i}",
                "setup": [s.format(xtype=xtype) for s in setup],
                "data": data_sqls(rng, n1, n2, g1, g2, nulls, shape),
                "query": q,
                "arm": arm, "shape": shape, "xtype": xtype,
            })
    return cases


# -------------------------------------------------------------------
# plan gate: Partial aggregate below a Join
# -------------------------------------------------------------------

JOIN_NODES = {"Nested Loop", "Merge Join", "Hash Join"}


def _walk(node: dict, under_join: bool, hits: list) -> None:
    ntype = node.get("Node Type", "")
    now_under = under_join or ntype in JOIN_NODES
    if "Aggregate" in ntype and node.get("Partial Mode") == "Partial":
        hits.append((ntype, under_join))
    for child in node.get("Plans", []):
        _walk(child, now_under, hits)


def eager_fired(plan_json) -> tuple[bool, list]:
    """True iff some Partial aggregate sits below a join node."""
    try:
        root = plan_json[0]["Plan"]
    except (TypeError, IndexError, KeyError):
        return False, []
    hits: list = []
    _walk(root, False, hits)
    fired = any(under for _, under in hits)
    return fired, hits


# -------------------------------------------------------------------
# runner
# -------------------------------------------------------------------

def run_case(pg: PostgresRunner, case: dict, timeout: float,
             secondary: list[str] | None = None) -> dict:
    out = {"name": case["name"], "arm": case["arm"], "shape": case["shape"],
           "query": case["query"], "status": "ok"}
    setup_res = pg.setup(case["setup"] + case["data"])
    if any(err for _, err in setup_res):
        out["status"] = "setup_fail"
        out["detail"] = [e for _, e in setup_res if e][:2]
        return out

    sec = [f"SET {g}" for g in (secondary or [])]
    set_on = ["SET enable_eager_aggregate = on",
              "SET max_parallel_workers_per_gather = 0"] + sec
    set_off = ["SET enable_eager_aggregate = off",
               "SET max_parallel_workers_per_gather = 0"] + sec

    # two firing attempts: default threshold, then forced (threshold=0)
    fired = False
    hits = []
    for force in (False, True):
        for s in set_on:
            pg.run(s)
        if force:
            pg.run("SET min_eager_agg_group_size = 0")
        else:
            pg.run("RESET min_eager_agg_group_size")
        plan = pg.explain_plan(case["query"])
        if plan is None:
            out["status"] = "explain_fail"
            return out
        f, hits = eager_fired(plan)
        if f:
            fired = True
            out["forced"] = force
            break
    if not fired:
        out["status"] = "not_fired"
        out["partials"] = [h[0] for h in hits]
        return out
    out["partial_nodes"] = [h[0] for h in hits]

    # ON result
    for s in set_on:
        pg.run(s)
    r_on = pg.run(case["query"], timeout_s=timeout)
    # OFF result
    for s in set_off:
        pg.run(s)
    r_off = pg.run(case["query"], timeout_s=timeout)
    # restore defaults
    pg.run("SET enable_eager_aggregate = on")

    if r_on.is_internal_error or r_off.is_internal_error:
        out["status"] = "crash"
        out["on_err"] = r_on.error
        out["off_err"] = r_off.error
        return out
    if not r_on.ok or not r_off.ok:
        # statement-level error (legal e.g. divisor) — only flag if the
        # two sides disagree on *whether* it errors
        if (r_on.ok) != (r_off.ok):
            out["status"] = "hit_errdiff"
            out["on_err"] = r_on.error
            out["off_err"] = r_off.error
            return out
        out["status"] = "stmt_err"
        out["detail"] = r_on.error
        return out
    if r_on.bag() != r_off.bag():
        out["status"] = "hit"
        out["on_rows"] = r_on.rows[:50]
        out["off_rows"] = r_off.rows[:50]
        return out
    out["n_rows"] = len(r_on.rows)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="pgbld/pgmaster_assert")
    ap.add_argument("--datadir", default="results/pg_eager_agg/dd")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--per-shape", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--filter", default="")
    ap.add_argument("--secondary-gucs", default="",
                    help="comma-separated g=v applied to both runs, e.g. "
                         "'enable_partitionwise_aggregate=on'")
    ap.add_argument("--out", default="results/pg_eager_agg")
    args = ap.parse_args()

    prefix = str(Path(args.prefix).resolve())
    if "asan" in prefix:
        os.environ.setdefault("ASAN_OPTIONS", "detect_leaks=0")

    cases = gen_cases(args.seed, args.per_shape)
    if args.filter:
        cases = [c for c in cases if args.filter in c["name"]]
    LOGGER.info("%d cases, prefix=%s", len(cases), prefix)

    pg = PostgresRunner(args.datadir, pg_prefix=prefix)
    stats: Counter = Counter()
    findings: list[dict] = []
    arm_stats: dict[str, Counter] = {}
    t0 = time.monotonic()
    try:
        secondary = [s.strip() for s in args.secondary_gucs.split(",")
                     if s.strip()]
        for i, case in enumerate(cases):
            res = run_case(pg, case, args.timeout, secondary)
            stats[res["status"]] += 1
            arm_stats.setdefault(case["arm"], Counter())[res["status"]] += 1
            if res["status"].startswith("hit") or res["status"] == "crash":
                findings.append(res)
                LOGGER.warning("HIT %s: %s", res["status"], res["name"])
            if i % 25 == 0:
                LOGGER.info("case %d/%d stats=%s", i, len(cases),
                            dict(stats))
            fatals = pg.log_fatal_lines(pg.log_new_lines())
            if fatals:
                LOGGER.warning("server log fatal: %s", fatals[:3])
                stats["log_fatal"] += 1
    finally:
        pg.cleanup()

    elapsed = time.monotonic() - t0
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    build = Path(prefix).name
    summary = {
        "build": build, "seed": args.seed, "cases": len(cases),
        "secondary_gucs": args.secondary_gucs,
        "stats": dict(stats), "elapsed_s": round(elapsed, 1),
        "findings": findings,
        "arm_stats": {k: dict(v) for k, v in arm_stats.items()},
    }
    tag = ("_" + args.secondary_gucs.replace("=", "").replace(",", "_")
           .replace(" ", "")) if args.secondary_gucs else ""
    (outdir / f"run_{build}_s{args.seed}{tag}.json").write_text(
        json.dumps(summary, indent=2, default=str))
    LOGGER.info("DONE %s stats=%s fired_rate=%d/%d",
                build, dict(stats), stats.get("ok", 0), len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
