"""Real DuckDB execution and a deterministic optimizer differential oracle."""

from __future__ import annotations

import math
import multiprocessing as mp
import queue
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb

from .models import Candidate, OracleObservation, QueryOutcome


def _stable_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        if math.isnan(value):
            return "FLOAT:nan"
        if math.isinf(value):
            return "FLOAT:+inf" if value > 0 else "FLOAT:-inf"
        return f"FLOAT:{value.hex()}"
    if isinstance(value, Decimal):
        return f"DECIMAL:{value}"
    if isinstance(value, (date, datetime)):
        return f"TEMPORAL:{value.isoformat()}"
    if isinstance(value, bytes):
        return f"BYTES:{value.hex()}"
    return f"{type(value).__name__}:{value!r}"


def _canonical_rows(rows: Iterable[tuple[Any, ...]]) -> list[str]:
    # A SQL result is treated as a bag unless the candidate explicitly encodes
    # ordering into each row. Sorting preserves duplicate multiplicities.
    return sorted("|".join(_stable_value(value) for value in row) for row in rows)


def _execute_once(candidate: Candidate, disable_optimizer: bool) -> QueryOutcome:
    connection = duckdb.connect(":memory:")
    try:
        for statement in candidate.setup_sql:
            connection.execute(statement)
        # Candidate queries run with external file/network access disabled. The
        # process boundary and limits contain crashes and resource exhaustion.
        connection.execute("SET enable_external_access = false")
        connection.execute("SET memory_limit = '512MB'")
        connection.execute("SET threads = 1")
        if disable_optimizer:
            connection.execute("PRAGMA disable_optimizer")
        plan_rows = connection.execute(f"EXPLAIN {candidate.query}").fetchall()
        plan = "\n".join(str(row[-1]) for row in plan_rows)
        cursor = connection.execute(candidate.query)
        columns = [item[0] for item in (cursor.description or [])]
        return QueryOutcome(
            status="ok",
            rows=_canonical_rows(cursor.fetchall()),
            columns=columns,
            plan=plan,
        )
    except duckdb.Error as exc:
        return QueryOutcome(
            status="error", error_type=type(exc).__name__, error_message=str(exc)
        )
    finally:
        connection.close()


def _worker(output: Any, candidate: Candidate, disable_optimizer: bool) -> None:
    output.put(_execute_once(candidate, disable_optimizer))


class DuckDBOptimizerOracle:
    """Compare the same query with DuckDB's optimizer enabled and disabled."""

    def __init__(
        self, repetitions: int = 2, timeout_seconds: float = 10.0, isolate: bool = True
    ):
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        self.repetitions = repetitions
        self.timeout_seconds = timeout_seconds
        self.isolate = isolate

    def _run(self, candidate: Candidate, disable_optimizer: bool) -> QueryOutcome:
        if not self.isolate:
            return _execute_once(candidate, disable_optimizer)
        context = mp.get_context("spawn")
        output = context.Queue(maxsize=1)
        process = context.Process(
            target=_worker, args=(output, candidate, disable_optimizer)
        )
        process.start()
        process.join(self.timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join()
            output.close()
            return QueryOutcome(
                status="timeout", error_type="Timeout", error_message="query timed out"
            )
        try:
            return output.get_nowait()
        except queue.Empty:
            return QueryOutcome(
                status="crash",
                error_type="ProcessExit",
                error_message=f"worker exited with code {process.exitcode}",
            )
        finally:
            output.close()

    @staticmethod
    def _compare(left: QueryOutcome, right: QueryOutcome) -> tuple[str, str]:
        if left.status == "crash" or right.status == "crash":
            return "mismatch", f"DBMS worker crashed: {left.status} vs {right.status}"
        if left.status == "ok" and right.status == "ok":
            if left.columns != right.columns:
                return "mismatch", "column metadata differs"
            if left.rows != right.rows:
                return "mismatch", "result bags differ"
            return "pass", "result bags are equal"
        if left.status != right.status:
            return (
                "mismatch",
                f"optimizer modes differ: {left.status} vs {right.status}",
            )
        return "invalid", "query fails in both optimizer modes"

    def evaluate(self, candidate: Candidate) -> OracleObservation:
        candidate.validate()
        observations = []
        for _ in range(self.repetitions):
            optimized = self._run(candidate, disable_optimizer=False)
            unoptimized = self._run(candidate, disable_optimizer=True)
            verdict, reason = self._compare(optimized, unoptimized)
            observations.append((optimized, unoptimized, verdict, reason))

        first = observations[0]
        signatures = [
            (
                item[2],
                item[0].rows,
                item[0].error_type,
                item[1].rows,
                item[1].error_type,
            )
            for item in observations
        ]
        reproducible = all(signature == signatures[0] for signature in signatures[1:])
        verdict = first[2] if reproducible else "flaky"
        reason = first[3] if reproducible else "outcome changed across repetitions"
        return OracleObservation(
            candidate=candidate,
            optimized=first[0],
            unoptimized=first[1],
            verdict=verdict,
            reason=reason,
            reproducible=reproducible,
        )
