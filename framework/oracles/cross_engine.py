"""Cross-engine differential oracle.

Runs the same schema + query on the *target* engine and on a *reference*
engine (e.g. PostgreSQL target checked against a local DuckDB reference).
Any disagreement is a ``cross_engine`` candidate — dialect differences make
this signal inherently weaker than intra-engine oracles, so the triager
decides which engine is correct before the candidate can become a bug.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .models import KIND_CRASH, KIND_CROSS_ENGINE, Candidate, new_candidate_id
from .normalize import loose_bag

LOGGER = logging.getLogger(__name__)


class CrossEngineOracle:
    """Compare query results between the target runner and a reference runner."""

    def __init__(
        self,
        reference_factory: Callable[[], Any],
        reference_name: str = "duckdb",
        timeout_s: float = 10.0,
    ):
        self.reference_factory = reference_factory
        self.reference_name = reference_name
        self.timeout_s = timeout_s

    def check(
        self,
        target_runner: Any,
        query: str,
        schema_sqls: list[str],
        inserts: list[str],
        category: str = "unknown",
        rewrite_kind: str = "",
    ) -> Candidate | None:
        """Run ``query`` on both engines; return a Candidate on divergence."""
        target = target_runner.run(query, timeout_s=self.timeout_s)
        if target.is_internal_error:
            return Candidate(
                id=new_candidate_id("xeng-crash", query),
                kind=KIND_CRASH,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=query,
                r1_summary={**target.summary(), "engine": "target"},
                category=category,
                rewrite_kind=rewrite_kind,
                notes="internal error on target engine",
            )

        reference = self.reference_factory()
        try:
            setup = list(schema_sqls) + list(inserts)
            ref_setup = reference.setup(setup)
            if any(err for _, err in ref_setup):
                # Reference cannot even build the schema (dialect mismatch):
                # no signal either way.
                LOGGER.debug("cross-engine: reference setup failed, skipped")
                return None
            ref = reference.run(query, timeout_s=self.timeout_s)
        finally:
            reference.close()

        if ref.is_internal_error:
            # Reference crashed — evidence about the reference, not target.
            return None
        if target.timed_out or ref.timed_out:
            # A timeout on either side is inconclusive, not a divergence.
            return None

        target_ok, ref_ok = target.ok, ref.ok
        if target_ok != ref_ok or (
            target_ok and ref_ok and loose_bag(target.rows) != loose_bag(ref.rows)
        ):
            return Candidate(
                id=new_candidate_id("xeng", query),
                kind=KIND_CROSS_ENGINE,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=query,
                r1_summary={**target.summary(), "engine": "target"},
                r2_summary={
                    "ok": ref_ok,
                    "error": ref.error,
                    "row_count": len(ref.rows),
                    "sample_rows": ref.rows[:8],
                    "engine": self.reference_name,
                },
                category=category,
                rewrite_kind=rewrite_kind,
                notes="cross_engine_divergence: engines disagree",
            )
        return None
