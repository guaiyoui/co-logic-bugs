"""Known-answer recall cases for the PostgreSQL harness.

Each case is a real bug fixed during the PostgreSQL 16.x lifecycle, with a
verbatim reproducer from the bug report or regression test. The harness runs
every case on every installed version and checks that the buggy signature
appears exactly on the affected versions — this validates that the oracle
stack can actually detect a PostgreSQL bug before we claim "PG is clean".

Fields:
- name, source: identification and upstream reference
- setup_sqls: statements run before the query
- pre_sqls: per-session settings applied before the query (GUCs needed to
  force the buggy plan shape)
- query: the probe
- buggy: how the bug manifests — 'error_substr' (a specific ERROR string),
  'wrong_rows' (result differs from expected_rows), or 'error_or_crash'
  (fires on backend crash/assert — is_internal_error — on buggy_error
  substring, on timeout when timeout_fires is set, or on wrong rows when
  expected_rows is given)
- expected_rows: expected result on fixed versions (normalized form —
  temporal strings look like 'temporal:<iso>', bools are True/False)
- affected: per-major {major: (lo, hi)} of buggy minor ranges, inclusive.
  Listing a major whose fixed branch is installed turns that build into an
  expected_clean check; omitting a major leaves it as observe-only.  Windows
  past the installed minors are approximate — tighten from observed results.
- requires: capability list ('contrib:<name>', 'icu', 'jit', 'sessions:2',
  'restart'); the checker reports 'skipped' instead of absent when a build
  lacks a capability — the recall matrix doubles as a capability audit.
"""

PG_RECALL = [
    {
        "name": "pg18284_lateral_outer_join_null",
        "source": "BUG #18284, fixed in 16.2 (PlaceHolderVar re-wrap on pullup)",
        "setup_sqls": [],
        "pre_sqls": [],
        "query": (
            "WITH r1 AS (VALUES(null)), r2 AS (VALUES(null), (null)) "
            "SELECT ljl.val_filtered FROM r1 "
            "LEFT JOIN(SELECT j666.val FROM r2 "
            "JOIN (SELECT 666 AS val) as j666 ON true) AS lj_r2 ON true "
            "LEFT JOIN LATERAL(SELECT lj_r2.val AS val_filtered "
            "WHERE false) AS ljl ON true"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[None], [None]],
        "buggy_marker": "returns 666 instead of NULL",
        "affected": {16: (0, 1)},
    },
    {
        "name": "pg18344_bool_partprune_isnot",
        "source": "BUG #18344, fixed in 16.3 (IS NOT true/false prunes NULL partition)",
        "setup_sqls": [
            "CREATE TABLE tl (b bool) PARTITION BY LIST (b)",
            "CREATE TABLE tl_t PARTITION OF tl FOR VALUES IN (true)",
            "CREATE TABLE tl_f PARTITION OF tl FOR VALUES IN (false)",
            "CREATE TABLE tl_n PARTITION OF tl FOR VALUES IN (NULL)",
            "INSERT INTO tl VALUES (true), (false), (NULL)",
        ],
        "pre_sqls": [],
        "query": "SELECT b FROM tl WHERE b IS NOT true ORDER BY b NULLS LAST",
        "buggy": "wrong_rows",
        "expected_rows": [[False], [None]],
        "buggy_marker": "NULL partition wrongly pruned, only false row returned",
        "affected": {16: (0, 2)},
    },
    {
        "name": "pg18305_window_runcond_subplan",
        "source": "BUG #18305, fixed in 16.3 (run condition + subquery pullup)",
        "setup_sqls": [],
        "pre_sqls": [],
        "query": (
            "select 1 from (select ntile(s1.x) over () as c "
            "from (select (select 1) as x) as s1) s where s.c = 1"
        ),
        "buggy": "error_substr",
        "buggy_error": "WindowFunc not found in subplan target lists",
        "expected_rows": [[1]],
        "affected": {16: (0, 2)},
    },
    {
        "name": "pg18522_merge_right_anti_unique",
        "source": "BUG #18522, fixed in 16.4 (merge right anti join skips inner)",
        "setup_sqls": [
            "create table tbl_ra(a int unique, b int)",
            "insert into tbl_ra select i, i%100 from generate_series(1,1000) i",
            "create index on tbl_ra (b)",
            "analyze tbl_ra",
        ],
        "pre_sqls": [
            "set enable_hashjoin to off",
            "set enable_nestloop to off",
        ],
        "query": (
            "select t1.a from tbl_ra t1 "
            "where not exists (select 1 from tbl_ra t2 where t2.b = t1.a) "
            "and t1.b < 2 order by t1.a"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [
            [100], [101], [200], [201], [300], [301], [400], [401],
            [500], [501], [600], [601], [700], [701], [800], [801],
            [900], [901], [1000],
        ],
        "buggy_marker": "merge right anti join with Inner Unique skips inner tuples",
        "affected": {16: (0, 3)},
    },
    {
        "name": "pg18652_unionall_pathkey_sort",
        "source": "BUG #18652, fixed in 16.5 (UNION ALL sort expr matches expr index)",
        "setup_sqls": [
            "CREATE TABLE t(i int, j int)",
            "CREATE INDEX idx on t((i + 0))",
        ],
        "pre_sqls": [],
        "query": (
            "SELECT * FROM t, (SELECT i + 0 AS i FROM "
            "(SELECT i FROM t UNION ALL SELECT i + 1 FROM t) AS t1) AS t2 "
            "WHERE t2.i = t.j"
        ),
        "buggy": "error_substr",
        "buggy_error": "could not find pathkey item to sort",
        "expected_rows": [],
        "affected": {16: (0, 4), 17: (0, 10)},  # fired on 17.0 same signature
    },
    {
        "name": "pg16_agg_pathkey_partitionwise",
        "source": "fixed in 16.1 (agg ORDER BY/DISTINCT pathkeys above Agg)",
        "setup_sqls": [
            "CREATE TABLE t (a int, b text) PARTITION BY RANGE (a)",
            "CREATE TABLE td PARTITION OF t DEFAULT",
            "INSERT INTO t SELECT 1 AS a, '' AS b",
        ],
        "pre_sqls": ["SET enable_partitionwise_aggregate = on"],
        "query": "SELECT a, COUNT(DISTINCT b) FROM t GROUP BY a ORDER BY a",
        "buggy": "error_substr",
        "buggy_error": "could not find pathkey item to sort",
        "expected_rows": [[1, 1]],
        "affected": {16: (0, 0)},
    },
    # ------------------------------------------------------------------
    # Recall v2: post-16.6 fixes — all our 16.x builds are buggy.
    # Sources: release-*.sgml Branch trailers + verbatim regress tests
    # (see results/PG_RECALL_V2_LIBRARY.md for the full library).
    # ------------------------------------------------------------------
    {
        # A24 — join.sql:1342-1364 (17.11 tree); expected join.out:4087
        "name": "pg_lateral_unionall_nullingrel",
        "source": "varnullingrels dropped translating appendrel Var (ec20a4552)",
        "setup_sqls": [
            "create table t_append (a int not null, b int)",
            "insert into t_append values (1, 1)",
            "insert into t_append values (2, 3)",
        ],
        "pre_sqls": [],
        "query": (
            "select t1.a, s.a from t_append t1 "
            "left join t_append t2 on t1.a = t2.b "
            "join lateral (select t1.a as a union all select t2.a as a) s "
            "on true where s.a is not null order by 1, 2"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1, 1], [1, 1], [2, 2]],
        "buggy_marker": "IS NOT NULL elided -> extra NULL row",
        "affected": {17: (0, 10), 18: (0, 2)},  # 16.x observed clean: bug likely introduced >16.6,
    },
    {
        # A25 — join.sql:2238-2248 (18.6), bug #19460
        "name": "pg19460_restrictinfo_fulljoin",
        "source": "BUG #19460 (stale RestrictInfo survives join removal)",
        "setup_sqls": ["create table a (id int)"],
        "pre_sqls": [],
        "query": (
            "select * from "
            "(select 1::int as id) as lhs "
            "full join "
            "(select dummy_source.id "
            " from (select null::int as id) as dummy_source "
            " left join (select a.id from a where a.id = 42) as sub "
            " on sub.id = dummy_source.id) as rhs "
            "on lhs.id = rhs.id"
        ),
        "buggy": "error_substr",
        "buggy_error": "FULL JOIN is only supported",
        "affected": {17: (1, 10), 18: (1, 3)},  # 16.x + 17.0/18.0 observed clean,
    },
    {
        # A29 — union.sql:187-190 (17.11); REL_15 fix never reached 15.19 ->
        # expected_fired there is a live-bug probe.
        "name": "pg_union_container_hash",
        "source": "setop assumes container element type is hashable (cf2bfe073)",
        "setup_sqls": [],
        "pre_sqls": ["set enable_hashagg to on"],
        "query": (
            "select x from (values (array['10'::varbit]), (array['11'::varbit])) _(x) "
            "union select x from (values (array['10'::varbit]), (array['01'::varbit])) _(x)"
        ),
        "buggy": "error_substr",
        "buggy_error": "could not identify a hash function",
        "affected": {15: (0, 18), 17: (1, 10), 18: (1, 5)},  # 15.19/16.x/17.0/18.0 observed clean,
    },
    {
        # A32 — partition_prune.sql:121-139 verbatim + data; fix absent from
        # release-17.sgml -> expected_fired on 17.11 is a live-bug probe.
        "name": "pg_range_default_prune_in",
        "source": "RANGE DEFAULT partition wrongly pruned on IN list (9a0cd8e73)",
        "setup_sqls": [
            "create table rangepart (a int) partition by range (a)",
            "create table rangepart1 partition of rangepart for values from (0) to (10)",
            "create table rangepart2 partition of rangepart for values from (10) to (20)",
            "create table rangepart_def partition of rangepart default",
            "insert into rangepart values (5), (15), (20)",
        ],
        "pre_sqls": [],
        "query": "select * from rangepart where a in (20, 21) order by a",
        "buggy": "wrong_rows",
        "expected_rows": [[20]],
        "buggy_marker": "default partition wrongly skipped -> []",
        "affected": {15: (0, 18), 17: (1, 10), 18: (1, 5)},  # 16.x/17.0/18.0 observed clean,
    },
    {
        # A30 — window.sql:1491-1497 verbatim; bug #19532/#19533
        "name": "pg_window_exclude_runcond",
        "source": "run-condition pushdown unsound with EXCLUDE frame (a85732162)",
        "setup_sqls": [
            "create table empsalary (depname varchar, empno bigint, "
            "salary int, enroll_date date)",
            "insert into empsalary values "
            "('develop',1,2000,'1998-01-01'),('develop',2,2100,'1999-01-01'),"
            "('develop',3,2200,'2000-01-01'),('personnel',4,2300,'2001-01-01'),"
            "('personnel',5,2400,'2002-01-01'),('sales',6,2500,'2003-01-01'),"
            "('sales',7,2600,'2004-01-01'),('sales',8,2700,'2005-01-01'),"
            "('sales',9,2800,'2006-01-01'),('develop',10,2900,'2007-01-01')",
        ],
        "pre_sqls": [],
        "query": (
            "select * from "
            "(select empno, salary, count(*) over (order by salary "
            "rows between unbounded preceding and 0 preceding "
            "exclude current row) c from empsalary) emp "
            "where c <= 3 order by empno"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1, 2000, 0], [2, 2100, 1], [3, 2200, 2], [4, 2300, 3]],
        "affected": {17: (1, 10), 18: (1, 5)},  # 15/16.x + 17.0/18.0 observed clean,
    },
    {
        # A28 — memoize.sql:135-148 verbatim; binary-mode tuple deform
        "name": "pg_memoize_signext",
        "source": "Memoize binary-mode sign extension on -0.0/+0.0 (1bd90c887)",
        "setup_sqls": [
            "create table flt (f float)",
            "create index flt_f_idx on flt (f)",
            "insert into flt values('-0.0'::float),('+0.0'::float)",
            "analyze flt",
        ],
        "pre_sqls": [
            "set enable_seqscan to off",
            "set enable_hashjoin to off",
            "set enable_mergejoin to off",
        ],
        "query": "select * from flt f1 inner join flt f2 on f1.f >= f2.f",
        "buggy": "error_or_crash",
        "buggy_error": "could not find memoization table entry",
        "expected_rows": [[0.0, 0.0]] * 4,
        "affected": {15: (0, 18), 17: (1, 10), 18: (1, 3)},  # 16.x/17.0/18.0 observed clean,
    },
    {
        # A27 — expressions.sql:137-279 verbatim fixture; non-strict equality
        # flips hashed ScalarArrayOp semantics (8 vs 9 list members).
        "name": "pg_hashed_in_nonstrict",
        "source": "hashed IN/NOT IN with non-strict equality (a2a0060d5)",
        "setup_sqls": [
            "create type myint",
            "create function myintin(cstring) returns myint strict immutable "
            "language internal as 'int4in'",
            "create function myintout(myint) returns cstring strict immutable "
            "language internal as 'int4out'",
            "create function myinthash(myint) returns integer strict immutable "
            "language internal as 'hashint4'",
            "create type myint (input = myintin, output = myintout, like = int4)",
            "create cast (int4 as myint) without function",
            "create cast (myint as int4) without function",
            "create function myinteq(myint, myint) returns bool as $$ "
            "begin if $1 is null and $2 is null then return true; "
            "else return $1::int = $2::int; end if; end; $$ language plpgsql immutable",
            "create function myintne(myint, myint) returns bool as $$ "
            "begin return not myinteq($1, $2); end; $$ language plpgsql immutable",
            "create operator = (leftarg = myint, rightarg = myint, "
            "commutator = =, negator = <>, procedure = myinteq, "
            "restrict = eqsel, join = eqjoinsel, merges)",
            "create operator <> (leftarg = myint, rightarg = myint, "
            "commutator = <>, negator = =, procedure = myintne, "
            "restrict = eqsel, join = eqjoinsel, merges)",
            "create operator class myint_ops default for type myint using hash as "
            "operator 1 = (myint, myint), function 1 myinthash(myint)",
            "create table inttest (a myint)",
            "insert into inttest values (null), (0::myint), (1::myint)",
        ],
        "pre_sqls": [],
        "query": (
            "select a, "
            "a in (1::myint,2::myint,3::myint,4::myint,5::myint,6::myint,7::myint,8::myint) as not_hashed, "
            "a in (1::myint,2::myint,3::myint,4::myint,5::myint,6::myint,7::myint,8::myint,9::myint) as hashed "
            "from inttest"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[None, None, None], ["0", False, False], ["1", True, True]],
        "affected": {15: (0, 18), 17: (1, 10), 18: (0, 3)},  # 16.x/17.0 clean; 18.0 FIRED,
    },
    {
        # A17 — merge.sql:1272-1283 verbatim shape; bug #18871
        "name": "pg18871_merge_donothing_partitioned",
        "source": "BUG #18871 (ExecInitPartitionInfo mishandles DO NOTHING)",
        "setup_sqls": [
            "create table pa_target (tid int, balance float, val text) "
            "partition by list (tid)",
            "create table pa_target_p1 partition of pa_target for values in (1,2,3)",
            "insert into pa_target values (1, 10, 'x')",
        ],
        "pre_sqls": [],
        "query": (
            "merge into pa_target t using (values (1, 100)) as s(sid, delta) "
            "on t.tid = s.sid "
            "when not matched then insert values (1, 10, 'inserted by merge') "
            "when matched then do nothing"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "unknown action in MERGE",
        "affected": {15: (0, 18), 17: (1, 10)},  # 16.x/17.0 observed clean,
    },
    {
        # A21 — triggers.sql:1835-1916 verbatim fixture; resrel cache holds a
        # child-format tupdesc while transition table needs parent format.
        "name": "pg_trigger_resrel_tupdesc",
        "source": "result-relation cache vs non-identical partition tupdesc (a546964db)",
        "setup_sqls": [
            "create function dump_insert() returns trigger language plpgsql "
            "as $$ begin return null; end $$",
            "create function dump_update() returns trigger language plpgsql "
            "as $$ begin return null; end $$",
            "create function dump_delete() returns trigger language plpgsql "
            "as $$ begin return null; end $$",
            "create table parent (a text, b int) partition by list (a)",
            "create table child1 partition of parent for values in ('AAA')",
            "create table child2 (x int, a text, b int)",
            "alter table child2 drop column x",
            "alter table parent attach partition child2 for values in ('BBB')",
            "create table child3 (b int, a text)",
            "alter table parent attach partition child3 for values in ('CCC')",
            "create trigger parent_update_trig after update on parent "
            "referencing old table as old_table new table as new_table "
            "for each statement execute procedure dump_update()",
            "create trigger child3_update_trig after update on child3 "
            "referencing old table as old_table new table as new_table "
            "for each statement execute procedure dump_update()",
            "insert into child3 values (42, 'CCC')",
        ],
        "pre_sqls": [],
        "query": "update parent set b = b + 1",
        "buggy": "error_or_crash",
        "affected": {15: (0, 18), 17: (1, 10)},  # 16.x/17.0/18.0 observed clean,
    },
    {
        # A34 — join.sql:2589-2621 verbatim; expected join.out:6862
        "name": "pg_join_removal_const_null",
        "source": "removed join's PHV const folded to NULL (3e1fe25e6/d610d8e8b)",
        "setup_sqls": [
            "create table t (a int unique, b int)",
            "insert into t values (1,1), (2,2)",
        ],
        "pre_sqls": [],
        "query": (
            "select t1.a, s.* from t t1 "
            "left join lateral (select t2.a, coalesce(t1.a, 1) as c "
            "from t t2 left join t t3 on t2.a = t3.a) s on true "
            "left join t t4 on true where s.a < s.c"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[2, 1, 2], [2, 1, 2]],
        "affected": {17: (1, 10), 18: (1, 5)},  # 16.x/17.0/18.0 observed clean,
    },
    {
        # A14 — partition_prune.sql:411-424 verbatim
        "name": "pg_altsubplan_prune",
        "source": "AlternativeSubPlan left inside runtime-prune qual (94c02bd33)",
        "setup_sqls": [
            "create table int4_tbl (f1 int)",
            "insert into int4_tbl select generate_series(0,5)",
            "create table asptab (id int primary key) partition by range (id)",
            "create table asptab0 partition of asptab for values from (0) to (1)",
            "create table asptab1 partition of asptab for values from (1) to (2)",
        ],
        "pre_sqls": [],
        "query": (
            "select * from "
            "(select exists (select 1 from int4_tbl tinner where f1 = touter.f1) as b "
            "from int4_tbl touter) ss, asptab "
            "where asptab.id > ss.b::int"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "unrecognized node type",
        "affected": {15: (0, 18), 16: (0, 6), 17: (0, 10)},
    },
    {
        # A18 — merge.sql:1725-1745 verbatim shape; MERGE into plain
        # inheritance parent with NO INHERIT check constraint.
        "name": "pg_merge_inherit_parent_wco",
        "source": "MERGE into plain-inheritance parent mishandles WCO/RETURNING (3611794af)",
        "setup_sqls": [
            "create table measurement (city_id int not null, logdate date not null, "
            "peaktemp int, unitsales int)",
            "create table m_y2006m02 (check (logdate >= date '2006-02-01' "
            "and logdate < date '2006-03-01')) inherits (measurement)",
            "alter table measurement add constraint mcheck check (city_id = 0) no inherit",
        ],
        "pre_sqls": [],
        "query": (
            "merge into measurement m "
            "using (values (1, '01-17-2007'::date)) nm(city_id, logdate) "
            "on (m.city_id = nm.city_id and m.logdate = nm.logdate) "
            "when not matched then insert (city_id, logdate, peaktemp, unitsales) "
            "values (city_id - 1, logdate, 25, 100)"
        ),
        "buggy": "error_or_crash",
        "affected": {15: (0, 18), 17: (1, 10)},  # 16.x/17.0 observed clean,
    },
    {
        # A36 — create_table_like.sql:99-102 verbatim; bug #18468
        "name": "pg18468_stats_like_including_all",
        "source": "BUG #18468 (CREATE STATISTICS expr reads dropped-col attno)",
        "setup_sqls": [
            "create table test_like_6 (a int, c text, b text)",
            "create statistics ext_stat on (a || b) from test_like_6",
            "alter table test_like_6 drop column c",
        ],
        "pre_sqls": [],
        "query": "create table test_like_6c (like test_like_6 including all)",
        "buggy": "error_or_crash",
        "affected": {},  # never fired on 16.0/16.2/16.6 - verbatim runs clean
    },
    {
        # A35 — BUG #18550, verbatim alter_table.sql:2387-2426 (16.6 tree).
        # A former inheritance parent attached as partition keeps a stale
        # partdesc; UPDATE on it asserts.  Bug introduced ~16.2 (16.0 clean).
        "name": "pg18550_former_inherit_update",
        "source": "BUG #18550 (stale partition descriptor after INHERITS)",
        "setup_sqls": [
            "create table list_parted (a int not null, b char(2) collate \"C\", "
            "constraint check_a check (a > 0)) partition by list (a)",
            "create table parent (like list_parted)",
            "create table child () inherits (parent)",
            "alter table list_parted attach partition child for values in (1)",
            "drop table child",
            "alter table parent add constraint check_a check (a > 0)",
            "alter table list_parted attach partition parent for values in (1)",
            "insert into parent values (1)",
        ],
        "pre_sqls": [],
        "query": "update parent set a = 2 where a = 1",
        "buggy": "error_or_crash",
        # fixed behavior is a legitimate partition-constraint violation;
        # the bug is an assert crash on a stale partdesc.
        "clean_error": "violates partition constraint",
        "affected": {16: (1, 3)},
    },
    {
        # A37 — GiST index-only scan mis-decodes non-first range_ops column
        "name": "pg_gist_ios_range_ops",
        "source": "GiST IOS tuple mis-decode, range_ops non-first col (release-18:2118)",
        "setup_sqls": [
            "create table gist_ios (a int, r int4range)",
            "create index gist_ios_idx on gist_ios using gist (a, r)",
            "insert into gist_ios select i, int4range(i, i + 2) "
            "from generate_series(1, 100) i",
            "vacuum gist_ios",
        ],
        "pre_sqls": ["set enable_seqscan to off"],
        "query": "select r from gist_ios where a = 42",
        "buggy": "error_or_crash",
        "expected_rows": [["[42, 44)"]],
        "affected": {15: (0, 18), 17: (1, 10), 18: (1, 5)},  # 16.x/17.0/18.0 observed clean,
    },
    {
        # A38 — scrollable plpgsql cursor over a Result-only plan
        "name": "pg_plpgsql_scroll_simple",
        "source": "scrollable cursor on simple SELECT -> unexpected plan node",
        "setup_sqls": [],
        "pre_sqls": [],
        "query": (
            "do $$ declare c scroll cursor for select 1; "
            "begin open c; move last in c; end $$"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "unexpected plan node",
        "affected": {16: (0, 7), 17: (0, 4)},
    },
    {
        # A39 — multi-key RANGE pruning skips DEFAULT (commit 709dfd27f)
        "name": "pg_range_default_multikey",
        "source": "multi-key range pruning wrongly skips DEFAULT partition",
        "setup_sqls": [
            "create table rp(a int, b varchar) partition by range(a, b)",
            "create table rp1 partition of rp for values from (1,'a') to (1,'b')",
            "create table rpd partition of rp default",
            "insert into rp values (0,'z')",
        ],
        "pre_sqls": [],
        "query": "select * from rp where a <= 0 and b = 'z'",
        "buggy": "wrong_rows",
        "expected_rows": [[0, "z"]],
        "affected": {17: (1, 10), 18: (1, 5)},  # 16.x/17.0/18.0 observed clean,
    },
    # --- datatype wrong-value probes (16.2-16.4 era fixes) ---
    {
        # A8 — release-16:3791; ts<origin with diff ≡ 0 mod stride
        "name": "pg16_datebin_neg_stride",
        "source": "date_bin mis-rounds when source<origin, diff%stride==0 (17db5436e)",
        "setup_sqls": [],
        "pre_sqls": [],
        "query": (
            "select date_bin('15 min', timestamp '2022-03-24 04:30:00', "
            "timestamp '2022-03-24 05:15:00')"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [["temporal:2022-03-24T04:30:00"]],
        "buggy_marker": "returns 04:15 (one stride too early)",
        "affected": {15: (0, 18), 16: (0, 2)},
    },
    {
        # A9 — silent timestamp+interval overflow instead of out-of-range error
        "name": "pg16_ts_interval_overflow",
        "source": "timestamp + interval returns wrong result instead of error (3752e3d21)",
        "setup_sqls": [
            "create function ovf() returns int language plpgsql as $$ "
            "begin perform timestamp '294247-01-10 04:00:54' + interval '128 years'; "
            "return 0; exception when others then return 1; end $$",
        ],
        "pre_sqls": [],
        "query": "select ovf()",
        "buggy": "wrong_rows",
        "expected_rows": [[1]],
        "buggy_marker": "silently wraps instead of erroring -> 0",
        "affected": {15: (0, 18)},  # 16.0/16.2 observed clean - overflow already errors there,
    },
    {
        # A10 — money underflow wraps around instead of erroring
        "name": "pg16_money_overflow",
        "source": "money arithmetic wraps instead of out-of-range error (34e9dce69)",
        "setup_sqls": [
            "create function ovf() returns int language plpgsql as $$ "
            "begin perform '-92233720368547758.08'::money - '1'::money; "
            "return 0; exception when others then return 1; end $$",
        ],
        "pre_sqls": [],
        "query": "select ovf()",
        "buggy": "wrong_rows",
        "expected_rows": [[1]],
        "affected": {15: (0, 18), 16: (0, 3)},
    },
    {
        # A11 — numeric scale clamped to +/-2000 hides the difference;
        # compare via <> so Decimal->float normalization can't mask it.
        "name": "pg16_numeric_trunc_clamp",
        "source": "numeric round/trunc scale clamped to +/-2000 (f7aec8c1d)",
        "setup_sqls": [],
        "pre_sqls": [],
        "query": (
            "select (trunc(1 + 1e-2500::numeric, 2500) <> "
            "trunc(1 + 1e-2500::numeric, 2000))::int"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[1]],
        "buggy_marker": "clamp makes both equal -> 0",
        "affected": {15: (0, 18), 16: (0, 3)},
    },
    {
        # A40 — extended-stats lookup ignoring nulling-rel bits (low
        # confidence shape; FAIL on affected builds means refine the repro)
        "name": "pg_mvndistinct_nullingrel",
        "source": "corrupt MVNDistinct entry — stats lookup drops nulling bit",
        "setup_sqls": [
            "create table mvd(a int, b int)",
            "insert into mvd select i % 10, i % 7 from generate_series(1, 500) i",
            "create statistics mvd_s (ndistinct) on a + b from mvd",
            "analyze mvd",
        ],
        "pre_sqls": [],
        "query": (
            "select count(*) from mvd t1 left join mvd t2 "
            "on (t1.a + t1.b) = (t2.a + t2.b)"
        ),
        "buggy": "error_or_crash",
        "buggy_error": "MVNDistinct",
        "affected": {17: (1, 2)},  # 16.x/17.0 observed clean,
    },
    # --- capability-gated audit rows (fire once Step 3 lands) ---
    {
        "name": "pg_ltree_deep_compare",
        "source": "ltree compare() int16 overflow >14,653 labels (release-18:3393)",
        "requires": ["contrib:ltree"],
        "setup_sqls": ["create extension ltree"],
        "pre_sqls": [],
        "query": (
            "select (repeat('a.',14999)||'a')::ltree > 'a'::ltree"
        ),
        "buggy": "wrong_rows",
        "expected_rows": [[True]],
        "affected": {15: (0, 18), 16: (0, 6), 17: (0, 10), 18: (0, 5)},
        # 15.19 observed clean: int16-overflow compare likely introduced in 16
    },
    {
        "name": "pg_btreegist_neq_varlena",
        "source": "btree_gist <> on varlena uses wrong comparator on non-leaf pages",
        "requires": ["contrib:btree_gist"],
        "setup_sqls": [
            "create extension btree_gist",
            "create table bt(t text)",
            "create index bt_idx on bt using gist(t)",
            "insert into bt select 'v' || i from generate_series(1, 2000) i",
        ],
        "pre_sqls": ["set enable_seqscan to off"],
        "query": "select count(*) from bt where t <> 'zzz'",
        "buggy": "wrong_rows",
        "expected_rows": [[2000]],
        "affected": {15: (0, 18), 17: (1, 10), 18: (1, 5)},  # 16.6/17.0/18.0 observed clean
    },
]
