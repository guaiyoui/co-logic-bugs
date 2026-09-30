"""Fetch upstream regression tests into the local seed corpus.

Downloads a bounded set of DuckDB ``.test`` files and PostgreSQL regression
``.sql`` files via the GitHub API (no full clone), parses them into Seed
records, and caches the corpus at ``seeds/corpus.json``.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import urllib.request
from pathlib import Path

import re

from seeds.parse import _split_statements, _SKIP_SQL, parse_seed_file
from seeds.store import SeedCorpus

LOGGER = logging.getLogger("fetch_seeds")
PROJECT_DIR = Path(__file__).resolve().parent.parent

DUCKDB_DIRS = [
    "test/issues/general",
    "test/sql/subquery/scalar",
    "test/sql/subquery/exists",
    "test/sql/aggregate",
    "test/sql/window",
    "test/sql/join/inner",
    "test/sql/join/outer",
    "test/sql/types/decimal",
    "test/sql/filter",
    "test/sql/order",
]

PG_FILES = [
    "join.sql",
    "subselect.sql",
    "aggregates.sql",
    "case.sql",
    "window.sql",
    "with.sql",
    "union.sql",
    "select.sql",
    "arrays.sql",
    "select_distinct.sql",
    "select_having.sql",
    "boolean.sql",
]

MAX_PER_DIR = 20
MAX_PER_FILE = 30


def _github_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "coevo-seed-fetch"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_duckdb(corpus: SeedCorpus) -> None:
    for d in DUCKDB_DIRS:
        api = f"https://api.github.com/repos/duckdb/duckdb/contents/{d}"
        try:
            entries = json.loads(_github_get(api))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("listing %s failed: %s", d, exc)
            continue
        names = [
            e["name"]
            for e in entries
            if isinstance(e, dict) and e.get("name", "").endswith(".test")
        ][:MAX_PER_DIR]
        for name in names:
            raw = (
                f"https://raw.githubusercontent.com/duckdb/duckdb/main/{d}/{name}"
            )
            try:
                text = _github_get(raw).decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("fetch %s failed: %s", raw, exc)
                continue
            seeds = parse_seed_file(text, f"duckdb:{d}/{name}", "duckdb")
            for seed in seeds[:MAX_PER_FILE]:
                corpus.add(seed)
            LOGGER.info("%s/%s -> %d seeds", d, name, len(seeds))


def _pg_prelude(base: str) -> list[str]:
    """Shared DDL from test_setup.sql — PG regress files reference these
    tables without redefining them."""
    try:
        text = _github_get(f"{base}/test_setup.sql").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("test_setup.sql fetch failed: %s", exc)
        return []
    creates, inserts, views = [], [], []
    for stmt in _split_statements(text):
        if _SKIP_SQL.match(stmt) or re.match(
            r"^\s*create\s+tablespace", stmt, re.I
        ):
            continue
        flat = re.sub(r"\s+", " ", stmt).strip()
        if re.match(r"^\s*create\s+table", stmt, re.I):
            creates.append(flat)
        elif re.match(r"^\s*insert", stmt, re.I):
            inserts.append(flat)
        elif re.match(r"^\s*create\s+(or\s+replace\s+)?(view|index)", stmt, re.I):
            views.append(flat)
    return creates + inserts + views


def fetch_postgres(corpus: SeedCorpus) -> None:
    base = (
        "https://raw.githubusercontent.com/postgres/postgres/master/"
        "src/test/regress/sql"
    )
    prelude = _pg_prelude(base)
    LOGGER.info("pg prelude: %d setup statements", len(prelude))
    for name in PG_FILES:
        try:
            text = _github_get(f"{base}/{name}").decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("fetch %s failed: %s", name, exc)
            continue
        seeds = parse_seed_file(text, f"postgres:regress/{name}", "postgres")
        for seed in seeds[:MAX_PER_FILE]:
            seen = {" ".join(s.split()).lower() for s in seed.setup_sqls}
            extra = [p for p in prelude if " ".join(p.split()).lower() not in seen]
            seed.setup_sqls = extra + seed.setup_sqls
            corpus.add(seed)
        LOGGER.info("pg regress/%s -> %d seeds", name, len(seeds))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", type=Path, default=PROJECT_DIR / "seeds" / "corpus.json"
    )
    parser.add_argument("--engines", nargs="+", default=["duckdb", "postgres"])
    args = parser.parse_args()

    corpus = SeedCorpus.load(args.out)
    before = len(corpus)
    if "duckdb" in args.engines:
        fetch_duckdb(corpus)
    if "postgres" in args.engines:
        fetch_postgres(corpus)
    corpus.save(args.out)
    print(f"corpus: {before} -> {len(corpus)} seeds at {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
