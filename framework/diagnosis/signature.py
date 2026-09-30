"""Diagnostic signature sigma(t): deterministic bug-family identity.

``sigma`` replaces the old keyword-based ``root_signature``. Its core is
the fix set from intervention bisection (which switch resolves the
discrepancy), plus a plan-diff class and a version-ladder marker. Two
minimized cases with the same sigma are treated as the same behavioral
family.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from oracles.models import normalize_sql
from oracles.normalize import loose_bag

from .bisect import (
    all_off_prelude,
    compute_fix_set,
    interventions_for,
    plan_diff_ops,
)

LOGGER = logging.getLogger(__name__)


def _error_class(candidate: Any) -> str:
    """Normalized error text from the candidate's result summaries.

    For internal-error/crash hits the assertion location or error head is
    a far better family discriminator than an empty fix set.
    """
    parts: list[str] = []
    for summary in (
        getattr(candidate, "r1_summary", None),
        getattr(candidate, "r2_summary", None),
    ):
        err = str((summary or {}).get("error") or "")
        if err:
            parts.append(" ".join(err.split())[:160])
    if not parts:
        notes = str(getattr(candidate, "notes", "") or "")
        if notes:
            parts.append(" ".join(notes.split())[:160])
    return "|".join(parts)[:240]


def _case_digest(candidate: Any, minimal: dict[str, Any] | None) -> str:
    """Content hash of the minimized case (whitespace-insensitive)."""
    src = minimal or {}
    sqls = (
        list(src.get("schema_sqls") or getattr(candidate, "schema_sqls", []))
        + list(src.get("inserts") or getattr(candidate, "inserts", []))
        + [
            src.get("q1") or getattr(candidate, "q1", ""),
            src.get("q2") or getattr(candidate, "q2", "") or "",
        ]
    )
    blob = "\n".join(normalize_sql(s) for s in sqls if s)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class SignatureEngine:
    """Computes sigma(candidate, minimal_case) on the target engine."""

    def __init__(
        self,
        runner_factory: Callable[[], Any],
        checker: Any,
        engine: str = "duckdb",
        differential: Any | None = None,
        max_bisect_executions: int = 120,
    ):
        self.runner_factory = runner_factory
        self.checker = checker
        self.engine = engine
        self.differential = differential
        self.max_bisect_executions = max_bisect_executions

    # ------------------------------------------------------------- pieces
    def _version_ladder(
        self, minimal: dict[str, Any], local_default: Any | None
    ) -> str:
        """nu: does the minimized query behave identically on the old build?

        ``longstanding`` — old version returns the same (buggy) bag;
        ``recent`` — old version returns a different bag or errors;
        ``unknown/unavailable`` — cannot decide.
        """
        if self.engine != "duckdb" or self.differential is None:
            return "unavailable"
        if not getattr(self.differential, "available", False):
            return "unavailable"
        setup = list(minimal["schema_sqls"]) + list(minimal["inserts"])
        try:
            remote = self.differential.run_remote(setup, [minimal["q1"]])
        except Exception:  # noqa: BLE001
            LOGGER.debug("version ladder failed", exc_info=True)
            return "unknown"
        if remote is None or not remote.get("results"):
            return "unknown"
        record = remote["results"][0]
        if record.get("error") is not None:
            err = str(record["error"])
            # A parse/bind/catalog failure on the old build means the
            # feature likely did not exist there — "recent" would
            # falsely imply a regression. Runtime/engine errors still
            # count as behavioral divergence.
            for marker in ("Parser Error", "Binder Error", "Catalog Error",
                           "syntax error"):
                if marker in err:
                    return "unknown"
            return "recent"
        if local_default is None or not local_default.ok:
            return "unknown"
        if loose_bag(record.get("rows") or []) == loose_bag(local_default.rows):
            return "longstanding"
        return "recent"

    # -------------------------------------------------------------- entry
    def compute(
        self, candidate: Any, minimal: dict[str, Any]
    ) -> dict[str, Any]:
        """Return sigma dict; ``key`` is the dedup identity."""
        schema = list(minimal.get("schema_sqls", candidate.schema_sqls))
        inserts = list(minimal.get("inserts", candidate.inserts))
        q1 = minimal.get("q1", candidate.q1)
        q2 = minimal.get("q2", candidate.q2)

        runner = self.runner_factory()
        minimal_case = {
            "schema_sqls": schema, "inserts": inserts, "q1": q1, "q2": q2,
        }
        try:
            setup_errors = runner.setup(schema + inserts)
            if any(err for _, err in setup_errors):
                return self._signature(
                    fix_kind="setup_error", fix_set=[], plan_diff=[],
                    nu="unknown", executions=0,
                    candidate=candidate, minimal=minimal_case,
                )

            def fails(prelude: list[str]) -> bool:
                return self.checker.reproduces(
                    candidate, schema, inserts, q1, q2,
                    prelude_sqls=prelude,
                )

            interventions = interventions_for(self.engine, runner)
            names = [name for name, _ in interventions]
            off = all_off_prelude(self.engine, names)

            fix = compute_fix_set(
                fails,
                self.engine,
                runner,
                max_executions=self.max_bisect_executions,
            )
            local_default = runner.run(q1, timeout_s=10.0)
            plan_diff = plan_diff_ops(runner, q1, self.engine, off)
        except Exception:  # noqa: BLE001 - diagnosis must never kill a run
            LOGGER.exception("signature computation failed")
            return self._signature(
                fix_kind="error", fix_set=[], plan_diff=[], nu="unknown",
                executions=0,
                candidate=candidate, minimal=minimal_case,
            )
        finally:
            runner.close()

        nu = self._version_ladder(minimal | {"q1": q1}, local_default)
        fix_kind = fix["fix_kind"]
        fix_set = fix["fix_set"]
        # Wide fix sets mean attribution failed: when disabling a large
        # fraction of the basis each "resolves" the discrepancy, the
        # interventions are avoiding the bug's trigger, not isolating its
        # root cause. Reclassify as ``pervasive`` so the identity falls
        # back to the (stable) plan-diff + version markers instead of an
        # unstable rule subset — prevents one fault surface exploding
        # into hundreds of phantom families.
        if (
            fix_kind in ("single", "minimal_set")
            and len(fix_set) > max(6, len(names) // 4)
        ):
            fix_kind = "pervasive"
            fix_set = []
        return self._signature(
            fix_kind=fix_kind,
            fix_set=fix_set,
            plan_diff=plan_diff,
            nu=nu,
            executions=fix["executions"],
            candidate=candidate,
            minimal=minimal_case,
        )

    def _signature(
        self,
        *,
        fix_kind: str,
        fix_set: list[str],
        plan_diff: list[str],
        nu: str,
        executions: int,
        candidate: Any | None = None,
        minimal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Family identity = the minimal intervention that resolves it.

        When a fix set exists it dominates the key: two cases resolved by
        the same rule set are the same behavioral family, regardless of
        downstream plan-diff or version-ladder detail (those are recorded
        as evidence). Only when no intervention resolves the case
        (``non_optimizer``/``unresolved``) do plan_diff/nu/AST shape carry
        the identity — the honest degraded path.
        """
        canonical = {
            "engine": self.engine,
            "fix_kind": fix_kind,
            "fix_set": sorted(fix_set),
            "plan_diff": sorted(plan_diff),
            "nu": nu,
        }
        if fix_kind in ("single", "minimal_set") and fix_set:
            ident = {
                "engine": self.engine,
                "fix_kind": fix_kind,
                "fix_set": sorted(fix_set),
                # plan_diff joins the identity: different root causes can
                # be coincidentally "resolved" by the same intervention
                # (avoidance != attribution), e.g. prefer_range_joins and
                # join_order hits were over-merged. Two cases with the
                # same fix set but disjoint plan diffs are not one family.
                "plan_diff": sorted(plan_diff),
            }
        elif fix_kind == "flaky_parallel":
            # A parallelism race's version-ladder marker is noise (the
            # old build flakes the same way) — keep it out of identity
            # so flaky re-bisects collapse onto one family.
            ident = {
                "engine": self.engine,
                "fix_kind": fix_kind,
                "fix_set": sorted(fix_set),
                "plan_diff": sorted(plan_diff),
            }
        else:
            ident = canonical
            if (
                not fix_set
                and not plan_diff
                and nu in ("unknown", "unavailable")
                and candidate is not None
            ):
                # Unlocalized: bisection found no resolving intervention
                # and the degraded markers are empty, so the canonical
                # identity carries zero diagnostic content — every such
                # case on this engine would share one key (the 6094e8fd
                # collision). Fall back to the error class plus the
                # normalized case text: distinct unlocalized cases stay
                # distinct families instead of false-merging, while an
                # identical minimal case still dedups.
                ident = {
                    "engine": self.engine,
                    "fix_kind": fix_kind,
                    "unlocalized": True,
                    "err_class": _error_class(candidate),
                    "case": _case_digest(candidate, minimal),
                }
        key = hashlib.sha256(
            json.dumps(ident, sort_keys=True).encode()
        ).hexdigest()[:20]
        return {**canonical, "key": key, "bisect_executions": executions}
