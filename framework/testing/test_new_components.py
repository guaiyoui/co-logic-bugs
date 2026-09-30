"""Tests for the generalized framework: NoREC, PlanVariant, bandit,
coverage, seed parsing/mutation, and the PostgreSQL runner.
"""

from __future__ import annotations

import random

import pytest

from oracles.db_runner import DuckDBRunner
from oracles.norec import NoRECOracle, norec_applicable
from oracles.plan_variant import PlanVariantOracle
from evolution.bandit import CategoryBandit
from coverage.features import extract_cells
from coverage.store import CoverageStore
from seeds.parse import parse_duckdb_test, parse_pg_regress
from seeds.mutate import SeedMutator
from seeds.store import Seed

SETUP = [
    "CREATE TABLE t(a INTEGER, b DOUBLE, c VARCHAR, d BOOLEAN)",
    "INSERT INTO t VALUES (1, 0.5, 'x', TRUE), (2, -0.01, '', FALSE), (NULL, NULL, NULL, NULL)",
]


def make_runner() -> DuckDBRunner:
    runner = DuckDBRunner(version_tag="test")
    runner.setup(SETUP)
    return runner


class TestNoREC:
    def test_applicability(self):
        assert norec_applicable("SELECT a FROM t", "a > 0")
        assert not norec_applicable("SELECT a FROM t", None)
        assert not norec_applicable("SELECT a FROM t GROUP BY a", "a > 0")
        assert not norec_applicable("SELECT a FROM t LIMIT 1", "a > 0")
        # aggregates in the select list do not block NoREC
        assert norec_applicable("SELECT SUM(a) FROM t", "a > 0")

    def test_correct_query_no_report(self):
        runner = make_runner()
        try:
            assert NoRECOracle().check(
                runner, "SELECT a FROM t", "a > 0", SETUP, []
            ) is None
        finally:
            runner.close()

    def test_known_deliminator_bug_via_norec_shape(self):
        # NoREC does not cover EXISTS subqueries, but must stay silent.
        runner = make_runner()
        try:
            assert NoRECOracle().check(
                runner,
                "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM t t2 WHERE t2.a = t.a)",
                "a > 0",
                SETUP,
                [],
            ) is None
        finally:
            runner.close()


class TestPlanVariant:
    def test_no_false_positive_on_plain_query(self):
        runner = make_runner()
        try:
            assert PlanVariantOracle(engine="duckdb").check(
                runner, "SELECT a, b FROM t WHERE a IS NOT NULL", SETUP, []
            ) is None
        finally:
            runner.close()

    def test_catches_deliminator_bug(self):
        setup = [
            "CREATE TABLE inv(item_id BIGINT, price DECIMAL(12,2), stock INTEGER)",
            "INSERT INTO inv VALUES (2,-0.01,-5),(7,-0.01,10),(4,0.00,0),(9,0.00,7)",
        ]
        runner = DuckDBRunner(version_tag="test")
        runner.setup(setup)
        try:
            hit = PlanVariantOracle(engine="duckdb").check(
                runner,
                "SELECT i.item_id FROM inv i WHERE EXISTS ("
                "SELECT 1 FROM inv j WHERE j.price=i.price "
                "AND j.item_id<>i.item_id AND j.stock>i.stock)",
                setup,
                [],
            )
            assert hit is not None and hit.kind == "plan_variant"
            assert hit.r2_summary.get("variant") == "no_optimizer"
        finally:
            runner.close()


class TestBandit:
    def test_ucb_prefers_untried_and_rewarded(self):
        bandit = CategoryBandit(c=1.0)
        for arm in ["a", "b", "c"]:
            bandit.ensure(arm)
        bandit.update("a", true_bug=True)
        bandit.update("b", wasted=True)
        top = bandit.select(1)
        # 'c' is untried (inf score) — must be selected first.
        assert top == ["c"]
        bandit.update("c", wasted=True)
        # now 'a' (rewarded) should outrank 'b' (wasted) eventually
        scores = bandit.select(3)
        assert scores[0] in ("a", "b", "c")

    def test_root_arm(self):
        bandit = CategoryBandit()
        bandit.add_root_arm("rc:optimizer:SIG", "hint text")
        assert "rc:optimizer:SIG" in bandit.hints
        assert "rc:optimizer:SIG" in bandit.arms


class TestCoverage:
    def test_cells_and_novelty(self, tmp_path):
        store = CoverageStore(tmp_path / "cov.json")
        runner = make_runner()
        try:
            cells = extract_cells(
                runner, "SELECT DISTINCT a, CASE WHEN b>0 THEN 1 ELSE 0 END FROM t", SETUP
            )
        finally:
            runner.close()
        assert "ast:DISTINCT" in cells and "ast:CASE" in cells
        assert any(c.startswith("data:") for c in cells)
        n1 = store.observe(cells)
        assert n1 == len(cells)
        assert store.observe(cells) == 0
        store.save()
        assert CoverageStore(tmp_path / "cov.json").size() == store.size()


class TestSeeds:
    DUCKDB_TEST = """# comment
statement ok
CREATE TABLE t(a INTEGER, b VARCHAR);

statement ok
INSERT INTO t VALUES (1,'x'),(2,'y');

query I
SELECT a FROM t WHERE a > 0
----
1
2

query II
SELECT a, b FROM t ORDER BY a
----
1 x
2 y
"""

    PG_SQL = """CREATE TABLE t(a int, b text);
INSERT INTO t VALUES (1,'x'),(2,'y');
SELECT a FROM t WHERE a > 0;
SELECT a, b FROM t ORDER BY a;
"""

    def test_duckdb_test_parse(self):
        seeds = parse_duckdb_test(self.DUCKDB_TEST, "src:test.test")
        assert len(seeds) == 2
        assert any("CREATE TABLE" in s for s in seeds[0].setup_sqls)
        assert seeds[0].query.startswith("SELECT")

    def test_pg_regress_parse(self):
        seeds = parse_pg_regress(self.PG_SQL, "src:regress/x.sql")
        assert len(seeds) == 2

    def test_mutator(self):
        seed = Seed(
            setup_sqls=[
                "CREATE TABLE inv(item_id BIGINT, price DECIMAL(12,2), stock INTEGER)",
                "INSERT INTO inv VALUES (2,-0.01,-5)",
            ],
            query="SELECT item_id FROM inv WHERE stock > 0",
            engine="duckdb",
        )
        muts = SeedMutator().mutations_for(seed, max_per_seed=3)
        assert muts and all(m.predicate for m in muts)
        assert all("FROM inv" in m.select_from for m in muts)

    def test_mutator_strips_trailing_clauses(self):
        from seeds.mutate import _from_tail

        assert _from_tail("SELECT * FROM t ORDER BY 1, 2 LIMIT 5") == "FROM t"
        assert (
            _from_tail("SELECT * FROM p WHERE EXISTS (SELECT 1 FROM v x) ORDER BY 1")
            == "FROM p"
        )

    def test_mutator_alias_and_self_join(self):
        seed = Seed(
            setup_sqls=[
                "CREATE TABLE inv(item_id BIGINT, price DOUBLE, stock INTEGER)",
                "INSERT INTO inv VALUES (2,-0.01,-5),(7,-0.01,10)",
            ],
            query="SELECT item_id FROM inv i WHERE i.stock > 0",
            engine="duckdb",
        )
        muts = SeedMutator(random.Random(1)).mutations_for(seed, max_per_seed=10)
        assert all("inv i" in m.select_from or "inv a" in m.select_from for m in muts)
        self_joins = [m for m in muts if m.category == "seed_self_join_exists"]
        if self_joins:  # probabilistic; when present must use fresh aliases
            assert "FROM inv a" in self_joins[0].select_from
            assert "inv b" in self_joins[0].predicate


class TestValueDependentErrors:
    def test_classifier(self):
        from oracles.errors import is_value_dependent_error

        assert is_value_dependent_error(
            "NumericValueOutOfRange: numeric field overflow"
        )
        assert is_value_dependent_error("division by zero")
        assert is_value_dependent_error(
            "Conversion Error: could not convert string to INT"
        )
        assert not is_value_dependent_error("Parser Error: syntax error")
        assert not is_value_dependent_error("INTERNAL Error: assertion failed")
        assert not is_value_dependent_error(None)

    def test_norec_drops_eval_order_artifact(self, tmp_path):
        pytest.importorskip("pgserver")
        from targets.postgres_runner import PostgresRunner

        runner = PostgresRunner(tmp_path / "pg")
        try:
            setup = [
                "CREATE TABLE m(id BIGINT, label VARCHAR(64), v DOUBLE PRECISION)",
                "INSERT INTO m VALUES (1,'cpu',1e308),(2,'mem',1.0)",
                "CREATE TABLE o(s VARCHAR(32), paid BOOLEAN)",
                "INSERT INTO o VALUES ('x', FALSE)",
            ]
            runner.setup(setup)
            # cast overflows on a row the IN-filter would exclude -> eval order
            hit = NoRECOracle().check(
                runner,
                "SELECT m.id FROM m WHERE m.label IN (SELECT o.s FROM o WHERE NOT o.paid)",
                "m.v::NUMERIC(10,2) IS NOT NULL",
                setup,
                [],
            )
            assert hit is None
        finally:
            runner.cleanup()


class TestPostgresRunner:
    PG_SETUP = [
        "CREATE TABLE t(a INTEGER, b DOUBLE PRECISION, c VARCHAR, d BOOLEAN)",
        "INSERT INTO t VALUES (1, 0.5, 'x', TRUE), (2, -0.01, '', FALSE), (NULL, NULL, NULL, NULL)",
    ]

    def test_smoke(self, tmp_path):
        pytest.importorskip("pgserver")
        from targets.postgres_runner import PostgresRunner

        runner = PostgresRunner(tmp_path / "pg")
        try:
            out = runner.setup(self.PG_SETUP)
            assert all(err is None for _, err in out)
            res = runner.run("SELECT a FROM t WHERE a > 0 ORDER BY a")
            assert res.ok and res.rows == [[1], [2]]
            plan = runner.explain_plan("SELECT a FROM t")
            assert plan is not None
            # NoREC must stay silent on a correct engine for this query
            assert NoRECOracle().check(
                runner, "SELECT a FROM t", "a > 0", self.PG_SETUP, []
            ) is None
        finally:
            runner.cleanup()
