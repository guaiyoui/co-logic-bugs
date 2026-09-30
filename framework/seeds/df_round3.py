"""DataFusion round-3 seed templates (54.0.0).

New surfaces: recursive-CTE zero-column scans (DF-H), high-precision
decimal literal casts (DF-G), quantified ALL comparisons (DF-I),
lateral UNNEST-in-FROM (DF-J), plus decorrelation-position probes that
extend DF-E. All portable SQL where the reference engines support the
syntax; DF-specific syntax is confined to cases that still parse
elsewhere (UNNEST AS-col, WITH ORDINALITY).
"""
from __future__ import annotations

DF_R3_TEMPLATES: list[tuple[str, list[str], str]] = [
    # ---- DF-H: recursive CTE under zero-column scan ----------------------
    ("r3_rcte_count_star_all", [],
     "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT count(*) FROM t"),
    ("r3_rcte_count_star_union", [],
     "WITH RECURSIVE t AS (SELECT 1 AS n UNION SELECT n+1 FROM t WHERE n < 5) SELECT count(*) FROM t"),
    ("r3_rcte_const_proj", [],
     "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT 42 FROM t LIMIT 3"),
    ("r3_rcte_count_subq", [],
     "SELECT count(*) FROM (WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT n FROM t) s"),
    ("r3_rcte_count_join", [],
     "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT count(*) FROM t JOIN (SELECT 1 AS x) s ON true"),
    # control: same query WITH a projected column must work
    ("r3_rcte_count_col", [],
     "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT count(n) FROM t"),
    # ---- DF-G: high-precision decimal literal through f64 ----------------
    ("r3_dec_lit_cast", [],
     "SELECT CAST(123456789012345678901234567890.123 AS DECIMAL(38,3))"),
    ("r3_dec_lit_add", [],
     "SELECT CAST(123456789012345678901234567890.123 AS DECIMAL(38,3)) + CAST(0.001 AS DECIMAL(38,3))"),
    ("r3_dec_lit_17dig", [],
     "SELECT CAST(12345678901234567.891 AS DECIMAL(38,3))"),
    ("r3_dec_lit_scale", [],
     "SELECT CAST(0.123456789012345678901234567890 AS DECIMAL(38,36))"),
    ("r3_dec_lit_bigint", [],
     "SELECT CAST(123456789012345678901234567890 AS DECIMAL(38,0))"),
    # control: string-sourced decimal is exact
    ("r3_dec_str_cast", [],
     "SELECT CAST('123456789012345678901234567890.123' AS DECIMAL(38,3))"),
    # ---- DF-I: uncorrelated op ALL + unprojected column ------------------
    ("r3_all_gt", ["CREATE TABLE t(a INT, b INT)",
                   "INSERT INTO t VALUES (1,10),(2,20),(3,30),(NULL,40)",
                   "CREATE TABLE u(a INT, w INT)",
                   "INSERT INTO u VALUES (1,5),(2,7)"],
     "SELECT a FROM t WHERE b > ALL (SELECT w FROM u) ORDER BY a"),
    ("r3_all_lt", ["CREATE TABLE t(a INT, b INT)",
                   "INSERT INTO t VALUES (1,10),(2,20),(3,30),(NULL,40)",
                   "CREATE TABLE u(a INT, w INT)",
                   "INSERT INTO u VALUES (1,5),(2,7)"],
     "SELECT a FROM t WHERE b < ALL (SELECT w FROM u) ORDER BY a"),
    ("r3_all_ne", ["CREATE TABLE t(a INT, b INT)",
                   "INSERT INTO t VALUES (1,10),(2,20),(3,30),(NULL,40)",
                   "CREATE TABLE u(a INT, w INT)",
                   "INSERT INTO u VALUES (1,5),(2,7)"],
     "SELECT a FROM t WHERE b <> ALL (SELECT w FROM u) ORDER BY a"),
    ("r3_all_limit", ["CREATE TABLE t(a INT, b INT)",
                      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
                      "CREATE TABLE u(w INT)", "INSERT INTO u VALUES (5),(7)"],
     "SELECT a FROM t WHERE b > ALL (SELECT w FROM u LIMIT 1) ORDER BY a"),
    # control: projected compared column works
    ("r3_all_projected", ["CREATE TABLE t(a INT, b INT)",
                          "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
                          "CREATE TABLE u(w INT)", "INSERT INTO u VALUES (5),(7)"],
     "SELECT a, b FROM t WHERE b > ALL (SELECT w FROM u) ORDER BY a"),
    # control: ANY works
    ("r3_any_gt", ["CREATE TABLE t(a INT, b INT)",
                   "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
                   "CREATE TABLE u(w INT)", "INSERT INTO u VALUES (5),(7)"],
     "SELECT a FROM t WHERE b > ANY (SELECT w FROM u) ORDER BY a"),
    # ---- DF-J: lateral UNNEST in FROM ------------------------------------
    ("r3_unnest_lateral_alias", ["CREATE TABLE t(id INT, l INT[])",
                                "INSERT INTO t VALUES (1,[10,20]),(2,[30])"],
     "SELECT t.id, e.c FROM t, UNNEST(t.l) AS e(c) ORDER BY t.id, e.c"),
    ("r3_unnest_lateral_bare", ["CREATE TABLE t(id INT, l INT[])",
                               "INSERT INTO t VALUES (1,[10,20]),(2,[30])"],
     "SELECT id, e FROM t, UNNEST(t.l) AS e ORDER BY id, e"),
    ("r3_unnest_lateral_join", ["CREATE TABLE t(id INT, l INT[])",
                               "INSERT INTO t VALUES (1,[10,20]),(2,[30])"],
     "SELECT id, e FROM t LEFT JOIN UNNEST(t.l) AS e ON true ORDER BY id, e"),
    ("r3_unnest_ordinality", [],
     "SELECT * FROM UNNEST([10,20,30]) WITH ORDINALITY"),
    # control: projection-form unnest works in DF
    ("r3_unnest_projection", ["CREATE TABLE t(id INT, l INT[])",
                             "INSERT INTO t VALUES (1,[10,20]),(2,[30])"],
     "SELECT id, unnest(l) AS e FROM t ORDER BY id, e"),
    # ---- DF-E extensions: decorrelation positions ------------------------
    ("r3_corr_scalar_orderby", ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2)",
                                "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,5)"],
     "SELECT a FROM t ORDER BY (SELECT max(w) FROM u WHERE u.a = t.a) NULLS LAST"),
    ("r3_corr_exists_orderby", ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2)",
                                "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (1)"],
     "SELECT a FROM t ORDER BY EXISTS (SELECT 1 FROM u WHERE u.a = t.a) DESC, a"),
    ("r3_exists_select_list", ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2)",
                               "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (1)"],
     "SELECT a, EXISTS (SELECT 1 FROM u WHERE u.a = t.a) FROM t ORDER BY a"),
    ("r3_in_on_join", ["CREATE TABLE t(a INT, b INT)", "INSERT INTO t VALUES (1,10),(2,20)",
                       "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (1),(2)"],
     "SELECT t.a, u.a FROM t LEFT JOIN u ON u.a = t.a AND t.b IN (SELECT 10) ORDER BY t.a"),
    # recursive CTE column-alias list dropped (DF-E root area, 12+ repros)
    ("r3_rcte_alias_list", [],
     "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT count(*) FROM t"),
    ("r3_rcte_alias_list2", [],
     "WITH RECURSIVE t(n, d) AS (SELECT 1, 0 UNION ALL SELECT n*2, d+1 FROM t WHERE d < 4) SELECT max(n) FROM t"),
    # ---- misc surfaces ----------------------------------------------------
    # duplicate projection expressions rejected (upstream issue #6543)
    ("r3_dup_projection", ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (5)"],
     "SELECT a, a FROM t"),
    ("r3_dup_literal", [],
     "SELECT 1, 1"),
    # unsigned x signed coercion in joins (UNSIGNED accepted in DDL only)
    ("r3_uint_join", ["CREATE TABLE t(v BIGINT)", "INSERT INTO t VALUES (5),(-5)",
                      "CREATE TABLE u(v INT UNSIGNED)", "INSERT INTO u VALUES (5)"],
     "SELECT t.v, u.v FROM t JOIN u ON t.v = u.v"),
    # approx aggregates sanity vs exact
    ("r3_approx_median", ["CREATE TABLE t(v INT)",
                          "INSERT INTO t VALUES " + ",".join(f"({i})" for i in range(1, 11))],
     "SELECT approx_median(v), median(v) FROM t"),
    ("r3_approx_distinct", ["CREATE TABLE t(v INT)",
                            "INSERT INTO t VALUES " + ",".join(f"({i%7})" for i in range(1, 41))],
     "SELECT approx_distinct(v), count(DISTINCT v) FROM t"),
    # NOT IN + NULL three-valued logic (correct on 54.0.0 — regression guard)
    ("r3_not_in_null_rhs", ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3),(NULL)",
                            "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (1),(2),(NULL)"],
     "SELECT a FROM t WHERE a NOT IN (SELECT a FROM u) ORDER BY a"),
    # recursive term must be rightmost UNION branch
    ("r3_rcte_left_branch", [],
     "WITH RECURSIVE t AS (SELECT n+1 FROM t WHERE n < 4 UNION ALL SELECT 1 AS n) SELECT count(*) FROM t"),
]


def as_seeds():
    return [{"setup_sqls": s, "query": q,
             "source": f"df_round3:{n}#{i}", "engine": "datafusion"}
            for i, (n, s, q) in enumerate(DF_R3_TEMPLATES)]
