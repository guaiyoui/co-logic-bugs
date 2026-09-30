"""Feedback policy connecting verified discovery evidence to later prompts."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .models import BugArtifact, OracleObservation, RepairResult


class ExplorationPolicy:
    """A small explainable bandit-like policy over SQL feature families."""

    DEFAULT_FEATURES = (
        "join",
        "null",
        "subquery",
        "window",
        "setop",
        "aggregate",
        "cast",
    )

    def __init__(self):
        self.scores = Counter({feature: 0.0 for feature in self.DEFAULT_FEATURES})
        self.attempts = Counter({feature: 0 for feature in self.DEFAULT_FEATURES})
        self.repair_hints: list[str] = []
        self.stagnation = 0

    def strategy(self) -> dict[str, Any]:
        # Prefer useful features, but give under-sampled features an exploration bonus.
        ranked = sorted(
            self.DEFAULT_FEATURES,
            key=lambda feature: (
                self.scores[feature] + 1.0 / (1 + self.attempts[feature])
            ),
            reverse=True,
        )
        return {
            "required_features": ranked[:3],
            "temperature": min(1.0, 0.45 + 0.1 * self.stagnation),
            "repair_hints": self.repair_hints[-6:],
            "objective": "maximize reproducible novel oracle mismatches, not predicted bug likelihood",
        }

    def observe_execution(self, observation: OracleObservation) -> None:
        tags = observation.candidate.feature_tags or ["unknown"]
        reward = {"mismatch": 4.0, "pass": 0.2, "invalid": -1.0, "flaky": -0.5}[
            observation.verdict
        ]
        for tag in tags:
            self.attempts[tag] += 1
            self.scores[tag] += reward

    def observe_iteration(self, new_bugs: int, new_plans: int) -> None:
        self.stagnation = self.stagnation + 1 if new_bugs == 0 and new_plans == 0 else 0

    def observe_repair(self, artifact: BugArtifact, result: RepairResult) -> None:
        # Repairability does not determine hunter reward. It only supplies causal
        # hypotheses that can expand the neighborhood around a confirmed bug.
        artifact.repair_status = result.status
        artifact.repair_notes = result.notes
        artifact.exploration_hints = list(result.exploration_hints)
        self.repair_hints.extend(result.exploration_hints)
        if result.status == "validated":
            for tag in artifact.candidate.feature_tags:
                self.scores[tag] += 0.5
