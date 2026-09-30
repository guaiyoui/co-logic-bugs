"""Belief-directed generation: queries crafted so the planner MUST take a
position on a boundary belief, then the belief is audited AGAINST THE DATA
with an independent Python evaluator — not a second engine query, so the
audit cannot be mis-optimized by the same bug.

Each generated case:
    setup      schema + adversarial data (unmatched outer rows, dup keys,
               NULLs, partition-edge values)
    q          the query under test
    asserted   does the plan assert the target belief (evidence string)
    violated   Python check of the claim on the generated data — a single
               counterexample falsifies the universal claim
    expected   absolute result computed by the Python evaluator

Verdicts: false_belief (asserted AND violated — the finding exists even
when expected happens to match), wrong_result, fired_clean, not_fired.

Families:
  runcond  window monotonicity -> Run Condition          (#19533 class)
  ojr      LEFT JOIN reduction / qual strictness         (#19412 class)
  lat      pushed qual through LATERAL UNION ALL         (#19412 verbatim)
  inline   STRICT SQL-function inlining                  (SAOP strictness)
  iu       inner uniqueness / join removal               (join_is_removable)
  part     partition pruning / constraint exclusion

Usage:
    python scripts/pg_belief_gen.py \
        --prefix $COEVO_PGBLD/pgmaster_assert \
        --cases 200 --seed 1 --out results/belief_gen/master_a
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner  # noqa: E402
from oracles.pg_plan_beliefs import (  # noqa: E402
    walk_plan, _scanned_relations)


# ============================ plan helpers ============================

def nodes_of(plan_json) -> list[dict]:
    try:
        return list(walk_plan(plan_json[0]["Plan"]))
    except (KeyError, IndexError, TypeError):
        return []


def plan_join_types(nodes) -> list[str]:
    return [n["Join Type"] for n in nodes if "Join Type" in n]


def run_condition_present(nodes) -> bool:
    return any("Run Condition" in n for n in nodes)


def rel_scanned(nodes, rel: str) -> bool:
    return rel in _scanned_relations(nodes)


def inner_unique_present(nodes) -> bool:
    return any(n.get("Inner Unique") for n in nodes)


# ============================ python mini-evaluator ============================
# value model: int | None (SQL NULL). 3VL everywhere.

def tvl_and(a, b):
    return (False if a is False or b is False
            else None if a is None or b is None else True)


def tvl_or(a, b):
    return (True if a is True or b is True
            else None if a is None or b is None else False)


def tvl_not(a):
    return None if a is None else not a


def cmp3(a, op, b):
    """3VL comparison on ints; None anywhere -> None."""
    if a is None or b is None:
        return None
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
            "=": a == b, "<>": a != b}[op]


def eval_window(rows: list[tuple[int, Any]], func: str, order: bool,
                lo: str, hi: str) -> list[Any]:
    """rows = [(r, x)] in input order; returns c per row in processing
    order.  Supports count(*)/count(x)/sum/min/max/row_number over
    ROWS BETWEEN simple bounds."""
    idx = sorted(range(len(rows)), key=lambda i: rows[i][0]) \
        if order else list(range(len(rows)))
    n = len(idx)
    out = [None] * n

    def bound(b, i):
        if b == "UNBOUNDED PRECEDING":
            return 0
        if b == "UNBOUNDED FOLLOWING":
            return n - 1
        if b.endswith("PRECEDING"):
            return i - int(b.split()[0])
        if b == "CURRENT ROW":
            return i
        return i + int(b.split()[0])  # n FOLLOWING

    for pos, ri in enumerate(idx):
        a = max(0, bound(lo, pos))
        bb = min(n - 1, bound(hi, pos))
        frame = [rows[idx[j]][1] for j in range(a, bb + 1)] if a <= bb else []
        vals = [v for v in frame if v is not None]
        if func == "count(*)":
            c = len(frame)
        elif func == "count(x)":
            c = len(vals)
        elif func == "sum(x)":
            c = sum(vals) if vals else None
        elif func == "min(x)":
            c = min(vals) if vals else None
        elif func == "max(x)":
            c = max(vals) if vals else None
        elif func == "row_number()":
            c = pos + 1
        else:
            raise ValueError(func)
        out[pos] = (rows[ri][0], c)
    return out, idx


_RC_TAIL = re.compile(r"(<=|>=|<|>|=)\s*(-?\d+(?:\.\d+)?)\s*\)?\s*$")


def _rc_pred(text: str):
    """Parse the trailing '<wfexpr> <op> <const>' of a Run Condition
    string into a python predicate over the computed column value."""
    m = _RC_TAIL.search(text or "")
    if not m:
        return None
    op, ks = m.group(1), m.group(2)
    k = float(ks)
    if k.is_integer():
        k = int(k)
    return lambda c: None if c is None else cmp3(c, op, k)


def rc_violated(proc_cs, rc_pred, qual_pred) -> tuple[bool, str]:
    """Run-condition guarantee: once it evaluates not-TRUE at position
    i, no row at/after i may satisfy the outer qual.  Violated iff
    exists i with rc(cs[i]) not True and some j >= i with q(cs[j])
    True."""
    for i, c in enumerate(proc_cs):
        if rc_pred(c) is not True:
            if any(qual_pred(proc_cs[j]) is True
                   for j in range(i, len(proc_cs))):
                return True, (f"rc false at pos {i} (c={c}) but outer "
                              f"qual true later in frame order")
    return False, "rc never false before last true qual row"


# ============================ generators ============================

def _vals(rng, n, pool=(0, 1, 1, 1, 2, 3, None)):
    return [rng.choice(pool) for _ in range(n)]


def _mk_table(name, rows2):
    def lit(v):
        return "NULL" if v is None else str(v)
    return [f"CREATE TABLE {name}(r int, x int)",
            f"INSERT INTO {name} VALUES " +
            ",".join(f"({r},{lit(x)})" for r, x in rows2)]


def gen_runcond(rng) -> dict:
    n = rng.randint(3, 7)
    rs = rng.sample(range(1, n + 1), n)   # permuted so ORDER BY bites
    rows = [(rs[i], v) for i, v in enumerate(_vals(rng, n))]
    order = rng.random() < 0.6
    func = rng.choice(["count(x)", "count(*)", "sum(x)",
                       "min(x)", "max(x)", "row_number()"])
    lo = rng.choice(["UNBOUNDED PRECEDING", "1 PRECEDING",
                     "2 PRECEDING", "CURRENT ROW"])
    hi = rng.choice(["CURRENT ROW", "UNBOUNDED FOLLOWING",
                     "1 FOLLOWING", "2 FOLLOWING"])
    frame = ("ORDER BY r " if order else "") + \
        f"ROWS BETWEEN {lo} AND {hi}"
    op, k = rng.choice([("=", 1), ("=", 2), ("<=", 2), ("<", 2),
                        (">=", 1), (">", 0), ("<=", 1), ("=", 0)])
    q = (f"SELECT r, c FROM (SELECT r, {func} OVER ({frame}) AS c "
         f"FROM w) s WHERE c {op} {k}")

    # python truth; out[] is ALREADY in frame-processing order
    # (out[pos] = (r, c) of the row processed at position pos)
    out, idx = eval_window(rows, func, order, lo, hi)
    cs = out                                     # [(r, c)] frame order
    proc_cs = [out[p][1] for p in range(n)]      # c in frame order
    expected = [[r, c] for r, c in cs
                if cmp3(c, op, k) is True]
    qpy = lambda c: cmp3(c, op, k) is True

    return {
        "family": "runcond", "setup": _mk_table("w", rows), "q": q,
        "asserted": lambda n_, _t: run_condition_present(n_),
        "asserted_desc": "Run Condition in plan",
        "rc_data": (proc_cs, qpy),
        "expected": expected,
        "meta": {"func": func, "frame": frame, "qual": f"c{op}{k}"},
    }


_OJ_QUALS: list[tuple[str, Callable[[dict | None], Any], bool]] = [
    # (sql template over alias b, py over brow|None, strict?)
    ("b.x IS NOT NULL", lambda b: b is not None and b["x"] is not None, True),
    ("b.x > 0", lambda b: None if b is None or b["x"] is None else b["x"] > 0, True),
    ("b.x IS NOT DISTINCT FROM 0",
     # strict: NULL IS NOT DISTINCT FROM 0 -> FALSE, rejects null-ext
     lambda b: b is not None and b["x"] is not None and b["x"] == 0, True),
    ("b.x IS NOT DISTINCT FROM NULL",
     lambda b: b is None or b["x"] is None, False),
    ("COALESCE(b.x,-1) > -2",
     lambda b: (b["x"] if b and b["x"] is not None else -1) > -2, False),
    ("b.x = ANY (ARRAY[1,2])",
     lambda b: None if b is None or b["x"] is None else b["x"] in (1, 2), True),
    ("b.x = ANY ('{}'::int[])",
     lambda b: False, True),  # empty ANY -> FALSE even for NULL
    ("b.x <> ALL ('{}'::int[])",
     lambda b: True, False),  # empty ALL -> TRUE even for NULL (!)
    ("b.x IS NULL OR b.x > 5",
     lambda b: (b is None or b["x"] is None) or b["x"] > 5, False),
    ("NOT (b.x IS NULL)",
     lambda b: b is not None and b["x"] is not None, True),
    ("b.x <> ALL (ARRAY[1,2])",
     lambda b: None if b is None or b["x"] is None else b["x"] not in (1, 2), True),
    ("CASE WHEN b.x IS NULL THEN true ELSE b.x > 0 END",
     lambda b: True if b is None or b["x"] is None else b["x"] > 0, False),
    ("b.x IS NOT NULL AND b.x > 0",
     lambda b: b is not None and b["x"] is not None and b["x"] > 0, True),
]


def gen_ojr(rng) -> dict:
    na, nb = rng.randint(2, 4), rng.randint(2, 5)
    aks = list(range(1, na + 1))
    # b keys: subset of a's + own — guarantees unmatched a rows often
    bks = [rng.choice(aks + [na + 1]) for _ in range(nb)]
    a_rows = [(k, rng.choice([10, 20, 30])) for k in aks]
    b_rows = [(k, rng.choice([0, 1, 2, None, 5])) for k in bks]
    qidx = rng.randrange(len(_OJ_QUALS))
    qsql, qpy, strict = _OJ_QUALS[qidx]
    q = ("SELECT a.k, b.x FROM a LEFT JOIN b ON a.k = b.k "
         f"WHERE {qsql}")

    def lit(v):
        return "NULL" if v is None else str(v)
    setup = [
        "CREATE TABLE a(k int, v int)",
        "INSERT INTO a VALUES " +
        ",".join(f"({k},{v})" for k, v in a_rows),
        "CREATE TABLE b(k int, x int)",
        "INSERT INTO b VALUES " +
        ",".join(f"({k},{lit(x)})" for k, x in b_rows),
    ]

    # python truth + belief check
    violated = False
    expected = []
    for k, v in a_rows:
        ms = [b for b in b_rows if b[0] == k]
        if not ms:
            if qpy(None) is True:
                violated = True   # qual TRUE on null-extended row
                expected.append([k, None])
        else:
            for bk, bx in ms:
                if qpy({"k": bk, "x": bx}) is True:
                    expected.append([k, bx])

    asserted = lambda n, _t: (
        plan_join_types(n).count("Left") +
        plan_join_types(n).count("Right") < 1)

    return {
        "family": "ojr", "setup": setup, "q": q,
        "asserted": asserted,
        "asserted_desc": "declared LEFT JOIN absent from plan",
        "violated": violated,
        "violation_desc": "qual TRUE on a null-extended row "
                          f"(qual={qsql})",
        "expected": expected,
        "meta": {"qual": qsql, "strict_by_design": strict},
    }


def gen_lat(rng) -> dict:
    """#19412 shape family: pushed qual through LATERAL UNION ALL.

    The buggy drop fires only when the pushed qual is *redundant on
    the base column* — 'x IS NOT NULL' on a NOT NULL x, or 'x > 0'
    under CHECK (x > 0).  On buggy builds nullingrels fails to reach
    through the UNION ALL branch, so the drop also strips the qual
    from null-extended rows (x really IS NULL there).  On fixed
    builds the pushed qual is kept for the nullable side.
    """
    na = rng.randint(2, 4)
    aks = list(range(1, na + 1))
    nb = rng.randint(1, 4)
    bks = [rng.choice(aks + [na + 1]) for _ in range(nb)]
    a_rows = [(k, rng.choice([1, 2, 3, None])) for k in aks]
    variant = rng.choice(["notnull", "check_pos", "check_eq"])
    if variant == "notnull":
        decl = "CREATE TABLE t2 (k int, x int NOT NULL)"
        qual, qpy = ("s.u IS NOT NULL", lambda u: u is not None)
        needle = "t2.x IS NOT NULL"
        b_rows = [(k, rng.choice([1, 2, 3])) for k in bks]
    elif variant == "check_pos":
        decl = "CREATE TABLE t2 (k int, x int NOT NULL CHECK (x > 0))"
        qual, qpy = ("s.u > 0", lambda u: None if u is None else u > 0)
        needle = "t2.x > 0"
        b_rows = [(k, rng.choice([1, 2, 3])) for k in bks]
    else:
        decl = "CREATE TABLE t2 (k int, x int NOT NULL CHECK (x = 2))"
        qual, qpy = ("s.u = 2", lambda u: None if u is None else u == 2)
        needle = "t2.x = 2"
        b_rows = [(k, 2) for k in bks]

    def lit(v):
        return "NULL" if v is None else str(v)
    setup = [
        "CREATE TABLE t_append (k int not null, v int)",
        "INSERT INTO t_append VALUES " +
        ",".join(f"({k},{lit(v)})" for k, v in a_rows),
        decl,
        "INSERT INTO t2 VALUES " +
        ",".join(f"({k},{x})" for k, x in b_rows),
    ]
    q = ("SELECT t1.k, s.u FROM t_append t1 LEFT JOIN t2 "
         "ON t1.k = t2.k JOIN LATERAL "
         "(SELECT t1.v AS u UNION ALL SELECT t2.x AS u) s ON true "
         f"WHERE {qual}")

    # belief: the pushed qual on the t2 branch can be dropped because
    # 't2.x' (null-extended) is believed never NULL
    unmatched = [a for a in a_rows
                 if not any(b[0] == a[0] for b in b_rows)]
    violated = bool(unmatched)  # any null-ext row feeds a NULL u

    expected = []
    for k, v in a_rows:
        ms = [b for b in b_rows if b[0] == k]
        # each matched join row emits (u=v, u=x_i) from the UNION ALL;
        # an unmatched (null-extended) row emits (u=v, u=NULL)
        us = ([v] * len(ms) + [b[1] for b in ms]) if ms else [v, None]
        for u in us:
            if qpy(u) is True:
                expected.append([k, u])

    asserted = lambda _n, t: needle not in t

    return {
        "family": "lat", "setup": setup, "q": q,
        "asserted": asserted,
        "asserted_desc": f"pushed qual {needle!r} absent from plan",
        "violated": violated,
        "violation_desc": f"{len(unmatched)} null-extended rows feed "
                          "the t2 branch",
        "expected": expected,
        "meta": {"variant": variant, "qual": qual},
    }


_INLINE_BODIES = [
    # (body sql ($1), return type, py(x), strict? -> STRICT contract
    # demands NULL->NULL)
    ("$1 = ANY ('{}'::int[])", "bool", lambda x: False, True),
    ("$1 = ALL ('{}'::int[])", "bool", lambda x: True, True),
    ("$1 = ANY (ARRAY[1,2])", "bool",
     lambda x: None if x is None else x in (1, 2), True),
    ("$1 = ALL (ARRAY[1,2])", "bool",
     lambda x: None if x is None else all(x == e for e in (1, 2)),
     True),
    ("$1 + 0", "int", lambda x: None if x is None else x, True),
    ("$1 * 2", "int", lambda x: None if x is None else x * 2, True),
    ("$1 IS NOT DISTINCT FROM 5", "bool",
     lambda x: False if x is None else x == 5, False),
    ("COALESCE($1, 0)", "int", lambda x: 0 if x is None else x, False),
    ("CASE WHEN $1 IS NULL THEN false ELSE true END", "bool",
     lambda x: False if x is None else True, False),
    ("NOT ($1 IS NULL)", "bool",
     lambda x: False if x is None else True, False),
]


def gen_inline(rng) -> dict:
    body, rtype, bpy, _strict = \
        _INLINE_BODIES[rng.randrange(len(_INLINE_BODIES))]
    fname = f"f{rng.randrange(10**6)}"
    xs = [None] + _vals(rng, rng.randint(2, 4), pool=(0, 1, 2, 5))
    q = f"SELECT x, {fname}(x) FROM t ORDER BY x NULLS FIRST"
    setup = [
        "CREATE TABLE t (x int)",
        "INSERT INTO t VALUES " + ",".join(
            "(NULL)" if v is None else f"({v})" for v in xs),
        f"CREATE FUNCTION {fname}(int) RETURNS {rtype} LANGUAGE SQL "
        f"STRICT IMMUTABLE AS $$ SELECT {body} $$",
    ]
    # STRICT contract: NULL in -> NULL out; body evaluated on NULL must
    # be NULL, else inlining violated the contract
    violated = bpy(None) is not None

    expected = [[x, None if x is None else bpy(x)] for x in xs]
    # assertion: function name gone from plan = body inlined
    asserted = lambda _n, t: fname not in t

    return {
        "family": "inline", "setup": setup, "q": q,
        "asserted": asserted,
        "asserted_desc": "function body inlined (name absent from plan)",
        "violated": violated,
        "violation_desc": f"body({None}) evaluates to {bpy(None)}, "
                          "STRICT contract demands NULL",
        "expected": expected,
        "meta": {"body": body},
    }


def gen_iu(rng) -> dict:
    variant = rng.choice(["uniq", "partial_uniq", "expr_uniq",
                          "composite_uniq", "dup", "distinct_sub",
                          "join_removal", "jr_partial"])
    a_rows = [(i + 1, rng.choice([1, 2, 3])) for i in range(rng.randint(3, 6))]
    nb = rng.randint(3, 7)
    if variant in ("uniq", "expr_uniq", "join_removal"):
        # a real unique index over all of b.k: keep keys duplicate-free
        # or CREATE INDEX fails and the case goes not_fired
        bks = rng.sample([1, 2, 3, 4, 5, 6], min(nb, 6))
    else:
        # duplicates legal in the unconstrained space — the violation
        # surface for partial/composite proofs
        bks = [rng.choice([1, 2, 3]) for _ in range(nb)]
    b_rows = [(k, rng.choice([10, 20, None])) for k in bks]

    def lit(v):
        return "NULL" if v is None else str(v)
    setup = [
        "CREATE TABLE a(k int, j int)",
        "INSERT INTO a VALUES " + ",".join(f"({k},{j})" for k, j in a_rows),
        "CREATE TABLE b(k int, x int)",
        "INSERT INTO b VALUES " + ",".join(f"({k},{lit(x)})" for k, x in b_rows),
    ]
    if variant == "uniq":
        setup.append("CREATE UNIQUE INDEX buk ON b(k)")
    elif variant == "partial_uniq":
        setup.append("CREATE UNIQUE INDEX bpuk ON b(k) WHERE k > 1")
    elif variant == "expr_uniq":
        setup.append("CREATE UNIQUE INDEX beuk ON b((k + 0))")
    elif variant == "composite_uniq":
        setup.append("CREATE UNIQUE INDEX bcuk ON b(k, x)")
    elif variant in ("join_removal", "jr_partial"):
        setup.append("CREATE UNIQUE INDEX bpuk ON b(k) WHERE k > 1"
                     if variant == "jr_partial"
                     else "CREATE UNIQUE INDEX buk ON b(k)")

    # the claim 'at most one inner match per outer row' is over the
    # WHOLE inner rel — a partial unique index cannot discharge rows
    # outside its predicate
    all_ks = [b[0] for b in b_rows]
    has_dups = len(all_ks) != len(set(all_ks))

    if variant == "distinct_sub":
        q = ("SELECT a.k, d.k FROM a JOIN (SELECT DISTINCT k FROM b) d "
             "ON a.j = d.k")
        inner_rows = sorted({b[0] for b in b_rows})
        violated = False  # DISTINCT output is unique by construction
        expected = [[a_k, k] for a_k, a_j in a_rows
                    for k in inner_rows if k == a_j]
        asserted = lambda n, _t: inner_unique_present(n)
        desc = "Inner Unique via DISTINCT"
    elif variant in ("join_removal", "jr_partial"):
        q = ("SELECT count(*) FROM a LEFT JOIN b ON a.j = b.k")
        asserted = lambda n, _t: not rel_scanned(n, "b")
        desc = "join removed (b not scanned)"
        violated = has_dups
        cnt = sum(max(1, sum(1 for b in b_rows if b[0] == a_j))
                  for _k, a_j in a_rows)
        expected = [[cnt]]
    else:
        q = ("SELECT a.k, b.x FROM a JOIN b ON a.j = b.k")
        violated = has_dups
        expected = [[a_k, b[1]] for a_k, a_j in a_rows
                    for b in b_rows if b[0] == a_j]
        asserted = lambda n, _t: inner_unique_present(n)
        desc = "Inner Unique asserted"

    return {
        "family": "iu", "setup": setup, "q": q,
        "asserted": asserted,
        "asserted_desc": desc,
        "violated": violated,
        "violation_desc": "inner join key has duplicates "
                          f"(variant={variant})",
        "expected": expected,
        "meta": {"variant": variant},
    }


def gen_part(rng) -> dict:
    setup = [
        "CREATE TABLE p(x int) PARTITION BY RANGE (x)",
        "CREATE TABLE p1 PARTITION OF p FOR VALUES FROM (0) TO (100)",
        "CREATE TABLE p2 PARTITION OF p FOR VALUES FROM (100) TO (200)",
        "CREATE TABLE p3 PARTITION OF p FOR VALUES FROM (200) TO (300)",
    ]
    rows_p1 = _vals(rng, rng.randint(1, 4), pool=(0, 1, 50, 99))
    rows_p2 = _vals(rng, rng.randint(1, 4), pool=(100, 101, 150, 199))
    rows_p3 = _vals(rng, rng.randint(1, 4), pool=(200, 250, 299))
    all_rows = rows_p1 + rows_p2 + rows_p3
    setup.append("INSERT INTO p VALUES " +
                 ",".join(f"({v})" for v in all_rows))

    if rng.random() < 0.35:
        # join-time pruning: EC propagates 'q.k <op> const' onto p.x,
        # then the partition selector runs on the propagated qual
        setup += ["CREATE TABLE q(k int, y int)",
                  "INSERT INTO q VALUES (50,1),(150,1),(250,1)"]
        op, k = rng.choice([("=", 150), ("=", 50), ("=", 250),
                            ("<", 100), ("<=", 99), (">=", 200),
                            ("=", 150.5)])
        q = (f"SELECT p.x FROM p JOIN q ON p.x = q.k "
             f"WHERE q.k {op} {k}")
        qpy = lambda x: cmp3(x, op, k)
        child_rows = {"p1": rows_p1, "p2": rows_p2, "p3": rows_p3}
        qks = [50, 150, 250]
        expected = []
        for x in all_rows:
            if qpy(x) is True:
                expected += [[x]] * sum(1 for qk in qks if qk == x)
        return {
            "family": "part", "setup": setup, "q": q,
            "asserted": "NEEDS_PLAN",
            "asserted_desc": f"join-propagated qual p.x {op} {k} "
                             "pruned a child",
            "violated_fn": lambda scanned: any(
                qpy(x) is True
                for child, xs in child_rows.items()
                if child not in scanned for x in xs),
            "violated": None,
            "expected": sorted(expected),
            "meta": {"qual": f"q.k {op} {k}", "variant": "join"},
        }

    quals = [
        ("x < 100", lambda x: cmp3(x, "<", 100), {"p2", "p3"}),
        ("x >= 200", lambda x: cmp3(x, ">=", 200), {"p1", "p2"}),
        ("x = 150", lambda x: cmp3(x, "=", 150), {"p1", "p3"}),
        ("x = ANY (ARRAY[50,150])",
         lambda x: x in (50, 150) if x is not None else None,
         {"p3"}),
        ("x IS NOT NULL", lambda x: x is not None, set()),
        ("x < 100 OR x >= 200",
         lambda x: tvl_or(cmp3(x, "<", 100), cmp3(x, ">=", 200)), {"p2"}),
        ("x <> 150", lambda x: cmp3(x, "<>", 150), set()),
        ("x < 100.5", lambda x: cmp3(x, "<", 100.5), {"p2", "p3"}),
        ("x + 0 < 100", lambda x: cmp3(x, "<", 100), set()),
    ]
    qsql, qpy, _ = quals[rng.randrange(len(quals))]
    q = f"SELECT x FROM p WHERE {qsql}"
    child_rows = {"p1": rows_p1, "p2": rows_p2, "p3": rows_p3}
    expected = sorted([[x] for x in all_rows if qpy(x) is True])

    return {
        "family": "part", "setup": setup, "q": q,
        "asserted": "NEEDS_PLAN",   # filled by driver via partition logic
        "asserted_desc": "some child partition pruned",
        "violated_fn": lambda scanned: any(
            qpy(x) is True
            for child, xs in child_rows.items()
            if child not in scanned for x in xs),
        "violated": None,
        "expected": expected,
        "meta": {"qual": qsql},
    }


_COL_MODS = {"two": 2, "ten": 10, "twenty": 20, "hundred": 100,
             "unique1": 10000}


def gen_memoize(rng) -> dict:
    """Shared-parameter Memoize cache-key family (the live BUG #17213-
    extension shape).  The inner aggregate is parameterised by outer
    columns; the belief is 'the Cache Key tuple determines the inner
    result' — violated when two outer rows share the key columns but
    have different true counts.
    """
    A = rng.choice(["two", "ten", "twenty", "hundred"])
    B = rng.choice(["two", "ten", "twenty", "hundred"])
    C = rng.choice(["two", "ten", "twenty", "hundred"])
    T2 = rng.choice(["two", "ten", "twenty", "hundred"])
    setup = [
        "CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, "
        "g%10 AS ten, g%20 AS twenty, g%100 AS hundred "
        "FROM generate_series(0,9999) g",
        "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
        "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
        "ANALYZE tenk1",
        "SET enable_seqscan = off",
        "SET enable_mergejoin = off",
        "SET work_mem = '64kB'",
    ]
    q = (f"SELECT sum(c)::bigint FROM (SELECT t0.unique1, "
         f"(SELECT count(*) FROM tenk1 t2 JOIN tenk1 t1 ON "
         f"t1.unique1 = t2.{T2} + t0.{A} "
         f"WHERE t1.{B} = t0.{C}) AS c "
         f"FROM tenk1 t0 WHERE t0.unique1 < 200) s")
    Amod, Bmod, Cmod, Hmod = (_COL_MODS[A], _COL_MODS[B],
                            _COL_MODS[C], _COL_MODS[T2])
    hmult = 10000 // Hmod
    total = 0
    for u in range(200):
        a, cval = u % Amod, u % Cmod
        cnt = 0
        if cval < Bmod:  # else no t1.B can equal the t0.C value
            r = (cval - a) % Bmod
            h = r
            while h < Hmod:
                if h + a <= 9999:   # matching t1.unique1 must exist
                    cnt += hmult
                h += Bmod
        total += cnt

    # The bug precondition (BUG #17213 extension, Brazeal): the SAME
    # outer column feeds the join-expr Param and a second qual Param.
    # Param identity (not column identity) is what the cache key
    # captures; when A == C the planner shares one Param slot and the
    # cache key silently drops the second use -> stale entries collide
    # on the expr value across different t2 rows.  Observed: every
    # A == C instance is wrong on every build; A != C is always clean
    # (two distinct Params are both keyed).
    violated = (A == C)
    vdesc = (f"shared outer column t0.{A} feeds both the join-expr "
             f"param and the '{B} = t0.{C}' qual param -> cache key "
             f"cannot distinguish the two uses" if violated else
             f"distinct param columns (t0.{A} vs t0.{C})")

    return {
        "family": "memoize", "setup": setup, "q": q,
        "teardown": ["RESET enable_seqscan", "RESET enable_mergejoin",
                     "RESET work_mem"],
        "asserted": None,   # handled in driver (needs plan nodes)
        "asserted_desc": "Memoize node in plan",
        "violated": violated,
        "violation_desc": vdesc,
        "expected": [[total]],
        "meta": {"A": A, "B": B, "C": C, "T2": T2},
    }


_OJ2_QUALS: list[tuple[str, Callable[[dict | None], Any]]] = [
    # (sql over alias c, py over crow|None)
    ("c.y IS NOT NULL", lambda c: c is not None and c["y"] is not None),
    ("c.y IS NOT DISTINCT FROM NULL",
     lambda c: c is None or c["y"] is None),
    ("c.y > 0", lambda c: None if c is None or c["y"] is None
     else c["y"] > 0),
    ("COALESCE(c.y, -1) > -2",
     lambda c: (c["y"] if c and c["y"] is not None else -1) > -2),
    ("c.y IS NULL OR c.y > 5",
     lambda c: (c is None or c["y"] is None) or c["y"] > 5),
]


def gen_ojr2(rng) -> dict:
    """Nested outer-join chain: a LJ b LJ c WHERE <qual on c>.

    Second-level nullingrels: the c-qual must be strict on the
    c-nullable side of the second join AND the composite (b,c) side
    fed by the first join's null-extension.  A reduction is unsound
    whenever the qual can be TRUE on a null-extended c row.
    """
    na = rng.randint(2, 4)
    aks = list(range(1, na + 1))
    a_rows = [(k,) for k in aks]
    b_rows = [(rng.choice(aks + [na + 1]), rng.choice([1, 2, 3]))
              for _ in range(rng.randint(1, 4))]
    c_rows = [(rng.choice([1, 2, 3]), rng.choice([0, 1, None, 5]))
              for _ in range(rng.randint(1, 4))]
    qsql, qpy = _OJ2_QUALS[rng.randrange(len(_OJ2_QUALS))]
    q = ("SELECT a.k, b.x, c.y FROM a LEFT JOIN b ON a.k = b.k "
         "LEFT JOIN c ON b.x = c.k WHERE " + qsql)

    def lit(v):
        return "NULL" if v is None else str(v)
    setup = [
        "CREATE TABLE a(k int)",
        "INSERT INTO a VALUES " + ",".join(f"({k})" for k, in a_rows),
        "CREATE TABLE b(k int, x int)",
        "INSERT INTO b VALUES " +
        ",".join(f"({k},{x})" for k, x in b_rows),
        "CREATE TABLE c(k int, y int)",
        "INSERT INTO c VALUES " +
        ",".join(f"({k},{lit(y)})" for k, y in c_rows),
    ]

    # python truth: per a-row, per b-match (or null), per c-match (or
    # null).  An unmatched b makes b.x NULL -> c can never match.
    expected = []
    violated = False
    for (ak,) in a_rows:
        bs = [b for b in b_rows if b[0] == ak] or [None]
        for b in bs:
            if b is None:
                cs = [None]
            else:
                cs = [c for c in c_rows if c[0] == b[1]] or [None]
            for c in cs:
                cd = None if c is None else {"k": c[0], "y": c[1]}
                if qpy(cd) is True:
                    expected.append([ak, b[1] if b else None,
                                     c[1] if c else None])
                if c is None and qpy(None) is True:
                    violated = True   # qual TRUE on null-extended c row
    # assertion: any declared one-sided join reduced to a two-sided one
    def asserted(nodes, _t):
        planned = [n.get("Join Type") for n in nodes
                   if "Join Type" in n]
        one_sided = sum(1 for j in planned if j in ("Left", "Right"))
        return one_sided < 2   # at least one outer join reduced/removed

    return {
        "family": "ojr2", "setup": setup, "q": q,
        "asserted": asserted,
        "asserted_desc": "one of the two LEFT JOINs reduced to inner",
        "violated": violated,
        "violation_desc": f"qual TRUE on a null-extended c row ({qsql})",
        "expected": expected,
        "meta": {"qual": qsql},
    }


GENERATORS = [gen_runcond, gen_ojr, gen_ojr2, gen_lat, gen_inline,
              gen_iu, gen_part, gen_memoize]


# ============================ driver ============================

def run_case(pg: PostgresRunner, case: dict, timeout: float) -> dict:
    pg.setup(case["setup"])
    r = pg.run(f"EXPLAIN (FORMAT JSON, VERBOSE) {case['q']}",
               timeout_s=timeout)
    pjson = None
    if r.ok and r.rows:
        payload = r.rows[0][0]
        pjson = json.loads(payload) if isinstance(payload, str) else payload
    ptxt_r = pg.run(f"EXPLAIN (VERBOSE, COSTS OFF) {case['q']}",
                    timeout_s=timeout)
    ptxt = ("\n".join(str(x[0]) for x in ptxt_r.rows)
            if ptxt_r.ok else "")
    nodes = nodes_of(pjson)

    audit_state = "ok"
    vdesc = case.get("violation_desc", "")
    if case["family"] == "part":
        scanned = _scanned_relations(nodes)
        children = {"p1", "p2", "p3"}
        fired = bool(children & scanned) and bool(children - scanned)
        asserted = fired
        violated = (case["violated_fn"](scanned & children)
                    if fired else False)
        evidence = f"scanned={sorted(scanned & children)}"
    elif case["family"] == "runcond":
        rcs = [n["Run Condition"] for n in nodes
               if "Run Condition" in n]
        asserted = bool(rcs)
        evidence = f"Run Condition {rcs}"
        violated = False
        if asserted:
            proc_cs, qpy = case["rc_data"]
            for rc in rcs:
                rpy = _rc_pred(rc)
                if rpy is None:
                    audit_state = "no_audit"
                    vdesc = f"unparseable run condition {rc!r}"
                    break
                v, vdesc = rc_violated(proc_cs, rpy, qpy)
                if v:
                    violated = True
                    break
    elif case["family"] == "memoize":
        memos = [n for n in nodes if n.get("Node Type") == "Memoize"]
        asserted = bool(memos)
        evidence = (f"Memoize Cache Key "
                    f"{[m.get('Cache Key') for m in memos]}")
        violated = case["violated"]
        vdesc = case.get("violation_desc", "")
    else:
        asserted = case["asserted"](nodes, ptxt)
        violated = case["violated"]
        evidence = case["asserted_desc"]

    res = pg.run(case["q"], timeout_s=timeout)
    for t in case.get("teardown", []):
        pg.run(t, timeout_s=timeout)
    if not res.ok:
        outcome = {"outcome": "error", "error": res.error}
    else:
        got = Counter(repr(tuple(x)) for x in res.rows)
        want = Counter(repr(tuple(x)) for x in (case["expected"] or []))
        outcome = {"outcome": "ok" if got == want else "wrong_result",
                   "rows": res.rows[:10], "expected": case["expected"][:10]}
    wrong = outcome["outcome"] == "wrong_result"

    verdict = ("not_fired" if not asserted
               else "asserted_no_audit" if audit_state != "ok"
               else "false_belief" if violated
               else "fired_clean")
    return {"family": case["family"], "q": case["q"], "setup": case["setup"],
            "meta": case.get("meta", {}), "asserted": bool(asserted),
            "evidence": evidence, "violated": bool(violated),
            "violation_desc": vdesc,
            "verdict": verdict, "wrong_result": wrong,
            "result": outcome}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--cases", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--families", default=None,
                    help="comma subset of runcond,ojr,lat,inline,iu,part")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    gens = GENERATORS
    if args.families:
        wanted = set(args.families.split(","))
        gens = [g for g in GENERATORS
                if g.__name__.replace("gen_", "") in wanted]

    tag = Path(args.prefix).name
    pg = PostgresRunner(out / f"data_{tag}", pg_prefix=args.prefix)
    print(f"== {tag} (server {pg.engine_version}), "
          f"{args.cases} cases seed={args.seed}")

    rng = random.Random(args.seed)
    stats = Counter()
    hits = []
    for i in range(args.cases):
        g = gens[rng.randrange(len(gens))]
        try:
            case = g(rng)
        except Exception as exc:  # noqa: BLE001
            stats["gen_error"] += 1
            continue
        try:
            res = run_case(pg, case, args.timeout)
        except Exception as exc:  # noqa: BLE001
            stats["run_error"] += 1
            res = {"family": case["family"], "q": case["q"],
                   "verdict": "run_error", "error": str(exc)}
        stats[f"{res['family']}/{res['verdict']}"] += 1
        if res["verdict"] == "false_belief" or res.get("wrong_result"):
            hits.append(res)
            print(f"   !! {res['verdict']}"
                  f"{'+WRONG' if res.get('wrong_result') else ''} "
                  f"{res['family']} {res.get('meta')} | "
                  f"{res['q'][:90]}", flush=True)
        if (i + 1) % 50 == 0:
            print(f"   ... {i + 1}/{args.cases}", flush=True)

    (out / "belief_gen.json").write_text(json.dumps(
        {"seed": args.seed, "prefix": tag, "stats": dict(stats),
         "hits": hits}, indent=2, default=str))
    print("\n".join(f"   {k:<34} {v}" for k, v in sorted(stats.items())))
    print(f"\n-> {out}/belief_gen.json")
    pg.cleanup()


if __name__ == "__main__":
    main()
