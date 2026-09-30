"""Determinism screening for candidate queries.

A query whose result depends on unspecified row order is unusable for every
oracle — TLP, equivalence, and differential — because a legitimate engine may
produce either ordering. This module detects two such patterns:

* top-level ``LIMIT``/``OFFSET``/``FETCH`` without an ``ORDER BY`` whose keys
  uniquely identify rows on the actual data, and
* order-dependent window functions (ROW_NUMBER, RANK, ..., or aggregates with
  ``OVER (... ORDER BY ...)``) whose ORDER BY key is not unique within its
  PARTITION BY group.

On parse failure the module is conservative: the query is flagged
nondeterministic so it gets skipped rather than silently trusted.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .db_runner import QueryResult

LOGGER = logging.getLogger(__name__)

_CLAUSE_KEYWORDS = (
    "WHERE", "GROUP BY", "HAVING", "ORDER BY", "LIMIT", "OFFSET", "FETCH",
    "UNION", "INTERSECT", "EXCEPT", "WINDOW", "QUALIFY",
)

_ORDER_DEPENDENT_WINDOW = re.compile(
    r"\b(row_number|rank|dense_rank|ntile|lag|lead|first_value|last_value|"
    r"nth_value)\s*\(",
    re.IGNORECASE,
)

# Constructs whose result is nondeterministic BY SPECIFICATION — no oracle
# verdict on them is meaningful. Sampling and RNG/timestamp functions can
# coincidentally agree across two runs, so they must be rejected lexically,
# not by execution probing.
_NONDETERMINISTIC_LEXEMES = re.compile(
    r"\b(setseed|shuffle|random|uuid|uuidv[0-9]+|gen_random_uuid|"
    r"newid|clock_timestamp|transaction_timestamp|statement_timestamp|"
    r"timeofday|get_current_timestamp|current_timestamp|localtimestamp|"
    r"current_time|current_date|localtime|now|"
    # Per-transaction / per-session volatile sysfuncs: a fresh call yields a
    # fresh value, so cross-run bag equality can never hold.
    r"txid_current|pg_current_xact_id|pg_current_xact_id_if_assigned|"
    r"pg_current_snapshot|pg_current_wal_lsn|pg_current_wal_insert_lsn|"
    r"pg_current_wal_flush_lsn|pg_last_wal_receive_lsn|"
    r"pg_last_wal_replay_lsn|pg_last_committed_xact|pg_backend_pid|"
    r"pg_postmaster_start_time|pg_conf_load_time|"
    r"pg_notification_queue_usage)\b",
    re.IGNORECASE,
)

_SAMPLING = re.compile(r"\b(tablesample|using\s+sample)\b", re.IGNORECASE)
_REPEATABLE = re.compile(r"\brepeatable\s*\(", re.IGNORECASE)

# Order-aggregates: list()/array_agg()/string_agg() build an ordered
# collection whose element order is unspecified unless an ORDER BY appears
# inside the call — different plans then produce different *values* (not
# just row order), which every oracle reports as a discrepancy.
_ORDER_AGG = re.compile(
    r"\b(list|listagg|array_agg|list_agg|string_agg|"
    r"json_objectagg|json_object_agg|json_arrayagg|json_array_agg|"
    r"jsonb_object_agg|jsonb_arrayagg|jsonb_objectagg|"
    r"json_agg|jsonb_agg)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_AGG_INNER_ORDER = re.compile(r"\border\s+by\b", re.IGNORECASE)


def has_intrinsic_nondeterminism(sql: str) -> bool:
    """Lexical check for by-spec nondeterministic constructs."""
    if _SAMPLING.search(sql) and not _REPEATABLE.search(sql):
        # Unseeded sampling returns a different bag every run.
        return True
    if _NONDETERMINISTIC_LEXEMES.search(sql):
        return True
    for match in _ORDER_AGG.finditer(sql):
        if not _AGG_INNER_ORDER.search(match.group(2)):
            return True
    return False


def _top_level_positions(sql: str) -> list[tuple[int, str]]:
    """Positions of clause keywords occurring at paren depth 0.

    Returns ``[(offset, "ORDER BY"), ...]`` in source order. Word-boundary
    aware; string literals are skipped crudely (single-quote runs).
    """
    upper = sql.upper()
    hits: list[tuple[int, str]] = []
    index, depth = 0, 0
    in_str = False
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
        elif depth == 0:
            for kw in _CLAUSE_KEYWORDS:
                if upper.startswith(kw, index):
                    before_ok = index == 0 or not (
                        upper[index - 1].isalnum() or upper[index - 1] == "_"
                    )
                    end = index + len(kw)
                    after_ok = end >= len(upper) or not (
                        upper[end].isalnum() or upper[end] == "_"
                    )
                    if before_ok and after_ok:
                        hits.append((index, kw))
                        index = end - 1
                        break
        index += 1
    return hits


def _clause_text(sql: str, positions: list[tuple[int, str]], keyword: str) -> str | None:
    """Text between a top-level keyword and the next top-level clause."""
    for i, (pos, kw) in enumerate(positions):
        if kw == keyword:
            end = positions[i + 1][0] if i + 1 < len(positions) else len(sql)
            return sql[pos + len(keyword) : end].strip()
    return None


def _split_top_level_commas(text: str) -> list[str]:
    items, depth, current, in_str = [], 0, "", False
    for char in text:
        if char == "'":
            in_str = not in_str
        if in_str:
            current += char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current.strip())
    return items


_ORDER_SUFFIX = re.compile(
    r"\s+(asc|desc|nulls\s+(first|last))+\s*$", re.IGNORECASE
)


def _order_key_exprs(order_by_text: str) -> list[str]:
    """Strip direction/collation suffixes from ORDER BY items."""
    keys = []
    for item in _split_top_level_commas(order_by_text):
        item = _ORDER_SUFFIX.sub("", item.strip())
        if item:
            keys.append(item)
    return keys


def _group_cardinality(
    runner: Any, from_where: str, keys: list[str]
) -> int | None:
    """Max multiplicity of the key tuple over the given relation; None on error."""
    if not keys:
        return None
    probe = (
        "SELECT MAX(c) FROM (SELECT COUNT(*) AS c "
        f"{from_where} GROUP BY {', '.join(keys)})"
    )
    result = runner.run(probe, timeout_s=10.0)
    if not result.ok or not result.rows or result.rows[0][0] is None:
        return None
    return int(result.rows[0][0])


def is_result_order_sensitive(sql: str) -> bool:
    """Syntactic check: top-level LIMIT/OFFSET/FETCH without top-level ORDER BY."""
    positions = _top_level_positions(sql)
    keywords = {kw for _, kw in positions}
    has_limit = bool(keywords & {"LIMIT", "OFFSET", "FETCH"})
    return has_limit and "ORDER BY" not in keywords


def _top_level_from_pos(sql: str) -> int | None:
    """Offset of the first ``FROM`` occurring at paren depth 0."""
    upper = sql.upper()
    depth, index, in_str = 0, 0, False
    while index < len(sql):
        char = sql[index]
        if in_str:
            if char == "'":
                in_str = False
        elif char == "'":
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
                return index
        index += 1
    return None


def _from_where_text(sql: str, positions: list[tuple[int, str]]) -> str | None:
    """Extract ``FROM ...`` covering any WHERE (the grouping relation)."""
    start = _top_level_from_pos(sql)
    if start is None:
        return None
    end = len(sql)
    for pos, kw in positions:
        if pos > start and kw in {"ORDER BY", "LIMIT", "OFFSET", "FETCH", "WINDOW"}:
            end = pos
            break
    return sql[start:end].strip()


def _over_clauses(sql: str) -> list[tuple[str, str]]:
    """``(function_name, over_body)`` for every ``FUNC(...) OVER (...)``."""
    clauses = []
    for match in re.finditer(r"\bover\s*\(", sql, re.IGNORECASE):
        func_match = re.search(
            r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\([^)]*\)\s*$", sql[: match.start()]
        )
        func = func_match.group(1).lower() if func_match else ""
        depth, start = 1, match.end()
        index = start
        while index < len(sql) and depth:
            if sql[index] == "(":
                depth += 1
            elif sql[index] == ")":
                depth -= 1
            index += 1
        if depth == 0:
            clauses.append((func, sql[start : index - 1]))
    return clauses


def has_nondeterministic_tiebreak(
    sql: str, runner: Any, schema_sqls: list[str]
) -> bool:
    """Data-dependent check: LIMIT or order-dependent windows on non-unique keys.

    Returns True when the query's output can vary across executions/plans.
    Parse failures conservatively return True (skip the query).
    """
    runner.setup(schema_sqls)
    positions = _top_level_positions(sql)
    keywords = {kw for _, kw in positions}

    # LIMIT/OFFSET/FETCH with an ORDER BY: keys must uniquely identify rows.
    if keywords & {"LIMIT", "OFFSET", "FETCH"}:
        order_text = _clause_text(sql, positions, "ORDER BY")
        if order_text is None:
            return True
        keys = _order_key_exprs(order_text)
        from_where = _from_where_text(sql, positions)
        if not keys or from_where is None:
            return True
        card = _group_cardinality(runner, from_where, keys)
        if card is None or card > 1:
            return True

    # Window functions: ORDER BY key must be unique inside each PARTITION.
    over_clauses = _over_clauses(sql)
    if not over_clauses:
        return False
    from_where = _from_where_text(sql, positions)
    if from_where is None:
        return True
    for func, clause in over_clauses:
        part_match = re.search(
            r"\bpartition\s+by\b(.*?)(?=\border\s+by\b|$)",
            clause,
            re.IGNORECASE | re.DOTALL,
        )
        order_match = re.search(
            r"\border\s+by\b(.*)$", clause, re.IGNORECASE | re.DOTALL
        )
        pcols = (
            _split_top_level_commas(part_match.group(1)) if part_match else []
        )
        has_order = order_match is not None
        order_dependent = bool(_ORDER_DEPENDENT_WINDOW.search(func + "("))
        if not has_order:
            # Ranking/navigation functions without ORDER BY have undefined order.
            if order_dependent:
                return True
            continue
        ocols = _order_key_exprs(order_match.group(1))
        keys = [k for k in (pcols + ocols) if k]
        if not keys:
            return True
        card = _group_cardinality(runner, from_where, keys)
        if card is None:
            LOGGER.debug("tiebreak probe failed; treating query as nondeterministic")
            return True
        if card > 1:
            return True
    return False


def is_usable_for_oracles(
    sql: str, runner: Any, schema_sqls: list[str]
) -> bool:
    """True when the query result is deterministic enough for all oracles."""
    if has_intrinsic_nondeterminism(sql):
        return False
    if is_result_order_sensitive(sql):
        return False
    return not has_nondeterministic_tiebreak(sql, runner, schema_sqls)
