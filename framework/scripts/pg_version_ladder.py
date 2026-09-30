"""PostgreSQL version-ladder differential.

Replays a seed corpus across several self-compiled PostgreSQL install
prefixes (``--prefixes`` order defines the version order, oldest first) and
reports cross-version result divergences. Each prefix gets its own datadir
under ``<out>/data_<tag>`` driven by ``targets.pg_local_server.LocalPgServer``
through ``PostgresRunner(pg_prefix=...)``.

A *divergence* is: same seed, same query, and either a different normalized
result bag or a different error-class outcome (``ok`` / ``error`` /
``internal-error`` / ``timeout`` / ``setup-error``) between any two versions.
Both adjacent pairs (regressions between consecutive releases) and all-vs-all
pairs are reported — a feature added in version N legitimately diverges from
N-1, but then N..N+k should agree with each other.

Lexical determinism gates (``has_intrinsic_nondeterminism`` /
``is_result_order_sensitive`` from oracles.determinism) skip seeds whose
verdicts could never be trusted on any version.

Usage:
    python scripts/pg_version_ladder.py \
        --prefixes /path/pg162_assert,/path/pg1711_assert \
        --out results/pg_ladder_1 --timeout 10
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import itertools
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracles.determinism import (  # noqa: E402
    has_intrinsic_nondeterminism,
    is_result_order_sensitive,
)
from oracles.normalize import is_internal_error  # noqa: E402
from seeds.store import SeedCorpus  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_version_ladder")

_QUERY_PREFIXES = ("SELECT", "WITH", "TABLE", "VALUES")


def load_seeds(spec: str | None) -> list[dict]:
    """Seed dicts from a corpus .json, a .py file or dotted module exposing
    ``as_seeds()``, or (default) the pg_targets template set."""
    if spec is None:
        # as_seeds() already contains the round-2 templates; the explicit
        # as_seeds_r2() call is kept for clarity and deduped by (source, query)
        # so a template listing change can never double-run seeds.
        from seeds.pg_targets import as_seeds, as_seeds_r2

        seen: set[tuple[str, str]] = set()
        seeds: list[dict] = []
        for d in as_seeds() + as_seeds_r2():
            key = (d["source"], d["query"])
            if key not in seen:
                seen.add(key)
                seeds.append(d)
        return seeds
    if spec.endswith(".json"):
        return [
            {"setup_sqls": s.setup_sqls, "query": s.query,
             "source": s.source}
            for s in SeedCorpus.load(spec).seeds
        ]
    if os.path.exists(spec):
        spec_obj = importlib.util.spec_from_file_location(
            "ladder_seeds", spec)
        mod = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(mod)
    else:
        mod = importlib.import_module(spec)
    return list(mod.as_seeds())


def _classify(result) -> str:
    """Error-class of one run: ok | timeout | internal-error | error."""
    if result.timed_out:
        return "timeout"
    if result.ok:
        return "ok"
    if result.is_internal_error or is_internal_error(result.error):
        return "internal-error"
    return "error"


def _signature(rec: dict):
    """Comparison key: error class, plus the canonical bag when ok."""
    if rec["class"] != "ok":
        return (rec["class"], None)
    return ("ok", tuple(sorted(rec["bag"].items())))


def _public(rec: dict) -> dict:
    """JSON-safe view of one version's outcome for hits.json."""
    out = {"class": rec["class"]}
    if rec.get("error"):
        out["error"] = rec["error"][:500]
    if "errors" in rec:
        out["setup_errors"] = [e[:300] for e in rec["errors"]][:10]
    if rec["class"] == "ok":
        out["rows"] = [list(r) for r in rec["rows"]][:20]
        out["row_count"] = len(rec["rows"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefixes", required=True,
                    help="comma list of install prefixes, oldest first; "
                         "each must contain bin/initdb + bin/pg_ctl")
    ap.add_argument("--seeds", default=None,
                    help="corpus .json, .py file or module exposing "
                         "as_seeds(); default: seeds.pg_targets "
                         "as_seeds()+as_seeds_r2()")
    ap.add_argument("--out", default="results/pg_ladder_1")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="per-query wall-clock timeout, seconds")
    ap.add_argument("--max-seeds", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    os.makedirs(args.out, exist_ok=True)

    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    if len(prefixes) < 2:
        LOGGER.error("need at least two prefixes to diff")
        return 2
    tags: list[str] = []
    for i, prefix in enumerate(prefixes):
        tag = Path(prefix).name or f"v{i}"
        if tag in tags:  # same leaf name twice: disambiguate by position
            tag = f"{tag}#{i}"
        tags.append(tag)

    runners: dict[str, PostgresRunner] = {}
    versions: dict[str, str] = {}
    for tag, prefix in zip(tags, prefixes):
        LOGGER.info("starting %s from %s", tag, prefix)
        pg = PostgresRunner(
            os.path.join(args.out, f"data_{tag}"),
            version_tag=tag,
            pg_prefix=prefix,
        )
        runners[tag] = pg
        try:
            versions[tag] = pg.engine_version
        except Exception:  # noqa: BLE001 - version probe is informational
            versions[tag] = "unknown"
    LOGGER.info("versions: %s", versions)

    seeds = load_seeds(args.seeds)
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    LOGGER.info("ladder over %d seeds x %d versions", len(seeds), len(tags))

    hits: list[dict] = []
    stats = {"seeds": 0, "run": 0, "skipped_nondet": 0,
             "skipped_nonselect": 0, "all_ok": 0, "hits": 0,
             "adjacent_hits": 0,
             "outcomes": {t: {"ok": 0, "error": 0, "internal-error": 0,
                              "timeout": 0, "setup-error": 0}
                          for t in tags}}
    t0 = time.time()
    try:
        for i, seed in enumerate(seeds):
            stats["seeds"] += 1
            q = seed["query"].strip().rstrip(";")
            if not q.upper().lstrip().startswith(_QUERY_PREFIXES):
                stats["skipped_nonselect"] += 1
                continue
            if (has_intrinsic_nondeterminism(q)
                    or is_result_order_sensitive(q)):
                stats["skipped_nondet"] += 1
                continue
            stats["run"] += 1
            per_version: dict[str, dict] = {}
            for tag in tags:
                pg = runners[tag]
                outcomes = pg.setup(seed["setup_sqls"])
                if any(err for _, err in outcomes):
                    rec = {"class": "setup-error",
                           "errors": [e for _, e in outcomes if e]}
                else:
                    res = pg.run(q, timeout_s=args.timeout)
                    eff.count("queries_executed")
                    rec = {"class": _classify(res), "error": res.error,
                           "rows": res.rows, "bag": res.bag()}
                stats["outcomes"][tag][rec["class"]] += 1
                per_version[tag] = rec
            sigs = {t: _signature(r) for t, r in per_version.items()}
            if all(r["class"] == "ok" for r in per_version.values()):
                stats["all_ok"] += 1
            divergent = [(a, b) for a, b in itertools.combinations(tags, 2)
                         if sigs[a] != sigs[b]]
            if not divergent:
                continue
            stats["hits"] += 1
            adjacent = [(a, b) for a, b in zip(tags, tags[1:])
                        if sigs[a] != sigs[b]]
            if adjacent:
                stats["adjacent_hits"] += 1
            hits.append({
                "source": seed["source"],
                "query": q,
                "setup_sqls": list(seed["setup_sqls"]),
                "versions": tags,
                "outcomes": {t: _public(r)
                             for t, r in per_version.items()},
                "divergent_pairs": [[a, b] for a, b in divergent],
                "adjacent_divergent": [[a, b] for a, b in adjacent],
            })
            LOGGER.info("DIVERGENCE %s pairs=%s", seed["source"],
                        [f"{a}~{b}" for a, b in divergent])
            if (i + 1) % 40 == 0:
                LOGGER.info("seed %d/%d hits=%d", i + 1, len(seeds),
                            stats["hits"])
    finally:
        for pg in runners.values():
            try:
                pg.cleanup()
            except Exception:  # noqa: BLE001
                LOGGER.debug("runner cleanup failed", exc_info=True)

    with open(os.path.join(args.out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1, default=str)
    summary = {**stats, "prefixes": prefixes, "versions": versions,
               "timeout": args.timeout,
               "elapsed_s": time.time() - t0,
               "efficiency": eff.snapshot()}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
