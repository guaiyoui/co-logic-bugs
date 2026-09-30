"""Machine-local resource paths shared by scripts, oracles and tests.

Everything in this repo is portable except a handful of *external*
resources that live outside the checkout:

  - the PostgreSQL assert/ASan build prefixes (``pgbld/``)
  - the old-DuckDB virtualenv used by the differential oracle
  - the DuckDB source checkout used by repair_bench localization
  - the sibling Argus checkout used by the argus_* baselines
  - the LLM key file used by the repair_bench drivers

Each constant reads an environment variable first and falls back to
the original developer layout, so a fresh clone keeps working by
setting variables instead of editing source::

    export COEVO_PGBLD=/path/to/pgbld
    export COEVO_OLD_VENV=/path/to/venv_duckdb_old
    export COEVO_DUCKDB_SRC=/path/to/duckdb_src
    export COEVO_ARGUS_DIR=/path/to/Argus
    export COEVO_KEYS_YML=/path/to/api-keys.yml

``PostgresRunner`` separately honors ``COEVO_PG_PREFIX`` (a full install
prefix, not a root) and ``COEVO_PG_EXTRA_OPTS``.
"""
from __future__ import annotations

import os
from pathlib import Path

PGBLD = os.environ.get("COEVO_PGBLD", "/home/user/work/db_safety/pgbld")
OLD_VENV = Path(os.environ.get(
    "COEVO_OLD_VENV", "/home/user/work/db_safety/venv_duckdb_old"))
DUCKDB_SRC = Path(os.environ.get(
    "COEVO_DUCKDB_SRC", "/home/user/work/db_safety/duckdb_src"))
ARGUS_DIR = Path(os.environ.get(
    "COEVO_ARGUS_DIR", "/home/user/work/db_safety/Argus"))
KEYS_YML = Path(os.environ.get(
    "COEVO_KEYS_YML", "/home/user/work/db_safety/Argus/api-keys.yml"))


def pg_build_prefix(build: str) -> str:
    """Resolve a PG build name (``pg186_assert``) or an absolute install
    prefix to the ``pg_prefix`` value expected by PostgresRunner."""
    return build if os.path.isabs(build) else os.path.join(PGBLD, build)
