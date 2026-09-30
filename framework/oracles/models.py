"""Candidate records exchanged between the hunter, oracles, and triager."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

KIND_TLP = "tlp"
KIND_TLP_GROUPBY = "tlp_groupby"
KIND_TLP_DISTINCT = "tlp_distinct"
KIND_PINOLO = "pinolo_lite"
KIND_NOREC = "norec"
KIND_PLAN_VARIANT = "plan_variant"
KIND_EQUIV = "equiv"
KIND_CRASH = "crash"
KIND_DIFFERENTIAL = "differential"
KIND_CROSS_ENGINE = "cross_engine"
KIND_ERROR_MISMATCH = "error_mismatch"

VERDICT_TRUE_BUG = "true_bug"
VERDICT_FALSE_POSITIVE = "false_positive"
VERDICT_UNDETERMINED = "undetermined"
VERDICT_VERSION_DIVERGENCE = "version_divergence"
VERDICT_CROSS_ENGINE_DIVERGENCE = "cross_engine_divergence"
VERDICT_SKIPPED_NONDETERMINISTIC = "skipped_nondeterministic"
# LLM-judged evidence without a sound target-local oracle witness. Recorded
# for auditing but never counted as a confirmed bug.
VERDICT_UNVERIFIED = "unverified_bug"

_SIGNATURE_KEYWORDS = (
    "DISTINCT",
    "LIMIT",
    "OFFSET",
    "OVER",
    "WINDOW",
    "RECURSIVE",
    "UNION",
    "EXCEPT",
    "INTERSECT",
    "LEFT JOIN",
    "RIGHT JOIN",
    "FULL JOIN",
    "CASE",
    "CAST",
    "EXISTS",
    "IN",
    "ORDER BY",
    "GROUP BY",
    "HAVING",
)
_FUNCTION_NAME = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


def root_signature(*sqls: str | None) -> str:
    """Root-cause signature: kind-stable feature set of the involved queries.

    Collects function names (``NAME(``) and a fixed keyword vocabulary from
    all given queries; identical feature sets are considered the same root
    cause for deduplication.
    """
    features: set[str] = set()
    for sql in sqls:
        if not sql:
            continue
        upper = " ".join(sql.upper().split())
        features.update(
            match.group(1).upper() for match in _FUNCTION_NAME.finditer(sql)
        )
        for keyword in _SIGNATURE_KEYWORDS:
            if re.search(
                rf"(?<![A-Z0-9_]){re.escape(keyword)}(?![A-Z0-9_])", upper
            ):
                features.add(keyword)
    return ",".join(sorted(features))


def normalize_sql(sql: str) -> str:
    """Whitespace-insensitive SQL fingerprint text."""
    return " ".join(sql.split()).strip().rstrip(";").lower()


@dataclass
class Candidate:
    """One oracle discrepancy observed on a generated database."""

    id: str
    kind: str  # tlp | equiv | crash | differential | error_mismatch
    schema_sqls: list[str]
    inserts: list[str]
    q1: str
    q2: str | None = None
    r1_summary: dict[str, Any] = field(default_factory=dict)
    r2_summary: dict[str, Any] = field(default_factory=dict)
    category: str = "unknown"
    rewrite_kind: str = ""
    notes: str = ""
    inspired_by: str = ""

    def dedup_key(self) -> str:
        """Stable key on normalized query text plus oracle kind."""
        payload = f"{self.kind}|{normalize_sql(self.q1)}|{normalize_sql(self.q2 or '')}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def root_key(self, q1: str | None = None, q2: str | None = None) -> str:
        """Root-cause dedup key: kind + feature signature of the queries."""
        signature = root_signature(q1 or self.q1, q2 if q2 is not None else self.q2)
        return f"{self.kind}|{signature}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


def new_candidate_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"cand-{digest}"


def candidate_from_dict(data: dict[str, Any]) -> Candidate:
    return Candidate.from_dict(data)


def reproducible_script(candidate: Candidate) -> str:
    """Self-contained SQL script that reproduces the candidate."""
    lines = [sql.rstrip(";") + ";" for sql in candidate.schema_sqls + candidate.inserts]
    lines.append("-- q1")
    lines.append(candidate.q1.rstrip(";") + ";")
    if candidate.q2:
        lines.append("-- q2")
        lines.append(candidate.q2.rstrip(";") + ";")
    return "\n".join(lines)


def looks_order_sensitive(sql: str) -> bool:
    return bool(re.search(r"\border\s+by\b", sql, re.IGNORECASE))
