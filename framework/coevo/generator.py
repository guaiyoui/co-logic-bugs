"""Constrained candidate generators. LLMs generate inputs, never verdicts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from .models import Candidate

DEFAULT_SETUP = [
    "CREATE TABLE users(id INTEGER, age INTEGER, department VARCHAR)",
    "INSERT INTO users VALUES (1, 18, 'a'), (2, NULL, 'a'), (3, 42, 'b'), (4, -1, NULL)",
    "CREATE TABLE orders(id INTEGER, user_id INTEGER, amount DECIMAL(10,2), status VARCHAR)",
    (
        "INSERT INTO orders VALUES (10, 1, 0.00, 'new'), (11, 1, 19.99, 'paid'), "
        "(12, 2, NULL, 'void'), (13, NULL, -3.50, NULL)"
    ),
]


SEED_QUERIES = [
    (
        "join",
        "SELECT u.department, SUM(o.amount) FROM users u LEFT JOIN orders o ON u.id=o.user_id GROUP BY u.department",
    ),
    (
        "null",
        "SELECT id, age IS NULL, COALESCE(age, -99) FROM users WHERE age > 10 OR age IS NULL",
    ),
    (
        "subquery",
        "SELECT id FROM users WHERE id IN (SELECT user_id FROM orders WHERE amount >= 0)",
    ),
    (
        "window",
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY department ORDER BY age NULLS LAST) FROM users",
    ),
    (
        "setop",
        "SELECT id FROM users WHERE age >= 0 UNION ALL SELECT id FROM users WHERE age < 0 OR age IS NULL",
    ),
]


def _candidate_id(query: str) -> str:
    return "cand-" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]


class SeedCandidateGenerator:
    """Deterministic offline baseline and fallback for an unavailable LLM."""

    def generate(
        self, count: int, strategy: dict[str, Any], history: Sequence[dict[str, Any]]
    ) -> list[Candidate]:
        required = set(strategy.get("required_features", []))
        ordered = sorted(SEED_QUERIES, key=lambda item: item[0] not in required)
        candidates = []
        for tag, query in ordered[:count]:
            candidates.append(
                Candidate(
                    candidate_id=_candidate_id(query),
                    query=query,
                    setup_sql=list(DEFAULT_SETUP),
                    feature_tags=[tag],
                    rationale="trusted offline seed",
                )
            )
        return candidates


class LLMCandidateGenerator:
    """Ask an LLM for read-only queries under an externally fixed oracle."""

    def __init__(
        self,
        completion: Callable[[str], str],
        fallback: SeedCandidateGenerator | None = None,
    ):
        self.completion = completion
        self.fallback = fallback or SeedCandidateGenerator()

    @staticmethod
    def _extract_json(text: str) -> Any:
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        payload = fenced.group(1) if fenced else text
        start, end = payload.find("["), payload.rfind("]")
        if start < 0 or end < start:
            raise ValueError("LLM response does not contain a JSON array")
        return json.loads(payload[start : end + 1])

    def generate(
        self, count: int, strategy: dict[str, Any], history: Sequence[dict[str, Any]]
    ) -> list[Candidate]:
        prompt = f"""
You generate stress queries for DuckDB. The harness, not you, decides whether a bug exists by
comparing optimizer-enabled and optimizer-disabled executions.

Schema and data setup:
{json.dumps(DEFAULT_SETUP, indent=2)}

Exploration strategy:
{json.dumps(strategy, indent=2)}

Recent verified feedback (never invent facts beyond it):
{json.dumps(list(history)[-8:], indent=2)}

Return exactly a JSON array with at most {count} objects. Each object has:
  query: one read-only SELECT/WITH/VALUES statement, without markdown or comments
  feature_tags: 1-4 short tags
  rationale: one sentence explaining the DBMS component stressed
Do not emit DDL, DML, PRAGMA, multiple statements, or a bug verdict.
""".strip()
        candidates: list[Candidate] = []
        try:
            items = self._extract_json(self.completion(prompt))
            if not isinstance(items, list):
                raise TypeError("LLM payload must be a list")
            for item in items[:count]:
                query = str(item["query"]).strip().rstrip(";")
                candidate = Candidate(
                    candidate_id=_candidate_id(query),
                    query=query,
                    setup_sql=list(DEFAULT_SETUP),
                    feature_tags=[str(tag) for tag in item.get("feature_tags", [])][:4],
                    rationale=str(item.get("rationale", "")),
                )
                candidate.validate()
                candidates.append(candidate)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            candidates = []
        if len(candidates) < count:
            seen = {candidate.query for candidate in candidates}
            for candidate in self.fallback.generate(count, strategy, history):
                if candidate.query not in seen:
                    candidates.append(candidate)
                    seen.add(candidate.query)
                if len(candidates) >= count:
                    break
        return candidates
