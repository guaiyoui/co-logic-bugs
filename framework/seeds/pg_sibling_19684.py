"""Sibling-mutation probes around BUG #19684 (zero-column set-op dedup under
forced parallelism -> Gather-path create_sort_path lacks the groupList!=NIL
guard the Append path has -> Assert(nkeys > 0)).

Verbatim: SELECT FROM t UNION SELECT FROM t with
  cpu_tuple_cost=1000 + min_parallel_table_scan_size=1
fires Assert on 17.0->20devel (16.x plans differently, stays clean).

Mutations: INTERSECT / EXCEPT / ALL-variants / 3-arm / mixed / subquery /
CTE / LIMIT / ORDER BY / EXISTS-subquery / zero-column TABLE / generate_
series / partitioned append / aggregate arms / GROUP BY () control.
All arms carry the same two GUCs as pre_sqls.
"""

GUC = [
    "SET cpu_tuple_cost = 1000",
    "SET min_parallel_table_scan_size = 1",
]
T1 = ["CREATE TABLE t(i int)"]
T2 = ["CREATE TABLE t(i int)", "CREATE TABLE t2(i int)"]
TZ = ["CREATE TABLE z()"]
TPART = [
    "CREATE TABLE p(i int) PARTITION BY HASH(i)",
    "CREATE TABLE p0 PARTITION OF p FOR VALUES WITH (modulus 2, remainder 0)",
    "CREATE TABLE p1 PARTITION OF p FOR VALUES WITH (modulus 2, remainder 1)",
]


def _case(name, source, setup, query, buggy_error=None, extra_pre=None):
    return {
        "name": name,
        "source": source,
        "setup_sqls": setup,
        "pre_sqls": GUC + list(extra_pre or ()),
        "query": query,
        "buggy": "error_or_crash",
        "buggy_error": buggy_error,
        "affected": {17: (0, 11), 18: (0, 6), 20: (0, 0)},
    }


PROBES = [
    _case("s19684_verbatim",
          "#19684 verbatim control: zero-col UNION under parallel GUCs.",
          T1, "SELECT FROM t UNION SELECT FROM t"),
    _case("s19684_intersect",
          "zero-col INTERSECT — same dedup sort requirement.",
          T1, "SELECT FROM t INTERSECT SELECT FROM t"),
    _case("s19684_except",
          "zero-col EXCEPT.",
          T1, "SELECT FROM t EXCEPT SELECT FROM t"),
    _case("s19684_intersect_all",
          "zero-col INTERSECT ALL (keeps multiplicities, still groups).",
          T1, "SELECT FROM t INTERSECT ALL SELECT FROM t"),
    _case("s19684_except_all",
          "zero-col EXCEPT ALL.",
          T1, "SELECT FROM t EXCEPT ALL SELECT FROM t"),
    _case("s19684_union_all_control",
          "UNION ALL needs no dedup -> expected clean control.",
          T1, "SELECT FROM t UNION ALL SELECT FROM t"),
    _case("s19684_union3",
          "three-arm zero-col UNION (recursive setop tree).",
          T1, "SELECT FROM t UNION SELECT FROM t UNION SELECT FROM t"),
    _case("s19684_mixed_union_unionall",
          "UNION over a UNION ALL arm: dedup still required on top.",
          T1,
          "SELECT FROM t UNION SELECT FROM t UNION ALL SELECT FROM t"),
    _case("s19684_union_subquery",
          "zero-col UNION inside a subquery, aggregated outside.",
          T1,
          "SELECT count(*) FROM (SELECT FROM t UNION SELECT FROM t) s"),
    _case("s19684_union_cte",
          "zero-col UNION inside a CTE.",
          T1,
          "WITH u AS (SELECT FROM t UNION SELECT FROM t) "
          "SELECT count(*) FROM u"),
    _case("s19684_union_limit",
          "zero-col UNION with LIMIT on the setop result.",
          T1, "SELECT FROM t UNION SELECT FROM t LIMIT 2"),
    _case("s19684_union_orderby",
          "zero-col UNION + ORDER BY 1 — expected 'other_error' "
          "(no output column to order by) -> parser boundary.",
          T1, "SELECT FROM t UNION SELECT FROM t ORDER BY 1"),
    _case("s19684_union_exists",
          "zero-col UNION inside an EXISTS subquery (subplan, probably "
          "no Gather inside -> boundary).",
          T1,
          "SELECT FROM t WHERE EXISTS "
          "(SELECT FROM t UNION SELECT FROM t)"),
    _case("s19684_zerocol_table",
          "UNION over a genuinely zero-column table (TABLE z).",
          TZ, "TABLE z UNION TABLE z"),
    _case("s19684_zerocol_table_sel",
          "SELECT FROM z UNION SELECT FROM z (zero-col table).",
          TZ, "SELECT FROM z UNION SELECT FROM z"),
    _case("s19684_gen_series",
          "zero-col over generate_series vs table.",
          T1,
          "SELECT FROM t UNION SELECT FROM generate_series(1,3)"),
    _case("s19684_two_tables",
          "zero-col UNION across two different tables.",
          T2, "SELECT FROM t UNION SELECT FROM t2"),
    _case("s19684_partitioned",
          "zero-col UNION over a 2-partition append (parallel append "
          "path under Gather).",
          TPART, "SELECT FROM p UNION SELECT FROM p"),
    _case("s19684_partitioned_1col_control",
          "same partitioned UNION but one real column -> sort key "
          "exists -> expected clean.",
          TPART, "SELECT i FROM p UNION SELECT i FROM p"),
    _case("s19684_agg_1col_control",
          "single-col aggregate arms: dedup sort has a real key -> "
          "expected clean.",
          T1,
          "SELECT count(*) FROM t UNION SELECT count(*) FROM t"),
    _case("s19684_groupby_empty",
          "GROUP BY () — aggregation, not setop dedup -> expected clean.",
          T1, "SELECT FROM t GROUP BY ()"),
    _case("s19684_recursive_cte",
          "zero-col UNION inside a WITH RECURSIVE wrapper — reaches the "
          "same Unique->Sort->Gather->ParallelAppend shape through the "
          "recursive-union surface.",
          T1,
          "WITH RECURSIVE r AS (SELECT FROM t UNION SELECT FROM t) "
          "SELECT FROM r"),
    _case("s19684_debug_parallel",
          "zero-col UNION under debug_parallel_query=regress instead "
          "of cost GUCs (master/18.x only GUC).",
          T1, "SELECT FROM t UNION SELECT FROM t",
          extra_pre=["SET debug_parallel_query = regress"]),
]
