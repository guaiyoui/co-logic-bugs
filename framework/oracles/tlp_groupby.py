"""Ternary Logic Partitioning oracle for GROUP BY / DISTINCT queries.

For ``Q = SELECT k1[,k2] FROM <from> GROUP BY k1[,k2]`` and a predicate
``p`` the three partitions ``WHERE p`` / ``WHERE NOT p`` / ``WHERE p IS
NULL`` must produce — under **set** union — the same group-key set as the
unfiltered query::

    keys(Q) == keys(Q|p) ∪ keys(Q|¬p) ∪ keys(Q|p IS NULL)

Why a set and not a bag: one logical group's *rows* can split across
partitions (some rows satisfy ``p``, others ``NOT p``), so the same group
key legitimately appears in several partitions.  A ``Counter``/bag
comparison would be a systematic false positive — this is exactly why
sqlancer's ``SQLite3TLPGroupByOracle`` compares via
``getCombinedResultSetNoDuplicates`` (``UNION``, not ``UNION ALL``).

Two deliberate restrictions keep the oracle sound:

* The select list contains only the GROUP BY key expressions.  Aggregate
  values legitimately differ when a group splits across partitions, so
  they are excluded by construction.
* ``-0.0``/``0.0`` (and nested) cells are canonicalized before keying:
  values that are ``=`` but not bit-identical may legitimately surface
  with a different representative row in different partitions.

``SELECT DISTINCT c1[,c2] FROM <from>`` obeys the same identity on the
projected column set, again with set semantics.

Free rider — PINOLO-lite: rows satisfying ``p AND q`` are a subset of the
rows satisfying ``p``, so the group-key set of ``Q|p∧q`` must be a subset
of ``Q|p``'s (the conjunction-dropped-qual signature, upstream #19560
class).  One extra query per case reuses the already-computed ``WHERE p``
partition.

Error handling mirrors ``oracles.tlp.TLPOracle``: an internal error is a
crash hit, a timed-out partition is inconclusive, value-dependent errors
(division-by-zero et al. — evaluation order is unspecified) are filtered,
and a predicate that fails every partition identically is a bad test, not
a bug.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .errors import is_value_dependent_error
from .models import (
    KIND_CRASH,
    KIND_PINOLO,
    KIND_TLP_DISTINCT,
    KIND_TLP_GROUPBY,
    Candidate,
    new_candidate_id,
)

LOGGER = logging.getLogger(__name__)

# check() status codes
CLEAN = "clean"
HIT = "hit"
CRASH = "crash"
SKIP_ORIG_ERR = "skip_orig_err"      # unpartitioned query errored
SKIP_PRED_ERR = "skip_pred_err"      # predicate invalid / value-dependent
INCONCLUSIVE = "inconclusive_timeout"


def _canon_value(value: Any) -> Any:
    """Collapse representative-arbitrary cells into a canonical form.

    ``-0.0`` and ``0.0`` compare equal under ``GROUP BY``/``DISTINCT`` but
    JSON-encode differently; which representative row a group emits is
    unspecified, so both spellings map to one key.  (sqlancer does the
    same in ``canonicalizeResultValue``.)
    """
    if isinstance(value, float) and value == 0.0:
        return 0.0
    if isinstance(value, list):
        return [_canon_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _canon_value(v) for k, v in value.items()}
    return value


def row_key(row: list[Any]) -> str:
    """Stable string key for one normalized row (same shape as bag())."""
    return json.dumps(
        [_canon_value(v) for v in row], sort_keys=True, default=str
    )


def rowset(rows: list[list[Any]]) -> frozenset:
    """Dedup set of row keys — the GROUP BY/DISTINCT result signature."""
    return frozenset(row_key(r) for r in rows)


def build_query(
    select_list: str,
    from_tail: str,
    group_by: str | None,
    where: str | None = None,
    distinct: bool = False,
    base_where: str | None = None,
) -> str:
    """Assemble ``SELECT [DISTINCT] <list> <from> [WHERE ..] [GROUP BY]``.

    ``base_where`` is a pre-existing filter (e.g. lifted from the seed's
    own WHERE): the TLP partitions then split *within* that filtered
    relation — ``(base ∧ p) ∪ (base ∧ ¬p) ∪ (base ∧ p IS NULL)`` still
    reconstructs ``base`` exactly, so the oracle stays sound.
    """
    sql = f"SELECT {'DISTINCT ' if distinct else ''}{select_list} {from_tail}"
    conds = []
    if base_where:
        conds.append(f"({base_where})")
    if where:
        conds.append(f"({where})")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    if group_by:
        sql += f" GROUP BY {group_by}"
    return sql


class TLPGroupByOracle:
    """Set-union TLP oracle for grouped/distinct queries (no LLM).

    ``check`` returns ``(status, candidates)`` so the driver can keep
    inconclusive/skip accounting separate from true divergences.
    """

    def check(
        self,
        runner,
        *,
        select_list: str,
        from_tail: str,
        predicate: str,
        group_by: str | None = None,
        conjoin: str | None = None,
        base_where: str | None = None,
        schema_sqls: list[str] | tuple = (),
        inserts: list[str] | tuple = (),
        category: str = "unknown",
        timeout_s: float = 10.0,
    ) -> tuple[str, list[Candidate]]:
        distinct = group_by is None
        kind = KIND_TLP_DISTINCT if distinct else KIND_TLP_GROUPBY
        pred = predicate.strip()
        schema_sqls = list(schema_sqls)
        inserts = list(inserts)

        original_sql = build_query(
            select_list, from_tail, group_by, distinct=distinct,
            base_where=base_where,
        )
        original = runner.run(original_sql, timeout_s=timeout_s)
        if original.is_internal_error:
            return CRASH, [
                Candidate(
                    id=new_candidate_id("tlpg-crash", original_sql),
                    kind=KIND_CRASH,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=original_sql,
                    r1_summary=original.summary(),
                    category=category,
                    notes="internal error on unpartitioned group-by query",
                )
            ]
        if original.timed_out or not original.ok:
            LOGGER.debug("tlpg skip: base query failed: %s", original.error)
            return (
                INCONCLUSIVE if original.timed_out else SKIP_ORIG_ERR
            ), []

        where_parts = [
            f"({pred})",
            f"NOT ({pred})",
            f"({pred}) IS NULL",
        ]
        parts: list[tuple[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        for where in where_parts:
            part_sql = build_query(
                select_list, from_tail, group_by, where=where,
                distinct=distinct, base_where=base_where,
            )
            part = runner.run(part_sql, timeout_s=timeout_s)
            if part.timed_out:
                # A timed-out partition is inconclusive, not a divergence:
                # slow plans on assert builds are a perf artifact.
                return INCONCLUSIVE, []
            parts.append((part_sql, part))
            summaries.append(part.summary())
            if part.is_internal_error:
                return CRASH, [
                    Candidate(
                        id=new_candidate_id("tlpg-crash", part_sql),
                        kind=KIND_CRASH,
                        schema_sqls=schema_sqls,
                        inserts=inserts,
                        q1=part_sql,
                        r1_summary=part.summary(),
                        category=category,
                        notes="internal error on tlp-groupby partition query",
                    )
                ]

        n_ok = sum(1 for _, p in parts if p.ok)
        if n_ok < 3:
            # Same policy as TLPOracle: an identically-failing predicate is
            # a bad test; all-value-dependent failures are eval-order
            # artifacts.  Mixed outcomes are a semantics violation.
            error_kinds = {
                (p.error or "").split(":", 1)[0] for _, p in parts if not p.ok
            }
            if n_ok == 0 and len(error_kinds) <= 1:
                return SKIP_PRED_ERR, []
            if all(
                is_value_dependent_error(p.error) for _, p in parts if not p.ok
            ):
                return SKIP_PRED_ERR, []
            fail_sql, fail_res = next((s, p) for s, p in parts if not p.ok)
            return HIT, [
                Candidate(
                    id=new_candidate_id("tlpg-err", original_sql, fail_sql),
                    kind=kind,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=original_sql,
                    q2=fail_sql,
                    r1_summary=original.summary(),
                    r2_summary={
                        **fail_res.summary(),
                        "predicate": pred,
                        "partition_summaries": summaries,
                    },
                    category=category,
                    notes="tlp-groupby partitions disagree: "
                    "some partitions errored",
                )
            ]

        union: set[str] = set()
        for _, part in parts:
            union |= rowset(part.rows)
        orig_set = rowset(original.rows)

        hits: list[Candidate] = []
        if union != orig_set:
            missing = orig_set - union
            extra = union - orig_set
            hits.append(
                Candidate(
                    id=new_candidate_id("tlpg", original_sql, pred),
                    kind=kind,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=original_sql,
                    q2=" UNION ".join(sql for sql, _ in parts),
                    r1_summary=original.summary(),
                    r2_summary={
                        "predicate": pred,
                        "partition_summaries": summaries,
                        "groups_missing_from_partitions": sorted(missing)[:8],
                        "groups_extra_in_partitions": sorted(extra)[:8],
                    },
                    category=category,
                    notes="tlp-groupby: partition key-set union differs "
                    "from unfiltered query",
                )
            )

        # ---- PINOLO-lite: WHERE (p) AND (q) ⊆ WHERE (p) ------------------
        p_res = parts[0][1]
        if conjoin and p_res.ok:
            and_sql = build_query(
                select_list, from_tail, group_by,
                where=f"({pred}) AND ({conjoin})",
                distinct=distinct, base_where=base_where,
            )
            and_res = runner.run(and_sql, timeout_s=timeout_s)
            if and_res.is_internal_error:
                hits.append(
                    Candidate(
                        id=new_candidate_id("pinolo-crash", and_sql),
                        kind=KIND_CRASH,
                        schema_sqls=schema_sqls,
                        inserts=inserts,
                        q1=and_sql,
                        r1_summary=and_res.summary(),
                        category=category,
                        notes="internal error on conjoined predicate query",
                    )
                )
            elif and_res.timed_out or not and_res.ok:
                # An error/timeout on the AND-form alone is *usually* a
                # legal evaluation-order artifact (q may be evaluated on
                # rows that fail p); not reported as a divergence.
                LOGGER.debug(
                    "pinolo skip: conjoined query failed: %s", and_res.error
                )
            elif not rowset(and_res.rows) <= rowset(p_res.rows):
                extra = rowset(and_res.rows) - rowset(p_res.rows)
                hits.append(
                    Candidate(
                        id=new_candidate_id("pinolo", and_sql, pred, conjoin),
                        kind=KIND_PINOLO,
                        schema_sqls=schema_sqls,
                        inserts=inserts,
                        q1=build_query(
                            select_list, from_tail, group_by,
                            where=f"({pred})", distinct=distinct,
                            base_where=base_where,
                        ),
                        q2=and_sql,
                        r1_summary=p_res.summary(),
                        r2_summary={
                            **and_res.summary(),
                            "predicate": pred,
                            "conjoined": conjoin,
                            "groups_not_in_p_partition": sorted(extra)[:8],
                        },
                        category=category,
                        notes="pinolo-lite: WHERE p AND q produced keys "
                        "absent from WHERE p (lost-qual signature)",
                    )
                )
        return (HIT if hits else CLEAN), hits
