"""Live / suspected-live PostgreSQL bug probes (upstream-reported, still firing).

Unlike seeds/pg_recall.py (known bugs with closed fix windows), these are
verbatim reproducers from recent pgsql-bugs / pgsql-hackers reports whose fix
is either master-only or still pending — a "fired" outcome on the newest
released build (18.6 / 17.11 / 16.15) is a live-bug signal, and on
pgmaster (20devel) it is the strongest possible signal.

Same schema as pg_recall.py.  `affected` marks empirically verified firing
ranges (tightened from observed results); majors not listed report 'observe'.
"""

PG_LIVE_PROBES = [
    # ---------------- verified live ----------------
    {
        "name": "live19560_leftjoin_phv_qual",
        "source": "BUG #19560 (2026-07, 19beta2): removable LEFT JOIN + "
                  "one-row subquery pullup + strict qual => WHERE qual dropped; "
                  "silent wrong rows. Regression since 16.0.",
        "setup_sqls": [
            "CREATE TABLE items (id text, owner text)",
            "CREATE TABLE follows (item_id text, user_id text, "
            "UNIQUE (user_id, item_id))",
            "INSERT INTO items VALUES ('item1', 'alice')",
        ],
        "pre_sqls": [],
        "query": (
            "WITH viewer AS (SELECT 'bob' AS id) "
            "SELECT count(*) FROM items "
            "LEFT JOIN follows ON follows.item_id = items.id "
            "AND follows.user_id = 'bob' "
            "LEFT JOIN viewer ON TRUE "
            "WHERE items.owner = viewer.id"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[0]],
        "buggy_marker": "returns 1 instead of 0 (qual silently dropped)",
        # verified fires: 16.0/16.6/17.11/18.6; clean: 20devel
        "affected": {16: (0, 15), 17: (0, 11), 18: (0, 6)},
    },
    {
        "name": "live19626_sje_appendrel",
        "source": "BUG #19626 (2026-08, 19beta3/20devel): self-join elimination "
                  "does not rewrite append_rel_list translated_vars => "
                  "'no relation entry for relid 1' / SEGV. enable_self_join_"
                  "elimination=off avoids it (PG18 feature).",
        "setup_sqls": ["CREATE TABLE t0(c2 INT PRIMARY KEY, c3 INT)"],
        "pre_sqls": [],
        "query": (
            "SELECT count(*) FROM t0 "
            "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT t0.c3) AS s "
            "ON (s.c3 IS NOT NULL) "
            "WHERE t0.c2 IN (SELECT c2 FROM t0)"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "no relation entry for relid",
        # verified fires: 18.0/18.6; clean: 17.11/16.6 (no SJE), 20devel
        "affected": {18: (0, 6)},
    },
    {
        "name": "live_memoize_expr_param",
        "source": "Brazeal 2026-07 (pgsql-hackers): Memoize cache key shares "
                  "outer Param with a qual => stale cache across rescan; "
                  "enable_memoize=on gives 82000 vs 100000. Unfixed on master "
                  "(thread open through 2026-09-11). Extension of BUG #17213.",
        "setup_sqls": [
            "CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, "
            "g%10 AS ten, g%20 AS twenty, g%100 AS hundred "
            "FROM generate_series(0,9999) g",
            "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
            "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
            "ANALYZE tenk1",
        ],
        "pre_sqls": [
            "SET enable_seqscan = off",
            "SET enable_mergejoin = off",
            "SET work_mem = '64kB'",
            "SET enable_memoize = on",
        ],
        "query": (
            "SELECT sum(c) FROM (SELECT t0.unique1, "
            "(SELECT count(*) FROM tenk1 t2 JOIN tenk1 t1 "
            "ON t1.unique1 = t2.hundred + t0.ten "
            "WHERE t1.twenty = t0.ten) AS c "
            "FROM tenk1 t0 WHERE t0.unique1 < 200) s"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[100000.0]],
        "buggy_marker": "returns 82000 under memoize (stale expr-param cache)",
        # verified fires: every build incl. 15.19/18.6/20devel+asan
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    # ---------------- verified recall-window ----------------
    {
        "name": "brazeal_partprune_default",
        "source": "Brazeal 2026-07: IS NOT NULL + IN() multi-step pruning loses "
                  "RANGE DEFAULT partition (scan_default vs -1 bound_offset "
                  "intersect). Fixed in latest releases; fires on 16.6/18.0.",
        "setup_sqls": [
            "CREATE TABLE s2 (a int) PARTITION BY RANGE (a)",
            "CREATE TABLE s2_1 PARTITION OF s2 FOR VALUES FROM (0) TO (10)",
            "CREATE TABLE s2_d PARTITION OF s2 DEFAULT",
            "INSERT INTO s2 VALUES (5), (15)",
        ],
        "pre_sqls": [],
        "query": "SELECT count(*) FROM s2 WHERE a IS NOT NULL AND a IN (5, 15)",
        "buggy": "wrong_rows",
        "expected_rows": [[2]],
        "buggy_marker": "default partition pruned => returns 1",
        # verified fires: 16.0/16.6/17.0/18.0; clean: 15.19/17.11/18.6/20d
        "affected": {16: (0, 6), 17: (0, 5), 18: (0, 3)},
    },
    {
        "name": "brazeal_partprune_runtime",
        "source": "Same bug via run-time pruning (generic plan + PREPARE).",
        "setup_sqls": [
            "CREATE TABLE s2 (a int) PARTITION BY RANGE (a)",
            "CREATE TABLE s2_1 PARTITION OF s2 FOR VALUES FROM (0) TO (10)",
            "CREATE TABLE s2_d PARTITION OF s2 DEFAULT",
            "INSERT INTO s2 VALUES (5), (15)",
        ],
        "pre_sqls": [
            "SET plan_cache_mode = force_generic_plan",
            "PREPARE s2p(int,int) AS SELECT count(*) FROM s2 "
            "WHERE a IS NOT NULL AND a IN ($1, $2)",
        ],
        "query": "EXECUTE s2p(5,15)",
        "buggy": "wrong_rows",
        "expected_rows": [[2]],
        "buggy_marker": "runtime pruning drops default subplan => returns 1",
        "affected": {16: (0, 6), 17: (0, 5), 18: (0, 3)},
    },
    {
        "name": "rule_gencol_new_stored",
        "source": "Chao Li 2026-04 / commit c6a79be: RULE action NEW.gen reads "
                  "OLD value (rewriteTargetListIU drops gencol TLEs). "
                  "Back-patched all branches; fires on 16.6.",
        "setup_sqls": [
            "CREATE TABLE t (id int PRIMARY KEY, a int, "
            "gen int GENERATED ALWAYS AS (a * 2) STORED)",
            "CREATE TABLE t_log (op text, old_gen int, new_gen int)",
            "CREATE RULE t_log AS ON UPDATE TO t DO ALSO "
            "INSERT INTO t_log VALUES ('UPD', OLD.gen, NEW.gen)",
            "INSERT INTO t (id, a) VALUES (1, 5)",
            "UPDATE t SET a = 100 WHERE id = 1",
        ],
        "pre_sqls": [],
        "query": "SELECT * FROM t_log",
        "buggy": "wrong_rows",
        "expected_rows": [["UPD", 10, 200]],
        "buggy_marker": "NEW.gen reads pre-update value => ('UPD',10,10)",
        "affected": {16: (0, 6), 17: (0, 5), 18: (0, 0)},
    },
    {
        "name": "rule_gencol_new_virtual",
        "source": "Same defect, VIRTUAL generated column (PG18+ only). "
                  "Fires on 18.0; fixed by 18.6.",
        "setup_sqls": [
            "CREATE TABLE t (id int PRIMARY KEY, a int, "
            "gen int GENERATED ALWAYS AS (a * 2) VIRTUAL)",
            "CREATE TABLE t_log (op text, old_gen int, new_gen int)",
            "CREATE RULE t_log AS ON UPDATE TO t DO ALSO "
            "INSERT INTO t_log VALUES ('UPD', OLD.gen, NEW.gen)",
            "INSERT INTO t (id, a) VALUES (1, 5)",
            "UPDATE t SET a = 100 WHERE id = 1",
        ],
        "pre_sqls": [],
        "query": "SELECT * FROM t_log",
        "buggy": "wrong_rows",
        "expected_rows": [["UPD", 10, 200]],
        "buggy_marker": "NEW.gen reads pre-update value => ('UPD',10,10)",
        # VIRTUAL is 18-only; other majors observe (setup syntax error)
        "affected": {18: (0, 3)},
    },
    {
        "name": "live19684_union_nkeys",
        "source": "BUG #19684 (Lakhin 2026-08, 19beta3): zero-column UNION "
                  "under forced parallelism => Gather-path create_sort_path "
                  "lacks the groupList!=NIL guard the Append path has => "
                  "Assert(nkeys > 0) in tuplesortvariants.c. Patch pending; "
                  "fires on every assert build 17.0->20devel (16.x plans "
                  "differently and stays clean).",
        "setup_sqls": ["CREATE TABLE t(i int)"],
        "pre_sqls": [
            "SET cpu_tuple_cost = 1000",
            "SET min_parallel_table_scan_size = 1",
        ],
        "query": "SELECT FROM t UNION SELECT FROM t",
        "buggy": "error_or_crash",
        # backend dies on Assert -> 'server closed the connection'
        "affected": {17: (0, 11), 18: (0, 6), 20: (0, 0)},
    },
    {
        "name": "live19653_partwise_phv_nestloop",
        "source": "BUG #19653 (2026-09, 18.x): partitionwise join supplies a "
                  "PlaceHolderVar as a nestloop param from a child join => "
                  "'variable not found in subplan target list'. Fixed "
                  "35d3f2069a3c (master only, not yet in any released minor); "
                  "needs per-partition indexes for the param-driven path. "
                  "Fires on every released build 15.19->18.6.",
        "setup_sqls": [
            "CREATE TABLE prt1 (a int, b int, c varchar) "
            "PARTITION BY RANGE(a)",
            "CREATE TABLE prt1_p1 PARTITION OF prt1 "
            "FOR VALUES FROM (0) TO (250)",
            "CREATE TABLE prt1_p3 PARTITION OF prt1 "
            "FOR VALUES FROM (500) TO (600)",
            "CREATE TABLE prt1_p2 PARTITION OF prt1 "
            "FOR VALUES FROM (250) TO (500)",
            "INSERT INTO prt1 SELECT i, i % 25, to_char(i, 'FM0000') "
            "FROM generate_series(0, 599) i WHERE i % 2 = 0",
            "CREATE INDEX iprt1_p1_a on prt1_p1(a)",
            "CREATE INDEX iprt1_p2_a on prt1_p2(a)",
            "CREATE INDEX iprt1_p3_a on prt1_p3(a)",
            "ANALYZE prt1",
            "CREATE TABLE prt2 (a int, b int, c varchar) "
            "PARTITION BY RANGE(b)",
            "CREATE TABLE prt2_p1 PARTITION OF prt2 "
            "FOR VALUES FROM (0) TO (250)",
            "CREATE TABLE prt2_p2 PARTITION OF prt2 "
            "FOR VALUES FROM (250) TO (500)",
            "CREATE TABLE prt2_p3 PARTITION OF prt2 "
            "FOR VALUES FROM (500) TO (600)",
            "INSERT INTO prt2 SELECT i % 25, i, to_char(i, 'FM0000') "
            "FROM generate_series(0, 599) i WHERE i % 3 = 0",
            "CREATE INDEX iprt2_p1_b on prt2_p1(b)",
            "CREATE INDEX iprt2_p2_b on prt2_p2(b)",
            "CREATE INDEX iprt2_p3_b on prt2_p3(b)",
            "ANALYZE prt2",
        ],
        "pre_sqls": [
            "SET enable_partitionwise_join TO on",
            "SET enable_hashjoin TO false",
            "SET enable_mergejoin TO false",
        ],
        "query": (
            "SELECT t1.a, t1.c, t2.b, t2.c FROM prt1 t1 LEFT JOIN "
            "(SELECT b, COALESCE(c, 'x') AS c FROM prt2 WHERE a = 0) t2 "
            "ON t1.a = t2.b WHERE t1.c = t2.c ORDER BY t1.a, t2.b"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "variable not found in subplan",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6)},
    },
    {
        "name": "live19649_jsonb_wrap_pushdown",
        "source": "BUG #19649 (2026-09, 18.6): qual on a representation-"
                  "sensitive wrapper (j::text) of a grouping column is "
                  "pushed below GROUP BY, splitting one jsonb-equal group => "
                  "wrong count. Patch v3 in flight; fires on every build "
                  "tested incl. 15.19 and 20devel.",
        "setup_sqls": [
            "CREATE TABLE t(id int primary key, j jsonb)",
            "INSERT INTO t VALUES (1,'1'),(2,'1.0')",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT j, c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
            "WHERE j::text = '1'"
        ),
        "buggy": "wrong_rows",
        # psycopg2 deserializes jsonb '1' to int 1 via json.loads
        "expected_rows": [[1, 2]],
        "buggy_marker": "qual pushed below GROUP BY => returns (1, 1)",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "live_plancache_cast_invalidate",
        "source": "Samokhvalov 2026-09 (pgsql-hackers): plan cache is not "
                  "invalidated when a cast's underlying function is swapped "
                  "(CASTSOURCETARGET syscache not registered) => EXECUTE "
                  "returns stale result. Tom Lane found a function-overload "
                  "sibling. Confirmed on master/18/14; patch pending. "
                  "FLAKY: whether the CachedPlanSource survives depends on "
                  "sinval catchup timing — observed fired on 186/master/asan "
                  "and clean on the same builds in other runs.",
        "setup_sqls": [
            "CREATE SCHEMA cast_repro",
            "CREATE TYPE cast_repro.key_t AS (v int)",
            "CREATE FUNCTION cast_repro.cast_old(cast_repro.key_t) "
            "RETURNS int LANGUAGE sql IMMUTABLE STRICT "
            "AS 'select ($1).v'",
            "CREATE FUNCTION cast_repro.cast_new(cast_repro.key_t) "
            "RETURNS int LANGUAGE sql IMMUTABLE STRICT "
            "AS 'select ($1).v + 100'",
            "CREATE CAST (cast_repro.key_t AS int) "
            "WITH FUNCTION cast_repro.cast_old(cast_repro.key_t) "
            "AS IMPLICIT",
        ],
        "pre_sqls": [
            "SET search_path = cast_repro, pg_catalog",
            "PREPARE q(key_t) AS SELECT $1::int",
            "EXECUTE q(row(1)::key_t)",
            "DROP CAST (key_t AS int)",
            "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
            "AS IMPLICIT",
        ],
        "query": "EXECUTE q(row(1)::key_t)",
        "buggy": "wrong_rows",
        "expected_rows": [[101]],
        "buggy_marker": "stale cached plan => returns 1",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "live_notenforced_inherit_validate",
        "source": "Samokhvalov 2026-09-13 (pgsql-hackers): ALTER TABLE ADD "
                  "CONSTRAINT merging an inherited NOT ENFORCED check marks "
                  "conenforced+convalidated without scanning => pre-existing "
                  "violating row sits under a 'validated' constraint. "
                  "MergeCheckConstraint flips is_enforced/skip_validation. "
                  "Fires on 18.x and 20devel (NOT ENFORCED is 18+).",
        "setup_sqls": [
            "CREATE TABLE p (a int CONSTRAINT ck CHECK (a > 0) "
            "NOT ENFORCED)",
            "CREATE TABLE c () INHERITS (p)",
            "INSERT INTO c VALUES (-1)",
            "ALTER TABLE c ADD CONSTRAINT ck CHECK (a > 0)",
        ],
        "pre_sqls": [],
        # buggy iff a merged c.ck constraint is marked validated
        "query": (
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'c'::regclass AND conname = 'ck' "
            "AND convalidated"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[0]],
        "buggy_marker": "merged constraint marked validated without scan",
        "affected": {18: (0, 6), 20: (0, 0)},
    },
    # ---------------- structural bait (siblings of the live veins) ----------------
    {
        "name": "bait19649_window_partition",
        "source": "#19649 arm over window PARTITION BY — upstream flags the "
                  "non-GROUP-BY routing as more exposed. Verified live on "
                  "186 and master.",
        "setup_sqls": [
            "CREATE TABLE t(id int primary key, j jsonb)",
            "INSERT INTO t VALUES (1,'1'),(2,'1.0')",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT j, c FROM (SELECT j, count(*) OVER (PARTITION BY j) c "
            "FROM t) s WHERE j::text = '1'"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1, 2]],
        "buggy_marker": "qual pushed below window partition => (1, 1)",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "bait19649_unionall_group",
        "source": "#19649 arm under set-ops: wrapper qual pushed into each "
                  "UNION ALL arm's GROUP BY. Verified live on 186/master.",
        "setup_sqls": [
            "CREATE TABLE t(id int primary key, j jsonb)",
            "INSERT INTO t VALUES (1,'1'),(2,'1.0')",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT j, c FROM (SELECT j, count(*) c FROM t GROUP BY j "
            "UNION ALL SELECT j, count(*) c FROM t GROUP BY j) s "
            "WHERE j::text = '1' ORDER BY 1"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1, 2], [1, 2]],
        "buggy_marker": "qual pushed into UNION ALL arms => (1,1),(1,1)",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "bait19649_concat_wrap",
        "source": "#19649 sibling: wrapper made opaque to the pushdown "
                  "matcher via (j::text||'') — still pushed below GROUP "
                  "BY on 20devel.",
        "setup_sqls": [
            "CREATE TABLE t(id int primary key, j jsonb)",
            "INSERT INTO t VALUES (1,'1'),(2,'1.0')",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT j, c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
            "WHERE (j::text || '') = '1'"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1, 2]],
        "buggy_marker": "concat-wrapped qual pushed below GROUP BY => (1,1)",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "bait19649_cube_mixed",
        "source": "#19649 sibling over GROUPING SETS: CUBE(j,id) emits the "
                  "'1' group via two paths — pushed below only one => "
                  "missing a (1,1) row. Verified on 18.6 + 20devel; "
                  "single-key CUBE(j) stays clean (MixedAggregate keeps "
                  "the filter above the grouping sets).",
        "setup_sqls": [
            "CREATE TABLE t(id int primary key, j jsonb)",
            "INSERT INTO t VALUES (1,'1'),(2,'1.0')",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT j, c FROM (SELECT j, count(*) c FROM t "
            "GROUP BY CUBE(j, id)) s WHERE j::text = '1' ORDER BY 1, 2"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1, 1], [1, 1], [1, 2]],
        "buggy_marker": "inconsistent pushdown across grouping sets "
                        "=> (1,1),(1,2)",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "bait19649_phantom_distinct",
        "source": "#19649 STRONGEST witness: qual pushed below UNIQUE/dedup "
                  "=> a row whose output cannot exist in the subquery's "
                  "result. Dedup normalizes '1.0' to '1', so j::text='1.0' "
                  "must be empty — yet pushdown returns '1.0'. Verified "
                  "18.6 + 20devel.",
        "setup_sqls": [
            "CREATE TABLE t(id int primary key, j jsonb)",
            "INSERT INTO t VALUES (1,'1'),(2,'1.0')",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT j::text FROM (SELECT DISTINCT j FROM t) s "
            "WHERE j::text = '1.0'"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [],
        "buggy_marker": "phantom row '1.0' returned that subquery cannot "
                        "emit",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "bait19649_numeric_scale",
        "source": "#19649 sibling on numeric: 1.0 = 1.00 but scale()/::text "
                  "distinguishes them — the exact 'function over column' "
                  "case commit 44fb59f's comment admits leaving uncaught. "
                  "Verified still firing post-fix on 20devel.",
        "setup_sqls": [
            "CREATE TABLE tn(id int primary key, n numeric)",
            "INSERT INTO tn VALUES (1, 1.0), (2, 1.00)",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT n, c FROM (SELECT n, count(*) c FROM tn GROUP BY n) s "
            "WHERE scale(n) = 1"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1.0, 2]],
        "buggy_marker": "scale() qual pushed below GROUP BY => (1.0,1)",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "bait19649_numeric_cast",
        "source": "Same uncaught class via n::text (CoerceViaIO) — the "
                  "jsonb verbatim mechanism on a different equality domain.",
        "setup_sqls": [
            "CREATE TABLE tn(id int primary key, n numeric)",
            "INSERT INTO tn VALUES (1, 1.0), (2, 1.00)",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT n, c FROM (SELECT n, count(*) c FROM tn GROUP BY n) s "
            "WHERE n::text = '1.0'"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1.0, 2]],
        "buggy_marker": "cast qual pushed below GROUP BY => (1.0,1)",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
    {
        "name": "bait19653_nested_childjoin",
        "source": "#19653 nested arm (verbatim from master's partition_join "
                  "regression test): PHV needed by a parameterized child "
                  "join nested inside the supplying child join.",
        "setup_sqls": [
            "CREATE TABLE prt1 (a int, b int, c varchar) "
            "PARTITION BY RANGE(a)",
            "CREATE TABLE prt1_p1 PARTITION OF prt1 "
            "FOR VALUES FROM (0) TO (250)",
            "CREATE TABLE prt1_p3 PARTITION OF prt1 "
            "FOR VALUES FROM (500) TO (600)",
            "CREATE TABLE prt1_p2 PARTITION OF prt1 "
            "FOR VALUES FROM (250) TO (500)",
            "INSERT INTO prt1 SELECT i, i % 25, to_char(i, 'FM0000') "
            "FROM generate_series(0, 599) i WHERE i % 2 = 0",
            "CREATE INDEX iprt1_p1_a on prt1_p1(a)",
            "CREATE INDEX iprt1_p2_a on prt1_p2(a)",
            "CREATE INDEX iprt1_p3_a on prt1_p3(a)",
            "ANALYZE prt1",
            "CREATE TABLE prt3 (a int, b int, c varchar) "
            "PARTITION BY RANGE(a)",
            "CREATE TABLE prt3_p1 PARTITION OF prt3 "
            "FOR VALUES FROM (0) TO (250)",
            "CREATE TABLE prt3_p2 PARTITION OF prt3 "
            "FOR VALUES FROM (250) TO (500)",
            "CREATE TABLE prt3_p3 PARTITION OF prt3 "
            "FOR VALUES FROM (500) TO (600)",
            "INSERT INTO prt3 SELECT lb + i / 2, i % 25, "
            "to_char(i, 'FM0000') FROM generate_series(0, 99) i, "
            "(VALUES (0), (250), (500)) v(lb)",
            "CREATE INDEX iprt3_a ON prt3(a)",
            "ANALYZE prt3",
            "CREATE TABLE prt4 (a int, b int, c varchar) "
            "PARTITION BY RANGE(a)",
            "CREATE TABLE prt4_p1 PARTITION OF prt4 "
            "FOR VALUES FROM (0) TO (250)",
            "CREATE TABLE prt4_p2 PARTITION OF prt4 "
            "FOR VALUES FROM (250) TO (500)",
            "CREATE TABLE prt4_p3 PARTITION OF prt4 "
            "FOR VALUES FROM (500) TO (600)",
            "INSERT INTO prt4 SELECT lb + i / 5, i % 25, "
            "to_char(i % 5, 'FM0000') FROM generate_series(0, 249) i, "
            "(VALUES (0), (250), (500)) v(lb)",
            "CREATE INDEX iprt4_c_a ON prt4(c, a)",
            "ANALYZE prt4",
        ],
        "pre_sqls": [
            "SET enable_partitionwise_join TO on",
            "SET enable_hashjoin TO false",
            "SET enable_mergejoin TO false",
        ],
        "query": (
            "SELECT t1.a, t1.c, t2.a, t2.c FROM prt4 t1 LEFT JOIN "
            "(SELECT t3.a, COALESCE(t3.c, t4.c) AS c FROM prt3 t3 "
            "JOIN prt1 t4 ON t3.a = t4.a WHERE t4.b = 0) t2 "
            "ON t1.a = t2.a WHERE t1.c = t2.c AND t2.a IS NOT NULL "
            "ORDER BY t1.a, t1.c"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "variable not found in subplan",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6)},
    },
    {
        "name": "bait19560_two_removable",
        "source": "#19560 mechanism variant: second removable LEFT JOIN "
                  "between items and viewer.",
        "setup_sqls": [
            "CREATE TABLE items (id text, owner text)",
            "CREATE TABLE follows (item_id text, user_id text, "
            "UNIQUE (user_id, item_id))",
            "CREATE TABLE tags (item_id text, tag text, "
            "UNIQUE (item_id, tag))",
            "INSERT INTO items VALUES ('item1', 'alice')",
        ],
        "pre_sqls": [],
        "query": (
            "WITH viewer AS (SELECT 'bob' AS id) "
            "SELECT count(*) FROM items "
            "LEFT JOIN follows ON follows.item_id = items.id "
            "AND follows.user_id = 'bob' "
            "LEFT JOIN tags ON tags.item_id = items.id "
            "AND tags.tag = 'x' "
            "LEFT JOIN viewer ON TRUE "
            "WHERE items.owner = viewer.id"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[0]],
        "buggy_marker": "qual dropped => returns 1",
        "affected": {16: (0, 15), 17: (0, 11), 18: (0, 6)},
    },
    {
        "name": "bait19560_inline_subq",
        "source": "#19560 mechanism variant: inline single-row subquery "
                  "instead of CTE.",
        "setup_sqls": [
            "CREATE TABLE items (id text, owner text)",
            "CREATE TABLE follows (item_id text, user_id text, "
            "UNIQUE (user_id, item_id))",
            "INSERT INTO items VALUES ('item1', 'alice')",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT count(*) FROM items "
            "LEFT JOIN follows ON follows.item_id = items.id "
            "AND follows.user_id = 'bob' "
            "LEFT JOIN (SELECT 'bob' AS id) viewer ON TRUE "
            "WHERE items.owner = viewer.id"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[0]],
        "buggy_marker": "qual dropped => returns 1",
        "affected": {16: (0, 15), 17: (0, 11), 18: (0, 6)},
    },
    {
        "name": "bait19560_values_subq",
        "source": "#19560 mechanism variant: VALUES-based one-row subquery.",
        "setup_sqls": [
            "CREATE TABLE items (id text, owner text)",
            "CREATE TABLE follows (item_id text, user_id text, "
            "UNIQUE (user_id, item_id))",
            "INSERT INTO items VALUES ('item1', 'alice')",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT count(*) FROM items "
            "LEFT JOIN follows ON follows.item_id = items.id "
            "AND follows.user_id = 'bob' "
            "LEFT JOIN (VALUES ('bob')) AS viewer(id) ON TRUE "
            "WHERE items.owner = viewer.id"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[0]],
        "buggy_marker": "qual dropped => returns 1",
        "affected": {16: (0, 15), 17: (0, 11), 18: (0, 6)},
    },
    {
        "name": "bait19626_exists_form",
        "source": "#19626 mechanism variant: EXISTS instead of IN subquery.",
        "setup_sqls": ["CREATE TABLE t0(c2 INT PRIMARY KEY, c3 INT)"],
        "pre_sqls": [],
        "query": (
            "SELECT count(*) FROM t0 "
            "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT t0.c3) AS s "
            "ON (s.c3 IS NOT NULL) "
            "WHERE EXISTS (SELECT 1 FROM t0 t1 WHERE t1.c2 = t0.c2)"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "no relation entry for relid",
        "affected": {18: (0, 6)},
    },
    {
        "name": "bait19626_union3",
        "source": "#19626 mechanism variant: three-arm UNION ALL appendrel.",
        "setup_sqls": ["CREATE TABLE t0(c2 INT PRIMARY KEY, c3 INT)"],
        "pre_sqls": [],
        "query": (
            "SELECT count(*) FROM t0 "
            "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT t0.c3 "
            "UNION ALL SELECT t0.c3) AS s ON (s.c3 IS NOT NULL) "
            "WHERE t0.c2 IN (SELECT c2 FROM t0)"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "no relation entry for relid",
        "affected": {18: (0, 6)},
    },
    {
        "name": "bait_memoize_exists",
        "source": "Memoize expr-param variant: EXISTS-shaped correlated "
                  "subquery sharing outer param.",
        "setup_sqls": [
            "CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, "
            "g%10 AS ten, g%20 AS twenty, g%100 AS hundred "
            "FROM generate_series(0,9999) g",
            "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
            "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
            "ANALYZE tenk1",
        ],
        "pre_sqls": [
            "SET enable_seqscan = off",
            "SET enable_mergejoin = off",
            "SET work_mem = '64kB'",
            "SET enable_memoize = on",
        ],
        "query": (
            "SELECT sum(c) FROM (SELECT t0.unique1, "
            "(SELECT count(*) FROM tenk1 t2 JOIN tenk1 t1 "
            "ON t1.unique1 = t2.hundred + t0.ten "
            "WHERE t1.twenty = t0.ten) AS c "
            "FROM tenk1 t0 WHERE t0.unique1 < 100) s"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[50000.0]],
        "buggy_marker": "stale memoize cache changes the sum",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    },
]
