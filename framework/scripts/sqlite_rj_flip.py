"""RIGHT/FULL JOIN direction-flip metamorphic oracle for SQLite.

mirror(join-tree): swap operands of every join, flip LEFT<->RIGHT,
keep FULL/INNER/CROSS, ON/USING text identical (it still references
the same pair of tables — only their left/right roles exchange).

  a LEFT JOIN b ON p   ==   b RIGHT JOIN a ON p
  (a J1 b) J2 c        ==   c J2' (b J1' a)      -- parens required

count(*) form requires no projection rewrite.
"""
from __future__ import annotations

import itertools
import sqlite3
import sys

FLIP = {"LEFT": "RIGHT", "RIGHT": "LEFT", "FULL": "FULL",
        "INNER": "INNER", "CROSS": "CROSS", "JOIN": "JOIN"}

SETUP = (
    "CREATE TABLE a(x INT, y INT);"
    "INSERT INTO a VALUES (1,10),(2,20),(3,30),(NULL,40),(2,21);"
    "CREATE TABLE b(x INT, z INT);"
    "INSERT INTO b VALUES (2,200),(4,400),(NULL,500),(3,600);"
)
SETUP3 = SETUP + (
    "CREATE TABLE c(x INT, w INT);"
    "INSERT INTO c VALUES (2,7),(5,9),(NULL,11),(4,13);"
)


def run(setup, q):
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(setup + ";")
        rows = con.execute(q).fetchall()
        res = ("ok", sorted(map(repr, rows)))
    except Exception as e:
        res = ("err", f"{type(e).__name__}: {e}")
    finally:
        con.close()
    return res


def gen_pairs():
    pairs = []
    ons = ["ON a.x=b.x", "ON a.x<b.x", "ON a.x IS NOT b.x", "ON true",
           "ON a.y=b.z", "ON coalesce(a.x,-1)=coalesce(b.x,-1)",
           "ON a.x=b.x AND a.y>15", "ON a.x=b.x OR a.y=b.z",
           "ON a.x IS NOT DISTINCT FROM b.x",
           "ON a.x IS DISTINCT FROM b.x", "USING(x)"]
    for jt, on in itertools.product(
            ["LEFT", "RIGHT", "FULL", "INNER"], ons):
        j2 = FLIP[jt]
        q1 = f"SELECT count(*) FROM a {jt} JOIN b {on}"
        q2 = f"SELECT count(*) FROM b {j2} JOIN a {on}"
        pairs.append((f"cnt {jt} {on}", SETUP, q1, q2))
        p = "a.x, a.y, b.x, b.z"
        q1 = f"SELECT {p} FROM a {jt} JOIN b {on}"
        q2 = f"SELECT {p} FROM b {j2} JOIN a {on}"
        pairs.append((f"cols {jt} {on}", SETUP, q1, q2))
    return pairs


def gen_three_table_pairs():
    pairs = []
    i = 0
    jts = ["LEFT", "RIGHT", "FULL", "INNER"]
    conds = [("ON a.x=b.x", "ON b.x=c.x"),
             ("USING(x)", "USING(x)"),
             ("ON a.x=b.x", "USING(x)"),
             ("USING(x)", "ON b.x=c.x"),
             ("ON a.x=b.x AND a.y>15", "ON b.x=c.x AND c.w>0")]
    for (c1, c2), (j1, j2) in itertools.product(conds,
                                              itertools.product(jts, jts)):
        left = f"SELECT count(*) FROM a {j1} JOIN b {c1} {j2} JOIN c {c2}"
        right = (f"SELECT count(*) FROM c {FLIP[j2]} JOIN "
                 f"(b {FLIP[j1]} JOIN a {c1}) {c2}")
        pairs.append((f"3t:{i} {j1}/{j2} {c1}|{c2}", SETUP3, left, right))
        i += 1
    return pairs


def main():
    findings = []
    pairs = gen_pairs() + gen_three_table_pairs()
    for tag, setup, q1, q2 in pairs:
        r1 = run(setup, q1)
        r2 = run(setup, q2)
        if r1 != r2:
            findings.append((tag, q1, r1, q2, r2))
            print(f"[DIFF] {tag}\n   {q1}\n   -> {r1[0]} "
                  f"{r1[1] if r1[0] == 'err' else r1[1]}"
                  f"\n   {q2}\n   -> {r2[0]} "
                  f"{r2[1] if r2[0] == 'err' else r2[1]}")
    print(f"{len(findings)} divergent flip-pairs of {len(pairs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
