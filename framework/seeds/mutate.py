"""Rule-based seed mutation: turns historical-bug queries into new TLP/NoREC
test cases without spending any LLM calls.

For each seed we extract the tables and columns from its CREATE TABLE
statements, synthesize predicates over those columns, and emit mutation
records the hunter feeds straight into the deterministic oracles.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

from seeds.store import Seed

_CREATE_TABLE = re.compile(
    r"create\s+(?:or\s+replace\s+)?table\s+(?:if\s+not\s+exists\s+)?"
    r"([a-zA-Z_][\w.]*)\s*\(([^;]*)\)",
    re.IGNORECASE | re.DOTALL,
)

_NUMERIC_TYPES = re.compile(
    r"\b(int|integer|bigint|smallint|tinyint|hugeint|serial|bigserial|"
    r"double|real|float|numeric|decimal|oid)\b",
    re.IGNORECASE,
)

_BOOL_TYPES = re.compile(r"\bbool(ean)?\b", re.IGNORECASE)
_STRING_TYPES = re.compile(r"\b(varchar|char|text|string|name)\b", re.IGNORECASE)
_DATE_TYPES = re.compile(r"\b(date|timestamp|datetime)\b", re.IGNORECASE)

_NUMERIC_CONSTS = ["0", "1", "-1", "42", "-2147483648", "2147483647", "0.5", "-0.5", "999999999.99", "-1e308", "1e308"]
_STRING_CONSTS = ["''", "'a'", "'dup'", "'%'", "'_'" ]
_DATE_CONSTS = ["DATE '1970-01-01'", "DATE '2000-01-01'", "TIMESTAMP '1970-01-01 00:00:00'"]


@dataclass
class Mutation:
    """One zero-LLM test case derived from a seed."""

    setup_sqls: list[str]
    select_from: str  # SELECT ... FROM ... (no injected predicate)
    predicate: str | None
    category: str
    source: str


def _parse_columns(setup_sqls: list[str]) -> dict[str, list[tuple[str, str]]]:
    """table -> [(column, type)] extracted from CREATE TABLE statements."""
    tables: dict[str, list[tuple[str, str]]] = {}
    for stmt in setup_sqls:
        match = _CREATE_TABLE.search(stmt)
        if not match:
            continue
        name = match.group(1).split(".")[-1]
        cols: list[tuple[str, str]] = []
        for part in match.group(2).split(","):
            part = part.strip()
            if not part or re.match(
                r"(primary|foreign|unique|check|constraint|key)\b", part, re.I
            ):
                continue
            pieces = part.split(None, 1)
            if len(pieces) < 2:
                continue
            col, ctype = pieces[0].strip('"`'), pieces[1]
            col = re.sub(r"\s+.*$", "", col)  # strip trailing whitespace bits
            cols.append((col, ctype.strip()))
        if cols:
            tables[name] = cols
    return tables


def _predicate_for_column(
    table_alias: str, col: str, ctype: str, rng: random.Random
) -> str | None:
    """One predicate referencing ``alias.col`` of the given SQL type."""
    qual = f"{table_alias}.{col}" if table_alias else col
    roll = rng.random()
    if _NUMERIC_TYPES.search(ctype):
        const = rng.choice(_NUMERIC_CONSTS)
        return rng.choice(
            [
                f"{qual} > {const}",
                f"{qual} <= {const}",
                f"{qual} <> {const}",
                f"{qual} IS NOT NULL",
                f"{qual} BETWEEN {const} AND {const} + 10",
            ]
        )
    if _BOOL_TYPES.search(ctype):
        return rng.choice(
            [f"{qual} IS TRUE", f"{qual} IS NOT TRUE", f"{qual} IS NOT NULL"]
        )
    if _STRING_TYPES.search(ctype):
        const = rng.choice(_STRING_CONSTS)
        return rng.choice(
            [
                f"{qual} <> {const}",
                f"{qual} LIKE '%'",
                f"{qual} IS NOT NULL",
                f"length({qual}) >= 0",
            ]
        )
    if _DATE_TYPES.search(ctype):
        const = rng.choice(_DATE_CONSTS)
        return rng.choice([f"{qual} >= {const}", f"{qual} IS NOT NULL"])
    return f"{qual} IS NOT NULL"


def _alias_for(tail: str, tname: str) -> str:
    """Return the alias used for ``tname`` in a FROM tail, else ``tname``."""
    match = re.search(
        rf"\b{re.escape(tname)}\b\s+(?:as\s+)?([a-zA-Z_]\w*)", tail, re.I
    )
    if match and match.group(1).upper() not in {
        "WHERE", "JOIN", "ON", "LEFT", "RIGHT", "INNER", "CROSS", "FULL",
        "NATURAL", "GROUP", "ORDER", "LIMIT",
    }:
        return match.group(1)
    return tname


_CLAUSE_KEYWORDS = ("WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET",
                    "UNION", "EXCEPT", "INTERSECT", "FETCH", "FOR")


def _from_tail(query: str) -> str | None:
    """Extract the top-level ``FROM <joins>`` segment of a SELECT.

    Stops at the first top-level clause keyword (WHERE/GROUP/ORDER/LIMIT/...)
    so callers can safely append a fresh WHERE predicate.
    """
    upper = query.upper()
    depth, index, in_str = 0, 0, False
    start = -1
    while index < len(query):
        ch = query[index]
        if in_str:
            if ch == "'":
                in_str = False
            index += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if start < 0 and upper.startswith("FROM", index):
                before_ok = index == 0 or not (
                    upper[index - 1].isalnum() or upper[index - 1] == "_"
                )
                if before_ok:
                    start = index
                    index += 4
                    continue
            if start >= 0:
                for kw in _CLAUSE_KEYWORDS:
                    if upper.startswith(kw, index):
                        before_ok = index == 0 or not (
                            upper[index - 1].isalnum()
                            or upper[index - 1] == "_"
                        )
                        after = index + len(kw)
                        after_ok = after >= len(upper) or not (
                            upper[after].isalnum() or upper[after] == "_"
                        )
                        if before_ok and after_ok:
                            return query[start:index].strip()
        index += 1
    return query[start:].strip() if start >= 0 else None


class SeedMutator:
    """Mutates corpus seeds into oracle-ready test cases."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random(0)

    def mutations_for(
        self, seed: Seed, max_per_seed: int = 4
    ) -> list[Mutation]:
        """Rule-based mutations of one seed query.

        Two mutation shapes:
        * ``predicate_wrap``: run the seed query's FROM tail under a fresh
          synthesized predicate (TLP/NoREC targets it).
        * ``predicate_combo``: three-predicate combination stressing
          correlated/self-join shapes (the class that found our deliminator
          bug).
        """
        tables = _parse_columns(seed.setup_sqls)
        if not tables:
            return []
        tail = _from_tail(seed.query)
        if tail is None:
            return []
        select_from = f"SELECT * {tail}"
        out: list[Mutation] = []
        # pick a table that actually appears in the FROM tail
        names = [t for t in tables if re.search(rf"\b{re.escape(t)}\b", tail, re.I)]
        if not names:
            names = list(tables)
        for _ in range(max_per_seed):
            tname = self.rng.choice(names)
            cols = tables[tname]
            roll = self.rng.random()
            if roll < 0.30 and len(cols) >= 2:
                mut = self._correlated_self_join(tname, cols, seed)
                if mut is not None:
                    out.append(mut)
                    continue
            if roll < 0.45:
                mut = self._correlated_variant(tname, cols, seed)
                if mut is not None:
                    out.append(mut)
                    continue
            if roll < 0.75:
                # Structural shapes aimed at fault surfaces *other* than the
                # correlated-subquery one — one per optimizer rule family.
                mut = self._structural_variant(tname, cols, seed, tail)
                if mut is not None:
                    out.append(mut)
                    continue
            col, ctype = self.rng.choice(cols)
            qual = _alias_for(tail, tname)
            pred = _predicate_for_column(qual, col, ctype, self.rng)
            if not pred:
                continue
            if self.rng.random() < 0.4 and len(cols) > 1:
                col2, ctype2 = self.rng.choice(cols)
                p2 = _predicate_for_column(qual, col2, ctype2, self.rng)
                if p2:
                    pred = f"({pred}) AND ({p2})"
            out.append(
                Mutation(
                    setup_sqls=seed.setup_sqls,
                    select_from=select_from,
                    predicate=pred,
                    category="seed_mutation",
                    source=seed.source,
                )
            )
        return out

    # ------------------------------------------------- structural shapes
    def _structural_variant(
        self, tname: str, cols: list[tuple[str, str]], seed: Seed, tail: str
    ) -> Mutation | None:
        """Mutation shapes that each aim at a different optimizer rule's
        fault surface (window, IN-list, set-op, aggregate, join, regex, ...).
        Unlike predicate-level mutations these replace the query skeleton.
        """
        numeric = [c for c, t in cols if _NUMERIC_TYPES.search(t)]
        strings = [c for c, t in cols if _STRING_TYPES.search(t)]
        if not numeric:
            return None
        c1 = self.rng.choice([c for c, _ in cols])
        n1 = self.rng.choice(numeric)
        n2 = self.rng.choice(numeric) if len(numeric) > 1 else n1
        s1 = self.rng.choice(strings) if strings else c1

        shape = self.rng.choice(
            [
                "window_range", "window_partition", "in_list_null",
                "not_in_null", "set_op", "agg_distinct", "agg_filter",
                "self_join_eq", "like_prefix", "case_null", "scalar_select",
                "grouping_having", "union_null", "cast_pred", "cte_twice",
            ]
        )
        select_from: str | None = None
        predicate: str | None = None
        query: str | None = None

        if shape == "window_range":
            query = (
                f"SELECT a.{n1}, SUM(a.{n2}) OVER (ORDER BY a.{n1} RANGE "
                f"BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM {tname} a"
            )
        elif shape == "window_partition":
            query = (
                f"SELECT a.{c1}, a.{n1}, ROW_NUMBER() OVER (PARTITION BY "
                f"a.{c1} ORDER BY a.{n1}), AVG(a.{n2}) OVER (PARTITION BY "
                f"a.{c1}) FROM {tname} a"
            )
        elif shape == "in_list_null":
            predicate = f"{n1} IN (0, 1, NULL, -1)"
            select_from = f"SELECT * {tail}"
        elif shape == "not_in_null":
            # NOT IN over a subquery that may yield NULL — 3VL trap
            predicate = (
                f"a.{n1} NOT IN (SELECT {n2} FROM {tname} b WHERE b.{c1} <> "
                f"a.{c1})"
            )
            select_from = f"SELECT * FROM {tname} a"
        elif shape == "set_op":
            query = (
                f"(SELECT a.{n1} FROM {tname} a WHERE a.{n1} >= 0 UNION ALL "
                f"SELECT a.{n1} FROM {tname} a WHERE a.{n1} < 0) EXCEPT "
                f"(SELECT a.{n1} FROM {tname} a)"
            )
        elif shape == "agg_distinct":
            query = (
                f"SELECT COUNT(DISTINCT a.{c1}), SUM(DISTINCT a.{n1}), "
                f"COUNT(a.{n1}) FROM {tname} a GROUP BY a.{c1}"
            )
        elif shape == "agg_filter":
            query = (
                f"SELECT SUM(a.{n1}) FILTER (WHERE a.{n1} > 0), COUNT(*) "
                f"FILTER (WHERE a.{c1} IS NULL), AVG(a.{n2}) FROM {tname} a"
            )
        elif shape == "self_join_eq":
            query = (
                f"SELECT a.{n1} FROM {tname} a, {tname} b WHERE a.{c1} = "
                f"b.{c1} AND a.{n1} = b.{n1}"
            )
        elif shape == "like_prefix" and strings:
            predicate = f"{s1} LIKE 'a%' OR {s1} LIKE '%z'"
            select_from = f"SELECT * {tail}"
        elif shape == "case_null":
            predicate = (
                f"CASE WHEN {n1} > 0 THEN {n2} WHEN {n1} IS NULL THEN 0 "
                f"ELSE -{n2} END IS NOT NULL"
            )
            select_from = f"SELECT * {tail}"
        elif shape == "scalar_select":
            query = (
                f"SELECT a.{c1}, (SELECT MAX(b.{n1}) FROM {tname} b WHERE "
                f"b.{c1} = a.{c1}) FROM {tname} a"
            )
        elif shape == "grouping_having":
            query = (
                f"SELECT a.{c1}, SUM(a.{n1}) FROM {tname} a GROUP BY a.{c1} "
                f"HAVING COUNT(*) >= 1 AND SUM(a.{n2}) IS NOT NULL"
            )
        elif shape == "union_null":
            query = (
                f"SELECT a.{n1} FROM {tname} a UNION SELECT NULL UNION "
                f"SELECT a.{n1} FROM {tname} a WHERE a.{n1} IS NULL"
            )
        elif shape == "cast_pred":
            predicate = f"CAST({n1} AS DECIMAL(18,4)) <> CAST({n1} AS DOUBLE)"
            select_from = f"SELECT * {tail}"
        elif shape == "cte_twice":
            query = (
                f"WITH x AS (SELECT a.{c1}, a.{n1} FROM {tname} a) SELECT * "
                f"FROM x a JOIN x b ON a.{c1} = b.{c1} AND a.{n1} <> b.{n1}"
            )
        if query is None and select_from is None:
            return None
        return Mutation(
            setup_sqls=seed.setup_sqls,
            select_from=select_from or "",
            predicate=predicate,
            category=f"seed_struct_{shape}",
            source=seed.source,
        ) if select_from else Mutation(
            setup_sqls=seed.setup_sqls,
            select_from=query,
            predicate=None,
            category=f"seed_struct_{shape}",
            source=seed.source,
        )

    def _correlated_self_join(
        self, tname: str, cols: list[tuple[str, str]], seed: Seed
    ) -> Mutation | None:
        """The eq+neq+inequality correlated EXISTS shape behind the
        deliminator bug, instantiated on the seed's own table."""
        numeric = [c for c, t in cols if _NUMERIC_TYPES.search(t)]
        eq_candidates = [c for c, _ in cols]
        if len(numeric) < 2 or not eq_candidates:
            return None
        eq_col = self.rng.choice(eq_candidates)
        neq_col = self.rng.choice(eq_candidates)
        gt_col = self.rng.choice(numeric)
        if eq_col == neq_col:
            return None
        select_from = f"SELECT * FROM {tname} a"
        predicate = (
            f"EXISTS (SELECT 1 FROM {tname} b WHERE b.{eq_col} = a.{eq_col} "
            f"AND b.{neq_col} <> a.{neq_col} AND b.{gt_col} > a.{gt_col})"
        )
        return Mutation(
            setup_sqls=seed.setup_sqls,
            select_from=select_from,
            predicate=predicate,
            category="seed_self_join_exists",
            source=seed.source,
        )

    def _correlated_variant(
        self, tname: str, cols: list[tuple[str, str]], seed: Seed
    ) -> Mutation | None:
        """Other historically buggy correlated-subquery shapes on the seed's
        own table: NOT EXISTS anti-join, IN-with-NULL, correlated scalar."""
        numeric = [c for c, t in cols if _NUMERIC_TYPES.search(t)]
        if len(cols) < 2 or not numeric:
            return None
        c1 = self.rng.choice([c for c, _ in cols])
        c2 = self.rng.choice(numeric)
        if c1 == c2 and len(numeric) > 1:
            c2 = self.rng.choice([c for c in numeric if c != c1])
        shape = self.rng.choice(
            ["not_exists", "in_subq", "scalar_cmp", "not_in",
             "any_all", "exists_having"]
        )
        if shape == "not_exists":
            pred = (
                f"NOT EXISTS (SELECT 1 FROM {tname} b WHERE b.{c1} = a.{c1} "
                f"AND b.{c2} <> a.{c2})"
            )
        elif shape == "in_subq":
            pred = f"a.{c2} IN (SELECT b.{c2} FROM {tname} b WHERE b.{c1} <> a.{c1})"
        elif shape == "not_in":
            pred = f"a.{c2} NOT IN (SELECT b.{c2} FROM {tname} b WHERE b.{c1} = a.{c1})"
        elif shape == "any_all":
            op = self.rng.choice(["= ANY", "<> ALL", "> ANY"])
            pred = (
                f"a.{c2} {op} (SELECT b.{c2} FROM {tname} b "
                f"WHERE b.{c1} = a.{c1})"
            )
        elif shape == "exists_having":
            # EXISTS with an aggregate inside — stresses mark-join + groupby
            pred = (
                f"EXISTS (SELECT 1 FROM {tname} b WHERE b.{c1} = a.{c1} "
                f"HAVING COUNT(*) > 1 AND MAX(b.{c2}) > a.{c2})"
            )
        else:  # scalar_cmp
            pred = (
                f"a.{c2} < (SELECT MAX(b.{c2}) FROM {tname} b "
                f"WHERE b.{c1} = a.{c1})"
            )
        return Mutation(
            setup_sqls=seed.setup_sqls,
            select_from=f"SELECT * FROM {tname} a",
            predicate=pred,
            category=f"seed_corr_{shape}",
            source=seed.source,
        )
