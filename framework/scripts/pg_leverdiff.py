#!/usr/bin/env python3
"""Lever-pair differential oracle — targets the 2025-26 upstream vein of
qual-vanishing / nullability-bookkeeping bugs (#19553, #19560, #19579,
missing-quals, #19412).

Mechanism: every upstream repro in this family is a PAIR of query surface
forms differing in exactly one semantics-preserving lever:

  L1  empty subtree spelled WHERE false  vs  LIMIT 0  vs  VALUES-with-FALSE
  L2  CTE inlined (default)              vs  MATERIALIZED
  L3  provably-empty subtree present     vs  spelled as NULL literal cols
  L4  join removable by UNIQUE           vs  same query sans the join
  L5  x = const                          vs  x IS NOT DISTINCT FROM const
        (only on provably-non-nullable inputs)
  L6  ON FALSE outer join                vs  dropping the join entirely
  L7  redundant IS NOT NULL on a NOT NULL column (must be dropped, but
      nullingrels bookkeeping must still merge — the #19412 lever)
  L8  ON TRUE                            vs  CROSS JOIN spelling

If the engine returns different results for a lever pair, exactly one of the
two forms hit a bookkeeping bug — the pair is self-localizing.  Where a
closed form exists (empty LEFT side must null-extend, empty FULL side must
emit all rows, redundant-qual must not filter real rows) we also compare
against the absolute expectation — that catches cases where BOTH arms are
wrong the same way.

Builds: pg180/186 retain the pre-fix window for several of these bugs
(recall check); pgmaster is the search target.
"""
import argparse, json, random, sys, time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.paths import pg_build_prefix  # noqa: E402


def gen_schema(rng):
    """Small join-nest schema: two keyed tables + one unique-keyed +
    one plain; a few NULLs to feed outer-join extension."""
    setup = [
        "DROP TABLE IF EXISTS t1 CASCADE", "DROP TABLE IF EXISTS t2 CASCADE",
        "DROP TABLE IF EXISTS u CASCADE", "DROP TABLE IF EXISTS v CASCADE",
        "CREATE TABLE t1(k int PRIMARY KEY, a int, b int)",
        "CREATE TABLE t2(k int, x int NOT NULL)",
        "CREATE TABLE u(uk int UNIQUE, y int)",
        "CREATE TABLE v(k int, z int)",
    ]
    t1r, t2r, ur, vr = [], [], [], []
    for i in range(1, rng.randint(5, 9)):
        t1r.append("(%d,%s,%s)" % (i, rng.choice([1, 2, 3, "NULL"]),
                                   rng.choice([0, 1, 7, "NULL"])))
    for i in range(rng.randint(3, 7)):
        t2r.append("(%s,%d)" % (rng.choice([1, 2, 3, "NULL"]),
                                rng.choice([0, 1, 2, 7])))
    for i in range(1, rng.randint(4, 7)):
        ur.append("(%d,%d)" % (i, rng.choice([0, 1, 2, 7])))
    for i in range(rng.randint(2, 6)):
        vr.append("(%s,%d)" % (rng.choice([1, 2, 3, "NULL"]),
                               rng.choice([0, 1, 7])))
    setup += ["INSERT INTO t1 VALUES " + ",".join(t1r),
              "INSERT INTO t2 VALUES " + ",".join(t2r),
              "INSERT INTO u VALUES " + ",".join(ur),
              "INSERT INTO v VALUES " + ",".join(vr),
              "ANALYZE t1", "ANALYZE t2", "ANALYZE u", "ANALYZE v"]
    data = {"t1": [eval(r, {"NULL": None}) for r in t1r],
            "t2": [eval(r, {"NULL": None}) for r in t2r],
            "u": [eval(r) for r in ur],
            "v": [eval(r, {"NULL": None}) for r in vr]}
    return setup, data


EMPTY_FORMS = [
    "(SELECT %s FROM t2 WHERE false)",
    "(SELECT %s FROM t2 LIMIT 0)",
    "(SELECT %s FROM t2 WHERE 1 = 2)",
    "(SELECT %s FROM t2 WHERE k IS NULL AND k IS NOT NULL)",
]


def const_empty(sel):
    """RTE_RESULT-shaped empty subtree: a constant SELECT with WHERE false.
    Different planner path than a scanned-empty base rel — the upstream
    2025-26 bugs mostly live here.  `sel` is the needed column name(s)."""
    if sel == "x":
        return "(SELECT NULL::int AS x WHERE false)"
    if sel == "k, x":
        return "(SELECT NULL::int AS k, NULL::int AS x WHERE false)"
    return "(SELECT %s WHERE false)" % sel


def gen_cases(rng, i):
    fams = ["res_hoist", "empty_oj", "qual_survive", "rjc", "lat_notnull",
            "full_empty", "ojr_lever", "clonequal", "cte_mat",
            "isnd_lever", "uniq_in", "sje", "grp_null", "nest_onfalse",
            "scalar_empty", "empty_union"]
    fam = fams[i % len(fams)]
    setup, data = gen_schema(rng)
    case = {"family": fam, "id": i, "setup": setup, "variants": [],
            "closed": None}
    c1, c2 = rng.randint(0, 9), rng.randint(0, 9)

    if fam == "res_hoist":
        # #19553: provably-empty subtree inside an outer-join nest under a
        # constant target — the const must NOT leak out of the null side.
        # v LEFT JOIN (SELECT c1 AS q FROM <empty> LEFT JOIN (SELECT c2)
        #              ON true) s ON true
        # closed form: every v row with q = NULL.
        for inner in [e % ("%d AS q" % c1) for e in EMPTY_FORMS] + \
                     [const_empty("%d AS q" % c1)]:
            q = ("SELECT v.k, s.q FROM v LEFT JOIN "
                 "(SELECT q FROM %s e LEFT JOIN (SELECT %d AS r) f ON true) s"
                 " ON true ORDER BY v.k" % (inner, c2))
            case["variants"].append(q)
        # L2 lever: same shape through a MATERIALIZED CTE
        case["variants"].append(
            "WITH s AS MATERIALIZED (SELECT q FROM "
            "(SELECT %d AS q FROM t2 WHERE false) e "
            "LEFT JOIN (SELECT %d AS r) f ON true) "
            "SELECT v.k, s.q FROM v LEFT JOIN s ON true ORDER BY v.k"
            % (c1, c2))
        case["closed"] = [(k, None) for (k, _z) in data["v"]]

    elif fam == "empty_oj":
        # outer LEFT JOIN to a provably-empty subtree: outer rows survive
        # with NULL-extended inner cols — regardless of how the empty side
        # is spelled, and regardless of an additional nested empty inside.
        for e in EMPTY_FORMS + [const_empty("x")]:
            inner = e % "x" if "%s" in e else e
            q = ("SELECT t1.k, s.x FROM t1 LEFT JOIN %s s ON t1.k = s.x"
                 " ORDER BY t1.k" % inner)
            case["variants"].append(q)
        # L6: same result spelled without the join at all
        case["variants"].append(
            "SELECT t1.k, NULL::int AS x FROM t1 ORDER BY t1.k")
        case["closed"] = [(k, None) for (k, _a, _b) in data["t1"]]

    elif fam == "qual_survive":
        # qual on the null-extended col:  s.x IS NOT NULL must kill the
        # extension rows (0 rows out);  s.x IS NULL must keep all.
        e = rng.choice(EMPTY_FORMS) % "x"
        case["variants"].append(
            "SELECT t1.k FROM t1 LEFT JOIN %s s ON t1.k = s.x"
            " WHERE s.x IS NOT NULL ORDER BY t1.k" % e)
        case["closed"] = []
        case2 = dict(case)
        case2["variants"] = [
            "SELECT t1.k FROM t1 LEFT JOIN %s s ON t1.k = s.x"
            " WHERE s.x IS NULL ORDER BY t1.k" % e]
        case2["closed"] = [(k,) for (k, _a, _b) in data["t1"]]
        case["second"] = case2

    elif fam == "rjc":
        # #19560 shape: removable LEFT JOIN (unique inner, no output refs)
        # beside a second join feeding a WHERE qual — the EC-derived
        # restriction on the remaining base must not be orphaned.
        # t1 LEFT JOIN u ON t1.k=u.uk   (removable: u contributes nothing)
        #     JOIN v ON t1.k=v.k
        #     WHERE v.z = c
        case["variants"].append(
            "SELECT t1.k, v.z FROM t1 LEFT JOIN u ON t1.k = u.uk"
            " JOIN v ON t1.k = v.k WHERE v.z = %d ORDER BY t1.k, v.z" % c1)
        # L4 lever: without the removable join — provably same result
        case["variants"].append(
            "SELECT t1.k, v.z FROM t1 JOIN v ON t1.k = v.k"
            " WHERE v.z = %d ORDER BY t1.k, v.z" % c1)
        # L2 lever: the joined table through an inlined vs materialized CTE
        case["variants"].append(
            "WITH w AS (SELECT * FROM v) "
            "SELECT t1.k, w.z FROM t1 LEFT JOIN u ON t1.k = u.uk"
            " JOIN w ON t1.k = w.k WHERE w.z = %d ORDER BY t1.k, w.z" % c1)
        case["closed"] = [
            (k, z) for (k, _a, _b) in data["t1"]
            for (vk, z) in data["v"] if vk == k and z == c1]

    elif fam == "lat_notnull":
        # #19412 shape generalized: LATERAL UNION ALL whose output col is
        # backed by a NOT NULL base col on one arm — a pushed-down
        # IS NOT NULL is redundant on the base col but NOT on null-
        # extended rows fed by an outer LEFT JOIN.
        # t1 LEFT JOIN t2 (so t2.x null-extends) JOIN LATERAL
        #   (SELECT t1.a AS u UNION ALL SELECT t2.x AS u) s
        #   ON s.u IS NOT NULL
        case["variants"].append(
            "SELECT t1.k, s.u FROM t1 LEFT JOIN t2 ON t1.k = t2.k"
            " JOIN LATERAL (SELECT t1.a AS u UNION ALL SELECT t2.x AS u) s"
            " ON s.u IS NOT NULL ORDER BY t1.k, s.u")
        # L7 lever: make the redundancy explicit differently — wrap the
        # arm column in a non-foldable expr; bookkeeping must still merge
        case["variants"].append(
            "SELECT t1.k, s.u FROM t1 LEFT JOIN t2 ON t1.k = t2.k"
            " JOIN LATERAL (SELECT t1.a AS u UNION ALL"
            "              SELECT t2.x + 0 AS u) s"
            " ON s.u IS NOT NULL ORDER BY t1.k, s.u")
        # closed form: the LEFT JOIN emits one joined row per (t1, t2)
        # pair (m = #matches, or 1 null-extended row when none); the
        # LATERAL then emits t1.a and that row's t2.x PER JOINED ROW —
        # so t1.a appears m times, not once.
        exp = []
        bmap = {}
        for (bk, bx) in data["t2"]:
            bmap.setdefault(bk, []).append(bx)
        for (k, a, _b) in data["t1"]:
            ms = bmap.get(k)
            if ms:
                for bx in ms:
                    if a is not None:
                        exp.append((k, a))
                    if bx is not None:
                        exp.append((k, bx))
            elif a is not None:
                exp.append((k, a))
        case["closed"] = exp

    elif fam == "full_empty":
        # #19579: FULL JOIN with a provably-empty side + outer WHERE —
        # the filter must not be dropped.
        e = rng.choice(EMPTY_FORMS + [const_empty("k, x")])
        e = e % ("k, x") if "%s" in e else e
        case["variants"].append(
            "SELECT coalesce(t1.k, s.k) kk FROM t1 FULL JOIN %s s"
            " ON t1.k = s.k WHERE coalesce(t1.k, s.k) <> %d ORDER BY 1"
            % (e, c1))
        case["variants"].append(
            "SELECT coalesce(t1.k, s.k) kk FROM t1 FULL JOIN %s s"
            " ON true WHERE coalesce(t1.k, s.k) <> %d ORDER BY 1" % (e, c1))
        case["closed"] = [(k,) for (k, _a, _b) in data["t1"] if k != c1]

    elif fam == "ojr_lever":
        # outer-join reduction levers: LEFT JOIN whose inner side is
        # provably empty collapses to plain scan — spelling must not matter
        e = rng.choice(EMPTY_FORMS + [const_empty("k, x")])
        e = e % ("k, x") if "%s" in e else e
        case["variants"].append(
            "SELECT t1.k, s.x FROM t1 LEFT JOIN %s s ON t1.a = s.x"
            " ORDER BY t1.k" % e)
        case["variants"].append(
            "SELECT t1.k, NULL::int AS x FROM t1 ORDER BY t1.k")
        # nested: an INNER join to a provably-empty subtree inside the
        # inner side — w is then empty and the outer LEFT JOIN must emit
        # the same null extension
        case["variants"].append(
            "SELECT t1.k, w.x FROM t1 LEFT JOIN"
            " (SELECT t2.k, t2.x FROM t2 JOIN %s e ON true) w"
            " ON t1.a = w.x ORDER BY t1.k" % e)
        # L6: LEFT JOIN ... ON false vs the no-join spelling — the join
        # can never extend-or-match, regardless of what's inside s
        case["variants"].append(
            "SELECT t1.k, s.x FROM t1 LEFT JOIN t2 s ON false"
            " ORDER BY t1.k")
        case["closed"] = [(k, None) for (k, _a, _b) in data["t1"]]

    elif fam == "clonequal":
        # LEFT vs FULL JOIN to a provably-empty subtree nested under an
        # outer LEFT JOIN — provably-equal spellings exercising clone-qual
        # bookkeeping across the empty boundary.
        for inner in (
            "SELECT t2.x FROM t2 LEFT JOIN"
            " (SELECT 1 AS fx WHERE false) f ON false",
            "SELECT t2.x FROM t2 FULL JOIN"
            " (SELECT 1 AS fx WHERE false) f ON false",
        ):
            case["variants"].append(
                "SELECT t1.k, j.x FROM t1 LEFT JOIN (%s) j ON t1.b = j.x"
                " ORDER BY t1.k, j.x" % inner)
        exp = []
        for (k, _a, b) in data["t1"]:
            m = [x for (_tk, x) in data["t2"] if b is not None and x == b]
            exp += [(k, x) for x in m] or [(k, None)]
        case["closed"] = exp

    elif fam == "cte_mat":
        # L2: CTE inlined (pulled up into the jointree — quals can be
        # cloned/pushed across the boundary) vs MATERIALIZED (fence).
        # The pull-up must never change the result.
        for mat in ("", "MATERIALIZED", "NOT MATERIALIZED"):
            case["variants"].append(
                "WITH w AS %s (SELECT t2.k, t2.x FROM t2"
                " WHERE t2.x <> %d) "
                "SELECT t1.k, w.x FROM t1 LEFT JOIN w ON t1.b = w.x"
                " ORDER BY t1.k, w.x" % (mat, c1))
        exp = []
        for (k, _a, b) in data["t1"]:
            m = [x for (_tk, x) in data["t2"]
                 if x != c1 and b is not None and x == b]
            exp += [(k, x) for x in m] or [(k, None)]
        case["closed"] = exp

    elif fam == "isnd_lever":
        # L5: on the declared-NOT-NULL t2.x,  x = c  and  x IS NOT
        # DISTINCT FROM c  are provably identical — but they feed
        # different EC bookkeeping (IS NOT DISTINCT FROM builds its own
        # equality image).  Both in WHERE-qual and join-cond position.
        case["variants"].append(
            "SELECT t1.k, s.x FROM t1 LEFT JOIN t2 s ON t1.a = s.x"
            " WHERE s.x = %d ORDER BY t1.k, s.x" % c1)
        case["variants"].append(
            "SELECT t1.k, s.x FROM t1 LEFT JOIN t2 s ON t1.a = s.x"
            " WHERE s.x IS NOT DISTINCT FROM %d ORDER BY t1.k, s.x" % c1)
        case["variants"].append(
            "SELECT t1.k, s.x FROM t1 JOIN t2 s"
            " ON t1.a = s.x AND s.x = %d ORDER BY t1.k, s.x" % c1)
        case["variants"].append(
            "SELECT t1.k, s.x FROM t1 JOIN t2 s"
            " ON t1.a = s.x AND s.x IS NOT DISTINCT FROM %d"
            " ORDER BY t1.k, s.x" % c1)
        case["closed"] = [
            (k, x) for (k, a, _b) in data["t1"]
            for (_tk, x) in data["t2"]
            if a is not None and x == a and x == c1]

    elif fam == "uniq_in":
        # L4: u.uk is declared UNIQUE, so t1 JOIN u ON k=uk cannot
        # multiply t1 rows — join, semi-join and comma+DISTINCT must all
        # agree.  The planner can only exploit the proof via the index;
        # the results must coincide either way.
        case["variants"].append(
            "SELECT t1.k FROM t1 JOIN u ON t1.k = u.uk ORDER BY t1.k")
        case["variants"].append(
            "SELECT t1.k FROM t1 WHERE t1.k IN (SELECT uk FROM u)"
            " ORDER BY t1.k")
        case["variants"].append(
            "SELECT DISTINCT t1.k FROM t1, u WHERE t1.k = u.uk"
            " ORDER BY t1.k")
        uks = {uk for (uk, _y) in data["u"]}
        case["closed"] = [(k,) for (k, _a, _b) in data["t1"] if k in uks]

    elif fam == "sje":
        # self-join elimination: t1 JOIN t1 ON primary key is the
        # identity — whether or not the planner proves it removable.
        case["variants"].append(
            "SELECT t1.k, t1.a, t2b.b FROM t1 JOIN t1 t2b"
            " ON t1.k = t2b.k ORDER BY t1.k")
        case["variants"].append(
            "SELECT t1.k, t1.a, t1.b FROM t1 ORDER BY t1.k")
        # and under a left-join roof — the redundant self-join must not
        # perturb row multiplicity when an empty outer join sits beside it
        case["variants"].append(
            "SELECT t1.k, t1.a, t2b.b FROM t1 JOIN t1 t2b ON t1.k = t2b.k"
            " LEFT JOIN %s s ON t1.a = s.x ORDER BY t1.k"
            % (rng.choice(EMPTY_FORMS) % ("k, x")))
        case["closed"] = [(k, a, b) for (k, a, b) in data["t1"]]

    elif fam == "grp_null":
        # GROUP BY over a null-extended outer join: the NULL group must
        # carry the full multiplicity of the outer rel.
        e = rng.choice(EMPTY_FORMS + [const_empty("k, x")])
        e = e % ("k, x") if "%s" in e else e
        case["variants"].append(
            "SELECT s.x, count(*) FROM t1 LEFT JOIN %s s ON t1.a = s.x"
            " GROUP BY s.x ORDER BY s.x" % e)
        case["variants"].append(
            "SELECT NULL::int AS x, count(*) FROM t1"
            " GROUP BY 1 ORDER BY 1")
        case["closed"] = [(None, len(data["t1"]))]

    elif fam == "nest_onfalse":
        # missing-quals shape: an INNER join made provably-empty by
        # ON false, nested as the inner side of a LEFT JOIN — the whole
        # j side must null-extend, and outer quals must still see it.
        case["variants"].append(
            "SELECT t1.k, j.x, j.z FROM t1 LEFT JOIN"
            " (t2 JOIN v ON false) j ON t1.a = j.x ORDER BY t1.k")
        case["variants"].append(
            "SELECT t1.k, NULL::int, NULL::int FROM t1 ORDER BY t1.k")
        # lever: ON false -> ON true turns the inner side into a real
        # cross join — a two-arm case with a different closed form
        case2 = dict(case)
        case2["variants"] = [
            "SELECT t1.k, j.x, j.z FROM t1 LEFT JOIN"
            " (t2 JOIN v ON true) j ON t1.a = j.x ORDER BY t1.k, j.x, j.z",
            "SELECT t1.k, j.x, j.z FROM t1 LEFT JOIN"
            " (t2 CROSS JOIN v) j ON t1.a = j.x ORDER BY t1.k, j.x, j.z"]
        exp = []
        for (k, a, _b) in data["t1"]:
            m = [(x, z) for (_tk, x) in data["t2"] for (_vk, z) in data["v"]
                 if a is not None and x == a]
            exp += [(k, x, z) for (x, z) in m] or \
                   [(k, None, None)]
        case2["closed"] = exp
        case["closed"] = [(k, None, None) for (k, _a, _b) in data["t1"]]
        case["second"] = case2

    elif fam == "scalar_empty":
        # empty subtree in the SELECT list: scalar subquery over a
        # provably-empty set must yield NULL per row — including when
        # the outer rel is itself produced by an outer join.
        for e in EMPTY_FORMS:
            inner = e % "x"
            case["variants"].append(
                "SELECT t1.k, (SELECT x FROM %s s) FROM t1"
                " ORDER BY t1.k" % inner)
        case["variants"].append(
            "SELECT t1.k, NULL::int FROM t1 ORDER BY t1.k")
        # and the same scalar under a null-extended row source (empty
        # join side -> every t1 row survives with sx NULL)
        case["variants"].append(
            "SELECT j.k2, (SELECT x FROM t2 WHERE false) FROM"
            " (SELECT t1.k AS k2, s.x AS sx FROM t1 LEFT JOIN"
            "  (SELECT x FROM t2 WHERE false) s ON t1.a = s.x) j"
            " ORDER BY j.k2")
        case["closed"] = [(k, None) for (k, _a, _b) in data["t1"]]

    elif fam == "empty_union":
        # an empty UNION ALL arm inside the join inner side: the Append
        # empty-child bookkeeping must not eat the live arm nor the
        # outer-join extension.
        for e in EMPTY_FORMS + [const_empty("x")]:
            inner = e % "x" if "%s" in e else e
            case["variants"].append(
                "SELECT t1.k, w.x FROM t1 LEFT JOIN"
                " (SELECT t2.x FROM t2 UNION ALL SELECT x FROM %s) w"
                " ON t1.a = w.x ORDER BY t1.k, w.x" % inner)
        exp = []
        for (k, a, _b) in data["t1"]:
            m = [x for (_tk, x) in data["t2"] if a is not None and x == a]
            exp += [(k, x) for x in m] or [(k, None)]
        case["closed"] = exp

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
    # second half for two-arm families (qual_survive)
    if case.get("second"):
        s = case["second"]
        r = pg.run(s["variants"][0])
        if r.ok:
            want = Counter(tuple(x) if isinstance(x, tuple) else (x,)
                           for x in s["closed"])
            got = Counter(map(tuple, r.rows))
            out["second_diff"] = (got != want)
        else:
            out["second_diff"] = "ERR:" + repr(r.error)[:150]
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
    outdir = Path("results/leverdiff/%s_s%d" % (args.build, args.seed))
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    pg = PostgresRunner(datadir="/tmp/leverdiff_%s_%d" % (args.build, args.seed),
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
