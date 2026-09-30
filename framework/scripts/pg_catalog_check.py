"""Catalog-invariant checker for PostgreSQL DDL/DML churn.

After each seeded random workload round (60-120 DDL+DML statements), this
script asserts a set of catalog invariants that must ALWAYS hold. Any
nonzero count is a candidate finding:

a. ``pg_index`` vs ``pg_class``: every ``indexrelid``/``indrelid`` points at
   an existing ``pg_class`` row; no ``indisready=false`` indexes survive.
b. ``pg_index.indisvalid``: no invalid indexes remain, EXCEPT those whose
   ``CREATE INDEX CONCURRENTLY`` we observed fail this round (expected PG
   behaviour — tracked by index name and excluded as harness artifacts).
c. ``pg_constraint`` CHECK: every ``convalidated`` + enforced constraint is
   verified by ``SELECT count(*) FROM ONLY t WHERE NOT (<expr>)``. Nonzero
   = real finding. NOT ENFORCED constraints (PG 18+, ``conenforced=false``)
   are scanned too but reported under a separate label. A violation on an
   enforced constraint whose ancestor (``conparentid``) is NOT ENFORCED is
   labelled ``known_not_enforced_inherit_bug`` — the control case that is
   expected to fire on 18+/master.
d. Orphans: ``pg_constraint.conkey`` must not reference ``attisdropped``
   columns; ``pg_depend`` entries must not reference deleted ``pg_class``
   objects (either direction, user objects only).
e. Sequences: ``is_called=false`` implies ``last_value = seqstart``.
f. Partitions: every ``relispartition`` class has exactly one ``pg_inherits``
   parent; every ``pg_inherits`` parent/child pair has sane relkinds.
g. ``pg_get_expr`` must parse every ``conbin``/``indexprs``/``indpred``/
   ``adbin``/``relpartbound`` — an error means a dangling function/type ref.
h. ``pg_statistic_ext.stxrelid`` must point at a live ``pg_class`` row.

All workload statements are logged per round so any finding can be replayed
(``round_N_workload.sql``). Deterministic via ``--seed``.

Usage:
    COEVO_PG_PREFIX=/path/to/prefix \
        python scripts/pg_catalog_check.py --seed 1 --rounds 8 \
            --out results/pg_catalog_check_pg166
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from targets.postgres_runner import PostgresRunner  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("pg_catalog_check")


# =============================================================== workload
class Workload:
    """Seeded random DDL+DML churn generator.

    Tracks live objects optimistically (only on observed success) so most
    generated statements are legal; a small fraction are deliberately
    illegal to exercise error paths.
    """

    def __init__(self, rng: random.Random, major: int):
        self.rng = rng
        self.major = major
        self.seq = 0  # unique name counter
        # live state — updated by the driver from observed outcomes
        self.tables: dict[str, dict[str, Any]] = {}
        self.indexes: set[str] = set()
        self.views: set[str] = set()
        self.matviews: set[str] = set()
        self.sequences: set[str] = set()
        self.types: set[str] = set()
        self.functions: set[str] = set()
        self.constraints: dict[str, set[str]] = {}  # table -> constraint names

    # ------------------------------------------------------------ helpers
    def _name(self, prefix: str) -> str:
        self.seq += 1
        return f"cat_{prefix}{self.seq}"

    def _table(self) -> str | None:
        return self.rng.choice(sorted(self.tables)) if self.tables else None

    def _plain_table(self) -> str | None:
        plain = [t for t, m in sorted(self.tables.items())
                 if not m["partitioned"] and not m["partition"]]
        return self.rng.choice(plain) if plain else None

    def _partitioned(self) -> str | None:
        parts = [t for t, m in sorted(self.tables.items()) if m["partitioned"]]
        return self.rng.choice(parts) if parts else None

    def _coldef(self) -> str:
        typ = self.rng.choice(["int", "int", "bigint", "text", "numeric",
                               "int not null"])
        return f"c{self.rng.randint(1, 6)} {typ}"

    # ---------------------------------------------------------- generators
    def gen(self) -> str:
        """Return one workload statement."""
        r = self.rng.random()
        # ~8% deliberate failures
        if r < 0.08:
            return self._gen_bad()
        choices = [
            (self._create_table, 14),
            (self._create_partition_table, 6),
            (self._drop_table, 6),
            (self._alter_table, 14),
            (self._create_index, 8),
            (self._drop_index, 3),
            (self._add_constraint, 8),
            (self._drop_constraint, 3),
            (self._view_mv, 5),
            (self._dml, 14),
            (self._truncate, 2),
            (self._misc_objects, 6),
            (self._not_enforced_scenario, 7 if self.major >= 18 else 0),
            (self._cic_fail_scenario, 3),
            (self._txn_block, 4),
        ]
        pool = [(fn, w) for fn, w in choices if w > 0]
        total = sum(w for _, w in pool)
        pick = self.rng.uniform(0, total)
        acc = 0.0
        for fn, w in pool:
            acc += w
            if pick <= acc:
                return fn()
        return self._create_table()

    # ------------------------------------------------------- legal stmts
    def _create_table(self) -> str:
        n = self._name("t")
        cols = ", ".join(self._coldef() for _ in range(
            self.rng.randint(1, 4)))
        style = self.rng.random()
        if style < 0.18:
            # plain-inheritance child of an existing table
            parent = self._table()
            if parent:
                return f"CREATE TABLE {n}() INHERITS ({parent})"
        if style < 0.45:
            return (f"CREATE TABLE {n}(a int, {cols}) "
                    f"PARTITION BY RANGE (a)")
        extras = []
        if self.rng.random() < 0.25:
            extras.append(f"CONSTRAINT {n}_chk CHECK (c1 > 0)")
        if self.rng.random() < 0.15:
            extras.append("PRIMARY KEY (c1)")
        body = ", ".join([cols] + extras)
        return f"CREATE TABLE {n}({body})"

    def _create_partition_table(self) -> str:
        parent = self._partitioned()
        if not parent:
            return self._create_table()
        n = self._name("p")
        m = self.tables[parent]
        lo = m.setdefault("next_bound", 0)
        hi = lo + self.rng.choice([50, 100, 200])
        m["next_bound"] = hi
        if self.rng.random() < 0.5:
            return (f"CREATE TABLE {n} PARTITION OF {parent} "
                    f"FOR VALUES FROM ({lo}) TO ({hi})")
        return (f"CREATE TABLE {n} PARTITION OF {parent} "
                f"FOR VALUES FROM ({lo}) TO ({hi}) PARTITION BY RANGE (c2)")

    def _drop_table(self) -> str:
        t = self._table()
        if not t:
            return self._create_table()
        cascade = " CASCADE" if self.rng.random() < 0.4 else ""
        return f"DROP TABLE {t}{cascade}"

    def _alter_table(self) -> str:
        t = self._table()
        if not t:
            return self._create_table()
        ops = [
            f"ALTER TABLE {t} ADD COLUMN {self._coldef()}",
            f"ALTER TABLE {t} DROP COLUMN IF EXISTS "
            f"c{self.rng.randint(1, 6)}",
            f"ALTER TABLE {t} ALTER COLUMN c1 TYPE bigint",
            f"ALTER TABLE {t} ALTER COLUMN c1 SET NOT NULL",
            f"ALTER TABLE {t} ALTER COLUMN c1 DROP NOT NULL",
        ]
        parent = self._partitioned()
        if parent:
            spare = self._plain_table()
            if spare and spare != parent:
                m = self.tables[parent]
                lo = m.setdefault("next_bound", 0)
                hi = lo + self.rng.choice([50, 100])
                m["next_bound"] = hi
                ops.append(
                    f"ALTER TABLE {parent} ATTACH PARTITION {spare} "
                    f"FOR VALUES FROM ({lo}) TO ({hi})")
            part = [c for c, mm in sorted(self.tables.items())
                    if mm.get("parent") == parent]
            if part:
                ops.append(
                    f"ALTER TABLE {parent} DETACH PARTITION "
                    f"{self.rng.choice(part)}")
        return self.rng.choice(ops)

    def _create_index(self) -> str:
        t = self._table()
        if not t:
            return self._create_table()
        n = self._name("i")
        unique = "UNIQUE " if self.rng.random() < 0.25 else ""
        concurrently = "CONCURRENTLY " if self.rng.random() < 0.45 else ""
        col = self.rng.choice(["c1", "c2", "a", "c3"])
        pred = " WHERE c1 > 0" if self.rng.random() < 0.2 else ""
        return f"CREATE {unique}INDEX {concurrently}{n} ON {t}({col}){pred}"

    def _drop_index(self) -> str:
        if not self.indexes:
            return self._create_index()
        n = self.rng.choice(sorted(self.indexes))
        conc = "CONCURRENTLY " if self.rng.random() < 0.4 else ""
        return f"DROP INDEX {conc}{n}"

    def _add_constraint(self) -> str:
        t = self._plain_table()
        if not t:
            return self._create_table()
        n = self._name("c")
        kind = self.rng.random()
        if kind < 0.4:
            ne = (" NOT ENFORCED" if self.major >= 18
                  and self.rng.random() < 0.35 else "")
            return (f"ALTER TABLE {t} ADD CONSTRAINT {n} "
                    f"CHECK (c1 > -1000000){ne}")
        if kind < 0.6:
            return (f"ALTER TABLE {t} ADD CONSTRAINT {n} "
                    f"CHECK (c1 > 0) NOT VALID")
        if kind < 0.8:
            return f"ALTER TABLE {t} ADD CONSTRAINT {n} UNIQUE (c1)"
        other = self._plain_table()
        if other and other != t:
            return (f"ALTER TABLE {t} ADD CONSTRAINT {n} "
                    f"FOREIGN KEY (c1) REFERENCES {other}(c1)")
        return f"ALTER TABLE {t} ADD CONSTRAINT {n} CHECK (c2 IS NOT NULL)"

    def _drop_constraint(self) -> str:
        t = self._table()
        if not t or not self.constraints.get(t):
            return self._add_constraint()
        n = self.rng.choice(sorted(self.constraints[t]))
        return f"ALTER TABLE {t} DROP CONSTRAINT {n}"

    def _view_mv(self) -> str:
        r = self.rng.random()
        if r < 0.25 and self.views:
            return f"DROP VIEW {self.rng.choice(sorted(self.views))}"
        if r < 0.4 and self.matviews:
            return (f"DROP MATERIALIZED VIEW "
                    f"{self.rng.choice(sorted(self.matviews))}")
        t = self._table()
        if not t:
            return self._create_table()
        n = self._name("v")
        if r < 0.7:
            return f"CREATE VIEW {n} AS SELECT * FROM {t}"
        return (f"CREATE MATERIALIZED VIEW {n} AS "
                f"SELECT count(*)::int AS k FROM {t}")

    def _dml(self) -> str:
        t = self._table()
        if not t:
            return self._create_table()
        r = self.rng.random()
        if r < 0.5:
            cols = [c for c in self.tables[t]["cols"]][:self.rng.randint(1, 3)]
            vals = ", ".join(str(self.rng.randint(-100, 500))
                             for _ in cols)
            return f"INSERT INTO {t}({', '.join(cols)}) VALUES ({vals})"
        if r < 0.7:
            return (f"INSERT INTO {t}(c1) SELECT i FROM "
                    f"generate_series(1, {self.rng.randint(1, 30)}) i")
        if r < 0.85:
            return (f"UPDATE {t} SET c1 = c1 + 1 WHERE c1 = "
                    f"{self.rng.randint(0, 100)}")
        return f"DELETE FROM {t} WHERE c1 > {self.rng.randint(0, 400)}"

    def _truncate(self) -> str:
        t = self._table()
        return f"TRUNCATE {t}" if t else self._create_table()

    def _misc_objects(self) -> str:
        r = self.rng.random()
        if r < 0.3:
            n = self._name("ty")
            if self.rng.random() < 0.5:
                return f"CREATE TYPE {n} AS ENUM ('a','b','c')"
            return f"CREATE TYPE {n} AS (x int, y text)"
        if r < 0.55:
            n = self._name("f")
            return (f"CREATE FUNCTION {n}(int) RETURNS int "
                    f"LANGUAGE sql IMMUTABLE AS 'SELECT $1 + 1'")
        if r < 0.75:
            n = self._name("s")
            start = self.rng.randint(1, 1000)
            return f"CREATE SEQUENCE {n} START {start}"
        if r < 0.9:
            t = self._table()
            if t:
                return f"COMMENT ON TABLE {t} IS 'coevo churn'"
        n = self._name("s2")
        return f"CREATE SEQUENCE {n} START {self.rng.randint(1, 1000)}"

    # ---------------------------------------------- scenario generators
    def _not_enforced_scenario(self) -> str:
        """Emit one step of the NOT-ENFORCED-inherit control scenario.

        Kept as a single statement per ``gen()`` call so the driver log
        stays linear; state machine driven by ``self._ne_stage``.
        The CHECK is ``a < 100050`` over a 100000..100100 range partition
        so violating values (100075/100080) still satisfy the bound.
        A violation on a child constraint that the catalog marks
        enforced+validated is the known PG18 NOT-ENFORCED inherit bug
        (control case).
        """
        stage = getattr(self, "_ne_stage", 0)
        flavour = getattr(self, "_ne_flavour", 0)
        steps = 5
        self._ne_stage = (stage + 1) % steps
        if stage == 0:
            self._ne_flavour = self.rng.randint(0, 1)
            if flavour:
                return "CREATE TABLE ne_parent(a int)"
            return ("CREATE TABLE ne_parent(a int) "
                    "PARTITION BY RANGE (a)")
        if stage == 1:
            if flavour:
                return "CREATE TABLE ne_child() INHERITS (ne_parent)"
            # high bound range so random partitions (bounds grow from 0)
            # never overlap us
            return ("CREATE TABLE ne_child PARTITION OF ne_parent "
                    "FOR VALUES FROM (100000) TO (100100)")
        if stage == 2:
            return ("ALTER TABLE ne_parent ADD CONSTRAINT ne_chk "
                    "CHECK (a < 100050) NOT ENFORCED")
        if stage == 3:
            # violating rows land directly in the child: allowed by NOT
            # ENFORCED on the parent; the child's inherited copy must
            # stay non-enforced or the catalog claims a constraint that
            # data breaks.
            return "INSERT INTO ne_child VALUES (100075), (100080)"
        # VALIDATE on a NOT ENFORCED parent scans the children; failure
        # path must not leave the child's copy half-validated.
        return "ALTER TABLE ne_parent VALIDATE CONSTRAINT ne_chk"

    def _cic_fail_scenario(self) -> str:
        """CREATE UNIQUE INDEX CONCURRENTLY doomed to fail (dup values).

        Leaves an invalid index — expected PG behaviour, tracked by name
        and excluded from the invalid-index finding.
        """
        stage = getattr(self, "_cic_stage", 0)
        self._cic_stage = (stage + 1) % 3
        if stage == 0:
            return "CREATE TABLE cic_t(a int)"
        if stage == 1:
            return "INSERT INTO cic_t VALUES (1),(1),(2)"
        return "CREATE UNIQUE INDEX CONCURRENTLY cic_bad_idx ON cic_t(a)"

    def _txn_block(self) -> str:
        """Multi-statement txn incl. a failing DDL — tests abort cleanup."""
        t = self._table()
        inner = [
            "BEGIN",
            f"INSERT INTO {t}(c1) VALUES (1)" if t else "SELECT 1",
            "ALTER TABLE nonexistent_tbl ADD COLUMN x int",  # fails
            "ROLLBACK",
        ]
        stage = getattr(self, "_txn_stage", 0)
        self._txn_stage = (stage + 1) % len(inner)
        return inner[stage]

    # ------------------------------------------------------ bad statements
    def _gen_bad(self) -> str:
        """Deliberately failing statement — exercises error paths."""
        bads = [
            "DROP TABLE no_such_table_xyz",
            "DROP INDEX no_such_index_xyz",
            "ALTER TABLE no_such_table_xyz ADD COLUMN q int",
            "DROP SEQUENCE no_such_seq_xyz",
            "DROP FUNCTION no_such_fn_xyz(int)",
            "DROP TYPE no_such_type_xyz",
            "CREATE TABLE bad_##name(x int)",
        ]
        t = self._table()
        if t:
            bads += [
                f"ALTER TABLE {t} DROP CONSTRAINT no_such_con",
                f"ALTER TABLE {t} ATTACH PARTITION no_such_part "
                f"FOR VALUES FROM (0) TO (10)",
            ]
        return self.rng.choice(bads)


# --------------------------------------------------------- state tracking
_CIC_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(\w+)", re.IGNORECASE)
_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_DROP_TABLE_RE = re.compile(
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_PART_OF_RE = re.compile(r"PARTITION\s+OF\s+(\w+)", re.IGNORECASE)
_ADDCON_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+CONSTRAINT\s+(\w+)", re.IGNORECASE)
_DROPCON_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE)
_SEQ_RE = re.compile(
    r"CREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_COLS_RE = re.compile(r"INSERT\s+INTO\s+\w+\(([^)]+)\)", re.IGNORECASE)


_COL_KEYWORDS = {"constraint", "primary", "unique", "check", "foreign",
                 "like", "exclude", "partition", "inherits"}


def _parse_cols(sql: str) -> list[str]:
    """Extract column names from the CREATE TABLE column list."""
    m = re.search(r"\((.*)\)", sql, re.DOTALL)
    if not m:
        return []
    body = re.split(r"PARTITION\s+BY", m.group(1),
                    flags=re.IGNORECASE)[0]
    cols = []
    depth = 0
    for part in re.split(r"(,|\(|\))", body):
        part = part.strip()
        if part == "(":
            depth += 1
        elif part == ")":
            depth -= 1
        elif part and part != "," and depth == 0:
            tok = part.split()[0].strip('"').lower()
            if re.match(r"^[a-z_]\w*$", tok) and tok not in _COL_KEYWORDS:
                cols.append(tok)
    return cols


def track_success(wl: Workload, sql: str) -> None:
    """Update optimistic object pool after a statement succeeded."""
    m = _TABLE_RE.match(sql)
    if m:
        name = m.group(1)
        parent_m = _PART_OF_RE.search(sql)
        inh_m = re.search(r"INHERITS\s*\(\s*(\w+)", sql, re.IGNORECASE)
        partitioned = "PARTITION BY" in sql.upper()
        cols = _parse_cols(sql)
        ref = (parent_m.group(1) if parent_m
               else inh_m.group(1) if inh_m else None)
        if not cols and ref in wl.tables:
            cols = list(wl.tables[ref]["cols"])
        wl.tables[name] = {
            "partitioned": partitioned,
            "partition": parent_m is not None,
            "parent": ref,
            "cols": cols or ["c1", "a"],
            "next_bound": 0,
        }
        wl.constraints.setdefault(name, set())
    m = re.match(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
                 sql, re.IGNORECASE)
    if m and m.group(1) in wl.tables:
        wl.tables[m.group(1)]["cols"].append(m.group(2))
    m = re.match(r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+"
                 r"(?:IF\s+EXISTS\s+)?(\w+)", sql, re.IGNORECASE)
    if m and m.group(1) in wl.tables:
        try:
            wl.tables[m.group(1)]["cols"].remove(m.group(2))
        except ValueError:
            pass
    m = _DROP_TABLE_RE.match(sql)
    if m and m.group(1) in wl.tables:
        del wl.tables[m.group(1)]
    m = _INDEX_RE.match(sql)
    if m:
        wl.indexes.add(m.group(1))
    m = re.match(r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?"
                 r"(?:IF\s+EXISTS\s+)?(\w+)", sql, re.IGNORECASE)
    if m:
        wl.indexes.discard(m.group(1))
    m = _ADDCON_RE.search(sql)
    if m:
        wl.constraints.setdefault(m.group(1), set()).add(m.group(2))
    m = _DROPCON_RE.search(sql)
    if m:
        wl.constraints.get(m.group(1), set()).discard(m.group(2))
    m = _SEQ_RE.match(sql)
    if m:
        wl.sequences.add(m.group(1))
    m = re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)",
                 sql, re.IGNORECASE)
    if m:
        wl.views.add(m.group(1))
    m = re.match(r"DROP\s+VIEW\s+(?:IF\s+EXISTS\s+)?(\w+)",
                 sql, re.IGNORECASE)
    if m:
        wl.views.discard(m.group(1))
    m = re.match(r"CREATE\s+MATERIALIZED\s+VIEW\s+(\w+)",
                 sql, re.IGNORECASE)
    if m:
        wl.matviews.add(m.group(1))
    m = re.match(r"DROP\s+MATERIALIZED\s+VIEW\s+(?:IF\s+EXISTS\s+)?(\w+)",
                 sql, re.IGNORECASE)
    if m:
        wl.matviews.discard(m.group(1))


# ============================================================= invariants
def _count(res) -> int | None:
    if not res.ok or not res.rows:
        return None
    return int(res.rows[0][0])


def run_invariants(pg: PostgresRunner, major: int,
                   failed_cic: set[str]) -> list[dict]:
    """Run every catalog invariant; return a list of finding dicts."""
    findings: list[dict] = []

    def add(kind: str, severity: str, query: str, detail: Any,
            note: str = ""):
        findings.append({
            "kind": kind, "severity": severity, "query": query,
            "detail": detail, "note": note,
        })

    # ---- scalar count invariants: (name, sql, severity, note) ----------
    checks: list[tuple[str, str, str, str]] = [
        ("index_relid_orphan",
         "SELECT count(*) FROM pg_index i "
         "LEFT JOIN pg_class c ON c.oid = i.indexrelid "
         "WHERE c.oid IS NULL",
         "real", "pg_index.indexrelid without pg_class row"),
        ("index_indrelid_orphan",
         "SELECT count(*) FROM pg_index i "
         "LEFT JOIN pg_class c ON c.oid = i.indrelid "
         "WHERE c.oid IS NULL",
         "real", "pg_index.indrelid without pg_class row"),
        ("index_wrong_relkind",
         "SELECT count(*) FROM pg_index i JOIN pg_class c "
         "ON c.oid = i.indexrelid WHERE c.relkind NOT IN ('i','I')",
         "real", "pg_index row whose class is not an index relkind"),
        ("conkey_dropped_col",
         "SELECT count(*) FROM pg_constraint c "
         "WHERE c.conkey IS NOT NULL AND EXISTS ("
         "  SELECT 1 FROM pg_attribute a "
         "  WHERE a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)"
         "    AND a.attisdropped)",
         "real", "constraint conkey references a dropped column"),
        ("conrelid_orphan",
         "SELECT count(*) FROM pg_constraint c "
         "LEFT JOIN pg_class r ON r.oid = c.conrelid "
         "WHERE c.conrelid <> 0 AND r.oid IS NULL",
         "real", "pg_constraint.conrelid without pg_class row"),
        ("depend_objid_orphan",
         "SELECT count(*) FROM pg_depend d "
         "WHERE d.classid = 'pg_class'::regclass AND d.objid > 16383 "
         "AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = d.objid)",
         "real", "pg_depend.objid references deleted pg_class"),
        ("depend_refobjid_orphan",
         "SELECT count(*) FROM pg_depend d "
         "WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid > 16383 "
         "AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = d.refobjid)",
         "real", "pg_depend.refobjid references deleted pg_class"),
        ("partition_no_parent",
         "SELECT count(*) FROM pg_class c "
         "WHERE c.relispartition AND NOT EXISTS ("
         "  SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid)",
         "real", "relispartition class missing from pg_inherits"),
        ("partition_multi_parent",
         "SELECT count(*) FROM (SELECT i.inhrelid FROM pg_inherits i "
         "JOIN pg_class c ON c.oid = i.inhrelid "
         "WHERE c.relispartition "
         "GROUP BY i.inhrelid HAVING count(*) > 1) x",
         "real", "partition with more than one pg_inherits parent"),
        ("inherits_bad_parent_relkind",
         "SELECT count(*) FROM pg_inherits i JOIN pg_class p "
         "ON p.oid = i.inhparent WHERE p.relkind NOT IN ('r','p','I')",
         "real", "pg_inherits parent is not a table/partitioned "
                 "table/partitioned index"),
        ("inherits_bad_child_relkind",
         "SELECT count(*) FROM pg_inherits i JOIN pg_class ch "
         "ON ch.oid = i.inhrelid "
         "WHERE ch.relkind NOT IN ('r','p','i','I','f')",
         "real", "pg_inherits child is not a table/partitioned "
                 "table/index/partitioned index/foreign table"),
        ("inherits_orphan_child",
         "SELECT count(*) FROM pg_inherits i "
         "LEFT JOIN pg_class ch ON ch.oid = i.inhrelid "
         "WHERE ch.oid IS NULL",
         "real", "pg_inherits.inhrelid without pg_class row"),
        ("inherits_orphan_parent",
         "SELECT count(*) FROM pg_inherits i "
         "LEFT JOIN pg_class p ON p.oid = i.inhparent "
         "WHERE p.oid IS NULL",
         "real", "pg_inherits.inhparent without pg_class row"),
        ("statistic_ext_orphan",
         "SELECT count(*) FROM pg_statistic_ext s "
         "LEFT JOIN pg_class c ON c.oid = s.stxrelid "
         "WHERE c.oid IS NULL",
         "real", "pg_statistic_ext.stxrelid without pg_class row"),
        ("attrdef_orphan",
         "SELECT count(*) FROM pg_attrdef d "
         "LEFT JOIN pg_class c ON c.oid = d.adrelid "
         "WHERE c.oid IS NULL",
         "real", "pg_attrdef.adrelid without pg_class row"),
        ("trigger_orphan",
         "SELECT count(*) FROM pg_trigger t "
         "LEFT JOIN pg_class c ON c.oid = t.tgrelid "
         "WHERE c.oid IS NULL",
         "real", "pg_trigger.tgrelid without pg_class row"),
        ("rewrite_orphan",
         "SELECT count(*) FROM pg_rewrite w "
         "LEFT JOIN pg_class c ON c.oid = w.ev_class "
         "WHERE c.oid IS NULL",
         "real", "pg_rewrite.ev_class without pg_class row"),
        ("dropped_col_has_default",
         "SELECT count(*) FROM pg_attribute a JOIN pg_attrdef d "
         "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
         "WHERE a.attisdropped",
         "real", "dropped column still has pg_attrdef default"),
        ("partition_bound_orphan",
         "SELECT count(*) FROM pg_class c "
         "WHERE c.relpartbound IS NOT NULL AND NOT c.relispartition "
         "AND c.relkind = 'r'",
         "info", "non-partition plain table carries relpartbound "
                 "(can be legit after DETACH — informational)"),
    ]
    for name, sql, severity, note in checks:
        res = pg.run(sql, timeout_s=20.0)
        eff.count("invariant_queries")
        if not res.ok:
            add(f"{name}_query_error", "harness", sql, res.error,
                "invariant query itself failed")
            continue
        n = int(res.rows[0][0])
        if n:
            detail_q = sql.replace("count(*)", "*")
            detail = pg.run(detail_q, timeout_s=20.0)
            rows = ([list(r) for r in detail.rows][:20]
                    if detail.ok else str(detail.error))
            add(name, severity, sql, {"count": n, "rows": rows}, note)

    # ---- non-indisready indexes (failed-CIC names are artifacts) -------
    q = ("SELECT i.indexrelid::regclass::text, i.indrelid::regclass::text "
         "FROM pg_index i WHERE NOT i.indisready ORDER BY 1")
    res = pg.run(q, timeout_s=20.0)
    if res.ok and res.rows:
        real, expected = [], []
        for idx, tbl in res.rows:
            bare = str(idx).split(".")[-1]
            (expected if bare in failed_cic else real).append(
                {"index": idx, "table": tbl})
        if expected:
            add("not_ready_index_expected", "artifact", q, expected,
                "failed CREATE INDEX CONCURRENTLY leaves "
                "indisready=false — expected, excluded")
        if real:
            add("index_not_ready", "real", q, real,
                "index left non-indisready (blocks inserts)")
    elif not res.ok:
        add("index_not_ready_query_error", "harness", q, res.error)

    # ---- invalid indexes (excluding tracked failed CIC builds) ---------
    q = ("SELECT i.indexrelid::regclass::text, i.indrelid::regclass::text "
         "FROM pg_index i WHERE NOT i.indisvalid ORDER BY 1")
    res = pg.run(q, timeout_s=20.0)
    if res.ok and res.rows:
        real, expected = [], []
        for idx, tbl in res.rows:
            bare = str(idx).split(".")[-1]
            (expected if bare in failed_cic else real).append(
                {"index": idx, "table": tbl})
        if expected:
            add("invalid_index_expected", "artifact", q, expected,
                "failed CREATE INDEX CONCURRENTLY leaves invalid index — "
                "expected PG behaviour, excluded from violations")
        if real:
            add("invalid_index", "real", q, real,
                "invalid index not attributable to a tracked failed CIC")
    elif not res.ok:
        add("invalid_index_query_error", "harness", q, res.error)

    # ---- CHECK-constraint verification scans ---------------------------
    extra = ("c.conenforced, c.coninhcount"
             if major >= 18 else "true, 0")
    q = (f"SELECT c.oid, c.conname, c.conrelid, "
         f"c.conrelid::regclass::text, c.convalidated, {extra}, "
         f"pg_get_expr(c.conbin, c.conrelid) "
         f"FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
         f"WHERE c.contype = 'c' ORDER BY c.oid")
    res = pg.run(q, timeout_s=30.0)
    if not res.ok:
        add("constraint_catalog_error", "harness", q, res.error,
            "pg_get_expr over pg_constraint failed — dangling function "
            "reference is itself a finding")
    else:
        # child table -> parent tables, for the NOT-ENFORCED inherit label
        parents: dict[int, list[int]] = {}
        inh = pg.run("SELECT inhrelid, inhparent FROM pg_inherits",
                     timeout_s=20.0)
        if inh.ok:
            for child, parent in inh.rows:
                parents.setdefault(int(child), []).append(int(parent))
        # (conrelid, conname) -> enforced flag, for parent lookup
        enforced_by_relname: dict[tuple[int, str], bool] = {
            (int(r[2]), str(r[1])): bool(r[5]) for r in res.rows}
        for row in res.rows:
            (oid, conname, conrelid, rel, validated, enforced,
             inhcount, expr) = row[:8]
            # Scan constraints the catalog claims hold (validated) plus
            # NOT ENFORCED ones (reported separately per checklist).
            # Enforced + NOT VALID constraints legitimately allow
            # violating rows — skip them.
            if not validated and enforced:
                continue
            scan = (f"SELECT count(*) FROM ONLY {rel} "
                    f"WHERE NOT ({expr})")
            sres = pg.run(scan, timeout_s=20.0)
            eff.count("constraint_scans")
            if not sres.ok:
                add("check_scan_error", "real", scan, sres.error,
                    f"verification scan failed for constraint "
                    f"{conname} on {rel} — catalog expression broken?")
                continue
            n = int(sres.rows[0][0])
            if n == 0:
                continue
            detail = {"constraint": conname, "table": rel,
                      "violations": n, "expr": expr,
                      "enforced": bool(enforced),
                      "inhcount": inhcount}
            if not enforced:
                add("not_enforced_violation", "expected", scan, detail,
                    "NOT ENFORCED constraint — violating rows are legal; "
                    "reported separately per checklist")
                continue
            # enforced constraint with violating rows: check whether an
            # ancestor's same-named constraint is NOT ENFORCED — that is
            # the known PG18 NOT-ENFORCED-inherit bug (control case).
            known = False
            if major >= 18 and inhcount:
                for prelid in parents.get(int(conrelid), []):
                    penf = enforced_by_relname.get((prelid, str(conname)))
                    if penf is False:
                        known = True
                        detail["parent_relid"] = prelid
                        break
            if known:
                add("known_not_enforced_inherit_bug", "known_bug", scan,
                    detail, "enforced+validated constraint inherited from "
                    "a NOT ENFORCED parent — the known PG18 NOT-ENFORCED "
                    "inherit bug (control case)")
            else:
                add("constraint_violated", "real", scan, detail,
                    "validated+enforced CHECK constraint does not hold")

    # ---- sequence last_value consistency -------------------------------
    q = ("SELECT c.oid::regclass::text, s.seqstart "
         "FROM pg_sequence s JOIN pg_class c ON c.oid = s.seqrelid "
         "ORDER BY 1")
    res = pg.run(q, timeout_s=20.0)
    if res.ok:
        for seqname, seqstart in res.rows:
            sres = pg.run(
                f"SELECT last_value, is_called FROM {seqname}",
                timeout_s=10.0)
            eff.count("sequence_scans")
            if not sres.ok:
                add("sequence_scan_error", "real",
                    f"SELECT last_value, is_called FROM {seqname}",
                    sres.error)
                continue
            last_value, is_called = sres.rows[0]
            if not is_called and int(last_value) != int(seqstart):
                add("sequence_bad_start", "real",
                    f"SELECT last_value, is_called FROM {seqname}",
                    {"sequence": seqname, "last_value": int(last_value),
                     "seqstart": int(seqstart)},
                    "is_called=false but last_value != seqstart")
    else:
        add("sequence_catalog_error", "harness", q, res.error)

    # ---- pg_get_expr over expression-bearing catalogs ------------------
    expr_checks = [
        ("conbin_deparse",
         "SELECT count(*) FROM (SELECT pg_get_expr(c.conbin, c.conrelid) "
         "FROM pg_constraint c WHERE c.conbin IS NOT NULL) x"),
        ("indexprs_deparse",
         "SELECT count(*) FROM (SELECT pg_get_expr(i.indexprs, i.indrelid) "
         "FROM pg_index i WHERE i.indexprs IS NOT NULL) x"),
        ("indpred_deparse",
         "SELECT count(*) FROM (SELECT pg_get_expr(i.indpred, i.indrelid) "
         "FROM pg_index i WHERE i.indpred IS NOT NULL) x"),
        ("adbin_deparse",
         "SELECT count(*) FROM (SELECT pg_get_expr(d.adbin, d.adrelid) "
         "FROM pg_attrdef d) x"),
        ("partbound_deparse",
         "SELECT count(*) FROM (SELECT pg_get_expr(c.relpartbound, c.oid) "
         "FROM pg_class c WHERE c.relpartbound IS NOT NULL) x"),
    ]
    for name, sql in expr_checks:
        res = pg.run(sql, timeout_s=30.0)
        eff.count("deparse_queries")
        if not res.ok:
            add(name, "real", sql, res.error,
                "pg_get_expr failed — dangling proc/type reference in "
                "a catalog expression")
    return findings


# ================================================================= driver
def summarize(findings: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        key = f"{f['severity']}:{f['kind']}"
        out[key] = out.get(key, 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--min-stmts", type=int, default=60)
    ap.add_argument("--max-stmts", type=int, default=120)
    ap.add_argument("--pg-prefix", default=None,
                    help="install prefix; falls back to COEVO_PG_PREFIX")
    ap.add_argument("--pg-datadir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    prefix = args.pg_prefix or os.environ.get("COEVO_PG_PREFIX")
    tag = (os.path.basename(str(prefix).rstrip("/"))
           if prefix else "embedded")
    out = args.out or f"results/pg_catalog_check_{tag}"
    datadir = args.pg_datadir or f"/tmp/coevo_pgcat_{tag}_{args.seed}"
    os.makedirs(out, exist_ok=True)

    pg = PostgresRunner(datadir,
                        statement_timeout_ms=int(args.timeout * 1000),
                        pg_prefix=prefix)
    all_findings: list[dict] = []
    stats = {"stmts": 0, "stmt_errors": 0, "internal_errors": 0,
             "rounds": 0, "invariant_queries": 0}
    t0 = time.time()
    try:
        ver = pg.run("SHOW server_version_num", timeout_s=10.0)
        vernum = int(ver.rows[0][0]) if ver.ok else 0
        major = vernum // 10000
        pg._version = pg.engine_version
        LOGGER.info("build=%s server_version_num=%d major=%d",
                    tag, vernum, major)

        for rnd in range(args.rounds):
            rng = random.Random(args.seed * 100003 + rnd)
            wl = Workload(rng, major)
            n_stmts = rng.randint(args.min_stmts, args.max_stmts)
            pg.setup([])  # reset public schema
            wl_log: list[dict] = []
            failed_cic: set[str] = set()
            for i in range(n_stmts):
                sql = wl.gen()
                res = pg.run(sql, timeout_s=args.timeout)
                stats["stmts"] += 1
                err = res.error if not res.ok else None
                if err:
                    stats["stmt_errors"] += 1
                if res.is_internal_error:
                    stats["internal_errors"] += 1
                    all_findings.append({
                        "kind": "workload_internal_error",
                        "severity": "real", "round": rnd, "stmt_idx": i,
                        "query": sql, "detail": res.error,
                    })
                m = _CIC_RE.match(sql)
                if m and not res.ok:
                    failed_cic.add(m.group(1))
                if res.ok:
                    track_success(wl, sql)
                wl_log.append({"i": i, "sql": sql, "ok": res.ok,
                               "error": err})
            stats["rounds"] += 1

            # A round may end inside an open/aborted txn_block txn —
            # clear it so invariant queries see committed catalog state.
            pg.run("ROLLBACK", timeout_s=5.0)
            findings = run_invariants(pg, major, failed_cic)
            for f in findings:
                f["round"] = rnd
                f["repro_file"] = f"round_{rnd}_workload.sql"
            all_findings.extend(findings)

            # per-round workload log (deterministic replay artefact)
            with open(os.path.join(
                    out, f"round_{rnd}_workload.sql"), "w") as fh:
                fh.write(f"-- round {rnd} seed {args.seed} build {tag}\n")
                for e in wl_log:
                    fh.write(f"{e['sql']};\n")
            with open(os.path.join(
                    out, f"round_{rnd}_workload.jsonl"), "w") as fh:
                for e in wl_log:
                    fh.write(json.dumps(e) + "\n")
            LOGGER.info("round %d: %d stmts, %d findings so far",
                        rnd, n_stmts, len(all_findings))
            fatals = pg.log_fatal_lines(pg.log_new_lines())
            for ln in fatals:
                all_findings.append({
                    "kind": "server_log_fatal", "severity": "real",
                    "round": rnd, "query": "(server log)",
                    "detail": ln})
    finally:
        pg.cleanup()

    with open(os.path.join(out, "findings.json"), "w") as fh:
        json.dump(all_findings, fh, indent=1, default=str)
    summary = {
        "build": tag,
        "pg_version": getattr(pg, "_version", "unknown"),
        "seed": args.seed, "rounds": args.rounds,
        **stats,
        "finding_kinds": summarize(all_findings),
        "elapsed_s": round(time.time() - t0, 1),
        "efficiency": eff.snapshot(),
    }
    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
