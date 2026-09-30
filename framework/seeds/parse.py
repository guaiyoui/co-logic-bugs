"""Parsers turning upstream regression-test files into Seed records.

DuckDB ``.test`` files use ``statement ok|error|query <types>`` blocks; the
``----`` line terminates a query's expected output. PostgreSQL regression
files are plain ``.sql`` scripts — we split on semicolons outside strings and
keep CREATE/INSERT as setup plus SELECT statements as probe queries.
"""

from __future__ import annotations

import re
from typing import Any

from seeds.store import Seed

_SKIP_SQL = re.compile(
    r"^\s*(copy|explain|pragma|require|skip|onlyif|mode|load|install|"
    r"drop\s+database|create\s+database|attach|"
    r"detach|use|listen|notify|"
    r"unlisten|comment|security|alter\s+system|"
    r"drop\s+(extension|function|trigger|procedure|role|user)|\\)",
    re.IGNORECASE,
)

# Statements that seed database state (incl. transaction ordering) rather
# than acting as probes — kept verbatim into setup_sqls.
_SETUP_SQL = re.compile(
    r"^\s*(create|insert|update|delete|merge|alter|drop|truncate|"
    r"prepare|execute|deallocate|set\s+|reset|show|begin|commit|rollback|"
    r"start\s+transaction|abort|end\b|savepoint|release|declare|fetch|move|"
    r"close|vacuum|analyze|analyse|checkpoint|reindex|cluster|grant|revoke|"
    r"call|do\b|lock|refresh|discard|import\s+foreign|security\s+label)",
    re.IGNORECASE,
)

_SELECT_START = re.compile(r"^\s*(select|with|values|table\b)", re.IGNORECASE)

_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$")


def _split_statements(script: str) -> list[str]:
    """Split a SQL script on top-level semicolons (string/comment aware).

    Dollar-quoted bodies ($$...$$, $func$...$func$) are opaque — without
    this, plpgsql function bodies get shredded at their inner semicolons
    and CREATE FUNCTION/TRIGGER/DO can never reach setup.
    """
    stmts: list[str] = []
    current: list[str] = []
    in_str = False
    in_line_comment = False
    dollar_tag: str | None = None
    index = 0
    while index < len(script):
        ch = script[index]
        nxt = script[index + 1] if index + 1 < len(script) else ""
        if in_line_comment:
            # Drop comment text entirely: statements are later flattened to a
            # single line, and a trailing -- comment would eat the tail.
            if ch == "\n":
                in_line_comment = False
                current.append(" ")
            index += 1
            continue
        if dollar_tag is not None:
            if script.startswith(dollar_tag, index):
                current.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            current.append(ch)
            index += 1
            continue
        if in_str:
            current.append(ch)
            if ch == "'":
                in_str = False
            index += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            index += 1
            continue
        if ch == "'":
            in_str = True
            current.append(ch)
            index += 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG.match(script, index)
            if m:
                dollar_tag = m.group(0)
                current.append(dollar_tag)
                index += len(dollar_tag)
                continue
            current.append(ch)
            index += 1
            continue
        if ch == ";":
            stmts.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(ch)
        index += 1
    if "".join(current).strip():
        stmts.append("".join(current).strip())
    return [s for s in stmts if s]


def _clean(stmt: str) -> str:
    return re.sub(r"\s+", " ", stmt).strip()


def parse_duckdb_test(text: str, source: str) -> list[Seed]:
    """Extract (setup, query) pairs from one DuckDB .test file."""
    lines = text.splitlines()
    setup: list[str] = []
    queries: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        head = line.split(None, 1)[0].lower()
        if head in ("statement", "query"):
            parts = line.split(None, 2)
            directive = parts[0].lower()
            body_lines: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                nxt_head = nxt.split(None, 1)[0].lower() if nxt.strip() else ""
                if nxt.strip() == "----" or nxt_head in (
                    "statement",
                    "query",
                    "require",
                    "skip",
                    "onlyif",
                    "mode",
                    "loop",
                    "endloop",
                    "foreach",
                    "endforeach",
                    "halt",
                    "test",
                    "concurrentloop",
                ):
                    if nxt.strip() == "----":
                        index += 1
                        # skip expected result block until the next directive
                        while index < len(lines):
                            nxt2 = lines[index].strip()
                            nxt2_head = nxt2.split(None, 1)[0].lower() if nxt2 else ""
                            if nxt2_head in (
                                "statement", "query", "require", "skip",
                                "onlyif", "mode", "loop", "endloop", "foreach",
                                "endforeach", "halt", "test",
                            ):
                                break
                            index += 1
                    break
                body_lines.append(nxt)
                index += 1
            body = " ".join(body_lines).strip()
            if directive == "statement" and line.lower().startswith("statement ok"):
                for stmt in _split_statements(body):
                    if re.match(
                        r"^\s*(create\s+table|insert|create\s+or\s+replace\s+table|"
                        r"create\s+view|create\s+index)",
                        stmt,
                        re.IGNORECASE,
                    ):
                        setup.append(_clean(stmt))
            elif directive == "query":
                for stmt in _split_statements(body):
                    if _SELECT_START.match(stmt) and not _SKIP_SQL.match(stmt):
                        queries.append(_clean(stmt))
        # non-directive lines are comments/expected output — ignored
    return [
        Seed(setup_sqls=list(setup), query=q, source=source, engine="duckdb")
        for q in queries
        if setup
    ]


def parse_pg_regress(text: str, source: str) -> list[Seed]:
    """Extract (setup, query) pairs from one PostgreSQL regression .sql file."""
    setup: list[str] = []
    seeds: list[Seed] = []
    for stmt in _split_statements(text):
        if _SKIP_SQL.match(stmt):
            continue
        if _SETUP_SQL.match(stmt):
            setup.append(_clean(stmt))
        elif _SELECT_START.match(stmt) and setup:
            seeds.append(
                Seed(
                    setup_sqls=list(setup),
                    query=_clean(stmt),
                    source=source,
                    engine="postgres",
                )
            )
    return seeds


def parse_seed_file(text: str, source: str, engine: str) -> list[Seed]:
    if engine == "postgres":
        return parse_pg_regress(text, source)
    return parse_duckdb_test(text, source)
