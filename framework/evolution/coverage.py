"""Coverage-conditioned exploration state (cold-start regime of DCE).

When no bug has fired yet, there is no root-cause signature sigma to
condition on — so the explorer conditions on *coverage* instead: which
(SQL-feature, plan-operator, verdict-class) cells have been observed.
Novelty pressure replaces diagnostic pressure until the first
non-clean verdict produces a sigma and the warm machinery takes over.

Same conditioning interface, different signal source.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

# -- feature tagger -------------------------------------------------------

_FEATURES: list[tuple[str, re.Pattern]] = [
    ("merge", re.compile(r"\bmerge\s+into\b", re.I)),
    ("lateral", re.compile(r"\blateral\b", re.I)),
    ("window", re.compile(r"\bover\s*\(", re.I)),
    ("win_exclude", re.compile(r"\bexclude\s+(current|group|ties)", re.I)),
    ("partition_ddl", re.compile(r"\bpartition\s+by\b", re.I)),
    ("subquery", re.compile(r"\(\s*select\b", re.I)),
    ("cte", re.compile(r"\bwith\b.+\bas\s*\(", re.I)),
    ("returning", re.compile(r"\breturning\b", re.I)),
    ("on_conflict", re.compile(r"\bon\s+conflict\b", re.I)),
    ("join_left", re.compile(r"\bleft\s+(outer\s+)?join\b", re.I)),
    ("join_full", re.compile(r"\bfull\s+(outer\s+)?join\b", re.I)),
    ("join_inner", re.compile(r"\b(inner\s+|cross\s+)?join\b", re.I)),
    ("agg", re.compile(r"\b(count|sum|avg|min|max|string_agg|array_agg|"
                       r"bool_and|bool_or)\s*\(", re.I)),
    ("distinct", re.compile(r"\bdistinct\b", re.I)),
    ("groupby", re.compile(r"\bgroup\s+by\b", re.I)),
    ("having", re.compile(r"\bhaving\b", re.I)),
    ("in_list", re.compile(r"\b(not\s+)?in\s*\(", re.I)),
    ("case", re.compile(r"\bcase\s+when\b", re.I)),
    ("is_null", re.compile(r"\bis\s+(not\s+)?null\b", re.I)),
    ("is_distinct", re.compile(r"\bis\s+(not\s+)?distinct\s+from\b", re.I)),
    ("cast", re.compile(r"::\s*\w+|\bcast\s*\(", re.I)),
    ("ext_ltree", re.compile(r"\bltree\b|::ltree", re.I)),
    ("ext_intarray", re.compile(r"\bintarray\b", re.I)),
    ("arrays", re.compile(r"\barray\s*\[|\bunnest\s*\(", re.I)),
    ("jsonb", re.compile(r"\bjsonb\b|->>|->", re.I)),
    ("interval", re.compile(r"\binterval\b|\bdate_bin\s*\(", re.I)),
    ("numeric", re.compile(r"\bnumeric\b|\bdecimal\b", re.I)),
    ("generated", re.compile(r"\bgenerated\s+(always|by\s+default)\b", re.I)),
    ("savepoint", re.compile(r"\bsavepoint\b", re.I)),
    ("serializable", re.compile(r"\bserializable\b", re.I)),
    ("recursive", re.compile(r"\brecursive\b", re.I)),
    ("setop", re.compile(r"\bunion\b|\bintersect\b|\bexcept\b", re.I)),
    ("ordinals", re.compile(r"\border\s+by\b|\blimit\b|\boffset\b", re.I)),
    ("update", re.compile(r"^\s*update\b", re.I)),
    ("delete", re.compile(r"^\s*delete\b", re.I)),
    ("insert", re.compile(r"^\s*insert\b", re.I)),
]

_PLAN_NODE = re.compile(
    r"(Seq Scan|Index Only Scan|Index Scan|Bitmap Heap Scan|Bitmap Index Scan|"
    r"Tid Scan|Memoize|Nested Loop|Hash Join|Merge Join|Merge Append|Append|"
    r"Sort|Incremental Sort|HashAggregate|GroupAggregate|Aggregate|WindowAgg|"
    r"Gather|Gather Merge|Subquery Scan|Materialize|CTE Scan|WorkTable Scan|"
    r"Result|ProjectSet|Unique|SetOp|Recursive Union|LockRows|Foreign Scan|"
    r"Function Scan|Values Scan|Limit|ModifyTable|Hash|InitPlan|SubPlan)"
)


def feature_tags(sql: str) -> frozenset[str]:
    return frozenset(name for name, pat in _FEATURES if pat.search(sql))


def plan_ops(explain_lines: Iterable[str]) -> frozenset[str]:
    ops: set[str] = set()
    for line in explain_lines:
        ops.update(m.group(0) for m in _PLAN_NODE.finditer(line))
    return frozenset(ops)


# -- tracker --------------------------------------------------------------

class CoverageTracker:
    """Counts observed (feature) / (feature x planop) / (feature, verdict)
    cells and reports novelty pressure for the cold-start explorer."""

    def __init__(self) -> None:
        self.feat_cells: Counter[str] = Counter()
        self.feat_op_cells: Counter[tuple[str, str]] = Counter()
        self.feat_verdict_cells: Counter[tuple[str, str]] = Counter()
        self.op_cells: Counter[str] = Counter()
        self.verdicts: Counter[str] = Counter()
        self.n_queries = 0

    def novelty(self, features: frozenset[str],
              ops: frozenset[str] | None = None) -> int:
        """How many previously-unseen cells this query would add."""
        n = sum(1 for f in features if self.feat_cells[f] == 0)
        if ops:
            n += sum(1 for o in ops if self.op_cells[o] == 0)
            n += sum(1 for f in features for o in ops
                     if self.feat_op_cells[(f, o)] == 0)
        return n

    def record(self, features: frozenset[str], ops: frozenset[str],
               verdict: str) -> int:
        """Record one executed query; return its novelty count."""
        nv = self.novelty(features, ops)
        for f in features:
            self.feat_cells[f] += 1
            self.feat_verdict_cells[(f, verdict)] += 1
        for o in ops:
            self.op_cells[o] += 1
            for f in features:
                self.feat_op_cells[(f, o)] += 1
        self.verdicts[verdict] += 1
        self.n_queries += 1
        return nv

    def frontier(self, space: Iterable[str]) -> list[str]:
        """Feature names in `space` never observed (or rarest-first list)."""
        return sorted(space, key=lambda f: self.feat_cells[f])

    def summary(self) -> dict:
        return {
            "queries": self.n_queries,
            "features_seen": len(self.feat_cells),
            "plan_ops_seen": len(self.op_cells),
            "feature_x_op_cells": len(self.feat_op_cells),
            "verdicts": dict(self.verdicts),
            "rarest_features": self.frontier(list(self.feat_cells))[:10],
        }
