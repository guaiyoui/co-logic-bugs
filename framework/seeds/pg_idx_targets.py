"""Index-predicate-dense seeds for the PostgreSQL index-presence oracle.

Each case shapes a table whose columns are *meant* to be indexed —
range filters, IN lists, NULL-heavy columns, array containment, LIKE
prefixes — so the access-path code (btree desc/asc, bitmap AND/OR,
partial-index matching, GIN pending lists, index-only scans) is what
actually executes the query. Tables carry enough rows (~2-5k) that
index plans are cost-competitive even before seqscan is disabled.
"""
from __future__ import annotations

import random


def _rows(n: int, rng: random.Random) -> str:
    vals = []
    for i in range(n):
        a = i if rng.random() > 0.1 else "NULL"
        b = rng.choice([0, 1, 2, 3, 7, 42, -1, -99])
        c = f"'{rng.choice(['alpha', 'beta', 'gamma', '', 'x' * 50])}'"
        d = rng.choice(["NULL", f"'{2020 + i % 5}-0{1 + i % 9}-0{1 + i % 9}'"])
        vals.append(f"({a},{b},{c},{d})")
    return ", ".join(vals)


def _arr_rows(n: int, rng: random.Random) -> str:
    vals = []
    for i in range(n):
        arr = "{" + ",".join(str(rng.randint(0, 20))
                           for _ in range(rng.randint(0, 4))) + "}"
        t = rng.choice(["red", "green", "blue", "alpha"])
        vals.append(f"('{arr}','{t}')")
    return ", ".join(vals)


def as_seeds() -> list[dict]:
    rng = random.Random(20260915)
    seeds: list[dict] = []

    base_setup = [
        "CREATE TABLE r(a INT, b INT, c TEXT, d DATE)",
        f"INSERT INTO r VALUES {_rows(3000, rng)}",
    ]
    queries = [
        ("range_and", "SELECT a, b FROM r WHERE a > 100 AND a < 5000 "
                      "AND b = 7 ORDER BY a"),
        ("in_list", "SELECT b, count(*) FROM r WHERE b IN (0,3,42) "
                    "GROUP BY b ORDER BY b"),
        ("or_pred", "SELECT a FROM r WHERE a = 5 OR a = 999 OR a = 2000 "
                    "ORDER BY a"),
        ("null_heavy", "SELECT count(*), sum(b) FROM r WHERE a IS NULL"),
        ("text_prefix", "SELECT c FROM r WHERE c LIKE 'alp%' ORDER BY c"),
        ("min_max", "SELECT min(a), max(a) FROM r WHERE b > 2"),
        ("range_order_limit", "SELECT a, d FROM r WHERE d > '2021-01-01' "
                              "ORDER BY d DESC LIMIT 50"),
        ("self_join", "SELECT r1.a, r2.b FROM r r1 JOIN r r2 "
                      "ON r1.a = r2.a WHERE r1.b = 3 ORDER BY r1.a "
                      "LIMIT 100"),
        ("distinct_b", "SELECT DISTINCT b FROM r ORDER BY b"),
        ("count_btree", "SELECT count(*) FROM r WHERE a BETWEEN 500 "
                        "AND 1500"),
        ("exists_subq", "SELECT a FROM r x WHERE EXISTS (SELECT 1 FROM r y "
                        "WHERE y.b = x.b AND y.a < 10) ORDER BY a LIMIT 50"),
        ("not_in", "SELECT b FROM r WHERE b NOT IN (SELECT a FROM r "
                   "WHERE a < 0) GROUP BY b ORDER BY b"),
    ]
    for name, q in queries:
        seeds.append({"source": f"pg_idx:{name}", "setup_sqls": base_setup,
                      "query": q, "category": "index_presence"})

    arr_setup = [
        "CREATE TABLE ar(t INT[], tag TEXT)",
        f"INSERT INTO ar VALUES {_arr_rows(2000, rng)}",
    ]
    arr_queries = [
        ("gin_contains", "SELECT tag, count(*) FROM ar WHERE t @> '{7}' "
                         "GROUP BY tag ORDER BY tag"),
        ("gin_overlap", "SELECT count(*) FROM ar WHERE t && '{3,8,15}'"),
        ("gin_contained", "SELECT count(*) FROM ar "
                         "WHERE t <@ '{1,2,3,4,5,6,7,8,9,10}'"),
        ("gin_tag", "SELECT tag FROM ar WHERE tag = 'red' AND t @> '{1}' "
                    "ORDER BY tag LIMIT 20"),
    ]
    for name, q in arr_queries:
        seeds.append({"source": f"pg_idx:{name}", "setup_sqls": arr_setup,
                      "query": q, "category": "index_presence"})

    # HOT-chain + index-only-scan stress: covering index, then updates
    # that keep the indexed column stable so chains stay heap-only.
    hot_setup = [
        "CREATE TABLE h(id INT PRIMARY KEY, payload INT, tag TEXT)",
        "INSERT INTO h SELECT i, i % 100, 't' || (i % 50) "
        "FROM generate_series(1, 4000) i",
    ]
    hot_queries = [
        ("hot_ios_count", "SELECT count(*) FROM h WHERE id BETWEEN 100 "
                          "AND 500"),
        ("hot_ios_fetch", "SELECT id, payload FROM h WHERE id >= 3900 "
                          "ORDER BY id"),
        ("hot_point", "SELECT payload FROM h WHERE id = 777"),
    ]
    for name, q in hot_queries:
        seeds.append({"source": f"pg_idx:{name}", "setup_sqls": hot_setup,
                      "query": q, "category": "index_presence"})

    return seeds
