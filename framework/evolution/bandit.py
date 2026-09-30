"""UCB1 bandit over query-generation categories.

Each arm is a query category (or a fixer-spawned root-cause arm). The reward
for an arm is ``2 * true_bugs + 0.5 * novel_coverage_cells - wasted`` where
``wasted`` counts non-bug outcomes (false positives, nondeterministic
skips, unusable candidates). UCB1 gives the standard sublinear-regret
explore/exploit guarantee used in the paper's evaluation.
"""

from __future__ import annotations

import math
from typing import Any


class CategoryBandit:
    """UCB1 scheduler over named arms with dynamic arm creation."""

    def __init__(self, c: float = 1.0):
        self.c = c
        self.arms: dict[str, dict[str, float]] = {}
        self.hints: dict[str, str] = {}  # arm -> prompt hint (root-cause arms)

    def ensure(self, arm: str, prior_tries: float = 0.0, prior_reward: float = 0.0) -> None:
        if arm not in self.arms:
            self.arms[arm] = {
                "tries": float(prior_tries),
                "reward": float(prior_reward),
                "true_bugs": 0,
                "wasted": 0,
                "novel": 0.0,
            }

    def add_root_arm(self, arm: str, hint: str) -> None:
        """Register a fixer-spawned arm around a confirmed root cause."""
        self.ensure(arm, prior_tries=1.0, prior_reward=1.0)
        self.hints[arm] = hint

    def _score(self, arm: str, total: float) -> float:
        stats = self.arms[arm]
        tries = stats["tries"]
        if tries <= 0:
            return float("inf")
        exploit = stats["reward"] / tries
        explore = self.c * math.sqrt(2.0 * math.log(max(total, 2.0)) / tries)
        return exploit + explore

    def select(self, k: int = 3) -> list[str]:
        total = sum(s["tries"] for s in self.arms.values())
        ranked = sorted(
            self.arms, key=lambda a: (self._score(a, total), a), reverse=True
        )
        return ranked[:k]

    def update(
        self,
        arm: str,
        *,
        true_bug: bool = False,
        wasted: bool = False,
        novel_cells: int = 0,
    ) -> None:
        """Charge one try (a query instantiation) plus any outcome reward."""
        self.ensure(arm)
        stats = self.arms[arm]
        stats["tries"] += 1
        stats["novel"] += novel_cells
        if true_bug:
            stats["true_bugs"] += 1
            stats["reward"] += 2.0
        elif wasted:
            stats["wasted"] += 1
            stats["reward"] -= 1.0
        stats["reward"] += 0.5 * novel_cells

    def reward(
        self,
        arm: str,
        *,
        true_bug: bool = False,
        wasted: bool = False,
    ) -> None:
        """Append an outcome to an already-charged try (no new try).

        Verdicts arrive after instantiation was already accounted via
        ``update``; adding tries here would under-charge categories that
        produced zero candidates (they still consumed budget).
        """
        self.ensure(arm)
        stats = self.arms[arm]
        if true_bug:
            stats["true_bugs"] += 1
            stats["reward"] += 2.0
        elif wasted:
            stats["wasted"] += 1
            stats["reward"] -= 1.0

    def snapshot(self) -> dict[str, Any]:
        return {arm: dict(stats) for arm, stats in self.arms.items()}
