"""Re-run an oracle check on a (possibly reduced) test case.

Used by the triager's delta debugging to confirm a candidate still
reproduces after removing INSERT statements or SELECT columns. Engine-
agnostic: the checker runs on whatever runner factory it is given.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .db_runner import DuckDBRunner
from .differential import DifferentialOracle
from .equivalence import EquivalenceOracle
from .norec import NoRECOracle
from .normalize import loose_bag
from .models import (
    KIND_CRASH,
    KIND_CROSS_ENGINE,
    KIND_DIFFERENTIAL,
    KIND_EQUIV,
    KIND_ERROR_MISMATCH,
    KIND_NOREC,
    KIND_PLAN_VARIANT,
    KIND_TLP,
    Candidate,
)
from .plan_variant import PlanVariantOracle
from .tlp import TLPOracle

LOGGER = logging.getLogger(__name__)


class CaseChecker:
    """Deterministically re-evaluates whether a candidate still reproduces."""

    def __init__(
        self,
        differential: DifferentialOracle | None = None,
        runner_factory: Callable[[], Any] | None = None,
        reference_factory: Callable[[], Any] | None = None,
        engine: str = "duckdb",
    ):
        self.tlp = TLPOracle()
        self.norec = NoRECOracle()
        self.equiv = EquivalenceOracle()
        self.differential = differential
        self.runner_factory = runner_factory or (lambda: DuckDBRunner())
        self.reference_factory = reference_factory
        self.engine = engine
        self.executions = 0

    def reproduces(
        self,
        candidate: Candidate,
        schema_sqls: list[str],
        inserts: list[str],
        q1: str | None = None,
        q2: str | None = None,
        timeout_s: float = 10.0,
        prelude_sqls: list[str] | None = None,
    ) -> bool:
        """Return True when the discrepancy persists on the reduced case.

        ``prelude_sqls`` are executed after setup but before the oracle
        recheck — used to re-run the check under an intervention (e.g. an
        optimizer rule disabled) without changing the oracle logic.
        """
        q1 = q1 if q1 is not None else candidate.q1
        q2 = q2 if q2 is not None else candidate.q2
        runner = self.runner_factory()
        try:
            setup_errors = runner.setup(list(schema_sqls) + list(inserts))
            if any(err for _, err in setup_errors):
                return False
            for stmt in prelude_sqls or []:
                runner.run(stmt, timeout_s=timeout_s)
            self.executions += 1
            if candidate.kind == KIND_TLP:
                predicate = (candidate.r2_summary or {}).get("predicate")
                recheck = self.tlp.check(
                    runner,
                    q1,
                    predicate,
                    schema_sqls,
                    inserts,
                    category=candidate.category,
                    timeout_s=timeout_s,
                )
                self.executions += 4  # original + up to 3 partitions
                return recheck is not None
            if candidate.kind == KIND_NOREC:
                summary = candidate.r2_summary or {}
                select_from = summary.get("select_from") or q1
                predicate = summary.get("predicate")
                recheck = self.norec.check(
                    runner,
                    select_from,
                    predicate,
                    schema_sqls,
                    inserts,
                    category=candidate.category,
                    timeout_s=timeout_s,
                )
                self.executions += 3
                return recheck is not None
            if candidate.kind in (KIND_EQUIV, KIND_ERROR_MISMATCH, KIND_CRASH):
                if candidate.kind == KIND_CRASH and not q2:
                    result = runner.run(q1, timeout_s=timeout_s)
                    return result.is_internal_error
                if not q2:
                    return False
                recheck = self.equiv.check(
                    runner,
                    q1,
                    q2,
                    schema_sqls,
                    inserts,
                    category=candidate.category,
                    rewrite_kind=candidate.rewrite_kind,
                    timeout_s=timeout_s,
                )
                self.executions += 2
                if candidate.kind == KIND_CRASH:
                    return recheck is not None and recheck.kind == KIND_CRASH
                return recheck is not None and recheck.kind == candidate.kind
            if candidate.kind == KIND_PLAN_VARIANT:
                variant_label = (candidate.r2_summary or {}).get("variant")
                oracle = PlanVariantOracle(engine=self.engine)
                variants = oracle.variants
                if variant_label:
                    variants = [v for v in variants if v[0] == variant_label] or variants
                oracle.variants = variants
                recheck = oracle.check(
                    runner,
                    q1,
                    schema_sqls,
                    inserts,
                    category=candidate.category,
                    timeout_s=timeout_s,
                )
                self.executions += 1 + len(variants)
                return recheck is not None
            if candidate.kind == KIND_DIFFERENTIAL:
                if self.differential is None or not self.differential.available:
                    return True  # cannot re-verify remotely; accept reduction
                remote = self.differential.run_remote(
                    list(schema_sqls) + list(inserts), [q1]
                )
                self.executions += 2
                if remote is None or not remote.get("results"):
                    return False
                local = runner.run(q1, timeout_s=timeout_s)
                record = remote["results"][0]
                remote_ok = record.get("error") is None
                if local.ok != remote_ok:
                    return True
                if local.ok and remote_ok:
                    return loose_bag(local.rows) != loose_bag(
                        record.get("rows") or []
                    )
                return False
            if candidate.kind == KIND_CROSS_ENGINE:
                if self.reference_factory is None:
                    return True
                reference = self.reference_factory()
                try:
                    setup = list(schema_sqls) + list(inserts)
                    ref_setup = reference.setup(setup)
                    if any(err for _, err in ref_setup):
                        return False
                    ref = reference.run(q1, timeout_s=timeout_s)
                finally:
                    reference.close()
                local = runner.run(q1, timeout_s=timeout_s)
                self.executions += 2
                ref_ok = ref.ok
                if local.ok != ref_ok:
                    return True
                if local.ok and ref_ok:
                    return loose_bag(local.rows) != loose_bag(ref.rows)
                return False
            return False
        finally:
            runner.close()
