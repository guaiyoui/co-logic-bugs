#!/usr/bin/env python3
"""Executor-driver invariance oracle.

Belief auditing checked the *planner's* assumptions; this oracle checks the
*executor's* state machines: the same query, driven through different
execution patterns, must return the same result.

Drivers (all must agree with direct execution):
  direct    - plain run
  spill     - work_mem=64kB (disk paths: hash batches, sort tapes)
  parallel  - forced parallel gather (partial-agg combine paths)
  cur1      - portal cursor, FETCH 1 at a time (suspend/resume per row)
  scroll    - SCROLL cursor with randomized FETCH/MOVE positioning
              (tuplestore re-read; every fetched row checked positionally)
  prep      - PREPARE/EXECUTE with repeated + generic plans (plan cache)
  forupd    - same query under FOR UPDATE in a txn (locking eval path)

Query shapes are chosen to stress rescan-prone machinery: LATERAL UNION ALL
(async-append rescan), correlated subplans, recursive CTE worktables,
SRF/ProjectSet, hash/sort spilling, multi-use CTEs, partitioned append
under nested loop, memoize shared-param family.
"""
import argparse, json, random, re, sys, time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.paths import pg_build_prefix  # noqa: E402

INTS = [0, 1, 2, 3, 5, 7, 10, -1, -3, None]


def uniq_order(cols):
    return "ORDER BY " + ", ".join(cols)


# ----------------------------------------------------------------- schema
def gen_schema(rng, n):
    """a: outer table with dup keys (param repetition); b: inner; p: partitioned."""
    setup = [
        "DROP TABLE IF EXISTS a CASCADE", "DROP TABLE IF EXISTS b CASCADE",
        "DROP TABLE IF EXISTS p CASCADE", "DROP TABLE IF EXISTS w CASCADE",
        "CREATE TABLE a(k int, ten int, twenty int, hundred int, v int)",
        "CREATE TABLE b(k int, x int)",
        ("CREATE TABLE p(x int, y int) PARTITION BY RANGE (x);"
         " CREATE TABLE p0 PARTITION OF p FOR VALUES FROM (0) TO (100);"
         " CREATE TABLE p1 PARTITION OF p FOR VALUES FROM (100) TO (200);"
         " CREATE TABLE p2 PARTITION OF p FOR VALUES FROM (200) TO (300);"),
    ]
    arows, brows, prows = [], [], []
    for i in range(n):
        arows.append("(%d,%d,%d,%d,%d)" % (
            rng.randint(0, 15), 10 * rng.randint(0, 3), 20 * rng.randint(0, 2),
            100 * rng.randint(0, 2), rng.randint(-5, 40)))
        if rng.random() < 0.8:
            brows.append("(%d,%d)" % (rng.randint(0, 15), rng.randint(-5, 40)))
        if rng.random() < 0.7:
            prows.append("(%d,%d)" % (rng.randint(0, 299), rng.randint(0, 9)))
    if arows:
        setup.append("INSERT INTO a VALUES " + ",".join(arows))
    if brows:
        setup.append("INSERT INTO b VALUES " + ",".join(brows))
    if prows:
        setup.append("INSERT INTO p VALUES " + ",".join(prows))
    setup.append("ANALYZE a"); setup.append("ANALYZE b"); setup.append("ANALYZE p")
    data = {"a": [tuple(int(t.strip()) for t in r[1:-1].split(","))
                  for r in arows],
            "b": [tuple(int(t.strip()) for t in r[1:-1].split(","))
                  for r in brows],
            "p": [tuple(int(t.strip()) for t in r[1:-1].split(","))
                  for r in prows]}
    return setup, data


# ----------------------------------------------------------------- shapes
def gen_cases(rng, i, n):
    fams = ["latunion", "corrsub", "reccte", "srf", "aggspill",
            "sortspill", "win", "nestloop", "multicte",
            "setop", "memo", "partjoin", "latpart", "fdwlat",
            "incsort", "bitmapor", "jsontbl", "epq", "merge",
            "dmlcte"]
    fam = fams[i % len(fams)]
    c = rng.randint(0, 15)
    setup, data = gen_schema(rng, n)
    case = {"family": fam, "id": i, "setup": setup, "teardown": []}

    if fam == "latunion":
        # inner Append rescanned once per outer row; dup a.k => same-param rescans
        c2 = rng.randint(0, 30)
        case["query"] = (
            "SELECT a.k, t.x, t.tag FROM a, LATERAL ("
            "SELECT b.x, 'L' AS tag FROM b WHERE b.k = a.k AND b.x > %d "
            "UNION ALL "
            "SELECT a.v + b2.x, 'R' FROM b b2 WHERE b2.k = a.k AND b2.x < %d"
            ") t ORDER BY a.k, a.ctid, t.tag, t.x" % (c, c2))
        case["ordered"] = True

    elif fam == "corrsub":
        case["query"] = (
            "SELECT a.k, (SELECT count(*) FROM b WHERE b.k = a.k AND b.x > %d),"
            " (SELECT sum(b.x) FROM b WHERE b.k = a.k) FROM a"
            " ORDER BY a.k, a.ctid" % c)
        case["ordered"] = True

    elif fam == "reccte":
        lim = rng.randint(50, 400)
        kind = rng.choice(["i", "sumj", "mod"])
        if kind == "i":
            q = ("WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM r"
                 " WHERE i < %d) SELECT i FROM r ORDER BY i" % lim)
        elif kind == "sumj":
            q = ("WITH RECURSIVE r(i,j) AS (SELECT 1,1 UNION ALL SELECT i+1,"
                 " j + (i %% 7) FROM r WHERE i < %d) SELECT i, j FROM r"
                 " ORDER BY i" % lim)
        else:
            q = ("WITH RECURSIVE r(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM r"
                 " WHERE i < %d) SELECT i %% %d g, count(*) FROM r"
                 " GROUP BY 1 ORDER BY 1" % (lim, rng.randint(3, 17)))
        case["query"] = q
        case["ordered"] = True

    elif fam == "srf":
        case["query"] = (
            "SELECT a.k, g FROM a, generate_series(1, least(abs(a.v), 6)) g"
            " ORDER BY a.k, a.ctid, g")
        case["ordered"] = True

    elif fam == "aggspill":
        # many groups + DISTINCT agg -> HashAgg spill under low work_mem
        case["setup"] += [
            "INSERT INTO a SELECT g, g%%10*10, g%%20*20, g%%100*100,"
            " g%%37 FROM generate_series(%d,%d) g" % (n, n + 2000)]
        case["query"] = (
            "SELECT (a.k * 7 + a.v) %% %d g, count(*) AS c1, sum(a.v) AS s1,"
            " count(DISTINCT a.ten) AS c2 FROM a GROUP BY 1 ORDER BY 1"
            % rng.randint(50, 400))
        case["ordered"] = True

    elif fam == "sortspill":
        case["setup"] += [
            "INSERT INTO a SELECT g, g%%10*10, g%%20*20, g%%100*100,"
            " g%%37 FROM generate_series(%d,%d) g" % (n, n + 4000)]
        case["query"] = (
            "SELECT a.k, md5(a.v::text || a.ten::text) m FROM a"
            " ORDER BY m, a.k, a.ctid")
        case["ordered"] = True

    elif fam == "win":
        # window ORDER BY must be total (a.ctid tiebreak) or row_number's
        # assignment to same-v rows is legitimately nondeterministic
        part = rng.choice(["a.k % 3", "a.ten", "a.hundred"])
        case["query"] = (
            "SELECT a.k, row_number() OVER (PARTITION BY %s"
            " ORDER BY a.v, a.ctid),"
            " sum(a.v) OVER (PARTITION BY %s),"
            " count(*) OVER (PARTITION BY %s ORDER BY a.v, a.ctid"
            " ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"
            " FROM a ORDER BY a.k, a.ctid" % (part, part, part))
        case["ordered"] = True

    elif fam == "nestloop":
        # join over dup keys; planner may pick nestloop under spill/parallel too
        case["query"] = (
            "SELECT a.k, b.x FROM a JOIN b ON a.k = b.k WHERE b.x > %d"
            " ORDER BY a.k, b.x, a.ctid, b.ctid" % rng.randint(-5, 30))
        case["ordered"] = True

    elif fam == "multicte":
        case["query"] = (
            "WITH w AS MATERIALIZED (SELECT k, sum(x) sx, count(*) cx FROM b"
            " GROUP BY k) SELECT w1.k, w1.sx, w2.cx FROM w w1 JOIN w w2"
            " ON w1.k = w2.k WHERE w1.sx > %d ORDER BY w1.k" % rng.randint(0, 60))
        case["ordered"] = True

    elif fam == "setop":
        case["query"] = (
            "SELECT t.k, count(*) FROM (SELECT k FROM a WHERE v > %d"
            " UNION ALL SELECT k FROM b WHERE x < %d) t"
            " GROUP BY t.k ORDER BY t.k" % (rng.randint(0, 30), rng.randint(0, 30)))
        case["ordered"] = True

    elif fam == "memo":
        # known-live shared-param family: reuse the verified generator from
        # pg_belief_gen (tenk1 shape, two outer Params off one column).
        import pg_belief_gen as bg
        mc = bg.gen_memoize(rng)
        case["setup"] = mc["setup"]
        case["teardown"] = mc["teardown"]
        case["query"] = mc["q"]
        case["ordered"] = False
        case["expected"] = mc["expected"]
        case["meta"] = dict(mc["meta"], shared_param=mc["violated"])

    elif fam == "partjoin":
        case["query"] = (
            "SELECT p.y, count(*) FROM p JOIN (SELECT %d AS k) q"
            " ON p.x = q.k GROUP BY p.y ORDER BY p.y" % rng.randint(0, 299))
        case["ordered"] = True

    elif fam == "latpart":
        # partitioned Append as LATERAL inner: rescanned once per outer row
        # (async-append rescan territory)
        case["query"] = (
            "SELECT a.k, t.x, t.y FROM a, LATERAL ("
            "SELECT x, y FROM p WHERE p.x %% %d = a.k %% %d) t"
            " ORDER BY a.k, a.ctid, t.x, t.y"
            % (rng.choice([20, 37, 50]), rng.choice([3, 5, 7, 20])))
        case["ordered"] = True

    elif fam == "fdwlat":
        # postgres_fdw loopback UNION ALL as LATERAL inner: the Append over
        # async-capable ForeignScans is rescanned once per outer row —
        # exactly the async-append rescan machinery (the 18.6 bug class).
        case["needs_fdw"] = True
        if rng.random() < 0.5:
            # force the async Append to rescan for every outer row instead
            # of being absorbed by Memoize
            case["setup"] += ["SET enable_memoize = off"]
            case["teardown"] = ["RESET enable_memoize"]
        case["query"] = (
            "SELECT a.k, t.x, t.tag FROM a, LATERAL ("
            "SELECT x, 'L' AS tag FROM f1 WHERE f1.k = a.k "
            "UNION ALL "
            "SELECT x, 'R' FROM f2 WHERE f2.k = a.k) t"
            " ORDER BY a.k, a.ctid, t.tag, t.x")
        case["ordered"] = True

    elif fam == "incsort":
        # index gives inner presorted-by-k; incremental sort adds x per
        # outer rescan — presorted-prefix state is the rescan surface
        case["setup"] += [
            "CREATE INDEX b_k ON b(k)", "CREATE INDEX b_kx ON b(k, x)"]
        case["query"] = (
            "SELECT a.k, t.x FROM a, LATERAL ("
            "SELECT x FROM b WHERE b.k = a.k ORDER BY b.k, b.x"
            " LIMIT 8) t ORDER BY a.k, a.ctid, t.x")
        case["ordered"] = True

    elif fam == "bitmapor":
        # BitmapOr over two indexes rescanned per outer row
        case["setup"] += [
            "CREATE INDEX b_k ON b(k)", "CREATE INDEX b_x ON b(x)"]
        case["query"] = (
            "SELECT a.k, t.x FROM a, LATERAL ("
            "SELECT x FROM b WHERE b.k = a.k AND (b.x < %d OR b.x %% 7 = 0)"
            " ) t ORDER BY a.k, a.ctid, t.x" % rng.randint(0, 30))
        case["ordered"] = True

    elif fam == "jsontbl":
        # JSON_TABLE (17+) executor rescan under LATERAL
        case["setup"] += [
            "ALTER TABLE a ADD COLUMN jd jsonb",
            ("UPDATE a SET jd = jsonb_build_object('items',"
             " jsonb_build_array(jsonb_build_object('v', k),"
             " jsonb_build_object('v', v)))") ]
        case["query"] = (
            "SELECT a.k, jt.v FROM a, LATERAL JSON_TABLE(a.jd,"
            " '$.items[*]' COLUMNS (v int PATH '$.v')) jt"
            " ORDER BY a.k, a.ctid, jt.v")
        case["ordered"] = True

    elif fam == "merge":
        # MERGE (15+ executor modify path) vs closed-form model of the
        # final table state. Source is a deduped aggregate of b so the
        # ON-clause join is 1:1 per key.
        mode = rng.choice(["upd_ins", "upd_del", "cond"])
        akeys = {}
        for (k, t, w, h, v) in data["a"]:
            akeys.setdefault(k, []).append((t, w, h, v))
        bsum = {}
        for (k, x) in data["b"]:
            bsum[k] = bsum.get(k, 0) + x
        if mode == "upd_ins":
            case["query"] = (
                "MERGE INTO a USING (SELECT k, sum(x) sx FROM b"
                " GROUP BY k) s ON a.k = s.k WHEN MATCHED THEN"
                " UPDATE SET v = a.v + s.sx WHEN NOT MATCHED THEN"
                " INSERT VALUES (s.k, 0,0,0, s.sx)")
            exp = Counter()
            for k, rows in akeys.items():
                for (t, w, h, v) in rows:
                    nv = v + bsum[k] if k in bsum else v
                    exp[(k, t, w, h, nv)] += 1
            for k, sx in bsum.items():
                if k not in akeys:
                    exp[(k, 0, 0, 0, sx)] += 1
        elif mode == "upd_del":
            # MATCHED rows split by a WHEN condition on the source value
            case["query"] = (
                "MERGE INTO a USING (SELECT k, sum(x) sx FROM b"
                " GROUP BY k) s ON a.k = s.k WHEN MATCHED AND s.sx > 0"
                " THEN UPDATE SET v = a.v + s.sx WHEN MATCHED THEN DELETE"
                " WHEN NOT MATCHED THEN INSERT VALUES (s.k, 0,0,0, s.sx)")
            exp = Counter()
            for k, rows in akeys.items():
                for (t, w, h, v) in rows:
                    if k in bsum:
                        if bsum[k] > 0:
                            exp[(k, t, w, h, v + bsum[k])] += 1
                        # else deleted: contributes nothing
                    else:
                        exp[(k, t, w, h, v)] += 1
            for k, sx in bsum.items():
                if k not in akeys:
                    exp[(k, 0, 0, 0, sx)] += 1
        else:  # cond: NOT MATCHED BY SOURCE delete — a-only keys removed
            case["query"] = (
                "MERGE INTO a USING (SELECT k, sum(x) sx FROM b"
                " GROUP BY k) s ON a.k = s.k WHEN MATCHED THEN"
                " UPDATE SET v = a.v + s.sx WHEN NOT MATCHED BY SOURCE"
                " THEN DELETE")
            exp = Counter()
            for k, rows in akeys.items():
                if k in bsum:
                    for (t, w, h, v) in rows:
                        exp[(k, t, w, h, v + bsum[k])] += 1
            # NOT MATCHED BY SOURCE a-rows are deleted; no inserts
        case["expected_bag"] = exp
        case["check_sql"] = ("SELECT k, ten, twenty, hundred, v FROM a")

    elif fam == "dmlcte":
        # ModifyTable -> RETURNING through a CTE worktable: the emitted
        # projection must equal the closed-form post-image. Side effect
        # is checked too (final table state) — absolute oracle.
        c2 = rng.randint(0, 15)
        kind = rng.choice(["upd_ret", "del_ret", "ins_ret", "upd_ord"])
        afinal = Counter((k, t, w, h, v) for (k, t, w, h, v) in data["a"])
        if kind == "upd_ret":
            case["query"] = (
                "WITH d AS (UPDATE a SET v = v + 1 WHERE k >= %d"
                " RETURNING k, v) SELECT * FROM d ORDER BY k, v" % c2)
            exp = sorted((k, v + 1) for (k, _t, _w, _h, v) in data["a"]
                         if k >= c2)
            afinal = Counter()
            for (k, t, w, h, v) in data["a"]:
                afinal[(k, t, w, h, v + 1 if k >= c2 else v)] += 1
        elif kind == "del_ret":
            case["query"] = (
                "WITH d AS (DELETE FROM a WHERE k >= %d RETURNING k, v)"
                " SELECT * FROM d ORDER BY k, v" % c2)
            exp = sorted((k, v) for (k, _t, _w, _h, v) in data["a"]
                         if k >= c2)
            afinal = Counter((k, t, w, h, v) for (k, t, w, h, v) in data["a"]
                             if k < c2)
        elif kind == "ins_ret":
            case["query"] = (
                "WITH d AS (INSERT INTO a SELECT 99, 0,0,0, b.x FROM b"
                " WHERE b.k = %d RETURNING k, v) SELECT * FROM d"
                " ORDER BY v" % c2)
            exp = sorted((99, x) for (k, x) in data["b"] if k == c2)
            for (k, x) in data["b"]:
                if k == c2:
                    afinal[(99, 0, 0, 0, x)] += 1
        else:  # upd_ord: RETURNING of an expression over updated row
            case["query"] = (
                "WITH d AS (UPDATE a SET v = v * 2 WHERE k %% 4 = %d"
                " RETURNING k, v, v + 1 AS v1) SELECT * FROM d"
                " ORDER BY k, v" % (c2 % 4))
            exp = sorted((k, v * 2, v * 2 + 1)
                         for (k, _t, _w, _h, v) in data["a"]
                         if k % 4 == c2 % 4)
            afinal = Counter()
            for (k, t, w, h, v) in data["a"]:
                afinal[(k, t, w, h, v * 2 if k % 4 == c2 % 4 else v)] += 1
        case["expected_rows"] = exp
        case["expected_bag"] = afinal
        case["check_sql"] = ("SELECT k, ten, twenty, hundred, v FROM a")
        case["ordered"] = True

    elif fam == "epq":
        # EvalPlanQual: a FOR UPDATE cursor fetches m rows, a second
        # connection updates only rows the cursor has NOT reached yet
        # (k >= cut where cut is a strict k-boundary), then the cursor
        # resumes — LockRows must re-qualify/re-join each remaining row
        # against the NEW tuple version.
        op = rng.choice(["plus100", "negdrop", "del", "rekey", "rekey2",
                         "skip", "bmod", "bkey", "chain",
                         "pmove", "pdel", "ownupd", "subplan"])
        bside = op in ("bmod", "bkey")
        join = op in ("rekey", "rekey2", "bmod", "bkey")
        part = op in ("pmove", "pdel")
        where = " WHERE a.v > 0" if op == "negdrop" else ""
        if op == "subplan":
            # EPQ must re-evaluate the correlated subplan under the NEW
            # outer value — parameter staleness territory
            q = ("SELECT a.k, a.v, (SELECT count(*) FROM b"
                 " WHERE b.k = a.k) FROM a ORDER BY a.k, a.ctid")
        elif part:
            # partitioned table: cross-partition moves create ctid chains
            # that CROSS relations — historically fragile EPQ territory
            q = "SELECT p.x, p.y FROM p ORDER BY p.x"
        elif bside:
            # inner join so BOTH sides are lockable rowmarks
            q = ("SELECT a.k, a.v, b.x FROM a JOIN b ON a.k = b.k%s"
                 " ORDER BY a.k, a.ctid, b.ctid" % where)
        elif join:
            q = ("SELECT a.k, a.v, b.x FROM a LEFT JOIN b ON a.k = b.k%s"
                 " ORDER BY a.k, a.ctid, b.ctid" % where)
        else:
            q = ("SELECT a.k, a.v FROM a%s ORDER BY a.k, a.ctid" % where)
        case["query"] = q
        case["lockq"] = q + (" FOR UPDATE OF a SKIP LOCKED" if op == "skip"
                             else " FOR UPDATE OF a, b" if bside
                             else " FOR UPDATE OF p" if part
                             else " FOR UPDATE OF a")
        if part:
            # ORDER BY x is not total across partitions -> bag oracle.
            # cut is computed in the driver as max(fetched x)+1.
            case["epq"] = {"op": op, "m": rng.randint(2, 5)}
            case["ordered"] = False
            return case
        # simulate the base sequence offline (insertion order == ctid order)
        brows = data["b"]
        seq = []                    # (k, v, x, a_index)
        for ai, (k, _t, _w, _h, v) in enumerate(data["a"]):
            if op == "negdrop" and not (v > 0):
                continue
            if join:
                ms = [j for j in range(len(brows)) if brows[j][0] == k]
                if ms:
                    for j in ms:
                        seq.append((k, v, brows[j][1], ai))
                elif not bside:
                    seq.append((k, v, None, ai))
            elif op == "subplan":
                cnt = sum(1 for j in range(len(brows)) if brows[j][0] == k)
                seq.append((k, v, cnt, ai))
            else:
                seq.append((k, v, None, ai))
        seq.sort(key=lambda r: (r[0], r[-1]))
        # pick a fetch boundary where k strictly increases
        bounds = [i for i in range(2, len(seq) - 1)
                  if seq[i - 1][0] < seq[i][0]]
        if not bounds:
            return case
        m = rng.choice(bounds)
        cut = seq[m][0]
        # EPQ model: each stored output slot re-evaluates (a_new, b_orig)
        # — the same b tuple from the original row is re-fetched by TID and
        # the join qual is re-tested with the NEW a values. A failed join
        # yields a null-extended row; slot multiplicity is preserved.
        if op == "plus100":
            upd = "UPDATE a SET v = v + 100 WHERE k >= %d" % cut
            exp = seq[:m] + [(k, v + 100, x, ai)
                             for (k, v, x, ai) in seq[m:]]
        elif op == "negdrop":
            upd = "UPDATE a SET v = -v - 1 WHERE k >= %d" % cut
            exp = seq[:m]
        elif op == "del":
            upd = "DELETE FROM a WHERE k >= %d" % cut
            exp = seq[:m]
        elif op == "rekey":
            # k -> k+100: a_new.k can never equal b_orig.k (= old k)
            upd = "UPDATE a SET k = k + 100 WHERE k >= %d" % cut
            exp = seq[:m] + [(k + 100, v, None, ai)
                             for (k, v, x, ai) in seq[m:]]
        elif op == "rekey2":
            # k -> k%3; match survives iff b_orig.k == k%3 i.e. k<3
            upd = "UPDATE a SET k = k %% 3 WHERE k >= %d" % cut
            exp = seq[:m] + [(k % 3, v, x if k % 3 == k else None, ai)
                             for (k, v, x, ai) in seq[m:]]
        elif op == "bmod":
            # b-side rowmark updated: EPQ re-fetches b by TID -> new x
            upd = "UPDATE b SET x = -x - 100 WHERE k >= %d" % cut
            exp = seq[:m] + [(k, v, -x - 100, ai)
                             for (k, v, x, ai) in seq[m:]]
        elif op == "bkey":
            # b's join key moved: (a_orig, b_new) fails the inner join
            # -> the whole output row disappears
            upd = "UPDATE b SET k = k + 100 WHERE k >= %d" % cut
            exp = seq[:m]
        elif op == "chain":
            # two committed versions: EPQ must follow the ctid chain to
            # the latest version (v+100)*2
            upd = ["UPDATE a SET v = v + 100 WHERE k >= %d" % cut,
                   "UPDATE a SET v = v * 2 WHERE k >= %d" % cut]
            exp = seq[:m] + [(k, (v + 100) * 2, x, ai)
                             for (k, v, x, ai) in seq[m:]]
        elif op == "ownupd":
            # TM_SelfModified semantics (nodeLockRows.c): a tuple updated
            # by the same txn AFTER the portal snapshot is treated as
            # deleted — the FOR UPDATE cursor must SKIP it, not return
            # either version (Halloween-problem avoidance).
            upd = "UPDATE a SET v = v + 500 WHERE k >= %d" % cut
            exp = seq[:m]
        elif op == "subplan":
            # rekey a.k -> k%3: EPQ re-evals (a_new) -> subplan recounts
            # under the NEW k. A stale param would return the old count.
            upd = "UPDATE a SET k = k %% 3 WHERE k >= %d" % cut
            exp = seq[:m]
            for (k, v, _x, ai) in seq[m:]:
                nk = k % 3
                cnt = sum(1 for j in range(len(brows))
                          if brows[j][0] == nk)
                exp.append((nk, v, cnt, ai))
        else:  # skip: conn2 holds its txn OPEN during the resume; rows it
            # has locked (k>=cut, k even) are skipped outright — not EPQ'd
            upd = ("UPDATE a SET v = v + 100 WHERE k >= %d AND k %% 2 = 0"
                   % cut)
            exp = seq[:m] + [r for r in seq[m:] if r[0] % 2 == 1]
        case["epq"] = {"op": op, "m": m, "update": upd,
                       "hold_txn": op == "skip",
                       "own_txn": op == "ownupd",
                       "rr": (rng.random() < 0.25 and
                              op not in ("skip", "ownupd"))}
        case["expected_seq"] = ([r[:-1] for r in exp]
                                if join or op == "subplan"
                                else [(k, v) for (k, v, _x, _ai) in exp])
        case["ordered"] = True

    case.setdefault("ordered", True)
    return case


# ----------------------------------------------------------------- drivers
class DriverError(Exception):
    pass


def _conn(pg):
    return pg._conn or pg.connect()


def _rb(conn):
    """Explicit ROLLBACK on the shared connection. Runner connections are
    autocommit=True, so conn.rollback() is a no-op and cannot clear a dead
    (INERROR) transaction — only the ROLLBACK statement does. Emits a
    harmless warning when no txn is open."""
    try:
        conn.cursor().execute("ROLLBACK")
    except Exception:
        pass


def d_direct(pg, case):
    return pg.run(case["query"]).rows


def d_spill(pg, case):
    conn = _conn(pg)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute("SET LOCAL work_mem = '64kB'")
        cur.execute("SET LOCAL enable_memoize = on")
        cur.execute(case["query"])
        rows = cur.fetchall()
        cur.execute("COMMIT")
        return rows
    finally:
        _rb(conn)


def d_parallel(pg, case):
    conn = _conn(pg)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute("SET LOCAL max_parallel_workers_per_gather = 4")
        cur.execute("SET LOCAL parallel_setup_cost = 0")
        cur.execute("SET LOCAL parallel_tuple_cost = 0")
        cur.execute("SET LOCAL min_parallel_table_scan_size = 0")
        cur.execute("SET LOCAL min_parallel_index_scan_size = 0")
        try:
            cur.execute("SET LOCAL debug_parallel_query = on")
        except Exception:
            _rb(conn)
            cur.execute("BEGIN")
            for s in ("max_parallel_workers_per_gather=4",
                      "parallel_setup_cost=0", "parallel_tuple_cost=0",
                      "min_parallel_table_scan_size=0",
                      "min_parallel_index_scan_size=0"):
                cur.execute("SET LOCAL " + s)
        cur.execute(case["query"])
        rows = cur.fetchall()
        cur.execute("COMMIT")
        return rows
    finally:
        _rb(conn)


def d_cur1(pg, case):
    """Plain cursor, one row per FETCH: executor suspends/resumes per row."""
    conn = _conn(pg)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute("DECLARE c1 CURSOR FOR " + case["query"])
        rows = []
        while True:
            cur.execute("FETCH FORWARD 1 FROM c1")
            r = cur.fetchall()
            if not r:
                break
            rows.extend(r)
            if len(rows) > 200000:
                # result too large to fetch row-at-a-time profitably —
                # bag check already ran; treat as driver-skip not error
                cur.execute("COMMIT")
                return None
        cur.execute("COMMIT")
        return rows
    finally:
        _rb(conn)


class ScrollModel:
    """Model of a SCROLL cursor over fixed sequence E.

    cur = index of the row the cursor is ON; -1 = before first, n = after
    last.  Empirically established on pgmaster:
      FORWARD k lands ON last fetched row, or n if it ran out.
      BACKWARD k returns rows strictly BEFORE cur, lands ON last returned.
      FORWARD ALL / overshoot ends at cur = n (no current row).
      RELATIVE 0 refetches cur; empty when cur is a boundary.
      MOVE mirrors FETCH's final position without returning rows.
    """
    def __init__(self, E):
        self.E = [tuple(r) for r in E]
        self.cur = -1
        self.n = len(E)

    def _pos(self, tgt):
        """Position result of landing on row tgt (FETCH) or boundary."""
        if 0 <= tgt < self.n:
            self.cur = tgt
            return self.E[tgt:tgt + 1]
        self.cur = self.n if tgt >= self.n else -1
        return []

    def fetch(self, op, arg):
        E, n, cur = self.E, self.n, self.cur
        if op == "FORWARD":
            if arg == "ALL":
                r = E[cur + 1:]
                self.cur = n
                return r
            r = E[cur + 1:cur + 1 + arg]
            # overshoot lands at after-last (n); exact landing stays ON the
            # last row fetched (empirically verified: FORWARD n on n rows
            # leaves RELATIVE 0 -> last row).
            self.cur = cur + arg if cur + arg < n else n
            return r
        if op == "BACKWARD":
            if arg == "ALL":
                # ALL always lands before-first even when it exactly
                # exhausts the rows (differs from BACKWARD n!).
                r = E[:cur][::-1] if cur > 0 else []
                self.cur = -1
                return r
            r = E[max(0, cur - arg):cur][::-1] if cur > 0 else []
            # overshoot lands at before-first (-1) even if rows returned
            # (empirical: cur=1 BACKWARD 2 returns [0] then lands -1).
            self.cur = cur - arg if cur - arg >= 0 else -1
            return r
        if op == "FIRST":
            self.cur = 0 if n else -1
            return E[:1]
        if op == "LAST":
            self.cur = n - 1
            return E[n - 1:n] if n else []
        if op == "ABSOLUTE":
            if arg == 0:
                self.cur = -1
                return []
            return self._pos(arg - 1 if arg > 0 else n + arg)
        if op == "RELATIVE":
            return self._pos(cur + arg)
        raise ValueError(op)

    def move(self, op, arg):
        n, cur = self.n, self.cur
        if op == "FORWARD":
            self.cur = n if arg == "ALL" else min(cur + arg, n)
        elif op == "BACKWARD":
            self.cur = -1 if arg == "ALL" else max(cur - arg, -1)
        elif op == "ABSOLUTE":
            tgt = -1 if arg == 0 else (arg - 1 if arg > 0 else n + arg)
            self.cur = max(-1, min(n, tgt))
        elif op == "RELATIVE":
            self.cur = max(-1, min(n, cur + arg))
        elif op == "FIRST":
            self.cur = 0 if n else -1
        elif op == "LAST":
            self.cur = n - 1


def d_scroll(pg, case, rng):
    """SCROLL cursor: random FETCH/MOVE sequence; every fetched row must equal
    the positional model over the direct result sequence."""
    base = d_direct(pg, case)
    model = ScrollModel(base)
    conn = _conn(pg)
    cur = conn.cursor()
    diffs = []
    try:
        cur.execute("BEGIN")
        cur.execute("DECLARE cs SCROLL CURSOR FOR " + case["query"])
        fetches = {"FORWARD": [1, 2, 3, 5, "ALL"],
                   "BACKWARD": [1, 2, 3, "ALL"],
                   "FIRST": [None], "LAST": [None],
                   "ABSOLUTE": [0, 1, -1, rng.randint(1, max(2, len(base)))],
                   "RELATIVE": [0, 1, -1, 2, -2]}
        moves = {"FORWARD": [1, 3, "ALL"], "BACKWARD": [1, 2, "ALL"],
                 "ABSOLUTE": [0, 1, rng.randint(0, max(1, len(base)))],
                 "RELATIVE": [1, -1, 3, -3]}
        for _ in range(60):
            if rng.random() < 0.7:
                op = rng.choice(list(fetches))
                arg = rng.choice(fetches[op])
                sql = "FETCH %s %s FROM cs" % (op, "" if arg is None else arg)
                cur.execute(sql)
                got = [tuple(r) for r in cur.fetchall()]
                want = model.fetch(op, arg)
                if got != want:
                    diffs.append({"sql": sql, "got": got, "want": want,
                                  "cur": model.cur})
                    if len(diffs) > 5:
                        break
            else:
                op = rng.choice(list(moves))
                arg = rng.choice(moves[op])
                sql = "MOVE %s %s IN cs" % (op, "" if arg is None else arg)
                cur.execute(sql)
                model.move(op, arg)
        cur.execute("COMMIT")
    finally:
        _rb(conn)
    return {"base": base, "diffs": diffs}


def d_prep(pg, case):
    """PREPARE + EXECUTE a param probe: forces custom->generic plan transition."""
    conn = _conn(pg)
    cur = conn.cursor()
    rows = []
    try:
        cur.execute("BEGIN")
        cur.execute("PREPARE pp(int) AS SELECT a.k, count(*) FROM a"
                    " WHERE a.ten > $1 GROUP BY a.k ORDER BY a.k")
        seq = [0, 10, 20, 10, 0, 30, 10, 0]
        for p in seq:
            cur.execute("EXECUTE pp(%d)" % p)
            rows.append((p, tuple(tuple(r) for r in cur.fetchall())))
        try:
            cur.execute("SET LOCAL plan_cache_mode = force_generic_plan")
            for p in seq[:4]:
                cur.execute("EXECUTE pp(%d)" % p)
                rows.append(("gen", p, tuple(tuple(r) for r in cur.fetchall())))
        except Exception:
            _rb(conn)
            cur.execute("BEGIN")
        cur.execute("COMMIT")
        return rows
    finally:
        _rb(conn)
        try:
            conn.cursor().execute("DEALLOCATE pp")
        except Exception:
            pass


def d_ctas(pg, case):
    """CREATE TABLE AS runs the query through the DestReceiver path."""
    conn = _conn(pg)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute("DROP TABLE IF EXISTS dst")
        cur.execute("CREATE TABLE dst AS " + case["query"])
        cur.execute("SELECT * FROM dst")
        rows = cur.fetchall()
        cur.execute("DROP TABLE dst")
        cur.execute("COMMIT")
        return rows
    finally:
        _rb(conn)


def d_spt(pg, case):
    """Interleave a failed statement + ROLLBACK TO SAVEPOINT mid-fetch:
    the cursor's executor state must survive subxact abort and resume
    seamlessly."""
    conn = _conn(pg)
    cur = conn.cursor()
    rows = []
    try:
        cur.execute("BEGIN")
        cur.execute("DECLARE cs2 CURSOR FOR " + case["query"])
        cur.execute("FETCH FORWARD 3 FROM cs2")
        rows += cur.fetchall()
        cur.execute("SAVEPOINT sp1")
        try:
            cur.execute("SELECT 1/0")
        except Exception:
            pass
        cur.execute("ROLLBACK TO SAVEPOINT sp1")
        cur.execute("FETCH FORWARD 2 FROM cs2")
        rows += cur.fetchall()
        cur.execute("SAVEPOINT sp2")
        try:
            cur.execute("SELECT * FROM nonexistent_rel")
        except Exception:
            pass
        cur.execute("ROLLBACK TO SAVEPOINT sp2")
        # catalog change mid-cursor: the open portal holds its plan snapshot
        # and must continue producing the same remaining rows. A CREATE
        # INDEX on a table the portal reads raises ObjectInUse — that
        # conflict is itself exercised by wrapping in a savepoint.
        if any("CREATE TABLE a" in s for s in case.get("setup", [])):
            cur.execute("SAVEPOINT sp3")
            try:
                cur.execute("CREATE INDEX a_v_ix ON a(v)")
                cur.execute("DROP INDEX a_v_ix")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp3")
            # non-conflicting catalog churn: stats update fires relcache
            # invalidation while the portal keeps its plan snapshot
            cur.execute("ANALYZE a")
        cur.execute("FETCH FORWARD 3 FROM cs2")
        rows += cur.fetchall()
        cur.execute("FETCH FORWARD ALL FROM cs2")
        rows += cur.fetchall()
        cur.execute("COMMIT")
        return rows
    finally:
        _rb(conn)


def d_hold(pg, case, rng):
    """SCROLL + WITH HOLD cursor: the result is materialized eagerly, then
    fetched post-commit — exercises the tuplestore-across-commit path.
    Every fetched row is checked positionally against the direct run."""
    base = d_direct(pg, case)
    model = ScrollModel(base)
    conn = _conn(pg)
    cur = conn.cursor()
    diffs = []
    fetches = {"FORWARD": [1, 2, 3, "ALL"], "BACKWARD": [1, 2, "ALL"],
               "FIRST": [None], "LAST": [None],
               "ABSOLUTE": [0, 1, -1, rng.randint(1, max(2, len(base)))],
               "RELATIVE": [0, 1, -1, 2]}
    moves = {"FORWARD": [1, 3], "BACKWARD": [1, 2],
             "ABSOLUTE": [0, 1], "RELATIVE": [1, -1, 2, -2]}
    def run_ops(nops):
        for _ in range(nops):
            if rng.random() < 0.7:
                op = rng.choice(list(fetches))
                arg = rng.choice(fetches[op])
                cur.execute("FETCH %s %s FROM ch" % (op, "" if arg is None else arg))
                got = [tuple(r) for r in cur.fetchall()]
                want = model.fetch(op, arg)
                if got != want:
                    diffs.append({"sql": "FETCH %s %s" % (op, arg),
                                  "got": got[:3], "want": want[:3],
                                  "cur": model.cur})
                    if len(diffs) > 5:
                        return
            else:
                op = rng.choice(list(moves))
                arg = rng.choice(moves[op])
                cur.execute("MOVE %s %s IN ch" % (op, "" if arg is None else arg))
                model.move(op, arg)
    try:
        cur.execute("BEGIN")
        cur.execute("DECLARE ch SCROLL CURSOR WITH HOLD FOR " + case["query"])
        run_ops(15)
        cur.execute("COMMIT")
        run_ops(35)          # post-commit tuplestore reads
        cur.execute("CLOSE ch")
    finally:
        # WITH HOLD portals survive rollback/commit — close explicitly or
        # they pin the schema for the next case's setup (CLOSE needs a txn)
        try:
            _rb(conn)
            cur.execute("BEGIN")
            cur.execute("CLOSE ch")
            cur.execute("COMMIT")
        except Exception:
            _rb(conn)
    return {"diffs": diffs}


def d_epq(pg, case):
    """EvalPlanQual probe: open a FOR UPDATE cursor, fetch m rows, then a
    second connection mutates only rows the cursor has NOT reached yet
    (k >= a strict boundary). On resume, LockRows must re-evaluate each
    stored row against the NEW tuple version — re-qualifying WHERE and
    re-running the join. The result is compared against a closed-form
    model of EPQ semantics."""
    import psycopg2
    spec = case["epq"]
    conn = _conn(pg)
    cur = conn.cursor()
    conn2 = psycopg2.connect(pg._server.get_uri())
    conn2.autocommit = True
    cur2 = conn2.cursor()
    cur2.execute("SET lock_timeout = '8s'")
    diffs = []
    part = spec["op"] in ("pmove", "pdel")
    try:
        cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ"
                    if spec.get("rr") else "BEGIN")
        cur.execute("DECLARE e CURSOR FOR " + case["lockq"])
        cur.execute("FETCH %d FROM e" % spec["m"])
        pre = [tuple(r) for r in cur.fetchall()]
        aborted = None
        if part:
            # boundary from observed fetch: every fetched row has x < cut,
            # so the update touches only unfetched rows
            cut = max(r[0] for r in pre) + 1
            updates = (["UPDATE p SET x = 299 - x WHERE x >= %d" % cut]
                       if spec["op"] == "pmove"
                       else ["DELETE FROM p WHERE x >= %d" % cut])
            for u in updates:
                cur2.execute(u)
            try:
                cur.execute("FETCH FORWARD ALL FROM e")
                post = [tuple(r) for r in cur.fetchall()]
                cur.execute("COMMIT")
            except Exception as e:
                aborted = repr(e)[:160]
                post = None
        elif spec.get("hold_txn"):
            cur2.execute("BEGIN")
            cur2.execute(spec["update"])
            cur.execute("FETCH FORWARD ALL FROM e")
            post = [tuple(r) for r in cur.fetchall()]
            cur2.execute("COMMIT")
        elif spec.get("own_txn"):
            # same transaction mutates rows the cursor hasn't reached —
            # the portal's snapshot must hide them
            cur.execute(spec["update"])
            cur.execute("FETCH FORWARD ALL FROM e")
            post = [tuple(r) for r in cur.fetchall()]
        else:
            for u in ([spec["update"]] if isinstance(spec["update"], str)
                      else spec["update"]):
                cur2.execute(u)
            try:
                cur.execute("FETCH FORWARD ALL FROM e")
                post = [tuple(r) for r in cur.fetchall()]
                cur.execute("COMMIT")
            except Exception as e:
                aborted = repr(e)[:160]
                post = None
    finally:
        _rb(conn)
        conn2.close()
    if spec.get("rr"):
        # REPEATABLE READ: EPQ cannot re-evaluate against a post-snapshot
        # version — the resume MUST fail with could-not-serialize.
        # Silently returning stale rows would be the anomaly.
        if aborted is None:
            diffs.append({"op": spec["op"], "rr_no_abort": True,
                          "ngot": len(pre + (post or []))})
        elif "could not serialize" not in aborted:
            diffs.append({"op": spec["op"], "rr_bad_abort": aborted})
        return {"diffs": diffs}
    if part:
        if aborted is not None:
            # a cross-partition move is a delete+insert across rels —
            # FOR UPDATE legitimately reports it as a serialization
            # failure. Any OTHER abort, or silently wrong data, is a
            # candidate bug.
            if "moved to another partition" in aborted:
                return {"diffs": [], "note": aborted}
            diffs.append({"op": spec["op"], "unexpected_abort": aborted})
            return {"diffs": diffs}
        got = pre + post
        # bag oracle: pre rows (x < cut) unchanged; every row with
        # x >= cut was unfetched and gets EPQ-transformed / deleted
        base = [tuple(r) for r in case["base_rows"]]
        if spec["op"] == "pmove":
            want_bag = (Counter(r for r in base if r[0] < cut) +
                        Counter((299 - r[0], r[1]) for r in base
                                if r[0] >= cut))
        else:
            want_bag = Counter(r for r in base if r[0] < cut)
        got_bag = Counter(got)
        if got_bag != want_bag:
            diffs.append({"op": spec["op"], "cut": cut,
                          "extra": [list(r) for r in
                                    (got_bag - want_bag).elements()][:6],
                          "missing": [list(r) for r in
                                      (want_bag - got_bag).elements()][:6]})
        return {"diffs": diffs}
    if aborted is not None:
        diffs.append({"op": spec["op"], "unexpected_abort": aborted})
        return {"diffs": diffs}
    got = pre + post
    want = [tuple(r) for r in case["expected_seq"]]
    if got != want:
        # locate first divergence for the report
        at = next((i for i in range(min(len(got), len(want)))
                   if got[i] != want[i]), min(len(got), len(want)))
        diffs.append({"op": spec["op"], "at": at,
                      "got": got[max(0, at - 1):at + 3],
                      "want": want[max(0, at - 1):at + 3],
                      "ngot": len(got), "nwant": len(want)})
    return {"diffs": diffs}


def d_curupd(pg, case, rng):
    """UPDATE ... WHERE CURRENT OF: each positioned update must hit exactly
    the row the cursor sits on — v+1M marks each visited row, so a missed
    hit shows as count mismatch, a double hit as a >1.9M value."""
    conn = _conn(pg)
    cur = conn.cursor()
    diffs = []
    try:
        cur.execute("BEGIN")
        cur.execute("DECLARE cu CURSOR FOR SELECT k FROM a "
                    "ORDER BY k FOR UPDATE")
        nupd = 0
        while nupd < 40:
            cur.execute("FETCH FORWARD 1 FROM cu")
            if not cur.fetchall():
                break
            cur.execute("UPDATE a SET v = v + 1000000 WHERE CURRENT OF cu")
            if cur.rowcount != 1:
                diffs.append({"rowcount": cur.rowcount, "at": nupd})
                break
            nupd += 1
        cur.execute("COMMIT")
        cur.execute("SELECT count(*) FILTER (WHERE abs(v) > 900000),"
                    " count(*) FILTER (WHERE abs(v) > 1900000) FROM a")
        once, twice = cur.fetchone()
        if once != nupd or twice != 0:
            diffs.append({"once": once, "twice": twice, "nupd": nupd})
    finally:
        _rb(conn)
    return {"diffs": diffs}


# planner-feature toggles are semantically transparent: disabling any of
# these must not change the result bag. Each arm reaches different
# machinery (mergejoin mark/restore, index-only VM, memoize cache,
# incsort presorted-prefix, async append, hashagg spill, ...).
TOGGLES = ["enable_partition_pruning", "enable_indexonlyscan",
           "enable_indexscan", "enable_bitmapscan", "enable_seqscan",
           "enable_hashjoin", "enable_mergejoin", "enable_nestloop",
           "enable_memoize", "enable_material", "enable_sort",
           "enable_hashagg", "enable_incremental_sort",
           "enable_parallel_append", "enable_gathermerge",
           "enable_async_append", "enable_partitionwise_join",
           "enable_partitionwise_aggregate", "enable_tidscan",
           "enable_presorted_aggregate", "enable_self_join_elimination",
           "enable_distinct_reordering", "enable_eager_aggregate",
           "enable_groupagg", "enable_group_by_reordering",
           "enable_parallel_hash"]
# non-boolean transparency toggles: forced JIT compiles expression eval —
# a completely different evaluation machinery that must agree
GUC_VALS = [("jit_above_cost", "0"), ("jit_inline_above_cost", "0"),
            ("jit_tuple_deform", "on")]


def d_guctoggle(pg, case, rng, base):
    """Random feature toggles off -> bag must equal direct execution."""
    conn = _conn(pg)
    cur = conn.cursor()
    base_bag = Counter(map(tuple, base))
    diffs = []
    arms = [(g, "off") for g in rng.sample(TOGGLES, 3)]
    if rng.random() < 0.4:
        arms += rng.sample(GUC_VALS, 1)  # forced-JIT arm
    for g, val in arms:
        try:
            cur.execute("BEGIN")
            cur.execute("SET LOCAL %s = %s" % (g, val))
            cur.execute(case["query"])
            rows = [tuple(r) for r in cur.fetchall()]
            cur.execute("COMMIT")
        except Exception as e:
            _rb(conn)
            if "unrecognized configuration" not in repr(e):
                diffs.append({"guc": g, "err": repr(e)[:140]})
            continue
        if Counter(rows) != base_bag:
            diffs.append({"guc": g, "ngot": len(rows), "nwant": len(base)})
        elif case.get("ordered") and rows != [tuple(x) for x in base]:
            diffs.append({"guc": g, "order": True})
    return {"diffs": diffs}


DRIVERS = ["direct", "spill", "parallel", "cur1", "scroll", "prep",
           "ctas", "hold", "spt", "epq", "guctoggle"]


# ----------------------------------------------------------------- runner
def _fdw_setup(pg):
    """postgres_fdw loopback: two foreign tables over local table b,
    async_capable so Append over them uses the async-rescan path."""
    from urllib.parse import urlparse, parse_qs
    u = urlparse(pg._server.get_uri())
    qs = parse_qs(u.query)
    host = qs["host"][0] if "host" in qs else (u.hostname or "127.0.0.1")
    port = qs.get("port", [str(u.port or 5432)])[0]
    who = pg.run("SELECT current_user")
    user = who.rows[0][0] if who.ok and who.rows else "postgres"
    return [
        "CREATE EXTENSION IF NOT EXISTS postgres_fdw",
        "DROP SERVER IF EXISTS loopback CASCADE",
        "CREATE SERVER loopback FOREIGN DATA WRAPPER postgres_fdw "
        "OPTIONS (host '%s', dbname 'postgres', port '%s', "
        "fetch_size '10', async_capable 'true')" % (host, port),
        "CREATE USER MAPPING FOR CURRENT_USER SERVER loopback "
        "OPTIONS (user '%s')" % user,
        "DROP FOREIGN TABLE IF EXISTS f1",
        "DROP FOREIGN TABLE IF EXISTS f2",
        "CREATE FOREIGN TABLE f1 (k int, x int) SERVER loopback "
        "OPTIONS (table_name 'b')",
        "CREATE FOREIGN TABLE f2 (k int, x int) SERVER loopback "
        "OPTIONS (table_name 'b')",
    ]


def run_case(pg, case, rng):
    setup = list(case["setup"])
    if case.get("needs_fdw"):
        setup += _fdw_setup(pg)
    try:
        pg.setup(setup)
    except Exception as e:
        return {"family": case["family"], "id": case["id"],
                "verdict": "setup_error", "err": repr(e)[:300]}
    out = {"family": case["family"], "id": case["id"], "meta": case.get("meta")}
    # merge/dmlcte families mutate the table — checked against a
    # closed-form model of emitted rows AND final state, not driver
    # equivalence
    if case["family"] in ("merge", "dmlcte"):
        key = case["family"]
        try:
            conn = _conn(pg)
            cur = conn.cursor()
            cur.execute(case["query"])
            emitted = [tuple(r) for r in cur.fetchall()] \
                if case["family"] == "dmlcte" else None
            conn.commit()
            cur.execute(case["check_sql"])
            got = Counter(map(tuple, cur.fetchall()))
            want = case["expected_bag"]
            bad = {}
            if got != want:
                bad["final"] = {"extra": [list(r) for r in
                                          (got - want).elements()][:6],
                                "missing": [list(r) for r in
                                            (want - got).elements()][:6]}
            if emitted is not None and emitted != case["expected_rows"]:
                bad["emitted"] = {"got": emitted[:5],
                                  "want": case["expected_rows"][:5],
                                  "ngot": len(emitted),
                                  "nwant": len(case["expected_rows"])}
            out[key] = "ok" if not bad else bad
        except Exception as e:
            _rb(conn)
            out[key] = "ERR:" + repr(e)[:200]
        out.setdefault("verdict", "done")
        return out
    try:
        base = d_direct(pg, case)
        base_bag = Counter(map(tuple, base))
        out["rows"] = len(base)

        for d in ["spill", "parallel", "cur1", "ctas", "spt"]:
            try:
                r = {"direct": d_direct, "spill": d_spill,
                     "parallel": d_parallel, "cur1": d_cur1,
                     "ctas": d_ctas, "spt": d_spt}[d](pg, case)
            except Exception as e:
                out[d] = "ERR:" + repr(e)[:200]
                continue
            if r is None:                    # driver skipped (row cap)
                out[d] = "skip"
                continue
            same = Counter(map(tuple, r)) == base_bag
            # sequence check only for drivers that preserve result order
            if d not in ("ctas",) and case.get("ordered") and same:
                same = [tuple(x) for x in r] == [tuple(x) for x in base]
            out[d] = "ok" if same else {"diff": True,
                                        "got": len(r), "want": len(base)}
        if case.get("ordered") and len(base) <= 5000:
            for d, fn in (("scroll", d_scroll), ("hold", d_hold)):
                try:
                    sc = fn(pg, case, rng)
                    out[d] = "ok" if not sc["diffs"] else {"diffs": sc["diffs"][:3]}
                except Exception as e:
                    out[d] = "ERR:" + repr(e)[:200]
        try:
            pr = d_prep(pg, case) if any("CREATE TABLE a" in s
                                         for s in case["setup"]) else []
            # group by param value: all execs of same param must be identical
            seenp = {}
            bad = []
            for item in pr:
                if item[0] == "gen":
                    _, p, res = item
                    if seenp.get(p) != res:
                        bad.append(("generic_vs_custom", p))
                else:
                    seenp[item[0]] = item[1]
            out["prep"] = "ok" if not bad else {"diff": bad}
        except Exception as e:
            out["prep"] = "ERR:" + repr(e)[:200]

        # feature-toggle differential: disabling planner features must be
        # semantically transparent (pruning, index-only, memoize, incsort,
        # async append, join-algo swaps incl. mergejoin mark/restore)
        try:
            gt = d_guctoggle(pg, case, rng, base)
            out["guctoggle"] = ("ok" if not gt["diffs"]
                                else {"diffs": gt["diffs"][:4]})
        except Exception as e:
            out["guctoggle"] = "ERR:" + repr(e)[:200]

        # EPQ: second-connection concurrent update mid-cursor
        if case["family"] == "epq" and case.get("epq"):
            try:
                case["base_rows"] = base
                ep = d_epq(pg, case)
                out["epq"] = "ok" if not ep["diffs"] else {"diffs": ep["diffs"]}
            except Exception as e:
                out["epq"] = "ERR:" + repr(e)[:200]

        # absolute check for memo family (closed form already computed)
        if case["family"] == "memo" and case.get("expected"):
            exp = case["expected"][0][0]
            got = base[0][0] if base else None
            out["memo_expected"] = exp
            out["memo_got"] = got
            if exp != got:
                out["memo_wrong"] = True

        # WHERE CURRENT OF — runs LAST: it mutates table a, so every
        # read-only driver (incl. epq expectations built from base rows)
        # must have finished first.
        if any("CREATE TABLE a" in s for s in case["setup"]):
            try:
                cu = d_curupd(pg, case, rng)
                out["curupd"] = ("ok" if not cu["diffs"]
                                 else {"diffs": cu["diffs"][:4]})
            except Exception as e:
                out["curupd"] = "ERR:" + repr(e)[:200]
    except Exception as e:
        out["verdict"] = "error"
        out["err"] = repr(e)[:300]
    finally:
        for t in case.get("teardown", []):
            try:
                pg._conn.cursor().execute(t)
            except Exception:
                _rb(pg._conn)
        _rb(pg._conn)
    out.setdefault("verdict", "done")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="pgmaster_assert")
    ap.add_argument("--cases", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    prefix = pg_build_prefix(args.build)
    outdir = Path("results/execdiff/%s_s%d" % (args.build, args.seed))
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    pg = PostgresRunner(datadir="/tmp/execdiff_%s_%d" % (args.build, args.seed),
                        pg_prefix=prefix)
    counts = Counter()
    t0 = time.time()
    with open(outdir / "report.jsonl", "w") as fh:
        for i in range(args.cases):
            case = gen_cases(rng, i, args.n)
            r = run_case(pg, case, rng)
            bad = any(isinstance(r.get(d), dict) for d in
                      ("spill", "parallel", "cur1", "scroll", "prep",
                       "ctas", "hold", "spt", "epq", "merge", "dmlcte",
                       "guctoggle", "curupd"))
            bad = bad or r.get("memo_wrong") or "ERR:" in json.dumps(r)
            key = "%s/%s" % (r["family"], "DIFF" if bad else "ok")
            counts[key] += 1
            if bad:
                r["query"] = case["query"]
                r["setup"] = case["setup"]
            fh.write(json.dumps(r, default=str) + "\n")
            if i % 25 == 0:
                print("[%s] %d/%d %s (%.0fs)" % (args.build, i, args.cases,
                                               dict(counts), time.time() - t0),
                      flush=True)
    print("DONE", dict(counts))
    print("report:", outdir / "report.jsonl")


if __name__ == "__main__":
    main()
