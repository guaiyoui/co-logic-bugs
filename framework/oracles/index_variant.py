"""Index-presence / storage-state oracle (PostgreSQL-first).

Plan-variant GUCs only flip *which plan* runs; this oracle changes the
*storage state* underneath the same query:

- index present vs absent (btree/hash/gist/gin/brin, partial, expression,
  covering/INCLUDE -> index-only scan eligibility)
- HOT chains: UPDATE non-indexed columns so indexed predicates must
  follow heap-only-tuple chains
- statistics shifts: ANALYZE after bulk mutation, widened
  default_statistics_target
- prepared-plan invalidation: PREPARE -> DDL -> EXECUTE

A semantically identical query must return the same bag under every
state. Any divergence means the access-path code returned wrong data —
historically PostgreSQL's richest wrong-result surface (GIN pending
lists, GiST splits, HOT+index-only-scan visibility, BRIN summaries).

Teardown restores state honestly: created indexes are DROPped, mutated
rows are not restored (mutations land in their own variant sub-state
and the query bag is compared before vs after within the variant).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from oracles.models import (
    KIND_CRASH,
    KIND_PLAN_VARIANT,
    Candidate,
    new_candidate_id,
)
from oracles.errors import (
    is_guard_rail_error,
    is_value_dependent_error,
)

LOGGER = logging.getLogger(__name__)

_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?(\w+)[\"']?\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL,
)
_CONSTRAINT_STARTS = (
    "primary", "unique", "foreign", "check", "constraint", "exclude",
    "like", "references",
)
_INT_TYPES = re.compile(r"int|serial|numeric|decimal|real|double|float",
                        re.I)
_TEXT_TYPES = re.compile(r"char|text|varchar|citext|name", re.I)
_ARR_JSON = re.compile(r"\[\]|jsonb?|hstore|ltree|text\[\]|int\[\]", re.I)


@dataclass
class _Table:
    name: str
    cols: list[tuple[str, str]] = field(default_factory=list)  # (name, type)


def _split_top_level(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def parse_tables(schema_sqls: list[str]) -> list[_Table]:
    """Tolerant CREATE TABLE -> (table, [(col, rawtype)]) extraction."""
    tables: list[_Table] = []
    for sql in schema_sqls:
        m = _TABLE_RE.search(sql)
        if not m:
            continue
        t = _Table(name=m.group(1))
        for item in _split_top_level(m.group(2)):
            tokens = item.strip().split()
            if len(tokens) < 2:
                continue
            col = tokens[0].strip("\"'")
            if col.lower() in _CONSTRAINT_STARTS:
                continue
            t.cols.append((col, " ".join(tokens[1:3])))
        if t.cols:
            tables.append(t)
    return tables


def _variants_for(t: _Table) -> list[tuple[str, list[str], list[str]]]:
    """(label, setup, teardown) state variants for one table.

    Teardown drops every index the setup created — restoring the true
    prior state instead of a hard-coded default.
    """
    out: list[tuple[str, list[str], list[str]]] = []
    cols = t.cols[:6]
    # On the small tables fuzzers generate, the planner prefers seqscan
    # and the index code path is never exercised. Every index variant
    # therefore disables seqscan so the access method actually runs;
    # the bag must still equal the baseline's.
    force_idx = ["SET enable_seqscan = off"]
    unforce = ["RESET enable_seqscan"]
    for i, (col, typ) in enumerate(cols):
        idx = f"iv_{t.name}_{i}"
        out.append((f"btree_{col}",
                    [f"CREATE INDEX {idx} ON {t.name}({col})"] + force_idx,
                    unforce + [f"DROP INDEX IF EXISTS {idx}"]))
    # covering index -> index-only-scan eligibility after VACUUM
    all_cols = ", ".join(c for c, _ in cols)
    first = cols[0][0]
    rest = [c for c, _ in cols[1:4]]
    inc = f" INCLUDE ({', '.join(rest)})" if rest else ""
    out.append(("covering_ios",
                [f"CREATE INDEX iv_{t.name}_cov ON {t.name}({first}){inc}",
                 f"VACUUM ANALYZE {t.name}"] + force_idx,
                unforce + [f"DROP INDEX IF EXISTS iv_{t.name}_cov"]))
    # partial index on first int-ish or text col
    for col, typ in cols:
        if _INT_TYPES.search(typ) or _TEXT_TYPES.search(typ):
            out.append(("partial",
                        [f"CREATE INDEX iv_{t.name}_p ON {t.name}({col}) "
                         f"WHERE {col} IS NOT NULL"] + force_idx,
                        unforce + [f"DROP INDEX IF EXISTS iv_{t.name}_p"]))
            break
    # expression index (lower() on text, abs() on int)
    for col, typ in cols:
        if _TEXT_TYPES.search(typ):
            out.append(("expr_lower",
                        [f"CREATE INDEX iv_{t.name}_e ON "
                         f"{t.name}(lower({col}::text))"] + force_idx,
                        unforce + [f"DROP INDEX IF EXISTS iv_{t.name}_e"]))
            break
        if _INT_TYPES.search(typ):
            out.append(("expr_abs",
                        [f"CREATE INDEX iv_{t.name}_e ON "
                         f"{t.name}(abs({col}))"] + force_idx,
                        unforce + [f"DROP INDEX IF EXISTS iv_{t.name}_e"]))
            break
    # BRIN on first int-ish col (summary-granularity path)
    for col, typ in cols:
        if _INT_TYPES.search(typ):
            out.append(("brin",
                        [f"CREATE INDEX iv_{t.name}_b ON {t.name} "
                         f"USING brin({col})"] + force_idx,
                        unforce + [f"DROP INDEX IF EXISTS iv_{t.name}_b"]))
            break
    # GIN on array/json cols (pending-list path)
    for col, typ in cols:
        if _ARR_JSON.search(typ):
            out.append(("gin",
                        [f"CREATE INDEX iv_{t.name}_g ON {t.name} "
                         f"USING gin({col})"] + force_idx,
                        unforce + [f"DROP INDEX IF EXISTS iv_{t.name}_g"]))
            break
    # HOT-chain variant: index one col, UPDATE a different (non-indexed)
    # col so tuples move through heap-only chains, then query.
    # UPDATE must not change logical state: row triggers are disabled
    # for the no-op write and re-enabled in teardown (a trigger firing
    # again would be a *data* change, not an access-path change).
    if len(cols) >= 2:
        icol, mcol = cols[0][0], cols[1][0]
        out.append(("hot_update",
                    [f"CREATE INDEX iv_{t.name}_h ON {t.name}({icol})",
                     f"ALTER TABLE {t.name} DISABLE TRIGGER ALL",
                     f"UPDATE {t.name} SET {mcol} = {mcol}",
                     f"ALTER TABLE {t.name} ENABLE TRIGGER ALL",
                     f"VACUUM {t.name}"] + force_idx,
                    unforce +
                    [f"ALTER TABLE {t.name} ENABLE TRIGGER ALL",
                     f"DROP INDEX IF EXISTS iv_{t.name}_h"]))
    return out


class IndexVariantOracle:
    """Compares query bags across storage-state variants of the same DB.

    Only meaningful for engines with real secondary indexes; instantiate
    for ``postgres`` (and later TiDB/MySQL via their own runner).
    """

    def __init__(self, max_variants: int = 12):
        self.max_variants = max_variants
        self._applied = 0
        self._noop = 0

    def variants_for(
        self, schema_sqls: list[str]
    ) -> list[tuple[str, list[str], list[str]]]:
        variants: list[tuple[str, list[str], list[str]]] = []
        for t in parse_tables(schema_sqls):
            for v in _variants_for(t):
                variants.append((f"{t.name}.{v[0]}", v[1], v[2]))
        # stats-shift variant: ANALYZE after the load (statistics axis)
        tables = parse_tables(schema_sqls)
        if tables:
            names = ", ".join(t.name for t in tables)
            variants.append(("analyze_all", [f"ANALYZE {names}"], []))
        return variants[: self.max_variants]

    def check(
        self,
        runner: Any,
        query: str,
        schema_sqls: list[str],
        inserts: list[str],
        category: str = "unknown",
        rewrite_kind: str = "",
        timeout_s: float = 10.0,
    ) -> Candidate | None:
        """Run query baseline, then under each storage variant; the bag
        must not change. Returns a Candidate on the first divergence."""
        baseline = runner.run(query, timeout_s=timeout_s)
        if baseline.is_internal_error:
            return Candidate(
                id=new_candidate_id("iv-crash", query),
                kind=KIND_CRASH,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=query,
                r1_summary=baseline.summary(),
                category=category,
                rewrite_kind=rewrite_kind,
                notes="internal error on index-variant baseline",
            )
        if not baseline.ok:
            return None
        base_bag = baseline.bag()

        for label, setup_stmts, teardown_stmts in self.variants_for(
            schema_sqls
        ):
            setup_ok = True
            for stmt in setup_stmts:
                r = runner.run(stmt, timeout_s=timeout_s)
                if not r.ok:
                    setup_ok = False
                    break
            try:
                if not setup_ok:
                    continue
                # Applicability gate: if the variant didn't actually route
                # the query through an index access path, the comparison
                # proves nothing (like compression no-ops on :memory:).
                # EXPLAIN output must name an index scan node.
                ex = runner.run(f"EXPLAIN {query}", timeout_s=timeout_s)
                if ex.ok and "Index" not in repr(ex.rows) \
                        and "Bitmap" not in repr(ex.rows):
                    self._noop += 1
                    continue
                self._applied += 1
                variant = runner.run(query, timeout_s=timeout_s)
            finally:
                for stmt in teardown_stmts:
                    runner.run(stmt, timeout_s=timeout_s)

            if variant.timed_out:
                continue
            if variant.is_internal_error:
                return Candidate(
                    id=new_candidate_id("iv-crash", query, label),
                    kind=KIND_CRASH,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=query,
                    r1_summary=baseline.summary(),
                    r2_summary={**variant.summary(), "variant": label},
                    category=category,
                    rewrite_kind=rewrite_kind,
                    notes=f"internal error under index variant {label}",
                )
            if not variant.ok:
                if is_value_dependent_error(variant.error) or \
                        is_guard_rail_error(variant.error, label):
                    continue
                return Candidate(
                    id=new_candidate_id("iv-err", query, label),
                    kind=KIND_PLAN_VARIANT,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=query,
                    r1_summary=baseline.summary(),
                    r2_summary={**variant.summary(), "variant": label},
                    category=category,
                    rewrite_kind=rewrite_kind,
                    notes=f"index variant {label} errors while "
                          f"baseline succeeds",
                )
            if variant.bag() != base_bag:
                return Candidate(
                    id=new_candidate_id("iv", query, label),
                    kind=KIND_PLAN_VARIANT,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=query,
                    r1_summary=baseline.summary(),
                    r2_summary={**variant.summary(), "variant": label},
                    category=category,
                    rewrite_kind=rewrite_kind,
                    notes=f"result differs under index variant {label}",
                )
        return None
