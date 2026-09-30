#!/usr/bin/env python3
"""PG parallel-vs-serial oracle: same query under parallel plan vs
max_parallel_workers_per_gather=0 / debug_parallel_query=on.

Bugs in partial aggregation, gather merge, parallel append, or
parallel-safe misclassification surface as result diffs.
"""
import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_parallel")

SETUP = [
    "CREATE TABLE big_fact (id int, grp int, val numeric(12,2), ts timestamp)",
    """INSERT INTO big_fact
       SELECT g, g % 977, (g * 1.7) % 10000,
              timestamp '2024-01-01' + (g % 900) * interval '1 day'
       FROM generate_series(1, 1500000) g""",
    "CREATE TABLE big_dim (grp int primary key, label text, cap numeric)",
    """INSERT INTO big_dim
       SELECT g, 'lbl_' || (g % 31), (g % 500) * 3.14
       FROM generate_series(1, 977) g""",
    "CREATE INDEX big_fact_grp ON big_fact(grp)",
    "CREATE INDEX big_fact_ts ON big_fact(ts)",
    "ANALYZE big_fact", "ANALYZE big_dim",
    # skewed second table: hot key + long tail
    "CREATE TABLE skew (k int, v int)",
    """INSERT INTO skew
       SELECT CASE WHEN g % 3 = 0 THEN 1 ELSE g % 997 END, g % 50
       FROM generate_series(1, 800000) g""",
    "CREATE INDEX skew_k ON skew(k)", "ANALYZE skew",
    # partitioned table for parallel append / partition pruning
    """CREATE TABLE part_t (id int, p int, amt numeric)
       PARTITION BY RANGE (p)""",
    "CREATE TABLE part_a PARTITION OF part_t FOR VALUES FROM (0) TO (500)",
    "CREATE TABLE part_b PARTITION OF part_t FOR VALUES FROM (500) TO (1000)",
    "CREATE TABLE part_c PARTITION OF part_t FOR VALUES FROM (1000) TO (2000)",
    """INSERT INTO part_t SELECT g, g % 2000, (g * 0.37) % 500
       FROM generate_series(1, 900000) g""",
    "ANALYZE part_t",
]

QUERIES = [
    # partial aggregation paths
    "SELECT grp, count(*), sum(val), avg(val), min(val), max(val) FROM big_fact GROUP BY grp ORDER BY grp",
    "SELECT grp % 37 g, count(DISTINCT ts) FROM big_fact GROUP BY 1 ORDER BY 1",
    "SELECT count(*) FROM (SELECT DISTINCT grp, val FROM big_fact) s",
    "SELECT grp, string_agg(DISTINCT label, ',') FROM big_fact JOIN big_dim USING (grp) GROUP BY grp ORDER BY grp",
    # hash join vs parallel hash
    "SELECT count(*), sum(f.val) FROM big_fact f JOIN big_dim d ON f.grp=d.grp",
    "SELECT d.label, count(*) FROM big_fact f JOIN big_dim d ON f.grp=d.grp GROUP BY d.label ORDER BY d.label",
    # skewed join: hot key
    "SELECT k, count(*), sum(v) FROM skew GROUP BY k ORDER BY k LIMIT 30",
    "SELECT s.k, count(*) FROM skew s JOIN big_dim d ON s.k=d.grp GROUP BY s.k ORDER BY s.k LIMIT 30",
    # parallel append on partitioned table
    "SELECT p, count(*), sum(amt) FROM part_t GROUP BY p ORDER BY p LIMIT 20",
    "SELECT count(*) FROM part_t WHERE p >= 250 AND p < 1750",
    "SELECT p % 100 q, sum(amt) FROM part_t WHERE p < 1000 GROUP BY 1 ORDER BY 1 LIMIT 20",
    # partitionwise join
    "SELECT count(*) FROM part_t a JOIN part_t b ON a.p=b.p AND a.id=b.id",
    # bitmap heap scan / index-only mix
    "SELECT count(*) FROM big_fact WHERE grp < 500",
    "SELECT grp, count(*) FROM big_fact WHERE grp < 500 GROUP BY grp ORDER BY grp",
    # window over big data
    "SELECT id, sum(val) OVER (PARTITION BY grp ORDER BY ts) FROM big_fact WHERE id % 50000 = 0 ORDER BY id",
    # subquery / semi-join
    "SELECT count(*) FROM big_fact f WHERE EXISTS (SELECT 1 FROM big_dim d WHERE d.grp=f.grp AND d.cap>1000)",
    "SELECT count(*) FROM big_fact WHERE grp IN (SELECT grp FROM big_dim WHERE cap < 100)",
    # UNION ALL + parallel
    "SELECT count(*) FROM (SELECT grp FROM big_fact UNION ALL SELECT grp FROM big_dim) s",
    # LIMIT with parallel sort (gather merge)
    "SELECT id, val FROM big_fact ORDER BY val DESC, id LIMIT 50",
]

PAR_ON = [
    "SET max_parallel_workers_per_gather=4",
    "SET parallel_setup_cost=0",
    "SET parallel_tuple_cost=0",
    "SET min_parallel_table_scan_size=0",
    "SET min_parallel_index_scan_size=0",
    "SET enable_partitionwise_join=on",
    "SET enable_partitionwise_aggregate=on",
]
PAR_OFF = [
    "SET max_parallel_workers_per_gather=0",
    "SET debug_parallel_query=off",
    "SET enable_partitionwise_join=off",
    "SET enable_partitionwise_aggregate=off",
]


def norm(rows):
    from oracles.differential import loose_bag
    return loose_bag(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_par")
    ap.add_argument("--out", default="results/pg_parallel_1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)
    with eff.phase("setup"):
        runner = PostgresRunner(args.pg_datadir)
        runner.setup(SETUP)
        LOGGER.info("setup done")
    def apply(stmts):
        for s in stmts:
            runner.run(s)

    divs = []
    with eff.phase("screen"):
        for qi, q in enumerate(QUERIES):
            apply(PAR_OFF)
            off = runner.run(q)
            apply(PAR_ON)
            par = runner.run(q)
            # debug_parallel_query=on forces a Gather even on tiny plans
            apply(["SET debug_parallel_query=on"])
            dbg = runner.run(q)
            apply(["SET debug_parallel_query=off"])
            eff.count("queries_executed", 3)
            b_off, b_par, b_dbg = norm(off.rows), norm(par.rows), norm(dbg.rows)
            if not (b_off == b_par == b_dbg):
                rec = {"q": q, "off": str(b_off)[:400],
                       "par": str(b_par)[:400], "dbg": str(b_dbg)[:400]}
                divs.append(rec)
                LOGGER.info("DIVERGENT q%d: %s", qi, q[:80])
                with open(os.path.join(args.out, "divergences.jsonl"),
                          "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
            if qi % 5 == 0:
                LOGGER.info("%d/%d divergent=%d", qi, len(QUERIES), len(divs))
    summ = {"queries": len(QUERIES), "divergent": len(divs),
            "efficiency": eff.snapshot()}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summ, fh, indent=1)
    LOGGER.info("done: %d divergences", len(divs))
    runner.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
