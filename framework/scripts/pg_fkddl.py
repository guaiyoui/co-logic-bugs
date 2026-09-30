#!/usr/bin/env python3
"""FK-over-partition-DDL sequence oracle — exercises the constraint-
trigger bookkeeping that ATTACH/DETACH must maintain (upstream vein:
FK triggers missing on freshly attached partitions, phantom triggers
after DETACH, validation skipped for pre-existing rows).

Case shape:
  ref(uk PK)                    -- referenced table
  pt(k int, fk REFERENCES ref)  -- partitioned child, partitions p0,p1
  px(like pt)                   -- candidate partition with KNOWN rows
                                  (some deliberately FK-violating)

Probe expectations (python model):
  ATTACH px -> must FAIL iff px holds a row whose fk is not in ref
  INSERT bad-fk into pt via attached px range -> must fail when px is
      attached, must SUCCEED after DETACH
  DELETE ref row still referenced -> must fail (RESTRICT default)
  UPDATE ref.uk -> must fail while referenced, allowed after children
      removed
Every expectation is absolute — a mismatch is a wrong-enforcement bug,
not a plan-diff.
"""
import argparse, json, random, sys, time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.paths import pg_build_prefix  # noqa: E402


def gen_cases(rng, i):
    fam = rng.choice(["fkddl", "pref", "pref_sub", "selfref"])
    if fam == "selfref":
        # pt(k PK, pk REFERENCES pt(k)) — self-FK on a partitioned
        # table; px attaches as the [80,130) partition.  All k unique.
        k0 = rng.sample(range(0, 50), rng.randint(3, 7))
        k1 = rng.sample(range(50, 80), rng.randint(2, 5))
        kx = rng.sample(range(80, 121), rng.randint(3, 7))
        ptk = k0 + k1  # keys guaranteed present in pt at insert time
        def refs(ks):
            return [(k, rng.choice([x for x in ptk if x != k]
                                   + ["NULL"])) for k in ks]
        p0rows, p1rows = refs(k0), refs(k1)
        # px rows' pk preferentially reference keys that live in pt —
        # keeps the post-detach DELETE probe armed
        pxrows = [(k, rng.choice(k0 + k1 + ["NULL"])) for k in kx]
        nviol = rng.choice([0, 0, 1, 1, 2])
        free = [k for k in range(80, 121) if k not in kx]
        for _ in range(nviol):
            pxrows.append((free.pop(), rng.randint(500, 600)))
        return {"id": i, "family": fam, "p0": p0rows, "p1": p1rows,
                "px": pxrows, "nviol": nviol,
                "refkeys": ptk + kx, "kx": kx}
    refkeys = sorted(rng.sample(range(1, 120), rng.randint(4, 10)))
    # pt partitioned on k: p0 = k<50, p1 = k>=50 (px will cover a slice)
    p0rows, p1rows, pxrows = [], [], []
    for _ in range(rng.randint(3, 8)):
        p0rows.append((rng.randint(0, 49), rng.choice(refkeys + ["NULL"])))
    for _ in range(rng.randint(2, 6)):
        p1rows.append((rng.randint(50, 79), rng.choice(refkeys + ["NULL"])))
    # px covers k in [80,120); mix of valid and violating fks
    nviol = rng.choice([0, 0, 0, 1, 1, 2])
    for _ in range(rng.randint(2, 6)):
        pxrows.append((rng.randint(80, 120), rng.choice(refkeys + ["NULL"])))
    for _ in range(nviol):
        pxrows.append((rng.randint(80, 120), rng.randint(200, 300)))
    case = {"id": i, "refkeys": refkeys, "p0": p0rows, "p1": p1rows,
            "px": pxrows, "nviol": nviol,
            "family": fam}
    return case


def fmt_rows(rows):
    return ",".join("(%s,%s)" % ("NULL" if v is None else v,
                                 "NULL" if f is None else f)
                    for (v, f) in rows)


def run_case(pg, case):
    fam = case["family"]
    out = {"family": fam, "id": case["id"], "probes": {}}
    if fam == "selfref":
        # pt references itself; px's pk values point at pt keys
        try:
            pg.setup([
                "CREATE TABLE pt(k int PRIMARY KEY,"
                " pk int REFERENCES pt(k)) PARTITION BY RANGE (k)",
                "CREATE TABLE p0 PARTITION OF pt"
                " FOR VALUES FROM (0) TO (50)",
                "CREATE TABLE p1 PARTITION OF pt"
                " FOR VALUES FROM (50) TO (80)",
                "CREATE TABLE px(k int PRIMARY KEY, pk int)",
            ])
        except Exception as e:
            return {"family": fam, "id": case["id"],
                    "verdict": "setup_error", "err": repr(e)[:300]}
        for t in ("pt", "px"):
            rows = case["p0" if t == "pt" else "px"]
            if t == "pt":
                rows = case["p0"] + case["p1"]
            if rows:
                pg.run("INSERT INTO %s VALUES %s" % (t, fmt_rows(rows)))
        fails = []

        def probe(name, sql, expect_ok):
            r = pg.run(sql)
            out["probes"][name] = "ok" if r.ok else (
                "ERR:" + repr(r.error)[:120])
            if r.ok != expect_ok:
                fails.append(name)

        probe("attach",
              "ALTER TABLE pt ATTACH PARTITION px"
              " FOR VALUES FROM (80) TO (130)",
              expect_ok=(case["nviol"] == 0))
        attached = case["nviol"] == 0
        if attached:
            probe("ins_bad_attached",
                  "INSERT INTO pt VALUES (95, 999)", expect_ok=False)
            probe("detach", "ALTER TABLE pt DETACH PARTITION px",
                  expect_ok=True)
            probe("ins_bad_detached",
                  "INSERT INTO px VALUES (96, 999)", expect_ok=False)
            # px still enforces its self-FK clone against pt: deleting/
            # updating a pt key referenced by px must fail
            vk = next((f for (_k, f) in case["px"]
                       if f not in (None, "NULL")), None)
            if vk is not None:
                probe("del_pt_postdetach",
                      "DELETE FROM pt WHERE k = %d" % vk,
                      expect_ok=False)
                probe("upd_pt_postdetach",
                      "UPDATE pt SET k = k + 500 WHERE k = %d" % vk,
                      expect_ok=False)
        out["verdict"] = "DIFF" if fails else "ok"
        out["fails"] = fails
        if fails:
            out["case"] = {k: case[k] for k in ("p0", "p1", "px")}
        return out
    ref_ins = ",".join("(%d)" % k for k in case["refkeys"])
    if fam in ("pref", "pref_sub"):
        # referenced side is itself partitioned (rp0 k<50, rp1 k>=50) —
        # the detached-FK-on-partitioned-ref bug shape
        ddl = [
            "CREATE TABLE ref(uk int PRIMARY KEY) PARTITION BY RANGE (uk)",
            "CREATE TABLE rp0 PARTITION OF ref FOR VALUES FROM (0) TO (50)",
            "CREATE TABLE rp1 PARTITION OF ref FOR VALUES FROM (50) TO (200)",
            "INSERT INTO ref VALUES %s" % ref_ins,
        ]
    else:
        ddl = [
            "CREATE TABLE ref(uk int PRIMARY KEY)",
            "INSERT INTO ref VALUES %s" % ref_ins,
        ]
    pxddl = ("CREATE TABLE px(k int, fk int) PARTITION BY RANGE (k)"
             if fam == "pref_sub" else "CREATE TABLE px(k int, fk int)")
    try:
        pg.setup(ddl + [
            "CREATE TABLE pt(k int, fk int REFERENCES ref(uk))"
            " PARTITION BY RANGE (k)",
            "CREATE TABLE p0 PARTITION OF pt FOR VALUES FROM (0) TO (50)",
            "CREATE TABLE p1 PARTITION OF pt FOR VALUES FROM (50) TO (80)",
            pxddl,
        ] + (["CREATE TABLE pxa PARTITION OF px"
              " FOR VALUES FROM (80) TO (105)",
              "CREATE TABLE pxb PARTITION OF px"
              " FOR VALUES FROM (105) TO (130)"]
             if fam == "pref_sub" else []))
    except Exception as e:
        return {"family": fam, "id": case["id"],
                "verdict": "setup_error", "err": repr(e)[:300]}
    if case["p0"]:
        pg.run("INSERT INTO pt VALUES " + fmt_rows(case["p0"]))
    if case["p1"]:
        pg.run("INSERT INTO pt VALUES " + fmt_rows(case["p1"]))
    if case["px"]:
        pg.run("INSERT INTO px VALUES " + fmt_rows(case["px"]))

    fails = []

    def probe(name, sql, expect_ok):
        r = pg.run(sql)
        ok = r.ok
        out["probes"][name] = "ok" if ok else ("ERR:" +
                                               repr(r.error)[:120])
        if ok != expect_ok:
            fails.append(name)

    # P1: ATTACH must validate px rows against the FK
    probe("attach",
          "ALTER TABLE pt ATTACH PARTITION px FOR VALUES FROM (80) TO (130)",
          expect_ok=(case["nviol"] == 0))
    attached = case["nviol"] == 0
    # P2: a violating insert landing in px's range must fail when attached
    if attached:
        probe("ins_bad_attached",
              "INSERT INTO pt VALUES (90, 999)", expect_ok=False)
    # P3: a VALID insert into px range must succeed when attached
    if attached:
        probe("ins_ok_attached",
              "INSERT INTO pt VALUES (91, %d)" % case["refkeys"][0],
              expect_ok=True)
    # P4: DELETE a still-referenced ref row must fail
    live_children = [f for (_k, f) in case["p0"] + case["p1"]
                     if f not in (None, "NULL")] + \
        ([f for (_k, f) in case["px"] if f not in (None, "NULL")]
         if attached else [])
    refed = set(live_children)
    victim = next((k for k in case["refkeys"] if k in refed),
                  case["refkeys"][0])
    probe("del_ref", "DELETE FROM ref WHERE uk = %d" % victim,
          expect_ok=(victim not in refed))
    # P5: DETACH then the same violating insert into px must STILL FAIL
    # — the detached partition retains its own copy of the FK
    if attached:
        probe("detach", "ALTER TABLE pt DETACH PARTITION px",
              expect_ok=True)
        probe("ins_bad_detached",
              "INSERT INTO px VALUES (92, 999)", expect_ok=False)
        # P5b (the half-backed-FK bug shape): px still references ref —
        # deleting/updating a ref key it references must fail, and the
        # check must fire on the right partition of a partitioned ref.
        pxrefs = [f for (_k, f) in case["px"] if f not in (None, "NULL")]
        if pxrefs:
            vk = pxrefs[0]
            probe("del_ref_postdetach",
                  "DELETE FROM ref WHERE uk = %d" % vk, expect_ok=False)
            probe("upd_ref_postdetach",
                  "UPDATE ref SET uk = uk + 500 WHERE uk = %d" % vk,
                  expect_ok=False)
    # P6: after removing all referencing children, ref delete must pass
    pg.run("DELETE FROM pt")
    if attached:
        pg.run("DELETE FROM px")
    probe("del_ref_free", "DELETE FROM ref WHERE uk = %d" % victim,
          expect_ok=True)

    out["verdict"] = "DIFF" if fails else "ok"
    out["fails"] = fails
    if fails:
        out["case"] = {k: case[k] for k in ("refkeys", "p0", "p1", "px")}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="pgmaster_assert")
    ap.add_argument("--cases", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    prefix = pg_build_prefix(args.build)
    outdir = Path("results/fkddl/%s_s%d" % (args.build, args.seed))
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    pg = PostgresRunner(datadir="/tmp/fkddl_%s_%d" % (args.build, args.seed),
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
