"""Repair backends with an explicit validated/unvalidated boundary."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from .models import BugArtifact, RepairResult


class Repairer(Protocol):
    def attempt(self, artifact: BugArtifact) -> RepairResult: ...


class UnavailableRepairer:
    """Default when no DBMS source tree and validator are configured."""

    def attempt(self, artifact: BugArtifact) -> RepairResult:
        tags = artifact.candidate.feature_tags
        hints = [f"mutate the {tag} neighborhood of {artifact.bug_id}" for tag in tags]
        return RepairResult(
            status="unavailable",
            notes="no coding-agent source workspace and validation commands configured",
            exploration_hints=hints,
        )


class CommandRepairer:
    """Run an opt-in coding agent and validators in a target source tree.

    A result is `validated` only when the agent creates a patch and both the
    reproducer and regression commands succeed. The caller should provide a
    clean, disposable checkout; this class never claims success from LLM text.
    """

    def __init__(
        self,
        repository: Path,
        agent_command: Sequence[str],
        reproducer_command: Sequence[str],
        regression_command: Sequence[str],
        timeout_seconds: int = 1800,
    ):
        self.repository = repository.resolve()
        self.agent_command = list(agent_command)
        self.reproducer_command = list(reproducer_command)
        self.regression_command = list(regression_command)
        self.timeout_seconds = timeout_seconds

    def _run(
        self, argv: list[str], cwd: Path, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=cwd,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )

    def attempt(self, artifact: BugArtifact) -> RepairResult:
        try:
            dirty = self._run(["git", "status", "--porcelain"], self.repository)
        except subprocess.TimeoutExpired:
            return RepairResult(status="rejected", notes="git status timed out")
        if dirty.returncode != 0 or dirty.stdout.strip():
            return RepairResult(
                status="rejected", notes="repair workspace must be a clean git checkout"
            )
        prompt = (
            f"Fix verified DBMS bug {artifact.bug_id}. Query: {artifact.candidate.query}\n"
            f"Oracle reason: {artifact.observation.reason}. Preserve semantics and add a regression test."
        )
        try:
            agent = subprocess.run(
                self.agent_command,
                cwd=self.repository,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RepairResult(status="rejected", notes="coding agent timed out")
        try:
            patch_result = self._run(["git", "diff", "--binary"], self.repository)
        except subprocess.TimeoutExpired:
            return RepairResult(
                status="rejected", notes="collecting the patch timed out"
            )
        patch = patch_result.stdout
        if agent.returncode != 0 or not patch.strip():
            return RepairResult(
                status="rejected",
                notes="coding agent did not produce a patch",
                patch=patch,
            )
        try:
            reproducer = self._run(self.reproducer_command, self.repository)
            regression = (
                self._run(self.regression_command, self.repository)
                if reproducer.returncode == 0
                else None
            )
        except subprocess.TimeoutExpired:
            return RepairResult(
                status="rejected", notes="repair validation timed out", patch=patch
            )
        validated = (
            reproducer.returncode == 0
            and regression is not None
            and regression.returncode == 0
        )
        notes = (
            "reproducer and regression suite passed"
            if validated
            else "patch failed validation"
        )
        return RepairResult(
            status="validated" if validated else "rejected",
            notes=notes,
            patch=patch,
            exploration_hints=[
                f"stress nearby {tag} optimizer rules"
                for tag in artifact.candidate.feature_tags
            ],
            reproducer_passed=reproducer.returncode == 0,
            regression_passed=bool(regression and regression.returncode == 0),
        )
