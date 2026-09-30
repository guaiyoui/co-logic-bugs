"""DML snapshot oracle: compare FINAL TABLE STATE across optimizer configs.

Replay-based oracles cannot check UPDATE/DELETE — re-executing a mutating
statement changes its own input. This oracle instead snapshots every base
table after the DML executes, under each plan variant, on a FRESH
connection per variant. The comparison target is the post-state bag, which
is deterministic for a well-specified DML.

Case format (hand templates or generated):
    {"setup_sqls": [...], "dml": "UPDATE ...", "probe": "SELECT ..."}

Verdict = state diff between default and each variant config.

    python scripts/dml_hunt.py --out results/dml_155
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.normalize import loose_bag  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("dml_hunt")

# (name, prelude, post_setup)
#   prelude    — SET/PRAGMA statements applied right after connect, BEFORE
#                the setup statements (so e.g. force_compression shapes how
#                the setup data is physically stored).
#   post_setup — statements run after setup, before the DML (e.g. FORCE
#                CHECKPOINT so compression actually engages on setup data).
# SET/reset pairs mirror oracles/plan_variant.py DUCKDB_VARIANTS; reset is
# unnecessary here because every config runs on a fresh connection.
VARIANTS: list[tuple[str, list[str], list[str]]] = [
    ("default", [], []),
    ("no_optimizer", ["PRAGMA disable_optimizer"], []),
    ("no_filter_pushdown", ["SET disabled_optimizers='filter_pushdown'"], []),
    ("no_join_order",
     ["SET disabled_optimizers='join_order,build_side_probe_side'"], []),
    ("threads1", ["PRAGMA threads=1"], []),
    # Executor-mode axes (plan_variant.py proven):
    ("external_exec", ["SET debug_force_external=true"], []),
    ("window_combine", ["SET debug_window_mode='combine'"], []),
    ("window_separate", ["SET debug_window_mode='separate'"], []),
    ("no_perfect_ht", ["SET perfect_ht_threshold=0"], []),
    # NB: threshold=0 is rejected on 1.5.5 ("value must be positive") — 1 is
    # the minimal legal value and still forces the ordered-agg path early.
    ("ordered_agg_off", ["SET ordered_aggregate_threshold=1"], []),
    ("no_preserve_order", ["SET preserve_insertion_order=false"], []),
    # Vector-verification axes:
    ("verify_vconst", ["SET debug_verify_vector='CONSTANT_OPERATOR'"], []),
    ("verify_vdict", ["SET debug_verify_vector='DICTIONARY_OPERATOR'"], []),
    # Storage/checkpoint axes: compression only engages when data is
    # checkpointed, so FORCE CHECKPOINT runs post-setup.
    ("checkpoint", [], ["FORCE CHECKPOINT"]),
    ("compress_dict", ["SET force_compression='dictionary'"],
     ["FORCE CHECKPOINT"]),
    ("compress_fsst", ["SET force_compression='fsst'"], ["FORCE CHECKPOINT"]),
]

# (tag, setup, dml, probe) — probe reads final state of all touched tables.
DML_CASES: list[tuple[str, list[str], str, str]] = [
    ("update_corr",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,100),(2,200)"],
     "UPDATE t SET b = (SELECT MAX(u.w) FROM u WHERE u.a = t.a) WHERE a <= 2",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_from",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,100),(3,300)"],
     "UPDATE t SET b = u.w FROM u WHERE t.a = u.a",
     "SELECT a, b FROM t ORDER BY a"),
    ("delete_exists",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2),(3),(4)",
      "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (2),(4)"],
     "DELETE FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a)",
     "SELECT a FROM t ORDER BY a"),
    ("delete_notin_null",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2),(3)",
      "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (2),(NULL)"],
     "DELETE FROM t WHERE a NOT IN (SELECT a FROM u)",
     "SELECT a FROM t ORDER BY a"),
    ("insert_select_agg",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,10),(1,20),(2,5)",
      "CREATE TABLE r(g INT, s INT)"],
     "INSERT INTO r SELECT g, SUM(v) FROM t GROUP BY g",
     "SELECT g, s FROM r ORDER BY g"),
    ("insert_select_window",
     ["CREATE TABLE t(v INT)",
      "INSERT INTO t VALUES (3),(1),(2)",
      "CREATE TABLE r(v INT, rn INT)"],
     "INSERT INTO r SELECT v, ROW_NUMBER() OVER (ORDER BY v) FROM t",
     "SELECT v, rn FROM r ORDER BY rn"),
    ("update_case",
     ["CREATE TABLE t(a INT, b VARCHAR)",
      "INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'z')"],
     "UPDATE t SET b = CASE WHEN a = 1 THEN 'A' WHEN a = 2 THEN 'B' ELSE 'C' END",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_self_join",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)"],
     "UPDATE t SET b = t2.b + 1 FROM t t2 WHERE t.a = t2.a AND t.a > 1",
     "SELECT a, b FROM t ORDER BY a"),
    ("delete_limit",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2),(3),(4),(5)"],
     "DELETE FROM t WHERE a IN (SELECT a FROM t ORDER BY a DESC LIMIT 2)",
     "SELECT a FROM t ORDER BY a"),
    ("update_null_coalesce",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,NULL),(2,5),(NULL,9)"],
     "UPDATE t SET b = COALESCE(b, 0) + a WHERE a IS NOT NULL",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_select_join",
     ["CREATE TABLE t(a INT, v INT)", "CREATE TABLE u(a INT, w INT)",
      "INSERT INTO t VALUES (1,10),(2,20)", "INSERT INTO u VALUES (1,5),(3,7)",
      "CREATE TABLE r(x INT, y INT)"],
     "INSERT INTO r SELECT t.a, t.v + u.w FROM t JOIN u ON t.a = u.a",
     "SELECT x, y FROM r ORDER BY x"),
    ("update_agg_subq",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,10),(1,20),(2,5)",
      "CREATE TABLE stats(g INT, mx INT)"],
     "INSERT INTO stats SELECT DISTINCT g, 0 FROM t",
     "SELECT g, mx FROM stats ORDER BY g"),
    # ------------------------------------------------------------ batch 2
    ("update_exists",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (1),(3)"],
     "UPDATE t SET b = 0 WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a)",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_corr_expr",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,5),(1,7),(2,3)"],
     "UPDATE t SET b = b + (SELECT SUM(u.w) FROM u WHERE u.a = t.a)",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_in_subq",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (2),(3),(NULL)"],
     "UPDATE t SET b = -1 WHERE a IN (SELECT a FROM u)",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_from_agg",
     ["CREATE TABLE t(a INT, tot INT)",
      "INSERT INTO t VALUES (1,0),(2,0)",
      "CREATE TABLE u(a INT, w INT)",
      "INSERT INTO u VALUES (1,5),(1,7),(2,3)"],
     "UPDATE t SET tot = s.sumw FROM (SELECT a, SUM(w) sumw FROM u GROUP BY a) s WHERE t.a = s.a",
     "SELECT a, tot FROM t ORDER BY a"),
    ("delete_using",
     ["CREATE TABLE t(a INT)", "CREATE TABLE u(a INT)",
      "INSERT INTO t VALUES (1),(2),(3),(4)", "INSERT INTO u VALUES (2),(4)"],
     "DELETE FROM t USING u WHERE t.a = u.a",
     "SELECT a FROM t ORDER BY a"),
    ("delete_corr",
     ["CREATE TABLE t(a INT)", "CREATE TABLE u(a INT, w INT)",
      "INSERT INTO t VALUES (1),(2),(3)", "INSERT INTO u VALUES (1,5),(2,100)"],
     "DELETE FROM t WHERE a IN (SELECT a FROM u WHERE w > 50)",
     "SELECT a FROM t ORDER BY a"),
    ("insert_select_union",
     ["CREATE TABLE t(a INT)", "CREATE TABLE u(a INT)",
      "INSERT INTO t VALUES (1),(2)", "INSERT INTO u VALUES (2),(3)",
      "CREATE TABLE r(a INT)"],
     "INSERT INTO r SELECT a FROM t UNION SELECT a FROM u",
     "SELECT a FROM r ORDER BY a"),
    ("insert_select_distinct",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,1),(1,1),(1,2),(2,1)",
      "CREATE TABLE r(a INT, b INT)"],
     "INSERT INTO r SELECT DISTINCT a, b FROM t",
     "SELECT a, b FROM r ORDER BY a, b"),
    ("insert_select_cte",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2),(3)",
      "CREATE TABLE r(a INT)"],
     "INSERT INTO r WITH c AS (SELECT a*2 x FROM t) SELECT x FROM c WHERE x > 2",
     "SELECT a FROM r ORDER BY a"),
    ("ctas_window",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,10),(1,20),(2,5)"],
     "CREATE TABLE r AS SELECT g, v, ROW_NUMBER() OVER (PARTITION BY g ORDER BY v DESC) rn FROM t",
     "SELECT g, v, rn FROM r ORDER BY g, rn"),
    ("ctas_groupby",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,10),(1,20),(2,NULL)"],
     "CREATE TABLE r AS SELECT g, COUNT(*) c, SUM(v) s FROM t GROUP BY g",
     "SELECT g, c, s FROM r ORDER BY g"),
    ("on_conflict_nothing",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "INSERT INTO t VALUES (2,99),(3,30) ON CONFLICT DO NOTHING",
     "SELECT a, b FROM t ORDER BY a"),
    ("on_conflict_update",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "INSERT INTO t VALUES (2,99),(3,30) ON CONFLICT (a) DO UPDATE SET b = excluded.b + 1",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_all_rows",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)"],
     "UPDATE t SET b = b * 2 WHERE true",
     "SELECT a, b FROM t ORDER BY a"),
    ("delete_all",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2),(3)"],
     "DELETE FROM t",
     "SELECT a FROM t ORDER BY a"),
    ("update_expr_types",
     ["CREATE TABLE t(a INT, b DOUBLE)",
      "INSERT INTO t VALUES (1,1.5),(2,2.5)"],
     "UPDATE t SET b = a * 1.5 + b",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_nested_corr",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,0),(2,0)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,5),(2,9)"],
     "UPDATE t SET b = (SELECT w FROM u WHERE u.a = t.a) + (SELECT COUNT(*) FROM u WHERE u.a = t.a)",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_default_cols",
     ["CREATE TABLE t(a INT DEFAULT 7, b VARCHAR DEFAULT 'd')",
      "INSERT INTO t VALUES (1,'x')"],
     "INSERT INTO t (b) VALUES ('y')",
     "SELECT a, b FROM t ORDER BY b, a"),
    ("update_null_join",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(NULL,30)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,5),(NULL,9)"],
     "UPDATE t SET b = COALESCE(u.w, -1) FROM u WHERE t.a = u.a",
     "SELECT a, b FROM t ORDER BY a"),
    ("delete_subq_agg",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,1),(1,9),(2,5),(3,2)"],
     "DELETE FROM t WHERE v > (SELECT AVG(v) FROM t)",
     "SELECT g, v FROM t ORDER BY g"),
    ("insert_select_order_limit",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (3),(1),(2)",
      "CREATE TABLE r(a INT)"],
     "INSERT INTO r SELECT a FROM t ORDER BY a LIMIT 2",
     "SELECT a FROM r ORDER BY a"),
    ("update_set_row",
     ["CREATE TABLE t(a INT, b INT, c INT)",
      "INSERT INTO t VALUES (1,10,100),(2,20,200)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,5),(2,7)"],
     "UPDATE t SET (b, c) = ((SELECT w FROM u WHERE u.a = t.a), (SELECT w * 2 FROM u WHERE u.a = t.a))",
     "SELECT a, b, c FROM t ORDER BY a"),
    ("insert_select_case",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2),(3)",
      "CREATE TABLE r(a INT, lbl VARCHAR)"],
     "INSERT INTO r SELECT a, CASE WHEN a > 1 THEN 'big' ELSE 'small' END FROM t",
     "SELECT a, lbl FROM r ORDER BY a"),
    ("update_from_self_agg",
     ["CREATE TABLE t(g INT, v INT, mx INT)",
      "INSERT INTO t VALUES (1,10,NULL),(1,20,NULL),(2,5,NULL)"],
     "UPDATE t SET mx = s.m FROM (SELECT g, MAX(v) m FROM t GROUP BY g) s WHERE t.g = s.g",
     "SELECT g, v, mx FROM t ORDER BY g, v"),
    ("delete_window_subq",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,1),(1,2),(1,3),(2,5),(2,6)"],
     "DELETE FROM t WHERE (g, v) IN (SELECT g, v FROM (SELECT g, v, ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) rn FROM t) s WHERE rn > 2)",
     "SELECT g, v FROM t ORDER BY g, v"),
    # ------------------------------------------------------------ batch 3
    # MERGE INTO (added in DuckDB 1.4; grammar verified on 1.5.5)
    ("merge_basic",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)",
      "CREATE TABLE s(a INT, b INT)",
      "INSERT INTO s VALUES (2,99),(3,30)"],
     "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET b = s.b WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.b)",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_delete_cond",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE s(a INT, b INT)",
      "INSERT INTO s VALUES (1,5),(2,50),(4,40)"],
     "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED AND s.b > 25 THEN DELETE WHEN MATCHED THEN UPDATE SET b = s.b WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.b)",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_nmbs_delete",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE s(a INT, b INT)",
      "INSERT INTO s VALUES (2,99)"],
     "MERGE INTO t USING s ON t.a = s.a WHEN NOT MATCHED BY SOURCE THEN DELETE",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_nmbs_update",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE s(a INT, b INT)",
      "INSERT INTO s VALUES (2,99)"],
     "MERGE INTO t USING s ON t.a = s.a WHEN NOT MATCHED BY SOURCE THEN UPDATE SET b = 0 WHEN MATCHED THEN UPDATE SET b = s.b",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_subq_source",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)",
      "CREATE TABLE s(a INT, b INT)",
      "INSERT INTO s VALUES (2,99),(2,98),(3,30)"],
     "MERGE INTO t USING (SELECT a, MAX(b) b FROM s GROUP BY a) s ON t.a = s.a WHEN MATCHED THEN UPDATE SET b = s.b WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.b)",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_self",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "MERGE INTO t USING (SELECT a, b * 2 b FROM t) s ON t.a = s.a WHEN MATCHED THEN UPDATE SET b = s.b",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_null_keys",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(NULL,20)",
      "CREATE TABLE s(a INT, b INT)",
      "INSERT INTO s VALUES (1,99),(NULL,30)"],
     "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET b = s.b WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.b)",
     "SELECT a, b FROM t ORDER BY a"),
    # nondeterminism probe: duplicate source keys match one target row;
    # which source row wins is unspecified — a diff here is a real signal
    # but not necessarily an optimizer bug.
    ("merge_dup_source",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10)",
      "CREATE TABLE s(a INT, b INT)",
      "INSERT INTO s VALUES (1,5),(1,9)"],
     "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET b = s.b",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_do_nothing",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10)",
      "CREATE TABLE s(a INT, b INT)",
      "INSERT INTO s VALUES (1,5),(2,7)"],
     "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DO NOTHING WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.b)",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_partial_cols",
     ["CREATE TABLE t(a INT, b INT DEFAULT -1, c INT)",
      "INSERT INTO t VALUES (1,10,100)",
      "CREATE TABLE s(a INT)",
      "INSERT INTO s VALUES (1),(5)"],
     "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET c = 0 WHEN NOT MATCHED THEN INSERT (a, c) VALUES (s.a, 0)",
     "SELECT a, b, c FROM t ORDER BY a"),
    # UPDATE — new shapes
    ("update_from_two_table_join",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,5),(2,7)",
      "CREATE TABLE v(w INT, x INT)", "INSERT INTO v VALUES (5,50),(7,70)"],
     "UPDATE t SET b = u.w + v.x FROM u JOIN v ON u.w = v.w WHERE t.a = u.a",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_from_values",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,0),(2,0)"],
     "UPDATE t SET b = s.w FROM (VALUES (1,100),(2,200)) s(a,w) WHERE t.a = s.a",
     "SELECT a, b FROM t ORDER BY a"),
    # nondeterminism probe: duplicate join keys in FROM
    ("update_from_dupkey",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,0)",
      "CREATE TABLE u(a INT, w INT)",
      "INSERT INTO u VALUES (1,5),(1,9)"],
     "UPDATE t SET b = u.w FROM u WHERE t.a = u.a",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_window_subq",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,0),(2,0),(3,0)"],
     "UPDATE t SET b = (SELECT rn FROM (SELECT a, ROW_NUMBER() OVER (ORDER BY a DESC) rn FROM t) s WHERE s.a = t.a)",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_check_ok",
     ["CREATE TABLE t(a INT CHECK (a > 0), b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)"],
     "UPDATE t SET a = CASE WHEN b > 15 THEN a + 10 ELSE a END",
     "SELECT a, b FROM t ORDER BY a"),
    # constraint-violation probe: every config must reject this; a variant
    # that silently accepts it is a write-path bug (error asymmetry).
    ("update_check_violate",
     ["CREATE TABLE t(a INT CHECK (a > 0), b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "UPDATE t SET a = -a WHERE a = 2",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_gen_col",
     ["CREATE TABLE t(a INT, b INT GENERATED ALWAYS AS (a * 2))",
      "INSERT INTO t (a) VALUES (1),(2),(3)"],
     "UPDATE t SET a = a + 10 WHERE a <= 2",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_gen_col_virtual",
     ["CREATE TABLE t(a INT, b INT GENERATED ALWAYS AS (a * 2) VIRTUAL)",
      "INSERT INTO t (a) VALUES (1),(2),(3)"],
     "UPDATE t SET a = a + 10 WHERE a <= 2",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_list_col",
     ["CREATE TABLE t(a INT, l INT[])",
      "INSERT INTO t VALUES (1,[1,2]),(2,[3]),(3,NULL)"],
     "UPDATE t SET l = LIST_APPEND(COALESCE(l, []), a)",
     "SELECT a, l FROM t ORDER BY a"),
    ("update_struct_col",
     ["CREATE TABLE t(a INT, s STRUCT(x INT, y VARCHAR))",
      "INSERT INTO t VALUES (1,{'x':1,'y':'a'}),(2,{'x':2,'y':'b'})"],
     "UPDATE t SET s = {'x': s.x + 10, 'y': s.y || 'z'}",
     "SELECT a, s FROM t ORDER BY a"),
    ("update_map_col",
     ["CREATE TABLE t(a INT, m MAP(VARCHAR, INT))",
      "INSERT INTO t VALUES (1, MAP {'x':1}), (2, MAP {'y':2})"],
     "UPDATE t SET m = MAP {'k': a} WHERE a = 1",
     "SELECT a, m FROM t ORDER BY a"),
    ("update_seq_default",
     ["CREATE SEQUENCE sq START 10",
      "CREATE TABLE t(a INT DEFAULT nextval('sq'), b INT)",
      "INSERT INTO t (b) VALUES (1),(2)"],
     "UPDATE t SET b = b * 10",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_rowid",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (10),(20),(30)"],
     "UPDATE t SET a = 99 WHERE rowid = 0",
     "SELECT a FROM t ORDER BY a"),
    ("update_returning",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "UPDATE t SET b = b + 1 RETURNING a, b",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_from_self_join_other",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,5),(2,7)"],
     "UPDATE t SET b = t2.b + u.w FROM t t2 JOIN u ON t2.a = u.a WHERE t.a = t2.a",
     "SELECT a, b FROM t ORDER BY a"),
    # DELETE — new shapes
    ("delete_using_join",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3)",
      "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (1),(2)",
      "CREATE TABLE v(a INT)", "INSERT INTO v VALUES (2),(3)"],
     "DELETE FROM t USING u JOIN v ON u.a = v.a WHERE t.a = u.a",
     "SELECT a FROM t ORDER BY a"),
    ("delete_in_having",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3)",
      "CREATE TABLE u(a INT, w INT)",
      "INSERT INTO u VALUES (1,5),(1,6),(2,7),(3,1)"],
     "DELETE FROM t WHERE a IN (SELECT a FROM u GROUP BY a HAVING SUM(w) > 7)",
     "SELECT a FROM t ORDER BY a"),
    ("delete_qualify_subq",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,1),(1,2),(2,5)"],
     "DELETE FROM t WHERE (g, v) IN (SELECT g, v FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) = 1)",
     "SELECT g, v FROM t ORDER BY g, v"),
    ("delete_returning",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3)"],
     "DELETE FROM t WHERE a = 2 RETURNING a",
     "SELECT a FROM t ORDER BY a"),
    ("delete_rowid",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (10),(20),(30)"],
     "DELETE FROM t WHERE rowid = 1",
     "SELECT a FROM t ORDER BY a"),
    # constraint-violation probe (see update_check_violate)
    ("delete_fk_violate",
     ["CREATE TABLE p(a INT PRIMARY KEY)", "INSERT INTO p VALUES (1),(2)",
      "CREATE TABLE c(x INT REFERENCES p(a))", "INSERT INTO c VALUES (1)"],
     "DELETE FROM p WHERE a = 1",
     "SELECT a FROM p ORDER BY a"),
    ("delete_using_self",
     ["CREATE TABLE t(a INT, keep INT)",
      "INSERT INTO t VALUES (1,0),(1,1),(2,0)"],
     "DELETE FROM t USING t t2 WHERE t.a = t2.a AND t.keep < t2.keep",
     "SELECT a, keep FROM t ORDER BY a, keep"),
    # INSERT — new shapes
    ("insert_topk_offset",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3),(4)",
      "CREATE TABLE r(a INT)"],
     "INSERT INTO r SELECT a FROM t ORDER BY a DESC LIMIT 2 OFFSET 1",
     "SELECT a FROM r ORDER BY a"),
    ("insert_grouping_sets",
     ["CREATE TABLE t(g INT, h INT, v INT)",
      "INSERT INTO t VALUES (1,1,10),(1,2,20),(2,1,5)",
      "CREATE TABLE r(g INT, h INT, s BIGINT)"],
     "INSERT INTO r SELECT g, h, SUM(v) FROM t GROUP BY GROUPING SETS ((g),(g,h),())",
     "SELECT g, h, s FROM r ORDER BY g, h"),
    ("insert_pivot",
     ["CREATE TABLE t(g VARCHAR, v INT)",
      "INSERT INTO t VALUES ('a',1),('b',2),('a',3)",
      "CREATE TABLE r(a BIGINT, b BIGINT)"],
     "INSERT INTO r SELECT * FROM (PIVOT t ON g IN ('a','b') USING SUM(v))",
     "SELECT a, b FROM r"),
    ("insert_distinct_agg",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,1),(1,1),(1,2),(2,2)",
      "CREATE TABLE r(g INT, c BIGINT)"],
     "INSERT INTO r SELECT g, COUNT(DISTINCT v) FROM t GROUP BY g",
     "SELECT g, c FROM r ORDER BY g"),
    ("insert_except",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3)",
      "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (2),(3),(4)",
      "CREATE TABLE r(a INT)"],
     "INSERT INTO r SELECT a FROM t EXCEPT SELECT a FROM u",
     "SELECT a FROM r ORDER BY a"),
    ("insert_intersect",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3)",
      "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (2),(3),(4)",
      "CREATE TABLE r(a INT)"],
     "INSERT INTO r SELECT a FROM t INTERSECT SELECT a FROM u",
     "SELECT a FROM r ORDER BY a"),
    ("insert_by_name",
     ["CREATE TABLE t(a INT, b INT)", "INSERT INTO t VALUES (1,10)",
      "CREATE TABLE r(a INT, b INT)"],
     "INSERT INTO r BY NAME SELECT b, a FROM t",
     "SELECT a, b FROM r ORDER BY a"),
    ("insert_self",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2)"],
     "INSERT INTO t SELECT a + 10 FROM t",
     "SELECT a FROM t ORDER BY a"),
    ("insert_qualify_top",
     ["CREATE TABLE t(a INT, v INT)",
      "INSERT INTO t VALUES (1,5),(2,7),(3,9)",
      "CREATE TABLE r(a INT, v INT)"],
     "INSERT INTO r SELECT a, v FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) <= 2",
     "SELECT a, v FROM r ORDER BY a"),
    ("insert_two_windows",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,1),(1,2),(2,5),(2,6)",
      "CREATE TABLE r(g INT, v INT, r1 BIGINT, r2 BIGINT)"],
     "INSERT INTO r SELECT g, v, ROW_NUMBER() OVER (PARTITION BY g ORDER BY v), RANK() OVER (PARTITION BY g ORDER BY v DESC) FROM t",
     "SELECT g, v, r1, r2 FROM r ORDER BY g, v"),
    ("insert_seq_nextval",
     ["CREATE SEQUENCE sq2 START 100",
      "CREATE TABLE t(a INT, b VARCHAR)"],
     "INSERT INTO t VALUES (nextval('sq2'), 'x'), (nextval('sq2'), 'y')",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_self_window",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,1),(1,2),(2,5)"],
     "INSERT INTO t SELECT g, v * 10 + ROW_NUMBER() OVER (ORDER BY g, v) FROM t",
     "SELECT g, v FROM t ORDER BY g, v"),
    # ON CONFLICT — extended shapes (partial-index WHERE on the conflict
    # target is NOT supported on 1.5.5; DO UPDATE ... WHERE is)
    ("conflict_do_update_where",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "INSERT INTO t VALUES (2,99),(3,30) ON CONFLICT (a) DO UPDATE SET b = excluded.b WHERE t.b < 50",
     "SELECT a, b FROM t ORDER BY a"),
    ("conflict_multi_col_target",
     ["CREATE TABLE t(a INT, b INT, c INT, PRIMARY KEY (a, b))",
      "INSERT INTO t VALUES (1,1,10),(2,2,20)"],
     "INSERT INTO t VALUES (1,1,99),(3,3,30) ON CONFLICT (a,b) DO UPDATE SET c = excluded.c",
     "SELECT a, b, c FROM t ORDER BY a, b"),
    ("conflict_target_expr",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10)"],
     "INSERT INTO t VALUES (1,99) ON CONFLICT (a) DO UPDATE SET b = t.b + excluded.b",
     "SELECT a, b FROM t ORDER BY a"),
    ("conflict_second_unique",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT UNIQUE)",
      "INSERT INTO t VALUES (1,10)"],
     "INSERT INTO t VALUES (2,10) ON CONFLICT (b) DO UPDATE SET a = excluded.a",
     "SELECT a, b FROM t ORDER BY a"),
    ("conflict_multi_row_same_key",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10)"],
     "INSERT INTO t VALUES (1,99),(1,98),(2,20) ON CONFLICT (a) DO UPDATE SET b = excluded.b",
     "SELECT a, b FROM t ORDER BY a"),
    ("conflict_returning",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10)"],
     "INSERT INTO t VALUES (1,99),(2,20) ON CONFLICT (a) DO UPDATE SET b = excluded.b RETURNING a, b",
     "SELECT a, b FROM t ORDER BY a"),
    # cross-database write path (in-memory ATTACH keeps it self-contained)
    ("attach_insert_select",
     ["ATTACH ':memory:' AS aux",
      "CREATE TABLE aux.src(a INT)", "INSERT INTO aux.src VALUES (1),(2),(3)",
      "CREATE TABLE r(a INT)"],
     "INSERT INTO r SELECT a FROM aux.src WHERE a > 1",
     "SELECT a FROM r ORDER BY a"),
    ("attach_update_from",
     ["ATTACH ':memory:' AS aux",
      "CREATE TABLE aux.src(a INT)", "INSERT INTO aux.src VALUES (1),(2),(3)",
      "CREATE TABLE t(a INT, b INT)", "INSERT INTO t VALUES (1,0),(2,0),(3,0)"],
     "UPDATE t SET b = aux.src.a * 10 FROM aux.src WHERE t.a = aux.src.a",
     "SELECT a, b FROM t ORDER BY a"),
    ("attach_delete_using",
     ["ATTACH ':memory:' AS aux",
      "CREATE TABLE aux.src(a INT)", "INSERT INTO aux.src VALUES (2),(3)",
      "CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3)"],
     "DELETE FROM t USING aux.src WHERE t.a = aux.src.a",
     "SELECT a FROM t ORDER BY a"),
    ("attach_write_into_aux",
     ["ATTACH ':memory:' AS aux",
      "CREATE TABLE t(a INT)", "INSERT INTO t VALUES (1),(2),(3)",
      "CREATE TABLE aux.dst(a INT)"],
     "INSERT INTO aux.dst SELECT a * 2 FROM t WHERE a >= 2",
     "SELECT a FROM aux.dst ORDER BY a"),
    # CTAS extras
    ("ctas_order_limit",
     ["CREATE TABLE t(a INT)", "INSERT INTO t VALUES (3),(1),(2)"],
     "CREATE TABLE r AS SELECT a FROM t ORDER BY a LIMIT 2",
     "SELECT a FROM r ORDER BY a"),
    ("ctas_pivot",
     ["CREATE TABLE t(g VARCHAR, v INT)",
      "INSERT INTO t VALUES ('a',1),('b',2),('a',3)"],
     "CREATE TABLE r AS PIVOT t ON g IN ('a','b') USING SUM(v)",
     "SELECT a, b FROM r"),
    # larger-volume cases so external/verify/compression variants do real work
    ("update_big",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t SELECT range, range * 2 FROM range(5000)"],
     "UPDATE t SET b = CASE WHEN a % 3 = 0 THEN b + 1 WHEN a % 3 = 1 THEN b - 1 ELSE b END",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_select_big_agg",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t SELECT range % 50, range FROM range(5000)",
      "CREATE TABLE r(g INT, s BIGINT, c BIGINT)"],
     "INSERT INTO r SELECT g, SUM(v), COUNT(*) FROM t GROUP BY g",
     "SELECT g, s, c FROM r ORDER BY g"),
]


def snapshot(con, tables: list[str]):
    state = {}
    for t in tables:
        res = con.execute(f"SELECT * FROM {t}").fetchall()
        state[t] = loose_bag(res)
    return state


def _err(stage: str, error: str, internal: bool = False) -> dict:
    return {"stage": stage, "error": error[:400], "internal": internal}


def run_config(setup, dml, probe, tables, prelude, post_setup):
    """Fresh connection: connect, prelude, setup, post_setup, dml, snapshot.

    Returns ((state, probe_bag), None) on success or (None, err_dict).
    NB: prelude must be applied on the SAME connection that runs the DML —
    DuckDBRunner.setup() reconnects internally, so setup statements are run
    via run() here instead of through runner.setup().
    """
    con = DuckDBRunner(version_tag="dml")
    try:
        con.connect()
        for stmt in prelude:
            r = con.run(stmt, timeout_s=10.0)
            if not r.ok:
                return None, _err("prelude", r.error, r.is_internal_error)
        for stmt in setup:
            r = con.run(stmt, timeout_s=10.0)
            if not r.ok:
                return None, _err("setup", f"{r.error} [{stmt[:120]}]",
                                 r.is_internal_error)
        for stmt in post_setup:
            r = con.run(stmt, timeout_s=10.0)
            if not r.ok:
                return None, _err("post_setup", r.error, r.is_internal_error)
        d = con.run(dml, timeout_s=15.0)
        if not d.ok:
            return None, _err("dml", d.error, d.is_internal_error)
        try:
            st = snapshot(con._conn, tables)
        except Exception as exc:  # noqa: BLE001
            return None, _err("snapshot", str(exc))
        p = con.run(probe, timeout_s=15.0)
        if not p.ok:
            return None, _err("probe", p.error, p.is_internal_error)
        return (st, loose_bag(p.rows)), None
    finally:
        con.close()


# CREATE TABLE [aux.]name / OR REPLACE / IF NOT EXISTS — capture the
# (possibly schema-qualified) name so snapshots can read attached tables.
_TABLE_RE = re.compile(
    r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?(?:OR\s+REPLACE\s+)?TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(\w+(?:\.\w+)?)",
    re.IGNORECASE,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/dml_155")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    stats = {"cases": 0, "setup_ok": 0, "dml_ok": 0, "diffs": 0,
             "case_errors": 0, "variants": len(VARIANTS)}
    diffs = []
    case_errors = []
    t0 = time.time()

    # Pre-flight: every variant's prelude/post_setup must apply cleanly —
    # a rejected SET would otherwise surface as a bogus error asymmetry on
    # every case.
    scratch = DuckDBRunner(version_tag="dml")
    scratch.connect()
    live_variants = []
    for vname, prelude, post_setup in VARIANTS:
        ok = True
        for stmt in prelude + post_setup:
            r = scratch.run(stmt, timeout_s=10.0)
            if not r.ok:
                LOGGER.warning("variant %s disabled: %r fails: %s",
                               vname, stmt, r.error[:160])
                ok = False
                break
        if ok:
            live_variants.append((vname, prelude, post_setup))
    scratch.close()
    stats["variants"] = len(live_variants)
    stats["disabled_variants"] = [v for v, _, _ in VARIANTS
                                  if v not in {lv for lv, _, _ in live_variants}]
    if stats["disabled_variants"]:
        LOGGER.warning("disabled variants: %s", stats["disabled_variants"])
    run_variants = live_variants[1:]  # skip "default" — it is the baseline
    for tag, setup, dml, probe in DML_CASES:
        stats["cases"] += 1
        # extract table names touched by setup (CREATE TABLE [db.]x)
        tables = sorted(set(_TABLE_RE.findall(" ".join(setup))))
        with eff.phase("dml_check"):
            base, berr = run_config(setup, dml, probe, tables, [], [])
            if berr and berr["stage"] in ("prelude", "setup"):
                stats["case_errors"] += 1
                case_errors.append({"tag": tag, **berr})
                LOGGER.warning("case %s baseline %s error: %s",
                               tag, berr["stage"], berr["error"][:160])
                continue
            stats["setup_ok"] += 1
            if berr is None:
                stats["dml_ok"] += 1
            else:
                # baseline DML/probe failed — still run variants: a variant
                # that silently ACCEPTS e.g. a constraint violation is a bug.
                case_errors.append({"tag": tag, **berr})
                LOGGER.info("case %s baseline %s error (variants still "
                            "checked): %s", tag, berr["stage"],
                            berr["error"][:160])
            eff.count("queries_executed")
            for vname, prelude, post_setup in run_variants:
                alt, aerr = run_config(setup, dml, probe, tables,
                                       prelude, post_setup)
                rec = None
                if (berr is not None) or (aerr is not None):
                    base_internal = bool(berr and berr.get("internal"))
                    alt_internal = bool(aerr and aerr.get("internal"))
                    if base_internal or alt_internal:
                        rec = {"kind": "internal_error"}
                    elif (berr is None) != (aerr is None):
                        rec = {"kind": "error_asymmetry"}
                elif alt != base:
                    rec = {"kind": "state_diff"}
                if rec is not None:
                    stats["diffs"] += 1
                    rec.update({
                        "tag": tag, "variant": vname, "setup": setup,
                        "dml": dml, "probe": probe,
                        "base_err": berr, "alt_err": aerr,
                        "default_state": repr(base)[:500],
                        "variant_state": repr(alt)[:500],
                    })
                    diffs.append(rec)
                    LOGGER.info("DIFF[%s] %s under %s",
                                rec["kind"], tag, vname)
    with open(os.path.join(args.out, "diffs.jsonl"), "w") as fh:
        for d in diffs:
            fh.write(json.dumps(d, default=str) + "\n")
    with open(os.path.join(args.out, "case_errors.jsonl"), "w") as fh:
        for d in case_errors:
            fh.write(json.dumps(d, default=str) + "\n")
    summary = {**stats, "efficiency": eff.snapshot(),
               "elapsed_s": time.time() - t0}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
