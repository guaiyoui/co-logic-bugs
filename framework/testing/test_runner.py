"""Unit tests for the DuckDB runner and result normalization."""

from oracles.db_runner import DuckDBRunner, bags_equal
from oracles.normalize import is_internal_error, normalize_rows


SCHEMA = [
    "CREATE TABLE t (id INTEGER, x DOUBLE, s VARCHAR)",
    "INSERT INTO t VALUES (1, 0.5, 'a'), (2, NULL, ''), (3, -1.25, NULL)",
]


def make_runner() -> DuckDBRunner:
    runner = DuckDBRunner(version_tag="test")
    runner.setup(SCHEMA)
    return runner


def test_run_returns_normalized_rows():
    runner = make_runner()
    result = runner.run("SELECT id, x, s FROM t")
    assert result.ok
    assert len(result.rows) == 3
    assert result.columns == ["id", "x", "s"]
    assert [2, None, ""] in result.rows
    runner.close()


def test_setup_reports_failing_statements():
    runner = DuckDBRunner()
    outcomes = runner.setup(
        ["CREATE TABLE ok (a INTEGER)", "CREATE TABLE broken ("]
    )
    assert outcomes[0][1] is None
    assert outcomes[1][1] is not None
    runner.close()


def test_query_error_is_captured_not_raised():
    runner = make_runner()
    result = runner.run("SELECT nope FROM t")
    assert not result.ok
    assert result.error
    assert not result.is_internal_error
    runner.close()


def test_internal_error_detection():
    assert is_internal_error("INTERNAL Error: assertion failed in foo.cpp")
    assert is_internal_error("Segmentation fault")
    assert not is_internal_error("Binder Error: table not found")
    assert not is_internal_error(None)


def test_bag_comparison_ignores_order_and_counts_duplicates():
    runner = make_runner()
    a = runner.run("SELECT id FROM t")
    b = runner.run("SELECT id FROM t ORDER BY id DESC")
    assert bags_equal(a, b)

    c = runner.run("SELECT id FROM t WHERE id != 3")
    d = runner.run("SELECT id FROM t WHERE id != 3")
    assert bags_equal(c, d)
    assert not bags_equal(a, c)
    runner.close()


def test_float_normalization_rounds_to_six_decimals():
    rows = normalize_rows([(1.123456789,)])
    assert rows == [[1.123457]]
