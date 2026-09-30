#!/usr/bin/env python3
"""DQE-style DML cross-consistency oracle.

The same predicate p on the same table state must select the same row
set whether it is evaluated by a SELECT scan qual or by ModifyTable's
qual machinery:

  D1  SELECT a,b,c FROM t WHERE p
  D2  DELETE FROM t WHERE p RETURNING a,b,c          (fresh copy)
  D3  UPDATE t SET c=c+1000000 WHERE p
        RETURNING a,b,c-1000000                      (old image)
  D4  SELECT a,b,c FROM t WHERE p FOR UPDATE         (locked scan)
  D5  DELETE FROM t WHERE ctid IN
        (SELECT ctid FROM t WHERE p) RETURNING a,b,c (subplan path)

All five bags must be equal — and equal to the Python-evaluated set.
ModifyTable quals run through a different expression/scan path than
plain SELECT quals; subplan-ctid deletion exercises TidScan.
"""
import argparse, json, random, sys, time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.paths import pg_build_prefix  # noqa: E402
from pg_tlp import pred_pool  # noqa: E402


def gen_schema(rng):
    setup = ["CREATE TABLE t(a int, b int, c int)"]
    rows = [(rng.choice([0, 1, 2, 3, "NULL"]), rng.choice([0, 1, 2, "NULL"]),
             rng.choice([0, 1, 5, "NULL"])) for _ in range(rng.randint(5, 14))]
    setup += ["INSERT INTO t VALUES " +
              ",".join("(%s,%s,%s)" % r for r in rows), "ANALYZE t"]
    data = [tuple(None if v == "NULL" else v for v in r) for r in rows]
    return setup, data


def gen_cases(rng, i):
    setup, data = gen_schema(rng)
    p, pf = pred_pool(rng)
    case = {"family": "dqe", "id": i, "setup": setup, "pred": p,
            "variants": [
                "SELECT a, b, c FROM t WHERE %s" % p,
                "SELECT a, b, c FROM t WHERE %s FOR UPDATE" % p,
            ]}
    # closed: python truth over p
    case["closed"] = [tuple(r) for r in data if pf(r) is True]
    return case


def run_case(pg, case):
    try:
        pg.setup(case["setup"])
    except Exception as e:
        return {"family": case["family"], "id": case["id"],
                "verdict": "setup_error", "err": repr(e)[:300]}
    out = {"family": case["family"], "id": case["id"], "pred": case["pred"]}
    bags = []
    for q in case["variants"]:
        r = pg.run(q)
        if not r.ok:
            out["verdict"] = "query_error"
            out["err"] = repr(r.error)[:200]
            out["q"] = q
            return out
        bags.append(Counter(map(tuple, r.rows)))
    # DML arms — each on a fresh copy of the data (setup drops schema)
    p = case["pred"]
    for tag, q in [
        ("del", "DELETE FROM t WHERE %s RETURNING a, b, c" % p),
        ("upd", "UPDATE t SET c = c + 1000000 WHERE %s"
                " RETURNING a, b, c - 1000000" % p),
        ("ctid", "DELETE FROM t WHERE ctid IN"
                 " (SELECT ctid FROM t WHERE %s) RETURNING a, b, c" % p),
    ]:
        pg.setup(case["setup"])
        r = pg.run(q)
        if not r.ok:
            out["verdict"] = "query_error"
            out["err"] = "%s:%s" % (tag, repr(r.error)[:180])
            out["q"] = q
            return out
        bags.append(Counter(map(tuple, r.rows)))
    base = bags[0]
    diffs = [j for j, b in enumerate(bags[1:], 1) if b != base]
    out["pair_diffs"] = diffs
    want = Counter(tuple(r) for r in case["closed"])
    out["closed_diff"] = (base != want)
    if out["closed_diff"]:
        out["extra"] = [list(x) for x in (base - want).elements()][:5]
        out["missing"] = [list(x) for x in (want - base).elements()][:5]
    bad = bool(diffs) or out["closed_diff"]
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
    outdir = Path("results/dqe/%s_s%d" % (args.build, args.seed))
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    pg = PostgresRunner(datadir="/tmp/dqe_%s_%d" % (args.build, args.seed),
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
