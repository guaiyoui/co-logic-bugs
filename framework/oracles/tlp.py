"""Ternary Logic Partitioning oracle.

For a query ``SELECT <cols> FROM <from>`` and a predicate ``p`` the three
partitions ``p``, ``NOT p``, ``p IS NULL`` must together return the same row
multiset as the unfiltered query. Any disagreement is a sound bug signal.

TLP is only applied to plain SELECT-FROM queries: GROUP BY, DISTINCT,
aggregates, HAVING, LIMIT, window functions, or set operations change the
partitioning semantics, so such queries are skipped.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from .db_runner import DuckDBRunner
from .errors import is_value_dependent_error
from .models import KIND_CRASH, KIND_TLP, Candidate, new_candidate_id

LOGGER = logging.getLogger(__name__)

_SKIP_PATTERN = re.compile(
    r"\b(group\s+by|distinct|having|limit|offset|union|intersect|except|"
    r"over|qualify|order\s+by)\b|"
    r"\b(count|sum|avg|min|max|list|string_agg|array_agg|bool_and|bool_or|"
    r"row_number|rank|dense_rank|lag|lead|first_value|last_value)\s*\(",
    re.IGNORECASE,
)


def tlp_applicable(select_from: str, predicate: str | None) -> bool:
    """Return True when TLP partitioning is meaningful for the query."""
    if not predicate or not predicate.strip():
        return False
    return not _SKIP_PATTERN.search(select_from)


def _top_level_where_pos(sql: str) -> int:
    """Offset of a WHERE keyword outside any parenthesized subquery, or -1."""
    depth = 0
    upper = sql.upper()
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and upper.startswith("WHERE", index):
            before_ok = index == 0 or not (upper[index - 1].isalnum() or upper[index - 1] == "_")
            after = index + 5
            after_ok = after >= len(upper) or not (upper[after].isalnum() or upper[after] == "_")
            if before_ok and after_ok:
                return index
        index += 1
    return -1


def inject_predicate(select_from: str, predicate_expr: str) -> str:
    """Attach a predicate to a query, respecting an existing top-level WHERE.

    When a WHERE clause already exists, its condition is parenthesized and
    the new predicate is ANDed — ``A OR B AND (p)`` would otherwise parse as
    ``A OR (B AND p)`` and silently break the partition invariant.
    """
    pos = _top_level_where_pos(select_from)
    if pos >= 0:
        head = select_from[: pos + len("WHERE")]
        cond = select_from[pos + len("WHERE") :].strip()
        return f"{head} ({cond}) AND ({predicate_expr})"
    return f"{select_from} WHERE ({predicate_expr})"


class TLPOracle:
    """Partitioned-where-clause consistency oracle (no LLM involved)."""

    def check(
        self,
        runner: DuckDBRunner,
        select_from: str,
        predicate: str | None,
        schema_sqls: list[str],
        inserts: list[str],
        category: str = "unknown",
        timeout_s: float = 10.0,
    ) -> Candidate | None:
        """Run TLP on one query; return a Candidate on discrepancy or None.

        ``select_from`` is the query text without any WHERE clause (the hunter
        supplies the predicate separately so partitioning is unambiguous).
        """
        if not tlp_applicable(select_from, predicate):
            return None
        assert predicate is not None
        pred = predicate.strip()

        original = runner.run(select_from, timeout_s=timeout_s)
        if original.is_internal_error:
            return Candidate(
                id=new_candidate_id("tlp-crash", select_from, pred),
                kind=KIND_CRASH,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=select_from,
                r1_summary=original.summary(),
                category=category,
                notes="internal error on unpartitioned query (tlp)",
            )
        if not original.ok:
            LOGGER.debug("tlp skip: base query errors: %s", original.error)
            return None

        partition_sqls = [
            inject_predicate(select_from, pred),
            inject_predicate(select_from, f"NOT ({pred})"),
            inject_predicate(select_from, f"({pred}) IS NULL"),
        ]
        combined: Counter = Counter()
        summaries: list[dict[str, Any]] = []
        parts: list[tuple[str, Any]] = []
        for part_sql in partition_sqls:
            part = runner.run(part_sql, timeout_s=timeout_s)
            if part.timed_out:
                # A timed-out partition is inconclusive, not a divergence:
                # slow plans on assert builds are a perf artifact.
                return None
            parts.append((part_sql, part))
            summaries.append(part.summary())
            if part.is_internal_error:
                return Candidate(
                    id=new_candidate_id("tlp-crash", part_sql),
                    kind=KIND_CRASH,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=part_sql,
                    r1_summary=part.summary(),
                    category=category,
                    notes="internal error on tlp partition query",
                )

        n_ok = sum(1 for _, p in parts if p.ok)
        if n_ok < 3:
            # A malformed predicate (bad syntax, unknown column) fails every
            # partition identically — that is a bad test, not an engine bug.
            # Only report when the engine handled the same predicate
            # differently across partitions, or the error classes differ.
            error_kinds = {
                (p.error or "").split(":", 1)[0] for _, p in parts if not p.ok
            }
            if n_ok == 0 and len(error_kinds) <= 1:
                LOGGER.debug("tlp skip: predicate invalid in all partitions")
                return None
            # A value-dependent error firing on only some partitions (e.g.
            # 1/x where x=0 lands in one partition) is an evaluation-order
            # artifact, not a semantics violation.
            if all(
                is_value_dependent_error(p.error) for _, p in parts if not p.ok
            ):
                LOGGER.debug("tlp skip: value-dependent partition errors")
                return None
            fail_sql, fail_res = next((sql, p) for sql, p in parts if not p.ok)
            return Candidate(
                id=new_candidate_id("tlp-err", select_from, pred, fail_sql),
                kind=KIND_TLP,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=select_from,
                q2=fail_sql,
                r1_summary=original.summary(),
                r2_summary={
                    **fail_res.summary(),
                    "predicate": pred,
                    "partition_summaries": summaries,
                },
                category=category,
                notes="tlp partitions disagree: some partitions errored",
            )
        for _, part in parts:
            combined += part.bag()

        if combined != original.bag():
            missing = original.bag() - combined
            extra = combined - original.bag()
            return Candidate(
                id=new_candidate_id("tlp", select_from, pred),
                kind=KIND_TLP,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=select_from,
                q2=" UNION ALL ".join(partition_sqls),
                r1_summary=original.summary(),
                r2_summary={
                    "predicate": pred,
                    "partition_summaries": summaries,
                    "rows_missing_from_partitions": [list(r) for r in list(missing.elements())[:8]],
                    "rows_extra_in_partitions": [list(r) for r in list(extra.elements())[:8]],
                },
                category=category,
                notes="tlp partition union differs from unfiltered query",
            )
        return None
