"""Cursor/portal divergence oracle for PostgreSQL.

``DECLARE c CURSOR FOR q; FETCH ALL FROM c`` plans ``q`` under
``CURSOR_OPT`` mode: the planner optimizes for returning the first
``cursor_tuple_fraction`` (default 0.1) of rows quickly, which can pick
different plans than a plain SELECT — a bug surface that ordinary
plan-variant sweeps never reach. The bag of rows fetched through any
cursor must equal the bag from a direct run; a divergence is a sound
correctness signal.

Modes swept per query:
- default cursor (cursor_tuple_fraction = 0.1 implicit)
- cursor_tuple_fraction = 0.0 / 1.0 extremes
- WITH HOLD cursor fetched after COMMIT (materialize-to-tempfile path)
- WITH HOLD + fraction 0.0
- FETCH 2 in a loop (incremental portal execution)

The runner's ``run()`` uses autocommit, so server-side cursors are driven
through the raw psycopg2 connection inside an explicit BEGIN/COMMIT block
(pattern follows pg_dml_hunt.run_config).

Usage:
    python scripts/pg_cursor_oracle.py --out results/pg_cursor_1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.determinism import (  # noqa: E402
    has_intrinsic_nondeterminism,
    has_nondeterministic_tiebreak,
    is_result_order_sensitive,
)
from oracles.normalize import (  # noqa: E402
    is_internal_error,
    loose_bag,
    normalize_rows,
)
from seeds.pg_targets import as_seeds  # noqa: E402
from seeds.store import Seed, SeedCorpus  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_cursor_oracle")

# (label, holdable, cursor_tuple_fraction or None, fetch_chunk or 0=ALL)
CURSOR_MODES: list[tuple[str, bool, float | None, int]] = [
    ("cur_default", False, None, 0),
    ("ctf_zero", False, 0.0, 0),
    ("ctf_one", False, 1.0, 0),
    ("holdable", True, None, 0),
    ("hold_ctf0", True, 0.0, 0),
    ("fetch_2loop", False, None, 2),
]

_MAX_ROWS = 200_000  # safety bound for chunked fetch loops

# PG internal-class error markers beyond normalize.is_internal_error:
# psycopg2 names XX000 exceptions "InternalError" (mixed case, missed by
# the uppercase "INTERNAL" marker), and planner/executor internal errors
# carry distinctive messages that never appear for ordinary SQL errors.
_INTERNAL_MARKERS = (
    "xx000",
    "assert",
    "panic",
    "unexpected",
    "cache lookup failed",
    "unrecognized node",
    "variable not found in subplan",
    "no relation entry",
    "cannot compare",
    "could not find pathkey",
    "server closed the connection",
    "terminating connection",
    "connection not open",
    "could not receive data from server",
    "internalerror",
)


def looks_internal(error: str | None) -> bool:
    """True when an error string indicates a DBMS-internal failure."""
    if not error:
        return False
    if is_internal_error(error):
        return True
    low = error.lower()
    return any(marker in low for marker in _INTERNAL_MARKERS)


def _cursor_fetch(pg: PostgresRunner, query: str, holdable: bool,
                  fraction: float | None, chunk: int):
    """Fetch ``query`` through a server-side cursor.

    Returns ``(normalized_rows, None)`` or ``(None, error_str)``. Cursor
    state is always cleaned up; a dead connection triggers a reconnect so
    later seeds are unaffected.
    """
    try:
        conn = pg._conn or pg.connect()
        cur = conn.cursor()
    except Exception:  # noqa: BLE001 - stale/dead connection: reconnect once
        try:
            conn = pg.connect()
            cur = conn.cursor()
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"
    try:
        if fraction is not None:
            cur.execute(f"SET cursor_tuple_fraction = {fraction}")
        cur.execute("BEGIN")
        hold = " WITH HOLD" if holdable else ""
        cur.execute(f"DECLARE coevo_cur CURSOR{hold} FOR {query}")
        rows: list = []
        if holdable:
            # Committing with a held portal forces materialization into a
            # tuplestore temp file; fetching afterwards reads that path.
            cur.execute("COMMIT")
            cur.execute("FETCH ALL FROM coevo_cur")
            rows = cur.fetchall()
            cur.execute("CLOSE coevo_cur")
        else:
            if chunk:
                while True:
                    cur.execute(f"FETCH {chunk} FROM coevo_cur")
                    batch = cur.fetchall()
                    if not batch:
                        break
                    rows.extend(batch)
                    if len(rows) > _MAX_ROWS:
                        break
            else:
                cur.execute("FETCH ALL FROM coevo_cur")
                rows = cur.fetchall()
            cur.execute("CLOSE coevo_cur")
            cur.execute("COMMIT")
        if fraction is not None:
            cur.execute("RESET cursor_tuple_fraction")
        return normalize_rows(rows), None
    except Exception as exc:  # noqa: BLE001 - any engine error recorded
        err = f"{type(exc).__name__}: {exc}"
        try:
            conn.cursor().execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.cursor().execute("CLOSE coevo_cur")
        except Exception:  # noqa: BLE001
            pass
        if not pg._alive():
            try:
                pg.connect()
            except Exception:  # noqa: BLE001
                LOGGER.debug("reconnect after cursor failure failed",
                             exc_info=True)
        return None, err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-datadir", default="/tmp/coevo_pg_cursor")
    ap.add_argument("--corpus", default="seeds/corpus.json")
    ap.add_argument("--max-seeds", type=int, default=0)
    ap.add_argument("--out", default="results/pg_cursor_1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    corpus = SeedCorpus.load(args.corpus)
    seeds = [s for s in corpus.seeds
             if s.query and "SELECT" in s.query.upper()]
    seeds += [Seed(**d) for d in as_seeds()]
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("cursor oracle over %d seeds x %d modes",
                len(seeds), len(CURSOR_MODES))

    pg = PostgresRunner(args.pg_datadir)
    hits: list[dict] = []
    stats = {"seeds": 0, "exec_ok": 0, "cursor_runs": 0, "hits": 0,
             "skipped_nondet": 0, "cursor_errors": 0}
    t0 = time.time()
    try:
        for i, seed in enumerate(seeds):
            stats["seeds"] += 1
            q = seed.query.strip().rstrip(";")
            if not q.upper().lstrip().startswith(("SELECT", "WITH", "TABLE",
                                                  "VALUES")):
                continue
            if (has_intrinsic_nondeterminism(q)
                    or is_result_order_sensitive(q)):
                stats["skipped_nondet"] += 1
                continue
            outcomes = pg.setup(seed.setup_sqls)
            if any(err for _, err in outcomes):
                continue
            # Data-dependent gate: LIMIT on non-unique keys, order-dependent
            # windows on non-unique partition keys (re-runs setup internally).
            try:
                if has_nondeterministic_tiebreak(q, pg, seed.setup_sqls):
                    stats["skipped_nondet"] += 1
                    continue
            except Exception:  # noqa: BLE001 - gate failure: skip seed
                stats["skipped_nondet"] += 1
                continue
            direct = pg.run(q, timeout_s=10.0)
            if not direct.ok or direct.timed_out:
                continue
            stats["exec_ok"] += 1
            eff.count("queries_executed")
            base_bag = loose_bag(direct.rows)
            for label, holdable, fraction, chunk in CURSOR_MODES:
                rows, err = _cursor_fetch(pg, q, holdable, fraction, chunk)
                stats["cursor_runs"] += 1
                eff.count("cursor_executions")
                if err is not None:
                    stats["cursor_errors"] += 1
                    if looks_internal(err):
                        stats["hits"] += 1
                        hits.append({
                            "kind": "cursor_internal_error",
                            "mode": label,
                            "source": seed.source,
                            "query": q,
                            "setup_sqls": list(seed.setup_sqls),
                            "cursor_error": err,
                            "direct_rows":
                                [list(r) for r in direct.rows][:20],
                        })
                        LOGGER.info("CURSOR INTERNAL ERROR %s mode=%s",
                                    seed.source, label)
                    continue
                if loose_bag(rows) != base_bag:
                    stats["hits"] += 1
                    hits.append({
                        "kind": "divergence",
                        "mode": label,
                        "source": seed.source,
                        "query": q,
                        "setup_sqls": list(seed.setup_sqls),
                        "direct_rows": [list(r) for r in direct.rows][:20],
                        "cursor_rows": [list(r) for r in rows][:20],
                    })
                    LOGGER.info("DIVERGENCE %s mode=%s", seed.source, label)
            if (i + 1) % 40 == 0:
                LOGGER.info("seed %d/%d hits=%d", i + 1, len(seeds),
                            stats["hits"])
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
