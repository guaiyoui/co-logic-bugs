"""Parameterized PREPARE/EXECUTE seeds for the plan_cache_mode oracle.

Corpus queries carry no $n params, so PREPARE over them makes the
force_custom_plan vs force_generic_plan comparison vacuous. These cases
exercise the classes where generic and custom plans historically diverge:
runtime partition pruning (brazeal class), selectivity-driven index/seq
flips, IS [NOT] NULL semantics, ANY/IN expansion, and LIMIT/OFFSET params.
"""

PARAM_SEEDS = [
    {
        "name": "partprune_in_params",
        # brazeal-class: IS NOT NULL + IN() over RANGE DEFAULT partition
        "setup_sqls": [
            "CREATE TABLE s2 (a int) PARTITION BY RANGE (a)",
            "CREATE TABLE s2_1 PARTITION OF s2 FOR VALUES FROM (0) TO (10)",
            "CREATE TABLE s2_d PARTITION OF s2 DEFAULT",
            "INSERT INTO s2 VALUES (5), (15)",
        ],
        "types": ["int", "int"],
        "query": "SELECT count(*) FROM s2 "
                 "WHERE a IS NOT NULL AND a IN ($1, $2)",
        "exec_params": [[5, 15], [5, 5], [15, 15], [3, 7], [None, 5]],
    },
    {
        "name": "partprune_eq_params",
        "setup_sqls": [
            "CREATE TABLE s3 (a int, b int) PARTITION BY RANGE (a)",
            "CREATE TABLE s3_1 PARTITION OF s3 FOR VALUES FROM (0) TO (10)",
            "CREATE TABLE s3_d PARTITION OF s3 DEFAULT",
            "INSERT INTO s3 VALUES (5, 50), (15, 150)",
        ],
        "types": ["int"],
        "query": "SELECT b FROM s3 WHERE a = $1",
        "exec_params": [[5], [15], [7], [None]],
    },
    {
        "name": "like_prefix_flip",
        # $1='abc%' favours index scan; '%abc' cannot use the index
        "setup_sqls": [
            "CREATE TABLE lt (t text)",
            "INSERT INTO lt SELECT 'abc' || g FROM generate_series(0, 999) g",
            "INSERT INTO lt SELECT 'z' || g FROM generate_series(0, 99) g",
            "CREATE INDEX lt_t ON lt(t text_pattern_ops)",
            "ANALYZE lt",
        ],
        "types": ["text"],
        "query": "SELECT count(*) FROM lt WHERE t LIKE $1",
        "exec_params": [["abc%"], ["%abc"], ["abc"], ["%"]],
    },
    {
        "name": "isnotdistinct_param",
        "setup_sqls": [
            "CREATE TABLE nd (a int, b int)",
            "INSERT INTO nd VALUES (1, 10), (NULL, 20), (3, NULL)",
        ],
        "types": ["int"],
        "query": "SELECT b FROM nd WHERE a IS NOT DISTINCT FROM $1",
        "exec_params": [[1], [None], [3]],
    },
    {
        "name": "any_array_param",
        "setup_sqls": [
            "CREATE TABLE arr (a int)",
            "INSERT INTO arr VALUES (1), (2), (3), (4)",
        ],
        "types": ["int[]"],
        "query": "SELECT count(*) FROM arr WHERE a = ANY($1)",
        "exec_params": [["{1,2}"], ["{3,4,5}"], ["{}"], [None]],
    },
    {
        "name": "in_subq_param",
        "setup_sqls": [
            "CREATE TABLE it (a int, b int)",
            "INSERT INTO it VALUES (1, 10), (2, 20), (3, 30)",
        ],
        "types": ["int"],
        "query": "SELECT count(*) FROM it WHERE b IN "
                 "(SELECT b FROM it WHERE a >= $1)",
        "exec_params": [[1], [2], [10]],
    },
    {
        "name": "limit_offset_params",
        "setup_sqls": [
            "CREATE TABLE lo (a int)",
            "INSERT INTO lo SELECT g FROM generate_series(1, 50) g",
        ],
        "types": ["int", "int"],
        "query": "SELECT a FROM lo ORDER BY a LIMIT $1 OFFSET $2",
        "exec_params": [[5, 0], [5, 45], [0, 0], [None, 0]],
    },
    {
        "name": "bool_shortcircuit_param",
        # generic plan cannot fold $1 -> different branch evaluation paths
        "setup_sqls": [
            "CREATE TABLE bs (a int)",
            "INSERT INTO bs VALUES (1), (9)",
        ],
        "types": ["bool"],
        "query": "SELECT count(*) FROM bs WHERE $1 OR a > 5",
        "exec_params": [[True], [False], [None]],
    },
    {
        "name": "scalar_corr_param",
        "setup_sqls": [
            "CREATE TABLE sc (a int, b int)",
            "INSERT INTO sc VALUES (1, 100), (2, 200)",
        ],
        "types": ["int"],
        "query": "SELECT a, (SELECT count(*) FROM sc s2 "
                 "WHERE s2.b > sc.b - $1) FROM sc ORDER BY a",
        "exec_params": [[0], [100], [None]],
    },
    {
        "name": "interval_param",
        "setup_sqls": [
            "CREATE TABLE iv (d timestamptz)",
            "INSERT INTO iv VALUES (now() - interval '1 day'), "
            "(now() - interval '40 days')",
        ],
        "types": ["interval"],
        "query": "SELECT count(*) FROM iv WHERE d > now() - $1",
        "exec_params": [["2 days"], ["60 days"], [None]],
    },
    {
        "name": "jsonb_contains_param",
        "setup_sqls": [
            "CREATE TABLE jb (j jsonb)",
            "INSERT INTO jb VALUES ('{\"a\":1}'), ('{\"a\":2}'), ('{}')",
        ],
        "types": ["jsonb"],
        "query": "SELECT count(*) FROM jb WHERE j @> $1",
        "exec_params": [["{\"a\":1}"], ["{}"], [None]],
    },
    {
        "name": "between_params",
        "setup_sqls": [
            "CREATE TABLE bt (a int)",
            "INSERT INTO bt SELECT g FROM generate_series(1, 100) g",
            "CREATE INDEX bt_a ON bt(a)",
            "ANALYZE bt",
        ],
        "types": ["int", "int"],
        "query": "SELECT count(*) FROM bt WHERE a BETWEEN $1 AND $2",
        "exec_params": [[1, 5], [1, 100], [50, 10], [None, 5]],
    },
]
