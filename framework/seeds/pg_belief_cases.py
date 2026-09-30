"""Belief-auditing case corpus (PG_BELIEF_CASES).

Each case ships a fixture, a query under test, an optional absolute answer
(``expected``), and a list of belief specs consumed by
oracles/pg_plan_beliefs.extract_beliefs:

  {"id", "kind", "claim", "audit"}
    audit = SQL returning COUNTEREXAMPLE rows: empty => belief holds on this
    data, non-empty => the optimizer's asserted fact is false (the finding
    exists even when the query result is coincidentally right).

Case-level flags:
  "selftest": True   plan must NOT assert the belief; the audit must still
                     find counterexamples -> proves the auditor is non-vacuous.
  "known_live": True documented-but-unfixed upstream bug; expected absolute
                     answer is the correct one on every build.

Recall targets (must fire FALSE_BELIEF on pg180, silent on >= fix):
  b19412   BUG #19412, fixed 18.3 — varnullingrels through LATERAL UNION ALL
           dropped 't2.a IS NOT NULL' (belief: null-extended col never NULL)
  b19533   BUG #19533, fixed 18.6 — count(x) ROWS CURRENT ROW wrongly treated
           monotonic -> Run Condition pushed (two data variants: manifesting
           wrong result, and LATENT where the output is coincidentally right)
  saop_*   fixed 18.6 — is_strict_saop believed 'x op ANY/ALL (array)' strict
           w/o proving the array non-empty -> STRICT SQL function inlined to
           a non-strict body (pgsql-hackers 2026-07, Ayush Tiwari)
"""

def _w(vals):
    return ["CREATE TABLE w (x int)",
            "INSERT INTO w VALUES " + ",".join(f"({v})" for v in vals)]


def _ab():
    return [
        "CREATE TABLE a(pk int PRIMARY KEY, v int)",
        "INSERT INTO a VALUES (1,10),(2,20),(3,30)",
        "CREATE TABLE b(fk int, w int)",
        "INSERT INTO b VALUES (1,9),(1,8),(2,7)",
        "ANALYZE a", "ANALYZE b",
    ]


RUNCOND_AUDIT = (
    "SELECT * FROM ("
    "SELECT (c = 1) AS p, lag(c = 1) OVER (ORDER BY pos) AS prev "
    "FROM (SELECT count(x) OVER w AS c, row_number() OVER w AS pos "
    "FROM w WINDOW w AS (ROWS BETWEEN CURRENT ROW AND CURRENT ROW)) z"
    ") y WHERE prev IS FALSE AND p")

PG_BELIEF_CASES = [
    # ============================ recall ============================
    {
        "name": "b19412_nonnull",
        "arm": "recall",
        "source": "BUG #19412 fixed 18.3: PlaceHolderVar nullingrels not "
                  "propagated through LATERAL UNION ALL; planner dropped "
                  "the pushed 't2.a IS NOT NULL' qual on the t2 branch.",
        "setup_sqls": [
            "CREATE TABLE t_append (a int not null, b int)",
            "INSERT INTO t_append VALUES (1,1),(2,3)",
        ],
        "q": ("SELECT t1.a, s.a FROM t_append t1 LEFT JOIN t_append t2 "
              "ON t1.a = t2.b JOIN LATERAL "
              "(SELECT t1.a AS a UNION ALL SELECT t2.a AS a) s ON true "
              "WHERE s.a IS NOT NULL"),
        "expected": [[1, 1], [1, 1], [2, 2]],
        "beliefs": [{
            "id": "t2a_nonnull", "kind": "absent_qual",
            "needle": "t2.a IS NOT NULL",
            "claim": "t2.a (as seen above the LEFT JOIN) is never NULL, "
                     "so the pushed qual can be dropped",
            "audit": ("SELECT * FROM t_append t1 LEFT JOIN t_append t2 "
                      "ON t1.a = t2.b WHERE t2.a IS NULL"),
        }],
    },
    {
        "name": "b19533_runcond_manifest",
        "arm": "recall",
        "source": "BUG #19533 fixed 18.6: count(x) over ROWS CURRENT ROW "
                  "treated as monotonic; Run Condition stopped the scan "
                  "early (wrong result on this data).",
        "setup_sqls": _w([1, "NULL", 1]),
        "q": ("SELECT count(*) FROM (SELECT x, count(x) OVER "
              "(ROWS BETWEEN CURRENT ROW AND CURRENT ROW) c FROM w) s "
              "WHERE c = 1"),
        "expected": [[2]],
        "beliefs": [{
            "id": "count_monotonic", "kind": "run_condition",
            "claim": "(c = 1) is monotonic in window processing order: "
                     "once false it stays false",
            "audit": RUNCOND_AUDIT,
        }],
    },
    {
        "name": "b19533_runcond_latent",
        "arm": "recall",
        "source": "Same wrong monotonicity belief, latent data: the final "
                  "result is coincidentally RIGHT, so every output oracle "
                  "passes; the belief is still false (c takes 2 values "
                  "in one partition).",
        "setup_sqls": _w([1, "NULL"]),
        "q": ("SELECT count(*) FROM (SELECT x, count(x) OVER "
              "(ROWS BETWEEN CURRENT ROW AND CURRENT ROW) c FROM w) s "
              "WHERE c = 1"),
        "expected": [[1]],
        "beliefs": [{
            "id": "count_const_in_partition", "kind": "run_condition",
            "claim": "c is monotonic (BOTH) within the partition — "
                     "effectively constant for this frame",
            "audit": (
                "SELECT * FROM (SELECT c, lag(c) OVER (ORDER BY pos) "
                "AS pc FROM ("
                "SELECT count(x) OVER w AS c, row_number() OVER w AS pos "
                "FROM w WINDOW w AS (ROWS BETWEEN CURRENT ROW AND "
                "CURRENT ROW)) z) q WHERE c IS DISTINCT FROM pc"),
        }],
    },
    {
        "name": "saop_inline_any",
        "arm": "recall",
        "source": "fixed 18.6: is_strict_saop did not prove the array "
                  "non-empty; STRICT SQL function inlined to a body that "
                  "is not NULL-on-NULL (empty ANY returns FALSE).",
        "setup_sqls": [
            "CREATE TABLE t (x int)",
            "INSERT INTO t VALUES (NULL),(5)",
            "CREATE FUNCTION strict_any(int) RETURNS bool LANGUAGE SQL "
            "STRICT IMMUTABLE AS $$ SELECT $1 = ANY ('{}'::int[]) $$",
        ],
        "q": "SELECT x, strict_any(x) FROM t ORDER BY x NULLS FIRST",
        "expected": [[None, None], [5, False]],
        "beliefs": [{
            "id": "saop_body_strict", "kind": "inlined_expr",
            "present": "= ANY", "absent": "strict_any(",
            "claim": "body 'x = ANY(arr)' is strict (NULL in -> NULL "
                     "out), so inlining preserves the STRICT contract",
            "audit": ("SELECT * FROM (VALUES (NULL::int)) v(x) "
                      "WHERE (x = ANY ('{}'::int[])) IS NOT NULL"),
        }],
    },
    {
        "name": "saop_inline_all",
        "arm": "recall",
        "source": "same fix: 'x = ALL(empty)' returns TRUE on NULL input, "
                  "not NULL.",
        "setup_sqls": [
            "CREATE TABLE t (x int)",
            "INSERT INTO t VALUES (NULL),(5)",
            "CREATE FUNCTION strict_all(int) RETURNS bool LANGUAGE SQL "
            "STRICT IMMUTABLE AS $$ SELECT $1 = ALL ('{}'::int[]) $$",
        ],
        "q": "SELECT x, strict_all(x) FROM t ORDER BY x NULLS FIRST",
        "expected": [[None, None], [5, True]],
        "beliefs": [{
            "id": "saop_all_strict", "kind": "inlined_expr",
            "present": "= ALL", "absent": "strict_all(",
            "claim": "body 'x = ALL(arr)' is strict",
            "audit": ("SELECT * FROM (VALUES (NULL::int)) v(x) "
                      "WHERE (x = ALL ('{}'::int[])) IS NOT NULL"),
        }],
    },
    {
        "name": "memoize_shared_param",
        "arm": "recall",
        "known_live": True,
        "source": "Brazeal pgsql-hackers 2026-07 (ext of BUG #17213): "
                  "Memoize cache key shares an outer Param between join "
                  "qual and plain qual -> stale cache -> wrong sum. "
                  "UNFIXED on every build tested.",
        "setup_sqls": [
            "CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, "
            "g%10 AS ten, g%20 AS twenty, g%100 AS hundred "
            "FROM generate_series(0,9999) g",
            "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
            "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
            "ANALYZE tenk1",
        ],
        "gucs": ["SET enable_seqscan = off", "SET enable_mergejoin = off",
                 "SET work_mem = '64kB'"],
        "resets": ["RESET enable_seqscan", "RESET enable_mergejoin",
                   "RESET work_mem"],
        "q": ("SELECT sum(c) FROM (SELECT t0.unique1, (SELECT count(*) "
              "FROM tenk1 t2 JOIN tenk1 t1 ON "
              "t1.unique1 = t2.hundred + t0.ten WHERE t1.twenty = t0.ten) "
              "AS c FROM tenk1 t0 WHERE t0.unique1 < 200) s"),
        "expected": [[100000]],
        "beliefs": [{
            "id": "cache_key_sufficient", "kind": "memoize",
            "claim": "the Memoize cache key captures every outer param "
                     "the inner result depends on (no audit yet: needs a "
                     "per-param recomputation oracle)",
            "audit": None,
        }],
    },
    # ============================ controls ============================
    # belief asserted AND true -> must 'hold' on every build
    {
        "name": "ctl_inner_unique_pk",
        "arm": "ctl",
        "source": "join on PK -> Inner Unique asserted and true.",
        "setup_sqls": _ab(),
        "q": "SELECT * FROM a JOIN b ON a.pk = b.fk",
        "beliefs": [{
            "id": "inner_unique", "kind": "inner_unique",
            "claim": "each outer row matches <=1 inner row (inner key "
                     "unique)",
            "audit": ("SELECT pk FROM a GROUP BY pk "
                      "HAVING count(*) > 1"),
        }],
    },
    {
        "name": "ctl_oj_reduced_strict",
        "arm": "ctl",
        "source": "LEFT JOIN + strict qual -> reduced to inner; reduction "
                  "valid because 'b.fk IS NOT NULL' is never true on "
                  "null-extended rows.",
        "setup_sqls": _ab(),
        "q": ("SELECT * FROM a LEFT JOIN b ON a.pk = b.fk "
              "WHERE b.fk IS NOT NULL"),
        "expected": [[1, 10, 1, 9], [1, 10, 1, 8], [2, 20, 2, 7]],
        "beliefs": [{
            "id": "oj_reduced", "kind": "oj_reduced",
            "claim": "the WHERE qual rejects every null-extended row "
                     "(strict on the nullable side)",
            "audit": ("SELECT * FROM a LEFT JOIN b ON a.pk = b.fk "
                      "WHERE b.fk IS NULL "
                      "AND (b.fk IS NOT NULL) IS TRUE"),
        }],
    },
    {
        "name": "ctl_partition_pruned",
        "arm": "ctl",
        "source": "qual x<100 prunes partition p2; belief = p2 contains "
                  "no row satisfying the quals.",
        "setup_sqls": [
            "CREATE TABLE p(x int) PARTITION BY RANGE (x)",
            "CREATE TABLE p1 PARTITION OF p FOR VALUES FROM (0) TO (100)",
            "CREATE TABLE p2 PARTITION OF p FOR VALUES FROM (100) TO (200)",
            "INSERT INTO p VALUES (1),(50),(150)",
        ],
        "q": "SELECT * FROM p WHERE x < 100",
        "expected": [[1], [50]],
        "beliefs": [{
            "id": "p2_pruned", "kind": "partition_pruned", "parent": "p",
            "claim": "pruned partitions contain no row satisfying the "
                     "query quals",
            "audit": "SELECT * FROM p2 WHERE (x < 100)",
        }],
    },
    {
        "name": "ctl_group_key_fd",
        "arm": "ctl",
        "source": "GROUP BY pk,v with pk PRIMARY KEY -> Group Key reduced "
                  "to pk; belief = pk functionally determines v.",
        "setup_sqls": _ab(),
        "q": "SELECT pk, v FROM a GROUP BY pk, v",
        "expected": [[1, 10], [2, 20], [3, 30]],
        "beliefs": [{
            "id": "pk_det_v", "kind": "group_key_reduced", "query_keys": 2,
            "claim": "pk -> v on the grouped input",
            "audit": ("SELECT pk FROM a GROUP BY pk "
                      "HAVING count(DISTINCT v) > 1"),
        }],
    },
    {
        "name": "ctl_runcond_monotonic",
        "arm": "ctl",
        "source": "count(x) over ORDER BY r ROWS UNBOUNDED PRECEDING is "
                  "nondecreasing; run condition 'c <= 2' valid.",
        "setup_sqls": [
            "CREATE TABLE w2(r int, x int)",
            "INSERT INTO w2 VALUES (1,1),(2,NULL),(3,5),(4,7)",
        ],
        "q": ("SELECT * FROM (SELECT r, count(x) OVER "
              "(ORDER BY r ROWS UNBOUNDED PRECEDING) c FROM w2) s "
              "WHERE c <= 2"),
        "expected": [[1, 1], [2, 1], [3, 2]],
        "beliefs": [{
            "id": "mono_le2", "kind": "run_condition",
            "claim": "(c <= 2) is monotonic: once false it stays false",
            "audit": (
                "SELECT * FROM (SELECT (c <= 2) AS p, lag(c <= 2) "
                "OVER (ORDER BY y) AS prev FROM ("
                "SELECT y, count(x) OVER (ORDER BY y ROWS UNBOUNDED "
                "PRECEDING) c FROM (SELECT r AS y, x FROM w2) u) z) q "
                "WHERE prev IS FALSE AND p"),
        }],
    },
    {
        "name": "ctl_memoize_distinct_params",
        "arm": "ctl",
        "source": "verbatim memoize shape minus the shared Param: expr "
                  "param uses t0.ten, plain qual uses t0.hundred -> the "
                  "cache key really does determine the inner result.",
        "setup_sqls": [
            "CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, "
            "g%10 AS ten, g%20 AS twenty, g%100 AS hundred "
            "FROM generate_series(0,9999) g",
            "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
            "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
            "ANALYZE tenk1",
        ],
        "gucs": ["SET enable_seqscan = off", "SET enable_mergejoin = off",
                 "SET work_mem = '64kB'"],
        "resets": ["RESET enable_seqscan", "RESET enable_mergejoin",
                   "RESET work_mem"],
        "q": ("SELECT sum(c)::bigint FROM (SELECT t0.unique1, "
              "(SELECT count(*) FROM tenk1 t2 JOIN tenk1 t1 ON "
              "t1.unique1 = t2.hundred + t0.ten "
              "WHERE t1.twenty = t0.hundred) AS c "
              "FROM tenk1 t0 WHERE t0.unique1 < 200) s"),
        "expected": [[20000]],
        "beliefs": [{
            "id": "cache_key_sufficient", "kind": "memoize",
            "claim": "the param tuple (t0.ten, t0.hundred) captured by "
                     "the cache key determines the inner count: equal "
                     "keys -> equal results",
            "audit": (
                "SELECT t0.ten, t0.hundred, count(DISTINCT sub.c) "
                "FROM tenk1 t0 JOIN LATERAL ("
                "SELECT count(*) AS c FROM tenk1 t2 JOIN tenk1 t1 ON "
                "t1.unique1 = t2.hundred + t0.ten "
                "WHERE t1.twenty = t0.hundred) sub ON true "
                "WHERE t0.unique1 < 200 "
                "GROUP BY t0.ten, t0.hundred "
                "HAVING count(DISTINCT sub.c) > 1"),
        }],
    },
    # ============================ selftests ============================
    # plan must NOT assert; audit MUST find counterexamples (non-vacuous)
    {
        "name": "st_inner_unique_dup",
        "arm": "selftest",
        "selftest": True,
        "source": "join keys non-unique on both sides: no Inner Unique "
                  "may be asserted; the dup-check audit must find rows.",
        "setup_sqls": [
            "CREATE TABLE a2(k int, v int)",
            "INSERT INTO a2 VALUES (1,1),(1,2),(2,3)",
            "CREATE TABLE b2(k int, w int)",
            "INSERT INTO b2 VALUES (1,9),(1,8),(3,7)",
            "ANALYZE a2", "ANALYZE b2",
        ],
        "q": "SELECT * FROM a2 JOIN b2 ON a2.k = b2.k",
        "beliefs": [{
            "id": "b2k_unique", "kind": "inner_unique",
            "claim": "(fabricated) inner key b2.k is unique",
            "audit": "SELECT k FROM b2 GROUP BY k HAVING count(*) > 1",
        }],
    },
    {
        "name": "st_oj_nonstrict",
        "arm": "selftest",
        "selftest": True,
        "source": "qual 'b.w > 0 OR b.fk IS NULL' is TRUE on "
                  "null-extended rows -> LEFT JOIN must NOT be reduced; "
                  "audit finds such a row.",
        "setup_sqls": _ab(),
        "q": ("SELECT * FROM a LEFT JOIN b ON a.pk = b.fk "
              "WHERE b.w > 0 OR b.fk IS NULL"),
        "beliefs": [{
            "id": "oj_reduced", "kind": "oj_reduced",
            "claim": "(fabricated) qual is strict on the nullable side",
            "audit": ("SELECT * FROM a LEFT JOIN b ON a.pk = b.fk "
                      "WHERE b.fk IS NULL "
                      "AND (b.w > 0 OR b.fk IS NULL) IS TRUE"),
        }],
    },
    {
        "name": "st_runcond_sliding",
        "arm": "selftest",
        "selftest": True,
        "source": "count(x) over a sliding 2-row frame is not monotonic; "
                  "no Run Condition may be pushed; audit finds a flip.",
        "setup_sqls": [
            "CREATE TABLE w3(r int, x int)",
            "INSERT INTO w3 VALUES (1,NULL),(2,5),(3,NULL),(4,7)",
        ],
        "q": ("SELECT * FROM (SELECT r, count(x) OVER "
              "(ORDER BY r ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) c "
              "FROM w3) s WHERE c = 1"),
        "expected": [[2, 1], [3, 1], [4, 1]],
        "beliefs": [{
            "id": "mono_sliding", "kind": "run_condition",
            "claim": "(fabricated) (c = 1) monotonic in frame order",
            "audit": (
                "SELECT * FROM (SELECT (c = 1) AS p, lag(c = 1) "
                "OVER (ORDER BY y) AS prev FROM ("
                "SELECT y, count(x) OVER (ORDER BY y ROWS BETWEEN "
                "1 PRECEDING AND CURRENT ROW) c "
                "FROM (SELECT r AS y, x FROM w3) u) z) q "
                "WHERE prev IS FALSE AND p"),
        }],
    },
]
