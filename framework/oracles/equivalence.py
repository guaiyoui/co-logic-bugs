"""Equivalence oracle: execute a query pair and compare result bags."""

from __future__ import annotations

import logging
from typing import Any

from .db_runner import DuckDBRunner, bags_equal
from .errors import is_value_dependent_error
from .models import (
    KIND_CRASH,
    KIND_EQUIV,
    KIND_ERROR_MISMATCH,
    Candidate,
    new_candidate_id,
)

LOGGER = logging.getLogger(__name__)


class EquivalenceOracle:
    """Deterministic oracle for LLM-proposed equivalent rewrites (no LLM)."""

    def check(
        self,
        runner: DuckDBRunner,
        q1: str,
        q2: str,
        schema_sqls: list[str],
        inserts: list[str],
        category: str = "unknown",
        rewrite_kind: str = "",
        timeout_s: float = 10.0,
    ) -> Candidate | None:
        """Compare q1 vs q2 on the runner's database; None when consistent."""
        r1 = runner.run(q1, timeout_s=timeout_s)
        r2 = runner.run(q2, timeout_s=timeout_s)

        if r1.is_internal_error or r2.is_internal_error:
            return Candidate(
                id=new_candidate_id("crash", q1, q2),
                kind=KIND_CRASH,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=q1,
                q2=q2,
                r1_summary=r1.summary(),
                r2_summary=r2.summary(),
                category=category,
                rewrite_kind=rewrite_kind,
                notes="internal error raised during equivalence check",
            )
        if not r1.ok and not r2.ok:
            return None  # both fail: no signal
        if r1.timed_out or r2.timed_out:
            return None
        if r1.ok != r2.ok:
            # One side errors, the other succeeds. A value-dependent error is
            # an evaluation-order artifact, not evidence of a bug.
            bad = r1 if not r1.ok else r2
            if is_value_dependent_error(bad.error):
                LOGGER.debug(
                    "equiv skip: value-dependent error: %s", bad.error
                )
                return None
            return Candidate(
                id=new_candidate_id("errmm", q1, q2),
                kind=KIND_ERROR_MISMATCH,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=q1,
                q2=q2,
                r1_summary=r1.summary(),
                r2_summary=r2.summary(),
                category=category,
                rewrite_kind=rewrite_kind,
                notes="one side errored while the other succeeded",
            )
        if not bags_equal(r1, r2):
            return Candidate(
                id=new_candidate_id("equiv", q1, q2),
                kind=KIND_EQUIV,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=q1,
                q2=q2,
                r1_summary=r1.summary(),
                r2_summary=r2.summary(),
                category=category,
                rewrite_kind=rewrite_kind,
                notes="result multisets differ",
            )
        return None
