"""Focused differential probe: TiDB vs MySQL semantics + TiDB plan-variant pairs.

Case kinds:
  semantic — same setup+query on TiDB and on MySQL; compare loose row bags
             and ok/error-class. TiDB self-declares MySQL 8.0 compat, so a
             divergence on shared syntax is a candidate bug.
  planvar  — same setup+query on TiDB under two session-var settings
             (``vars_a`` vs ``vars_b``); the result bag must be identical
             (internal oracle — toggles may change the plan, never the result).

Usage: python3 scripts/tidb_probe.py [--verify] [--only ID[,ID...]]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracles.normalize import loose_bag  # noqa: E402
from targets.tidb_runner import MySQLRunner, TiDBRunner  # noqa: E402

TIDB_DSN = "mysql://root:@127.0.0.1:4000"
MYSQL_DSN = "mysql://root:@127.0.0.1:3307"

# Shared schema used by many cases.
SETUP_T = [
    "CREATE TABLE t(a INT, b VARCHAR(20), v DOUBLE)",
    "INSERT INTO t VALUES (1,'x',1.5),(2,'y',2.0),(3,'x',NULL),(NULL,'z',0.5),(NULL,NULL,-1.0)",
    "CREATE TABLE u(k INT, w INT)",
    "INSERT INTO u VALUES (1,10),(2,20),(2,25)",
]

CASES = [
    # ------------------------------------------------ numeric / arithmetic
    {"id": "int_div", "setup": [], "query": "SELECT 7/2, 7 DIV 2, -7 DIV 2, 7.0 DIV 2"},
    {"id": "div_precision", "setup": [], "query": "SELECT 1/3, 2/3, CAST(1 AS DECIMAL(10,4))/3"},
    {"id": "dec_round", "setup": [], "query": "SELECT CAST(1.235 AS DECIMAL(10,2)), CAST(-1.235 AS DECIMAL(10,2)), ROUND(2.5), ROUND(3.5), ROUND(-2.5)"},
    {"id": "unsigned_wrap", "setup": [], "query": "SELECT CAST(-1 AS UNSIGNED), CAST(0 AS UNSIGNED)-1"},
    {"id": "unsigned_sub", "setup": [], "query": "SELECT CAST(1 AS UNSIGNED) - CAST(2 AS UNSIGNED)"},
    {"id": "int_overflow", "setup": [], "query": "SELECT 9223372036854775807 + 1"},
    {"id": "div_zero", "setup": [], "query": "SELECT 1/0, 1 DIV 0, 1 MOD 0, MOD(1,0)"},
    {"id": "abs_minint", "setup": [], "query": "SELECT ABS(-9223372036854775808)"},
    {"id": "pow_over", "setup": [], "query": "SELECT POW(10,400), SQRT(-1), LOG(-1), LOG(0)"},
    {"id": "mod_neg", "setup": [], "query": "SELECT -7 MOD 3, 7 MOD -3, -7 MOD -3"},
    {"id": "ceil_floor", "setup": [], "query": "SELECT CEIL(-1.5), FLOOR(-1.5), TRUNCATE(1.999,1), TRUNCATE(-1.999,1)"},
    {"id": "bit_ops", "setup": [], "query": "SELECT 1 & 3, 1 | 2, ~0, 1 << 64, 5 >> 1, 1 ^ 3"},
    {"id": "float_repr", "setup": [], "query": "SELECT 0.1+0.2, 1.0/3.0*3.0"},
    # ------------------------------------------------ string <-> number coercion
    {"id": "str_num_cmp", "setup": [], "query": "SELECT '1a' = 1, 'abc' = 0, '' = 0, '1e1' = 10, ' 1' = 1"},
    {"id": "str_arith", "setup": [], "query": "SELECT 'a'+1, 'a'+'b', '10abc'+0"},
    {"id": "cast_signed", "setup": [], "query": "SELECT CAST('12a' AS SIGNED), CAST('-x' AS SIGNED), CAST(123 AS CHAR)"},
    {"id": "hex_ops", "setup": [], "query": "SELECT 0x10, X'41', HEX(255), HEX('ab')"},
    {"id": "bool_arith", "setup": [], "query": "SELECT TRUE+TRUE, FALSE-1, TRUE=TRUE, 2=TRUE"},
    {"id": "xor_op", "setup": [], "query": "SELECT TRUE XOR TRUE, 1 XOR 0, NULL XOR 1, 0 XOR 0"},
    # ------------------------------------------------ NULL semantics
    {"id": "null_eq", "setup": [], "query": "SELECT NULL = NULL, NULL <=> NULL, NULL != NULL, NULL IS NULL, NOT (NULL <=> NULL)"},
    {"id": "in_null", "setup": [], "query": "SELECT 1 IN (1,NULL), 3 IN (1,NULL), 3 NOT IN (1,2), 3 NOT IN (1,NULL)"},
    {"id": "not_in_null", "setup": SETUP_T, "query": "SELECT a FROM t WHERE a NOT IN (2, NULL)"},
    {"id": "greatest_null", "setup": [], "query": "SELECT GREATEST(1,NULL), LEAST(1,NULL,3), COALESCE(NULL,NULL,5)"},
    {"id": "greatest_mixed", "setup": [], "query": "SELECT GREATEST('a',1), LEAST(2,'10'), GREATEST(2,'10')"},
    {"id": "concat_null", "setup": [], "query": "SELECT CONCAT('a',NULL), CONCAT_WS(',', 'a', NULL, 'b'), NULLIF(1,1), NULLIF('a','b')"},
    {"id": "case_mixed", "setup": [], "query": "SELECT CASE WHEN 0 THEN 'x' WHEN 1 THEN 2.5 ELSE 'y' END, CASE NULL WHEN NULL THEN 'a' ELSE 'b' END"},
    {"id": "empty_agg", "setup": SETUP_T, "query": "SELECT SUM(v), COUNT(v), AVG(v), MIN(v), MAX(v) FROM t WHERE a = 999"},
    {"id": "count_distinct", "setup": SETUP_T, "query": "SELECT COUNT(DISTINCT b), COUNT(DISTINCT a,b), COUNT(DISTINCT NULL)"},
    # ------------------------------------------------ dates / intervals
    {"id": "month_arith", "setup": [], "query": "SELECT DATE '2024-01-31' + INTERVAL 1 MONTH, DATE '2024-01-31' - INTERVAL 1 MONTH, DATE '2024-03-31' + INTERVAL -1 MONTH"},
    {"id": "year_edge", "setup": [], "query": "SELECT YEAR('2024-02-29'), LAST_DAY('2024-02-01'), LAST_DAY('2023-02-01'), LAST_DAY('2024-01-31' + INTERVAL 1 MONTH)"},
    {"id": "datediff", "setup": [], "query": "SELECT DATEDIFF('2024-01-01','2024-01-05'), TIMESTAMPDIFF(DAY,'2024-01-05','2024-01-01'), TIMESTAMPDIFF(MONTH,'2024-01-31','2024-02-29')"},
    {"id": "zero_date", "setup": [], "vars": {"sql_mode": "''"}, "query": "SELECT CAST('0000-00-00' AS DATE), DATE('0000-00-00'), CAST('2024-02-30' AS DATE)"},
    {"id": "zero_date_strict", "setup": [], "query": "SELECT CAST('0000-00-00' AS DATE), CAST('2024-02-30' AS DATE)"},
    {"id": "time_edge", "setup": [], "query": "SELECT TIME '838:59:59' + INTERVAL 1 HOUR, CAST('25:00:00' AS TIME), TIME('-10:30:00')"},
    {"id": "weekday_extract", "setup": [], "query": "SELECT EXTRACT(YEAR_MONTH FROM '2024-02-29'), WEEKDAY('2024-01-01'), WEEK('2024-01-01'), YEARWEEK('2024-01-01')"},
    {"id": "unix_ts", "setup": [], "query": "SELECT UNIX_TIMESTAMP('1970-01-01 00:00:00'), FROM_UNIXTIME(0)"},
    # ------------------------------------------------ collation / LIKE / regexp
    {"id": "str_eq_ci", "setup": [], "query": "SELECT 'a' = 'A', 'a' LIKE 'A', 'abc' = 'ABC '"},
    {"id": "like_esc", "setup": [], "query": "SELECT 'a_b' LIKE 'a\\_b', 'aXb' LIKE 'a\\_b' ESCAPE '\\\\', '50%' LIKE '50\\%'"},
    {"id": "regexp", "setup": [], "query": "SELECT 'abc' REGEXP '^b', 'a1' REGEXP '[0-9]', 'ABC' REGEXP '^a', REGEXP_LIKE('abc','B')"},
    {"id": "min_max_str", "setup": SETUP_T, "query": "SELECT MIN(b), MAX(b) FROM t"},
    {"id": "collation_bin", "setup": [], "query": "SELECT 'a' = 'A' COLLATE utf8mb4_bin, 'a' < 'B' COLLATE utf8mb4_bin"},
    # ------------------------------------------------ string functions
    {"id": "substring", "setup": [], "query": "SELECT SUBSTRING('hello',-2), SUBSTRING('hello',2,100), LEFT('hi',5), RIGHT('hello',10), MID('hello',0,2)"},
    {"id": "find_in_set", "setup": [], "query": "SELECT FIND_IN_SET('b','a,b,c'), FIELD('x','a','x','b'), INSTR('hello','llo'), LOCATE('x','abc')"},
    {"id": "repeat_pad", "setup": [], "query": "SELECT REPEAT('ab',3), LPAD('x',5,'-'), RPAD('x',4,'ab'), REPEAT('a',0), LPAD('xy',1,'z')"},
    {"id": "trim_fns", "setup": [], "query": "SELECT TRIM('  x  '), LTRIM('  x'), REVERSE('abc'), UPPER('aBc'), CHAR_LENGTH('héllo')"},
    {"id": "space_fill", "setup": [], "query": "SELECT LENGTH('a '), CHAR_LENGTH('a '), 'a' = 'a ', LENGTH(TRIM('a '))"},
    # ------------------------------------------------ joins / subqueries / grouping
    {"id": "left_join", "setup": SETUP_T, "query": "SELECT t.a, u.w FROM t LEFT JOIN u ON t.a = u.k ORDER BY t.a, u.w"},
    {"id": "right_join", "setup": SETUP_T, "query": "SELECT t.a, u.k, u.w FROM t RIGHT JOIN u ON t.a = u.k ORDER BY u.k, u.w"},
    {"id": "join_using", "setup": [
        "CREATE TABLE a(x INT, y INT)", "INSERT INTO a VALUES (1,10),(2,20)",
        "CREATE TABLE b(x INT, z INT)", "INSERT INTO b VALUES (2,200),(3,300)"],
     "query": "SELECT * FROM a LEFT JOIN b USING(x) ORDER BY x"},
    {"id": "right_using", "setup": [
        "CREATE TABLE a(x INT, y INT)", "INSERT INTO a VALUES (1,10),(2,20)",
        "CREATE TABLE b(x INT, z INT)", "INSERT INTO b VALUES (2,200),(3,300)"],
     "query": "SELECT x, y, z FROM a RIGHT JOIN b USING(x) ORDER BY x"},
    {"id": "natural_join", "setup": [
        "CREATE TABLE a(x INT, y INT)", "INSERT INTO a VALUES (1,10),(2,20)",
        "CREATE TABLE b(x INT, z INT)", "INSERT INTO b VALUES (2,200),(3,300)"],
     "query": "SELECT * FROM a NATURAL JOIN b"},
    {"id": "corr_subq", "setup": SETUP_T, "query": "SELECT a, (SELECT MAX(w) FROM u WHERE u.k = t.a) FROM t ORDER BY a"},
    {"id": "exists_subq", "setup": SETUP_T, "query": "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.a AND u.w > 15) ORDER BY a"},
    {"id": "scalar_subq_null", "setup": SETUP_T, "query": "SELECT (SELECT w FROM u WHERE k = 999), (SELECT w FROM u WHERE k = 2)"},
    {"id": "group_null", "setup": SETUP_T, "query": "SELECT a, COUNT(*), COUNT(a) FROM t GROUP BY a ORDER BY a"},
    {"id": "having", "setup": SETUP_T, "query": "SELECT b, SUM(v) FROM t GROUP BY b HAVING SUM(v) > 0 OR SUM(v) IS NULL"},
    {"id": "rollup", "setup": SETUP_T, "query": "SELECT a, SUM(v) FROM t GROUP BY a WITH ROLLUP"},
    {"id": "union_types", "setup": [], "query": "SELECT 1 AS c UNION ALL SELECT 'a' UNION ALL SELECT 2.5"},
    {"id": "limit_ties", "setup": [
        "CREATE TABLE s(a INT, b INT)",
        "INSERT INTO s VALUES (1,5),(2,5),(3,5),(4,1)"],
     "query": "SELECT a FROM s ORDER BY b, a LIMIT 2"},
    {"id": "window_rn", "setup": SETUP_T, "query": "SELECT a, ROW_NUMBER() OVER (ORDER BY a), SUM(v) OVER () FROM t ORDER BY a"},
    {"id": "window_part", "setup": SETUP_T, "query": "SELECT b, SUM(v) OVER (PARTITION BY b) FROM t ORDER BY b"},
    {"id": "json_fns", "setup": [], "query": "SELECT JSON_EXTRACT('{\"a\":1}','$.a'), JSON_OBJECT('k',1), JSON_ARRAY(1,'x',NULL)"},
    {"id": "json_cmp", "setup": [], "query": "SELECT CAST('{\"a\":1}' AS JSON), JSON_TYPE('\"x\"'), JSON_VALID('{bad')"},
]

# ------------------------------------------- TiDB-internal plan-variant cases
PLANVAR_CASES = [
    {"id": "pv_vectorized", "setup": SETUP_T,
     "vars_a": {"tidb_enable_vectorized_expression": "ON"},
     "vars_b": {"tidb_enable_vectorized_expression": "OFF"},
     "query": "SELECT a+1, ABS(v), CONCAT(b,'!'), a*2-1 FROM t WHERE COALESCE(a,0) >= 0 ORDER BY a"},
    {"id": "pv_distinct_push", "setup": SETUP_T,
     "vars_a": {"tidb_opt_distinct_agg_push_down": "ON"},
     "vars_b": {"tidb_opt_distinct_agg_push_down": "OFF"},
     "query": "SELECT COUNT(DISTINCT b), COUNT(DISTINCT a) FROM t GROUP BY b"},
    {"id": "pv_insubq", "setup": SETUP_T,
     "vars_a": {"tidb_opt_insubq_to_join_and_agg": "ON"},
     "vars_b": {"tidb_opt_insubq_to_join_and_agg": "OFF"},
     "query": "SELECT a, v FROM t WHERE a IN (SELECT k FROM u WHERE w >= 20) ORDER BY a"},
    {"id": "pv_agg_push", "setup": SETUP_T,
     "vars_a": {"tidb_opt_agg_push_down": "ON"},
     "vars_b": {"tidb_opt_agg_push_down": "OFF"},
     "query": "SELECT a, SUM(v), COUNT(*), AVG(v) FROM t GROUP BY a ORDER BY a"},
    {"id": "pv_index_merge", "setup": [
        "CREATE TABLE m(a INT, b INT, INDEX ia(a), INDEX ib(b))",
        "INSERT INTO m VALUES (1,1),(2,2),(3,3),(NULL,1),(1,NULL)"],
     "vars_a": {"tidb_enable_index_merge": "ON"},
     "vars_b": {"tidb_enable_index_merge": "OFF"},
     "query": "SELECT a, b FROM m WHERE a = 1 OR b = 2 ORDER BY a, b"},
    {"id": "pv_npc", "setup": SETUP_T,
     "vars_a": {"tidb_enable_non_prepared_plan_cache": "ON"},
     "vars_b": {"tidb_enable_non_prepared_plan_cache": "OFF"},
     "query": "SELECT * FROM t WHERE a = 1 AND b = 'x'"},
    {"id": "pv_proj_push", "setup": SETUP_T,
     "vars_a": {"tidb_opt_projection_push_down": "ON"},
     "vars_b": {"tidb_opt_projection_push_down": "OFF"},
     "query": "SELECT a, UPPER(b) FROM t WHERE a IS NOT NULL ORDER BY a"},
    {"id": "pv_oj_reorder", "setup": [
        "CREATE TABLE j1(a INT)", "INSERT INTO j1 VALUES (1),(2),(NULL)",
        "CREATE TABLE j2(a INT)", "INSERT INTO j2 VALUES (1),(3)",
        "CREATE TABLE j3(a INT)", "INSERT INTO j3 VALUES (1),(2)"],
     "vars_a": {"tidb_enable_outer_join_reorder": "ON"},
     "vars_b": {"tidb_enable_outer_join_reorder": "OFF"},
     "query": "SELECT j1.a, j2.a, j3.a FROM j1 LEFT JOIN j2 ON j1.a = j2.a LEFT JOIN j3 ON j1.a = j3.a"},
]


def err_class(error: str | None) -> str | None:
    if error is None:
        return None
    # mysql-connector errors look like "ProgrammingError: 1064 (42000): msg"
    import re
    m = re.search(r"(\d{4})\s*\(", error)
    if m:
        return m.group(1)
    return error.split(":")[0][:60]


def outcome(runner, setup, query, vars_map=None):
    """Fresh-schema run of one query; returns (ok, errclass, bag)."""
    runner.setup(setup)
    if vars_map:
        for k, v in vars_map.items():
            r = runner.run(f"SET SESSION {k} = {v}")
            if not r.ok:
                return False, f"SETFAIL:{k}:{r.error}", None
    res = runner.run(query)
    if res.ok:
        return True, None, loose_bag(res.rows)
    return False, err_class(res.error), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", type=int, default=0,
                    help="re-run every divergence N times on fresh schemas")
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default="results/tidb_probe_report.jsonl")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else None
    tidb = TiDBRunner(TIDB_DSN, database="coevotest")
    mysql = MySQLRunner(MYSQL_DSN, database="coevotest")
    print("tidb:", tidb.engine_version, "| mysql:", mysql.engine_version)

    report = []
    divergences = []

    for case in CASES:
        if only and case["id"] not in only:
            continue
        vars_map = case.get("vars")
        t_ok, t_err, t_bag = outcome(tidb, case["setup"], case["query"], vars_map)
        m_ok, m_err, m_bag = outcome(mysql, case["setup"], case["query"], vars_map)
        div = (t_ok != m_ok) or (t_ok and t_bag != m_bag) or \
              (not t_ok and t_err != m_err)
        rec = {"id": case["id"], "kind": "semantic", "div": div,
               "tidb": {"ok": t_ok, "err": t_err,
                        "bag": dict(t_bag) if t_bag is not None else None},
               "mysql": {"ok": m_ok, "err": m_err,
                         "bag": dict(m_bag) if m_bag is not None else None}}
        report.append(rec)
        tag = "DIV" if div else "ok "
        print(f"[{tag}] {case['id']:22s} tidb_ok={t_ok} mysql_ok={m_ok}")
        if div:
            divergences.append(case["id"])

    for case in PLANVAR_CASES:
        if only and case["id"] not in only:
            continue
        a_ok, a_err, a_bag = outcome(tidb, case["setup"], case["query"], case["vars_a"])
        b_ok, b_err, b_bag = outcome(tidb, case["setup"], case["query"], case["vars_b"])
        div = (a_ok != b_ok) or (a_ok and a_bag != b_bag) or \
              (not a_ok and a_err != b_err)
        rec = {"id": case["id"], "kind": "planvar", "div": div,
               "vars_a": case["vars_a"], "vars_b": case["vars_b"],
               "a": {"ok": a_ok, "err": a_err,
                     "bag": dict(a_bag) if a_bag is not None else None},
               "b": {"ok": b_ok, "err": b_err,
                     "bag": dict(b_bag) if b_bag is not None else None}}
        report.append(rec)
        tag = "DIV" if div else "ok "
        print(f"[{tag}] {case['id']:22s} a_ok={a_ok} b_ok={b_ok}")
        if div:
            divergences.append(case["id"])

    # ------------------------------------------------------- determinism check
    if args.verify and divergences:
        print("\n=== verification (fresh schema each run) ===")
        for cid in divergences:
            case = next(c for c in CASES + PLANVAR_CASES if c["id"] == cid)
            stable = True
            for i in range(args.verify):
                if case["id"].startswith("pv_"):
                    a = outcome(tidb, case["setup"], case["query"], case["vars_a"])
                    b = outcome(tidb, case["setup"], case["query"], case["vars_b"])
                    same = (a == b)
                    print(f"  {cid} run{i}: a={a[:2]} b={b[:2]} same={same}")
                    stable &= not same
                else:
                    t = outcome(tidb, case["setup"], case["query"], case.get("vars"))
                    m = outcome(mysql, case["setup"], case["query"], case.get("vars"))
                    same = (t == m)
                    print(f"  {cid} run{i}: tidb={t[:2]} mysql={m[:2]} same={same}")
                    stable &= not same
            print(f"  {cid}: {'STABLE divergence' if stable else 'FLAKY — dropped'}")

    out = Path(args.out)
    out.write_text("\n".join(json.dumps(r, default=str) for r in report) + "\n")
    print(f"\nwrote {out}; divergences: {divergences}")

    tidb.cleanup()
    mysql.cleanup()


if __name__ == "__main__":
    main()
