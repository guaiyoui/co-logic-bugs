"""Process-global efficiency tracker: wall-time per phase + counters.

Every run records *how much work produced the result*, not just the
result: wall-clock seconds per pipeline phase (screen / diagnose / dce /
llm ...) and unit counters (oracle executions, probes, candidates).

Usage::

    from util.efficiency import eff

    with eff.phase("diagnose"):
        ...
    eff.count("oracle_executions", n)

    summary["efficiency"] = eff.snapshot()
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator


class EfficiencyTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phases: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        self._t0 = time.time()

    def reset(self) -> None:
        with self._lock:
            self._phases = {}
            self._counts = {}
            self._t0 = time.time()

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        t0 = time.time()
        try:
            yield
        finally:
            dt = time.time() - t0
            with self._lock:
                self._phases[name] = self._phases.get(name, 0.0) + dt

    def time_fn(self, name: str, fn, *a, **kw):
        t0 = time.time()
        try:
            return fn(*a, **kw)
        finally:
            dt = time.time() - t0
            with self._lock:
                self._phases[name] = self._phases.get(name, 0.0) + dt

    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + n

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "wall_s": round(time.time() - self._t0, 3),
                "phase_s": {k: round(v, 3) for k, v in
                            sorted(self._phases.items())},
                "counts": dict(sorted(self._counts.items())),
            }


eff = EfficiencyTracker()
