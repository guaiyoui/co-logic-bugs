"""DML snapshot oracle for PostgreSQL — same semantics as dml_hunt.py.

Each case runs its DML once on a fresh schema under each plan/GUC config,
then the final table state bags are compared. Replay is never used:
mutating statements are not idempotent.

PG-specific surface on top of the generic DML shapes:
- MERGE (PG 15+) — new execution machinery, historically buggy
- INSERT .. ON CONFLICT (arbiter index inference, dup keys, partitions)
- UPDATE .. FROM with duplicate join keys
- partitioned-table DML (tuple routing)
- RETURNING clause
- generated columns + DML

    python scripts/pg_dml_hunt.py --out results/pg_dml_1
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

from oracles.normalize import loose_bag  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_dml_hunt")

VARIANTS = [
    ("default", []),
    ("no_hashjoin", ["SET enable_hashjoin = off"]),
    ("no_mergejoin", ["SET enable_mergejoin = off"]),
    ("no_nestloop", ["SET enable_nestloop = off",
                     "SET enable_hashjoin = off"]),
    ("no_parallel", ["SET max_parallel_workers_per_gather = 0"]),
    ("jit_force", ["SET jit = on", "SET jit_above_cost = 0"]),
    ("partwise", ["SET enable_partitionwise_join = on",
                  "SET enable_partitionwise_aggregate = on"]),
    ("tiny_workmem", ["SET work_mem = '64kB'"]),
]

# (tag, setup, dml, probe)
DML_CASES: list[tuple[str, list[str], str, str]] = [
    ("merge_basic",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE s(a INT, w INT)",
      "INSERT INTO s VALUES (1,100),(4,400)"],
     "MERGE INTO t USING s ON t.a = s.a "
     "WHEN MATCHED THEN UPDATE SET b = s.w "
     "WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.w)",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_delete",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE s(a INT)",
      "INSERT INTO s VALUES (2),(3)"],
     "MERGE INTO t USING s ON t.a = s.a "
     "WHEN MATCHED THEN DELETE",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_subq",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)",
      "CREATE TABLE s(a INT, w INT)",
      "INSERT INTO s VALUES (1,5),(1,7),(2,9)"],
     "MERGE INTO t USING (SELECT a, sum(w) w FROM s GROUP BY a) s "
     "ON t.a = s.a WHEN MATCHED THEN UPDATE SET b = b + s.w",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_condition",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE s(a INT, w INT)",
      "INSERT INTO s VALUES (1,100),(2,200),(3,300)"],
     "MERGE INTO t USING s ON t.a = s.a "
     "WHEN MATCHED AND t.b < 25 THEN UPDATE SET b = s.w "
     "WHEN MATCHED THEN DELETE",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_partition",
     ["CREATE TABLE t(a INT, b INT) PARTITION BY RANGE (a)",
      "CREATE TABLE t_lo PARTITION OF t FOR VALUES FROM (0) TO (50)",
      "CREATE TABLE t_hi PARTITION OF t FOR VALUES FROM (50) TO (100)",
      "INSERT INTO t VALUES (10,1),(60,2)",
      "CREATE TABLE s(a INT, w INT)",
      "INSERT INTO s VALUES (10,100),(70,300)"],
     "MERGE INTO t USING s ON t.a = s.a "
     "WHEN MATCHED THEN UPDATE SET b = s.w "
     "WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.w)",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_do_nothing",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10)",
      "CREATE TABLE s(a INT, w INT)",
      "INSERT INTO s VALUES (1,99),(5,50)"],
     "MERGE INTO t USING s ON t.a = s.a "
     "WHEN MATCHED THEN DO NOTHING "
     "WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.w)",
     "SELECT a, b FROM t ORDER BY a"),
    ("merge_cte",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "WITH s AS (SELECT 1 a, 99 w UNION ALL SELECT 7, 77) "
     "MERGE INTO t USING s ON t.a = s.a "
     "WHEN MATCHED THEN UPDATE SET b = s.w "
     "WHEN NOT MATCHED THEN INSERT VALUES (s.a, s.w)",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_from",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,100),(3,300)"],
     "UPDATE t SET b = u.w FROM u WHERE t.a = u.a",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_from_dupkey",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)",
      "CREATE TABLE u(a INT, w INT)",
      "INSERT INTO u VALUES (1,100),(1,200),(2,300)"],
     "UPDATE t SET b = u.w FROM u WHERE t.a = u.a",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_corr",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,100),(2,200)"],
     "UPDATE t SET b = (SELECT MAX(u.w) FROM u WHERE u.a = t.a) WHERE a <= 2",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_case",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)"],
     "UPDATE t SET b = CASE WHEN a=1 THEN b+1 WHEN a=2 THEN b*10 ELSE b-1 END",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_selfjoin",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20),(3,30)"],
     "UPDATE t x SET b = y.b + 1 FROM t y WHERE x.a = y.a + 1",
     "SELECT a, b FROM t ORDER BY a"),
    ("delete_using",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2),(3),(4)",
      "CREATE TABLE u(a INT)", "INSERT INTO u VALUES (2),(4)"],
     "DELETE FROM t USING u WHERE t.a = u.a",
     "SELECT a FROM t ORDER BY a"),
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
    ("delete_window",
     ["CREATE TABLE t(a INT, v INT)",
      "INSERT INTO t VALUES (1,5),(1,9),(2,3),(2,7)"],
     "DELETE FROM t WHERE (a, v) IN (SELECT a, v FROM (SELECT a, v, "
     "ROW_NUMBER() OVER (PARTITION BY a ORDER BY v) rn FROM t) s WHERE rn=1)",
     "SELECT a, v FROM t ORDER BY a, v"),
    ("insert_select",
     ["CREATE TABLE t(g INT, v INT)",
      "INSERT INTO t VALUES (1,10),(1,20),(2,5)",
      "CREATE TABLE r(g INT, s INT)"],
     "INSERT INTO r SELECT g, SUM(v) FROM t GROUP BY g",
     "SELECT g, s FROM r ORDER BY g"),
    ("insert_select_union",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2)",
      "CREATE TABLE r(a INT)"],
     "INSERT INTO r SELECT a FROM t UNION ALL SELECT a+10 FROM t",
     "SELECT a FROM r ORDER BY a"),
    ("insert_conflict_nothing",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "INSERT INTO t VALUES (2,999),(3,30) ON CONFLICT DO NOTHING",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_conflict_update",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "INSERT INTO t VALUES (2,999),(3,30) ON CONFLICT (a) "
     "DO UPDATE SET b = excluded.b",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_conflict_where",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10),(2,20)"],
     "INSERT INTO t VALUES (1,99),(2,88) ON CONFLICT (a) "
     "DO UPDATE SET b = excluded.b WHERE t.b < 15",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_conflict_dup_batch",
     ["CREATE TABLE t(a INT PRIMARY KEY, b INT)",
      "INSERT INTO t VALUES (1,10)"],
     "INSERT INTO t VALUES (2,20),(2,22),(3,30) ON CONFLICT DO NOTHING",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_gencol",
     ["CREATE TABLE t(a INT, b INT GENERATED ALWAYS AS (a*2) STORED)",
      "INSERT INTO t(a) VALUES (1),(2)"],
     "INSERT INTO t(a) SELECT a+10 FROM t",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_partition",
     ["CREATE TABLE t(a INT, b INT) PARTITION BY RANGE (a)",
      "CREATE TABLE t_lo PARTITION OF t FOR VALUES FROM (0) TO (50)",
      "CREATE TABLE t_hi PARTITION OF t FOR VALUES FROM (50) TO (100)"],
     "INSERT INTO t VALUES (10,1),(60,2),(30,3),(80,4)",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_partition_move",
     ["CREATE TABLE t(a INT, b INT) PARTITION BY RANGE (a)",
      "CREATE TABLE t_lo PARTITION OF t FOR VALUES FROM (0) TO (50)",
      "CREATE TABLE t_hi PARTITION OF t FOR VALUES FROM (50) TO (100)",
      "INSERT INTO t VALUES (10,1),(60,2)"],
     "UPDATE t SET a = a + 50",
     "SELECT a, b FROM t ORDER BY a"),
    ("delete_partition",
     ["CREATE TABLE t(a INT, b INT) PARTITION BY RANGE (a)",
      "CREATE TABLE t_lo PARTITION OF t FOR VALUES FROM (0) TO (50)",
      "CREATE TABLE t_hi PARTITION OF t FOR VALUES FROM (50) TO (100)",
      "INSERT INTO t VALUES (10,1),(60,2),(70,3)"],
     "DELETE FROM t WHERE a >= 60",
     "SELECT a, b FROM t ORDER BY a"),
    ("update_null_join",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,10),(NULL,99),(3,30)",
      "CREATE TABLE u(a INT, w INT)", "INSERT INTO u VALUES (1,1),(NULL,2)"],
     "UPDATE t SET b = u.w FROM u WHERE t.a = u.a",
     "SELECT a, b FROM t ORDER BY a NULLS LAST"),
    ("insert_default",
     ["CREATE TABLE t(a INT DEFAULT 7, b INT)",
      "INSERT INTO t(b) VALUES (1),(2)"],
     "INSERT INTO t(b) SELECT b*10 FROM t",
     "SELECT a, b FROM t ORDER BY b"),
    ("truncate_fk",
     ["CREATE TABLE p(a INT PRIMARY KEY)",
      "INSERT INTO p VALUES (1),(2)",
      "CREATE TABLE c(a INT REFERENCES p(a))",
      "INSERT INTO c VALUES (1)"],
     "DELETE FROM p WHERE a = 2",
     "SELECT a FROM p ORDER BY a"),
    ("update_agg_subq",
     ["CREATE TABLE t(a INT, b INT)",
      "INSERT INTO t VALUES (1,0),(2,0)"],
     "UPDATE t SET b = (SELECT count(*) FROM t t2 WHERE t2.a <= t.a)",
     "SELECT a, b FROM t ORDER BY a"),
    ("insert_select_lateral",
     ["CREATE TABLE t(a INT)",
      "INSERT INTO t VALUES (1),(2)",
      "CREATE TABLE r(a INT, x INT)"],
     "INSERT INTO r SELECT t.a, s.x FROM t, LATERAL (SELECT t.a*10 x) s",
     "SELECT a, x FROM r ORDER BY a"),
]


def snapshot(pg, tables):
    state = {}
    for t in tables:
        res = pg.run(f"SELECT * FROM {t}", timeout_s=10.0)
        state[t] = loose_bag(res.rows) if res.ok else res.error
    return state


def run_config(pg, setup, dml, probe, tables, prelude):
    pg.setup(["DROP SCHEMA public CASCADE", "CREATE SCHEMA public"])
    conn = pg._conn or pg.connect()
    cur = conn.cursor()
    try:
        for stmt in prelude:
            cur.execute(stmt)
    except Exception:
        pass
    errs = pg.setup(setup)
    if any(e for _, e in errs):
        return None, "setup_error"
    d = pg.run(dml, timeout_s=15.0)
    if not d.ok:
        return None, "dml_error"
    st = snapshot(pg, tables)
    p = pg.run(probe, timeout_s=15.0)
    return (st, loose_bag(p.rows) if p.ok else p.error), None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_dml")
    ap.add_argument("--out", default="results/pg_dml_1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    pg = PostgresRunner(args.pg_datadir)
    stats = {"cases": 0, "setup_ok": 0, "dml_ok": 0, "diffs": 0}
    diffs = []
    t0 = time.time()
    try:
        for tag, setup, dml, probe in DML_CASES:
            stats["cases"] += 1
            tables = re.findall(
                r"CREATE\s+TABLE\s+(\w+)", " ".join(setup), re.IGNORECASE)
            with eff.phase("dml_check"):
                base, err = run_config(pg, setup, dml, probe, tables, [])
                if err:
                    LOGGER.info("skip %s: %s", tag, err)
                    continue
                stats["setup_ok"] += 1
                stats["dml_ok"] += 1
                eff.count("queries_executed")
                for vname, prelude in VARIANTS[1:]:
                    alt, aerr = run_config(
                        pg, setup, dml, probe, tables, prelude)
                    if aerr:
                        continue
                    if alt != base:
                        stats["diffs"] += 1
                        diffs.append({
                            "tag": tag, "variant": vname, "setup": setup,
                            "dml": dml, "probe": probe,
                            "default": repr(base)[:600],
                            "variant": repr(alt)[:600],
                        })
                        LOGGER.info("DIFF %s under %s", tag, vname)
    finally:
        pg.cleanup()
    with open(os.path.join(args.out, "diffs.jsonl"), "w") as fh:
        for d in diffs:
            fh.write(json.dumps(d, default=str) + "\n")
    summary = {**stats, "efficiency": eff.snapshot(),
               "elapsed_s": time.time() - t0}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
