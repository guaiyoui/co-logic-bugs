"""Evidence-gated Hunter -> Oracle -> Repairer -> Hunter loop."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from .archive import BugArchive
from .models import IterationMetrics
from .policy import ExplorationPolicy
from .repair import Repairer, UnavailableRepairer


class CoEvolutionController:
    def __init__(
        self,
        generator: Any,
        oracle: Any,
        archive: BugArchive | None = None,
        repairer: Repairer | None = None,
    ):
        self.generator = generator
        self.oracle = oracle
        self.archive = archive if archive is not None else BugArchive()
        self.repairer = repairer or UnavailableRepairer()
        self.policy = ExplorationPolicy()
        self.history: list[dict[str, Any]] = []
        self._plans: set[str] = set()

    def run(
        self, iterations: int, batch_size: int, stagnation_limit: int = 5
    ) -> list[IterationMetrics]:
        results: list[IterationMetrics] = []
        for iteration in range(iterations):
            strategy = self.policy.strategy()
            candidates = self.generator.generate(batch_size, strategy, self.history)
            valid = invalid = mismatches = new_bugs = duplicates = repairs = (
                new_plans
            ) = 0
            for candidate in candidates:
                try:
                    observation = self.oracle.evaluate(candidate)
                    valid += observation.verdict in {"pass", "mismatch"}
                    invalid += observation.verdict in {"invalid", "flaky"}
                    self.policy.observe_execution(observation)
                    for outcome in (observation.optimized, observation.unoptimized):
                        if outcome.plan:
                            plan_hash = hashlib.sha256(
                                outcome.plan.encode("utf-8")
                            ).hexdigest()
                            if plan_hash not in self._plans:
                                self._plans.add(plan_hash)
                                new_plans += 1
                    if observation.verdict == "mismatch" and observation.reproducible:
                        mismatches += 1
                        artifact, is_new = self.archive.add(observation, iteration)
                        if is_new:
                            new_bugs += 1
                            repair_result = self.repairer.attempt(artifact)
                            self.policy.observe_repair(artifact, repair_result)
                            self.archive.record_update(artifact)
                            repairs += repair_result.status == "validated"
                        else:
                            duplicates += 1
                except (ValueError, TypeError):
                    invalid += 1
            self.policy.observe_iteration(new_bugs, new_plans)
            metrics = IterationMetrics(
                iteration=iteration,
                generated=len(candidates),
                valid=int(valid),
                invalid=int(invalid),
                oracle_mismatches=mismatches,
                confirmed_new_bugs=new_bugs,
                duplicate_bugs=duplicates,
                unique_plans=new_plans,
                repair_validated=int(repairs),
                strategy=strategy,
            )
            results.append(metrics)
            self.history.append(asdict(metrics))
            # Stop only on lack of both coverage and confirmed evidence; a stable
            # high bug count is not convergence and zero output is not success.
            if self.policy.stagnation >= stagnation_limit:
                break
        return results
