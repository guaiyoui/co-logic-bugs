from pathlib import Path

import pytest

from coevo.archive import BugArchive
from coevo.controller import CoEvolutionController
from coevo.executor import DuckDBOptimizerOracle
from coevo.generator import DEFAULT_SETUP, LLMCandidateGenerator, SeedCandidateGenerator
from coevo.models import Candidate, OracleObservation, QueryOutcome, RepairResult


def test_real_duckdb_oracle_executes_both_modes():
    candidate = SeedCandidateGenerator().generate(1, {}, [])[0]
    observation = DuckDBOptimizerOracle(repetitions=2).evaluate(candidate)
    assert observation.verdict == "pass"
    assert observation.reproducible
    assert observation.optimized.plan
    assert observation.unoptimized.plan


def test_candidate_rejects_multiple_or_mutating_statements():
    with pytest.raises(ValueError):
        Candidate("x", "SELECT 1; DROP TABLE users", DEFAULT_SETUP).validate()
    with pytest.raises(ValueError):
        Candidate("x", "DELETE FROM users", DEFAULT_SETUP).validate()


def test_crash_is_a_bug_but_two_sql_errors_are_not():
    crash = QueryOutcome(status="crash", error_type="ProcessExit")
    error_a = QueryOutcome(status="error", error_type="ParserException")
    error_b = QueryOutcome(status="error", error_type="BinderException")
    ok = QueryOutcome(status="ok", rows=[])
    assert DuckDBOptimizerOracle._compare(crash, ok)[0] == "mismatch"
    assert DuckDBOptimizerOracle._compare(error_a, error_b)[0] == "invalid"


def test_llm_only_generates_candidates_not_bug_verdicts():
    response = '[{"query":"SELECT id FROM users WHERE age IS NULL","feature_tags":["null"],"rationale":"null path"}]'
    generated = LLMCandidateGenerator(lambda _: response).generate(1, {}, [])
    assert len(generated) == 1
    assert generated[0].oracle == "optimizer_differential"
    generated[0].validate()


class OneGenerator:
    def generate(self, count, strategy, history):
        return [Candidate("candidate", "SELECT 1", [])]


class MismatchOracle:
    def evaluate(self, candidate):
        return OracleObservation(
            candidate=candidate,
            optimized=QueryOutcome(status="ok", rows=["int:1"], plan="optimized"),
            unoptimized=QueryOutcome(status="ok", rows=["int:2"], plan="unoptimized"),
            verdict="mismatch",
            reason="result bags differ",
            reproducible=True,
        )


class ValidatingRepairer:
    def __init__(self):
        self.calls = 0

    def attempt(self, artifact):
        self.calls += 1
        return RepairResult(
            status="validated",
            notes="reproducer and regression suite passed",
            patch="diff --git a/a b/a",
            exploration_hints=["stress constant folding"],
            reproducer_passed=True,
            regression_passed=True,
        )


def test_controller_deduplicates_and_persists_verified_evidence(tmp_path: Path):
    archive_path = tmp_path / "bugs.jsonl"
    repairer = ValidatingRepairer()
    controller = CoEvolutionController(
        OneGenerator(), MismatchOracle(), BugArchive(archive_path), repairer
    )
    metrics = controller.run(iterations=2, batch_size=1)
    assert metrics[0].confirmed_new_bugs == 1
    assert metrics[1].duplicate_bugs == 1
    assert repairer.calls == 1
    assert len(controller.archive) == 1

    reloaded = BugArchive(archive_path)
    artifact = next(iter(reloaded.values()))
    assert artifact.repair_status == "validated"
    assert artifact.exploration_hints == ["stress constant folding"]


def test_zero_output_is_stagnation_not_success():
    class EmptyGenerator:
        def generate(self, count, strategy, history):
            return []

    controller = CoEvolutionController(EmptyGenerator(), MismatchOracle())
    metrics = controller.run(iterations=20, batch_size=1, stagnation_limit=3)
    assert len(metrics) == 3
    assert len(controller.archive) == 0
    assert sum(item.confirmed_new_bugs for item in metrics) == 0
