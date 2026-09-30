"""Seed corpus storage: parsed regression tests kept as reusable test bases."""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Seed:
    """One parsed regression test: setup statements plus a probe query."""

    setup_sqls: list[str]
    query: str
    source: str = ""  # e.g. "duckdb:test/issues/general/test_4950.test"
    engine: str = "generic"  # duckdb | postgres | generic
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class SeedCorpus:
    """A persisted list of seeds with sampling helpers."""

    def __init__(self, seeds: list[Seed] | None = None):
        self.seeds = seeds or []

    def __len__(self) -> int:
        return len(self.seeds)

    def add(self, seed: Seed) -> None:
        self.seeds.append(seed)

    def sample(self, k: int, rng: random.Random) -> list[Seed]:
        if len(self.seeds) <= k:
            return list(self.seeds)
        return rng.sample(self.seeds, k)

    def for_engine(self, engine: str) -> "SeedCorpus":
        """Seeds for one engine plus engine-agnostic ones."""
        return SeedCorpus(
            [s for s in self.seeds if s.engine in (engine, "generic")]
        )

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([s.to_dict() for s in self.seeds], indent=1, default=str)
        )

    @classmethod
    def load(cls, path: Path | str) -> "SeedCorpus":
        path = Path(path)
        if not path.exists():
            return cls([])
        data = json.loads(path.read_text())
        return cls([Seed(**item) for item in data])
