"""Sibling-mutation probes around BUG #19649 (repr-sensitive wrapper qual
pushed below GROUP BY / set-op dedup / window partition / HAVING).

Verbatim: j::text qual pushed below GROUP BY j on jsonb '1'/'1.0' pair ->
one jsonb-equal group is split, wrong count.  These arms vary

  * the repr-sensitive wrapper (j->>0, j->>'x', j #>> '{}', ::varchar,
    concat, upper/left, LIKE, regex, position(), <>, IN, CASE, coalesce,
    volatile, SubPlan boundary),
  * the grouping construct (GROUPING SETS, DISTINCT, UNION dedup, HAVING,
    FILTER control, string_agg/array_agg, computed-tlist, non-representative
    member match, join-above, two-level subquery, ROLLUP control),
  * the repr-sensitive TYPE (numeric 1.0/1.00, interval '1 day'/24:00:00
    via make_interval, float8 0/-0).

Expected rows computed by hand: every "one group of 2" base returns
count 2 (or repr '1' / '1 day' / '0' / '1.0' as noted per arm).
"""

T_J = [
    "CREATE TABLE t(id int primary key, j jsonb)",
    "INSERT INTO t VALUES (1,'1'),(2,'1.0')",
]
T_JARR = [
    "CREATE TABLE t(id int primary key, j jsonb)",
    "INSERT INTO t VALUES (1,'[1]'),(2,'[1.0]')",
]
T_JOBJ = [
    "CREATE TABLE t(id int primary key, j jsonb)",
    "INSERT INTO t VALUES (1,'{\"x\":1}'),(2,'{\"x\":1.0}')",
]
T_NUM = [
    "CREATE TABLE tn(id int primary key, n numeric)",
    "INSERT INTO tn VALUES (1,1.0),(2,1.00)",
]
T_INT = [
    "CREATE TABLE ti(id int primary key, i interval)",
    "INSERT INTO ti VALUES (1,'1 day'::interval),"
    "(2,make_interval(hours=>24))",
]
T_FLT = [
    "CREATE TABLE tf(id int primary key, f float8)",
    # '-0'::float8 literal is required: the numeric literal -0.0 parses
    # through numeric (which has no negative zero) and stores +0.
    "INSERT INTO tf VALUES (1,'0'::float8),(2,'-0'::float8)",
]


def _case(name, source, setup, query, expected, marker,
          buggy="wrong_rows", buggy_error=None):
    d = {
        "name": name,
        "source": source,
        "setup_sqls": setup,
        "pre_sqls": [],
        "query": query,
        "buggy": buggy,
        "buggy_marker": marker,
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    }
    if expected is not None:
        d["expected_rows"] = expected
    if buggy_error is not None:
        d["buggy_error"] = buggy_error
    return d


PROBES = [
    # ------------------------------------------------ verbatim control
    _case("s19649_verbatim",
          "#19649 verbatim control (fires everywhere).",
          T_J,
          "SELECT j, c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j::text = '1'",
          [[1, 2]], "pushed => (1,1)"),

    # ------------------------------------------- wrapper mutations (jsonb)
    _case("s19649_w_arr_elemsub",
          "wrapper j->>0 on jsonb array '[]1]'/'[1.0]' pair.",
          T_JARR,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j->>0 = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_w_obj_fieldsub",
          "wrapper j->>'x' on jsonb object '{\"x\":1}'/'{\"x\":1.0}' pair.",
          T_JOBJ,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j->>'x' = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_w_hash_extract",
          "wrapper j #>> '{}' (whole-doc text extract).",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j #>> '{}' = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_w_varchar",
          "wrapper j::varchar (coerce-via-IO cast).",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j::varchar = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_w_concat_x",
          "wrapper (j::text || 'x') const concat.",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE (j::text || 'x') = '1x'",
          [[2]], "pushed => 1"),
    _case("s19649_w_upper",
          "wrapper upper(j::text) — stable func over repr.",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE upper(j::text) = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_w_left2",
          "wrapper left(j::text,2): '1' vs '1.' discriminates.",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE left(j::text, 2) = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_w_like",
          "qual is a LIKE pattern on the wrapper (operator class like).",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j::text LIKE '1'",
          [[2]], "pushed => 1"),
    _case("s19649_w_regex",
          "qual is a regex match on the wrapper.",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j::text ~ '^1$'",
          [[2]], "pushed => 1"),
    _case("s19649_w_position",
          "qual position('.' in j::text) = 0 — expr-shaped qual.",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE position('.' in j::text) = 0",
          [[2]], "pushed => 1"),
    _case("s19649_w_neq",
          "qual j::text <> '1': correct filters repr '1' out (no rows); "
          "pushed keeps '1.0' -> phantom row.",
          T_J,
          "SELECT j::text, c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j::text <> '1'",
          [], "pushed => ('1.0',1) phantom"),
    _case("s19649_w_inlist",
          "qual j::text IN ('1','9').",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j::text IN ('1','9')",
          [[2]], "pushed => 1"),
    _case("s19649_w_case_when",
          "qual wrapped in CASE WHEN.",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE (CASE WHEN j::text = '1' THEN true ELSE false END)",
          [[2]], "pushed => 1"),
    _case("s19649_w_coalesce",
          "qual coalesce(j::text,'x') = '1'.",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE coalesce(j::text, 'x') = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_w_sublink_boundary",
          "qual j::text = (SELECT '1') — has SubPlan; boundary probe for "
          "pushdown eligibility (probably not pushed -> clean).",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j::text = (SELECT '1')",
          [[2]], "if pushed => 1"),
    _case("s19649_w_volatile_boundary",
          "qual (j::text || substr(random()::text,1,0)) = '1' — volatile "
          "wrapper; volatile quals must not be pushed -> expected clean.",
          T_J,
          "SELECT c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE (j::text || substr(random()::text, 1, 0)) = '1'",
          [[2]], "if pushed => 1"),

    # ------------------------------------- grouping-construct mutations
    _case("s19649_g_grouping_sets",
          "GROUPING SETS ((j),()): only the (j) path can receive the "
          "pushed qual -> sum(c) 2 vs 1.",
          T_J,
          "SELECT sum(c) FROM (SELECT j, count(*) c FROM t "
          "GROUP BY GROUPING SETS ((j),())) s WHERE j::text = '1'",
          [[2]], "partial pushdown => 1"),
    _case("s19649_g_distinct_nonrep",
          "DISTINCT dedup, qual matches NON-representative repr '1.0': "
          "correct = no rows (dedup keeps '1'); pushed = '1.0' survives.",
          T_J,
          "SELECT j::text FROM (SELECT DISTINCT j FROM t) s "
          "WHERE j::text = '1.0'",
          [], "pushed below DISTINCT => '1.0'"),
    _case("s19649_g_union_dedup_nonrep",
          "UNION dedup, qual '1.0': correct = no rows; pushed into both "
          "arms below dedup => '1.0'.",
          T_J,
          "SELECT j::text FROM (SELECT j FROM t UNION SELECT j FROM t) s "
          "WHERE j::text = '1.0'",
          [], "pushed below UNION => '1.0'"),
    _case("s19649_g_having",
          "HAVING j::text = '1' inside the aggregate query itself "
          "(HAVING->WHERE conversion arm).",
          T_J,
          "SELECT count(*) FROM t GROUP BY j HAVING j::text = '1'",
          [[2]], "HAVING qual pushed to WHERE => 1"),
    _case("s19649_g_filter_control",
          "count(*) FILTER (WHERE j::text='1') — per-row FILTER semantics; "
          "expected CLEAN control: (1,2).",
          T_J,
          "SELECT count(*) FILTER (WHERE j::text = '1'), count(*) "
          "FROM t GROUP BY j",
          [[1, 2]], "any other result => anomaly"),
    _case("s19649_g_string_agg",
          "string_agg observes the dropped member row.",
          T_J,
          "SELECT s, c FROM (SELECT j, "
          "string_agg(id::text, ',' ORDER BY id) s, count(*) c "
          "FROM t GROUP BY j) x WHERE j::text = '1'",
          [["1,2", 2]], "pushed => ('1',1)"),
    _case("s19649_g_array_agg",
          "array_agg observes the dropped member row.",
          T_J,
          "SELECT a, c FROM (SELECT j, array_agg(id ORDER BY id) a, "
          "count(*) c FROM t GROUP BY j) x WHERE j::text = '1'",
          [[[1, 2], 2]], "pushed => ([1],1)"),
    _case("s19649_g_computed_tlist",
          "wrapper inside the subquery target list: qual on alias jt "
          "matching the non-representative repr.",
          T_J,
          "SELECT jt, c FROM (SELECT j::text jt, count(*) c "
          "FROM t GROUP BY j) s WHERE jt = '1.0'",
          [], "pushed => ('1.0',1) phantom"),
    _case("s19649_g_nonrep_member",
          "verbatim shape but qual matches the non-representative "
          "member '1.0': correct = empty; pushed = phantom ('1.0',1).",
          T_J,
          "SELECT j::text, c FROM (SELECT j, count(*) c "
          "FROM t GROUP BY j) s WHERE j::text = '1.0'",
          [], "pushed => ('1.0',1) phantom"),
    _case("s19649_g_join_above",
          "qual sits above a join; must travel through join AND below "
          "GROUP BY.",
          T_J,
          "SELECT count(*) FROM (SELECT j, count(*) c "
          "FROM t GROUP BY j) s JOIN (VALUES (1)) v(x) ON true "
          "WHERE j::text = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_g_two_level",
          "qual pushed through two levels of subquery to below GROUP BY.",
          T_J,
          "SELECT c FROM (SELECT * FROM (SELECT j, count(*) c "
          "FROM t GROUP BY j) s1) s WHERE j::text = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_g_view",
          "verbatim shape through a flattened VIEW.",
          T_J + [
              "CREATE VIEW v AS SELECT j, count(*) c FROM t GROUP BY j",
          ],
          "SELECT j, c FROM v WHERE j::text = '1'",
          [[1, 2]], "pushed => (1,1)"),
    _case("s19649_g_rollup_control",
          "ROLLUP(j) — reported clean for verbatim shape; sum(c) "
          "distinguishes: 2 correct / 1 pushed.",
          T_J,
          "SELECT sum(c) FROM (SELECT j, count(*) c "
          "FROM t GROUP BY ROLLUP(j)) s WHERE j::text = '1'",
          [[2]], "pushed => 1"),
    _case("s19649_g_bare_groupkey_control",
          "qual on the bare grouped column j = '1'::jsonb — pushdown is "
          "LEGIT here (jsonb-equality respects the group); expected "
          "clean (1,2).",
          T_J,
          "SELECT j, c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j = '1'::jsonb",
          [[1, 2]], "any other result => anomaly"),

    # --------------------------------------------- repr-sensitive TYPES
    _case("s19649_t_numeric",
          "numeric 1.0 vs 1.00 — equal, different n::text reprs.",
          T_NUM,
          "SELECT c FROM (SELECT n, count(*) c FROM tn GROUP BY n) s "
          "WHERE n::text = '1.0'",
          [[2]], "pushed => 1"),
    _case("s19649_t_interval",
          "interval '1 day' vs make_interval(hours=>24): equal per "
          "interval_eq, different i::text reprs ('1 day'/'24:00:00').",
          T_INT,
          "SELECT c FROM (SELECT i, count(*) c FROM ti GROUP BY i) s "
          "WHERE i::text = '1 day'",
          [[2]], "pushed => 1 (or group-split => 1)"),
    _case("s19649_t_float_neg0",
          "float8 0 vs -0: equal, reprs '0'/'-0'; qual matches the "
          "non-representative member -> correct = 0 rows.",
          T_FLT,
          "SELECT count(*) FROM (SELECT f, count(*) c "
          "FROM tf GROUP BY f) s WHERE f::text = '-0'",
          [[0]], "pushed => 1"),
]
