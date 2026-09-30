"""Round-2 SQLite seed generator — combinatorial shapes for the
plan-invariance oracle and the RIGHT/FULL JOIN matrix.

as_seeds() -> list of {"source", "setup_sqls", "query"}
"""
from __future__ import annotations

import itertools


def _s(src, setup, query):
    return {"source": src, "setup_sqls": setup, "query": query}


# ---------- schema variants rich in NULLs / affinity mixes ----------

def gen_index_oracle():
    S = []
    # schema A: ints with NULLs
    a_int = [
        "CREATE TABLE t(x INT, y INT, z TEXT)",
        "INSERT INTO t VALUES (1,10,'a'),(2,20,'b'),(3,30,'c'),"
        "(NULL,40,'d'),(2,50,'e'),(0,NULL,'f'),(-1,70,'g'),"
        "(5,80,'h'),(NULL,NULL,'i'),(3,100,'j')",
        "CREATE TABLE u(x INT, w INT)",
        "INSERT INTO u VALUES (2,7),(3,8),(NULL,9),(0,10),(5,11),(-2,12)",
    ]
    # schema B: text with case/collation edges
    b_txt = [
        "CREATE TABLE s(v TEXT, n INT)",
        "INSERT INTO s VALUES ('a',1),('B',2),('c',3),('A',4),"
        "('apple',5),('Apple',6),('APPLE',7),('apples',8),"
        "('app',9),(NULL,10),('',11),('aPP',12),('10',13),('2',14)",
    ]
    # schema C: mixed affinity (deliberate '2' vs 2)
    c_mix = [
        "CREATE TABLE m(a, b INT, c TEXT)",
        "INSERT INTO m VALUES ('2',1,'x'),(2,2,'y'),('10',3,'z'),"
        "(10,4,'w'),(2.0,5,'v'),('2.0',6,'u'),(NULL,7,'t'),"
        "(' 2',8,'s'),('02',9,'r'),(20,10,'q')",
    ]

    preds_a = [
        "x > 1", "x >= 2", "x BETWEEN 0 AND 3", "x IS NULL",
        "x IS NOT NULL", "x IN (2,3)", "x NOT IN (2,3)",
        "y > 15", "x = 2 AND y > 15", "x = 2 OR y > 60",
        "x IS NULL OR y > 60", "(x = 2 OR x = 3) AND y < 90",
        "x IN (SELECT x FROM u)", "x NOT IN (SELECT x FROM u WHERE x IS NOT NULL)",
        "x IN (SELECT x FROM u WHERE w > 8)",
        "EXISTS (SELECT 1 FROM u WHERE u.x = t.x)",
        "NOT EXISTS (SELECT 1 FROM u WHERE u.x = t.x AND u.w > 8)",
        "z > 'b'", "z IS NOT NULL", "z LIKE '_'",
        "x+0 = 2", "abs(x) = 2", "coalesce(x,-5) = -5",
        "x IN (2,3,NULL)", "x = 2 OR x IS NULL",
        "NOT (x = 2)", "x <> 2", "x IS NOT 2", "x IS 2",
        "CASE WHEN y > 15 THEN x ELSE -x END > 0",
    ]
    for i, p in enumerate(preds_a):
        for extra in ("", " ORDER BY 1,2", " ORDER BY 1,2 LIMIT 6",
                      " LIMIT 5"):
            S.append(_s(f"idxA:p{i}{extra[:6]}",
                        a_int,
                        f"SELECT x, y FROM t WHERE {p}{extra}"))
        S.append(_s(f"idxA:cnt{i}", a_int,
                    f"SELECT count(*), count(x), count(DISTINCT x) "
                    f"FROM t WHERE {p}"))
        S.append(_s(f"idxA:mm{i}", a_int,
                    f"SELECT min(x), max(x), min(y), max(y) FROM t WHERE {p}"))
        S.append(_s(f"idxA:grp{i}", a_int,
                    f"SELECT x, count(*), min(y) FROM t WHERE {p} "
                    f"GROUP BY x ORDER BY 1"))

    preds_b = [
        "v > 'a'", "v >= 'A'", "v LIKE 'a%'", "v LIKE 'A%'",
        "v GLOB 'a*'", "v IN ('a','B','apple')",
        "v > 'a' COLLATE NOCASE", "v LIKE 'a%' COLLATE NOCASE",
        "v = 'APPLE' COLLATE NOCASE", "upper(v) = 'APPLE'",
        "v BETWEEN 'a' AND 'c'", "v BETWEEN 'a' AND 'c' COLLATE NOCASE",
        "length(v) > 1", "v IS NULL", "v < 'B'",
        "v LIKE 'app%' AND n > 5",
        "v GLOB 'app*'", "v GLOB 'APP*' COLLATE NOCASE",
        "v IN (SELECT v FROM s WHERE n > 100)",
        "v > 'a' OR n > 10", "substr(v,1,3) = 'app'",
        "v = 'a' OR v = 'B' OR v = 'c'",
    ]
    for i, p in enumerate(preds_b):
        for extra in ("", " ORDER BY 1,2", " ORDER BY 1 LIMIT 8"):
            S.append(_s(f"idxB:p{i}{extra[:6]}", b_txt,
                        f"SELECT v, n FROM s WHERE {p}{extra}"))
        S.append(_s(f"idxB:mm{i}", b_txt,
                    f"SELECT min(v), max(v) FROM s WHERE {p}"))
        S.append(_s(f"idxB:mmn{i}", b_txt,
                    f"SELECT min(v COLLATE NOCASE), max(v COLLATE NOCASE) "
                    f"FROM s WHERE {p}"))

    preds_c = [
        "a = 2", "a = '2'", "a IN (2,'2',10)", "b = 2", "c = 'x'",
        "a > 2", "a > '2'", "b IN (1,2,5)", "a IN (SELECT a FROM m)",
        "a IS 2", "a IS '2'", "a BETWEEN 1 AND 5", "a BETWEEN '1' AND '5'",
        "typeof(a) = 'integer'", "a+0 = 2", "CAST(a AS INT) = 2",
        "a LIKE '2%'", "a = 2 AND b < 9",
    ]
    for i, p in enumerate(preds_c):
        S.append(_s(f"idxC:p{i}", c_mix,
                    f"SELECT a, b, c FROM m WHERE {p} ORDER BY 2,1"))
        S.append(_s(f"idxC:j{i}", c_mix,
                    f"SELECT m1.c, m2.c FROM m m1 JOIN m m2 ON m1.a=m2.a "
                    f"WHERE {p.replace('a ', 'm1.a ')} ORDER BY 1,2"))

    # join / aggregate shapes over schema A + u
    joins = [
        ("SELECT t.x, u.w FROM t JOIN u ON t.x=u.x ORDER BY 1,2"),
        ("SELECT t.x, u.w FROM t LEFT JOIN u ON t.x=u.x ORDER BY 1,2"),
        ("SELECT t.x, u.w FROM t LEFT JOIN u ON t.x=u.x AND u.w>8 "
         "ORDER BY 1,2"),
        ("SELECT t.x, u.w FROM t JOIN u ON t.x=u.x OR t.y=u.w ORDER BY 1,2"),
        ("SELECT t.x, u.w FROM t LEFT JOIN u ON t.x=u.x WHERE u.w IS NULL "
         "OR u.w>10 ORDER BY 1,2"),
        ("SELECT t.x, count(u.w) FROM t LEFT JOIN u ON t.x=u.x "
         "GROUP BY t.x ORDER BY 1"),
        ("SELECT x, count(*) FROM u GROUP BY x HAVING count(*)>=1 "
         "ORDER BY 1"),
        ("SELECT DISTINCT t.x FROM t JOIN u ON t.x=u.x ORDER BY 1"),
        ("SELECT t.x FROM t JOIN u ON t.x=u.x GROUP BY t.x ORDER BY 1"),
        ("SELECT sum(w) FROM u WHERE x IN (SELECT x FROM t WHERE y>15)"),
        ("SELECT sum(y) FROM t WHERE x IN (SELECT x FROM u GROUP BY x)"),
        ("SELECT x, w FROM u WHERE w > (SELECT avg(w) FROM u) ORDER BY 1,2"),
        ("SELECT x, w FROM u ORDER BY x LIMIT 3 OFFSET 1"),
        ("SELECT x, w FROM u ORDER BY x DESC LIMIT 3"),
        ("SELECT x, w FROM u ORDER BY 1 NULLS FIRST, 2 DESC LIMIT 6"),
        ("SELECT x, (SELECT count(*) FROM t WHERE t.x=u.x) FROM u "
         "ORDER BY 1"),
        ("SELECT x FROM u UNION SELECT x FROM t ORDER BY 1"),
        ("SELECT x FROM u UNION ALL SELECT x FROM t ORDER BY 1"),
        ("SELECT x FROM u INTERSECT SELECT x FROM t ORDER BY 1"),
        ("SELECT x FROM u EXCEPT SELECT x FROM t ORDER BY 1"),
        ("SELECT x FROM u WHERE x IN (1,2,3) UNION SELECT x FROM t "
         "WHERE x IN (2,3,4) ORDER BY 1"),
    ]
    for i, q in enumerate(joins):
        S.append(_s(f"idxJ:j{i}", a_int, q))

    # partial-index implication probes: WHERE implies predicate
    impl = [
        ("x > 5", "x > 0"), ("x BETWEEN 3 AND 8", "x > 0"),
        ("x = 7", "x > 0"), ("x IN (1,2,3)", "x IS NOT NULL"),
        ("x = 2 AND y = 5", "x IS NOT NULL"),
        ("x > 0", "x IS NOT NULL"),
        ("y > 10 AND x > 2", "x > 0"),
        ("x IS NOT NULL AND y > 0", "x IS NOT NULL"),
        ("coalesce(x,0) > 0", "x IS NOT NULL"),
        ("x > 0 AND x < 100", "x BETWEEN -10 AND 1000"),
        ("x IN (1,2,3)", "x > -5"),
        ("x = NULL OR x = 1", "x IS NOT NULL"),  # degenerate
        ("z IS NOT NULL", "z IS NOT NULL"),
    ]
    for i, (qp, ip) in enumerate(impl):
        setup = a_int + [f"CREATE INDEX pxi ON t(x) WHERE {ip}"]
        S.append(_s(f"idxP:p{i}", setup,
                    f"SELECT x, y FROM t WHERE {qp} ORDER BY 1,2"))

    # DESC / ASC mixed indexes + min-max and ORDER BY
    descs = [
        "CREATE INDEX d1 ON t(x DESC)",
        "CREATE INDEX d2 ON t(x DESC, y ASC)",
        "CREATE INDEX d3 ON t(y DESC)",
        "CREATE INDEX d4 ON t(x DESC NULLS FIRST)",
        "CREATE INDEX d5 ON t(x ASC NULLS LAST)",
    ]
    for i, d in enumerate(descs):
        for q in (
            "SELECT min(x), max(x) FROM t",
            "SELECT min(y), max(y) FROM t WHERE x>0",
            "SELECT x, y FROM t ORDER BY x DESC, y LIMIT 6",
            "SELECT x, y FROM t ORDER BY x DESC NULLS LAST, y DESC LIMIT 6",
            "SELECT x, y FROM t ORDER BY x LIMIT 6",
            "SELECT min(x) FROM t WHERE y IS NOT NULL",
        ):
            S.append(_s(f"idxD:d{i}", a_int + [d], q))

    # NOT INDEXED / INDEXED BY query-text variants
    for q, tag in (
        ("SELECT x, y FROM t NOT INDEXED WHERE x=2 ORDER BY 1,2", "ni1"),
        ("SELECT x, y FROM t NOT INDEXED WHERE x>1 ORDER BY 1,2", "ni2"),
        ("SELECT x, y FROM t INDEXED BY ai1 WHERE x=2 ORDER BY 1,2", "ib1"),
        ("SELECT x, y FROM t INDEXED BY ai1 WHERE y>15 ORDER BY 1,2", "ib2"),
        ("SELECT a.x, b.x FROM t a INDEXED BY ai1 JOIN t b NOT INDEXED "
         "ON a.x=b.x ORDER BY 1,2", "ibj"),
    ):
        S.append(_s(f"idxN:{tag}", a_int + [
            "CREATE INDEX ai1 ON t(x)", "CREATE INDEX ai2 ON t(y)"], q))

    # likely()/unlikely()/likelihood() should never change results
    for i, p in enumerate(
            ["likely(x=2)", "unlikely(x=2)", "likelihood(x=2,0.5)",
             "likely(y>15) OR x=3", "unlikely(x IS NULL)"]):
        S.append(_s(f"idxL:l{i}", a_int,
                    f"SELECT x, y FROM t WHERE {p} ORDER BY 1,2"))

    # OR on two different indexed columns (multi-index OR)
    S.append(_s("idxO:or2col", a_int + [
        "CREATE INDEX oi1 ON t(x)", "CREATE INDEX oi2 ON t(y)"],
        "SELECT x, y FROM t WHERE x=2 OR y=70 ORDER BY 1,2"))
    S.append(_s("idxO:or3col", a_int + [
        "CREATE INDEX oi3 ON t(x)", "CREATE INDEX oi4 ON t(y)",
        "CREATE INDEX oi5 ON t(z)"],
        "SELECT x, y, z FROM t WHERE x=2 OR y=70 OR z='c' ORDER BY 1,2,3"))
    S.append(_s("idxO:or_in", a_int + [
        "CREATE INDEX oi6 ON t(x)"],
        "SELECT x FROM t WHERE x IN (SELECT x FROM u) OR x=3 ORDER BY 1"))
    return S


def gen_right_full_matrix():
    """Deep matrix around SQLITE-A's neighborhood."""
    S = []
    a = ["CREATE TABLE a(x INT, y INT)",
         "INSERT INTO a VALUES (1,10),(2,20),(3,30)"]
    b = ["CREATE TABLE b(x INT, z INT)",
         "INSERT INTO b VALUES (2,200),(4,400)"]
    c = ["CREATE TABLE c(x INT, w INT)",
         "INSERT INTO c VALUES (2,7),(5,9)"]
    abc = a + b + c
    jts = ["JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN",
           "INNER JOIN", "CROSS JOIN"]
    ons = ["ON a.x=b.x", "USING(x)", "ON true", "ON a.x=b.x AND a.y>5",
           "ON a.x<b.x", "ON a.x IS NOT b.x", "ON +a.x=+b.x"]
    for i, (jt, on) in enumerate(itertools.product(jts, ons)):
        if jt == "CROSS JOIN" and on != "ON true":
            continue
        if on == "ON true" and jt != "CROSS JOIN":
            on2 = "ON true"
        else:
            on2 = on
        q = f"SELECT count(*) FROM a {jt} b {on2}"
        S.append(_s(f"rjM:{i}", a + b, q))
        # SELECT * variant — the SQLITE-A shape family
        q2 = f"SELECT * FROM a {jt} b {on2} ORDER BY 1,2,3,4"
        S.append(_s(f"rjS:{i}", a + b, q2))
    # 3-table chains mixing directions
    chain_jts = ["JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"]
    i = 0
    for j1, j2 in itertools.product(chain_jts, chain_jts):
        for con1, con2 in (("ON a.x=b.x", "ON a.x=c.x"),
                           ("ON a.x=b.x", "ON b.x=c.x"),
                           ("USING(x)", "USING(x)"),
                           ("ON a.x=b.x", "USING(x)"),
                           ("USING(x)", "ON b.x=c.x")):
            q = (f"SELECT * FROM a {j1} b {con1} {j2} c {con2} "
                 f"ORDER BY 1,2,3,4,5")
            S.append(_s(f"rjC:{i}", abc, q))
            q3 = (f"SELECT count(*) FROM a {j1} b {con1} {j2} c {con2}")
            S.append(_s(f"rjCn:{i}", abc, q3))
            i += 1
    # RIGHT/FULL inside subquery / view / CTE
    inner_forms = [
        ("subq", "SELECT * FROM (SELECT a.x AS ax, b.x AS bx FROM a "
                 "RIGHT JOIN b ON a.x=b.x) ORDER BY 1,2"),
        ("subq2", "SELECT * FROM (SELECT * FROM a RIGHT JOIN b USING(x)) "
                  "ORDER BY 1,2,3"),
        ("view", None),
        ("cte", "WITH r AS (SELECT a.x AS ax, b.x AS bx, b.z FROM a "
                "FULL JOIN b ON a.x=b.x) SELECT * FROM r ORDER BY 1,2,3"),
        ("cte2", "WITH r AS (SELECT * FROM a RIGHT JOIN b USING(x)) "
                 "SELECT count(*) FROM r"),
        ("in_subq", "SELECT y FROM a WHERE x IN (SELECT x FROM a "
                    "RIGHT JOIN b ON a.x=b.x) ORDER BY 1"),
        ("exists", "SELECT y FROM a WHERE EXISTS (SELECT 1 FROM b "
                   "FULL JOIN c ON b.x=c.x WHERE a.y>0) ORDER BY 1"),
    ]
    for tag, q in inner_forms:
        if tag == "view":
            S.append(_s("rjN:view", a + b + [
                "CREATE VIEW rv AS SELECT * FROM a RIGHT JOIN b USING(x)"],
                "SELECT * FROM rv ORDER BY 1,2,3"))
            S.append(_s("rjN:view2", a + b + [
                "CREATE VIEW rv2 AS SELECT * FROM a FULL JOIN b "
                "ON a.x=b.x"],
                "SELECT * FROM rv2 ORDER BY 1,2,3,4"))
        else:
            S.append(_s(f"rjN:{tag}", abc, q))
    # USING + ON mixes; NATURAL mixes; alias collisions
    mixes = [
        ("SELECT * FROM a NATURAL JOIN b RIGHT JOIN c ON a.x=c.x "
         "ORDER BY 1,2,3,4,5", "nat_right"),
        ("SELECT * FROM a NATURAL LEFT JOIN b RIGHT JOIN c ON b.x=c.x "
         "ORDER BY 1,2,3,4,5", "natl_right"),
        ("SELECT * FROM a JOIN b USING(x) NATURAL RIGHT JOIN c "
         "ORDER BY 1,2,3,4,5", "using_natr"),
        ("SELECT * FROM a RIGHT JOIN b USING(x) JOIN c USING(x) "
         "ORDER BY 1,2,3,4,5", "r_using_using"),
        ("SELECT a.x, b.x, c.x FROM a FULL JOIN b USING(x) FULL JOIN c "
         "USING(x) ORDER BY 1,2,3", "full_using_alias"),
        ("SELECT * FROM a AS t1 JOIN a AS t2 USING(x) RIGHT JOIN b "
         "ON t1.x=b.x ORDER BY 1,2,3,4,5", "self_right"),
        ("SELECT * FROM (a JOIN b USING(x)) RIGHT JOIN c ON x=c.x "
         "ORDER BY 1,2,3,4,5", "paren_right"),
        ("SELECT * FROM a JOIN b USING(x) RIGHT JOIN c ON a.x=c.x "
         "ORDER BY 1,2,3,4,5", "sqliteA_core"),
        ("SELECT * FROM a RIGHT JOIN b USING(x) RIGHT JOIN c USING(x) "
         "ORDER BY 1,2,3,4,5", "right_right_using"),
        ("SELECT * FROM a FULL JOIN b USING(x) RIGHT JOIN c ON b.x=c.x "
         "ORDER BY 1,2,3,4,5", "full_right"),
        ("SELECT x, a.y, b.z, c.w FROM a FULL JOIN b USING(x) "
         "RIGHT JOIN c USING(x) ORDER BY 1,2,3,4", "merged_col_star"),
        ("SELECT a.*, b.*, c.* FROM a RIGHT JOIN b USING(x) JOIN c "
         "ON a.x=c.x ORDER BY 1,2,3,4,5", "tblstar"),
    ]
    for q, tag in mixes:
        S.append(_s(f"rjX:{tag}", abc, q))
    # USING(col) where col participates in expression index / generated col
    S.append(_s("rjG:expridx", a + b + [
        "CREATE INDEX eix ON b(x+0)"],
        "SELECT * FROM a RIGHT JOIN b USING(x) ORDER BY 1,2,3"))
    S.append(_s("rjG:gencol", [
        "CREATE TABLE g(x INT, y INT GENERATED ALWAYS AS (x*2) VIRTUAL)",
        "INSERT INTO g(x) VALUES (1),(2),(3)",
        "CREATE TABLE h(y INT, w INT)",
        "INSERT INTO h VALUES (2,7),(4,9)"],
        "SELECT * FROM g RIGHT JOIN h USING(y) ORDER BY 1,2,3"))
    S.append(_s("rjG:gencol2", [
        "CREATE TABLE g(x INT, y INT GENERATED ALWAYS AS (x*2) STORED)",
        "INSERT INTO g(x) VALUES (1),(2),(3)",
        "CREATE TABLE h(y INT, w INT)",
        "INSERT INTO h VALUES (2,7),(4,9)"],
        "SELECT * FROM g FULL JOIN h USING(y) ORDER BY 1,2,3"))
    # USING on columns of different affinity
    S.append(_s("rjG:affinity", [
        "CREATE TABLE t1(x INT, a TEXT)",
        "INSERT INTO t1 VALUES (2,'p'),('3','q')",
        "CREATE TABLE t2(x TEXT, b TEXT)",
        "INSERT INTO t2 VALUES ('2','r'),(4,'s')"],
        "SELECT * FROM t1 FULL JOIN t2 USING(x) ORDER BY 1,2,3"))
    # non-equality ON with RIGHT/FULL
    for i, on in enumerate(["ON a.x<b.x", "ON a.x<>b.x", "ON a.x BETWEEN b.x AND b.x+1",
                            "ON a.y>b.z", "ON a.x IS DISTINCT FROM b.x",
                            "ON NOT (a.x=b.x)", "ON coalesce(a.x,-1)<coalesce(b.x,9)"]):
        for jt in ("RIGHT", "FULL"):
            S.append(_s(f"rjO:{jt}{i}", a + b,
                        f"SELECT count(*) FROM a {jt} JOIN b {on}"))
            S.append(_s(f"rjOs:{jt}{i}", a + b,
                        f"SELECT * FROM a {jt} JOIN b {on} ORDER BY 1,2,3,4"))
    return S


def as_seeds():
    return gen_index_oracle() + gen_right_full_matrix()


if __name__ == "__main__":
    ss = as_seeds()
    print(len(ss), "seeds")
    from collections import Counter
    print(Counter(s["source"].split(":")[0] for s in ss))
