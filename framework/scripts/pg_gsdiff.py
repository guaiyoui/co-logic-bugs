#!/usr/bin/env python3
"""GROUPING SETS differential oracle — expression-matching across grouping
sets is a distinct upstream bug vein (4+ recent wrong-result bugs:
grouping() bitmask errors, expression tlist matching, DISTINCT set lists,
HAVING vs set expansion).

Invariants per case (all must produce the SAME bag, and match the
closed-form Python evaluation):

  G1  GROUPING SETS spelled out
  G2  identical expansion as UNION ALL of plain GROUP BY arms
  G3  CUBE/ROLLUP spelling when the set list is a cube/rollup
  G4  the same set list in a different order (bag identical)
  G5  GROUPING()/GROUPING_ID() flags recomputed per arm
  G6  GROUP BY DISTINCT GROUPING SETS (dedups the set list)

The Python model groups the generated rows itself — no SQL involved, so
a wrong bag on any arm is attributable.
"""
import argparse, json, random, sys, time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.paths import pg_build_prefix  # noqa: E402


def gen_schema(rng):
    setup = [
        "DROP TABLE IF EXISTS t CASCADE",
        "CREATE TABLE t(a int, b int, c int)",
    ]
    rows = []
    for _ in range(rng.randint(5, 14)):
        rows.append((rng.choice([0, 1, 2, "NULL"]),
                     rng.choice([0, 1, 2, "NULL"]),
                     rng.choice([0, 1, 5, "NULL"])))
    setup.append("INSERT INTO t VALUES " +
                 ",".join("(%s,%s,%s)" % (a, b, c) for a, b, c in
                          [(r[0], r[1], r[2]) for r in rows]))
    setup.append("ANALYZE t")
    data = [tuple(None if x == "NULL" else x for x in r) for r in rows]
    return setup, data


def py_group(rows, keyexprs, aggs):
    """Group rows by keyexprs (list of f(row)->val); emit dict rows for
    the select list.  aggs: list of (name, fn) over group rows."""
    groups = {}
    for r in rows:
        k = tuple(f(r) for f in keyexprs)
        groups.setdefault(k, []).append(r)
    out = []
    for k, g in groups.items():
        row = list(k)
        for _name, fn in aggs:
            row.append(fn(g))
        out.append(tuple(row))
    return out


def gen_cases(rng, i):
    fams = ["gs_cube", "gs_rollup", "gs_expr", "gs_dup", "gs_having",
            "gs_flag", "gs_distinct"]
    fam = fams[i % len(fams)]
    setup, data = gen_schema(rng)
    case = {"family": fam, "id": i, "setup": setup, "variants": [],
            "closed": None}
    k = rng.randint(0, 9)

    A = lambda r: r[0]
    B = lambda r: r[1]
    C = lambda r: r[2]
    _sqlsum = lambda g: (
        lambda xs: sum(xs) if xs else None)(
            [x[2] for x in g if x[2] is not None])
    sumc = [("s", _sqlsum)]
    cnt = [("n", lambda g: len(g))]
    nnc = [("n", lambda g: sum(1 for x in g if x[2] is not None))]

    if fam == "gs_cube":
        # CUBE(a,b) = sets (a,b),(a),(b),()
        case["variants"].append(
            "SELECT a, b, sum(c) FROM t GROUP BY GROUPING SETS"
            " ((a,b),(a),(b),())")
        case["variants"].append(
            "SELECT a, b, sum(c) FROM t GROUP BY CUBE(a,b)")
        case["variants"].append(
            "SELECT a, b, sum(c) FROM t GROUP BY GROUPING SETS"
            " ((),(b),(a),(a,b))")
        case["variants"].append(
            "SELECT a, b, sum(c) FROM t GROUP BY a, b UNION ALL"
            " SELECT a, NULL::int, sum(c) FROM t GROUP BY a UNION ALL"
            " SELECT NULL::int, b, sum(c) FROM t GROUP BY b UNION ALL"
            " SELECT NULL::int, NULL::int, sum(c) FROM t")
        # arm shapes: (a,b,s) (a,NULL,s) (NULL,b,s) (NULL,NULL,s)
        exp = ([(a, b, s) for (a, b, s) in
                py_group(data, [A, B], sumc)] +
               [(a, None, s) for (a, s) in py_group(data, [A], sumc)] +
               [(None, b, s) for (b, s) in py_group(data, [B], sumc)] +
               [(None, None, s) for (s,) in py_group(data, [], sumc)])
        case["closed"] = exp

    elif fam == "gs_rollup":
        # ROLLUP(a,b) = sets (a,b),(a),()
        case["variants"].append(
            "SELECT a, b, count(c) FROM t GROUP BY GROUPING SETS"
            " ((a,b),(a),())")
        case["variants"].append(
            "SELECT a, b, count(c) FROM t GROUP BY ROLLUP(a,b)")
        case["variants"].append(
            "SELECT a, b, count(c) FROM t GROUP BY a, b UNION ALL"
            " SELECT a, NULL::int, count(c) FROM t GROUP BY a UNION ALL"
            " SELECT NULL::int, NULL::int, count(c) FROM t")
        case["closed"] = (
            [(a, b, n) for (a, b, n) in py_group(data, [A, B], nnc)] +
            [(a, None, n) for (a, n) in py_group(data, [A], nnc)] +
            [(None, None, n) for (n,) in py_group(data, [], nnc)])

    elif fam == "gs_expr":
        # expression grouping keys: (a+0), (b*1) — the tlist exprs must
        # match the grouping exprs; upstream vein is tlist-expr
        # matching against the wrong set.
        case["variants"].append(
            "SELECT a+0, b*1, sum(c) FROM t GROUP BY GROUPING SETS"
            " ((a+0, b*1), (a+0), ())")
        case["variants"].append(
            "SELECT a+0, b*1, sum(c) FROM t GROUP BY a+0, b*1 UNION ALL"
            " SELECT a+0, NULL, sum(c) FROM t GROUP BY a+0 UNION ALL"
            " SELECT NULL::int, NULL::int, sum(c) FROM t")
        eA = lambda r: (None if r[0] is None else r[0] + 0)
        eB = lambda r: (None if r[1] is None else r[1] * 1)
        case["closed"] = (
            [(a, b, s) for (a, b, s) in py_group(data, [eA, eB], sumc)] +
            [(a, None, s) for (a, s) in py_group(data, [eA], sumc)] +
            [(None, None, s) for (s,) in py_group(data, [], sumc)])

    elif fam == "gs_dup":
        # duplicate set in the list: GROUPING SETS ((a),(a),(b)) must
        # emit the (a) groups TWICE — dedup is NOT legal unless
        # GROUP BY DISTINCT is spelled.
        case["variants"].append(
            "SELECT a, b, sum(c) FROM t GROUP BY GROUPING SETS"
            " ((a),(a),(b))")
        case["variants"].append(
            "SELECT a, NULL::int, sum(c) FROM t GROUP BY a UNION ALL"
            " SELECT a, NULL::int, sum(c) FROM t GROUP BY a UNION ALL"
            " SELECT NULL::int, b, sum(c) FROM t GROUP BY b")
        case["closed"] = (
            [(a, None, s) for (a, s) in py_group(data, [A], sumc)] * 2 +
            [(None, b, s) for (b, s) in py_group(data, [B], sumc)])
        # G6: GROUP BY DISTINCT dedups the set list — different bag
        case2 = dict(case)
        case2["variants"] = [
            "SELECT a, b, sum(c) FROM t GROUP BY DISTINCT GROUPING SETS"
            " ((a),(a),(b))",
            "SELECT a, b, sum(c) FROM t GROUP BY GROUPING SETS ((a),(b))"]
        case2["closed"] = (
            [(a, None, s) for (a, s) in py_group(data, [A], sumc)] +
            [(None, b, s) for (b, s) in py_group(data, [B], sumc)])
        case["second"] = case2

    elif fam == "gs_having":
        # HAVING applies per emitted group, AFTER set expansion —
        # filter sum(c) > k on every arm.
        case["variants"].append(
            "SELECT a, b, sum(c) FROM t GROUP BY GROUPING SETS"
            " ((a,b),(a),()) HAVING sum(c) > %d" % k)
        case["variants"].append(
            "SELECT a, b, sum(c) FROM t GROUP BY ROLLUP(a,b)"
            " HAVING sum(c) > %d" % k)
        case["variants"].append(
            "SELECT a, b, sum(c) FROM t GROUP BY a, b HAVING sum(c) > %d"
            " UNION ALL SELECT a, NULL::int, sum(c) FROM t GROUP BY a"
            " HAVING sum(c) > %d UNION ALL"
            " SELECT NULL::int, NULL::int, sum(c) FROM t HAVING sum(c) > %d"
            % (k, k, k))
        gt = [("s", _sqlsum)]
        case["closed"] = [
            r for r in (
                [(a, b, s) for (a, b, s) in py_group(data, [A, B], gt)] +
                [(a, None, s) for (a, s) in py_group(data, [A], gt)] +
                [(None, None, s) for (s,) in py_group(data, [], gt)])
            if r[2] is not None and r[2] > k]

    elif fam == "gs_flag":
        # GROUPING(a,b) bitmask: 1 per col absent from the active set.
        # (a,b)->0 (a)->1 (b)->2 ()->3  — absolute per-row oracle.
        case["variants"].append(
            "SELECT a, b, grouping(a, b), sum(c) FROM t"
            " GROUP BY GROUPING SETS ((a,b),(a),(b),())")
        case["variants"].append(
            "SELECT a, b, grouping(a, b), sum(c) FROM t"
            " GROUP BY CUBE(a,b)")
        case["variants"].append(
            "SELECT a, b, 0, sum(c) FROM t GROUP BY a, b UNION ALL"
            " SELECT a, NULL::int, 1, sum(c) FROM t GROUP BY a UNION ALL"
            " SELECT NULL::int, b, 2, sum(c) FROM t GROUP BY b UNION ALL"
            " SELECT NULL::int, NULL::int, 3, sum(c) FROM t")
        case["closed"] = (
            [(a, b, 0, s) for (a, b, s) in py_group(data, [A, B], sumc)] +
            [(a, None, 1, s) for (a, s) in py_group(data, [A], sumc)] +
            [(None, b, 2, s) for (b, s) in py_group(data, [B], sumc)] +
            [(None, None, 3, s) for (s,) in py_group(data, [], sumc)])

    elif fam == "gs_distinct":
        # GROUP BY DISTINCT GROUPING SETS: dedups rows within the whole
        # grouped result as well as the set list.
        case["variants"].append(
            "SELECT DISTINCT a, b FROM t GROUP BY DISTINCT"
            " GROUPING SETS ((a),(b))")
        case["variants"].append(
            "SELECT DISTINCT a, b FROM (SELECT a, NULL AS b FROM t"
            " UNION ALL SELECT NULL, b FROM t) s")
        case["closed"] = list(set(
            [(a, None) for (a,) in py_group(data, [A], [])] +
            [(None, b) for (b,) in py_group(data, [B], [])]))

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
    if case.get("second"):
        s = case["second"]
        r = pg.run(s["variants"][0])
        r2 = pg.run(s["variants"][1])
        if r.ok and r2.ok:
            want = Counter(tuple(x) if isinstance(x, tuple) else (x,)
                           for x in s["closed"])
            got, got2 = Counter(map(tuple, r.rows)), \
                Counter(map(tuple, r2.rows))
            out["second_diff"] = (got != want or got2 != want)
        else:
            out["second_diff"] = "ERR:%s|%s" % (
                repr(r.error)[:90], repr(r2.error)[:90])
    bad = bool(diffs) or out.get("closed_diff") or \
        out.get("second_diff") not in (None, False)
    out["verdict"] = "DIFF" if bad else "ok"
    if bad:
        out["variants"] = case["variants"]
        out["setup"] = case["setup"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="pgmaster_assert")
    ap.add_argument("--cases", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    prefix = pg_build_prefix(args.build)
    outdir = Path("results/gsdiff/%s_s%d" % (args.build, args.seed))
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    pg = PostgresRunner(datadir="/tmp/gsdiff_%s_%d" % (args.build, args.seed),
                        pg_prefix=prefix)
    counts = Counter()
    t0 = time.time()
    with open(outdir / "report.jsonl", "w") as fh:
        for i in range(args.cases):
            case = gen_cases(rng, i)
            r = run_case(pg, case)
            counts["%s/%s" % (r["family"], r["verdict"])] += 1
            fh.write(json.dumps(r, default=str) + "\n")
            if i % 25 == 0:
                print("[%s] %d/%d %s (%.0fs)" % (args.build, i, args.cases,
                                               dict(counts), time.time() - t0),
                      flush=True)
    print("DONE", dict(counts))
    print("report:", outdir / "report.jsonl")


if __name__ == "__main__":
    main()
