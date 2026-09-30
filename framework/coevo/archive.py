"""Append-only, deduplicated evidence archive."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from .models import BugArtifact, Candidate, OracleObservation, QueryOutcome


def observation_fingerprint(observation: OracleObservation) -> str:
    payload = {
        "query": " ".join(observation.candidate.query.split()),
        "optimized_status": observation.optimized.status,
        "optimized_rows": observation.optimized.rows,
        "optimized_error": observation.optimized.error_type,
        "unoptimized_status": observation.unoptimized.status,
        "unoptimized_rows": observation.unoptimized.rows,
        "unoptimized_error": observation.unoptimized.error_type,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BugArchive:
    def __init__(self, output_path: Path | None = None):
        self.output_path = output_path
        self._artifacts: dict[str, BugArtifact] = {}
        if output_path is not None and output_path.exists():
            self._load(output_path)

    def _load(self, path: Path) -> None:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            candidate = Candidate(**data["candidate"])
            observation_data = data["observation"]
            observation = OracleObservation(
                candidate=candidate,
                optimized=QueryOutcome(**observation_data["optimized"]),
                unoptimized=QueryOutcome(**observation_data["unoptimized"]),
                verdict=observation_data["verdict"],
                reason=observation_data["reason"],
                reproducible=observation_data["reproducible"],
            )
            artifact = BugArtifact(
                bug_id=data["bug_id"],
                fingerprint=data["fingerprint"],
                candidate=candidate,
                observation=observation,
                first_iteration=data["first_iteration"],
                occurrences=data.get("occurrences", 1),
                repair_status=data.get("repair_status", "unavailable"),
                repair_notes=data.get("repair_notes", ""),
                exploration_hints=data.get("exploration_hints", []),
            )
            self._artifacts[artifact.fingerprint] = artifact

    def add(
        self, observation: OracleObservation, iteration: int
    ) -> tuple[BugArtifact, bool]:
        if observation.verdict != "mismatch" or not observation.reproducible:
            raise ValueError("only reproducible oracle mismatches can be archived")
        fingerprint = observation_fingerprint(observation)
        existing = self._artifacts.get(fingerprint)
        if existing:
            existing.occurrences += 1
            return existing, False
        artifact = BugArtifact(
            bug_id="bug-" + fingerprint[:12],
            fingerprint=fingerprint,
            candidate=observation.candidate,
            observation=observation,
            first_iteration=iteration,
        )
        self._artifacts[fingerprint] = artifact
        self._append(artifact)
        return artifact, True

    def _append(self, artifact: BugArtifact) -> None:
        if self.output_path is None:
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(artifact.to_dict(), sort_keys=True) + "\n")

    def record_update(self, artifact: BugArtifact) -> None:
        """Append the latest state; loading keeps the last record per fingerprint."""
        if artifact.fingerprint not in self._artifacts:
            raise KeyError(artifact.fingerprint)
        self._append(artifact)

    def values(self) -> Iterable[BugArtifact]:
        return self._artifacts.values()

    def __len__(self) -> int:
        return len(self._artifacts)
