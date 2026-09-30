"""Feature-cell extraction for coverage-guided exploration.

Each executed query is mapped to a set of cells in a three-dimensional
feature space:

* ``ast:<FEATURE>``   — syntactic constructs present in the query text,
* ``plan:<OP>``       — physical operators in the engine's EXPLAIN plan,
* ``data:<TYPE>:<D>`` — column types combined with corner-case data traits.

A "new cell" is a feature combination never exercised before; the count of
new cells feeds the exploration bandit's novelty reward.
"""

from __future__ import annotations

import logging
import re
from typing import Any

LOGGER = logging.getLogger(__name__)

_AST_FEATURES: list[tuple[str, str]] = [
    ("DISTINCT", r"\bdistinct\b"),
    ("JOIN", r"\bjoin\b"),
    ("LEFT_JOIN", r"\bleft\s+(outer\s+)?join\b"),
    ("RIGHT_JOIN", r"\bright\s+(outer\s+)?join\b"),
    ("FULL_JOIN", r"\bfull\s+(outer\s+)?join\b"),
    ("CROSS_JOIN", r"\bcross\s+join\b"),
    ("EXISTS", r"\bexists\b"),
    ("NOT_EXISTS", r"\bnot\s+exists\b"),
    ("IN_SUBQUERY", r"\b(in|not\s+in)\s*\(\s*select\b"),
    ("SCALAR_SUBQUERY", r"\(\s*select\b"),
    ("CORRELATED_SUBQUERY", r""),  # filled below by heuristic
    ("CASE", r"\bcase\b"),
    ("COALESCE", r"\bcoalesce\s*\("),
    ("NULLIF", r"\bnullif\s*\("),
    ("CAST", r"\bcast\s*\(|::"),
    ("UNION", r"\bunion\b"),
    ("EXCEPT", r"\bexcept\b"),
    ("INTERSECT", r"\bintersect\b"),
    ("GROUP_BY", r"\bgroup\s+by\b"),
    ("ROLLUP", r"\brollup\s*\("),
    ("CUBE", r"\bcube\s*\("),
    ("GROUPING_SETS", r"\bgrouping\s+sets\b"),
    ("HAVING", r"\bhaving\b"),
    ("ORDER_BY", r"\border\s+by\b"),
    ("LIMIT", r"\blimit\b"),
    ("OFFSET", r"\boffset\b"),
    ("WINDOW", r"\bover\s*\("),
    ("CTE", r"\bwith\b"),
    ("RECURSIVE", r"\brecursive\b"),
    ("LIKE", r"\b(like|ilike)\b"),
    ("REGEXP", r"\b(regexp_matches|regexp_replace|~|similar\s+to)\b"),
    ("STRING_FN", r"\b(substring|substr|concat|length|upper|lower|trim|position)\s*\("),
    ("DATE_FN", r"\b(date_trunc|date_part|extract|age|date_diff|date_add)\s*\("),
    ("INTERVAL", r"\binterval\b"),
    ("AGGREGATE", r"\b(count|sum|avg|min|max|stddev|variance|string_agg|array_agg|list|bool_and|bool_or)\s*\("),
    ("ARITH", r"[+\-*/]"),
    ("IS_NULL", r"\bis\s+(not\s+)?null\b"),
    ("BETWEEN", r"\bbetween\b"),
    ("SELF_JOIN", r""),  # filled below
]

_CORRELATED_HINT = re.compile(
    r"\bexists\s*\(|\b(in|not\s+in)\s*\(\s*select\b", re.IGNORECASE
)


def _ast_cells(sql: str) -> set[str]:
    cells: set[str] = set()
    for name, pattern in _AST_FEATURES:
        if pattern and re.search(pattern, sql, re.IGNORECASE):
            cells.add(f"ast:{name}")
    # Self-join: same table name appearing twice in FROM/JOIN.
    tables = re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", sql, re.IGNORECASE)
    if len(tables) != len(set(t.lower() for t in tables)):
        cells.add("ast:SELF_JOIN")
    if _CORRELATED_HINT.search(sql) and re.search(
        r"\b\w+\.\w+\s*(=|<>|!=|<|>|<=|>=)\s*\w+\.\w+", sql
    ):
        cells.add("ast:CORRELATED_CMP")
    return cells


def _plan_cells(plan: Any) -> set[str]:
    cells: set[str] = set()
    if not plan:
        return cells
    if isinstance(plan, dict):
        for key in ("operator_name", "name", "Node Type", "node_type"):
            value = plan.get(key)
            if isinstance(value, str) and value:
                cells.add(f"plan:{value.strip().upper().replace(' ', '_')}")
        for value in plan.values():
            if isinstance(value, (dict, list)):
                cells.update(_plan_cells(value))
    elif isinstance(plan, list):
        for item in plan:
            cells.update(_plan_cells(item))
    return cells


_TYPE_PATTERNS: list[tuple[str, str]] = [
    ("INT", r"\b(int|integer|bigint|smallint|tinyint|hugeint|serial|bigserial)\b"),
    ("FP", r"\b(double|real|float|numeric|decimal)\b"),
    ("DECIMAL", r"\b(decimal|numeric)\s*\("),
    ("BOOL", r"\bboolean|bool\b"),
    ("STRING", r"\b(varchar|char|text|string)\b"),
    ("DATE", r"\bdate\b"),
    ("TIMESTAMP", r"\b(timestamp|datetime)\b"),
]

_DATA_TRAITS: list[tuple[str, str]] = [
    ("null", r"\bnull\b"),
    ("zero", r"(?<![\w.])0(?![\w.])"),
    ("negative", r"(?<![\w.])-\d"),
    ("empty_str", r"''"),
    ("extreme", r"\d{9,}|1e[+-]?\d{2,}|-\d{9,}"),
    ("dup", r""),  # heuristic: same INSERT prefix twice — checked separately
]


def _data_cells(setup_sqls: list[str]) -> set[str]:
    cells: set[str] = set()
    ddl = "\n".join(s for s in setup_sqls if s.lstrip().upper().startswith("CREATE"))
    dml = "\n".join(s for s in setup_sqls if s.lstrip().upper().startswith("INSERT"))
    types = {name for name, pat in _TYPE_PATTERNS if re.search(pat, ddl, re.IGNORECASE)}
    for tname in types:
        cells.add(f"data:{tname}")
        for dname, pat in _DATA_TRAITS:
            if pat and re.search(pat, dml, re.IGNORECASE):
                cells.add(f"data:{tname}:{dname}")
    # Duplicate rows: two INSERTs into the same table sharing a VALUES prefix.
    seen_prefix: set[str] = set()
    for stmt in setup_sqls:
        m = re.match(r"\s*insert\s+into\s+(\w+)\s*values\s*\(([^)]{0,20})", stmt, re.I)
        if m:
            key = f"{m.group(1).lower()}:{m.group(2).strip()[:12]}"
            if key in seen_prefix:
                cells.add("data:dup_rows")
            seen_prefix.add(key)
    return cells


def extract_cells(
    runner: Any,
    sql: str,
    setup_sqls: list[str],
    use_plan: bool = True,
) -> set[str]:
    """Return the coverage cell set for one executed query.

    ``runner`` must expose ``explain_plan(sql) -> dict|None``; when the
    explain fails the plan dimension is simply absent.
    """
    cells = _ast_cells(sql) | _data_cells(setup_sqls)
    if use_plan:
        try:
            plan = runner.explain_plan(sql)
        except Exception:  # noqa: BLE001 - coverage must never break the loop
            LOGGER.debug("explain_plan failed", exc_info=True)
            plan = None
        cells |= _plan_cells(plan)
    return cells
