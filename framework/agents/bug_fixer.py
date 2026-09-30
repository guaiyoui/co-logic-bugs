"""Bug Fixer agent: deterministic minimization + LLM triage.

The fixer first shrinks a candidate deterministically (delta debugging),
then asks the LLM for a verdict only where the oracle cannot decide
(equivalence rewrites). TLP partitions, crashes, and version differentials
are already sound signals.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from oracles.db_runner import DuckDBRunner
from oracles.determinism import is_usable_for_oracles
from oracles.differential import DifferentialOracle
from oracles.normalize import loose_bag
from oracles.models import (
    KIND_CROSS_ENGINE,
    KIND_DIFFERENTIAL,
    KIND_EQUIV,
    KIND_ERROR_MISMATCH,
    KIND_NOREC,
    VERDICT_UNVERIFIED,
    Candidate,
    root_signature,
    reproducible_script,
)
from oracles.plan_variant import PlanVariantOracle
from oracles.reproduce import CaseChecker

from .base_agent import BaseAgent
from .json_utils import call_llm_json

LOGGER = logging.getLogger(__name__)

MAX_MINIMIZE_EXECUTIONS = 30
_SELECT_ITEMS = re.compile(
    r"^\s*select\s+(?P<cols>.*?)\s+from\s+(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def split_select_items(sql: str) -> tuple[list[str], str] | None:
    """Split ``SELECT a, b, c FROM rest`` into top-level column items."""
    match = _SELECT_ITEMS.match(sql)
    if not match:
        return None
    cols_text, rest = match.group("cols"), match.group("rest")
    items, depth, current = [], 0, ""
    for char in cols_text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current.strip())
    if len(items) < 2:
        return None
    return items, rest


def drop_select_item(sql: str, index: int) -> str | None:
    """Return the query with select-list item ``index`` removed."""
    parsed = split_select_items(sql)
    if not parsed:
        return None
    items, rest = parsed
    if index >= len(items) or len(items) < 2:
        return None
    kept = items[:index] + items[index + 1 :]
    return f"SELECT {', '.join(kept)} FROM {rest}"


class BugFixer(BaseAgent):
    """Minimizes and triages oracle candidates."""

    def __init__(
        self,
        config: dict[str, Any],
        checker: CaseChecker | None = None,
        differential: DifferentialOracle | None = None,
        runner_factory: Any | None = None,
        reference_factory: Any | None = None,
        target_engine: str = "duckdb",
        reference_name: str = "duckdb",
    ):
        super().__init__(config)
        self.differential = differential or (
            DifferentialOracle() if target_engine == "duckdb" else None
        )
        self.runner_factory = runner_factory or (lambda: DuckDBRunner())
        self.reference_factory = reference_factory
        self.target_engine = target_engine
        self.reference_name = reference_name
        self.checker = checker or CaseChecker(
            self.differential,
            runner_factory=self.runner_factory,
            reference_factory=self.reference_factory,
            engine=target_engine,
        )
        self.component_stats: dict[str, int] = {}
        self.root_arms: list[dict[str, Any]] = []
        # When False, every LLM triage path is bypassed (no_llm mode).
        self.llm_enabled = not config.get("disable_llm", False)

    # -------------------------------------------------------- minimization
    def _minimize(self, candidate: Candidate) -> dict[str, Any]:
        """Delta-debug INSERTs (then SELECT columns) while it reproduces."""
        schema = list(candidate.schema_sqls)
        inserts = list(candidate.inserts)
        q1, q2 = candidate.q1, candidate.q2
        budget = MAX_MINIMIZE_EXECUTIONS

        def still(schema_s: list[str], ins: list[str], qa: str, qb: str | None) -> bool:
            if self.checker.executions >= budget:
                return False
            return self.checker.reproduces(candidate, schema_s, ins, qa, qb)

        # Remove INSERT statements one at a time.
        changed = True
        while changed:
            changed = False
            for sql in list(inserts):
                reduced = [s for s in inserts if s != sql]
                if still(schema, reduced, q1, q2):
                    inserts = reduced
                    changed = True

        # Remove SELECT-list columns when the query shape allows it.
        if candidate.kind in (KIND_EQUIV, KIND_ERROR_MISMATCH) and q2:
            items1, items2 = split_select_items(q1) or ([], ""), split_select_items(q2) or ([], "")
            if items1 and items2 and len(items1) == len(items2):
                index = 0
                while index < len(items1):
                    new_q1 = drop_select_item(q1, index)
                    new_q2 = drop_select_item(q2, index)
                    if new_q1 and new_q2 and still(schema, inserts, new_q1, new_q2):
                        q1, q2 = new_q1, new_q2
                        items1, items2 = (
                            split_select_items(q1) or ([], ""),
                            split_select_items(q2) or ([], ""),
                        )
                    else:
                        index += 1
        elif candidate.kind != KIND_EQUIV:
            parsed = split_select_items(q1)
            index = 0
            while parsed and index < len(parsed[0]):
                new_q1 = drop_select_item(q1, index)
                if new_q1 and still(schema, inserts, new_q1, q2):
                    q1 = new_q1
                    parsed = split_select_items(q1)
                else:
                    index += 1

        return {"schema_sqls": schema, "inserts": inserts, "q1": q1, "q2": q2}

    # ------------------------------------------------------------- prompts
    def _judge_equivalence(
        self, candidate: Candidate, minimal: dict[str, Any]
    ) -> dict[str, Any]:
        """One LLM call deciding whether q1/q2 are truly equivalent."""
        prompt = f"""
You are a rigorous SQL semantics expert. Decide whether two queries are truly
semantically equivalent (identical results on EVERY valid database instance),
considering NULL three-valued logic, type coercion, division by zero, string
comparison/collation, and floating-point precision.

Schema:
{chr(10).join(minimal['schema_sqls'])}
Data:
{chr(10).join(minimal['inserts'])}

q1: {minimal['q1']}
q2: {minimal['q2']}

Observed on DuckDB:
q1 result: {candidate.r1_summary}
q2 result: {candidate.r2_summary}

Output ONLY a ```json fenced block:
```json
{{"equivalent": true, "reason": "...", "which_is_wrong": "q1|q2|neither|unknown"}}
```
""".strip()
        try:
            payload = call_llm_json(
                self, prompt, temperature=0.2,
                event="triage_equiv", max_tokens=600,
            )
            return {
                "equivalent": bool(payload.get("equivalent")),
                "reason": str(payload.get("reason", "")),
                "which_is_wrong": str(payload.get("which_is_wrong", "unknown")),
            }
        except (ValueError, TypeError, AttributeError) as exc:
            LOGGER.error("equivalence judgment failed: %s", exc)
            return {
                "equivalent": None,
                "reason": f"llm judgment failed: {exc}",
                "which_is_wrong": "unknown",
            }

    def _analyze_root_cause(
        self, candidate: Candidate, minimal: dict[str, Any], verdict: str
    ) -> dict[str, Any]:
        """One LLM call hypothesizing the root cause of a confirmed bug."""
        prompt = f"""
A deterministic oracle found a {self.target_engine} discrepancy (kind={candidate.kind}).

Minimal reproduction:
{reproducible_script(Candidate(
    id=candidate.id,
    kind=candidate.kind,
    schema_sqls=minimal['schema_sqls'],
    inserts=minimal['inserts'],
    q1=minimal['q1'],
    q2=minimal['q2'],
))}

Observed results:
q1: {candidate.r1_summary}
q2: {candidate.r2_summary}
Notes: {candidate.notes}

Output ONLY a ```json fenced block:
```json
{{"component": "optimizer|executor|binder|type_system|storage|parser",
 "hypothesis": "...", "suggested_fix": "...", "severity": "low|medium|high"}}
```
""".strip()
        try:
            payload = call_llm_json(
                self, prompt, temperature=0.3,
                event="root_cause", max_tokens=900,
            )
            return {
                "component": str(payload.get("component", "unknown")),
                "hypothesis": str(payload.get("hypothesis", "")),
                "suggested_fix": str(payload.get("suggested_fix", "")),
                "severity": str(payload.get("severity", "medium")),
            }
        except (ValueError, TypeError, AttributeError) as exc:
            LOGGER.error("root cause analysis failed: %s", exc)
            return {
                "component": "unknown",
                "hypothesis": f"analysis failed: {exc}",
                "suggested_fix": "",
                "severity": "medium",
            }

    # ---------------------------------------------------------- determinism
    def _deterministic(self, minimal: dict[str, Any]) -> bool:
        """True when q1/q2 are deterministic enough to be oracle-usable."""
        setup = list(minimal["schema_sqls"]) + list(minimal["inserts"])
        runner = self.runner_factory()
        try:
            queries = [minimal["q1"]] + ([minimal["q2"]] if minimal.get("q2") else [])
            for sql in queries:
                if sql and not is_usable_for_oracles(sql, runner, setup):
                    return False
            return True
        finally:
            runner.close()

    def _cross_check_equiv(self, minimal: dict[str, Any]) -> str:
        """Re-check an equiv candidate on an independent reference engine.

        For a DuckDB target the reference is the old-version venv
        (regression evidence). For a PostgreSQL target the reference is a
        local DuckDB runner (cross-engine evidence).
        """
        if self.target_engine == "duckdb":
            return self._cross_version_equiv(minimal)
        return self._cross_engine_equiv(minimal)

    def _cross_engine_equiv(self, minimal: dict[str, Any]) -> str:
        """Run q1/q2 on the reference engine; classify the pattern."""
        if self.reference_factory is None:
            return "unavailable"
        setup = list(minimal["schema_sqls"]) + list(minimal["inserts"])
        reference = self.reference_factory()
        try:
            ref_setup = reference.setup(setup)
            if any(err for _, err in ref_setup):
                return "unavailable"
            ref1 = reference.run(minimal["q1"])
            ref2 = reference.run(minimal["q2"]) if minimal.get("q2") else None
        finally:
            reference.close()
        runner = self.runner_factory()
        try:
            runner.setup(setup)
            new1 = runner.run(minimal["q1"])
            new2 = runner.run(minimal["q2"]) if minimal.get("q2") else None
        finally:
            runner.close()
        if ref2 is None or new2 is None:
            return "unavailable"
        ref_equal = (
            ref1.ok
            and ref2.ok
            and loose_bag(ref1.rows) == loose_bag(ref2.rows)
        )
        new_equal = (
            new1.ok
            and new2.ok
            and loose_bag(new1.rows) == loose_bag(new2.rows)
        )
        if ref_equal and new_equal:
            return "both_equal"
        if ref_equal:
            return "old_equal_new_differ"  # reference agrees pair is equal
        # Reference (an independent implementation) also found q1 != q2.
        # Per-side comparison: if the target matches the reference on BOTH
        # queries, the pair is simply not equivalent — LLM rewrite fault.
        same1 = ref1.ok and new1.ok and loose_bag(ref1.rows) == loose_bag(new1.rows)
        same2 = ref2.ok and new2.ok and loose_bag(ref2.rows) == loose_bag(new2.rows)
        if same1 and same2:
            return "old_differs_same"
        return "old_differs"

    def _cross_version_equiv(self, minimal: dict[str, Any]) -> str:
        """Run q1/q2 on the old version; classify the divergence pattern."""
        if not self.differential or not self.differential.available:
            return "unavailable"
        setup = list(minimal["schema_sqls"]) + list(minimal["inserts"])
        remote = self.differential.run_remote(setup, [minimal["q1"], minimal["q2"]])
        runner = self.runner_factory()
        try:
            runner.setup(setup)
            new1 = runner.run(minimal["q1"])
            new2 = runner.run(minimal["q2"])
        finally:
            runner.close()
        if remote is None or len(remote.get("results", [])) < 2:
            return "unavailable"
        old1, old2 = remote["results"][0], remote["results"][1]
        old_equal = (
            old1.get("error") is None
            and old2.get("error") is None
            and loose_bag(old1.get("rows") or [])
            == loose_bag(old2.get("rows") or [])
        )
        new_equal = (
            new1.ok
            and new2.ok
            and loose_bag(new1.rows) == loose_bag(new2.rows)
        )
        if old_equal and new_equal:
            return "both_equal"  # candidate no longer reproduces
        if old_equal:
            return "old_equal_new_differ"
        return "old_differs"

    def _judge_version_divergence(
        self, candidate: Candidate, minimal: dict[str, Any]
    ) -> dict[str, Any]:
        """One LLM call deciding which version is right on a divergence."""
        prompt = f"""
Two versions of DuckDB disagree on the same query and data.

Schema + data:
{chr(10).join(minimal['schema_sqls'] + minimal['inserts'])}

Query:
{minimal['q1']}

New DuckDB ({candidate.r1_summary.get('version', 'local')}):
{json.dumps(candidate.r1_summary.get('sample_rows', []), default=str)[:1200]}
error: {candidate.r1_summary.get('error')}

Old DuckDB ({candidate.r2_summary.get('version', 'old')}):
{json.dumps(candidate.r2_summary.get('sample_rows', []), default=str)[:1200]}
error: {candidate.r2_summary.get('error')}

According to the SQL standard and documented DuckDB semantics, which version's
answer is correct? Consider NULL three-valued logic, type coercion, window
semantics, and aggregation rules.

Output ONLY a ```json fenced block:
```json
{{"correct_version": "new|old|both_valid|unknown", "reason": "..."}}
```
""".strip()
        try:
            payload = call_llm_json(
                self, prompt, temperature=0.2,
                event="triage_version", max_tokens=600,
            )
            return {
                "correct_version": str(payload.get("correct_version", "unknown")),
                "reason": str(payload.get("reason", "")),
            }
        except (ValueError, TypeError, AttributeError) as exc:
            LOGGER.error("version divergence judgment failed: %s", exc)
            return {"correct_version": "unknown", "reason": f"llm failed: {exc}"}

    # ---------------------------------------------------- sound promotion
    def _sound_promotion(
        self, candidate: Candidate, minimal: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Re-witness an unsound candidate under a target-local sound oracle.

        Each side of the candidate is run through the plan-variant oracle
        (default vs all interventions off, plus key single-rule variants).
        A hit means the engine is inconsistent with *itself* — sound bug
        evidence that does not depend on the original unsound signal.
        """
        setup = list(minimal["schema_sqls"]) + list(minimal["inserts"])
        runner = self.runner_factory()
        try:
            outcomes = runner.setup(setup)
            if any(err for _, err in outcomes):
                return None
            oracle = PlanVariantOracle(engine=self.target_engine)
            for side, sql in (
                ("q1", minimal.get("q1")),
                ("q2", minimal.get("q2")),
            ):
                if not sql:
                    continue
                hit = oracle.check(
                    runner,
                    sql,
                    minimal["schema_sqls"],
                    minimal["inserts"],
                    category=candidate.category,
                    timeout_s=10.0,
                )
                if hit is not None:
                    return {
                        "promoted_side": side,
                        "promoted_by": hit.kind,
                        "variant": (hit.r2_summary or {}).get("variant"),
                        "hit_id": hit.id,
                    }
        except Exception:  # noqa: BLE001 - promotion is best-effort
            LOGGER.debug("sound promotion failed", exc_info=True)
            return None
        finally:
            runner.close()
        return None

    # -------------------------------------------------------------- triage
    def triage(
        self, candidate: Candidate, minimal: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Decide a verdict for an already-minimized candidate.

        The verdict lane is deterministic: ``true_bug`` is only reachable
        through a sound target-local oracle (TLP/NoREC/plan-variant/crash),
        either directly or via sound promotion of an unsound candidate.
        LLM judgments may produce ``unverified_bug`` — recorded for audit,
        never counted.
        """
        # Determinism gate: nondeterministic queries are unusable for every
        # oracle; rejected without any LLM call.
        if not self._deterministic(minimal):
            return "skipped_nondeterministic", {
                "determinism": "query result depends on unspecified row order"
            }

        if candidate.kind == KIND_ERROR_MISMATCH and not candidate.r1_summary.get(
            "ok", True
        ):
            # The hunter's original query is itself rejected — an invalid test
            # case, not an engine discrepancy. No LLM call needed.
            return "false_positive", {
                "equivalence_judgment": {
                    "equivalent": None,
                    "reason": "original query errors; invalid test case",
                    "which_is_wrong": "q1",
                }
            }

        if candidate.kind in (
            KIND_EQUIV,
            KIND_ERROR_MISMATCH,
            KIND_DIFFERENTIAL,
            KIND_CROSS_ENGINE,
        ):
            # Sound promotion first: if either side independently fails a
            # sound oracle, the bug evidence is deterministic.
            promotion = self._sound_promotion(candidate, minimal)
            if promotion is not None:
                return "true_bug", {
                    "sound_promotion": promotion,
                    "severity": "high",
                    "severity_note": (
                        "unsound candidate re-witnessed by a target-local "
                        "sound oracle"
                    ),
                }

        if candidate.kind == KIND_CROSS_ENGINE:
            # Dialect differences are legitimate; never a bug by itself and
            # never worth an LLM call.
            return "cross_engine_divergence", {
                "engine_judgment": "not adjudicated (policy: never upgrades)"
            }

        if candidate.kind == KIND_DIFFERENTIAL:
            # Version disagreement without a sound witness stays a
            # divergence; the version-ladder marker in sigma records
            # provenance. No LLM adjudication.
            return "version_divergence", {"version_judgment": "not adjudicated"}

        if candidate.kind in (KIND_EQUIV, KIND_ERROR_MISMATCH):
            if not self.llm_enabled:
                return "undetermined", {"equivalence_judgment": "llm disabled"}
            judgment = self._judge_equivalence(candidate, minimal)
            if judgment["equivalent"] is False:
                return "false_positive", {"equivalence_judgment": judgment}
            if judgment["equivalent"] is not True:
                return "undetermined", {"equivalence_judgment": judgment}
            extra = {"equivalence_judgment": judgment}
            if judgment.get("which_is_wrong") in ("q1", "q2"):
                cross = self._cross_check_equiv(minimal)
                extra["cross_version_check"] = cross
                if cross == "both_equal":
                    return "false_positive", {
                        **extra,
                        "reason": "candidate no longer reproduces",
                    }
                if cross == "old_differs_same":
                    # Independent engine agrees with target per-side: the
                    # rewrite is not equivalent — an LLM mistake, not a bug.
                    return "false_positive", {
                        **extra,
                        "reason": (
                            "reference engine produces the same per-side "
                            "results; queries are not equivalent"
                        ),
                    }
            # LLM says equivalent but no sound oracle witnessed a bug:
            # record for audit, never count as confirmed.
            return VERDICT_UNVERIFIED, {
                **extra,
                "severity_note": "LLM equivalence judgment without a sound "
                "oracle witness",
            }

        # TLP partitions, NoREC count mismatches, plan-variant mismatches,
        # and internal errors are sound signals: no LLM verdict needed.
        return "true_bug", {}

    def _judge_engine_divergence(
        self, candidate: Candidate, minimal: dict[str, Any]
    ) -> dict[str, Any]:
        """One LLM call deciding which engine is right on a divergence."""
        prompt = f"""
Two SQL engines disagree on the same query and data.

Schema + data:
{chr(10).join(minimal['schema_sqls'] + minimal['inserts'])}

Query:
{minimal['q1']}

Target engine ({self.target_engine}):
{json.dumps(candidate.r1_summary.get('sample_rows', []), default=str)[:1200]}
error: {candidate.r1_summary.get('error')}

Reference engine ({self.reference_name}):
{json.dumps(candidate.r2_summary.get('sample_rows', []), default=str)[:1200]}
error: {candidate.r2_summary.get('error')}

According to the SQL standard, which engine's answer is correct? Consider
NULL three-valued logic, type coercion, collation, and whether the difference
is a legitimate dialect/implementation choice (e.g. error vs NULL on cast).

Output ONLY a ```json fenced block:
```json
{{"correct_engine": "target|reference|both_valid|unknown", "reason": "..."}}
```
""".strip()
        try:
            payload = call_llm_json(
                self, prompt, temperature=0.2,
                event="triage_engine", max_tokens=600,
            )
            return {
                "correct_engine": str(payload.get("correct_engine", "unknown")),
                "reason": str(payload.get("reason", "")),
            }
        except (ValueError, TypeError, AttributeError) as exc:
            LOGGER.error("engine divergence judgment failed: %s", exc)
            return {"correct_engine": "unknown", "reason": f"llm failed: {exc}"}

    # -------------------------------------------------------------- execute
    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """Minimize and triage one candidate."""
        candidate = Candidate.from_dict(context["candidate"])
        calls_before = self.performance_metrics["total_calls"]
        self.checker.executions = 0

        minimal = self._minimize(candidate)
        verdict, analysis_extra = self.triage(candidate, minimal)

        analysis: dict[str, Any] = {}
        root_arm: dict[str, Any] | None = None
        if verdict == "true_bug":
            if self.llm_enabled:
                analysis = self._analyze_root_cause(candidate, minimal, verdict)
            else:
                analysis = {"component": "unknown", "hypothesis": "llm disabled"}
            if candidate.kind == KIND_DIFFERENTIAL:
                analysis["severity"] = "regression"
            elif analysis_extra.get("severity"):
                analysis["severity"] = analysis_extra["severity"]
                if analysis_extra.get("severity_note"):
                    analysis["severity_note"] = analysis_extra["severity_note"]
            component = analysis.get("component", "unknown")
            self.component_stats[component] = self.component_stats.get(component, 0) + 1
            # Spawn a root-cause exploration arm: the hunter can spend future
            # budget generating variants around this confirmed cause.
            root_arm = {
                "id": f"rc:{component}:{root_signature(minimal.get('q1'), minimal.get('q2'))[:40]}",
                "component": component,
                "signature": root_signature(minimal.get("q1"), minimal.get("q2")),
                "hint": (
                    f"Confirmed {self.target_engine} bug in component "
                    f"'{component}': {analysis.get('hypothesis', '')[:200]}. "
                    "Generate variants around this root cause: swap predicate "
                    "combinations (=<>,>,>=,<,<=,IS NULL), rewrite EXISTS<->IN<->"
                    "JOIN<->NOT EXISTS<->scalar subquery, change correlated "
                    "column types, add/remove nesting levels."
                ),
                "source_candidate": candidate.id,
            }
            if all(a["id"] != root_arm["id"] for a in self.root_arms):
                self.root_arms.append(root_arm)

        difficulty = self._difficulty(minimal)
        return {
            "verdict": verdict,
            "minimal_case": minimal,
            "analysis": {**analysis, **analysis_extra},
            "difficulty": difficulty,
            "root_arm": root_arm,
            "llm_calls": self.performance_metrics["total_calls"] - calls_before,
        }

    @staticmethod
    def _difficulty(minimal: dict[str, Any]) -> str:
        statements = len(minimal["schema_sqls"]) + len(minimal["inserts"])
        if statements <= 3:
            return "easy"
        if statements <= 6:
            return "medium"
        return "hard"

    # ------------------------------------------------------------- feedback
    def learn_from_feedback(self, feedback: dict[str, Any]) -> None:
        """Track which components get confirmed, weighting later analysis."""
        component = feedback.get("component")
        if component:
            self.component_stats[component] = self.component_stats.get(component, 0) + 1
