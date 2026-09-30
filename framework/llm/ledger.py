"""Global LLM call ledger: every API call is recorded once, here.

``agents/base_agent.py`` calls :func:`ledger.record` for each attempted
request (including blocked/failed ones, marked via ``status``). Summary
totals must be recomputed from this ledger — never by summing partial
per-agent counters, which is what lost the guideline calls before.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any


class LLMLedger:
    """Append-only record of every LLM interaction for one run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._events: list[dict[str, Any]] = []

    def configure(self, path: Path | str | None) -> None:
        with self._lock:
            self._path = Path(path) if path else None
            self._events = []
            if self._path:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text("", encoding="utf-8")

    def record(
        self,
        *,
        agent: str,
        event: str,
        prompt: str = "",
        response: str | None = None,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        status: str = "ok",
        retries: int = 0,
        cache_hit: bool = False,
    ) -> dict[str, Any]:
        """Record one call. ``status``: ok | error | blocked | cache."""
        entry = {
            "ts": time.time(),
            "agent": agent,
            "event": event,
            "model": model,
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:16]
            if prompt
            else "",
            "response_hash": hashlib.sha256(response.encode()).hexdigest()[:16]
            if response
            else "",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "status": status,
            "retries": retries,
            "cache_hit": cache_hit,
        }
        with self._lock:
            self._events.append(entry)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry) + "\n")
        return entry

    def totals(self) -> dict[str, Any]:
        """Global totals recomputed from the ledger (single source)."""
        with self._lock:
            events = list(self._events)
        http_calls = sum(1 for e in events if e["status"] in ("ok", "error"))
        return {
            "total_calls": http_calls,
            "successful_calls": sum(1 for e in events if e["status"] == "ok"),
            "failed_calls": sum(1 for e in events if e["status"] == "error"),
            "blocked_calls": sum(1 for e in events if e["status"] == "blocked"),
            "total_tokens": sum(e["total_tokens"] for e in events),
            "by_event": {
                ev: sum(1 for e in events if e["event"] == ev)
                for ev in sorted({e["event"] for e in events})
            },
        }


#: Process-wide ledger. ``main`` configures its output path per run.
ledger = LLMLedger()


def null_llm(prompt: str, *args: Any, **kwargs: Any) -> None:
    """LLM callable that never makes a request — always returns None."""
    return None
