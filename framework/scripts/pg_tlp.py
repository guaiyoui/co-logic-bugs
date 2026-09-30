#!/usr/bin/env python3
"""Full-spectrum Ternary Logic Partitioning oracle.

For every deterministic predicate p and any row r, exactly one of
  p(r),  NOT p(r),  (p IS NULL)(r)
is true.  Hence splitting any deterministic bag by p produces a
3-arm UNION ALL whose bag must equal the unpartitioned query.

Partition sites (the upstream-proven bug surface):
  WHERE    ... WHERE w  ->  WHERE w AND p / w AND NOT p / w AND p IS NULL
  HAVING   ... HAVING h ->  same split on group-level predicates
  JOIN-IN  inner join output split via outer WHERE
  AGG-IN   scalar aggregate over a WHERE-partitioned derived table
  SETOP    UNION/EXCEPT output split via outer WHERE

Predicates are generated from the same random pool on both sides; the
python model evaluates the UNPARTITIONED query and the partitioned bag
independently — a diff means the engine partitioned wrong (dropped or
duplicated partition rows), the classic TLP failure.
"""
import argparse, json, random, sys, time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.paths import pg_build_prefix  # noqa: E402


def gen_schema(rng):
    setup = [
        "DROP TABLE IF EXISTS t1 CASCADE", "DROP TABLE IF EXISTS t2 CASCADE",
        "CREATE TABLE t1(a int, b int, c int)",
        "CREATE TABLE t2(x int, y int)",
    ]
    t1 = [(rng.choice([0, 1, 2, 3, "NULL"]), rng.choice([0, 1, 2, "NULL"]),
           rng.choice([0, 1, 5, "NULL"])) for _ in range(rng.randint(4, 12))]
    t2 = [(rng.choice([0, 1, 2, 3, "NULL"]), rng.choice([0, 1, 7, "NULL"]))
          for _ in range(rng.randint(3, 9))]
    setup += ["INSERT INTO t1 VALUES " +
              ",".join("(%s,%s,%s)" % r for r in t1),
              "INSERT INTO t2 VALUES " + ",".join("(%s,%s)" % r for r in t2),
              "ANALYZE t1", "ANALYZE t2"]
    data = {"t1": [tuple(None if v == "NULL" else v for v in r) for r in t1],
            "t2": [tuple(None if v == "NULL" else v for v in r) for r in t2]}
    return setup, data


# ---------------- predicate pool ----------------
# each entry: (sql_text, python_fn(t1row)->True/False/None,
#              usable_on_cols: which row layout)
def pred_pool(rng):
    """Return (sql, py) — py(row) -> True / False / None on a t1-shaped
    row (a,b,c).  SQL uses unqualified cols a,b,c."""
    k = rng.randint(0, 6)
    k2 = rng.randint(0, 6)
    pool = [
        ("a < %d" % k,
         lambda r: None if r[0] is None else r[0] < k),
        ("a >= %d" % k,
         lambda r: None if r[0] is None else r[0] >= k),
        ("a = %d" % k,
         lambda r: None if r[0] is None else r[0] == k),
        ("b = %d" % k,
         lambda r: None if r[1] is None else r[1] == k),
        ("a IS NOT NULL", lambda r: r[0] is not None),
        ("b IS NULL", lambda r: r[1] is None),
        ("c IS NOT NULL", lambda r: r[2] is not None),
        ("a + b > %d" % k,
         lambda r: (None if r[0] is None or r[1] is None
                    else r[0] + r[1] > k)),
        ("a * b <= %d" % k,
         lambda r: (None if r[0] is None or r[1] is None
                    else r[0] * r[1] <= k)),
        ("a IN (%d, %d)" % (k, k2),
         lambda r: (None if r[0] is None else r[0] in (k, k2))),
        ("b % 2 = 0",
         lambda r, _m=None: None if r[1] is None else r[1] % 2 == 0),
        ("c <> %d" % k,
         lambda r: None if r[2] is None else r[2] != k),
        ("a + b + c IS NOT NULL",
         lambda r: r[0] is not None and r[1] is not None
         and r[2] is not None),
        ("coalesce(a, -1) < %d" % k,
         lambda r: (r[0] if r[0] is not None else -1) < k),
    ]
    return rng.choice(pool)


def pred_pool_xy(rng):
    """Same but for joined rows (a,b,c,x,y): SQL refs t1.a.., t2.x, t2.y."""
    k = rng.randint(0, 6)
    pool = [
        ("t1.a < %d" % k,
         lambda r: None if r[0] is None else r[0] < k),
        ("t2.x = %d" % k,
         lambda r: None if r[3] is None else r[3] == k),
        ("t2.y IS NULL", lambda r: r[4] is None),
        ("t1.a + t2.x > %d" % k,
         lambda r: (None if r[0] is None or r[3] is None
                    else r[0] + r[3] > k)),
        ("t1.c = t2.y",
         lambda r: (None if r[2] is None or r[4] is None
                    else r[2] == r[4])),
        ("t2.x IS NOT NULL", lambda r: r[3] is not None),
        ("t1.b % 2 = t2.y % 2",
         lambda r: (None if r[1] is None or r[4] is None
                    else r[1] % 2 == r[4] % 2)),
    ]
    return rng.choice(pool)


def triv(v):
    """python truth value -> 't'/'f'/'n'"""
    return 't' if v is True else ('f' if v is False else 'n')


def gen_cases(rng, i):
    fams = ["where", "joinin", "joinout", "having", "aggin", "setop"]
    fam = fams[i % len(fams)]
    setup, data = gen_schema(rng)
    case = {"family": fam, "id": i, "setup": setup, "variants": [],
            "closed": None}

    if fam == "where":
        p, pf = pred_pool(rng)
        w = rng.choice(["true", "a IS NOT NULL OR b IS NOT NULL",
                        "c IS NULL OR c >= 0"])
        base = ("SELECT a, b, c FROM t1 WHERE %s" % w)
        part = ("SELECT a, b, c FROM t1 WHERE (%s) AND %s" % (w, p) +
                " UNION ALL SELECT a, b, c FROM t1 WHERE (%s) AND NOT (%s)"
                % (w, p) +
                " UNION ALL SELECT a, b, c FROM t1 WHERE (%s) AND (%s) IS NULL"
                % (w, p))
        case["variants"] = [base, part]
        # model: base bag
        wf = {"true": lambda r: True,
              "a IS NOT NULL OR b IS NOT NULL":
                  lambda r: r[0] is not None or r[1] is not None,
              "c IS NULL OR c >= 0":
                  lambda r: r[2] is None or r[2] >= 0}[w]
        case["closed"] = [tuple(r) for r in data["t1"] if wf(r)]

    elif fam == "joinin":
        p, pf = pred_pool_xy(rng)
        j = rng.choice(["t1.a = t2.x", "t1.a = t2.x AND t1.b IS NOT NULL",
                        "t1.c = t2.y"])
        base = ("SELECT t1.a, t2.x, t2.y FROM t1 JOIN t2 ON %s" % j)
        part = ("SELECT t1.a, t2.x, t2.y FROM t1 JOIN t2 ON %s WHERE %s"
                % (j, p) +
                " UNION ALL SELECT t1.a, t2.x, t2.y FROM t1 JOIN t2 ON %s"
                " WHERE NOT (%s)" % (j, p) +
                " UNION ALL SELECT t1.a, t2.x, t2.y FROM t1 JOIN t2 ON %s"
                " WHERE (%s) IS NULL" % (j, p))
        case["variants"] = [base, part]
        jf = {"t1.a = t2.x":
              lambda a, b, c, x, y: a is not None and x is not None
              and a == x,
              "t1.a = t2.x AND t1.b IS NOT NULL":
              lambda a, b, c, x, y: a is not None and x is not None
              and a == x and b is not None,
              "t1.c = t2.y":
              lambda a, b, c, x, y: c is not None and y is not None
              and c == y}[j]
        case["closed"] = [
            (a, x, y) for (a, b, c) in data["t1"] for (x, y) in data["t2"]
            if jf(a, b, c, x, y)]

    elif fam == "joinout":
        # LEFT JOIN output partitioned via WHERE — the missing-quals
        # bug shape: null-extended rows must land in the p-IS-NULL arm,
        # matched rows in p / NOT p per the predicate's truth value.
        p, pf = pred_pool_xy(rng)
        j = rng.choice(["t1.a = t2.x", "t1.c = t2.y"])
        base = ("SELECT t1.a, t2.x, t2.y FROM t1 LEFT JOIN t2 ON %s" % j)
        part = ("SELECT t1.a, t2.x, t2.y FROM t1 LEFT JOIN t2 ON %s WHERE %s"
                % (j, p) +
                " UNION ALL SELECT t1.a, t2.x, t2.y FROM t1 LEFT JOIN t2"
                " ON %s WHERE NOT (%s)" % (j, p) +
                " UNION ALL SELECT t1.a, t2.x, t2.y FROM t1 LEFT JOIN t2"
                " ON %s WHERE (%s) IS NULL" % (j, p))
        case["variants"] = [base, part]
        jf = {"t1.a = t2.x":
              lambda a, b, c, x, y: a is not None and x is not None
              and a == x,
              "t1.c = t2.y":
              lambda a, b, c, x, y: c is not None and y is not None
              and c == y}[j]
        exp = []
        for (a, b, c) in data["t1"]:
            m = [(x, y) for (x, y) in data["t2"] if jf(a, b, c, x, y)]
            exp += [(a, x, y) for (x, y) in m] or [(a, None, None)]
        case["closed"] = exp

    elif fam == "having":
        # group-level predicates on group col a or aggregate count/sum
        k = rng.randint(0, 6)
        kn = rng.randint(1, 3)
        kc = rng.randint(0, 3)
        hp, hpf = rng.choice([
            ("a < %d" % k, lambda a, g: None if a is None else a < k),
            ("a IS NOT NULL", lambda a, g: a is not None),
            ("count(*) >= %d" % kn, lambda a, g: len(g) >= kn),
            ("sum(c) > %d" % k,
             lambda a, g: (lambda xs: None if not xs else sum(xs) > k)(
                 [r[2] for r in g if r[2] is not None])),
            ("count(c) = %d" % kc,
             lambda a, g: sum(1 for r in g if r[2] is not None) == kc),
            ("count(*) % 2 = 0", lambda a, g: len(g) % 2 == 0),
        ])
        base = "SELECT a, count(*) FROM t1 GROUP BY a"
        part = ("SELECT a, count(*) FROM t1 GROUP BY a HAVING %s" % hp +
                " UNION ALL SELECT a, count(*) FROM t1 GROUP BY a"
                " HAVING NOT (%s)" % hp +
                " UNION ALL SELECT a, count(*) FROM t1 GROUP BY a"
                " HAVING (%s) IS NULL" % hp)
        case["variants"] = [base, part]
        groups = {}
        for r in data["t1"]:
            groups.setdefault(r[0], []).append(r)
        case["closed"] = [(a, len(g)) for a, g in groups.items()]

    elif fam == "aggin":
        # scalar aggregate over WHERE-partitioned derived table
        p, pf = pred_pool(rng)
        agg = rng.choice(["count(*)", "count(c)", "sum(c)", "sum(a)"])
        base = "SELECT %s FROM t1" % agg
        part = ("SELECT %s FROM (SELECT * FROM t1 WHERE %s UNION ALL"
                " SELECT * FROM t1 WHERE NOT (%s) UNION ALL"
                " SELECT * FROM t1 WHERE (%s) IS NULL) s"
                % (agg, p, p, p))
        case["variants"] = [base, part]
        if agg == "count(*)":
            case["closed"] = [(len(data["t1"]),)]
        elif agg == "count(c)":
            case["closed"] = [(sum(1 for r in data["t1"]
                                   if r[2] is not None),)]
        elif agg == "sum(c)":
            xs = [r[2] for r in data["t1"] if r[2] is not None]
            case["closed"] = [(sum(xs) if xs else None,)]
        else:
            xs = [r[0] for r in data["t1"] if r[0] is not None]
            case["closed"] = [(sum(xs) if xs else None,)]

    elif fam == "setop":
        while True:
            p, pf = pred_pool(rng)
            if "c" not in p.replace("coalesce(a, -1)", ""):
                break
        # UNION (dedup) of two selects, output partitioned — set
        # semantics make the partition legal as a derived table.
        base = ("SELECT a, b FROM t1 UNION SELECT x, y FROM t2")
        part = ("SELECT a, b FROM (SELECT a, b FROM t1 UNION"
                " SELECT x, y FROM t2) s WHERE %s" % p +
                " UNION ALL SELECT a, b FROM (SELECT a, b FROM t1 UNION"
                " SELECT x, y FROM t2) s WHERE NOT (%s)" % p +
                " UNION ALL SELECT a, b FROM (SELECT a, b FROM t1 UNION"
                " SELECT x, y FROM t2) s WHERE (%s) IS NULL" % p)
        case["variants"] = [base, part]
        s = set((a, b) for (a, b, _c) in data["t1"]) | \
            set((x, y) for (x, y) in data["t2"])
        case["closed"] = list(s)

    return case


def run_case(pg, case):
    try:
        pg.setup(case["setup"])
    except Exception as e:
        return {"family": case["family"], "id": case["id"],
                "verdict": "setup_error", "err": repr(e)[:300]}
    out = {"family": case["family"], "id": case["id"]}
    bags = []
    err = None
    for q in case["variants"]:
        r = pg.run(q)
        if not r.ok:
            err = repr(r.error)[:200]
            bags = None
            break
        bags.append(Counter(map(tuple, r.rows)))
    out["nvariants"] = len(case["variants"])
    if err is not None:
        out["verdict"] = "query_error"
        out["err"] = err
        out["q"] = q
        return out
    base = bags[0]
    diffs = [j for j, b in enumerate(bags[1:], 1) if b != base]
    out["pair_diffs"] = diffs
    if case.get("closed") is not None:
        want = Counter(tuple(r) if isinstance(r, tuple) else (r,)
                       for r in case["closed"])
        out["closed_diff"] = (base != want)
        if out["closed_diff"]:
            out["extra"] = [list(x) for x in (base - want).elements()][:5]
            out["missing"] = [list(x) for x in (want - base).elements()][:5]
    bad = bool(diffs) or out.get("closed_diff")
    out["verdict"] = "DIFF" if bad else "ok"
    if bad:
        out["variants"] = case["variants"]
        out["setup"] = case["setup"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="pgmaster_assert")
    ap.add_argument("--cases", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    prefix = pg_build_prefix(args.build)
    outdir = Path("results/tlp/%s_s%d" % (args.build, args.seed))
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    pg = PostgresRunner(datadir="/tmp/tlp_%s_%d" % (args.build, args.seed),
                        pg_prefix=prefix)
    counts = Counter()
    t0 = time.time()
    with open(outdir / "report.jsonl", "w") as fh:
        for i in range(args.cases):
            case = gen_cases(rng, i)
            r = run_case(pg, case)
            counts["%s/%s" % (r["family"], r["verdict"])] += 1
            fh.write(json.dumps(r, default=str) + "\n")
            if i % 50 == 0:
                print("[%s] %d/%d %s (%.0fs)" % (args.build, i, args.cases,
                                               dict(counts), time.time() - t0),
                      flush=True)
    print("DONE", dict(counts))
    print("report:", outdir / "report.jsonl")


if __name__ == "__main__":
    main()
