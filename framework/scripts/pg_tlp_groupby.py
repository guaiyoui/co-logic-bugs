#!/usr/bin/env python3
"""Drive the TLP GROUP BY/DISTINCT oracle (+ PINOLO-lite) on a PG build.

Pipeline: corpus seeds -> SeedMutator (same predicate generation as the
existing TLP-where hunt) -> for each mutation carrying a predicate, take
its FROM tail, resolve group-key columns from the seed's CREATE TABLEs
(alias-aware), and emit one case that runs BOTH shapes:

  SELECT k1[,k2] <tail> [WHERE p] GROUP BY k1[,k2]      (tlp_groupby)
  SELECT DISTINCT k1[,k2] <tail> [WHERE p]              (tlp_distinct)

plus the PINOLO-lite conjunction check on each shape (WHERE p AND q ⊆
WHERE p).  Original-vs-partitions comparison is a frozenset union — see
oracles/tlp_groupby.py for why a bag would be a systematic FP.

Usage:
  python scripts/pg_tlp_groupby.py --prefix $COEVO_PGBLD/pg186_assert \
      --cases 10                 # smoke
  python scripts/pg_tlp_groupby.py --prefix .../pgmaster_assert --cases 120
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracles.determinism import (  # noqa: E402
    _top_level_from_pos,
    _top_level_positions,
    has_intrinsic_nondeterminism,
)
from oracles.tlp_groupby import TLPGroupByOracle  # noqa: E402
from seeds.mutate import (  # noqa: E402
    _BOOL_TYPES,
    _NUMERIC_TYPES,
    _STRING_TYPES,
    _alias_for,
    _from_tail,
    _parse_columns,
    _predicate_for_column,
    SeedMutator,
)
from seeds.store import Seed  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
LOGGER = logging.getLogger("pg_tlp_groupby")

DEFAULT_MODULES = "pg_targets,pg18_targets,pg_edge_targets"


def load_seeds(modules: str) -> list[Seed]:
    seeds: list[Seed] = []
    for name in modules.split(","):
        name = name.strip()
        if not name:
            continue
        mod = importlib.import_module(f"seeds.{name}")
        for s in mod.as_seeds():
            seeds.append(
                Seed(
                    setup_sqls=list(s.get("setup_sqls", [])),
                    query=s["query"],
                    source=s.get("source", name),
                    engine=s.get("engine", "postgres"),
                    tags=list(s.get("tags", [])),
                )
            )
    return seeds


def _key_expr(qual: str, col: str, ctype: str, rng: random.Random) -> str:
    """Group-key expression: mostly the bare column, sometimes a cheap
    deterministic expression (exercises expressional grouping paths)."""
    base = f"{qual}.{col}"
    if rng.random() < 0.30:
        if _NUMERIC_TYPES.search(ctype):
            return rng.choice(
                [f"({base} % 7)", f"({base} + 0)", f"({base} * 2)"]
            )
        if _STRING_TYPES.search(ctype):
            return rng.choice(
                [f"substr({base}, 1, 2)", f"length({base})",
                 f"({base} || '')"]
            )
        if _BOOL_TYPES.search(ctype):
            return f"({base} IS TRUE)"
    return base


def _conjoin_pred(
    names: list[str],
    tables: dict[str, list[tuple[str, str]]],
    tail: str,
    prefer_not: str,
    rng: random.Random,
) -> str | None:
    """Second predicate for PINOLO-lite, biased to a *different* table than
    the one feeding the group key (join-qual-loss bugs live there)."""
    others = [t for t in names if t != prefer_not] or names
    tq = rng.choice(others)
    qc = _alias_for(tail, tq)
    col, ctype = rng.choice(tables[tq])
    return _predicate_for_column(qc, col, ctype, rng)


def _top_level_clause(query: str, keyword: str) -> str | None:
    """Text of a top-level clause (``GROUP BY``/``WHERE``), or None."""
    positions = _top_level_positions(query)
    for i, (pos, kw) in enumerate(positions):
        if kw == keyword:
            end = positions[i + 1][0] if i + 1 < len(positions) else len(query)
            return query[pos + len(keyword) : end].strip()
    return None


_GSET_RE = re.compile(r"\b(grouping\s+sets|rollup|cube)\b", re.IGNORECASE)
_DISTINCT_RE = re.compile(r"^\s*select\s+distinct\b", re.IGNORECASE)
_DISTINCT_ON_RE = re.compile(
    r"^\s*select\s+distinct\s+on\b", re.IGNORECASE
)


def _seed_distinct_cols(query: str) -> str | None:
    """Select list of a plain ``SELECT DISTINCT ...`` seed, or None.

    DISTINCT ON / no-FORM seeds are skipped; the lifted column list is
    reused as both the DISTINCT projection and (equivalently) the GROUP BY
    key list, so both executor paths see the seed's real expressions.
    """
    if not _DISTINCT_RE.search(query) or _DISTINCT_ON_RE.search(query):
        return None
    from_pos = _top_level_from_pos(query)
    if from_pos is None:
        return None
    head = query[:from_pos]
    m = _DISTINCT_RE.match(head)
    cols = head[m.end() :].strip()
    keys = [
        re.sub(r"\s+as\s+\w+\s*$", "", k.strip(), flags=re.IGNORECASE)
        for k in _split_commas(cols)
        if k.strip()
    ]
    if not keys or "*" in keys:
        return None
    return ", ".join(keys)


def _seed_group_keys(query: str) -> str | None:
    """GROUP BY key list lifted from the seed's own query, or None.

    Skips grouping-sets/rollup/cube (their placeholder NULL rows still
    satisfy the identity, but the key list is not a plain expression list)
    and pure-ordinal keys (``GROUP BY 1`` — positional, not an expression).
    """
    text = _top_level_clause(query, "GROUP BY")
    if not text or _GSET_RE.search(text):
        return None
    keys = [k.strip() for k in _split_commas(text) if k.strip()]
    if not keys or all(re.fullmatch(r"\d+", k) for k in keys):
        return None
    return ", ".join(keys)


def _split_commas(text: str) -> list[str]:
    out, depth, cur, in_str = [], 0, "", False
    for ch in text:
        if ch == "'":
            in_str = not in_str
        if not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                out.append(cur)
                cur = ""
                continue
        cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _tables_in_tail(tail: str, tables: dict) -> list[str]:
    return [t for t in tables if re.search(rf"\b{re.escape(t)}\b", tail, re.I)]


def _synth_pred(
    names: list[str],
    tables: dict[str, list[tuple[str, str]]],
    tail: str,
    rng: random.Random,
) -> str | None:
    """Fresh predicate over the tail's columns; for multi-table tails,
    ~half the time combine quals on two different tables (cross-side qual
    placement is the classic group-by fault surface)."""
    t1 = rng.choice(names)
    p1 = _predicate_for_column(
        _alias_for(tail, t1), *rng.choice(tables[t1]), rng
    )
    if p1 is None:
        return None
    if len(names) >= 2 and rng.random() < 0.5:
        t2 = rng.choice([t for t in names if t != t1])
        p2 = _predicate_for_column(
            _alias_for(tail, t2), *rng.choice(tables[t2]), rng
        )
        if p2:
            op = "AND" if rng.random() < 0.7 else "OR"
            return f"({p1}) {op} ({p2})"
    return p1


def _numeric_cols(cols: list[tuple[str, str]]) -> list[str]:
    return [c for c, t in cols if _NUMERIC_TYPES.search(t)]


def _synth_join_tails(
    tables: dict[str, list[tuple[str, str]]], rng: random.Random
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Synthesized FROM tails the seed pool under-provides: equi-joins on
    numeric columns, self-joins (SJE bait), and LEFT JOINs.

    Returns ``(tail, sides)`` where ``sides`` is a list of
    ``(alias, table_name)`` pairs — two entries for a self-join so that
    predicates/keys can target the ``b`` side too.
    """
    out: list[tuple[str, list[tuple[str, str]]]] = []
    names = list(tables)
    if len(names) >= 2:
        t1, t2 = rng.sample(names, 2)
        n1, n2 = _numeric_cols(tables[t1]), _numeric_cols(tables[t2])
        if n1 and n2:
            joinop = rng.choice(["JOIN", "LEFT JOIN"])
            out.append(
                (
                    f"FROM {t1} a {joinop} {t2} b "
                    f"ON a.{rng.choice(n1)} = b.{rng.choice(n2)}",
                    [("a", t1), ("b", t2)],
                )
            )
    if names:
        t = rng.choice(names)
        nc = _numeric_cols(tables[t])
        if len(nc) >= 2:
            k1, k2 = rng.sample(nc, 2)
            joinop = rng.choice(["JOIN", "LEFT JOIN"])
            out.append(
                (f"FROM {t} a {joinop} {t} b ON a.{k1} = b.{k2}",
                 [("a", t), ("b", t)])
            )
    return out


def gen_cases(
    seeds: list[Seed],
    rng: random.Random,
    max_cases: int,
    max_per_seed: int,
) -> list[dict]:
    """Derive oracle-ready group-by cases from three sources:

    1. ``mut``: SeedMutator mutations carrying a predicate (the existing
       TLP-where driver pipeline) — keys are columns of the tail's tables.
    2. ``seed_gb``: the seed's own GROUP BY key list and WHERE clause,
       repartitioned under a fresh predicate (partitioned tables and real
       join quals come in through here).
    3. ``join``: synthesized equi/left/self-join tails over the seed's own
       tables — the seed pool is overwhelmingly single-table, while the
       classic group-by TLP bugs need cross-side quals.
    """
    mutator = SeedMutator(random.Random(rng.randint(0, 1 << 30)))
    cases: list[dict] = []
    seen: set[tuple] = set()

    def emit(
        setup, tail, keys, pred, conjoin, base_where, category, source,
        seed_q,
    ) -> bool:
        dedup = (tail, keys, pred, base_where)
        if dedup in seen:
            return False
        probe = build_probe(keys, tail, keys, pred, base_where)
        if has_intrinsic_nondeterminism(probe):
            return False
        seen.add(dedup)
        cases.append(
            {
                "name": f"{source}::{category}#{len(cases)}",
                "setup": list(setup),
                "from_tail": tail,
                "select_list": keys,
                "group_by": keys,
                "predicate": pred,
                "conjoin": conjoin,
                "base_where": base_where,
                "category": category,
                "source": source,
                "seed_query": seed_q[:200],
            }
        )
        return True

    seed_order = list(seeds)
    rng.shuffle(seed_order)
    for seed in seed_order:
        if len(cases) >= max_cases:
            break
        tables = _parse_columns(seed.setup_sqls)

        # ---- source 1: mutator predicate pipeline ----
        for mut in mutator.mutations_for(seed, max_per_seed=max_per_seed):
            if len(cases) >= max_cases:
                break
            if not mut.predicate or not mut.select_from:
                continue
            tail = _from_tail(mut.select_from)
            if not tail:
                continue
            names = _tables_in_tail(tail, tables)
            if not names:
                continue
            tname = rng.choice(names)
            qual = _alias_for(tail, tname)
            keys = [_key_expr(qual, *rng.choice(tables[tname]), rng)]
            if rng.random() < 0.45:
                t2 = rng.choice(names)
                k2 = _key_expr(
                    _alias_for(tail, t2), *rng.choice(tables[t2]), rng
                )
                if k2 not in keys:
                    keys.append(k2)
            emit(
                mut.setup_sqls, tail, ", ".join(keys), mut.predicate,
                _conjoin_pred(names, tables, tail, tname, rng),
                None, mut.category, mut.source, seed.query,
            )

        # ---- source 2: seed's own GROUP BY keys / DISTINCT cols ----
        if len(cases) < max_cases and tables:
            keys = _seed_group_keys(seed.query) or _seed_distinct_cols(
                seed.query
            )
            tail = _from_tail(seed.query)
            if keys and tail:
                names = _tables_in_tail(tail, tables)
                if names:
                    pred = _synth_pred(names, tables, tail, rng)
                    if pred:
                        emit(
                            seed.setup_sqls, tail, keys, pred,
                            _conjoin_pred(names, tables, tail, names[0], rng),
                            _top_level_clause(seed.query, "WHERE"),
                            "seed_own_groupby", seed.source, seed.query,
                        )

        # ---- source 3: synthesized join tails ----
        if len(cases) < max_cases and tables:
            for tail, sides in _synth_join_tails(tables, rng):
                if len(cases) >= max_cases:
                    break
                pairs = [
                    (al, c, t)
                    for al, tname in sides
                    for (c, t) in tables[tname]
                ]
                if not pairs:
                    continue
                a1, c1, t1 = rng.choice(pairs)
                keys = [_key_expr(a1, c1, t1, rng)]
                if rng.random() < 0.6 and len(pairs) > 1:
                    a2, c2, t2 = rng.choice(pairs)
                    k2 = _key_expr(a2, c2, t2, rng)
                    if k2 not in keys:
                        keys.append(k2)
                # one predicate per join side when possible
                side_preds = []
                for al, tname in sides:
                    p = _predicate_for_column(
                        al, *rng.choice(tables[tname]), rng
                    )
                    if p:
                        side_preds.append(p)
                if not side_preds:
                    continue
                if len(side_preds) == 2:
                    op = "AND" if rng.random() < 0.7 else "OR"
                    pred = f"({side_preds[0]}) {op} ({side_preds[1]})"
                else:
                    pred = side_preds[0]
                cal, ctab = rng.choice(sides)
                conjoin = _predicate_for_column(
                    cal, *rng.choice(tables[ctab]), rng
                )
                emit(
                    seed.setup_sqls, tail, ", ".join(keys), pred, conjoin,
                    None, "synth_join", seed.source, seed.query,
                )
    return cases


def build_probe(
    select_list: str, tail: str, group_by: str, pred: str,
    base_where: str | None = None,
) -> str:
    sql = f"SELECT {select_list} {tail} WHERE "
    if base_where:
        sql += f"({base_where}) AND "
    return sql + f"({pred}) GROUP BY {group_by}"


# Benign setup leftovers: objects that survive DROP SCHEMA public CASCADE
# (extensions, etc.) must not disqualify a case.
_BENIGN_SETUP_ERR = re.compile(r"already exists", re.IGNORECASE)


def _setup_failures(outcomes: list[tuple[str, str | None]]) -> list[str]:
    return [e for _, e in outcomes if e and not _BENIGN_SETUP_ERR.search(e)]


def run_case(
    pg: PostgresRunner, oracle: TLPGroupByOracle, case: dict, timeout: float
) -> dict:
    out = {
        "name": case["name"],
        "category": case["category"],
        "source": case["source"],
        "select_list": case["select_list"],
        "from_tail": case["from_tail"],
        "predicate": case["predicate"],
        "conjoin": case["conjoin"],
        "statuses": {},
        "hits": [],
    }
    setup_res = pg.setup(case["setup"])
    bad = _setup_failures(setup_res)
    if bad:
        out["status"] = "setup_fail"
        out["detail"] = bad[:2]
        return out

    for mode in ("groupby", "distinct"):
        group_by = case["group_by"] if mode == "groupby" else None
        status, cands = oracle.check(
            pg,
            select_list=case["select_list"],
            from_tail=case["from_tail"],
            predicate=case["predicate"],
            group_by=group_by,
            conjoin=case["conjoin"],
            base_where=case.get("base_where"),
            schema_sqls=case["setup"],
            category=case["category"],
            timeout_s=timeout,
        )
        out["statuses"][mode] = status
        for c in cands:
            rec = c.to_dict()
            rec["mode"] = mode
            rec["case"] = case["name"]
            rec["repro_sql"] = _repro(case, mode)
            out["hits"].append(rec)
    fatals = pg.log_fatal_lines(pg.log_new_lines())
    if fatals:
        out["log_fatal"] = fatals[:4]
        out["statuses"]["log_fatal"] = True
    return out


def _repro(case: dict, mode: str) -> str:
    """Minimal self-contained reproduction script for one mode."""
    from oracles.tlp_groupby import build_query

    lines = [s.rstrip(";") + ";" for s in case["setup"]]
    gb = case["group_by"] if mode == "groupby" else None
    distinct = mode == "distinct"
    bw = case.get("base_where")
    lines.append("-- original")
    lines.append(
        build_query(case["select_list"], case["from_tail"], gb,
                    distinct=distinct, base_where=bw) + ";"
    )
    for label, w in (
        ("p", f"({case['predicate']})"),
        ("not p", f"NOT ({case['predicate']})"),
        ("p is null", f"({case['predicate']}) IS NULL"),
    ):
        lines.append(f"-- partition {label}")
        lines.append(
            build_query(case["select_list"], case["from_tail"], gb,
                        where=w, distinct=distinct, base_where=bw) + ";"
        )
    if case.get("conjoin"):
        lines.append("-- pinolo p AND q")
        lines.append(
            build_query(
                case["select_list"], case["from_tail"], gb,
                where=f"({case['predicate']}) AND ({case['conjoin']})",
                distinct=distinct, base_where=bw,
            ) + ";"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, help="PG install prefix")
    ap.add_argument("--datadir", default="",
                    help="server datadir (default: <out>/dd_<build>)")
    ap.add_argument("--out", default="results/pg_tlp_groupby")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cases", type=int, default=120)
    ap.add_argument("--max-per-seed", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--seed-modules", default=DEFAULT_MODULES)
    args = ap.parse_args()

    prefix = str(Path(args.prefix).resolve())
    build = Path(prefix).name
    if "asan" in build:
        os.environ.setdefault("ASAN_OPTIONS", "detect_leaks=0")
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    datadir = args.datadir or str(outdir / f"dd_{build}")

    seeds = load_seeds(args.seed_modules)
    rng = random.Random(args.seed)
    cases = gen_cases(seeds, rng, args.cases, args.max_per_seed)
    LOGGER.info(
        "%d cases from %d seeds, prefix=%s", len(cases), len(seeds), prefix
    )

    pg = PostgresRunner(datadir, pg_prefix=prefix)
    oracle = TLPGroupByOracle()
    stats: Counter = Counter()
    hits: list[dict] = []
    t0 = time.monotonic()
    try:
        LOGGER.info("server_version=%s", pg.engine_version)
        for i, case in enumerate(cases):
            res = run_case(pg, oracle, case, args.timeout)
            stats[res.get("status", "ok")] += 1
            for mode, st in res.get("statuses", {}).items():
                stats[f"{mode}:{st}"] += 1
            if res.get("hits"):
                hits.extend(res["hits"])
                for h in res["hits"]:
                    LOGGER.warning(
                        "HIT %s/%s %s", h["kind"], h["mode"], res["name"]
                    )
            if res.get("log_fatal"):
                stats["log_fatal"] += 1
                LOGGER.warning(
                    "server log fatal on %s: %s", res["name"],
                    res["log_fatal"][:2],
                )
            if (i + 1) % 20 == 0:
                LOGGER.info(
                    "case %d/%d stats=%s", i + 1, len(cases), dict(stats)
                )
    finally:
        pg.cleanup()

    elapsed = time.monotonic() - t0
    summary = {
        "build": build,
        "prefix": prefix,
        "seed": args.seed,
        "seed_modules": args.seed_modules,
        "cases": len(cases),
        "stats": dict(stats),
        "elapsed_s": round(elapsed, 1),
        "hits": hits,
    }
    run_path = outdir / f"run_{build}_s{args.seed}.json"
    run_path.write_text(json.dumps(summary, indent=2, default=str))
    if hits:
        hits_path = outdir / "hits.json"
        # merge with any earlier runs for the same output dir
        existing = []
        if hits_path.exists():
            try:
                existing = json.loads(hits_path.read_text())
            except json.JSONDecodeError:
                existing = []
        hits_path.write_text(
            json.dumps(existing + hits, indent=2, default=str)
        )
    LOGGER.info("DONE %s stats=%s hits=%d", build, dict(stats), len(hits))
    print(json.dumps({k: summary[k] for k in
                      ("build", "cases", "stats", "elapsed_s")}, indent=2))
    print(f"run file: {run_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
