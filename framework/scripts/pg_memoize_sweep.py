"""Sibling sweep around the memoize expr-param stale-cache bug
(Brazeal 2026-07, pgsql-hackers; extension of BUG #17213):
Memoize's cache key shares an outer Param between a join qual and a
plain qual -> stale cache across rescan -> wrong aggregate.

Verbatim:  sum = 100000, buggy = 82000 on every build.

Per arm the driver runs the same query twice on the same freshly-seeded
schema: once with enable_memoize=off (correct-value oracle — the query
is deterministic so the memoize-off result is the truth) and once with
enable_memoize=on.  fired iff actual != expected.

Arms vary the stale-param shape: param in ON vs WHERE, both-in-ON,
two shared params, LEFT JOIN, correlated EXISTS, scalar subquery,
param on inner-side expr, hash/merge-join inner (no memoize -> expected
boundary), different outer column, wider outer range.

Usage:
    python scripts/pg_memoize_sweep.py \
        --prefixes $COEVO_PGBLD/pg186_assert,\
$COEVO_PGBLD/pgmaster_assert \
        --out results/sibling_sweeps/sweep_e
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner  # noqa: E402

SETUP = [
    "CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, "
    "g%10 AS ten, g%20 AS twenty, g%100 AS hundred "
    "FROM generate_series(0,9999) g",
    "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
    "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
    "ANALYZE tenk1",
]

BASE = [
    "SET enable_seqscan = off",
    "SET enable_mergejoin = off",
    "SET work_mem = '64kB'",
]
RESETS = [
    "RESET enable_seqscan", "RESET enable_mergejoin", "RESET work_mem",
    "RESET enable_memoize", "RESET enable_nestloop", "RESET enable_hashjoin",
]

J = "tenk1 t2 JOIN tenk1 t1"


def outer(inner: str) -> str:
    return ("SELECT sum(c) FROM (SELECT t0.unique1, (" + inner + ") AS c "
            "FROM tenk1 t0 WHERE t0.unique1 < 200) s")


ARMS = [
    ("m_verbatim",
     "verbatim: expr-param in ON, shared param in WHERE.",
     outer(f"SELECT count(*) FROM {J} ON t1.unique1 = t2.hundred + t0.ten "
           "WHERE t1.twenty = t0.ten"), []),
    ("m_where_only",
     "shared-param qual in ON, expr-param moved to WHERE.",
     outer(f"SELECT count(*) FROM {J} ON t1.twenty = t0.ten "
           "WHERE t1.unique1 = t2.hundred + t0.ten"), []),
    ("m_on_only",
     "both quals inside the inner JOIN's ON clause.",
     outer(f"SELECT count(*) FROM {J} ON t1.unique1 = t2.hundred + t0.ten "
           "AND t1.twenty = t0.ten"), []),
    ("m_two_params",
     "two shared outer params in WHERE.",
     outer(f"SELECT count(*) FROM {J} ON t1.unique1 = t2.hundred + t0.ten "
           "WHERE t1.twenty = t0.ten AND t1.hundred = t0.hundred"), []),
    ("m_left_join",
     "LEFT JOIN, both quals in ON, count(*) — Memoize IS in the plan "
     "(NL Left Join -> Memoize -> Index Scan) but count(*) counts the "
     "outer row regardless of match -> staleness masked -> clean.",
     outer("SELECT count(*) FROM tenk1 t2 LEFT JOIN tenk1 t1 "
           "ON t1.unique1 = t2.hundred + t0.ten AND t1.twenty = t0.ten"), []),
    ("m_left_join_cnt",
     "LEFT JOIN counting actual matches (count(t1.unique1)) — same "
     "memoize plan, now the stale cache is observable -> fires 82000.",
     outer("SELECT count(t1.unique1) FROM tenk1 t2 LEFT JOIN tenk1 t1 "
           "ON t1.unique1 = t2.hundred + t0.ten AND t1.twenty = t0.ten"), []),
    ("m_exists",
     "correlated EXISTS subquery carrying both params.",
     outer("SELECT count(*) FROM tenk1 t2 WHERE EXISTS "
           "(SELECT 1 FROM tenk1 t1 WHERE t1.unique1 = t2.hundred + t0.ten "
           "AND t1.twenty = t0.ten)"), []),
    ("m_scalar",
     "scalar (non-count) correlated subquery in the tlist.",
     "SELECT sum(c) FROM (SELECT t0.unique1, "
     "(SELECT t1.unique1 FROM tenk1 t2 JOIN tenk1 t1 "
     "ON t1.unique1 = t2.hundred + t0.ten WHERE t1.twenty = t0.ten "
     "LIMIT 1) AS c FROM tenk1 t0 WHERE t0.unique1 < 200) s", []),
    ("m_param_inner_side",
     "expr-param on the inner side of the join qual.",
     outer(f"SELECT count(*) FROM {J} ON t1.unique1 + t0.ten = t2.hundred "
           "WHERE t1.twenty = t0.ten"), []),
    ("m_hashjoin_inner",
     "inner join forced to hash (no param'd inner -> no memoize): "
     "expected boundary -> clean.",
     outer(f"SELECT count(*) FROM {J} ON t1.unique1 = t2.hundred + t0.ten "
           "WHERE t1.twenty = t0.ten"),
     ["SET enable_nestloop = off", "SET enable_hashjoin = on"]),
    ("m_mergejoin_inner",
     "inner join forced to merge (nestloop+hash off): boundary.",
     outer(f"SELECT count(*) FROM {J} ON t1.unique1 = t2.hundred + t0.ten "
           "WHERE t1.twenty = t0.ten"),
     ["SET enable_nestloop = off", "SET enable_hashjoin = off",
      "SET enable_mergejoin = on"]),
    ("m_t0_hundred",
     "shared param on a different outer column (t0.hundred).",
     outer(f"SELECT count(*) FROM {J} "
           "ON t1.unique1 = t2.hundred + t0.hundred "
           "WHERE t1.twenty = t0.hundred"), []),
    ("m_outer_500",
     "wider outer range unique1 < 500.",
     "SELECT sum(c) FROM (SELECT t0.unique1, "
     "(SELECT count(*) FROM tenk1 t2 JOIN tenk1 t1 "
     "ON t1.unique1 = t2.hundred + t0.ten WHERE t1.twenty = t0.ten) AS c "
     "FROM tenk1 t0 WHERE t0.unique1 < 500) s", []),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefixes", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]

    report = {"versions": {}, "arms": []}
    for prefix in prefixes:
        tag = Path(prefix).name
        pg = PostgresRunner(out / f"data_{tag}", pg_prefix=prefix)
        ver = pg.engine_version
        report["versions"][tag] = ver
        pg.setup(SETUP)
        for name, src, q, extra in ARMS:
            # oracle: memoize off -> deterministic correct value
            for p in BASE + ["SET enable_memoize = off"] + extra:
                pg.run(p)
            roff = pg.run(ARMS_SQL[name])
            # buggy run: memoize on
            for p in BASE + ["SET enable_memoize = on"] + extra:
                pg.run(p)
            ron = pg.run(ARMS_SQL[name])
            for r in RESETS:
                pg.run(r)
            expected = roff.rows if roff.ok else f"ERR:{roff.error}"
            actual = ron.rows if ron.ok else f"ERR:{ron.error}"
            fired = (ron.ok and roff.ok and ron.rows != roff.rows)
            print(f"{tag:>18} {name:<20} expected={expected} "
                  f"actual={actual} -> {'FIRED' if fired else 'clean'}",
                  flush=True)
            report["arms"].append({
                "prefix": tag, "version": ver, "arm": name, "source": src,
                "expected": roff.rows, "actual": ron.rows,
                "oracle_error": roff.error, "error": ron.error,
                "outcome": "fired" if fired else "clean",
            })
        pg.cleanup()

    (out / "memoize_sweep.json").write_text(
        json.dumps(report, indent=2, default=str))
    print(f"\n-> {out}/memoize_sweep.json")


if __name__ == "__main__":
    ARMS_SQL = {name: q for name, _s, q, _e in ARMS}
    main()
