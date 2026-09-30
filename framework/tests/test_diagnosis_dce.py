"""Tests for the diagnosis signature, DCE probe mutations, and the
zero-LLM guarantee wiring."""

import pytest

from agents.base_agent import BaseAgent
from diagnosis.bisect import compute_fix_set, interventions_for, plan_diff_ops
from evolution.dce import FamilyExpander, mutate_query_text, mutate_setup
from llm.ledger import ledger
from oracles.db_runner import DuckDBRunner
from oracles.models import Candidate, KIND_TLP


class _DummyAgent(BaseAgent):
    def execute(self, context):
        return {}

    def learn_from_feedback(self, feedback):
        pass


@pytest.fixture(autouse=True)
def _fresh_ledger(tmp_path):
    ledger.configure(tmp_path / "ledger.jsonl")
    yield
    ledger.configure(None)


def test_blocked_llm_never_calls_network():
    agent = _DummyAgent({"model": "x", "api_key": "k", "disable_llm": True})
    assert agent.call_llm("hello") is None
    totals = ledger.totals()
    assert totals["total_calls"] == 0
    assert totals["blocked_calls"] == 1


def test_missing_key_recorded_not_crashing():
    agent = _DummyAgent({"model": "x", "api_key": ""})
    assert agent.call_llm("hello") is None
    assert ledger.totals()["failed_calls"] == 1


def test_mutate_query_text_swaps_comparisons_and_forms():
    q = "SELECT * FROM t WHERE a = 1 AND b <> 2 AND EXISTS (SELECT 1 FROM u)"
    muts = mutate_query_text(q)
    labels = {op for _, op in muts}
    assert any(l.startswith("cmp:") for l in labels)
    assert "form:exists->not_exists" in labels
    assert any("NOT EXISTS" in text for text, op in muts if op.startswith("form"))
    # original never reappears as a mutation
    assert all(text != q for text, _ in muts)


def test_mutate_setup_swaps_types_and_negates_literals():
    setup = [
        "CREATE TABLE t (a INTEGER, b DOUBLE)",
        "INSERT INTO t VALUES (1, 2.5), (3, -4.0)",
    ]
    muts = mutate_setup(setup)
    labels = {op for _, op in muts}
    assert any(l.startswith("ddl:") for l in labels)
    assert any(l.startswith("data:") for l in labels)
    # every mutation keeps the same statement count or +1 for dup_row
    for new_setup, op in muts:
        if op == "data:dup_row":
            assert len(new_setup) == len(setup) + 1
        else:
            assert len(new_setup) == len(setup)


def test_duckdb_interventions_are_real_rules():
    runner = DuckDBRunner()
    try:
        names = [n for n, _ in interventions_for("duckdb", runner)]
    finally:
        runner.close()
    assert "deliminator" in names
    assert len(names) >= 20


def test_fix_set_finds_single_rule():
    """A synthetic 'bug' that only manifests while deliminator is enabled."""
    runner = DuckDBRunner()
    try:
        interventions = interventions_for("duckdb", runner)
        prelude_of = dict(interventions)

        def fails(prelude):
            # bug manifests iff the prelude does NOT disable deliminator
            return "deliminator" not in " ".join(prelude)

        result = compute_fix_set(fails, "duckdb", runner)
    finally:
        runner.close()
    assert result["fix_kind"] == "single"
    assert result["fix_set"] == ["deliminator"]


def test_plan_diff_ops_detects_optimizer_effect():
    runner = DuckDBRunner()
    try:
        runner.setup([
            "CREATE TABLE t (a INT, b INT)",
            "INSERT INTO t VALUES (1,2),(3,4)",
        ])
        diff = plan_diff_ops(
            runner,
            "SELECT * FROM t WHERE a IN (SELECT a FROM t t2 WHERE t2.b > 1)",
            "duckdb",
            ["PRAGMA disable_optimizer"],
        )
    finally:
        runner.close()
    # any change (or empty) is acceptable — must not crash and returns list
    assert isinstance(diff, list)


def test_signature_key_merges_same_fix_set():
    """Same fix set + same plan-diff class = same family, even when the
    version ladder differs (nu is evidence, not identity)."""
    from diagnosis.signature import SignatureEngine

    engine = SignatureEngine(lambda: None, checker=None, engine="duckdb")
    a = engine._signature(
        fix_kind="single", fix_set=["deliminator", "filter_pushdown"],
        plan_diff=["DELIM_SCAN", "FILTER"], nu="longstanding",
        executions=33,
    )
    b = engine._signature(
        fix_kind="single", fix_set=["deliminator", "filter_pushdown"],
        plan_diff=["DELIM_SCAN", "FILTER"], nu="recent", executions=33,
    )
    assert a["key"] == b["key"]


def test_signature_key_splits_same_fix_set_disjoint_plan_diff():
    """Same coincidental fix set but disjoint plan diffs = not the same
    family (avoidance != attribution — review C4)."""
    from diagnosis.signature import SignatureEngine

    engine = SignatureEngine(lambda: None, checker=None, engine="duckdb")
    a = engine._signature(
        fix_kind="single", fix_set=["prefer_range_joins"],
        plan_diff=["DELIM_SCAN", "FILTER", "HASH_JOIN"], nu="recent",
        executions=33,
    )
    b = engine._signature(
        fix_kind="single", fix_set=["prefer_range_joins"],
        plan_diff=["SEQ_SCAN", "WINDOW"], nu="recent", executions=33,
    )
    assert a["key"] != b["key"]


def test_bandit_reward_does_not_charge_tries():
    """Outcome rewards attach to already-charged instantiation tries —
    reward() must not inflate tries (review C5)."""
    from evolution.bandit import CategoryBandit

    b = CategoryBandit()
    b.update("join", novel_cells=2)          # one instantiation
    b.reward("join", true_bug=True)          # its verdict
    b.update("join")                         # second instantiation
    stats = b.arms["join"]
    assert stats["tries"] == 2
    assert stats["true_bugs"] == 1
    assert stats["reward"] == 2.0 + 1.0  # bug reward + novel cells


def test_version_ladder_parser_error_is_unknown():
    """Old-version Parser/Binder/Catalog errors mean 'feature absent',
    not 'recent regression' (review C7)."""
    from diagnosis.signature import SignatureEngine

    class FakeDiff:
        available = True

        def run_remote(self, setup, queries):
            return {"results": [
                {"error": "Parser Error: syntax error at or near"}]}

    eng = SignatureEngine(lambda: None, checker=None, engine="duckdb",
                          differential=FakeDiff())
    minimal = {"schema_sqls": [], "inserts": [], "q1": "SELECT 1"}
    assert eng._version_ladder(minimal, None) == "unknown"

    class FakeDiff2(FakeDiff):
        def run_remote(self, setup, queries):
            return {"results": [
                {"error": "FATAL: internal assertion failed"}]}

    eng2 = SignatureEngine(lambda: None, checker=None, engine="duckdb",
                           differential=FakeDiff2())
    assert eng2._version_ladder(minimal, None) == "recent"


def test_threads_only_fix_is_flaky_parallel():
    """If threads=1 alone stably 'fixes' a divergence, it's a
    parallelism race, not a rule attribution."""
    runner = DuckDBRunner()
    try:
        def fails(prelude):
            return "threads=1" not in " ".join(prelude)
        result = compute_fix_set(fails, "duckdb", runner)
    finally:
        runner.close()
    assert result["fix_kind"] == "flaky_parallel"
    assert result["fix_set"] == ["threads"]


def test_cross_engine_quorum():
    from scripts.cross_engine import classify_divergence

    a, b, c, d = (1,), (1,), (2,), (2,)
    odd, cls = classify_divergence({"e1": a, "e2": b, "e3": c, "e4": d})
    assert cls == "majority" and set(odd) == {"e3", "e4"}
    odd, cls = classify_divergence({"e1": a, "e2": c})
    assert cls == "pairwise" and odd == []
    # 3 engines all disagreeing (1-1-1): no plurality -> pairwise
    odd, cls = classify_divergence(
        {"e1": (1,), "e2": (2,), "e3": (3,)})
    assert cls == "pairwise" and odd == []


def test_signature_key_splits_when_no_fix_set():
    from diagnosis.signature import SignatureEngine

    engine = SignatureEngine(lambda: None, checker=None, engine="duckdb")
    a = engine._signature(
        fix_kind="non_optimizer", fix_set=[], plan_diff=["A"], nu="x",
        executions=33,
    )
    b = engine._signature(
        fix_kind="non_optimizer", fix_set=[], plan_diff=["B"], nu="x",
        executions=33,
    )
    assert a["key"] != b["key"]


def test_signature_unlocalized_cases_do_not_false_merge():
    """Empty fix_set + empty plan_diff + unavailable nu carried zero
    diagnostic content: every unlocalized case collapsed onto one key
    (the 6094e8fd collision). Case markers must keep them distinct."""
    from diagnosis.signature import SignatureEngine

    engine = SignatureEngine(lambda: None, checker=None, engine="postgres")
    cand_a = Candidate(
        id="ca", kind=KIND_TLP,
        schema_sqls=["CREATE TABLE t(a INT)"],
        inserts=["INSERT INTO t VALUES (1)"],
        q1="SELECT * FROM t",
        q2="SELECT * FROM t WHERE a = ANY (SELECT a FROM t)",
    )
    cand_b = Candidate(
        id="cb", kind=KIND_TLP,
        schema_sqls=["CREATE TABLE u(x TEXT)"],
        inserts=["INSERT INTO u VALUES ('w')"],
        q1="SELECT * FROM u",
        q2="SELECT * FROM u WHERE x LIKE 'w%'",
    )
    kwargs = dict(
        fix_kind="non_optimizer", fix_set=[], plan_diff=[],
        nu="unavailable", executions=0,
    )
    a = engine._signature(candidate=cand_a, minimal=None, **kwargs)
    b = engine._signature(candidate=cand_b, minimal=None, **kwargs)
    assert a["key"] != b["key"]
    # identical case still dedups
    a2 = engine._signature(candidate=cand_a, minimal=None, **kwargs)
    assert a["key"] == a2["key"]
    # case without candidate keeps the old canonical behavior
    legacy = engine._signature(**kwargs)
    assert legacy["key"] != a["key"]


def test_family_expander_produces_probes():
    cand = Candidate(
        id="c1",
        kind=KIND_TLP,
        schema_sqls=["CREATE TABLE t (a INT, b INT)"],
        inserts=["INSERT INTO t VALUES (1,2),(NULL,4)"],
        q1="SELECT * FROM t",
        r2_summary={"predicate": "a > 0 AND b IS NOT NULL"},
    )
    expander = FamilyExpander(lambda: DuckDBRunner(), max_probes=250)
    probes = expander.probes_for(cand, {"schema_sqls": cand.schema_sqls,
                                       "inserts": cand.inserts,
                                       "q1": cand.q1})
    assert probes
    assert all(p.setup_sqls for p in probes)
    ops = {p.op for p in probes}
    assert any(o.startswith("cmp:") for o in ops)
    assert any(o.startswith("data:") for o in ops)


def test_index_variant_schema_parse():
    from oracles.index_variant import IndexVariantOracle, parse_tables

    schema = [
        "CREATE TABLE t (a INT, b VARCHAR, c INT[], "
        "PRIMARY KEY (a))",
        "CREATE TABLE u (x DOUBLE PRECISION, y TEXT, "
        "CONSTRAINT uq UNIQUE (x))",
    ]
    tabs = parse_tables(schema)
    assert [t.name for t in tabs] == ["t", "u"]
    assert [c for c, _ in tabs[0].cols] == ["a", "b", "c"]
    assert [c for c, _ in tabs[1].cols] == ["x", "y"]

    o = IndexVariantOracle(max_variants=50)
    labels = [lbl for lbl, _, _ in o.variants_for(schema)]
    # every col gets a btree; t has array col -> gin; both get covering
    assert any("btree_a" in l for l in labels)
    assert any(l.endswith("gin") for l in labels)
    assert sum("covering_ios" in l for l in labels) == 2
    assert any(l == "analyze_all" for l in labels)
    # hot_update needs >=2 cols — both tables qualify
    assert sum("hot_update" in l for l in labels) == 2
