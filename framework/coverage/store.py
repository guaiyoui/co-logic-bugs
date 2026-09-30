"""Persistent coverage-cell store shared across iterations and campaigns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


class CoverageStore:
    """Set of observed feature cells with JSON persistence."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self._cells: set[str] = set()
        if self.path and self.path.exists():
            try:
                self._cells = set(json.loads(self.path.read_text()))
            except (json.JSONDecodeError, OSError):
                self._cells = set()

    def observe(self, cells: Iterable[str]) -> int:
        """Record cells; return how many were previously unseen."""
        new = set(cells) - self._cells
        self._cells.update(new)
        return len(new)

    def size(self) -> int:
        return len(self._cells)

    def cells(self) -> set[str]:
        return set(self._cells)

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(sorted(self._cells)))
