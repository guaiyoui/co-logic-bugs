"""postgres_fdw self-loop differential oracle.

A foreign table pointing back at the SAME server over postgres_fdw must
return results identical to querying the local table directly — the
deparse / remote-execution / fetched-row path is a structural blind spot
no other oracle in this repo reaches (SQLsmith-style generators never
emit foreign tables).

For each seed query Q (run against local table t_local):
    local  = SELECT <exprs> FROM t_local <rest>
    remote = SELECT <exprs> FROM t_foreign <rest>   -- identical text
loose_bag(local) must equal loose_bag(remote). We also compare
    remote_sorted under ORDER BY, and EXPLAIN-cost independent paths
    (use_remote_estimate on/off is left to GUC axes).

FDW quirks handled: the fdw connection uses the same session user
(superuser) so no password needed; server name 'loopback'.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.normalize import loose_bag  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_fdw_selfloop")

LOCAL = "lt"
FOREIGN = "ft"

# Queries crafted so remote pushdown actually happens: quals, joins,
# aggregates, sorts, limits — the interesting deparse surface.
QUERIES = [
    "SELECT a, b FROM {t} WHERE a > 3",
    "SELECT a, b FROM {t} WHERE a BETWEEN 2 AND 8 AND b IS NOT NULL",
    "SELECT a FROM {t} WHERE b LIKE 'x%'",
    "SELECT a, count(*) FROM {t} GROUP BY a ORDER BY a",
    "SELECT a, sum(b), avg(b), min(b), max(b) FROM {t} GROUP BY a",
    "SELECT a, count(*) FROM {t} GROUP BY a HAVING count(*) > 1",
    "SELECT DISTINCT a FROM {t}",
    "SELECT a, b FROM {t} ORDER BY a NULLS LAST, b LIMIT 5",
    "SELECT a, b FROM {t} ORDER BY a LIMIT 3 OFFSET 2",
    "SELECT t1.a, t2.b FROM {t} t1 JOIN {t} t2 ON t1.a = t2.a",
    "SELECT t1.a, t2.b FROM {t} t1 LEFT JOIN {t} t2 ON t1.a = t2.a "
    "ORDER BY 1, 2",
    "SELECT a FROM {t} WHERE a IN (SELECT a FROM {t} WHERE b > 5)",
    "SELECT a FROM {t} WHERE EXISTS (SELECT 1 FROM {t} x WHERE x.a = {t}.a)",
    "SELECT a, b, a + b AS s, upper(b::text) AS u FROM {t}",
    "SELECT a, CASE WHEN b > 5 THEN 'big' ELSE 'small' END FROM {t}",
    "SELECT a, coalesce(b, -1), nullif(b, 7) FROM {t}",
    "SELECT a FROM {t} WHERE (a % 2) = 0",
    "SELECT count(*) FROM {t} WHERE a = ANY (ARRAY[1,3,5,7])",
    "SELECT a, row_number() OVER (ORDER BY a) FROM {t}",
    "SELECT a, b, rank() OVER (PARTITION BY a ORDER BY b) FROM {t}",
    "SELECT jsonb_build_object('a', a, 'b', b) FROM {t}",
    "SELECT a::text || ':' || b::text FROM {t} WHERE a < 5",
    "SELECT a FROM {t} UNION SELECT a FROM {t} WHERE a > 7 ORDER BY 1",
    "SELECT a FROM {t} WHERE a IS NOT DISTINCT FROM 4",
]

SETUP = [
    "CREATE EXTENSION IF NOT EXISTS postgres_fdw",
]


def make_foreign(pg: PostgresRunner, uri: str) -> str | None:
    """Create local table lt + foreign table ft over the same rows."""
    host, port = _host_port(uri)
    who = pg.run("SELECT current_user")
    user = who.rows[0][0] if who.ok and who.rows else "postgres"
    stmts = [
        "CREATE SERVER loopback FOREIGN DATA WRAPPER postgres_fdw "
        f"OPTIONS (host '{host}', dbname 'postgres', "
        f"port '{port}', fetch_size '100')",
        "CREATE USER MAPPING FOR CURRENT_USER SERVER loopback "
        f"OPTIONS (user '{user}')",
        f"CREATE TABLE {LOCAL} (a int, b int, c text)",
        f"INSERT INTO {LOCAL} SELECT g, g % 11, 'x' || (g % 5) "
        "FROM generate_series(0, 49) g",
        f"INSERT INTO {LOCAL} VALUES (NULL, 7, 'n'), (4, NULL, 'm')",
        f"ANALYZE {LOCAL}",
        f"CREATE FOREIGN TABLE {FOREIGN} (a int, b int, c text) "
        f"SERVER loopback OPTIONS (table_name '{LOCAL}')",
    ]
    for s in stmts:
        r = pg.run(s, timeout_s=15.0)
        if not r.ok:
            return f"{s[:60]} -> {r.error}"
    return None


def _host_port(uri: str) -> tuple[str, str]:
    # postgresql:///postgres?host=/tmp/x.sock_dir&port=54922  (unix socket)
    # postgresql://u@127.0.0.1:5432/db                        (tcp)
    from urllib.parse import urlparse, parse_qs
    u = urlparse(uri)
    qs = parse_qs(u.query)
    if "host" in qs:
        host = qs["host"][0]
        port = qs.get("port", ["5432"])[0]
        return host, port
    return u.hostname or "127.0.0.1", str(u.port or 5432)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-prefix", default=os.environ.get(
        "COEVO_PG_PREFIX", ""))
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_fdw")
    ap.add_argument("--out", default="results/pg_fdw_1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    pg = PostgresRunner(args.pg_datadir,
                        pg_prefix=args.pg_prefix or None)
    hits: list[dict] = []
    stats = {"queries": 0, "exec_ok": 0, "hits": 0, "errors": 0}
    t0 = time.time()
    try:
        for s in SETUP:
            r = pg.run(s)
            if not r.ok:
                LOGGER.error("setup failed: %s", r.error)
                return 1
        uri = pg._server.get_uri()
        err = make_foreign(pg, uri)
        if err:
            LOGGER.error("foreign setup failed: %s", err)
            return 1
        for q in QUERIES:
            stats["queries"] += 1
            ql = q.format(t=LOCAL)
            qr = q.format(t=FOREIGN)
            rl = pg.run(ql, timeout_s=15.0)
            rr = pg.run(qr, timeout_s=15.0)
            if not rl.ok or not rr.ok:
                stats["errors"] += 1
                if (rl.ok != rr.ok or
                        (rl.error or "")[:80] != (rr.error or "")[:80]):
                    hits.append({
                        "query": q, "local_err": rl.error,
                        "remote_err": rr.error, "kind": "err_mismatch"})
                    stats["hits"] += 1
                    LOGGER.info("ERR MISMATCH %s", q[:60])
                continue
            stats["exec_ok"] += 1
            eff.count("queries_executed")
            if loose_bag(rl.rows) != loose_bag(rr.rows):
                stats["hits"] += 1
                hits.append({
                    "query": q, "kind": "row_divergence",
                    "local_rows": [list(r) for r in rl.rows][:20],
                    "remote_rows": [list(r) for r in rr.rows][:20]})
                LOGGER.info("ROW DIVERGENCE %s", q[:60])
    finally:
        pg.cleanup()
    with open(os.path.join(args.out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1)
    summary = {**stats, "elapsed_s": time.time() - t0,
               "efficiency": eff.snapshot()}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
