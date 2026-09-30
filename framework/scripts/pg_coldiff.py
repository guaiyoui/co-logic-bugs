#!/usr/bin/env python3
"""Nondeterministic-collation differential oracle.

deterministic=false ICU collations make text equality diverge from byte
identity: 'a' = 'A' is true at level2 while the bytes differ.  Every
planner transform that crosses an equality boundary must carry the
RIGHT collation:

  C1  HAVING -> WHERE pushdown is only legal when the comparison uses
      the SAME collation as the grouping (the 18.6 fix shape)
  C2  UNION / INTERSECT / EXCEPT dedup follows the column collation;
      an =/IN comparison under a different collation must not be
      confused with it
  C3  DISTINCT s vs GROUP BY s vs count(DISTINCT s) — same equality
  C4  join keys: t1.s = t2.s under nd collation vs semi-join IN
  C5  DISTINCT ON / ORDER BY vs the grouping equality
  C6  partitionwise aggregate over same-collation partitions vs off

Python model: nd-key = s.lower() (level2 approximation — case+accent
fold), C-key = s.  Only case-only data is generated so lower() is exact
for the level2 equality we need.
"""
import argparse, json, random, sys, time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.paths import pg_build_prefix  # noqa: E402

ND = "ndt"          # nondeterministic collation name
DET = '"C"'         # deterministic collation


def gen_schema(rng):
    setup = [
        "DROP TABLE IF EXISTS t1 CASCADE", "DROP TABLE IF EXISTS t2 CASCADE",
        "DROP COLLATION IF EXISTS %s" % ND,
        "CREATE COLLATION %s (provider=icu, locale='und-u-ks-level2',"
        " deterministic=false)" % ND,
        "CREATE TABLE t1(id int, s text COLLATE %s, d text COLLATE %s)"
        % (ND, DET),
        "CREATE TABLE t2(s text COLLATE %s)" % ND,
    ]
    pool = ["a", "A", "b", "B", "aa", "AA", "Aa", "aA", "c", "C", "x", "z"]
    t1 = [(i + 1, rng.choice(pool + ["NULL"]), rng.choice(pool + ["NULL"]))
          for i in range(rng.randint(6, 14))]
    t2 = [(rng.choice(pool + ["NULL"]),) for _ in range(rng.randint(3, 9))]
    setup.append("INSERT INTO t1 VALUES " +
                 ",".join("(%d,'%s','%s')" % (i, s, d) for (i, s, d) in t1)
                 .replace("'NULL'", "NULL"))
    setup.append("INSERT INTO t2 VALUES " +
                 ",".join("('%s')" % s for (s,) in t2).replace("'NULL'", "NULL"))
    setup += ["ANALYZE t1", "ANALYZE t2"]
    data = {"t1": [(i, None if s == "NULL" else s, None if d == "NULL" else d)
                   for (i, s, d) in t1],
            "t2": [(None if s == "NULL" else s,) for (s,) in t2]}
    return setup, data


def ndkey(s):
    return None if s is None else s.lower()


def gen_cases(rng, i):
    fams = ["pushdown", "setop_dedup", "distinct_gb", "joinkey",
            "distinct_on", "in_vs_setop"]
    fam = fams[i % len(fams)]
    setup, data = gen_schema(rng)
    case = {"family": fam, "id": i, "setup": setup, "variants": [],
            "closed": None}
    lit = rng.choice(["a", "b", "aa", "c"])

    if fam == "pushdown":
        # HAVING under SAME collation as grouping — pushdown legal.
        # vs the equivalent WHERE spelling under the same collation.
        case["variants"].append(
            "SELECT s, count(*) FROM t1 GROUP BY s HAVING s = '%s'"
            " ORDER BY s" % lit)
        case["variants"].append(
            "SELECT s, count(*) FROM t1 WHERE s = '%s' GROUP BY s"
            " ORDER BY s" % lit)
        # nd-equality keeps every case-fold member in ONE group;
        # zero matches -> no group at all
        nnd = sum(1 for (_i, s, _d) in data["t1"]
                  if ndkey(s) == ndkey(lit))
        case["closed"] = [(ndkey(lit), nnd)] if nnd else []
        case["project"] = "ndkey"
        # unambiguous arm: WHERE under C filters by BYTES, then groups —
        # the pushed-comparison semantics, deterministic by construction
        case2 = dict(case)
        case2["variants"] = [
            "SELECT s, count(*) FROM t1 WHERE s = '%s' COLLATE %s"
            " GROUP BY s ORDER BY s" % (lit, DET)]
        nlit = sum(1 for (_i, s, _d) in data["t1"] if s == lit)
        case2["closed"] = [(ndkey(lit), nlit)] if nlit else []
        case["second"] = case2

    elif fam == "setop_dedup":
        # UNION dedup under nd collation == nd-grouped distinct set
        case["variants"].append(
            "SELECT s FROM t1 UNION SELECT s FROM t2 ORDER BY 1")
        case["variants"].append(
            "SELECT s FROM (SELECT s FROM t1 UNION ALL SELECT s FROM t2) u"
            " GROUP BY s ORDER BY 1")
        case["variants"].append(
            "SELECT DISTINCT s FROM (SELECT s FROM t1 UNION ALL"
            " SELECT s FROM t2) u ORDER BY 1")
        ks = set(ndkey(s) for (_i, s, _d) in data["t1"] if s is not None) | \
            set(ndkey(s[0]) for s in data["t2"] if s[0] is not None)
        hasnull = any(s is None for (_i, s, _d) in data["t1"]) or \
            any(s[0] is None for s in data["t2"])
        # the surviving REPRESENTATIVE of each nd-group is collation-
        # dependent — oracle checks the nd-key SET, not the surface text.
        case["closed"] = sorted(k for k in ks) + ([None] if hasnull else [])
        case["project"] = "ndkey"

    elif fam == "distinct_gb":
        case["variants"].append(
            "SELECT s, count(*) FROM t1 GROUP BY s ORDER BY s")
        case["variants"].append(
            "SELECT s, n FROM (SELECT s, count(*) n FROM t1 GROUP BY s) u"
            " ORDER BY s")
        groups = {}
        for (_i, s, _d) in data["t1"]:
            groups[ndkey(s)] = groups.get(ndkey(s), 0) + 1
        case["closed"] = sorted(groups.items(),
                                key=lambda kv: (kv[0] is None, kv[0]))
        case["project"] = "ndkey"
        case2 = dict(case)
        case2["variants"] = [
            "SELECT count(DISTINCT s) FROM t1",
            "SELECT count(*) FROM (SELECT DISTINCT s FROM t1"
            " WHERE s IS NOT NULL) u"]
        case2["closed"] = [(sum(1 for k in groups if k is not None),)]
        case["second"] = case2

    elif fam == "joinkey":
        # join under nd collation == semi-join IN under nd collation
        # (dedup'd by DISTINCT for multiplicity check)
        case["variants"].append(
            "SELECT DISTINCT t1.s FROM t1 JOIN t2 ON t1.s = t2.s"
            " ORDER BY 1")
        case["variants"].append(
            "SELECT DISTINCT t1.s FROM t1 WHERE t1.s IN"
            " (SELECT s FROM t2) ORDER BY 1")
        t2k = set(ndkey(s[0]) for s in data["t2"] if s[0] is not None)
        case["closed"] = sorted({ndkey(s) for (_i, s, _d) in data["t1"]
                                 if s is not None and ndkey(s) in t2k})
        case["project"] = "ndkey"

    elif fam == "distinct_on":
        # DISTINCT ON (s) picks one row per nd-group; ORDER BY s, id
        # makes the choice deterministic — count of distinct keys must
        # equal the group count.
        case["variants"].append(
            "SELECT count(*) FROM (SELECT DISTINCT ON (s) s FROM t1"
            " ORDER BY s, id) u")
        case["variants"].append(
            "SELECT count(*) FROM (SELECT s FROM t1 GROUP BY s) u")
        ng = len(set(ndkey(s) for (_i, s, _d) in data["t1"]
                     if s is not None))
        if any(s is None for (_i, s, _d) in data["t1"]):
            ng += 1
        case["closed"] = [(ng,)]

    elif fam == "in_vs_setop":
        # INTERSECT under nd collation vs nd IN (multiplicity-free)
        case["variants"].append(
            "SELECT s FROM t1 WHERE s IS NOT NULL INTERSECT"
            " SELECT s FROM t2 WHERE s IS NOT NULL ORDER BY 1")
        case["variants"].append(
            "SELECT DISTINCT s FROM t1 WHERE s IN (SELECT s FROM t2)"
            " ORDER BY 1")
        t2k = set(ndkey(s[0]) for s in data["t2"] if s[0] is not None)
        case["closed"] = sorted({ndkey(s) for (_i, s, _d) in data["t1"]
                                 if s is not None and ndkey(s) in t2k})
        case["project"] = "ndkey"

    return case


def run_case(pg, case):
    try:
        pg.setup(case["setup"])
    except Exception as e:
        return {"family": case["family"], "id": case["id"],
                "verdict": "setup_error", "err": repr(e)[:300]}
    out = {"family": case["family"], "id": case["id"]}
    proj = (lambda r: tuple(ndkey(v) if isinstance(v, str) else v
                            for v in r)) if case.get("project") else \
        (lambda r: tuple(r))
    bags = []
    err = None
    for q in case["variants"]:
        r = pg.run(q)
        if not r.ok:
            err = repr(r.error)[:200]
            bags = None
            break
        bags.append(Counter(map(proj, r.rows)))
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
        want = Counter(proj(tuple(r) if isinstance(r, tuple) else (r,))
                       for r in case["closed"])
        out["closed_diff"] = (base != want)
        if out["closed_diff"]:
            out["extra"] = [list(x) for x in (base - want).elements()][:5]
            out["missing"] = [list(x) for x in (want - base).elements()][:5]
    if case.get("second"):
        s = case["second"]
        rs = [pg.run(q) for q in s["variants"]]
        if all(r.ok for r in rs):
            want = Counter(proj(tuple(x) if isinstance(x, tuple) else (x,))
                           for x in s["closed"])
            out["second_diff"] = any(
                Counter(map(proj, r.rows)) != want for r in rs)
        else:
            out["second_diff"] = "ERR:" + repr(
                next(r.error for r in rs if not r.ok))[:120]
    bad = bool(diffs) or out.get("closed_diff") or \
        out.get("second_diff") not in (None, False)
    out["verdict"] = "DIFF" if bad else "ok"
    if bad:
        out["variants"] = case["variants"]
        out["setup"] = case["setup"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="pg186_icu")
    ap.add_argument("--cases", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    prefix = pg_build_prefix(args.build)
    outdir = Path("results/coldiff/%s_s%d" % (args.build, args.seed))
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    pg = PostgresRunner(datadir="/tmp/coldiff_%s_%d" % (args.build, args.seed),
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
