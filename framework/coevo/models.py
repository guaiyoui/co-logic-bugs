"""Typed evidence exchanged by the hunter, oracle, and repairer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    query: str
    setup_sql: list[str]
    feature_tags: list[str] = field(default_factory=list)
    rationale: str = ""
    parent_bug_id: str | None = None
    oracle: str = "optimizer_differential"

    def validate(self) -> None:
        statement = self.query.strip()
        if not statement:
            raise ValueError("candidate query is empty")
        if ";" in statement.rstrip(";"):
            raise ValueError("candidate must contain exactly one query")
        if not statement.upper().startswith(("SELECT", "WITH", "VALUES")):
            raise ValueError("candidate must be a read-only query")
        if self.oracle != "optimizer_differential":
            raise ValueError(f"unsupported oracle: {self.oracle}")


@dataclass
class QueryOutcome:
    status: str
    rows: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    plan: str | None = None


@dataclass
class OracleObservation:
    candidate: Candidate
    optimized: QueryOutcome
    unoptimized: QueryOutcome
    verdict: str
    reason: str
    reproducible: bool = False


@dataclass
class BugArtifact:
    bug_id: str
    fingerprint: str
    candidate: Candidate
    observation: OracleObservation
    first_iteration: int
    occurrences: int = 1
    repair_status: str = "unavailable"
    repair_notes: str = ""
    exploration_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairResult:
    status: str
    notes: str
    patch: str = ""
    exploration_hints: list[str] = field(default_factory=list)
    reproducer_passed: bool = False
    regression_passed: bool = False


@dataclass
class IterationMetrics:
    iteration: int
    generated: int
    valid: int
    invalid: int
    oracle_mismatches: int
    confirmed_new_bugs: int
    duplicate_bugs: int
    unique_plans: int
    repair_validated: int
    strategy: dict[str, Any]
