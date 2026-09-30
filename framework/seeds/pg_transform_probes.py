"""Planner-transformation equivalence probes (PG_TRANSFORM_PROBES).

Arms from PG_PLANNER_TRANSFORM_PLAN_2026-09-20 (proposals 2-8; eager-agg is
built separately).  Each case is a small deterministic fixture plus a list of
``checks`` executed by scripts/pg_transform_probes.py:

  {"kind": "expected", "q": SQL, "expected": [[...]]}
      bag-equal vs known answer
  {"kind": "pair", "q1": SQL, "q2": SQL}
      two formulations that MUST bag-equal
  {"kind": "guc", "guc": name, "value": "off", "q": SQL}
      bag must be identical with the feature GUC on vs off
  {"kind": "no_crash", "q": SQL}
      must not raise / crash (error recorded verbatim)
  optional "marker": substring that must appear in EXPLAIN (COSTS OFF) of q,
      otherwise the oracle is vacuous -> reported as ok_notfired.

Fixtures (created per case):
  su(pk PK, v)          = (1,10),(2,20),(3,30)          -- unique, for SJE
  nva(a NOT NULL, v)    = (1,10),(2,20),(3,30)          -- provably nonnull col
  nvl(a, v)             = (1,10),(NULL,99),(3,30)       -- nullable col
  t(a, b)               = (1,10),(2,20),(3,NULL)
  s(pk, m)              = (1,5),(2,NULL)                -- join partner, m nullable
  s2(pk, v)             = (1,10),(2,20)                 -- shorter join partner
  sm(m)                 = (1),(NULL),(3)                -- nullable single col
  u(x)                  = (2),(7)
"""

def _su(extra=None):
    out = [
        "CREATE TABLE su(pk int PRIMARY KEY, v int)",
        "INSERT INTO su VALUES (1,10),(2,20),(3,30)",
        "ANALYZE su",
    ]
    return out + (extra or [])


def _nva(extra=None):
    out = [
        "CREATE TABLE nva(a int NOT NULL, v int)",
        "INSERT INTO nva VALUES (1,10),(2,20),(3,30)",
        "ANALYZE nva",
    ]
    return out + (extra or [])


def _nvl(extra=None):
    out = [
        "CREATE TABLE nvl(a int, v int)",
        "INSERT INTO nvl VALUES (1,10),(NULL,99),(3,30)",
        "ANALYZE nvl",
    ]
    return out + (extra or [])


def _t(extra=None):
    out = [
        "CREATE TABLE t(a int, b int)",
        "INSERT INTO t VALUES (1,10),(2,20),(3,NULL)",
        "ANALYZE t",
    ]
    return out + (extra or [])


def _s(extra=None):
    out = [
        "CREATE TABLE s(pk int, m int)",
        "INSERT INTO s VALUES (1,5),(2,NULL)",
        "ANALYZE s",
    ]
    return out + (extra or [])


def _s2(extra=None):
    out = [
        "CREATE TABLE s2(pk int, v int)",
        "INSERT INTO s2 VALUES (1,10),(2,20)",
        "ANALYZE s2",
    ]
    return out + (extra or [])


def exp(q, expected, marker=None):
    c = {"kind": "expected", "q": q, "expected": expected}
    if marker:
        c["marker"] = marker
    return c


def pair(q1, q2, marker=None):
    c = {"kind": "pair", "q1": q1, "q2": q2}
    if marker:
        c["marker"] = marker
    return c


def guc(q, g="enable_self_join_elimination", value="off", marker=None):
    c = {"kind": "guc", "guc": g, "value": value, "q": q}
    if marker:
        c["marker"] = marker
    return c


def noc(q):
    return {"kind": "no_crash", "q": q}


SJE = "enable_self_join_elimination"

PG_TRANSFORM_PROBES = [
    # ================================================================
    # ARM ece: eval_const_expressions nullability folds (clauses.c)
    # DistinctExpr->OpExpr, COALESCE arg drop, NullTest/BooleanTest->Const,
    # count(x)->count(*), GREATEST/LEAST arg filtering.
    # ================================================================
    {
        "name": "ece_isdist_notnull",
        "arm": "ece",
        "source": "clauses.c DistinctExpr fold: both args provably nonnull -> "
                  "IS NOT DISTINCT FROM becomes OpExpr.",
        "setup_sqls": _nva(),
        "checks": [
            exp("SELECT count(*) FROM nva WHERE a IS NOT DISTINCT FROM 5",
                [[0]]),
            exp("SELECT count(*) FROM nva WHERE a IS NOT DISTINCT FROM 2",
                [[1]]),
            pair(
                "SELECT count(*) FROM nva WHERE a IS NOT DISTINCT FROM 2",
                "SELECT count(*) FROM nva WHERE a = 2"),
            pair(
                "SELECT count(*) FROM nva WHERE a IS DISTINCT FROM 2",
                "SELECT count(*) FROM nva WHERE a <> 2"),
            exp("SELECT count(*) FROM nva WHERE a IS DISTINCT FROM NULL",
                [[3]]),
            exp("SELECT count(*) FROM nva WHERE a IS NOT DISTINCT FROM NULL",
                [[0]]),
        ],
    },
    {
        "name": "ece_isdist_nullable",
        "arm": "ece",
        "source": "DistinctExpr on nullable col must keep 3VL exactly.",
        "setup_sqls": _nvl(),
        "checks": [
            exp("SELECT count(*) FROM nvl WHERE a IS DISTINCT FROM NULL",
                [[2]]),
            exp("SELECT count(*) FROM nvl WHERE a IS NOT DISTINCT FROM NULL",
                [[1]]),
            exp("SELECT count(*) FROM nvl WHERE a IS DISTINCT FROM 1",
                [[2]]),  # NULL and 3 are distinct-from 1
            exp("SELECT count(*) FROM nvl WHERE a IS NOT DISTINCT FROM 1",
                [[1]]),
            exp("SELECT count(*) FROM nvl WHERE a IS NOT DISTINCT FROM 10",
                [[0]]),  # no a equals 10
            pair(
                "SELECT count(*) FROM nvl WHERE a IS DISTINCT FROM 10",
                "SELECT count(*) FROM nvl WHERE a <> 10 OR a IS NULL"),
            pair(
                "SELECT count(*) FROM nvl WHERE a IS NOT DISTINCT FROM 10",
                "SELECT count(*) FROM nvl WHERE a = 10"),
        ],
    },
    {
        "name": "ece_isdist_oj_tlist",
        "arm": "ece",
        "source": "varnullingrels guard: s.v under LEFT JOIN is nullable. "
                  "Folding IS [NOT] DISTINCT FROM to =/<> in the tlist would "
                  "emit NULL instead of true/false on null-extended rows.",
        "setup_sqls": _t() + _s2(),
        "checks": [
            exp("SELECT t.a, (s2.v IS NOT DISTINCT FROM 10) "
                "FROM t LEFT JOIN s2 ON s2.pk = t.a ORDER BY t.a",
                [[1, True], [2, False], [3, False]]),
            exp("SELECT t.a, (s2.v IS DISTINCT FROM 10) "
                "FROM t LEFT JOIN s2 ON s2.pk = t.a ORDER BY t.a",
                [[1, False], [2, True], [3, True]]),
            pair(
                "SELECT t.a, (s2.v IS NOT DISTINCT FROM 10) "
                "FROM t LEFT JOIN s2 ON s2.pk = t.a ORDER BY t.a",
                "SELECT t.a, CASE WHEN s2.v IS NULL THEN false "
                "ELSE s2.v = 10 END "
                "FROM t LEFT JOIN s2 ON s2.pk = t.a ORDER BY t.a"),
        ],
    },
    {
        "name": "ece_coalesce_drop",
        "arm": "ece",
        "source": "COALESCE arg filtering: later args after a provably nonnull "
                  "arg are dead; the result must still equal the original.",
        "setup_sqls": _nva() + _nvl(),
        "checks": [
            # first arg provably nonnull -> COALESCE(a,v) == a
            pair("SELECT a, coalesce(a, v) FROM nva",
                 "SELECT a, a AS c FROM nva"),
            # nullable first arg -> no drop, keep semantic
            pair("SELECT a, coalesce(a, v) FROM nvl",
                 "SELECT a, CASE WHEN a IS NOT NULL THEN a ELSE v END "
                 "FROM nvl"),
            # leading constant-NULL args are dead
            exp("SELECT count(*) FROM nva WHERE coalesce(NULL::int, a, 99) = 2",
                [[1]]),
            pair("SELECT coalesce(NULL::int, a, 99) FROM nva",
                 "SELECT a FROM nva"),
            # middle arg nonnull kills the tail
            pair("SELECT coalesce(v, a, -1) FROM nva",
                 "SELECT coalesce(v, a) FROM nva"),
        ],
    },
    {
        "name": "ece_greatest_least",
        "arm": "ece",
        "source": "GREATEST/LEAST arg filtering by nonnullability; NULLs are "
                  "skipped, provably-null args dropped.",
        "setup_sqls": _nva() + _nvl(),
        "checks": [
            pair("SELECT greatest(v, a) FROM nva",
                 "SELECT CASE WHEN v IS NULL THEN a "
                 "WHEN v > a THEN v ELSE a END FROM nva"),
            pair("SELECT least(v, a) FROM nva",
                 "SELECT CASE WHEN v IS NULL THEN a "
                 "WHEN v < a THEN v ELSE a END FROM nva"),
            pair("SELECT greatest(a, NULL::int, v) FROM nvl",
                 "SELECT greatest(a, v) FROM nvl"),
            exp("SELECT count(*) FROM nva WHERE greatest(v, 0) > 15", [[2]]),
        ],
    },
    {
        "name": "ece_count_simplify",
        "arm": "ece",
        "source": "simplify_aggref/int8inc_support: count(nonnull expr) -> "
                  "count(*). Must NOT fire when the arg can be NULL, incl. "
                  "vars under an outer join (varnullingrels).",
        "setup_sqls": _nva() + _nvl(),
        "checks": [
            pair("SELECT count(a) FROM nva", "SELECT count(*) FROM nva"),
            exp("SELECT count(a) FROM nva", [[3]]),
            pair("SELECT count(a) FROM nvl",
                 "SELECT count(*) FILTER (WHERE a IS NOT NULL) FROM nvl"),
            exp("SELECT count(a) FROM nvl", [[2]]),
            pair("SELECT count(coalesce(a, 0)) FROM nvl",
                 "SELECT count(*) FROM nvl"),
            pair("SELECT count(a) FILTER (WHERE v > 15) FROM nva",
                 "SELECT count(*) FILTER (WHERE v > 15) FROM nva"),
            # outer-join arm: b.v must NOT be treated as nonnull
            exp("SELECT count(b.v) FROM nva a "
                "LEFT JOIN nvl b ON b.a = a.a", [[2]]),
            pair("SELECT count(b.v) FROM nva a LEFT JOIN nvl b ON b.a = a.a",
                 "SELECT count(*) FILTER (WHERE b.a IS NOT NULL) "
                 "FROM nva a LEFT JOIN nvl b ON b.a = a.a"),
            # count(x) must not fold under FILTER where arg still nullable
            exp("SELECT count(b.v) FILTER (WHERE a.a > 0) FROM nva a "
                "LEFT JOIN nvl b ON b.a = a.a", [[2]]),
        ],
    },
    {
        "name": "ece_nulltest_fold",
        "arm": "ece",
        "source": "NullTest -> Const when arg provably nonnull/nullable; "
                  "nested NullTest on the boolean result.",
        "setup_sqls": _nva() + _nvl(),
        "checks": [
            exp("SELECT count(*) FROM nva WHERE a IS NULL", [[0]]),
            exp("SELECT count(*) FROM nva WHERE a IS NOT NULL", [[3]]),
            exp("SELECT count(*) FROM nva WHERE (a IS NULL) IS NOT NULL",
                [[3]]),
            exp("SELECT count(*) FROM nva WHERE (a IS NULL) IS NULL", [[0]]),
            exp("SELECT count(*) FROM nvl WHERE (a IS NULL) IS NOT NULL",
                [[3]]),
            exp("SELECT count(*) FROM nvl WHERE (a IS NOT NULL) IS NULL",
                [[0]]),
            exp("SELECT count(*) FROM nvl WHERE a IS NULL", [[1]]),
            pair("SELECT count(*) FROM nvl WHERE (a IS NULL) IS TRUE",
                 "SELECT count(*) FROM nvl WHERE a IS NULL"),
        ],
    },
    {
        "name": "ece_booltest_fold",
        "arm": "ece",
        "source": "BooleanTest -> Const/arg when arg provably nonnull.",
        "setup_sqls": [
            "CREATE TABLE bt(b bool, bn bool NOT NULL, i int NOT NULL)",
            "INSERT INTO bt VALUES (true,true,1),(false,false,2),(NULL,true,3)",
            "ANALYZE bt",
        ],
        "checks": [
            exp("SELECT count(*) FROM bt WHERE bn IS TRUE", [[2]]),
            pair("SELECT count(*) FROM bt WHERE bn IS TRUE",
                 "SELECT count(*) FROM bt WHERE bn"),
            exp("SELECT count(*) FROM bt WHERE b IS UNKNOWN", [[1]]),
            exp("SELECT count(*) FROM bt WHERE bn IS UNKNOWN", [[0]]),
            exp("SELECT count(*) FROM bt WHERE bn IS NOT TRUE", [[1]]),
            pair("SELECT count(*) FROM bt WHERE (b IS NULL) IS NOT TRUE",
                 "SELECT count(*) FROM bt WHERE b IS NOT NULL"),
            exp("SELECT count(*) FROM bt WHERE (i > 1) IS TRUE", [[2]]),
            # i=1 -> false -> IS NOT FALSE excludes it
            exp("SELECT count(*) FROM bt WHERE (i > 1) IS NOT FALSE", [[2]]),
            exp("SELECT count(*) FROM bt WHERE (i > 1) IS NOT TRUE", [[1]]),
            exp("SELECT count(*) FROM bt WHERE (b AND bn) IS UNKNOWN", [[1]]),
        ],
    },
    {
        "name": "ece_rowexpr_wholerow",
        "arm": "ece",
        "source": "RowExpr/whole-row NullTest expansion; varattno==0 must "
                  "never be claimed nonnull.",
        "setup_sqls": _nva() + _nvl() + [
            "CREATE TABLE wr(a int, b int)",
            "INSERT INTO wr VALUES (NULL,NULL),(1,2)",
            "ANALYZE wr",
        ],
        "checks": [
            # ROW IS NULL requires ALL fields null: const field blocks it
            exp("SELECT count(*) FROM nvl WHERE ROW(a,1) IS NULL", [[0]]),
            exp("SELECT count(*) FROM nvl WHERE ROW(a,NULL) IS NULL", [[1]]),
            exp("SELECT count(*) FROM nva WHERE ROW(a,NULL) IS NULL", [[0]]),
            pair("SELECT count(*) FROM nvl WHERE ROW(a,NULL) IS NULL",
                 "SELECT count(*) FROM nvl WHERE a IS NULL"),
            exp("SELECT count(*) FROM nvl WHERE ROW(a,1) IS NOT NULL", [[2]]),
            exp("SELECT count(*) FROM wr WHERE wr IS NULL", [[1]]),
            exp("SELECT count(*) FROM wr WHERE wr IS NOT NULL", [[1]]),
            exp("SELECT count(*) FROM wr WHERE (wr).a IS NULL", [[1]]),
            # system columns are nonnull
            exp("SELECT count(*) FROM nva WHERE ctid IS NULL", [[0]]),
            exp("SELECT count(*) FROM nva WHERE tableoid IS NOT NULL", [[3]]),
        ],
    },
    {
        "name": "ece_notvalid_constraint",
        "arm": "ece",
        "source": "NOT VALID NOT NULL constraint sets attnotnull but "
                  "attnullability=INVALID; pre-existing NULLs must not be "
                  "folded away (plancat.c rel_notnullatts_hash).",
        "setup_sqls": [
            "CREATE TABLE nvc(a int)",
            "INSERT INTO nvc VALUES (NULL),(5)",
            "ALTER TABLE nvc ADD CONSTRAINT nn NOT NULL a NOT VALID",
            "ANALYZE nvc",
        ],
        "checks": [
            exp("SELECT count(*) FROM nvc WHERE a IS NULL", [[1]]),
            exp("SELECT count(*) FROM nvc WHERE a IS NOT NULL", [[1]]),
            exp("SELECT count(a) FROM nvc", [[1]]),
            exp("SELECT count(*) FROM nvc WHERE a IS DISTINCT FROM NULL",
                [[1]]),
        ],
    },
    {
        "name": "ece_inherit_divergent",
        "arm": "ece",
        "source": "Inheritance parent skipped by var_is_nonnullable (child "
                  "cols may lack NOT NULL); child tables keep own attnotnull.",
        "setup_sqls": [
            "CREATE TABLE ip(a int)",
            "CREATE TABLE ic(a int NOT NULL) INHERITS (ip)",
            "INSERT INTO ip VALUES (NULL)",
            "INSERT INTO ic VALUES (5)",
            "ANALYZE ip", "ANALYZE ic",
        ],
        "checks": [
            exp("SELECT count(*) FROM ip WHERE a IS NULL", [[1]]),
            exp("SELECT count(*) FROM ip WHERE a IS NOT NULL", [[1]]),
            exp("SELECT count(a) FROM ip", [[1]]),
            exp("SELECT count(*) FROM ONLY ip WHERE a IS NULL", [[1]]),
        ],
    },
    {
        "name": "ece_partition_notnull",
        "arm": "ece",
        "source": "Partitioned parents ARE consulted by var_is_nonnullable; "
                  "parent-level NOT NULL must apply to every child.",
        "setup_sqls": [
            "CREATE TABLE ptn(a int NOT NULL) PARTITION BY RANGE (a)",
            "CREATE TABLE ptn1 PARTITION OF ptn FOR VALUES FROM (0) TO (10)",
            "CREATE TABLE ptn2 PARTITION OF ptn FOR VALUES FROM (10) TO (20)",
            "INSERT INTO ptn VALUES (5),(15)",
            "CREATE TABLE pt(a int) PARTITION BY RANGE (a)",
            "CREATE TABLE pt1 PARTITION OF pt FOR VALUES FROM (0) TO (10)",
            "CREATE TABLE ptd PARTITION OF pt DEFAULT",
            "INSERT INTO pt VALUES (5),(NULL)",
            "ANALYZE ptn", "ANALYZE pt",
        ],
        "checks": [
            exp("SELECT count(*) FROM ptn WHERE a IS NULL", [[0]]),
            exp("SELECT count(*) FROM ptn WHERE a IS NOT NULL", [[2]]),
            exp("SELECT count(*) FROM pt WHERE a IS NULL", [[1]]),
            exp("SELECT count(*) FROM pt WHERE a IS NOT NULL", [[1]]),
            exp("SELECT count(a) FROM pt", [[1]]),
            exp("SELECT count(*) FROM ptn WHERE a IS DISTINCT FROM 5", [[1]]),
        ],
    },
    {
        "name": "ece_nullif_case",
        "arm": "ece",
        "source": "NULLIF/CaseExpr folding sanity alongside the new arms.",
        "setup_sqls": _nva(),
        "checks": [
            exp("SELECT count(*) FROM nva WHERE nullif(a,1) IS NULL", [[1]]),
            exp("SELECT count(*) FROM nva WHERE nullif(5,5) IS NULL", [[3]]),
            exp("SELECT count(*) FROM nva WHERE CASE WHEN a > 1 THEN 'x' "
                "ELSE 'y' END = 'x'", [[2]]),
            exp("SELECT count(*) FROM nva WHERE CASE WHEN true THEN a "
                "ELSE -1 END > 1", [[2]]),
        ],
    },

    # ================================================================
    # ARM notin: NOT IN -> anti join conversion (subselect.c)
    # sublink_testexpr_is_not_nullable / query_outputs_are_not_nullable /
    # op_is_safe_index_member / hashed subplan unknownEqFalse.
    # NULL-semantics differences vs NOT EXISTS are LEGAL on nullable cols;
    # pairs only equate them where both sides are provably nonnull.
    # ================================================================
    {
        "name": "ni_basic_notnull",
        "arm": "notin",
        "source": "NOT IN on provably nonnull cols converts to anti join; "
                  "must equal NOT EXISTS.",
        "setup_sqls": _nva() + _su(),
        "checks": [
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT pk FROM su WHERE pk < 3)", [[1]],
                marker="Anti Join"),
            pair("SELECT count(*) FROM nva WHERE a NOT IN "
                 "(SELECT pk FROM su WHERE pk < 3)",
                 "SELECT count(*) FROM nva WHERE NOT EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nva.a AND su.pk < 3)"),
            pair("SELECT count(*) FROM nva WHERE a NOT IN "
                 "(SELECT pk FROM su)",
                 "SELECT count(*) FROM nva WHERE NOT EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nva.a)"),
            exp("SELECT count(*) FROM nva WHERE a NOT IN (SELECT pk FROM su)",
                [[0]]),
        ],
    },
    {
        "name": "ni_nullable_subq",
        "arm": "notin",
        "source": "NULL in the subquery output must keep 3VL: everything "
                  "filters out.",
        "setup_sqls": _nva() + [
            "CREATE TABLE sm(m int)",
            "INSERT INTO sm VALUES (1),(NULL),(3)",
            "ANALYZE sm",
        ],
        "checks": [
            exp("SELECT count(*) FROM nva WHERE a NOT IN (SELECT m FROM sm)",
                [[0]]),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT m FROM sm WHERE m IS NOT NULL)", [[1]]),  # a=2
            # legal divergence vs NOT EXISTS (records it, not a hit)
            exp("SELECT count(*) FROM nva WHERE NOT EXISTS "
                "(SELECT 1 FROM sm WHERE sm.m = nva.a)", [[1]]),
        ],
    },
    {
        "name": "ni_nullable_outer",
        "arm": "notin",
        "source": "Nullable outer test expr: NULL rows must be filtered, "
                  "not kept.",
        "setup_sqls": _nvl() + _su(),
        "checks": [
            exp("SELECT count(*) FROM nvl WHERE a NOT IN (SELECT pk FROM su)",
                [[0]]),
            # subq={3}: a=1 not-in true, NULL filtered, a=3 excluded -> 1
            exp("SELECT count(*) FROM nvl WHERE a NOT IN "
                "(SELECT pk FROM su WHERE pk > 2)", [[1]]),
            pair("SELECT count(*) FROM nvl WHERE a IS NOT NULL AND a NOT IN "
                 "(SELECT pk FROM su WHERE pk > 2)",
                 "SELECT count(*) FROM nvl WHERE a IS NOT NULL AND "
                 "NOT EXISTS (SELECT 1 FROM su WHERE su.pk = nvl.a "
                 "AND su.pk > 2)"),
        ],
    },
    {
        "name": "ni_rowcompare",
        "arm": "notin",
        "source": "RowCompareExpr arm of sublink_testexpr_is_not_nullable.",
        "setup_sqls": _nva() + _nvl() + _su(),
        "checks": [
            exp("SELECT count(*) FROM nva WHERE (a,v) NOT IN "
                "(SELECT pk, v FROM su)", [[0]]),
            exp("SELECT count(*) FROM nva WHERE (a,v) NOT IN "
                "(SELECT pk, v+1 FROM su)", [[3]]),
            exp("SELECT count(*) FROM nva WHERE (a,v) NOT IN "
                "(SELECT pk, v FROM su WHERE pk < 3)", [[1]]),
            # (NULL,99): 99<>10/20 makes row-compare FALSE (not NULL) -> kept
            exp("SELECT count(*) FROM nvl WHERE (a,v) NOT IN "
                "(SELECT pk, v FROM su WHERE pk < 3)", [[2]]),
            # NULL member poisons rows whose a-part matches; (3,30)'s a-part
            # mismatches both -> FALSE AND NULL -> row survives -> 1
            exp("SELECT count(*) FROM nvl WHERE (a,v) NOT IN "
                "(SELECT pk, NULL::int FROM su WHERE pk < 3)", [[1]]),
        ],
    },
    {
        "name": "ni_correlated",
        "arm": "notin",
        "source": "Correlated NOT IN stays a subplan; semantics still exact.",
        "setup_sqls": _su() + [
            "CREATE TABLE t2(x int, y int)",
            "INSERT INTO t2 VALUES (1,5),(2,5),(9,0)",
            "ANALYZE t2",
        ],
        "checks": [
            exp("SELECT count(*) FROM t2 WHERE x NOT IN "
                "(SELECT pk FROM su WHERE pk < t2.y)", [[1]]),  # only (9,0)
            exp("SELECT t2.x FROM t2 WHERE x NOT IN "
                "(SELECT pk FROM su WHERE pk < t2.y)", [[9]]),
            # pk>y: y=5 -> {} -> NOT IN empty is TRUE (even for null side)
            exp("SELECT count(*) FROM t2 WHERE x NOT IN "
                "(SELECT pk FROM su WHERE pk > t2.y)", [[3]]),
        ],
    },
    {
        "name": "ni_subq_outerjoin",
        "arm": "notin",
        "source": "query_outputs_are_not_nullable: subquery containing a LEFT "
                  "JOIN produces nullable output -> must not convert.",
        "setup_sqls": _nva() + [
            "CREATE TABLE s1(id int)", "CREATE TABLE s2n(id int, m int)",
            "INSERT INTO s1 VALUES (1),(2)",
            "INSERT INTO s2n VALUES (1,10)",
            "ANALYZE s1", "ANALYZE s2n",
        ],
        "checks": [
            # output set: {10, NULL} -> NOT IN yields empty
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT s2n.m FROM s1 LEFT JOIN s2n ON s1.id = s2n.id)",
                [[0]]),
            # inner join keeps nonnull output {10}
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT s2n.m FROM s1 JOIN s2n ON s1.id = s2n.id)", [[3]]),
        ],
    },
    {
        "name": "ni_strict_subq_qual",
        "arm": "notin",
        "source": "find_safe_quals: m>0 proves output nonnull -> conversion "
                  "legal; result must still be exact.",
        "setup_sqls": _nva() + [
            "CREATE TABLE sm(m int)",
            "INSERT INTO sm VALUES (1),(NULL),(3)",
            "ANALYZE sm",
        ],
        "checks": [
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT m FROM sm WHERE m > 0)", [[1]]),  # a=2
            pair("SELECT count(*) FROM nva WHERE a NOT IN "
                 "(SELECT m FROM sm WHERE m > 0)",
                 "SELECT count(*) FROM nva WHERE NOT EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = nva.a AND sm.m > 0)"),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT m FROM sm WHERE m > 0 OR m IS NULL)", [[0]]),
        ],
    },
    {
        "name": "ni_domain_col",
        "arm": "notin",
        "source": "Domain-typed columns: no provable source -> conservative.",
        "setup_sqls": _su() + [
            "CREATE DOMAIN di AS int",
            "CREATE TABLE dt(d di)",
            "INSERT INTO dt VALUES (1),(NULL),(9)",
            "ANALYZE dt",
        ],
        "checks": [
            exp("SELECT count(*) FROM dt WHERE d NOT IN (SELECT pk FROM su)",
                [[1]]),  # d=9
            exp("SELECT count(*) FROM dt WHERE d NOT IN "
                "(SELECT pk FROM su WHERE pk < 9)", [[1]]),
        ],
    },
    {
        "name": "ni_check_constraint",
        "arm": "notin",
        "source": "CHECK (m IS NOT NULL) does NOT feed notnullattnums -> "
                  "subplan path; result must still be exact.",
        "setup_sqls": _nva() + [
            "CREATE TABLE ck(m int CHECK (m IS NOT NULL))",
            "INSERT INTO ck VALUES (1),(2)",
            "ANALYZE ck",
        ],
        "checks": [
            exp("SELECT count(*) FROM nva WHERE a NOT IN (SELECT m FROM ck)",
                [[1]]),
            pair("SELECT count(*) FROM nva WHERE a NOT IN (SELECT m FROM ck)",
                 "SELECT count(*) FROM nva WHERE NOT EXISTS "
                 "(SELECT 1 FROM ck WHERE ck.m = nva.a)"),
        ],
    },
    {
        "name": "ni_values_setop_agg",
        "arm": "notin",
        "source": "VALUES / set-op / DISTINCT / GROUP BY / aggregate outputs: "
                  "conversion is punted or guarded; results must be exact.",
        "setup_sqls": _nva() + _su() + [
            "CREATE TABLE sm(m int)",
            "INSERT INTO sm VALUES (1),(NULL),(3)",
            "ANALYZE sm",
        ],
        "checks": [
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(VALUES (1),(NULL))", [[0]]),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(VALUES (1),(2))", [[1]]),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT pk FROM su UNION SELECT 99)", [[0]]),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT pk FROM su WHERE pk < 3 UNION SELECT 99)", [[1]]),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT m FROM sm GROUP BY m)", [[0]]),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT DISTINCT m FROM sm)", [[0]]),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT count(*) FROM su)", [[2]]),  # count=3 keeps a=1,2
        ],
    },
    {
        "name": "ni_hashed_subplan",
        "arm": "notin",
        "source": "Large subquery -> hashed subplan arm (unknownEqFalse); "
                  "NULL member must still poison the NOT IN.",
        "setup_sqls": [
            "CREATE TABLE big(k int NOT NULL)",
            "INSERT INTO big SELECT g FROM generate_series(1,400) g",
            "CREATE TABLE big2(k int)",
            "INSERT INTO big2 SELECT g FROM generate_series(1,400) g",
            "INSERT INTO big2 VALUES (NULL)",
            "CREATE TABLE nt(x int)",
            "INSERT INTO nt VALUES (1),(401),(NULL)",
            "ANALYZE big", "ANALYZE big2", "ANALYZE nt",
        ],
        "checks": [
            exp("SELECT x FROM nt WHERE x NOT IN (SELECT k FROM big)",
                [[401]]),
            exp("SELECT count(*) FROM nt WHERE x NOT IN "
                "(SELECT k FROM big2)", [[0]]),
            exp("SELECT x FROM nt WHERE x NOT IN (SELECT k FROM big)",
                [[401]], marker="SubPlan"),
        ],
    },
    {
        "name": "ni_under_outerjoin",
        "arm": "notin",
        "source": "Outer test expr under LEFT JOIN carries varnullingrels -> "
                  "nullable; conversion must not fire, result exact.",
        "setup_sqls": _t() + _s2() + _su(),
        "checks": [
            # subq {pk>10} is empty -> NOT IN () is TRUE even for NULL s2.pk
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk NOT IN (SELECT pk FROM su WHERE pk > 10)",
                [[3]]),
            # nonempty subq {2,3}: a=1 keeps, a=2 drops, a=3 NULL->filtered
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk NOT IN (SELECT pk FROM su WHERE pk > 1)",
                [[1]]),
            exp("SELECT t.a FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk NOT IN (SELECT pk FROM su) ORDER BY t.a",
                []),
        ],
    },
    {
        "name": "ni_coalesce_outer_expr",
        "arm": "notin",
        "source": "coalesce(a,0) is provably nonnull -> conversion legal on "
                  "the outer side; result exact.",
        "setup_sqls": _nvl() + _su(),
        "checks": [
            exp("SELECT count(*) FROM nvl WHERE coalesce(a,0) NOT IN "
                "(SELECT pk FROM su)", [[1]]),  # the NULL row -> 0
            exp("SELECT nvl.a FROM nvl WHERE coalesce(a,0) NOT IN "
                "(SELECT pk FROM su)", [[None]]),
        ],
    },
    {
        "name": "ni_having_and_multi",
        "arm": "notin",
        "source": "NOT IN inside HAVING and multiple sublinks per qual.",
        "setup_sqls": _nva() + _su(),
        "checks": [
            exp("SELECT count(*) FROM (SELECT a FROM nva GROUP BY a "
                "HAVING a NOT IN (SELECT pk FROM su WHERE pk < 3)) q",
                [[1]]),
            exp("SELECT count(*) FROM nva WHERE a NOT IN "
                "(SELECT pk FROM su WHERE pk < 3) AND v NOT IN "
                "(SELECT v FROM su WHERE v < 30)", [[1]]),
            # v in {10,20,30} is never in {1,2,3} -> OR keeps every row
            exp("SELECT count(*) FROM nva WHERE a NOT IN (SELECT pk FROM su) "
                "OR v NOT IN (SELECT pk FROM su)", [[3]]),
        ],
    },

    # ================================================================
    # ARM ojred: reduce_outer_joins pass2 strength reduction
    # LEFT/FULL -> ANTI/RIGHT_ANTI, forced-null propagation,
    # remove_redundant_nullability_quals.
    # ================================================================
    {
        "name": "oj_left_to_anti",
        "arm": "ojred",
        "source": "WHERE s.pk IS NULL on LEFT JOIN -> anti join; must equal "
                  "NOT EXISTS formulation.",
        "setup_sqls": _t() + _s2(),
        "checks": [
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk IS NULL", [[1]]),
            pair("SELECT t.a FROM t LEFT JOIN s2 ON s2.pk = t.a "
                 "WHERE s2.pk IS NULL",
                 "SELECT t.a FROM t WHERE NOT EXISTS "
                 "(SELECT 1 FROM s2 WHERE s2.pk = t.a)"),
            exp("SELECT t.a, s2.v FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk IS NULL", [[3, None]]),
        ],
    },
    {
        "name": "oj_forced_by_own_qual",
        "arm": "ojred",
        "source": "forced_null_var_is_nonnullable: s.m IS NULL is provable "
                  "when the ON qual s.m>0 is strict on s.m -> reduction "
                  "changes which nullability quals remain.",
        "setup_sqls": _t() + _s(),
        "checks": [
            # s.m>0 in ON: matched rows have m=5; unmatched get m NULL.
            exp("SELECT count(*) FROM t LEFT JOIN s ON s.pk = t.a AND s.m > 0 "
                "WHERE s.m IS NULL", [[2]]),
            exp("SELECT t.a FROM t LEFT JOIN s ON s.pk = t.a AND s.m > 0 "
                "WHERE s.m IS NULL ORDER BY t.a", [[2], [3]]),
            # s(2,NULL) matches a=2 with m NULL -> kept alongside a=3
            exp("SELECT count(*) FROM t LEFT JOIN s ON s.pk = t.a "
                "WHERE s.m IS NULL", [[2]]),
        ],
    },
    {
        "name": "oj_full_reductions",
        "arm": "ojred",
        "source": "FULL JOIN reduced to ANTI / RIGHT_ANTI (master-only): the "
                  "IS NULL var must be provably nonnull (catalog NOT NULL) "
                  "so it can only be satisfied via null-extension.",
        "setup_sqls": _t() + _s2() + [
            "CREATE TABLE s2n(pk int NOT NULL, v int)",
            "INSERT INTO s2n VALUES (1,10),(2,20)",
            "CREATE TABLE tn(a int NOT NULL, b int)",
            "INSERT INTO tn VALUES (1,10),(2,20),(3,NULL)",
            "ANALYZE s2n", "ANALYZE tn",
        ],
        "checks": [
            # nullable col -> cannot prove forced-null -> stays FULL (ok)
            exp("SELECT count(*) FROM t FULL JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk IS NULL", [[1]]),
            # nonnull col -> FULL->ANTI fires on master
            exp("SELECT count(*) FROM t FULL JOIN s2n ON s2n.pk = t.a "
                "WHERE s2n.pk IS NULL", [[1]], marker="Anti Join"),
            exp("SELECT t.a, s2n.v FROM t FULL JOIN s2n ON s2n.pk = t.a "
                "WHERE s2n.pk IS NULL", [[3, None]], marker="Anti Join"),
            # RIGHT_ANTI arm: nonnull var on the left side
            exp("SELECT count(*) FROM tn FULL JOIN s2n ON s2n.pk = tn.a "
                "WHERE tn.a IS NULL", [[0]]),
            exp("SELECT s2n.pk FROM tn FULL JOIN s2n ON s2n.pk = tn.a "
                "WHERE tn.a IS NULL", []),
            # s2n row 9 unmatched on right side -> right-anti keeps it
            exp("SELECT count(*) FROM tn FULL JOIN "
                "(SELECT pk, v FROM s2n UNION ALL SELECT 9, 90) s2x "
                "ON s2x.pk = tn.a WHERE tn.a IS NULL", [[1]]),
            # OR blocks reduction -> stays FULL, same rows
            exp("SELECT count(*) FROM t FULL JOIN s2n ON s2n.pk = t.a "
                "WHERE s2n.pk IS NULL OR t.a IS NULL", [[1]]),
            exp("SELECT count(*) FROM t FULL JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk IS NOT NULL AND t.a IS NOT NULL", [[2]]),
            pair("SELECT t.a, s2n.pk FROM t FULL JOIN s2n ON s2n.pk = t.a "
                 "WHERE s2n.pk IS NULL",
                 "SELECT t.a, NULL::int FROM t WHERE NOT EXISTS "
                 "(SELECT 1 FROM s2n WHERE s2n.pk = t.a)"),
        ],
    },
    {
        "name": "oj_right_and_whole",
        "arm": "ojred",
        "source": "RIGHT JOIN normalized then reduced; whole-row Var IS NULL.",
        "setup_sqls": _t() + _s2() + [
            "INSERT INTO s2 VALUES (9,90)",
        ],
        "checks": [
            exp("SELECT count(*) FROM t RIGHT JOIN s2 ON s2.pk = t.a "
                "WHERE t.a IS NULL", [[1]]),  # s2 row 9 unmatched
            exp("SELECT s2.pk FROM t RIGHT JOIN s2 ON s2.pk = t.a "
                "WHERE t.a IS NULL", [[9]]),
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2 IS NULL", [[1]]),
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2 IS NOT NULL", [[2]]),
        ],
    },
    {
        "name": "oj_nested_forced_null",
        "arm": "ojred",
        "source": "Pass-2 state propagation through nested join domains; "
                  "lower forced-null var consumed by upper-level qual.",
        "setup_sqls": _t() + _s2() + _su() + [
            "CREATE TABLE u(x int)",
            "INSERT INTO u VALUES (2),(7)",
            "ANALYZE u",
        ],
        "checks": [
            # both null tests -> t.a=3 only
            exp("SELECT count(*) FROM (t LEFT JOIN s2 ON s2.pk = t.a) "
                "LEFT JOIN u ON u.x = s2.pk "
                "WHERE s2.pk IS NULL AND u.x IS NULL", [[1]]),
            # s2.v forced-null var consumed at the upper join level
            exp("SELECT count(*) FROM (t LEFT JOIN s2 ON s2.pk = t.a) "
                "LEFT JOIN u ON u.x = s2.v WHERE s2.v IS NULL", [[1]]),
            # FULL inside a LEFT nest
            exp("SELECT count(*) FROM (t FULL JOIN s2 ON s2.pk = t.a) "
                "LEFT JOIN u ON u.x = s2.pk WHERE s2.pk IS NULL", [[1]]),
            # three-level chain
            exp("SELECT count(*) FROM ((t LEFT JOIN s2 ON s2.pk = t.a) "
                "LEFT JOIN u ON u.x = t.a) LEFT JOIN su ON su.pk = u.x "
                "WHERE s2.pk IS NULL AND u.x IS NULL", [[1]]),
        ],
    },
    {
        "name": "oj_reduction_blocked",
        "arm": "ojred",
        "source": "Controls: shapes that must NOT reduce but still return "
                  "the same rows.",
        "setup_sqls": _t() + _s2(),
        "checks": [
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk IS NULL OR t.a = 2", [[2]]),
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE NOT (s2.pk IS NOT NULL)", [[1]]),
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE (s2.pk = t.a) IS UNKNOWN", [[1]]),
            pair("SELECT t.a FROM t LEFT JOIN s2 ON s2.pk = t.a "
                 "WHERE (s2.pk = t.a) IS UNKNOWN",
                 "SELECT t.a FROM t LEFT JOIN s2 ON s2.pk = t.a "
                 "WHERE s2.pk IS NULL"),
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk IS NULL AND t.b IS NOT NULL", [[0]]),
            # sysattr forced-null
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.ctid IS NULL", [[1]]),
            # redundant nullability quals on both cols
            exp("SELECT count(*) FROM t LEFT JOIN s2 ON s2.pk = t.a "
                "WHERE s2.pk IS NULL AND s2.v IS NULL", [[1]]),
        ],
    },

    # ================================================================
    # ARM sje: self-join elimination residual arms (analyzejoins.c)
    # guc oracle: enable_self_join_elimination on/off must bag-equal.
    # ================================================================
    {
        "name": "sje_multicopy_chain",
        "arm": "sje",
        "source": "3-4 copies of the same table; sequential elimination and "
                  "ChangeVarNodes bookkeeping.",
        "setup_sqls": _su(),
        "checks": [
            guc("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "JOIN su c ON b.pk = c.pk"),
            guc("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "JOIN su c ON b.pk = c.pk JOIN su d ON c.pk = d.pk"),
            exp("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "JOIN su c ON b.pk = c.pk JOIN su d ON c.pk = d.pk", [[3]]),
            guc("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "JOIN su c ON a.pk = c.pk AND c.v > 15"),
            exp("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "JOIN su c ON a.pk = c.pk AND c.v > 15", [[2]]),
            guc("SELECT sum(b.v * 2) FROM su a JOIN su b ON a.pk = b.pk"),
            exp("SELECT sum(b.v * 2) FROM su a JOIN su b ON a.pk = b.pk",
                [[120]]),
        ],
    },
    {
        "name": "sje_qual_matching",
        "arm": "sje",
        "source": "match_unique_clauses / uclauses: base quals matching "
                  "across sides; differing constants must block removal but "
                  "not change results.",
        "setup_sqls": _su(),
        "checks": [
            exp("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "AND a.v = 10 AND b.v = 20", [[0]]),
            guc("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "AND a.v = 10 AND b.v = 20"),
            # unique proof via base quals, not join clause
            exp("SELECT count(*) FROM su a JOIN su b ON a.v = b.v "
                "WHERE a.pk = 1 AND b.pk = 1", [[1]]),
            exp("SELECT count(*) FROM su a JOIN su b ON a.v = b.v "
                "WHERE a.pk = 1 AND b.pk = 2", [[0]]),
            guc("SELECT count(*) FROM su a JOIN su b ON a.v = b.v "
                "WHERE a.pk = 1 AND b.pk = 1"),
            # EC-implied equality: b.pk=2 implies a.pk=2
            exp("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "AND b.pk = 2", [[1]]),
            guc("SELECT count(*) FROM su a JOIN su b ON a.pk = b.pk "
                "AND b.pk = 2"),
        ],
    },
    {
        "name": "sje_emptied_fromexpr",
        "arm": "sje",
        "source": "fixup_selfjoin_jointree: removing a rel can empty a "
                  "FromExpr/JoinExpr node; orphan quals must stay valid.",
        "setup_sqls": _su(),
        "checks": [
            exp("SELECT count(*) FROM (VALUES (1)) x(n), "
                "su a JOIN su b ON a.pk = b.pk WHERE a.v > 15", [[2]]),
            guc("SELECT count(*) FROM (VALUES (1)) x(n), "
                "su a JOIN su b ON a.pk = b.pk WHERE a.v > 15"),
            exp("SELECT count(*) FROM su a JOIN "
                "(su b JOIN su c ON b.pk = c.pk) ON a.pk = b.pk "
                "WHERE c.v > 15", [[2]]),
            guc("SELECT count(*) FROM su a JOIN "
                "(su b JOIN su c ON b.pk = c.pk) ON a.pk = b.pk "
                "WHERE c.v > 15"),
        ],
    },
    {
        "name": "sje_rowmarks",
        "arm": "sje",
        "source": "Rowmark strength transfer when one side is removed; "
                  "asymmetric FOR UPDATE/SHARE combos.",
        "setup_sqls": _su(),
        "checks": [
            guc("SELECT a.pk, b.v FROM su a JOIN su b ON a.pk = b.pk "
                "FOR UPDATE OF b"),
            guc("SELECT a.pk, b.v FROM su a JOIN su b ON a.pk = b.pk "
                "FOR UPDATE OF a"),
            guc("SELECT a.pk, b.v FROM su a JOIN su b ON a.pk = b.pk "
                "FOR NO KEY UPDATE OF a FOR SHARE OF b"),
            guc("SELECT a.pk FROM su a JOIN su b ON a.pk = b.pk FOR UPDATE"),
            guc("SELECT a.pk FROM su a JOIN su b ON a.pk = b.pk "
                "FOR KEY SHARE OF b"),
            exp("SELECT a.pk FROM su a JOIN su b ON a.pk = b.pk "
                "ORDER BY a.pk FOR UPDATE OF b", [[1], [2], [3]]),
        ],
    },
    {
        "name": "sje_under_oj_exists",
        "arm": "sje",
        "source": "SJE inside a nullable join side and inside EXISTS; "
                  "nullingrel bookkeeping.",
        "setup_sqls": _su() + _t(),
        "checks": [
            exp("SELECT count(*) FROM t LEFT JOIN "
                "(su a JOIN su b ON a.pk = b.pk) ON a.pk = t.a", [[3]]),
            guc("SELECT count(*) FROM t LEFT JOIN "
                "(su a JOIN su b ON a.pk = b.pk) ON a.pk = t.a"),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT 1 FROM su a JOIN su b ON a.pk = b.pk "
                "WHERE a.pk = t.a)", [[3]]),
            guc("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT 1 FROM su a JOIN su b ON a.pk = b.pk "
                "WHERE a.pk = t.a)"),
            # USING form merges columns
            exp("SELECT count(*) FROM su a JOIN su b USING (pk)", [[3]]),
            guc("SELECT count(*) FROM su a JOIN su b USING (pk)"),
        ],
    },
    {
        "name": "sje_refused_shapes",
        "arm": "sje",
        "source": "Shapes where elimination must not fire: inheritance "
                  "parents, partitioned parents, views, lateral copies.",
        "setup_sqls": [
            "CREATE TABLE ipk(a int PRIMARY KEY)",
            "CREATE TABLE ick(a int) INHERITS (ipk)",
            "INSERT INTO ipk VALUES (1),(2)",
            "INSERT INTO ick VALUES (1),(9)",
            "CREATE TABLE ptp(a int PRIMARY KEY) PARTITION BY HASH (a)",
            "CREATE TABLE ptp0 PARTITION OF ptp FOR VALUES WITH "
            "(MODULUS 2, REMAINDER 0)",
            "CREATE TABLE ptp1 PARTITION OF ptp FOR VALUES WITH "
            "(MODULUS 2, REMAINDER 1)",
            "INSERT INTO ptp VALUES (1),(2),(3)",
            "CREATE TABLE su(pk int PRIMARY KEY, v int)",
            "INSERT INTO su VALUES (1,10),(2,20),(3,30)",
            "CREATE VIEW vsu AS SELECT pk, v FROM su",
            "ANALYZE ipk", "ANALYZE ick", "ANALYZE ptp", "ANALYZE su",
        ],
        "checks": [
            # a=1 exists in both parent and child: 2*2 self-matches
            exp("SELECT count(*) FROM ipk a JOIN ipk b ON a.a = b.a", [[6]]),
            guc("SELECT count(*) FROM ipk a JOIN ipk b ON a.a = b.a"),
            exp("SELECT count(*) FROM ptp a JOIN ptp b ON a.a = b.a", [[3]]),
            guc("SELECT count(*) FROM ptp a JOIN ptp b ON a.a = b.a"),
            exp("SELECT count(*) FROM vsu a JOIN su b ON a.pk = b.pk", [[3]]),
            exp("SELECT count(*) FROM su a JOIN LATERAL "
                "(SELECT * FROM su WHERE pk = a.pk) x ON true", [[3]]),
        ],
    },
    {
        "name": "sje_unionall_flatten",
        "arm": "sje",
        "source": "flatten_simple_union_all leaves share su's relid; verified "
                  "they stay under an Append (no jointree-level SJE). "
                  "Regression arm.",
        "setup_sqls": _su(),
        "checks": [
            exp("SELECT count(*) FROM (SELECT pk, v FROM su UNION ALL "
                "SELECT pk, v FROM su) s JOIN su t ON s.pk = t.pk", [[6]]),
            guc("SELECT count(*) FROM (SELECT pk, v FROM su UNION ALL "
                "SELECT pk, v FROM su) s JOIN su t ON s.pk = t.pk"),
            exp("SELECT count(*) FROM (SELECT pk FROM su UNION ALL "
                "SELECT pk FROM su UNION ALL SELECT pk FROM su) s "
                "JOIN su t ON s.pk = t.pk", [[9]]),
        ],
    },

    # ================================================================
    # ARM rteres: remove_useless_result_rtes LEFT/SEMI arms
    # substitute_phv_relids retargeting, find_dependent_phvs,
    # dropped_outer_joins nullingrel cleanup.
    # ================================================================
    {
        "name": "rr_left_values_phv",
        "arm": "rteres",
        "source": "RTE_RESULT under LEFT JOIN; PHV evaluated on the RTE must "
                  "be retargeted or the join kept.",
        "setup_sqls": _t(),
        "checks": [
            exp("SELECT t.a, v.x FROM t "
                "LEFT JOIN (VALUES (1)) v(x) ON v.x = t.a ORDER BY t.a",
                [[1, 1], [2, None], [3, None]]),
            exp("SELECT t.a, v.x FROM t "
                "LEFT JOIN (SELECT 42 AS x) v ON v.x = t.a ORDER BY t.a",
                [[1, None], [2, None], [3, None]]),
            exp("SELECT t.a, v.x FROM t "
                "LEFT JOIN (VALUES (1)) v(x) ON v.x = 999 ORDER BY t.a",
                [[1, None], [2, None], [3, None]]),
        ],
    },
    {
        "name": "rr_phv_upper_on",
        "arm": "rteres",
        "source": "PHV referenced ONLY in an upper ON qual (not the tlist): "
                  "find_dependent_phvs must see it or the reduction is wrong.",
        "setup_sqls": _t() + [
            "CREATE TABLE u(x int)",
            "INSERT INTO u VALUES (2),(7)",
            "ANALYZE u",
        ],
        "checks": [
            exp("SELECT t.a, u.x FROM t "
                "LEFT JOIN (VALUES (2)) v(x) ON true "
                "JOIN u ON u.x = v.x AND u.x = t.a ORDER BY t.a",
                [[2, 2]]),
            exp("SELECT t.a, v.x, u.x FROM t "
                "LEFT JOIN (VALUES (2)) v(x) ON true "
                "LEFT JOIN u ON u.x = v.x ORDER BY t.a",
                [[1, 2, 2], [2, 2, 2], [3, 2, 2]]),
            exp("SELECT t.a, u.x FROM t "
                "LEFT JOIN (VALUES (2)) v(x) ON true "
                "LEFT JOIN u ON u.x = v.x AND u.x = t.a ORDER BY t.a",
                [[1, None], [2, 2], [3, None]]),
        ],
    },
    {
        "name": "rr_chained_nested",
        "arm": "rteres",
        "source": "Chained/nested RTE_RESULT removal; PHV of first join "
                  "feeding the second join's qual.",
        "setup_sqls": _t(),
        "checks": [
            exp("SELECT t.a, v.x, w.y FROM t "
                "LEFT JOIN (VALUES (1)) v(x) ON v.x = t.a "
                "LEFT JOIN (VALUES (9)) w(y) ON w.y = v.x ORDER BY t.a",
                [[1, 1, None], [2, None, None], [3, None, None]]),
            exp("SELECT v.x, w.y FROM (VALUES (1)) v(x) "
                "LEFT JOIN (VALUES (2)) w(y) ON true",
                [[1, 2]]),
            exp("SELECT t.a, q.x, q.y FROM t LEFT JOIN "
                "((VALUES (1)) v(x) LEFT JOIN (VALUES (2)) w(y) ON true) q "
                "ON q.x = t.a ORDER BY t.a",
                [[1, 1, 2], [2, None, None], [3, None, None]]),
        ],
    },
    {
        "name": "rr_inner_full_semi",
        "arm": "rteres",
        "source": "RTE_RESULT in inner/FULL/semi positions; FULL is not "
                  "removable, semi keeps the EXISTS semantics.",
        "setup_sqls": _t(),
        "checks": [
            exp("SELECT count(*) FROM (VALUES (7)) v(x) "
                "JOIN t ON t.a > v.x", [[0]]),
            exp("SELECT count(*) FROM (VALUES (1)) v(x) "
                "JOIN t ON t.a >= v.x", [[3]]),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT 1 FROM (VALUES (1)) v(x) WHERE v.x = t.a)", [[1]]),
            exp("SELECT t.a, v.x FROM t FULL JOIN (VALUES (2)) v(x) "
                "ON v.x = t.a ORDER BY t.a",
                [[1, None], [2, 2], [3, None]]),
            exp("SELECT t.a, v.x FROM t LEFT JOIN LATERAL "
                "(VALUES (t.a + 1)) v(x) ON true ORDER BY t.a",
                [[1, 2], [2, 3], [3, 4]]),
        ],
    },

    # ================================================================
    # ARM restart: planmain restart-loop cross-pass staleness
    # chained removals: pullup -> semi -> unique-inner -> inner -> SJE,
    # RTE_RESULT produced by pullup then removed, etc.
    # ================================================================
    {
        "name": "rs_in_sje_semijoin",
        "arm": "restart",
        "source": "IN->semi on a self-joined subquery; inner-side SJE then "
                  "unique-semijoin reduction can chain in one plan.",
        "setup_sqls": _su() + _t(),
        "checks": [
            exp("SELECT count(*) FROM t WHERE t.a IN "
                "(SELECT a.pk FROM su a JOIN su b ON a.pk = b.pk)", [[3]]),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT 1 FROM su a JOIN su b ON a.pk = b.pk "
                "AND a.pk = t.a)", [[3]]),
            exp("SELECT count(*) FROM t WHERE t.a IN "
                "(SELECT DISTINCT pk FROM su)", [[3]]),
            exp("SELECT count(*) FROM t WHERE t.a IN "
                "(SELECT a.pk FROM su a JOIN su b ON a.pk = b.pk "
                "WHERE b.v > 15)", [[2]]),
        ],
    },
    {
        "name": "rs_pullup_then_result",
        "arm": "restart",
        "source": "Subquery pullup yields an RTE_RESULT that a later pass "
                  "removes; PHV/nulling bookkeeping across the restart.",
        "setup_sqls": _t(),
        "checks": [
            exp("SELECT t.a, s.x FROM t LEFT JOIN (SELECT 1 AS x) s "
                "ON s.x = t.a ORDER BY t.a",
                [[1, 1], [2, None], [3, None]]),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT 1 FROM (SELECT 1 AS x) s WHERE s.x = t.a)", [[1]]),
            exp("SELECT t.a, s.x FROM t JOIN (SELECT 2 AS x) s "
                "ON s.x = t.a", [[2, 2]]),
        ],
    },
    {
        "name": "rs_oj_sje_chain",
        "arm": "restart",
        "source": "LEFT JOIN over a self-joined subtree; SJE fires inside "
                  "the nullable side during the same preprocessing pass.",
        "setup_sqls": _su() + _t(),
        "checks": [
            exp("SELECT count(*) FROM t LEFT JOIN "
                "(su a JOIN su b ON a.pk = b.pk AND b.v > 15) "
                "ON a.pk = t.a", [[3]]),
            exp("SELECT t.a, a.v FROM t LEFT JOIN "
                "(su a JOIN su b ON a.pk = b.pk AND b.v > 15) "
                "ON a.pk = t.a ORDER BY t.a",
                [[1, None], [2, 20], [3, 30]]),
            guc("SELECT t.a, a.v FROM t LEFT JOIN "
                "(su a JOIN su b ON a.pk = b.pk AND b.v > 15) "
                "ON a.pk = t.a ORDER BY t.a"),
            # RTE_RESULT under the self-join
            exp("SELECT count(*) FROM su a JOIN (VALUES (1)) v(x) "
                "ON v.x = a.pk JOIN su b ON a.pk = b.pk", [[1]]),
        ],
    },
    {
        "name": "rs_or_extract",
        "arm": "restart",
        "source": "extract_restriction_or_clauses (orclauses.c) after the "
                  "restart loop: OR-of-AND join quals imply redundant base "
                  "restrictions; results must be preserved exactly.",
        "setup_sqls": _t() + _su(),
        "checks": [
            exp("SELECT count(*) FROM t JOIN su ON "
                "(t.a = 1 AND su.pk = 1) OR (t.a = 1 AND su.pk = 2)", [[2]]),
            exp("SELECT count(*) FROM t WHERE (a = 1 AND b = 10) "
                "OR (a = 1 AND b IS NULL)", [[1]]),
            exp("SELECT count(*) FROM t JOIN su ON "
                "(t.a = su.pk AND su.v = 10) OR (t.a = su.pk AND su.v = 30)",
                [[2]]),
        ],
    },

    # ================================================================
    # ARM restrict: restriction_is_always_true/false + joininfo
    # FALSE-replacement under clones/appendrels/check constraints.
    # ================================================================
    {
        "name": "rx_contradiction",
        "arm": "restrict",
        "source": "restriction_is_always_false: IS NULL AND IS NOT NULL on "
                  "the same provably-nonnull col; OR arm with mixed arms.",
        "setup_sqls": _nva() + _nvl(),
        "checks": [
            exp("SELECT count(*) FROM nva WHERE a IS NULL AND a IS NOT NULL",
                [[0]]),
            exp("SELECT count(*) FROM nva WHERE a IS NULL OR a > 1", [[2]]),
            exp("SELECT count(*) FROM nvl WHERE a IS NULL OR a IS NULL",
                [[1]]),
            exp("SELECT count(*) FROM nva WHERE a IS NOT NULL AND a > 1",
                [[2]]),
            exp("SELECT count(*) FROM nva WHERE (a IS NULL OR a > 0) "
                "AND a < 2", [[1]]),
        ],
    },
    {
        "name": "rx_joinqual_false",
        "arm": "restrict",
        "source": "add_join_clause_to_rels replaces an always-false join "
                  "qual with FALSE; clone quals under OJ must not confuse "
                  "the always-true/false provers.",
        "setup_sqls": _t() + _s(),
        "checks": [
            exp("SELECT count(*) FROM t JOIN s ON s.pk = t.a "
                "AND s.m IS NULL AND s.m IS NOT NULL", [[0]]),
            exp("SELECT count(*) FROM t LEFT JOIN s ON s.pk = t.a "
                "WHERE s.pk IS NOT NULL", [[2]]),
            pair("SELECT t.a, s.pk FROM t LEFT JOIN s ON s.pk = t.a "
                 "WHERE s.pk IS NOT NULL",
                 "SELECT t.a, s.pk FROM t JOIN s ON s.pk = t.a"),
            exp("SELECT count(*) FROM t LEFT JOIN s ON s.pk = t.a "
                "WHERE s.m IS NOT NULL", [[1]]),
        ],
    },
    {
        "name": "rx_appendrel_divergent",
        "arm": "restrict",
        "source": "apply_child_basequals -> get_relation_notnullatts per "
                  "child: children with divergent attnotnull get different "
                  "folds; the parent itself is skipped.",
        "setup_sqls": [
            "CREATE TABLE ip(a int, b int)",
            "CREATE TABLE ic(a int NOT NULL, b int) INHERITS (ip)",
            "INSERT INTO ip VALUES (NULL, NULL),(7,7)",
            "INSERT INTO ic VALUES (5,5)",
            "ANALYZE ip", "ANALYZE ic",
        ],
        "checks": [
            exp("SELECT count(*) FROM ip WHERE a IS NULL", [[1]]),
            exp("SELECT count(*) FROM ip WHERE a IS NOT NULL", [[2]]),
            exp("SELECT count(*) FROM ip WHERE ip IS NULL", [[1]]),
            exp("SELECT count(*) FROM ic WHERE a IS NULL", [[0]]),
            exp("SELECT count(*) FROM ip WHERE a IS NULL OR a > 6", [[2]]),
        ],
    },
    {
        "name": "rx_check_and_or",
        "arm": "restrict",
        "source": "CHECK-constraint exclusion at baserel + OR-branch "
                  "always-false arms.",
        "setup_sqls": _nva() + [
            "CREATE TABLE ck2(a int CHECK (a > 0))",
            "INSERT INTO ck2 VALUES (1),(5)",
            "ANALYZE ck2",
        ],
        "checks": [
            exp("SELECT count(*) FROM ck2 WHERE a < 0", [[0]]),
            exp("SELECT count(*) FROM ck2 WHERE a < 0 OR a = 5", [[1]]),
            exp("SELECT count(*) FROM nva WHERE (a > 1 OR a IS NULL) "
                "AND a < 10", [[2]]),
            exp("SELECT count(*) FROM nva WHERE a IS NULL OR "
                "(a > 0 AND a < 3)", [[2]]),
        ],
    },
]

# alias for the generic loader contract
PG_LIVE_PROBES = PG_TRANSFORM_PROBES
