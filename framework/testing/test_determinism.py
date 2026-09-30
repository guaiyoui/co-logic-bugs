"""Unit tests for determinism screening, loose comparison, and root dedup."""

from oracles.db_runner import DuckDBRunner
from oracles.determinism import (
    has_nondeterministic_tiebreak,
    is_result_order_sensitive,
    is_usable_for_oracles,
)
from oracles.models import Candidate, root_signature
from oracles.normalize import loose_rows_equal, normalize_value

SCHEMA = [
    "CREATE TABLE t (id INTEGER, grp VARCHAR, x DOUBLE)",
    (
        "INSERT INTO t VALUES (1, 'a', 1.0), (2, 'a', 1.0), "
        "(3, 'b', 2.0), (4, 'b', 2.0)"
    ),
]


def make_runner() -> DuckDBRunner:
    runner = DuckDBRunner(version_tag="test")
    runner.setup(SCHEMA)
    return runner


def test_limit_without_order_by_is_sensitive():
    assert is_result_order_sensitive("SELECT id FROM t LIMIT 3")
    assert is_result_order_sensitive("SELECT id FROM t OFFSET 2 LIMIT 3")
    assert not is_result_order_sensitive("SELECT id FROM t ORDER BY id LIMIT 3")
    assert not is_result_order_sensitive("SELECT id FROM t")


def test_limit_with_unique_order_key_is_usable():
    runner = make_runner()
    sql = "SELECT id, x FROM t ORDER BY id LIMIT 3"
    assert not has_nondeterministic_tiebreak(sql, runner, SCHEMA)
    assert is_usable_for_oracles(sql, runner, SCHEMA)
    runner.close()


def test_limit_with_duplicate_order_key_is_nondeterministic():
    runner = make_runner()
    # x has duplicate values, so LIMIT cannot pick a deterministic subset.
    sql = "SELECT id, x FROM t ORDER BY x LIMIT 2"
    assert has_nondeterministic_tiebreak(sql, runner, SCHEMA)
    runner.close()


def test_row_number_on_nonunique_order_is_nondeterministic():
    runner = make_runner()
    sql = "SELECT id, ROW_NUMBER() OVER (ORDER BY x) AS rn FROM t"
    assert has_nondeterministic_tiebreak(sql, runner, SCHEMA)
    runner.close()


def test_row_number_on_unique_order_is_deterministic():
    runner = make_runner()
    sql = "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM t"
    assert not has_nondeterministic_tiebreak(sql, runner, SCHEMA)
    runner.close()


def test_sum_over_order_by_nonunique_is_nondeterministic():
    runner = make_runner()
    sql = "SELECT id, SUM(x) OVER (PARTITION BY grp ORDER BY x) FROM t"
    assert has_nondeterministic_tiebreak(sql, runner, SCHEMA)
    runner.close()


def test_plain_aggregate_window_is_deterministic():
    runner = make_runner()
    sql = "SELECT id, SUM(x) OVER (PARTITION BY grp) FROM t"
    assert not has_nondeterministic_tiebreak(sql, runner, SCHEMA)
    runner.close()


def test_loose_normalization_date_equals_timestamp():
    assert loose_rows_equal(
        [[normalize_value(__import__("datetime").date(2024, 1, 1))]],
        [[normalize_value(__import__("datetime").datetime(2024, 1, 1, 0, 0, 0))]],
    )


def test_loose_normalization_numeric_types_unify():
    from decimal import Decimal

    assert loose_rows_equal(
        [[normalize_value(Decimal("1.5"))]],
        [[normalize_value(1.5)]],
    )
    assert loose_rows_equal(
        [[normalize_value(3)]], [[normalize_value(3.0)]]
    )
    # But genuinely different values still differ.
    assert not loose_rows_equal(
        [[normalize_value(Decimal("1.5"))]], [[normalize_value(1.6)]]
    )


def test_root_signature_dedupes_same_feature_set():
    q1 = "SELECT DISTINCT x FROM t ORDER BY x LIMIT 3"
    q2 = "SELECT DISTINCT y FROM u ORDER BY y LIMIT 5"
    assert root_signature(q1) == root_signature(q2)

    c1 = Candidate(
        id="a", kind="differential", schema_sqls=[], inserts=[], q1=q1
    )
    c2 = Candidate(
        id="b", kind="differential", schema_sqls=[], inserts=[], q1=q2
    )
    assert c1.root_key() == c2.root_key()

    # A different feature set produces a different key.
    q3 = "SELECT x FROM t WHERE x IN (SELECT x FROM u)"
    c3 = Candidate(
        id="c", kind="differential", schema_sqls=[], inserts=[], q1=q3
    )
    assert c3.root_key() != c1.root_key()
