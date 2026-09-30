"""Adaptive generation guidelines.

The first rounds use fixed hard constraints so the system bootstraps
without LLM cost. Afterwards the guideline generator periodically asks the
LLM to synthesize new constraints from verified outcomes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from agents.json_utils import call_llm_json, extract_json

LOGGER = logging.getLogger(__name__)

FIXED_CONSTRAINTS = [
    "every table must contain NULLs, zeros, negative values, empty strings, "
    "extreme values, and duplicates",
    "mix INTEGER, BIGINT, DOUBLE, DECIMAL, VARCHAR, BOOLEAN, DATE, TIMESTAMP "
    "column types",
    "prefer predicates exercising NULL three-valued logic",
    "avoid non-deterministic functions and unordered results",
]


class GuidelineGenerator:
    """Produces per-iteration generation guidelines for the hunter."""

    def __init__(self, llm_agent: Any | None = None, interval: int = 3, warmup: int = 2):
        self.llm_agent = llm_agent
        self.interval = interval
        self.warmup = warmup
        self.last_guidelines: dict[str, Any] = {}

    def generate(
        self,
        iteration: int,
        history: list[dict[str, Any]],
        pattern_memory: dict[str, dict[str, int]],
        rewrite_memory: dict[str, dict[str, int]],
        llm_call: Callable[[str], str | None] | None = None,
    ) -> dict[str, Any]:
        """Return guideline dict for this iteration.

        Rounds < warmup and non-interval rounds reuse fixed/cached
        guidelines without an LLM call.
        """
        call = llm_call or (
            self.llm_agent.call_llm if self.llm_agent is not None else None
        )
        if iteration < self.warmup or iteration % self.interval != 0 or call is None:
            base = dict(self.last_guidelines)
            base.setdefault("focus_categories", [])
            base.setdefault("avoid_rewrite_kinds", [])
            base.setdefault("new_constraints", list(FIXED_CONSTRAINTS))
            base.setdefault("rationale", "fixed bootstrap constraints")
            return base

        stats = {
            cat: mem
            for cat, mem in pattern_memory.items()
            if mem.get("tries", 0) > 0
        }
        rewrite_stats = {
            kind: mem
            for kind, mem in rewrite_memory.items()
            if mem.get("tries", 0) > 0
        }
        recent_bugs = [
            h.get("minimal_case", h)
            for h in history[-6:]
            if h.get("verdict") == "true_bug"
        ]
        prompt = f"""
You advise a DBMS fuzzing system. Based on verified outcomes, propose updated
generation guidelines for the next rounds.

Per-category statistics (tries / true_bugs / false_positives):
{stats}

Rewrite-kind statistics:
{rewrite_stats}

Recently confirmed minimal bug cases (truncated):
{recent_bugs[:3]}

Output ONLY a ```json fenced block:
```json
{{"focus_categories": ["..."], "avoid_rewrite_kinds": ["..."],
 "new_constraints": ["..."], "rationale": "..."}}
```
""".strip()
        try:
            agent = self.llm_agent
            if agent is not None:
                payload = call_llm_json(
                    agent, prompt, temperature=0.5,
                    event="guidelines", max_tokens=800,
                )
            else:
                payload = extract_json(call(prompt) or "")
            guidelines = {
                "focus_categories": [str(x) for x in payload.get("focus_categories", [])],
                "avoid_rewrite_kinds": [
                    str(x) for x in payload.get("avoid_rewrite_kinds", [])
                ],
                "new_constraints": [
                    str(x) for x in payload.get("new_constraints", FIXED_CONSTRAINTS)
                ],
                "rationale": str(payload.get("rationale", "")),
            }
            self.last_guidelines = guidelines
            return guidelines
        except (ValueError, TypeError, AttributeError) as exc:
            LOGGER.warning("guideline generation failed, using fallback: %s", exc)
            return {
                "focus_categories": [],
                "avoid_rewrite_kinds": [],
                "new_constraints": list(FIXED_CONSTRAINTS),
                "rationale": f"fallback after failure: {exc}",
            }
