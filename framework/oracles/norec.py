"""NoREC oracle (Non-optimizing Reference Engine Construction).

For a predicate ``p`` over a query's ``FROM`` relation, the optimized form
``SELECT COUNT(*) FROM t WHERE p`` must equal the unoptimized evaluation
``SELECT SUM(CASE WHEN p THEN 1 ELSE 0 END) FROM t`` — the latter computes
``p`` per row without filter push-down. Any disagreement is a sound bug
signal under three-valued logic (a NULL predicate filters the row in the
WHERE form and yields ELSE 0 in the reference form).

Unlike TLP, NoREC only needs the FROM tail, so the query's select list may
contain aggregates or window functions — they are discarded when the count
queries are built. GROUP BY / HAVING / set operations / LIMIT inside the
FROM tail still change row cardinality and are skipped.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .db_runner import QueryResult
from .errors import is_value_dependent_error
from .models import KIND_CRASH, KIND_NOREC, Candidate, new_candidate_id
from .tlp import inject_predicate

LOGGER = logging.getLogger(__name__)

_SKIP_REST = re.compile(
    r"\b(group\s+by|having|limit|offset|fetch|union|intersect|except|"
    r"qualify|window)\b",
    re.IGNORECASE,
)


def _top_level_from_tail(sql: str) -> str | None:
    """Return ``FROM ...`` text starting at the first depth-0 FROM."""
    upper = sql.upper()
    depth, index, in_str = 0, 0, False
    while index < len(sql):
        char = sql[index]
        if in_str:
            if char == "'":
                in_str = False
            index += 1
            continue
        if char == "'":
            in_str = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and upper.startswith("FROM", index):
            before_ok = index == 0 or not (
                upper[index - 1].isalnum() or upper[index - 1] == "_"
            )
            end = index + 4
            after_ok = end >= len(upper) or not (
                upper[end].isalnum() or upper[end] == "_"
            )
            if before_ok and after_ok:
                return sql[index:].strip()
        index += 1
    return None


def _strip_trailing_order_by(rest: str) -> str:
    """Drop a top-level ORDER BY from a FROM-tail (irrelevant for COUNT)."""
    upper = rest.upper()
    depth, index, in_str = 0, 0, False
    while index < len(rest):
        char = rest[index]
        if in_str:
            if char == "'":
                in_str = False
            index += 1
            continue
        if char == "'":
            in_str = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and upper.startswith("ORDER BY", index):
            before_ok = index == 0 or not (
                upper[index - 1].isalnum() or upper[index - 1] == "_"
            )
            end = index + 8
            after_ok = end >= len(upper) or not (
                upper[end].isalnum() or upper[end] == "_"
            )
            if before_ok and after_ok:
                return rest[:index].strip()
        index += 1
    return rest


def norec_applicable(select_from: str, predicate: str | None) -> bool:
    """True when NoREC applies: predicate given, FROM tail free of grouping."""
    if not predicate or not predicate.strip():
        return False
    rest = _top_level_from_tail(select_from)
    if rest is None:
        return False
    return not _SKIP_REST.search(rest)


class NoRECOracle:
    """Compare optimized COUNT-WHERE against unoptimized SUM-CASE (no LLM)."""

    def check(
        self,
        runner: Any,
        select_from: str,
        predicate: str | None,
        schema_sqls: list[str],
        inserts: list[str],
        category: str = "unknown",
        timeout_s: float = 10.0,
    ) -> Candidate | None:
        """Run NoREC on one query; return a Candidate on disagreement."""
        if not norec_applicable(select_from, predicate):
            return None
        assert predicate is not None
        pred = predicate.strip()

        rest = _strip_trailing_order_by(_top_level_from_tail(select_from) or "")
        if not rest:
            return None

        # Optimized: engine applies the predicate as a filter (push-down etc.).
        optimized_sql = inject_predicate(f"SELECT COUNT(*) {rest}", pred)
        # Reference: predicate evaluated per row inside CASE — no filtering.
        reference_sql = (
            f"SELECT SUM(CASE WHEN ({pred}) THEN 1 ELSE 0 END) {rest}"
        )
        # When the FROM tail already carries a WHERE, the reference must only
        # count rows that pass that filter, matching the optimized form.
        probe = f"SELECT COUNT(*) {rest}"
        probe_res = runner.run(probe, timeout_s=timeout_s)
        if probe_res.is_internal_error:
            return Candidate(
                id=new_candidate_id("norec-crash", probe),
                kind=KIND_CRASH,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=probe,
                r1_summary=probe_res.summary(),
                category=category,
                notes="internal error on norec base count",
            )
        if not probe_res.ok:
            return None

        results: dict[str, QueryResult] = {}
        for name, sql in (("optimized", optimized_sql), ("reference", reference_sql)):
            res = runner.run(sql, timeout_s=timeout_s)
            if res.timed_out:
                # Timeout is inconclusive, not a correctness signal.
                return None
            results[name] = res
            if res.is_internal_error:
                return Candidate(
                    id=new_candidate_id("norec-crash", sql),
                    kind=KIND_CRASH,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=sql,
                    r1_summary=res.summary(),
                    category=category,
                    notes=f"internal error on norec {name} query",
                )

        opt_res, ref_res = results["optimized"], results["reference"]
        if not opt_res.ok or not ref_res.ok:
            # Predicate invalid (bad column, unsupported syntax): not a bug.
            if not opt_res.ok and not ref_res.ok:
                return None
            # Value-dependent errors are evaluation-order artifacts, not bugs.
            bad = opt_res if not opt_res.ok else ref_res
            if is_value_dependent_error(bad.error):
                LOGGER.debug("norec skip: value-dependent error: %s", bad.error)
                return None
            return Candidate(
                id=new_candidate_id("norec-err", optimized_sql, reference_sql),
                kind=KIND_NOREC,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=optimized_sql,
                q2=reference_sql,
                r1_summary=opt_res.summary(),
                r2_summary={
                    **ref_res.summary(),
                    "predicate": pred,
                    "select_from": select_from,
                },
                category=category,
                notes="norec: one side errored while the other succeeded",
            )

        opt_count = opt_res.rows[0][0] if opt_res.rows else None
        ref_count = ref_res.rows[0][0] if ref_res.rows else None
        # SUM returns NULL on an empty relation; COUNT returns 0 — treat as equal.
        if ref_count is None:
            ref_count = 0
        if opt_count is None:
            opt_count = 0
        if float(opt_count) != float(ref_count):
            return Candidate(
                id=new_candidate_id("norec", optimized_sql, reference_sql),
                kind=KIND_NOREC,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=optimized_sql,
                q2=reference_sql,
                r1_summary=opt_res.summary(),
                r2_summary={
                    **ref_res.summary(),
                    "predicate": pred,
                    "select_from": select_from,
                },
                category=category,
                notes=(
                    f"norec count mismatch: WHERE-form={opt_count} "
                    f"vs per-row-form={ref_count}"
                ),
            )
        return None
