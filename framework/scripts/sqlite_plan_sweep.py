"""SQLite plan-invariance sweep.

For each case (setup_sqls, query), run the query under N plan-relevant
settings and compare result bags. Any deterministic difference in the
bag = candidate bug (index-presence oracle / plan invariance violated).

Variants per case:
  base         -- plain tables
  idx          -- CREATE INDEX on each column of each table (one per variant too)
  idx_all      -- one composite run: all single-col indexes
  partial      -- partial indexes (WHERE col IS NOT NULL)
  expridx      -- expression index on (col+0)
  auto_off     -- PRAGMA automatic_index=off
  rev_unord    -- PRAGMA reverse_unordered_selects=on
  stat_big     -- UPDATE sqlite_stat1 rows to claim huge tables + ANALYZE off
  stat_small   -- tiny stat1
  analyze      -- ANALYZE
  idx+auto_off, idx+rev_unord combos

Usage: python3 scripts/sqlite_plan_sweep.py [--seed-file seeds/x.py]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRAGMAS = {
    "auto_off": ["PRAGMA automatic_index=off"],
    "rev_unord": ["PRAGMA reverse_unordered_selects=on"],
    "both": ["PRAGMA automatic_index=off",
             "PRAGMA reverse_unordered_selects=on"],
}


def cols_of(con, table):
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def tables_of(setup_sqls):
    out = []
    for s in setup_sqls:
        for m in re.finditer(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", s, re.I):
            out.append(m.group(1))
    return out


def norm(v):
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return int(v)
    if isinstance(v, bytes):
        return "B:" + v.hex()
    return v


def run_case(setup_sqls, query, extra_ddl=None, pragmas=None,
             stat_override=None):
    con = sqlite3.connect(":memory:")
    try:
        for p in (pragmas or []):
            con.execute(p)
        con.executescript(";".join(setup_sqls) + ";")
        for d in (extra_ddl or []):
            try:
                con.execute(d)
            except sqlite3.Error:
                pass
        if stat_override:
            con.execute("ANALYZE")
            for t in tables_of(setup_sqls):
                con.execute(
                    "UPDATE sqlite_stat1 SET stat=? WHERE tbl=?",
                    (stat_override, t))
        cur = con.execute(query)
        rows = [[norm(v) for v in row] for row in cur.fetchall()]
        return ("ok", Counter(tuple(r) for r in rows))
    except sqlite3.Error as e:
        return ("err", f"{type(e).__name__}: {e}")
    finally:
        con.close()


def variants_for(setup_sqls, query):
    """Yield (name, extra_ddl, pragmas, stat_override) tuples."""
    yield "base", [], None, None
    tabs = tables_of(setup_sqls)
    # probe connection for column names
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(";".join(setup_sqls) + ";")
        cols = {t: cols_of(con, t) for t in tabs}
    except sqlite3.Error:
        cols = {t: [] for t in tabs}
    finally:
        con.close()

    # single-column indexes, one variant each
    i = 0
    for t in tabs:
        for c in cols.get(t, []):
            yield f"idx_{t}_{c}", [
                f"CREATE INDEX sidx{i} ON {t}({c})"], None, None
            i += 1
    all_idx = [f"CREATE INDEX aidx{j} ON {t}({c})"
               for j, (t, c) in enumerate(
                   (t, c) for t in tabs for c in cols.get(t, []))]
    if all_idx:
        yield "idx_all", all_idx, None, None
        yield "idx_all+auto_off", all_idx, PRAGMAS["auto_off"], None
        yield "idx_all+rev", all_idx, PRAGMAS["rev_unord"], None
        yield "idx_all+both", all_idx, PRAGMAS["both"], None
        # covering composite indexes on (first, second) col pairs
        for t in tabs:
            cs = cols.get(t, [])
            if len(cs) >= 2:
                yield ("covidx_" + t,
                       [f"CREATE INDEX cix_{t} ON {t}({','.join(cs)})"],
                       None, None)
    part = [f"CREATE INDEX pix{j} ON {t}({c}) WHERE {c} IS NOT NULL"
            for j, (t, c) in enumerate(
                (t, c) for t in tabs for c in cols.get(t, []))]
    if part:
        yield "partial", part, None, None
    expr = [f"CREATE INDEX eix{j} ON {t}({c}+0)"
            for j, (t, c) in enumerate(
                (t, c) for t in tabs for c in cols.get(t, []))]
    if expr:
        yield "expridx", expr, None, None
    yield "auto_off", [], PRAGMAS["auto_off"], None
    yield "rev_unord", [], PRAGMAS["rev_unord"], None
    yield "analyze", ["ANALYZE"], None, None
    yield "stat_big", [], None, "1000000 10"
    yield "stat_big+idx", all_idx, None, "1000000 10"


def sweep(seeds, limit=0, verbose=False):
    findings = []
    for si, seed in enumerate(seeds):
        if limit and si >= limit:
            break
        setup, query = seed["setup_sqls"], seed["query"]
        base_status, base = run_case(setup, query)
        results = {"base": (base_status, base)}
        for name, ddl, prag, stat in variants_for(setup, query):
            st, bag = run_case(setup, query, ddl, prag, stat)
            results[name] = (st, bag)
            if verbose:
                print(f"  {seed.get('source', si)} {name}: {st} "
                      f"{bag if st == 'err' else sum(bag.values())}")
        okbags = {n: b for n, (s, b) in results.items() if s == "ok"}
        errv = {n: b for n, (s, b) in results.items() if s == "err"}
        distinct = set()
        for b in okbags.values():
            distinct.add(tuple(sorted(
                ((tuple(repr(x) for x in k)), v) for k, v in b.items())))
        if len(distinct) > 1 or (errv and base_status == "ok"):
            findings.append({
                "source": seed.get("source", f"seed{si}"),
                "setup": setup, "query": query,
                "results": {n: (s, ({str(k): v for k, v in b.items()}
                                  if s == "ok" else b))
                            for n, (s, b) in results.items()}})
            print(f"[DIVERGENT] {seed.get('source', si)}: "
                  f"{len(distinct)} distinct bags, errs={list(errv)}")
    return findings


CASES: list[dict] = []


def _gen_builtin_cases():
    """Hand-built query shapes targeting known-fragile planner paths."""
    S = []
    setup_ab = [
        "CREATE TABLE a(x INT, y INT)",
        "INSERT INTO a VALUES (1,10),(2,20),(3,30),(NULL,99),(2,21)",
        "CREATE TABLE b(x INT, z INT)",
        "INSERT INTO b VALUES (2,200),(3,300),(4,400),(NULL,88),(2,201)",
    ]
    # join with OR predicate (OR-optimization path)
    S.append({"source": "builtin:join_or",
              "setup_sqls": setup_ab,
              "query": "SELECT a.x, b.x FROM a JOIN b ON a.x=b.x OR a.y=b.z "
                       "ORDER BY 1,2"})
    S.append({"source": "builtin:join_or2",
              "setup_sqls": setup_ab,
              "query": "SELECT count(*) FROM a LEFT JOIN b ON a.x=b.x "
                       "WHERE a.y>15 OR b.z>250"})
    # range + order by + limit (index could provide order)
    S.append({"source": "builtin:range_limit",
              "setup_sqls": setup_ab,
              "query": "SELECT x, y FROM a WHERE x>=1 AND x<=3 "
                       "ORDER BY y LIMIT 3"})
    S.append({"source": "builtin:range_limit2",
              "setup_sqls": setup_ab,
              "query": "SELECT x, y FROM a WHERE y>15 ORDER BY x DESC, y "
                       "LIMIT 4"})
    # DISTINCT
    S.append({"source": "builtin:distinct",
              "setup_sqls": setup_ab,
              "query": "SELECT DISTINCT x FROM a ORDER BY 1"})
    S.append({"source": "builtin:distinct_join",
              "setup_sqls": setup_ab,
              "query": "SELECT DISTINCT a.x FROM a JOIN b ON a.x=b.x "
                       "ORDER BY 1"})
    # IN subquery
    S.append({"source": "builtin:in_subq",
              "setup_sqls": setup_ab,
              "query": "SELECT y FROM a WHERE x IN (SELECT x FROM b) "
                       "ORDER BY 1"})
    S.append({"source": "builtin:not_in",
              "setup_sqls": setup_ab,
              "query": "SELECT y FROM a WHERE x NOT IN (SELECT x FROM b "
                       "WHERE x IS NOT NULL) ORDER BY 1"})
    S.append({"source": "builtin:in_or",
              "setup_sqls": setup_ab,
              "query": "SELECT y FROM a WHERE x IN (SELECT x FROM b) OR "
                       "y=99 ORDER BY 1"})
    # min/max (could use index for aggregate)
    S.append({"source": "builtin:minmax",
              "setup_sqls": setup_ab,
              "query": "SELECT min(x), max(x) FROM b"})
    S.append({"source": "builtin:minmax_where",
              "setup_sqls": setup_ab,
              "query": "SELECT min(z) FROM b WHERE x>1"})
    S.append({"source": "builtin:minmax_grp",
              "setup_sqls": setup_ab,
              "query": "SELECT x, min(z) FROM b GROUP BY x ORDER BY 1"})
    # UNION / EXCEPT with order
    S.append({"source": "builtin:union_or",
              "setup_sqls": setup_ab,
              "query": "SELECT x FROM a WHERE y>15 UNION SELECT x FROM b "
                       "WHERE z>250 ORDER BY 1"})
    # self-join with range
    S.append({"source": "builtin:selfjoin",
              "setup_sqls": setup_ab,
              "query": "SELECT a1.x, a2.y FROM a a1 JOIN a a2 ON a1.x=a2.x "
                       "AND a1.y<a2.y ORDER BY 1,2"})
    # correlated EXISTS
    S.append({"source": "builtin:exists",
              "setup_sqls": setup_ab,
              "query": "SELECT x FROM a WHERE EXISTS (SELECT 1 FROM b "
                       "WHERE b.x=a.x AND b.z>a.y) ORDER BY 1"})
    # LIKE / GLOB prefix (index-assisted LIKE)
    setup_txt = [
        "CREATE TABLE t(s TEXT, v INT)",
        "INSERT INTO t VALUES ('apple',1),('apricot',2),('banana',3),"
        "('APPle',4),('application',5),(NULL,6),('app',7)",
    ]
    S.append({"source": "builtin:like_prefix",
              "setup_sqls": setup_txt,
              "query": "SELECT v FROM t WHERE s LIKE 'app%' ORDER BY 1"})
    S.append({"source": "builtin:like_nocase",
              "setup_sqls": setup_txt,
              "query": "SELECT v FROM t WHERE s LIKE 'app%' COLLATE NOCASE "
                       "ORDER BY 1"})
    S.append({"source": "builtin:glob",
              "setup_sqls": setup_txt,
              "query": "SELECT v FROM t WHERE s GLOB 'app*' ORDER BY 1"})
    # collation on equality/range
    S.append({"source": "builtin:collate_range",
              "setup_sqls": setup_txt,
              "query": "SELECT v FROM t WHERE s>='app' COLLATE NOCASE "
                       "ORDER BY 1"})
    S.append({"source": "builtin:collate_in",
              "setup_sqls": setup_txt,
              "query": "SELECT v FROM t WHERE s COLLATE NOCASE IN "
                       "('APPLE','BANANA') ORDER BY 1"})
    # IS NULL / IS NOT NULL with index
    S.append({"source": "builtin:isnull",
              "setup_sqls": setup_ab,
              "query": "SELECT y FROM a WHERE x IS NULL ORDER BY 1"})
    S.append({"source": "builtin:notnull_or",
              "setup_sqls": setup_ab,
              "query": "SELECT y FROM a WHERE x IS NULL OR y>25 ORDER BY 1"})
    # BETWEEN + IN mix
    S.append({"source": "builtin:between_in",
              "setup_sqls": setup_ab,
              "query": "SELECT y FROM a WHERE x BETWEEN 1 AND 3 AND y IN "
                       "(10,21,99) ORDER BY 1"})
    # GROUP BY with HAVING over join
    S.append({"source": "builtin:grp_having",
              "setup_sqls": setup_ab,
              "query": "SELECT a.x, count(*) FROM a LEFT JOIN b ON a.x=b.x "
                       "GROUP BY a.x HAVING count(*)>0 ORDER BY 1"})
    # multi-way join
    setup_abc = setup_ab + [
        "CREATE TABLE c(x INT, w INT)",
        "INSERT INTO c VALUES (2,7),(3,8),(5,9)",
    ]
    S.append({"source": "builtin:join3",
              "setup_sqls": setup_abc,
              "query": "SELECT a.y, b.z, c.w FROM a JOIN b ON a.x=b.x "
                       "JOIN c ON b.x=c.x ORDER BY 1,2,3"})
    S.append({"source": "builtin:join3_or",
              "setup_sqls": setup_abc,
              "query": "SELECT count(*) FROM a JOIN b ON a.x=b.x LEFT JOIN "
                       "c ON b.x=c.x OR a.y=c.w"})
    # outer join + WHERE on right table (IS NULL push-down)
    S.append({"source": "builtin:left_where",
              "setup_sqls": setup_ab,
              "query": "SELECT a.x, b.z FROM a LEFT JOIN b ON a.x=b.x "
                       "WHERE b.z IS NULL OR b.z>205 ORDER BY 1,2"})
    # skip-scan candidate: IN on first col of would-be composite index
    S.append({"source": "builtin:skipscan",
              "setup_sqls": setup_ab,
              "query": "SELECT x, z FROM b WHERE x IN (2,3) AND z>150 "
                       "ORDER BY 1,2"})
    # ORDER BY on expression that index could cover
    S.append({"source": "builtin:expr_order",
              "setup_sqls": setup_ab,
              "query": "SELECT x+0 AS xp, y FROM a ORDER BY xp, y LIMIT 4"})
    # DISTINCT aggregates
    S.append({"source": "builtin:count_distinct",
              "setup_sqls": setup_ab,
              "query": "SELECT count(DISTINCT x), count(DISTINCT y) FROM a"})
    # view + index interplay
    S.append({"source": "builtin:view_join",
              "setup_sqls": setup_ab + [
                  "CREATE VIEW v AS SELECT x, z+1 AS zp FROM b"],
              "query": "SELECT a.x, v.zp FROM a JOIN v ON a.x=v.x "
                       "ORDER BY 1,2"})
    # CASE in predicate
    S.append({"source": "builtin:case_pred",
              "setup_sqls": setup_ab,
              "query": "SELECT x FROM a WHERE CASE WHEN y>15 THEN x END = 2 "
                       "ORDER BY 1"})
    # numeric affinity edge: text vs int column comparisons
    setup_aff = [
        "CREATE TABLE t1(a, b)",   # no affinity
        "INSERT INTO t1 VALUES ('2','x'),(2,'y'),('10','z'),(10,'w')",
        "CREATE TABLE t2(a INT, b TEXT)",
        "INSERT INTO t2 VALUES ('2','x'),(2,'y'),('10','z'),(10,'w')",
    ]
    S.append({"source": "builtin:affinity_eq",
              "setup_sqls": setup_aff,
              "query": "SELECT t1.b, t2.b FROM t1 JOIN t2 ON t1.a=t2.a "
                       "ORDER BY 1,2"})
    S.append({"source": "builtin:affinity_in",
              "setup_sqls": setup_aff,
              "query": "SELECT b FROM t2 WHERE a IN ('2',2,'10',10) "
                       "ORDER BY 1"})
    # window over indexed col
    S.append({"source": "builtin:window",
              "setup_sqls": setup_ab,
              "query": "SELECT x, sum(y) OVER (ORDER BY x) FROM a "
                       "ORDER BY 1"})
    # recursive CTE + index
    S.append({"source": "builtin:cte_join",
              "setup_sqls": setup_ab + [
                  "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 "
                  "FROM r WHERE n<5) CREATE TABLE d AS SELECT n FROM r"],
              "query": "SELECT d.n, a.y FROM d LEFT JOIN a ON d.n=a.x "
                       "ORDER BY 1,2"})
    return S


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-file", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default="results/sqlite_plan_sweep.json")
    args = ap.parse_args()

    seeds = list(CASES) + _gen_builtin_cases()
    if args.seed_file:
        spec = importlib.util.spec_from_file_location("sf", args.seed_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        seeds += mod.as_seeds()

    findings = sweep(seeds, args.limit, args.verbose)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(findings, f, indent=1, default=str)
    print(f"{len(findings)} divergent cases -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
