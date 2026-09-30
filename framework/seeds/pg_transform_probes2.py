"""Planner-transformation equivalence probes, batch 2 (PG_TRANSFORM_PROBES).

Companion to seeds/pg_transform_probes.py — same check protocol, executed by
scripts/pg_transform_probes.py.  Eight case groups, each pinning a rewrite
whose two formulations MUST bag-equal; divergence = logic bug.

Groups and equivalence premises
-------------------------------
a1  IN <-> EXISTS:  ``x IN (SELECT m FROM s)`` == ``EXISTS(SELECT 1 FROM s
    WHERE s.m = x)``.  In WHERE position both keep NULL-equivalent: IN's NULL
    verdict and EXISTS's false both filter the row.  Arms rotate DISTINCT /
    GROUP BY inner (rel_is_distinct_for distinctness proofs),
    convert_ANY_sublink_to_join / convert_EXISTS_sublink_to_join.
a3  junk-laden EXISTS: ``EXISTS(SELECT DISTINCT/GROUP BY/ORDER BY/LIMIT k)``
    == ``EXISTS(SELECT 1 ...)`` — EXISTS only tests emptiness.
    simplify_EXISTS_query + the 20devel RTE_GROUP->RTE_RESULT rewrite.
    OFFSET>0 banned from pairs (real difference); documented as boundary.
a5  setop decomposition: UNION == DISTINCT o UNION ALL (NULL-safe since both
    dedup under IS NOT DISTINCT FROM); INTERSECT == DISTINCT+IN and
    EXCEPT == DISTINCT+NOT IN on NOT NULL cols ONLY — on nullable cols the
    NULL-safe equivalents use IS NOT DISTINCT FROM + EXISTS.
a8  minmax: ``min(a)`` == ``(SELECT a FROM t ORDER BY a ASC NULLS LAST
    LIMIT 1)``; ``max(a)`` == ``ORDER BY a DESC NULLS LAST LIMIT 1``.
    NOTE: the brief's "DESC NULLS FIRST" diverges on nullable cols (NULL row
    sorts first) — the NULL-safe max equivalent is NULLS LAST; NULLS FIRST is
    recorded as a boundary expected-check.  planagg.c
    preprocess_minmax_aggregates arms: index on/off, empty, all-NULL.
a14 UNION ALL flatten: outer qual over ``(t1 UNION ALL t2) s WHERE p(s)``
    == hand-pushed ``t1 WHERE p UNION ALL t2 WHERE p``.
    prepjointree.c flatten_simple_union_all; is_simple_union_all_recurse
    requires identical colTypes — mixed-type arms are a boundary (not
    flattened but must still bag-equal).
a11 EC implied equality: ``a=b AND b=c AND a=c`` == ``a=b AND b=c``
    (transitively-implied clause is redundant).  Cross-type int=numeric arm
    labelled separately.
a9  CTE three-state: ``WITH x AS MATERIALIZED (q)`` == ``AS NOT MATERIALIZED``
    == ``FROM (q) x``; q deterministic, single-reference (one multi-ref
    boundary pair on a deterministic q).
a12 OFFSET 0 fence: ``(SELECT * FROM t) s WHERE p`` ==
    ``(SELECT * FROM t OFFSET 0) s WHERE p``; OFFSET 0 blocks pullup ->
    "Subquery Scan" marker expected on the fenced side.

No ICU/nondeterministic-collation arms (builds lack ICU).
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


def _sm(extra=None):
    """Nullable single col {1, NULL, 3}."""
    out = [
        "CREATE TABLE sm(m int)",
        "INSERT INTO sm VALUES (1),(NULL),(3)",
        "ANALYZE sm",
    ]
    return out + (extra or [])


def _smd(extra=None):
    """Nullable single col with dups {1, 1, NULL, 3}."""
    out = [
        "CREATE TABLE smd(m int)",
        "INSERT INTO smd VALUES (1),(1),(NULL),(3)",
        "ANALYZE smd",
    ]
    return out + (extra or [])


def _u(extra=None):
    """NOT NULL setop operands with duplicates."""
    out = [
        "CREATE TABLE u1(a int NOT NULL)",
        "INSERT INTO u1 VALUES (1),(2),(2),(3)",
        "CREATE TABLE u2(a int NOT NULL)",
        "INSERT INTO u2 VALUES (2),(3),(3),(4)",
        "CREATE TABLE u3(a int NOT NULL)",
        "INSERT INTO u3 VALUES (3),(5)",
        "ANALYZE u1", "ANALYZE u2", "ANALYZE u3",
    ]
    return out + (extra or [])


def _q(extra=None):
    """Nullable setop operands with NULLs and dups."""
    out = [
        "CREATE TABLE q1(a int)",
        "INSERT INTO q1 VALUES (1),(NULL),(NULL),(3)",
        "CREATE TABLE q2(a int)",
        "INSERT INTO q2 VALUES (NULL),(2),(3)",
        "ANALYZE q1", "ANALYZE q2",
    ]
    return out + (extra or [])


def _tu(extra=None):
    """Union-flatten operands: identical colTypes across arms."""
    out = [
        "CREATE TABLE t1(a int, b int)",
        "INSERT INTO t1 VALUES (1,10),(2,20)",
        "CREATE TABLE t2(a int, b int)",
        "INSERT INTO t2 VALUES (2,99),(4,40)",
        "CREATE TABLE t3(a int, b int)",
        "INSERT INTO t3 VALUES (5,50)",
        "ANALYZE t1", "ANALYZE t2", "ANALYZE t3",
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


PG_TRANSFORM_PROBES = [
    # ================================================================
    # GROUP a1: IN <-> EXISTS
    # Premise: in WHERE position, ``x IN (SELECT m FROM s)`` ==
    # ``EXISTS(SELECT 1 FROM s WHERE s.m = x)`` even on nullable cols:
    # IN yields NULL -> row dropped; EXISTS NULL->false -> row dropped.
    # New code: convert_ANY_sublink_to_join, convert_EXISTS_sublink_to_join,
    # rel_is_distinct_for (DISTINCT/GROUP BY inner uniqueness proofs).
    # ================================================================
    {
        "name": "a1_in_exists_notnull",
        "arm": "a1",
        "source": "IN->semi join on NOT NULL cols vs correlated EXISTS; "
                  "semi->inner via PK uniqueness on the inner side.",
        "setup_sqls": _nva() + _su(),
        "checks": [
            # su.pk is unique -> semi join reduces to a plain join
            exp("SELECT count(*) FROM nva WHERE a IN (SELECT pk FROM su)",
                [[3]], marker="Join"),
            pair("SELECT count(*) FROM nva WHERE a IN (SELECT pk FROM su)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nva.a)"),
            pair("SELECT a FROM nva WHERE a IN "
                 "(SELECT pk FROM su WHERE su.v > 15)",
                 "SELECT a FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nva.a AND su.v > 15)"),
            exp("SELECT a FROM nva WHERE a IN "
                "(SELECT pk FROM su WHERE su.v > 15)", [[2], [3]]),
            # EXISTS result == dedup'd inner join count
            pair("SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nva.a)",
                 "SELECT count(*) FROM nva JOIN "
                 "(SELECT DISTINCT pk FROM su) d ON d.pk = nva.a"),
        ],
    },
    {
        "name": "a1_in_exists_nullable_inner",
        "arm": "a1",
        "source": "Nullable subquery output: IN's NULL verdict and EXISTS's "
                  "false both drop the outer row -> pair stays legal.",
        "setup_sqls": _nva() + _sm(),
        "checks": [
            # sm={1,NULL,3}: a=1,3 match; a=2 -> NULL -> filtered.  Both = 2.
            exp("SELECT count(*) FROM nva WHERE a IN (SELECT m FROM sm)",
                [[2]]),
            pair("SELECT count(*) FROM nva WHERE a IN (SELECT m FROM sm)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = nva.a)"),
            exp("SELECT a FROM nva WHERE a IN (SELECT m FROM sm)",
                [[1], [3]]),
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT m FROM sm WHERE m IS NOT NULL)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = nva.a AND m IS NOT NULL)"),
            # subquery outputs only NULL -> IN NULL->dropped; EXISTS false
            exp("SELECT count(*) FROM nva WHERE a IN "
                "(SELECT m FROM sm WHERE m IS NULL)", [[0]]),
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT m FROM sm WHERE m IS NULL)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = nva.a AND sm.m IS NULL)"),
        ],
    },
    {
        "name": "a1_in_exists_nullable_outer",
        "arm": "a1",
        "source": "Nullable outer testexpr: NULL row drops on both sides; "
                  "coalesce-wrapped testexpr arm; FILTER-clause sublink arm.",
        "setup_sqls": _nvl() + _su(),
        "checks": [
            exp("SELECT count(*) FROM nvl WHERE a IN (SELECT pk FROM su)",
                [[2]]),
            pair("SELECT count(*) FROM nvl WHERE a IN (SELECT pk FROM su)",
                 "SELECT count(*) FROM nvl WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nvl.a)"),
            # NULL outer row coalesced to 0 -> no match either way
            exp("SELECT count(*) FROM nvl WHERE coalesce(a,0) IN "
                "(SELECT pk FROM su)", [[2]]),
            pair("SELECT count(*) FROM nvl WHERE coalesce(a,0) IN "
                 "(SELECT pk FROM su)",
                 "SELECT count(*) FROM nvl WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = coalesce(nvl.a,0))"),
            # empty subquery: IN -> false, EXISTS -> false
            pair("SELECT count(*) FROM nvl WHERE a IN "
                 "(SELECT pk FROM su WHERE su.pk > 10)",
                 "SELECT count(*) FROM nvl WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nvl.a AND su.pk > 10)"),
            pair("SELECT count(*) FILTER (WHERE a IN (SELECT pk FROM su)) "
                 "FROM nvl",
                 "SELECT count(*) FILTER (WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nvl.a)) FROM nvl"),
        ],
    },
    {
        "name": "a1_in_distinct_groupby_inner",
        "arm": "a1",
        "source": "Dedup inner arms exercise rel_is_distinct_for: DISTINCT / "
                  "GROUP BY / UNION inner must not change IN's verdict; "
                  "DISTINCT-IN converts semi->plain join over Unique.",
        "setup_sqls": _nva() + _smd(),
        "checks": [
            # smd={1,1,NULL,3}: IN keeps a=1,3 -> 2 on every formulation
            # DISTINCT-proven inner reduces semi->plain join (dedup via
            # Unique or HashAggregate depending on cost)
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT DISTINCT m FROM smd)",
                 "SELECT count(*) FROM nva WHERE a IN (SELECT m FROM smd)",
                 marker="Join"),
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT m FROM smd GROUP BY m)",
                 "SELECT count(*) FROM nva WHERE a IN (SELECT m FROM smd)"),
            exp("SELECT count(*) FROM nva WHERE a IN "
                "(SELECT DISTINCT m FROM smd)", [[2]]),
            # EXISTS over a dedup'd derived table == plain EXISTS
            pair("SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM (SELECT DISTINCT m FROM smd) d "
                 "WHERE d.m = nva.a)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM smd WHERE smd.m = nva.a)"),
            # UNION inner cannot pull up; dedup still preserves verdict
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT m FROM smd UNION SELECT m FROM smd)",
                 "SELECT count(*) FROM nva WHERE a IN (SELECT m FROM smd)"),
        ],
    },
    {
        "name": "a1_exists_unique_inner",
        "arm": "a1",
        "source": "convert_EXISTS_sublink_to_join + inner-side uniqueness "
                  "proofs (PK, DISTINCT, GROUP BY) reduce EXISTS to a plain "
                  "join; must still equal IN/semi-join formulations.",
        "setup_sqls": _t() + _su(),
        "checks": [
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT 1 FROM su WHERE su.pk = t.a)", [[3]]),
            pair("SELECT count(*) FROM t WHERE t.a IN (SELECT pk FROM su)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = t.a)"),
            # GROUP BY proves inner distinct without a unique index
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM (SELECT pk FROM su GROUP BY pk) d "
                 "WHERE d.pk = t.a)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = t.a)"),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM (SELECT DISTINCT pk FROM su) d "
                 "WHERE d.pk = t.a)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = t.a)"),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT 1 FROM su WHERE su.pk = t.a AND su.v > 15)", [[2]]),
            pair("SELECT t.a FROM t WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = t.a AND su.v > 15)",
                 "SELECT t.a FROM t WHERE t.a IN "
                 "(SELECT pk FROM su WHERE su.v > 15)"),
        ],
    },
    {
        "name": "a1_rowcompare",
        "arm": "a1",
        "source": "Multi-col IN (RowCompareExpr) vs AND-ed EXISTS; nullable "
                  "member arm keeps NULL semantics identical in WHERE.",
        "setup_sqls": _nva() + _su() + _s(),
        "checks": [
            exp("SELECT count(*) FROM nva WHERE (a,v) IN "
                "(SELECT pk, v FROM su)", [[3]]),
            pair("SELECT count(*) FROM nva WHERE (a,v) IN "
                 "(SELECT pk, v FROM su)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = nva.a AND su.v = nva.v)"),
            exp("SELECT count(*) FROM nva WHERE (a,v) IN "
                "(SELECT pk, v FROM su WHERE pk < 3)", [[2]]),
            # s={(1,5),(2,NULL)}: (2,20) hits NULL member -> IN NULL -> drop;
            # EXISTS: pk=2 AND m=20 -> NULL -> not true -> drop.  Both 0.
            exp("SELECT count(*) FROM nva WHERE (a,v) IN "
                "(SELECT pk, m FROM s)", [[0]]),
            pair("SELECT count(*) FROM nva WHERE (a,v) IN "
                 "(SELECT pk, m FROM s)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM s WHERE s.pk = nva.a AND s.m = nva.v)"),
        ],
    },
    {
        "name": "a1_correlated_inner",
        "arm": "a1",
        "source": "Already-correlated IN subquery stays a SubPlan; the "
                  "EXISTS twin carries the same correlation + testexpr.",
        "setup_sqls": _nva() + _nvl() + _su() + [
            "CREATE TABLE s2b(pk int, m int)",
            "INSERT INTO s2b VALUES (1,1),(2,NULL),(3,2)",
            "ANALYZE s2b",
        ],
        "checks": [
            # nva.v >= su.v gate: v=10->{1}, 20->{1,2}, 30->{1,2,3} -> all IN
            exp("SELECT count(*) FROM nva WHERE a IN "
                "(SELECT pk FROM su WHERE su.v <= nva.v)", [[3]]),
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT pk FROM su WHERE su.v <= nva.v)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.v <= nva.v AND su.pk = nva.a)"),
            # s2b m={1,NULL,2}: nvl a=1 matches, a=NULL drops, a=3 -> NULL
            exp("SELECT count(*) FROM nvl WHERE a IN "
                "(SELECT m FROM s2b WHERE s2b.pk <= nvl.v)", [[1]]),
            pair("SELECT count(*) FROM nvl WHERE a IN "
                 "(SELECT m FROM s2b WHERE s2b.pk <= nvl.v)",
                 "SELECT count(*) FROM nvl WHERE EXISTS "
                 "(SELECT 1 FROM s2b WHERE s2b.pk <= nvl.v "
                 "AND s2b.m = nvl.a)"),
        ],
    },
    {
        "name": "a1_text_collation",
        "arm": "a1",
        "source": "Text cols under the default (deterministic) collation: "
                  "rel_is_distinct_for's distinctness proof is collation-"
                  "aware; dedup inner must not change IN/EXISTS verdicts.",
        "setup_sqls": [
            "CREATE TABLE ct(c text NOT NULL)",
            "INSERT INTO ct VALUES ('a'),('b'),('b'),('c')",
            "CREATE TABLE cs(c text)",
            "INSERT INTO cs VALUES ('a'),('x'),('x'),(NULL)",
            "ANALYZE ct", "ANALYZE cs",
        ],
        "checks": [
            exp("SELECT c FROM ct WHERE c IN (SELECT c FROM cs)",
                [["a"]]),
            pair("SELECT count(*) FROM ct WHERE c IN "
                 "(SELECT DISTINCT c FROM cs)",
                 "SELECT count(*) FROM ct WHERE c IN (SELECT c FROM cs)",
                 marker="Join"),
            pair("SELECT count(*) FROM ct WHERE c IN (SELECT c FROM cs)",
                 "SELECT count(*) FROM ct WHERE EXISTS "
                 "(SELECT 1 FROM cs WHERE cs.c = ct.c)"),
            pair("SELECT count(*) FROM ct WHERE c IN "
                 "(SELECT c FROM cs GROUP BY c)",
                 "SELECT count(*) FROM ct WHERE EXISTS "
                 "(SELECT 1 FROM cs WHERE cs.c = ct.c)"),
            # NULL member: 'b' IN {a,x,NULL} -> NULL -> dropped; EXISTS false
            exp("SELECT c FROM ct WHERE c IN (SELECT c FROM cs) "
                "ORDER BY c", [["a"]]),
        ],
    },

    # ================================================================
    # GROUP a3: junk-laden EXISTS
    # Premise: EXISTS only tests emptiness -> DISTINCT / GROUP BY (no
    # HAVING) / ORDER BY / LIMIT k>0 / OFFSET 0 are junk and must not change
    # the verdict.  simplify_EXISTS_query strips them; 20devel rewrites
    # RTE_GROUP -> RTE_RESULT in place.  Real boundaries (recorded as
    # expected-checks, never paired): OFFSET>0, LIMIT 0, HAVING that can
    # filter, grouping sets containing () (emits a row on empty input), and
    # plain aggregates (always emit a row).
    # ================================================================
    {
        "name": "a3_distinct_junk",
        "arm": "a3",
        "source": "DISTINCT inside EXISTS is pure junk: correlated and "
                  "uncorrelated arms vs SELECT 1.",
        "setup_sqls": _t() + _sm(),
        "checks": [
            # sm={1,NULL,3}: t.a=1,3 match -> 2
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT DISTINCT m FROM sm WHERE sm.m = t.a)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT DISTINCT m FROM sm WHERE sm.m = t.a)", [[2]]),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT DISTINCT m FROM sm)",
                 "SELECT count(*) FROM t WHERE EXISTS (SELECT 1 FROM sm)"),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT DISTINCT m FROM sm)", [[3]]),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT DISTINCT m FROM sm WHERE m > 100)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE m > 100)"),
        ],
    },
    {
        "name": "a3_groupby_nohaving",
        "arm": "a3",
        "source": "GROUP BY without HAVING preserves emptiness exactly "
                  "(empty input -> no groups).  RTE_GROUP arm on 20devel.",
        "setup_sqls": _t() + _sm(),
        "checks": [
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m FROM sm WHERE sm.m = t.a GROUP BY m)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m, count(*) FROM sm WHERE sm.m = t.a GROUP BY m)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT m FROM sm WHERE sm.m = t.a GROUP BY m)", [[2]]),
            # empty input -> no groups -> same as plain false
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m FROM sm WHERE sm.m > 100 GROUP BY m)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m > 100)"),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT m FROM sm WHERE sm.m > t.a GROUP BY m)", [[2]]),
        ],
    },
    {
        "name": "a3_orderby_limit_junk",
        "arm": "a3",
        "source": "ORDER BY / LIMIT k>0 / OFFSET 0 are junk inside EXISTS; "
                  "emptiness is unchanged.",
        "setup_sqls": _t() + _sm() + _smd(),
        "checks": [
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a ORDER BY m)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m FROM sm WHERE sm.m = t.a "
                 "ORDER BY m DESC NULLS FIRST LIMIT 1)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m FROM smd WHERE smd.m = t.a "
                 "ORDER BY m LIMIT 2 OFFSET 0)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM smd WHERE smd.m = t.a)"),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT m FROM sm WHERE sm.m = t.a "
                "ORDER BY m LIMIT 1)", [[2]]),
        ],
    },
    {
        "name": "a3_grouping_sets_rte",
        "arm": "a3",
        "source": "GROUPING SETS / ROLLUP inside EXISTS hit the RTE_GROUP "
                  "path (20devel rewrites it to RTE_RESULT in place).  A set "
                  "containing () emits a row even on empty input -> always "
                  "true, recorded as expected (boundary).",
        "setup_sqls": _t() + _sm(),
        "checks": [
            # (m)-only sets preserve emptiness -> pair-legal
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m FROM sm WHERE sm.m = t.a "
                 "GROUP BY GROUPING SETS ((m)))",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m FROM sm WHERE sm.m = t.a "
                 "GROUP BY GROUPING SETS ((m)) HAVING count(*) >= 1)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
            # boundary: sets containing () emit the grand-total row on empty
            # input -> EXISTS is always true (3), NOT equiv to plain (2)
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT m FROM sm WHERE sm.m = t.a "
                "GROUP BY GROUPING SETS ((m), ()))", [[3]]),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT m FROM sm WHERE sm.m = t.a "
                "GROUP BY ROLLUP (m))", [[3]]),
            # HAVING that actually filters is not junk -> still exact
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT m FROM sm WHERE sm.m = t.a "
                "GROUP BY GROUPING SETS ((m)) HAVING m > 10)", [[0]]),
        ],
    },
    {
        "name": "a3_aggregate_boundary",
        "arm": "a3",
        "source": "An aggregate without GROUP BY always emits one row -> "
                  "EXISTS(agg) is always true; HAVING count(*)>0 restores "
                  "emptiness semantics -> pair-legal.",
        "setup_sqls": _t() + _sm() + _smd(),
        "checks": [
            # always true: count(*) emits a row even for empty input
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT count(*) FROM sm WHERE sm.m = t.a)", [[3]]),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT sum(m) FROM sm WHERE sm.m > 100)", [[3]]),
            # HAVING count(*)>0 fires iff the group exists -> equiv
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT count(*) FROM sm WHERE sm.m = t.a "
                 "HAVING count(*) > 0)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
            # smd has dup m=1: t.a=1 -> count 2 -> true; t.a=3 -> 1 -> false
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT count(*) FROM smd WHERE smd.m = t.a "
                "HAVING count(*) >= 2)", [[1]]),
        ],
    },
    {
        "name": "a3_combo_and_offset_boundary",
        "arm": "a3",
        "source": "Combined junk arms; OFFSET>0 / LIMIT 0 are real "
                  "differences recorded as expected-only boundaries.",
        "setup_sqls": _t() + _sm() + _smd(),
        "checks": [
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m FROM smd WHERE smd.m = t.a "
                 "GROUP BY m ORDER BY m LIMIT 1)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM smd WHERE smd.m = t.a)"),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT DISTINCT m FROM smd WHERE smd.m = t.a "
                 "ORDER BY m LIMIT 1 OFFSET 0)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM smd WHERE smd.m = t.a)"),
            # boundary: OFFSET 1 drops the sole match for t.a=3 -> 1 not 2
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT m FROM smd WHERE smd.m = t.a "
                "ORDER BY m LIMIT 1 OFFSET 1)", [[1]]),
            # boundary: LIMIT 0 -> always false
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT m FROM sm WHERE sm.m = t.a LIMIT 0)", [[0]]),
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT m FROM sm WHERE sm.m = t.a "
                 "ORDER BY m NULLS FIRST)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = t.a)"),
        ],
    },

    # ================================================================
    # GROUP a5: setop decomposition
    # Premise: UNION == DISTINCT o UNION ALL (NULL-safe: both dedup under
    # IS NOT DISTINCT FROM).  INTERSECT == DISTINCT + IN and
    # EXCEPT == DISTINCT + NOT IN only on NOT NULL cols; on nullable cols
    # the legal equivalents are DISTINCT + EXISTS(IS NOT DISTINCT FROM).
    # ================================================================
    {
        "name": "a5_union_notnull",
        "arm": "a5",
        "source": "UNION == DISTINCT o UNION ALL on NOT NULL cols with "
                  "duplicates on both sides.",
        "setup_sqls": _u(),
        "checks": [
            exp("SELECT a FROM u1 UNION SELECT a FROM u2",
                [[1], [2], [3], [4]], marker="Append"),
            pair("SELECT a FROM u1 UNION SELECT a FROM u2",
                 "SELECT DISTINCT a FROM "
                 "(SELECT a FROM u1 UNION ALL SELECT a FROM u2) q"),
            # three-arm union
            pair("SELECT a FROM u1 UNION SELECT a FROM u2 "
                 "UNION SELECT a FROM u3",
                 "SELECT DISTINCT a FROM "
                 "(SELECT a FROM u1 UNION ALL SELECT a FROM u2 "
                 "UNION ALL SELECT a FROM u3) q"),
            exp("SELECT a FROM u1 UNION SELECT a FROM u2 "
                "UNION SELECT a FROM u3", [[1], [2], [3], [4], [5]]),
            # UNION ALL is additive: 4 + 4 = 8
            exp("SELECT count(*) FROM "
                "(SELECT a FROM u1 UNION ALL SELECT a FROM u2) q", [[8]]),
            # UNION == GROUP BY dedup over UNION ALL
            pair("SELECT a FROM u1 UNION SELECT a FROM u2",
                 "SELECT a FROM "
                 "(SELECT a FROM u1 UNION ALL SELECT a FROM u2) q "
                 "GROUP BY a"),
        ],
    },
    {
        "name": "a5_union_nullable",
        "arm": "a5",
        "source": "UNION dedup treats NULLs as equal -> DISTINCT o UNION ALL "
                  "stays equivalent on nullable cols.",
        "setup_sqls": _q(),
        "checks": [
            # q1={1,NULL,NULL,3}, q2={NULL,2,3} -> union {1,NULL,2,3}
            exp("SELECT count(*) FROM "
                "(SELECT a FROM q1 UNION SELECT a FROM q2) q", [[4]]),
            pair("SELECT a FROM q1 UNION SELECT a FROM q2",
                 "SELECT DISTINCT a FROM "
                 "(SELECT a FROM q1 UNION ALL SELECT a FROM q2) q"),
            pair("SELECT count(*) FROM "
                 "(SELECT a FROM q1 UNION SELECT a FROM q2) q",
                 "SELECT count(*) FROM (SELECT DISTINCT a FROM "
                 "(SELECT a FROM q1 UNION ALL SELECT a FROM q2) d) z"),
        ],
    },
    {
        "name": "a5_union_multicol",
        "arm": "a5",
        "source": "Row-wise dedup: (a,b) UNION over a nullable b uses "
                  "IS NOT DISTINCT FROM per field; DISTINCT/GROUP BY twin "
                  "must agree.",
        "setup_sqls": [
            "CREATE TABLE p1(a int NOT NULL, b int)",
            "INSERT INTO p1 VALUES (1,10),(1,10),(2,NULL)",
            "CREATE TABLE p2(a int NOT NULL, b int)",
            "INSERT INTO p2 VALUES (1,10),(2,NULL),(3,30)",
            "ANALYZE p1", "ANALYZE p2",
        ],
        "checks": [
            exp("SELECT count(*) FROM "
                "(SELECT a,b FROM p1 UNION SELECT a,b FROM p2) q", [[3]]),
            pair("SELECT a,b FROM p1 UNION SELECT a,b FROM p2",
                 "SELECT DISTINCT a,b FROM "
                 "(SELECT a,b FROM p1 UNION ALL SELECT a,b FROM p2) q"),
            pair("SELECT a,b FROM p1 UNION SELECT a,b FROM p2",
                 "SELECT a,b FROM "
                 "(SELECT a,b FROM p1 UNION ALL SELECT a,b FROM p2) q "
                 "GROUP BY a,b"),
            exp("SELECT a,b FROM p1 UNION SELECT a,b FROM p2",
                [[1, 10], [2, None], [3, 30]]),
        ],
    },
    {
        "name": "a5_intersect_notnull",
        "arm": "a5",
        "source": "INTERSECT == DISTINCT + IN on NOT NULL cols (single and "
                  "row-compare forms).",
        "setup_sqls": _u() + [
            "CREATE TABLE pn1(a int NOT NULL, b int NOT NULL)",
            "INSERT INTO pn1 VALUES (1,10),(1,10),(2,20)",
            "CREATE TABLE pn2(a int NOT NULL, b int NOT NULL)",
            "INSERT INTO pn2 VALUES (1,10),(2,20),(3,30)",
            "ANALYZE pn1", "ANALYZE pn2",
        ],
        "checks": [
            exp("SELECT a FROM u1 INTERSECT SELECT a FROM u2",
                [[2], [3]]),
            pair("SELECT a FROM u1 INTERSECT SELECT a FROM u2",
                 "SELECT DISTINCT a FROM u1 "
                 "WHERE a IN (SELECT a FROM u2)"),
            pair("SELECT a FROM u1 INTERSECT SELECT a FROM u2",
                 "SELECT DISTINCT a FROM u1 WHERE EXISTS "
                 "(SELECT 1 FROM u2 WHERE u2.a = u1.a)"),
            exp("SELECT a,b FROM pn1 INTERSECT SELECT a,b FROM pn2",
                [[1, 10], [2, 20]]),
            pair("SELECT a,b FROM pn1 INTERSECT SELECT a,b FROM pn2",
                 "SELECT DISTINCT a,b FROM pn1 WHERE (a,b) IN "
                 "(SELECT a,b FROM pn2)"),
        ],
    },
    {
        "name": "a5_except_notnull",
        "arm": "a5",
        "source": "EXCEPT == DISTINCT + NOT IN on NOT NULL cols; EXCEPT ALL "
                  "keeps bag semantics (per-instance minus).",
        "setup_sqls": _u(),
        "checks": [
            exp("SELECT a FROM u1 EXCEPT SELECT a FROM u2", [[1]]),
            pair("SELECT a FROM u1 EXCEPT SELECT a FROM u2",
                 "SELECT DISTINCT a FROM u1 "
                 "WHERE a NOT IN (SELECT a FROM u2)"),
            exp("SELECT a FROM u2 EXCEPT SELECT a FROM u1", [[4]]),
            # EXCEPT ALL: u1 bag {1,2,2,3} - u2 bag {2,3,3,4} -> {1,2}
            exp("SELECT a FROM u1 EXCEPT ALL SELECT a FROM u2",
                [[1], [2]]),
            pair("SELECT a FROM u1 EXCEPT ALL SELECT a FROM u2",
                 "SELECT a FROM (SELECT a, row_number() OVER "
                 "(PARTITION BY a) rn FROM u1) z WHERE NOT EXISTS "
                 "(SELECT 1 FROM (SELECT a, row_number() OVER "
                 "(PARTITION BY a) rn FROM u2) w "
                 "WHERE w.a = z.a AND w.rn = z.rn)"),
        ],
    },
    {
        "name": "a5_nullable_boundary",
        "arm": "a5",
        "source": "Nullable cols: INTERSECT/EXCEPT are NOT equal to "
                  "IN/NOT IN forms (NULL member) — the legal equivalents use "
                  "IS NOT DISTINCT FROM + EXISTS.  Plain-IN divergence is "
                  "recorded as expected, not paired.",
        "setup_sqls": _q(),
        "checks": [
            # q1~q2 = {NULL,3}: NULL IS NOT DISTINCT FROM NULL matches
            exp("SELECT a FROM q1 INTERSECT SELECT a FROM q2",
                [[None], [3]]),
            pair("SELECT a FROM q1 INTERSECT SELECT a FROM q2",
                 "SELECT DISTINCT a FROM q1 WHERE EXISTS "
                 "(SELECT 1 FROM q2 WHERE q2.a IS NOT DISTINCT FROM q1.a)"),
            # q1-q2 = {1}
            exp("SELECT a FROM q1 EXCEPT SELECT a FROM q2", [[1]]),
            pair("SELECT a FROM q1 EXCEPT SELECT a FROM q2",
                 "SELECT DISTINCT a FROM q1 WHERE NOT EXISTS "
                 "(SELECT 1 FROM q2 WHERE q2.a IS NOT DISTINCT FROM q1.a)"),
            # documented divergence: plain IN drops the NULL row -> {3} only
            exp("SELECT DISTINCT a FROM q1 WHERE a IN (SELECT a FROM q2)",
                [[3]]),
            exp("SELECT DISTINCT a FROM q1 WHERE a NOT IN "
                "(SELECT a FROM q2)", []),
        ],
    },
    {
        "name": "a5_precedence_nested",
        "arm": "a5",
        "source": "INTERSECT binds tighter than UNION; EXCEPT is "
                  "left-associative.  Parenthesized twins must agree; "
                  "re-associated forms are recorded boundaries.",
        "setup_sqls": _u(),
        "checks": [
            pair("SELECT a FROM u1 INTERSECT SELECT a FROM u2 "
                 "UNION SELECT a FROM u3",
                 "SELECT a FROM (SELECT a FROM u1 INTERSECT "
                 "SELECT a FROM u2) i UNION SELECT a FROM u3"),
            exp("SELECT a FROM u1 INTERSECT SELECT a FROM u2 "
                "UNION SELECT a FROM u3", [[2], [3], [5]]),
            pair("SELECT a FROM u1 EXCEPT SELECT a FROM u2 "
                 "EXCEPT SELECT a FROM u3",
                 "SELECT a FROM (SELECT a FROM u1 EXCEPT "
                 "SELECT a FROM u2) e EXCEPT SELECT a FROM u3"),
            exp("SELECT a FROM u1 EXCEPT SELECT a FROM u2 "
                "EXCEPT SELECT a FROM u3", [[1]]),
            # boundary: re-associated EXCEPT differs: u1-(u2-u3)={1,3}
            exp("SELECT a FROM u1 EXCEPT "
                "(SELECT a FROM u2 EXCEPT SELECT a FROM u3)",
                [[1], [3]]),
        ],
    },

    # ================================================================
    # GROUP a8: minmax
    # Premise: min(a) == scalar subq ORDER BY a ASC NULLS LAST LIMIT 1;
    # max(a) == ORDER BY a DESC NULLS LAST LIMIT 1 (NULLS FIRST is a real
    # divergence on nullable cols — recorded as boundary).
    # preprocess_minmax_aggregates arms: indexed vs not, empty, all-NULL.
    # ================================================================
    {
        "name": "a8_min_indexed",
        "arm": "a8",
        "source": "min(a) over a btree-indexed col -> InitPlan+index-scan "
                  "rewrite; scalar-subquery twin must bag-equal.",
        "setup_sqls": [
            "CREATE TABLE mm(a int, b int)",
            "INSERT INTO mm SELECT g, g*2 FROM generate_series(1,3000) g",
            "INSERT INTO mm SELECT NULL, g FROM generate_series(1,50) g",
            "CREATE INDEX mm_a ON mm(a)",
            "ANALYZE mm",
        ],
        "checks": [
            exp("SELECT min(a) FROM mm", [[1]], marker="InitPlan"),
            pair("SELECT min(a) FROM mm",
                 "SELECT (SELECT a FROM mm WHERE a IS NOT NULL "
                 "ORDER BY a ASC NULLS LAST LIMIT 1)"),
            pair("SELECT min(a) FROM mm",
                 "SELECT (SELECT a FROM mm "
                 "ORDER BY a ASC NULLS LAST LIMIT 1)"),
            # qualified minmax: quals stay inside the rewrite
            pair("SELECT min(a) FROM mm WHERE a > 100",
                 "SELECT (SELECT a FROM mm WHERE a > 100 "
                 "ORDER BY a ASC NULLS LAST LIMIT 1)"),
            exp("SELECT min(a) FROM mm WHERE a > 100", [[101]]),
            exp("SELECT min(a) FROM mm WHERE a > 100000", [[None]]),
        ],
    },
    {
        "name": "a8_max_indexed",
        "arm": "a8",
        "source": "max(a) twin uses DESC NULLS LAST; NULLS FIRST is a real "
                  "divergence on nullable cols (NULL sorts first) -> "
                  "recorded as boundary expected-check.",
        "setup_sqls": [
            "CREATE TABLE mm(a int, b int)",
            "INSERT INTO mm SELECT g, g*2 FROM generate_series(1,3000) g",
            "INSERT INTO mm SELECT NULL, g FROM generate_series(1,50) g",
            "CREATE INDEX mm_a ON mm(a)",
            "ANALYZE mm",
        ],
        "checks": [
            exp("SELECT max(a) FROM mm", [[3000]], marker="InitPlan"),
            pair("SELECT max(a) FROM mm",
                 "SELECT (SELECT a FROM mm "
                 "ORDER BY a DESC NULLS LAST LIMIT 1)"),
            pair("SELECT max(a) FROM mm",
                 "SELECT (SELECT a FROM mm WHERE a IS NOT NULL "
                 "ORDER BY a DESC LIMIT 1)"),
            # boundary: DESC NULLS FIRST picks a NULL row -> NULL != 3000
            exp("SELECT (SELECT a FROM mm "
                "ORDER BY a DESC NULLS FIRST LIMIT 1)", [[None]]),
            pair("SELECT max(a) FROM mm WHERE a < 3000",
                 "SELECT (SELECT a FROM mm WHERE a < 3000 "
                 "ORDER BY a DESC NULLS LAST LIMIT 1)"),
            exp("SELECT max(a) FROM mm WHERE a < 3000", [[2999]]),
        ],
    },
    {
        "name": "a8_noidx_empty_allnull",
        "arm": "a8",
        "source": "No-index / empty / all-NULL arms: the rewrite must not "
                  "fire, but the scalar-subquery equivalence still holds.",
        "setup_sqls": [
            "CREATE TABLE mmn(a int, b int)",
            "INSERT INTO mmn SELECT g, g*2 FROM generate_series(1,500) g",
            "INSERT INTO mmn VALUES (NULL,-1)",
            "CREATE TABLE mme(a int)",
            "CREATE INDEX mme_a ON mme(a)",
            "CREATE TABLE mmz(a int)",
            "INSERT INTO mmz VALUES (NULL),(NULL),(NULL)",
            "CREATE INDEX mmz_a ON mmz(a)",
            "ANALYZE mmn", "ANALYZE mme", "ANALYZE mmz",
        ],
        "checks": [
            pair("SELECT min(a) FROM mmn",
                 "SELECT (SELECT a FROM mmn "
                 "ORDER BY a ASC NULLS LAST LIMIT 1)"),
            pair("SELECT max(a) FROM mmn",
                 "SELECT (SELECT a FROM mmn "
                 "ORDER BY a DESC NULLS LAST LIMIT 1)"),
            exp("SELECT min(a) FROM mme", [[None]]),
            pair("SELECT min(a) FROM mme",
                 "SELECT (SELECT a FROM mme "
                 "ORDER BY a ASC NULLS LAST LIMIT 1)"),
            exp("SELECT max(a) FROM mmz", [[None]]),
            pair("SELECT max(a) FROM mmz",
                 "SELECT (SELECT a FROM mmz "
                 "ORDER BY a DESC NULLS LAST LIMIT 1)"),
            pair("SELECT min(a) FROM mmz",
                 "SELECT (SELECT a FROM mmz WHERE a IS NOT NULL "
                 "ORDER BY a ASC LIMIT 1)"),
        ],
    },
    {
        "name": "a8_multi_agg",
        "arm": "a8",
        "source": "Multiple minmax aggs in one query rewrite to multiple "
                  "InitPlans; expression-on-agg arm.",
        "setup_sqls": [
            "CREATE TABLE mm(a int, b int)",
            "INSERT INTO mm SELECT g, g*2 FROM generate_series(1,3000) g",
            "INSERT INTO mm SELECT NULL, g FROM generate_series(1,50) g",
            "CREATE INDEX mm_a ON mm(a)",
            "CREATE INDEX mm_b ON mm(b)",
            "ANALYZE mm",
        ],
        "checks": [
            exp("SELECT min(a), max(a), count(*) FROM mm",
                [[1, 3000, 3050]]),
            pair("SELECT min(a), max(b) FROM mm",
                 "SELECT (SELECT a FROM mm WHERE a IS NOT NULL "
                 "ORDER BY a ASC LIMIT 1), "
                 "(SELECT b FROM mm WHERE b IS NOT NULL "
                 "ORDER BY b DESC LIMIT 1)"),
            pair("SELECT min(a) + max(a) FROM mm",
                 "SELECT (SELECT a FROM mm WHERE a IS NOT NULL "
                 "ORDER BY a ASC LIMIT 1) + "
                 "(SELECT a FROM mm WHERE a IS NOT NULL "
                 "ORDER BY a DESC LIMIT 1)"),
            # b carries 1..50 in the NULL-a rows, so min(b) is 1 not 2
            exp("SELECT min(b) FROM mm", [[1]]),
            pair("SELECT min(b) FROM mm",
                 "SELECT (SELECT b FROM mm "
                 "ORDER BY b ASC NULLS LAST LIMIT 1)"),
        ],
    },
    {
        "name": "a8_grouped_boundary",
        "arm": "a8",
        "source": "Grouped min/max is NOT rewritten (per-group aggs); "
                  "correlated scalar-subquery twin must still bag-equal.",
        "setup_sqls": [
            "CREATE TABLE mmg(g int, a int)",
            "INSERT INTO mmg VALUES (1,5),(1,3),(2,NULL),(2,8),(3,NULL)",
            "ANALYZE mmg",
        ],
        "checks": [
            exp("SELECT g, min(a) FROM mmg GROUP BY g",
                [[1, 3], [2, 8], [3, None]]),
            pair("SELECT g, min(a) FROM mmg GROUP BY g",
                 "SELECT g, (SELECT m2.a FROM mmg m2 "
                 "WHERE m2.g = mmg.g AND m2.a IS NOT NULL "
                 "ORDER BY m2.a ASC LIMIT 1) FROM mmg GROUP BY g"),
            pair("SELECT g, max(a) FROM mmg GROUP BY g",
                 "SELECT g, (SELECT m2.a FROM mmg m2 "
                 "WHERE m2.g = mmg.g AND m2.a IS NOT NULL "
                 "ORDER BY m2.a DESC LIMIT 1) FROM mmg GROUP BY g"),
            exp("SELECT count(*) FROM (SELECT g, min(a) FROM mmg "
                "GROUP BY g) q", [[3]]),
        ],
    },
    {
        "name": "a8_text_minmax",
        "arm": "a8",
        "source": "Text col under the default deterministic collation; "
                  "index-supported min/max must equal the ordered scalar "
                  "subquery.",
        "setup_sqls": [
            # 2000 deterministic text rows so the index path wins and the
            # minmax InitPlan rewrite fires
            "CREATE TABLE mmt(t text)",
            "INSERT INTO mmt SELECT md5(g::text) "
            "FROM generate_series(1,2000) g",
            "INSERT INTO mmt VALUES (NULL)",
            "CREATE INDEX mmt_t ON mmt(t)",
            "ANALYZE mmt",
        ],
        "checks": [
            pair("SELECT min(t) FROM mmt",
                 "SELECT (SELECT t FROM mmt "
                 "ORDER BY t ASC NULLS LAST LIMIT 1)",
                 marker="InitPlan"),
            exp("SELECT length(min(t)), length(max(t)) FROM mmt",
                [[32, 32]]),
            pair("SELECT max(t) FROM mmt",
                 "SELECT (SELECT t FROM mmt WHERE t IS NOT NULL "
                 "ORDER BY t DESC LIMIT 1)"),
            pair("SELECT min(t), max(t) FROM mmt",
                 "SELECT (SELECT t FROM mmt WHERE t IS NOT NULL "
                 "ORDER BY t ASC LIMIT 1), "
                 "(SELECT t FROM mmt WHERE t IS NOT NULL "
                 "ORDER BY t DESC LIMIT 1)"),
        ],
    },

    # ================================================================
    # GROUP a14: UNION ALL flatten + outer-qual pushdown
    # Premise: ``(t1 UNION ALL t2) s WHERE p(s)`` ==
    # ``t1 WHERE p UNION ALL t2 WHERE p`` — pushing a deterministic qual to
    # each arm preserves the bag.  is_simple_union_all_recurse requires
    # identical colTypes; mixed-type arms are a boundary (not flattened —
    # the equivalence still holds semantically).
    # ================================================================
    {
        "name": "a14_push_basic",
        "arm": "a14",
        "source": "Outer WHERE over a flattened UNION ALL vs hand-pushed "
                  "per-arm quals.",
        "setup_sqls": _tu(),
        "checks": [
            exp("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                "SELECT a FROM t2) s WHERE s.a > 1",
                [[2], [2], [4]], marker="Append"),
            pair("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM t2) s WHERE s.a > 1",
                 "SELECT a FROM t1 WHERE a > 1 UNION ALL "
                 "SELECT a FROM t2 WHERE a > 1"),
            pair("SELECT a, b FROM (SELECT a,b FROM t1 UNION ALL "
                 "SELECT a,b FROM t2) s WHERE s.a > 1 AND s.b < 50",
                 "SELECT a,b FROM t1 WHERE a > 1 AND b < 50 UNION ALL "
                 "SELECT a,b FROM t2 WHERE a > 1 AND b < 50"),
            # composite-expression qual
            pair("SELECT a FROM (SELECT a,b FROM t1 UNION ALL "
                 "SELECT a,b FROM t2) s WHERE s.a + s.b > 30",
                 "SELECT a FROM t1 WHERE a + b > 30 UNION ALL "
                 "SELECT a FROM t2 WHERE a + b > 30"),
            exp("SELECT count(*) FROM (SELECT a FROM t1 UNION ALL "
                "SELECT a FROM t2) s WHERE s.a = 2", [[2]]),
        ],
    },
    {
        "name": "a14_multiarm_nested",
        "arm": "a14",
        "source": "3-arm and nested UNION ALL subqueries; BETWEEN and "
                  "two-level wrapping.",
        "setup_sqls": _tu(),
        "checks": [
            pair("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM t2 UNION ALL SELECT a FROM t3) s "
                 "WHERE s.a BETWEEN 2 AND 4",
                 "SELECT a FROM t1 WHERE a BETWEEN 2 AND 4 UNION ALL "
                 "SELECT a FROM t2 WHERE a BETWEEN 2 AND 4 UNION ALL "
                 "SELECT a FROM t3 WHERE a BETWEEN 2 AND 4"),
            exp("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                "SELECT a FROM t2 UNION ALL SELECT a FROM t3) s "
                "WHERE s.a BETWEEN 2 AND 4", [[2], [2], [4]]),
            # nested union inside an arm
            pair("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                 "(SELECT a FROM t2 UNION ALL SELECT a FROM t3)) s "
                 "WHERE s.a > 1",
                 "SELECT a FROM t1 WHERE a > 1 UNION ALL "
                 "SELECT a FROM t2 WHERE a > 1 UNION ALL "
                 "SELECT a FROM t3 WHERE a > 1"),
            # double-wrapped outer quals at two levels
            pair("SELECT a FROM (SELECT a FROM "
                 "(SELECT a FROM t1 UNION ALL SELECT a FROM t2) i "
                 "WHERE i.a > 0) o WHERE o.a < 10",
                 "SELECT a FROM (SELECT a FROM t1 WHERE a > 0 UNION ALL "
                 "SELECT a FROM t2 WHERE a > 0) o WHERE a < 10"),
            pair("SELECT count(*) FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM t2 UNION ALL SELECT a FROM t3) s "
                 "WHERE s.a IS NOT NULL",
                 "SELECT (SELECT count(*) FROM t1 WHERE a IS NOT NULL) + "
                 "(SELECT count(*) FROM t2 WHERE a IS NOT NULL) + "
                 "(SELECT count(*) FROM t3 WHERE a IS NOT NULL)"),
        ],
    },
    {
        "name": "a14_qual_shapes",
        "arm": "a14",
        "source": "Qual-shape rotation over the union subquery: IN-list, "
                  "EXISTS-sublink and scalar-subquery quals.",
        "setup_sqls": _tu() + _su(),
        "checks": [
            pair("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM t2) s WHERE s.a IN (1,4)",
                 "SELECT a FROM t1 WHERE a IN (1,4) UNION ALL "
                 "SELECT a FROM t2 WHERE a IN (1,4)"),
            exp("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                "SELECT a FROM t2) s WHERE s.a IN (1,4)",
                [[1], [4]]),
            pair("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM t2) s WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = s.a)",
                 "SELECT a FROM t1 WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = t1.a) UNION ALL "
                 "SELECT a FROM t2 WHERE EXISTS "
                 "(SELECT 1 FROM su WHERE su.pk = t2.a)"),
            pair("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM t2) s WHERE s.a = (SELECT max(pk) FROM su)",
                 "SELECT a FROM t1 WHERE a = (SELECT max(pk) FROM su) "
                 "UNION ALL "
                 "SELECT a FROM t2 WHERE a = (SELECT max(pk) FROM su)"),
        ],
    },
    {
        "name": "a14_in_exists_over_union",
        "arm": "a14",
        "source": "IN over a union-all subquery == OR of per-arm INs "
                  "(3VL-safe); EXISTS over the flattened append.",
        "setup_sqls": _nva() + _sm() + _smd(),
        "checks": [
            # sm={1,NULL,3} smd={1,1,NULL,3}: union has NULL members
            exp("SELECT count(*) FROM nva WHERE a IN "
                "(SELECT m FROM sm UNION ALL SELECT m FROM smd)", [[2]]),
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT m FROM sm UNION ALL SELECT m FROM smd)",
                 "SELECT count(*) FROM nva WHERE a IN (SELECT m FROM sm) "
                 "OR a IN (SELECT m FROM smd)"),
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT m FROM sm UNION ALL SELECT m FROM smd)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM (SELECT m FROM sm UNION ALL "
                 "SELECT m FROM smd) q WHERE q.m = nva.a)"),
            pair("SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM (SELECT m FROM sm UNION ALL "
                 "SELECT m FROM smd) q WHERE q.m = nva.a)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM sm WHERE sm.m = nva.a) OR EXISTS "
                 "(SELECT 1 FROM smd WHERE smd.m = nva.a)"),
        ],
    },
    {
        "name": "a14_mixed_coltype_boundary",
        "arm": "a14",
        "source": "is_simple_union_all_recurse requires identical colTypes: "
                  "int/bigint, int/numeric and varchar typmod arms are not "
                  "fully flattened — boundary, but bag-equality must hold.",
        "setup_sqls": _tu() + [
            "CREATE TABLE tb(a bigint)",
            "INSERT INTO tb VALUES (1),(5)",
            "CREATE TABLE tn(a numeric)",
            "INSERT INTO tn VALUES (2),(7)",
            "CREATE TABLE tv(a varchar(10))",
            "INSERT INTO tv VALUES ('x'),('z')",
            "CREATE TABLE tv2(a varchar(20))",
            "INSERT INTO tv2 VALUES ('y'),('x')",
            "ANALYZE tb", "ANALYZE tn", "ANALYZE tv", "ANALYZE tv2",
        ],
        "checks": [
            pair("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM tb) s WHERE s.a > 0",
                 "SELECT a FROM t1 WHERE a > 0 UNION ALL "
                 "SELECT a FROM tb WHERE a > 0"),
            exp("SELECT count(*) FROM (SELECT a FROM t1 UNION ALL "
                "SELECT a FROM tb) s WHERE s.a > 0", [[4]]),
            pair("SELECT a FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM tn) s WHERE s.a > 1",
                 "SELECT a FROM t1 WHERE a > 1 UNION ALL "
                 "SELECT a FROM tn WHERE a > 1"),
            pair("SELECT a FROM (SELECT a FROM tv UNION ALL "
                 "SELECT a FROM tv2) s WHERE s.a = 'x'",
                 "SELECT a FROM tv WHERE a = 'x' UNION ALL "
                 "SELECT a FROM tv2 WHERE a = 'x'"),
            exp("SELECT count(*) FROM (SELECT a FROM tv UNION ALL "
                "SELECT a FROM tv2) s WHERE s.a = 'x'", [[2]]),
        ],
    },
    {
        "name": "a14_join_and_distinct_boundary",
        "arm": "a14",
        "source": "Join over the flattened append distributes to per-arm "
                  "joins; UNION (dedup) is not a simple union-all — the "
                  "filter still commutes with dedup (boundary pair).",
        "setup_sqls": _nva() + _tu(),
        "checks": [
            exp("SELECT count(*) FROM nva JOIN "
                "(SELECT a FROM t1 UNION ALL SELECT a FROM t2) s "
                "ON s.a = nva.a", [[3]]),
            pair("SELECT count(*) FROM nva JOIN "
                 "(SELECT a FROM t1 UNION ALL SELECT a FROM t2) s "
                 "ON s.a = nva.a",
                 "SELECT count(*) FROM "
                 "(SELECT nva.a FROM nva JOIN t1 ON t1.a = nva.a "
                 "UNION ALL "
                 "SELECT nva.a FROM nva JOIN t2 ON t2.a = nva.a) q"),
            pair("SELECT count(*) FROM nva WHERE a IN "
                 "(SELECT a FROM t1 UNION ALL SELECT a FROM t2)",
                 "SELECT count(*) FROM nva WHERE EXISTS "
                 "(SELECT 1 FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM t2) q WHERE q.a = nva.a)"),
            # boundary: UNION keeps a dedup node; filter commutes anyway
            pair("SELECT a FROM (SELECT a FROM t1 UNION "
                 "SELECT a FROM t2) s WHERE s.a > 1",
                 "SELECT a FROM t1 WHERE a > 1 UNION "
                 "SELECT a FROM t2 WHERE a > 1"),
            pair("SELECT DISTINCT a FROM (SELECT a FROM t1 UNION ALL "
                 "SELECT a FROM t2) s WHERE s.a > 1",
                 "SELECT DISTINCT a FROM (SELECT a FROM t1 WHERE a > 1 "
                 "UNION ALL SELECT a FROM t2 WHERE a > 1) q"),
        ],
    },

    # ================================================================
    # GROUP a11: EC implied equality
    # Premise: transitively-implied equality is redundant:
    # ``a=b AND b=c AND a=c`` == ``a=b AND b=c``; ``a=b AND b=10`` implies
    # ``a=10``.  Cross-type arm (int=numeric) is labelled separately.
    # ================================================================
    {
        "name": "a11_redundant_eq",
        "arm": "a11",
        "source": "Three-var equality chain: the implied third clause is "
                  "redundant; NULL members never satisfy strict =.",
        "setup_sqls": [
            "CREATE TABLE e(a int, b int, c int)",
            "INSERT INTO e VALUES (1,1,1),(1,1,2),(2,2,2),(NULL,1,1),"
            "(1,NULL,1),(2,2,NULL)",
            "CREATE TABLE e4(a int, b int, c int, d int)",
            "INSERT INTO e4 VALUES (1,1,1,1),(1,1,2,2),(2,2,2,9)",
            "ANALYZE e", "ANALYZE e4",
        ],
        "checks": [
            exp("SELECT count(*) FROM e WHERE a = b AND b = c", [[2]]),
            pair("SELECT count(*) FROM e WHERE a = b AND b = c AND a = c",
                 "SELECT count(*) FROM e WHERE a = b AND b = c"),
            pair("SELECT count(*) FROM e WHERE a = b AND a = c",
                 "SELECT count(*) FROM e WHERE a = b AND a = c AND b = c"),
            # 4-chain: a=b AND b=c AND c=d implies a=d, a=c, b=d
            exp("SELECT count(*) FROM e4 WHERE a = b AND b = c AND c = d",
                [[1]]),
            pair("SELECT count(*) FROM e4 WHERE a = b AND b = c AND c = d",
                 "SELECT count(*) FROM e4 WHERE a = b AND b = c AND c = d "
                 "AND a = d AND a = c AND b = d"),
            pair("SELECT a, b, c FROM e WHERE a = b AND b = c AND a = c",
                 "SELECT a, b, c FROM e WHERE a = b AND b = c"),
        ],
    },
    {
        "name": "a11_const_propagation",
        "arm": "a11",
        "source": "a=b AND b=k collapses to a=k AND b=k; either side's "
                  "constant clause is implied.",
        "setup_sqls": [
            "CREATE TABLE e(a int, b int, c int)",
            "INSERT INTO e VALUES (1,1,1),(1,1,2),(2,2,2),(NULL,1,1),"
            "(1,NULL,1),(2,2,NULL),(2,1,2)",
            "ANALYZE e",
        ],
        "checks": [
            exp("SELECT count(*) FROM e WHERE a = b AND b = 2", [[2]]),
            pair("SELECT count(*) FROM e WHERE a = b AND b = 2",
                 "SELECT count(*) FROM e WHERE a = 2 AND b = 2"),
            pair("SELECT count(*) FROM e WHERE a = b AND b = 2",
                 "SELECT count(*) FROM e WHERE a = b AND b = 2 AND a = 2"),
            pair("SELECT count(*) FROM e WHERE a = 2 AND b = 2 AND a = b",
                 "SELECT count(*) FROM e WHERE a = 2 AND b = 2"),
            # constant through two hops: a=b AND b=c AND c=2 -> a=2
            pair("SELECT count(*) FROM e WHERE a = b AND b = c AND c = 2",
                 "SELECT count(*) FROM e WHERE a = 2 AND b = 2 AND c = 2"),
        ],
    },
    {
        "name": "a11_join_chain",
        "arm": "a11",
        "source": "EC across join clauses: t.a=s.pk AND s.pk=u.x implies "
                  "t.a=u.x; adding/removing implied join quals is a no-op.",
        "setup_sqls": _t() + _s() + [
            "CREATE TABLE u(x int)",
            "INSERT INTO u VALUES (2),(7)",
            "ANALYZE u",
        ],
        "checks": [
            exp("SELECT count(*) FROM t JOIN s ON t.a = s.pk "
                "JOIN u ON s.pk = u.x", [[1]]),
            pair("SELECT count(*) FROM t JOIN s ON t.a = s.pk "
                 "JOIN u ON s.pk = u.x",
                 "SELECT count(*) FROM t JOIN s ON t.a = s.pk "
                 "JOIN u ON s.pk = u.x AND t.a = u.x"),
            pair("SELECT count(*) FROM t JOIN s ON t.a = s.pk "
                 "JOIN u ON s.pk = u.x AND t.a = u.x",
                 "SELECT count(*) FROM t JOIN s ON t.a = s.pk "
                 "JOIN u ON t.a = u.x"),
            # implied constant through a join
            pair("SELECT count(*) FROM t JOIN s ON t.a = s.pk "
                 "WHERE s.pk = 2",
                 "SELECT count(*) FROM t JOIN s ON t.a = s.pk "
                 "WHERE s.pk = 2 AND t.a = 2"),
            exp("SELECT t.a FROM t JOIN s ON t.a = s.pk "
                "JOIN u ON s.pk = u.x AND t.a = u.x", [[2]]),
        ],
    },
    {
        "name": "a11_crosstype_numeric",
        "arm": "a11",
        "source": "CROSS-TYPE arm: int=numeric equality.  i=n AND n=5 "
                  "implies i=5 exactly (int->numeric is injective); implied "
                  "clause must stay redundant.",
        "setup_sqls": [
            "CREATE TABLE en(i int, n numeric)",
            "INSERT INTO en VALUES (5,5),(5,5.0),(6,5),(NULL,5),(5,NULL)",
            "ANALYZE en",
        ],
        "checks": [
            exp("SELECT count(*) FROM en WHERE i = n AND n = 5", [[2]]),
            pair("SELECT count(*) FROM en WHERE i = n AND n = 5",
                 "SELECT count(*) FROM en WHERE i = n AND n = 5 AND i = 5"),
            pair("SELECT count(*) FROM en WHERE i = n AND n = 5 AND i = 5",
                 "SELECT count(*) FROM en WHERE i = 5 AND n = 5"),
            # cast form in the same EC
            pair("SELECT count(*) FROM en WHERE i::numeric = n AND n = 5",
                 "SELECT count(*) FROM en WHERE i = n AND n = 5"),
            # non-integer constant: i=n AND n=5.5 can never hold
            exp("SELECT count(*) FROM en WHERE i = n AND n = 5.5", [[0]]),
            exp("SELECT count(*) FROM en WHERE i = 5.5", [[0]]),
        ],
    },
    {
        "name": "a11_nullable_and_isnotdistinct",
        "arm": "a11",
        "source": "Nullable cols: strict-= chains drop NULLs identically; "
                  "IS NOT DISTINCT FROM is a true equivalence -> its "
                  "transitive closure is also redundant.",
        "setup_sqls": [
            "CREATE TABLE ne(a int, b int, c int)",
            "INSERT INTO ne VALUES (1,1,1),(1,NULL,1),(NULL,1,1),"
            "(2,2,NULL),(NULL,NULL,NULL),(4,4,4)",
            "ANALYZE ne",
        ],
        "checks": [
            exp("SELECT count(*) FROM ne WHERE a = b AND b = c", [[2]]),
            pair("SELECT count(*) FROM ne WHERE a = b AND b = c AND a = c",
                 "SELECT count(*) FROM ne WHERE a = b AND b = c"),
            # IS NOT DISTINCT FROM is transitive (equivalence reln): the
            # third clause is semantically redundant, incl. the NULL row
            exp("SELECT count(*) FROM ne WHERE a IS NOT DISTINCT FROM b "
                "AND b IS NOT DISTINCT FROM c", [[3]]),
            pair("SELECT count(*) FROM ne WHERE a IS NOT DISTINCT FROM b "
                 "AND b IS NOT DISTINCT FROM c "
                 "AND a IS NOT DISTINCT FROM c",
                 "SELECT count(*) FROM ne WHERE a IS NOT DISTINCT FROM b "
                 "AND b IS NOT DISTINCT FROM c"),
        ],
    },
    {
        "name": "a11_or_boundary",
        "arm": "a11",
        "source": "OR-branches: (a=c OR a=d) IS implied by a=b AND "
                  "(b=c OR c=d) — redundant pair.  OR-ing a non-implied "
                  "clause widens the set — recorded divergence boundary.",
        "setup_sqls": [
            "CREATE TABLE e4(a int, b int, c int, d int)",
            "INSERT INTO e4 VALUES (1,1,1,1),(1,1,2,2),(2,2,2,9),(5,9,5,5)",
            "ANALYZE e4",
        ],
        "checks": [
            exp("SELECT count(*) FROM e4 WHERE a = b AND "
                "(b = c OR c = d)", [[3]]),
            # implied: a=b AND (b=c OR b=d) makes (a=c OR a=d) redundant
            # (case b=c -> a=c; case b=d -> a=d)
            exp("SELECT count(*) FROM e4 WHERE a = b AND (b = c OR b = d)",
                [[2]]),
            pair("SELECT count(*) FROM e4 WHERE a = b AND (b = c OR b = d)",
                 "SELECT count(*) FROM e4 WHERE a = b AND (b = c OR b = d) "
                 "AND (a = c OR a = d)"),
            # boundary: (a=c OR a=d) is NOT implied by a=b AND (b=c OR c=d)
            # — witness row (1,1,2,2) satisfies the LHS via c=d but has
            # a!=c and a!=d -> the conjuncted form is smaller, recorded
            # as expected rather than paired.
            exp("SELECT count(*) FROM e4 WHERE a = b AND (b = c OR c = d) "
                "AND (a = c OR a = d)", [[2]]),
            # (5,9,5,5): a=c but neither a=b nor b=c -> OR-ing a=c widens
            exp("SELECT count(*) FROM e4 WHERE a = b OR b = c", [[3]]),
            exp("SELECT count(*) FROM e4 WHERE a = b OR b = c OR a = c",
                [[4]]),
        ],
    },

    # ================================================================
    # GROUP a9: CTE three-state
    # Premise: for a deterministic, single-referenced q:
    #   WITH x AS MATERIALIZED (q) == AS NOT MATERIALIZED == FROM (q) x.
    # ================================================================
    {
        "name": "a9_basic_filter",
        "arm": "a9",
        "source": "Simple filter CTE: MATERIALIZED / NOT MATERIALIZED / "
                  "inline subquery must bag-equal.",
        "setup_sqls": _nva(),
        "checks": [
            exp("WITH x AS MATERIALIZED (SELECT a FROM nva WHERE a > 1) "
                "SELECT count(*) FROM x", [[2]], marker="CTE Scan"),
            pair("WITH x AS MATERIALIZED (SELECT a FROM nva WHERE a > 1) "
                 "SELECT count(*) FROM x",
                 "WITH x AS NOT MATERIALIZED (SELECT a FROM nva WHERE a > 1) "
                 "SELECT count(*) FROM x"),
            pair("WITH x AS MATERIALIZED (SELECT a FROM nva WHERE a > 1) "
                 "SELECT count(*) FROM x",
                 "SELECT count(*) FROM (SELECT a FROM nva WHERE a > 1) x"),
            pair("WITH x AS NOT MATERIALIZED "
                 "(SELECT a, v FROM nva WHERE a > 1) "
                 "SELECT a, v FROM x",
                 "SELECT a, v FROM (SELECT a, v FROM nva WHERE a > 1) x"),
            exp("WITH x AS MATERIALIZED (SELECT a FROM nva WHERE a > 1) "
                "SELECT sum(a) FROM x", [[5]]),
        ],
    },
    {
        "name": "a9_aggregate_cte",
        "arm": "a9",
        "source": "Aggregate CTE consumed once; grouped result must match "
                  "the inline aggregate subquery.",
        "setup_sqls": _sm(),
        "checks": [
            exp("WITH x AS MATERIALIZED "
                "(SELECT m, count(*) c FROM sm GROUP BY m) "
                "SELECT sum(c) FROM x", [[3.0]]),  # sum(bigint) -> numeric
            pair("WITH x AS MATERIALIZED "
                 "(SELECT m, count(*) c FROM sm GROUP BY m) "
                 "SELECT sum(c) FROM x",
                 "WITH x AS NOT MATERIALIZED "
                 "(SELECT m, count(*) c FROM sm GROUP BY m) "
                 "SELECT sum(c) FROM x"),
            pair("WITH x AS MATERIALIZED "
                 "(SELECT m, count(*) c FROM sm GROUP BY m) "
                 "SELECT sum(c) FROM x",
                 "SELECT sum(c) FROM "
                 "(SELECT m, count(*) c FROM sm GROUP BY m) x"),
            pair("WITH x AS MATERIALIZED "
                 "(SELECT m, count(*) c FROM sm GROUP BY m) "
                 "SELECT count(*) FROM x WHERE c >= 1",
                 "SELECT count(*) FROM "
                 "(SELECT m, count(*) c FROM sm GROUP BY m) x "
                 "WHERE c >= 1"),
        ],
    },
    {
        "name": "a9_join_cte",
        "arm": "a9",
        "source": "CTE as a join partner (inner and LEFT JOIN); inlining "
                  "must not change the bag.",
        "setup_sqls": _nva() + _su(),
        "checks": [
            exp("WITH x AS MATERIALIZED "
                "(SELECT pk, v FROM su WHERE v >= 20) "
                "SELECT count(*) FROM nva JOIN x ON x.pk = nva.a", [[2]]),
            pair("WITH x AS MATERIALIZED "
                 "(SELECT pk, v FROM su WHERE v >= 20) "
                 "SELECT count(*) FROM nva JOIN x ON x.pk = nva.a",
                 "SELECT count(*) FROM nva JOIN "
                 "(SELECT pk, v FROM su WHERE v >= 20) x ON x.pk = nva.a"),
            pair("WITH x AS NOT MATERIALIZED "
                 "(SELECT pk, v FROM su WHERE v >= 20) "
                 "SELECT nva.a, x.v FROM nva LEFT JOIN x ON x.pk = nva.a",
                 "SELECT nva.a, x.v FROM nva LEFT JOIN "
                 "(SELECT pk, v FROM su WHERE v >= 20) x ON x.pk = nva.a"),
            pair("WITH x AS MATERIALIZED "
                 "(SELECT pk FROM su) "
                 "SELECT count(*) FROM nva WHERE a IN (SELECT pk FROM x)",
                 "SELECT count(*) FROM nva WHERE a IN (SELECT pk FROM su)"),
        ],
    },
    {
        "name": "a9_orderlimit_cte",
        "arm": "a9",
        "source": "ORDER BY + LIMIT inside the CTE is deterministic "
                  "(unique key); materialization must not lose the limit.",
        "setup_sqls": _su(),
        "checks": [
            exp("WITH x AS MATERIALIZED "
                "(SELECT pk FROM su ORDER BY pk LIMIT 2) "
                "SELECT count(*) FROM x", [[2]]),
            pair("WITH x AS MATERIALIZED "
                 "(SELECT pk FROM su ORDER BY pk LIMIT 2) "
                 "SELECT max(pk) FROM x",
                 "WITH x AS NOT MATERIALIZED "
                 "(SELECT pk FROM su ORDER BY pk LIMIT 2) "
                 "SELECT max(pk) FROM x"),
            pair("WITH x AS MATERIALIZED "
                 "(SELECT pk FROM su ORDER BY pk LIMIT 2) "
                 "SELECT pk FROM x",
                 "SELECT pk FROM (SELECT pk FROM su ORDER BY pk LIMIT 2) x"),
            exp("WITH x AS MATERIALIZED "
                "(SELECT pk FROM su ORDER BY pk DESC LIMIT 1) "
                "SELECT pk FROM x", [[3]]),
        ],
    },
    {
        "name": "a9_nested_and_multiref",
        "arm": "a9",
        "source": "CTE feeding a CTE; plus a deterministic multi-reference "
                  "boundary — double-eval vs materialize must still agree.",
        "setup_sqls": _nva() + _su(),
        "checks": [
            exp("WITH x AS MATERIALIZED (SELECT a, v FROM nva), "
                "y AS MATERIALIZED (SELECT a FROM x WHERE v > 10) "
                "SELECT count(*) FROM y", [[2]]),
            pair("WITH x AS MATERIALIZED (SELECT a, v FROM nva), "
                 "y AS MATERIALIZED (SELECT a FROM x WHERE v > 10) "
                 "SELECT count(*) FROM y",
                 "WITH x AS NOT MATERIALIZED (SELECT a, v FROM nva), "
                 "y AS NOT MATERIALIZED (SELECT a FROM x WHERE v > 10) "
                 "SELECT count(*) FROM y"),
            pair("WITH x AS MATERIALIZED (SELECT a, v FROM nva), "
                 "y AS MATERIALIZED (SELECT a FROM x WHERE v > 10) "
                 "SELECT count(*) FROM y",
                 "SELECT count(*) FROM (SELECT a FROM "
                 "(SELECT a, v FROM nva) x WHERE v > 10) y"),
            # multi-ref on a deterministic q: 3x3 self-cross = 9 either way
            pair("WITH x AS MATERIALIZED (SELECT pk FROM su) "
                 "SELECT count(*) FROM x a, x b",
                 "SELECT count(*) FROM (SELECT pk FROM su) a, "
                 "(SELECT pk FROM su) b"),
            exp("WITH x AS MATERIALIZED (SELECT pk FROM su) "
                "SELECT count(*) FROM x a JOIN x b ON a.pk = b.pk", [[3]]),
        ],
    },

    # ================================================================
    # GROUP a12: OFFSET 0 fence
    # Premise: OFFSET 0 skips nothing -> results identical, but it blocks
    # subquery pullup (optimization fence) -> "Subquery Scan" marker on the
    # fenced side.  OFFSET>0 is a real difference (recorded boundary).
    # ================================================================
    {
        "name": "a12_filter_fence",
        "arm": "a12",
        "source": "Outer qual over an OFFSET-0 subquery == direct qual; "
                  "the fenced side must keep a Subquery Scan node.",
        "setup_sqls": _t(),
        "checks": [
            pair("SELECT a FROM (SELECT a FROM t OFFSET 0) s WHERE s.a > 1",
                 "SELECT a FROM t WHERE a > 1",
                 marker="Subquery Scan"),
            exp("SELECT a FROM (SELECT a FROM t OFFSET 0) s WHERE s.a > 1",
                [[2], [3]]),
            pair("SELECT a, b FROM (SELECT * FROM t OFFSET 0) s "
                 "WHERE s.b IS NULL",
                 "SELECT a, b FROM t WHERE b IS NULL"),
            pair("SELECT count(*) FROM (SELECT a FROM t OFFSET 0) s "
                 "WHERE s.a IS NOT NULL OR s.a IS NULL",
                 "SELECT count(*) FROM t"),
        ],
    },
    {
        "name": "a12_join_fence",
        "arm": "a12",
        "source": "OFFSET-0 subquery as a join partner; the fence must not "
                  "change join semantics.",
        "setup_sqls": _nva() + _su() + _t(),
        "checks": [
            # single-rel ON qual lands on the SubqueryScan -> the node
            # survives trivial_subqueryscan elision, so the marker fires
            pair("SELECT count(*) FROM nva JOIN "
                 "(SELECT pk, v FROM su OFFSET 0) q "
                 "ON q.pk = nva.a AND q.v > 15",
                 "SELECT count(*) FROM nva JOIN su "
                 "ON su.pk = nva.a AND su.v > 15",
                 marker="Subquery Scan"),
            # note: a bare OFFSET-0 join partner is flattened anyway —
            # the SubqueryScan node is trivial (no qual, 1:1 tlist) and
            # gets elided by trivial_subqueryscan() — same bag either way
            pair("SELECT count(*) FROM nva JOIN "
                 "(SELECT pk, v FROM su OFFSET 0) q ON q.pk = nva.a",
                 "SELECT count(*) FROM nva JOIN su ON su.pk = nva.a"),
            exp("SELECT count(*) FROM nva JOIN "
                "(SELECT pk, v FROM su OFFSET 0) q ON q.pk = nva.a", [[3]]),
            pair("SELECT t.a, q.v FROM t LEFT JOIN "
                 "(SELECT pk, v FROM su OFFSET 0) q ON q.pk = t.a",
                 "SELECT t.a, su.v FROM t LEFT JOIN su ON su.pk = t.a"),
            pair("SELECT count(*) FROM nva JOIN "
                 "(SELECT pk FROM su OFFSET 0 ROWS) q ON q.pk = nva.a",
                 "SELECT count(*) FROM nva JOIN su ON su.pk = nva.a"),
        ],
    },
    {
        "name": "a12_limit_offset",
        "arm": "a12",
        "source": "LIMIT inside the fenced subquery is honored identically; "
                  "non-constant OFFSET expressions evaluating to 0 match.",
        "setup_sqls": _su(),
        "checks": [
            pair("SELECT pk FROM (SELECT pk FROM su ORDER BY pk "
                 "LIMIT 2 OFFSET 0) q",
                 "SELECT pk FROM su ORDER BY pk LIMIT 2"),
            exp("SELECT pk FROM (SELECT pk FROM su ORDER BY pk "
                "LIMIT 2 OFFSET 0) q", [[1], [2]]),
            pair("SELECT pk FROM (SELECT pk FROM su OFFSET (1-1)) q",
                 "SELECT pk FROM (SELECT pk FROM su OFFSET 0) q"),
            pair("SELECT pk FROM (SELECT pk FROM su OFFSET (SELECT 0)) q",
                 "SELECT pk FROM (SELECT pk FROM su OFFSET 0) q"),
            pair("SELECT pk FROM (SELECT pk FROM su LIMIT ALL OFFSET 0) q",
                 "SELECT pk FROM su"),
        ],
    },
    {
        "name": "a12_exists_lateral_fence",
        "arm": "a12",
        "source": "OFFSET 0 inside EXISTS sublinks and LATERAL subqueries.",
        "setup_sqls": _t() + [
            "CREATE TABLE s2b(pk int, m int)",
            "INSERT INTO s2b VALUES (1,1),(2,NULL),(3,2)",
            "ANALYZE s2b",
        ],
        "checks": [
            pair("SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM (SELECT m FROM s2b OFFSET 0) q "
                 "WHERE q.m = t.a)",
                 "SELECT count(*) FROM t WHERE EXISTS "
                 "(SELECT 1 FROM s2b WHERE s2b.m = t.a)"),
            exp("SELECT count(*) FROM t WHERE EXISTS "
                "(SELECT 1 FROM (SELECT m FROM s2b OFFSET 0) q "
                "WHERE q.m = t.a)", [[2]]),
            pair("SELECT t.a, q.m FROM t LEFT JOIN LATERAL "
                 "(SELECT m FROM s2b WHERE s2b.pk = t.a OFFSET 0) q "
                 "ON true",
                 "SELECT t.a, q.m FROM t LEFT JOIN LATERAL "
                 "(SELECT m FROM s2b WHERE s2b.pk = t.a) q ON true"),
            pair("SELECT count(*) FROM t WHERE t.a IN "
                 "(SELECT m FROM (SELECT m FROM s2b OFFSET 0) q)",
                 "SELECT count(*) FROM t WHERE t.a IN (SELECT m FROM s2b)"),
        ],
    },
    {
        "name": "a12_offset_positive_boundary",
        "arm": "a12",
        "source": "OFFSET > 0 really removes rows — recorded as expected "
                  "boundaries, never paired with OFFSET 0.",
        "setup_sqls": _su(),
        "checks": [
            exp("SELECT count(*) FROM (SELECT pk FROM su OFFSET 1) q",
                [[2]]),
            exp("SELECT pk FROM (SELECT pk FROM su ORDER BY pk "
                "OFFSET 1) q", [[2], [3]]),
            exp("SELECT count(*) FROM (SELECT pk FROM su OFFSET 0) q",
                [[3]]),
            exp("SELECT count(*) FROM (SELECT pk FROM su ORDER BY pk "
                "LIMIT 1 OFFSET 1) q", [[1]]),
            # OFFSET beyond the end -> empty
            exp("SELECT count(*) FROM (SELECT pk FROM su OFFSET 99) q",
                [[0]]),
        ],
    },
]

# alias for the generic loader contract
PG_LIVE_PROBES = PG_TRANSFORM_PROBES
