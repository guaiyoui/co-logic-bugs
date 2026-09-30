"""Cross-version differential oracle.

Runs the same schema + queries on the local DuckDB and on an older DuckDB
installed in a separate venv (``oracles/_remote_exec.py`` under that venv's
interpreter). Any divergence is a ``differential`` candidate tagged as a
potential regression or fix — the older version is not automatically wrong.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from .db_runner import DuckDBRunner
from util.paths import OLD_VENV
from .models import KIND_CRASH, KIND_DIFFERENTIAL, Candidate, new_candidate_id
from .normalize import loose_bag

LOGGER = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OLD_VENV = OLD_VENV


class DifferentialOracle:
    """Compare query results across two DuckDB versions."""

    def __init__(
        self,
        old_python: Path | str | None = None,
        timeout_s: float = 180.0,
    ):
        if old_python is None:
            old_python = DEFAULT_OLD_VENV / "bin" / "python"
        self.old_python = Path(old_python)
        self.timeout_s = timeout_s
        self.old_version: str | None = None

    @property
    def available(self) -> bool:
        return self.old_python.exists()

    def run_remote(
        self, schema_sqls: list[str], queries: list[str]
    ) -> dict[str, Any] | None:
        """Execute setup + queries on the old interpreter via subprocess."""
        if not self.available:
            LOGGER.warning("old duckdb interpreter missing: %s", self.old_python)
            return None
        payload = {"schema_sqls": schema_sqls, "queries": queries}
        try:
            proc = subprocess.run(
                [str(self.old_python), "-m", "oracles._remote_exec"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                cwd=str(PROJECT_DIR),
                env={
                    "PYTHONPATH": str(PROJECT_DIR),
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(Path.home()),
                },
            )
        except (subprocess.SubprocessError, OSError) as exc:
            LOGGER.error("remote duckdb execution failed: %s", exc)
            return None
        if proc.returncode != 0:
            LOGGER.error("remote duckdb exited %s: %s", proc.returncode, proc.stderr[-500:])
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            LOGGER.error("remote duckdb returned non-JSON output: %s", proc.stdout[-300:])
            return None

    def check(
        self,
        schema_sqls: list[str],
        queries: list[str],
        metas: list[dict[str, Any]] | None = None,
        local_runner: DuckDBRunner | None = None,
        inserts: list[str] | None = None,
    ) -> list[Candidate]:
        """Compare each query across versions; return divergence candidates.

        ``metas`` carries per-query bookkeeping (category, rewrite_kind,
        original select_from) supplied by the hunter.
        """
        if not queries:
            return []
        stored_inserts = list(inserts or [])
        setup_sqls = list(schema_sqls) + stored_inserts
        remote = self.run_remote(setup_sqls, queries)
        if remote is None:
            return []
        self.old_version = remote.get("duckdb_version")

        runner = local_runner or DuckDBRunner(version_tag="local")
        runner.setup(setup_sqls)
        candidates: list[Candidate] = []
        remote_results = remote.get("results", [])
        for index, query in enumerate(queries):
            if index >= len(remote_results):
                break
            meta = metas[index] if metas and index < len(metas) else {}
            local = runner.run(query)
            if local.timed_out:
                continue
            remote_record = remote_results[index]
            remote_rows = remote_record.get("rows") or []
            remote_error = remote_record.get("error")
            remote_internal = bool(remote_record.get("is_internal_error"))

            if local.is_internal_error or remote_internal:
                candidates.append(
                    Candidate(
                        id=new_candidate_id("diff-crash", query, str(index)),
                        kind=KIND_CRASH,
                        schema_sqls=schema_sqls,
                        inserts=stored_inserts,
                        q1=query,
                        r1_summary=local.summary(),
                        r2_summary={
                            "ok": remote_error is None,
                            "error": remote_error,
                            "row_count": len(remote_rows),
                            "remote_version": self.old_version,
                        },
                        category=meta.get("category", "unknown"),
                        rewrite_kind=meta.get("rewrite_kind", ""),
                        inspired_by=meta.get("inspired_by", ""),
                        notes="internal error in cross-version comparison",
                    )
                )
                continue

            local_ok, remote_ok = local.ok, remote_error is None
            # Loose bag compare: numerics unify across int/float/decimal and a
            # DATE equals the same TIMESTAMP at midnight, so pure return-type
            # changes (e.g. DATE_TRUNC DATE->TIMESTAMP) are not divergences.
            bags_differ = local_ok and remote_ok and loose_bag(
                local.rows
            ) != loose_bag(remote_rows)
            if local_ok != remote_ok or bags_differ:
                candidates.append(
                    Candidate(
                        id=new_candidate_id("diff", query, str(index)),
                        kind=KIND_DIFFERENTIAL,
                        schema_sqls=schema_sqls,
                        inserts=stored_inserts,
                        q1=query,
                        q2=meta.get("paired_query"),
                        r1_summary={
                            **local.summary(),
                            "version": f"local:{runner.engine_version}",
                        },
                        r2_summary={
                            "ok": remote_ok,
                            "error": remote_error,
                            "row_count": len(remote_rows),
                            "sample_rows": remote_rows[:8],
                            "version": f"old:{self.old_version}",
                        },
                        category=meta.get("category", "unknown"),
                        rewrite_kind=meta.get("rewrite_kind", ""),
                        inspired_by=meta.get("inspired_by", ""),
                        notes="regression_or_fix: versions disagree",
                    )
                )
        return candidates
