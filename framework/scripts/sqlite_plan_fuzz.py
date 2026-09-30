"""Randomized plan-invariance fuzz for SQLite.

Generates random SELECT queries over a fixed rich schema (NULLs, mixed
affinity, text) and checks each under a battery of index/pragma/stat
variants. Prints divergent cases. Deterministic via --seed.
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from collections import Counter

SCHEMA = (
    "CREATE TABLE t(x, y, z);"
    "CREATE TABLE u(x, w);"
    "CREATE TABLE s(v TEXT, n INT);"
)


def gen_data(rng):
    xs = [None, 0, 1, 2, 3, 5, -1, 10, 2, None, 7]
    ys = [None, 5, 10, 15, 20, 25, 50, 70, 99, 21]
    zs = [None, 'a', 'b', 'c', 'aa', 'A', 'app', '2', 'x']
    vs = [None, 'a', 'A', 'b', 'B', 'apple', 'Apple', 'app', 'x', '10', '2']
    ws = [None, 1, 2, 5, 7, 8, 10, 20]
    t = ", ".join(f"({x},{y},{'NULL' if z is None else repr(z)})"
                  for x, y, z in
                  [(rng.choice(xs), rng.choice(ys), rng.choice(zs))
                   for _ in range(rng.randint(8, 14))])
    u = ", ".join(f"({x},{w})" for x, w in
                  [(rng.choice(xs), rng.choice(ws))
                   for _ in range(rng.randint(6, 10))])
    s = ", ".join(f"({'NULL' if v is None else repr(v)},{n})"
                  for v, n in [(rng.choice(vs), rng.randint(0, 12))
                               for _ in range(rng.randint(8, 12))])
    return (f"INSERT INTO t VALUES {t};"
            f"INSERT INTO u VALUES {u};"
            f"INSERT INTO s VALUES {s};")


COLS = {"t": ["x", "y", "z"], "u": ["x", "w"], "s": ["v", "n"]}


def rpred(rng, tbl, depth=0):
    c = rng.choice(COLS[tbl])
    lit = rng.choice(["NULL", "0", "1", "2", "3", "5", "10", "-1",
                      "'a'", "'b'", "'app'", "'2'", "2.5"])
    k = rng.randint(0, 13 if depth < 2 else 9)
    if k == 0:
        return f"{tbl}.{c} IS NULL"
    if k == 1:
        return f"{tbl}.{c} IS NOT NULL"
    if k == 2:
        return f"{tbl}.{c} = {lit}"
    if k == 3:
        return f"{tbl}.{c} <> {lit}"
    if k == 4:
        return f"{tbl}.{c} > {lit}"
    if k == 5:
        return f"{tbl}.{c} BETWEEN {lit} AND {rng.choice(['5','10','100'])}"
    if k == 6:
        return (f"{tbl}.{c} IN ({lit}, {rng.choice(['1','2','3','NULL'])})")
    if k == 7:
        return f"{tbl}.{c} IN (SELECT x FROM u)"
    if k == 8:
        return f"EXISTS (SELECT 1 FROM u WHERE u.x={tbl}.{c})"
    if k == 9:
        op = rng.choice(["AND", "OR"])
        return f"({rpred(rng, tbl, depth+1)} {op} {rpred(rng, tbl, depth+1)})"
    if k == 10:
        return f"NOT ({rpred(rng, tbl, depth+1)})"
    if k == 11:
        return f"{tbl}.{c} IS NOT {lit}"
    if k == 12:
        return f"coalesce({tbl}.{c},{lit}) = {lit}"
    return f"abs({tbl}.{c}) > {rng.randint(0,5)}"


def gen_query(rng):
    kind = rng.randint(0, 9)
    if kind <= 2:
        tbl = rng.choice(["t", "u", "s"])
        q = f"SELECT {tbl}.rowid, * FROM {tbl} WHERE {rpred(rng, tbl)}"
        q += rng.choice(["", " ORDER BY 1", " ORDER BY 1 LIMIT 5",
                         " ORDER BY 2,3 LIMIT 7"])
        return q
    if kind == 3:
        return (f"SELECT t.x, t.y, u.w FROM t "
                f"{rng.choice(['JOIN','LEFT JOIN','INNER JOIN'])} u "
                f"ON t.x=u.x WHERE {rpred(rng,'t')} ORDER BY 1,2,3")
    if kind == 4:
        return (f"SELECT x, count(*), min(w) FROM u WHERE {rpred(rng,'u')} "
                f"GROUP BY x ORDER BY 1")
    if kind == 5:
        return (f"SELECT min(x), max(x), count(x) FROM t "
                f"WHERE {rpred(rng,'t')}")
    if kind == 6:
        return (f"SELECT x FROM u WHERE {rpred(rng,'u')} "
                f"{rng.choice(['UNION','UNION ALL','INTERSECT','EXCEPT'])} "
                f"SELECT x FROM t WHERE {rpred(rng,'t')} ORDER BY 1")
    if kind == 7:
        tbl = rng.choice(["t", "s"])
        return (f"SELECT * FROM {tbl} WHERE {rpred(rng,tbl)} "
                f"ORDER BY 1,2 LIMIT {rng.randint(2,8)} "
                f"OFFSET {rng.randint(0,2)}")
    if kind == 8:
        return (f"SELECT x, y, CASE WHEN x>2 THEN 'big' ELSE 'sml' END "
                f"FROM t WHERE {rpred(rng,'t')} ORDER BY 1,2")
    return (f"SELECT v, n FROM s WHERE {rpred(rng,'s')} ORDER BY 1,2")


def variants(setup):
    """(name, extra_setup, pragmas)"""
    idxs = []
    i = 0
    for t, cs in COLS.items():
        for c in cs:
            idxs.append(f"CREATE INDEX f{i} ON {t}({c})")
            i += 1
    yield "base", "", ""
    yield "idx_all", ";".join(idxs), ""
    yield "idx_partial", ";".join(
        f"CREATE INDEX p{j} ON {t}({c}) WHERE {c} IS NOT NULL"
        for j, (t, c) in enumerate(
            (t, c) for t in COLS for c in COLS[t])), ""
    yield "idx_expr", ";".join(
        f"CREATE INDEX e{j} ON {t}(coalesce({c},-1))"
        for j, (t, c) in enumerate(
            (t, c) for t in COLS for c in COLS[t])), ""
    yield "idx_desc", ";".join(
        f"CREATE INDEX d{j} ON {t}({c} DESC)"
        for j, (t, c) in enumerate(
            (t, c) for t in COLS for c in COLS[t])), ""
    yield "auto_off", "", "PRAGMA automatic_index=off"
    yield "rev", "", "PRAGMA reverse_unordered_selects=on"
    yield "idx+rev", ";".join(idxs), "PRAGMA reverse_unordered_selects=on"
    yield "idx+auto_off", ";".join(idxs), "PRAGMA automatic_index=off"
    yield "analyzed", "ANALYZE", ""
    yield "idx_analyzed", ";".join(idxs) + ";ANALYZE", ""
    yield "stat_huge", "ANALYZE;UPDATE sqlite_stat1 SET stat='900000 9'", ""
    yield "idx_huge", ";".join(idxs) + (
        ";ANALYZE;UPDATE sqlite_stat1 SET stat='900000 9'"), ""
    yield "stat_skip", "ANALYZE;UPDATE sqlite_stat1 SET stat='900000 45000'", ""
    yield "idx_skip", ";".join(idxs) + (
        ";ANALYZE;UPDATE sqlite_stat1 SET stat='900000 45000'"), ""


def norm(v):
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return int(v)
    if isinstance(v, bytes):
        return "B:" + v.hex()
    return v


def run(setup, q):
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(setup)
        rows = con.execute(q).fetchall()
        return ("ok", Counter(tuple(norm(v) for v in r) for r in rows))
    except Exception as e:
        return ("err", f"{type(e).__name__}: {e}")
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="results/sqlite_plan_fuzz.jsonl")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    found = 0
    import json, os
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    fout = open(args.out, "a")
    for it in range(args.iters):
        data = gen_data(rng)
        q = gen_query(rng)
        bags = {}
        for name, extra, pragma in variants(SCHEMA + data):
            st, bag = run(SCHEMA + data + extra + ";" + pragma, q)
            bags[name] = (st, bag)
        okb = [b for s, b in bags.values() if s == "ok"]
        errs = {n: b for n, (s, b) in bags.items() if s == "err"}
        canon = set()
        for b in okb:
            canon.add(tuple(sorted((tuple(map(repr, k)), v)
                                   for k, v in b.items())))
        base_st, base_bag = bags["base"]
        if len(canon) > 1 or (errs and base_st == "ok"):
            found += 1
            rec = {"iter": it, "query": q, "data": data,
                   "results": {n: [s, {str(k): v for k, v in b.items()}
                               if s == "ok" else b]
                               for n, (s, b) in bags.items()}}
            fout.write(json.dumps(rec) + "\n")
            fout.flush()
            print(f"[{it}] DIVERGENT {q[:90]}")
    print(f"done: {found} divergent of {args.iters}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
