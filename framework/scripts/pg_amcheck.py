#!/usr/bin/env python3
"""amcheck oracle: DML churn followed by bt_index_parent_check.

Corruption between heap and index (or inside the index) surfaces as an
error/assert in amcheck — a class invisible to row-level oracles. Each
batch runs inside a transaction so a failing amcheck can be re-verified
against a fresh table for determinism before being reported.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner

_SCHEMA = [
    "CREATE TABLE amt (id INT PRIMARY KEY, a INT, b TEXT, c NUMERIC, "
    "d TIMESTAMP, e INT[])",
    "CREATE INDEX amt_a ON amt(a)",
    "CREATE INDEX amt_b_trgm ON amt(b)",  # plain btree on text
    "CREATE INDEX amt_c ON amt(c)",
    "CREATE INDEX amt_d ON amt(d)",
    "CREATE INDEX amt_expr ON amt((a % 7))",
    "CREATE INDEX amt_part ON amt(a) WHERE a % 3 = 0",
    "CREATE UNIQUE INDEX amt_bu ON amt(b)",
    "CREATE INDEX amt_e ON amt USING gin(e)",
]

_INDEXES = ["amt_a", "amt_b_trgm", "amt_c", "amt_d", "amt_expr", "amt_part",
            "amt_bu", "amt_e"]


def _dml(rng: random.Random) -> str:
    k = rng.randint(0, 9999)
    return rng.choice([
        f"INSERT INTO amt VALUES ({k},{k%997},'t{k}',{k}.5,now(),"
        f"'{{{k%50},{k%51}}}') ON CONFLICT (id) DO UPDATE SET a = amt.a + 1",
        f"UPDATE amt SET a = a + {rng.randint(1,9)} WHERE id = {k}",
        f"UPDATE amt SET b = 'x{k}' WHERE a % 11 = {rng.randint(0,10)}",
        f"DELETE FROM amt WHERE id = {k}",
        f"DELETE FROM amt WHERE a % 13 = {rng.randint(0,12)}",
        f"INSERT INTO amt SELECT i,i,'s'||i,i::numeric,now(),'{{1}}' FROM "
        f"generate_series({k},{k+rng.randint(1,40)}) i ON CONFLICT DO NOTHING",
        f"UPDATE amt SET c = c * 1.0001 WHERE c < {rng.randint(0,9999)}",
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--batch", type=int, default=8,
                    help="DML ops per amcheck cycle")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    pg = PostgresRunner(tempfile.mkdtemp(prefix="amcheck_"),
                        pg_prefix=args.prefix)
    findings = []
    execs = 0
    try:
        pg.setup(_SCHEMA)
        r = pg.run("CREATE EXTENSION IF NOT EXISTS amcheck",
                   timeout_s=args.timeout)
        if not r.ok:
            print(f"amcheck unavailable: {r.error}")
            return 2
        for step in range(args.rounds):
            pg.run("begin", timeout_s=args.timeout)
            for _ in range(args.batch):
                pg.run(_dml(rng), timeout_s=args.timeout)
            idx = _INDEXES[step % len(_INDEXES)]
            r = pg.run(
                f"SELECT bt_index_parent_check('{idx}', true, true)",
                timeout_s=max(args.timeout, 60))
            execs += 1
            if r.is_internal_error or (
                    not r.ok and "corrupt" in (r.error or "").lower()):
                tag = "crash" if r.is_internal_error else "corruption"
                findings.append({
                    "kind": "amcheck", "id": f"am-{step}", "index": idx,
                    "verdict": tag, "error": (r.error or "")[:400],
                })
                print(f"AMCHECK {tag} step={step} idx={idx}: "
                      f"{(r.error or '')[:120]}")
                pg.run("rollback", timeout_s=args.timeout)
                continue
            # heapallindexed variant on a second index for coverage
            idx2 = _INDEXES[(step * 3 + 1) % len(_INDEXES)]
            r2 = pg.run(f"SELECT bt_index_check('{idx2}')",
                        timeout_s=args.timeout)
            execs += 1
            if r2.is_internal_error:
                findings.append({
                    "kind": "amcheck", "id": f"am-{step}-b", "index": idx2,
                    "verdict": "crash", "error": (r2.error or "")[:400]})
                print(f"AMCHECK crash step={step} idx={idx2}: "
                      f"{(r2.error or '')[:120]}")
            pg.run("commit", timeout_s=args.timeout)
            if step % 50 == 0:
                print(f"[{step}] execs={execs} findings={len(findings)}",
                      flush=True)
    finally:
        (out / "findings.json").write_text(
            json.dumps(findings, indent=1, default=str))
        print(f"\n{execs} amcheck calls, {len(findings)} findings -> {out}")


if __name__ == "__main__":
    sys.exit(main())
