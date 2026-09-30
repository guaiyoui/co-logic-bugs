"""Unit tests for the deterministic oracles."""

from oracles.db_runner import DuckDBRunner
from oracles.equivalence import EquivalenceOracle
from oracles.models import KIND_EQUIV, KIND_ERROR_MISMATCH, KIND_TLP
from oracles.tlp import TLPOracle, tlp_applicable


SCHEMA = [
    "CREATE TABLE t (id INTEGER, x DOUBLE, s VARCHAR)",
    "INSERT INTO t VALUES (1, 0.5, 'a'), (2, NULL, ''), (3, -1.25, NULL), (3, -1.25, NULL)",
]
INSERTS = []


def make_runner() -> DuckDBRunner:
    runner = DuckDBRunner(version_tag="test")
    runner.setup(SCHEMA)
    return runner


def test_tlp_no_report_on_correct_query():
    runner = make_runner()
    hit = TLPOracle().check(
        runner, "SELECT id, x FROM t", "x > 0", SCHEMA, INSERTS, category="test"
    )
    assert hit is None
    runner.close()


def test_tlp_partition_covers_nulls():
    # Rows where x IS NULL must appear via the (p) IS NULL partition.
    runner = make_runner()
    hit = TLPOracle().check(
        runner, "SELECT id, s FROM t", "s LIKE 'a%'", SCHEMA, INSERTS, category="test"
    )
    assert hit is None
    runner.close()


def test_tlp_skips_aggregates_and_distinct():
    assert not tlp_applicable("SELECT count(*) FROM t", "x > 0")
    assert not tlp_applicable("SELECT DISTINCT id FROM t", "x > 0")
    assert not tlp_applicable("SELECT id FROM t GROUP BY id", "x > 0")
    assert not tlp_applicable("SELECT id FROM t", None)
    assert tlp_applicable("SELECT id FROM t", "x > 0")


def test_equivalence_detects_three_valued_logic_difference():
    # NOT (x <= 0) is NOT equivalent to x > 0 OR x IS NULL under 3VL:
    # when x is NULL, x<=0 is NULL, NOT NULL is NULL, and the row is filtered.
    runner = make_runner()
    hit = EquivalenceOracle().check(
        runner,
        "SELECT id FROM t WHERE x > 0 OR x IS NULL",
        "SELECT id FROM t WHERE NOT (x <= 0)",
        SCHEMA,
        INSERTS,
        category="test",
        rewrite_kind="demorgan",
    )
    assert hit is not None and hit.kind == KIND_EQUIV
    runner.close()


def test_equivalence_pass_for_truly_equivalent():
    runner = make_runner()
    hit = EquivalenceOracle().check(
        runner,
        "SELECT id FROM t WHERE x > -2",
        "SELECT id FROM t WHERE NOT (x <= -2 OR x IS NULL)",
        SCHEMA,
        INSERTS,
        category="test",
    )
    assert hit is None
    runner.close()


def test_equivalence_flags_error_mismatch():
    runner = make_runner()
    hit = EquivalenceOracle().check(
        runner,
        "SELECT id FROM t",
        "SELECT id FROM missing_table",
        SCHEMA,
        INSERTS,
        category="test",
    )
    assert hit is not None and hit.kind == KIND_ERROR_MISMATCH
    runner.close()


def test_equivalence_silent_when_both_fail():
    runner = make_runner()
    hit = EquivalenceOracle().check(
        runner,
        "SELECT bad FROM missing1",
        "SELECT bad FROM missing2",
        SCHEMA,
        INSERTS,
    )
    assert hit is None
    runner.close()


def test_tlp_reports_when_partition_union_differs(monkeypatch):
    """Simulate an engine bug by stubbing one partition's result."""
    runner = make_runner()
    oracle = TLPOracle()
    calls = []

    real_run = runner.run

    def fake_run(sql, timeout_s=10.0):
        result = real_run(sql, timeout_s=timeout_s)
        calls.append(sql)
        if "IS NULL" in sql and "WHERE" in sql:
            result.rows = []  # pretend the engine drops the IS NULL partition
        return result

    monkeypatch.setattr(runner, "run", fake_run)
    hit = oracle.check(
        runner, "SELECT id, x FROM t", "x > 0", SCHEMA, INSERTS, category="test"
    )
    assert hit is not None and hit.kind == KIND_TLP
    assert len(calls) == 4
    runner.close()
