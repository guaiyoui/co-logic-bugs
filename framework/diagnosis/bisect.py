"""Intervention-basis fault attribution.

An *intervention* is a minimal causal experiment: flip one switch, re-run
the oracle check, observe whether the discrepancy still manifests. The
switch space is a ladder, cheapest first:

1. optimizer rules (DuckDB ``disabled_optimizers`` — 33 named rules);
2. plan-shape GUCs (PostgreSQL ``enable_*`` / ``jit`` / parallelism);
3. (future) execution settings, type lattice, source patches.

``compute_fix_set`` returns the single-intervention fix set S1 — the set
of switches whose disabling alone resolves the discrepancy — or, when no
single switch suffices, a ddmin-minimal disabling set, or the marker
``non_optimizer`` when no intervention helps.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

# Fallback when ``duckdb_optimizers()`` is unavailable (older versions).
DUCKDB_FALLBACK_RULES = [
    "expression_rewriter", "filter_pullup", "filter_pushdown",
    "empty_result_pullup", "cte_filter_pusher", "regex_range", "in_clause",
    "join_order", "deliminator", "unnest_rewriter", "unused_columns",
    "statistics_propagation", "common_subexpressions", "common_aggregate",
    "column_lifetime", "limit_pushdown", "row_group_pruner", "top_n",
    "top_n_window_elimination", "build_side_probe_side",
    "compressed_materialization", "duplicate_groups", "reorder_filter",
    "sampling_pushdown", "join_filter_pushdown", "extension",
    "materialized_cte", "sum_rewriter", "late_materialization",
    "cte_inlining", "common_subplan", "join_elimination", "window_self_join",
]

# PostgreSQL planner/executor GUCs: each steers the plan without forbidding
# an operator outright, so disabling one never makes the query unrunnable.
POSTGRES_GUC_INTERVENTIONS = [
    ("enable_hashjoin", "SET enable_hashjoin = off"),
    ("enable_mergejoin", "SET enable_mergejoin = off"),
    ("enable_nestloop", "SET enable_nestloop = off"),
    ("enable_seqscan", "SET enable_seqscan = off"),
    ("enable_indexscan", "SET enable_indexscan = off"),
    ("enable_bitmapscan", "SET enable_bitmapscan = off"),
    ("enable_indexonlyscan", "SET enable_indexonlyscan = off"),
    ("enable_hashagg", "SET enable_hashagg = off"),
    ("enable_sort", "SET enable_sort = off"),
    ("enable_incremental_sort", "SET enable_incremental_sort = off"),
    ("enable_memoize", "SET enable_memoize = off"),
    ("enable_material", "SET enable_material = off"),
    ("enable_gathermerge", "SET enable_gathermerge = off"),
    ("enable_partitionwise_join", "SET enable_partitionwise_join = off"),
    ("enable_partitionwise_aggregate", "SET enable_partitionwise_aggregate = off"),
    ("enable_parallel_append", "SET enable_parallel_append = off"),
    ("jit", "SET jit = off"),
    ("geqo", "SET geqo = off"),
    ("parallel", "SET max_parallel_workers_per_gather = 0"),
    ("enable_self_join_elimination", "SET enable_self_join_elimination = off"),
    ("enable_distinct_reordering", "SET enable_distinct_reordering = off"),
    ("enable_group_by_reordering", "SET enable_group_by_reordering = off"),
    ("enable_presorted_aggregate", "SET enable_presorted_aggregate = off"),
    ("enable_async_append", "SET enable_async_append = off"),
    ("debug_parallel_query", "SET debug_parallel_query = 'off'"),
]


def duckdb_rules(runner: Any) -> list[str]:
    """Live optimizer-rule list; falls back to the 1.5.x table."""
    try:
        result = runner.run("SELECT name FROM duckdb_optimizers()", timeout_s=5.0)
        if result.ok and result.rows:
            return sorted(str(row[0]) for row in result.rows)
    except Exception:  # noqa: BLE001 - probing must never raise
        LOGGER.debug("duckdb_optimizers() unavailable", exc_info=True)
    return list(DUCKDB_FALLBACK_RULES)


# Non-rule interventions appended to the DuckDB basis: executor-level
# switches outside ``disabled_optimizers``. ``threads`` isolates
# parallelism-dependent wrong results (a real bug class the optimizer-rule
# basis cannot reach).
DUCKDB_EXTRA_INTERVENTIONS = [
    ("threads", ["PRAGMA threads=1"]),
]

# SQLite plan-affecting pragmas: each flips a real planner/executor choice
# (auto-index use, scan direction, parallel sort). Semantics must hold.
SQLITE_INTERVENTIONS = [
    ("auto_index", ["PRAGMA automatic_index=OFF"]),
    ("rev_scan", ["PRAGMA reverse_unordered_selects=ON"]),
    ("threads", ["PRAGMA threads=1"]),
]

# DataFusion session config toggles (DF>=37 supports SQL SET): each forces
# a different physical-plan choice — repartitioning, fan-in, sort reuse.
DATAFUSION_INTERVENTIONS = [
    ("repart_joins", ["SET datafusion.optimizer.repartition_joins = false"]),
    ("repart_aggs",
     ["SET datafusion.optimizer.repartition_aggregations = false"]),
    ("repart_windows",
     ["SET datafusion.optimizer.repartition_windows = false"]),
    ("round_robin",
     ["SET datafusion.optimizer.enable_round_robin_repartition = false"]),
    ("target_par1", ["SET datafusion.execution.target_partitions = 1"]),
    ("no_coalesce", ["SET datafusion.execution.coalesce_batches = false"]),
    ("no_pref_sort",
     ["SET datafusion.optimizer.prefer_existing_sort = false"]),
]

_ENGINE_INTERVENTIONS = {
    "sqlite": SQLITE_INTERVENTIONS,
    "datafusion": DATAFUSION_INTERVENTIONS,
}


def interventions_for(engine: str, runner: Any) -> list[tuple[str, list[str]]]:
    """(name, prelude) pairs: applying prelude disables that intervention."""
    if engine == "postgres":
        return [(name, [sql]) for name, sql in POSTGRES_GUC_INTERVENTIONS]
    if engine in _ENGINE_INTERVENTIONS:
        return list(_ENGINE_INTERVENTIONS[engine])
    return [
        (rule, [f"SET disabled_optimizers='{rule}'"])
        for rule in duckdb_rules(runner)
    ] + list(DUCKDB_EXTRA_INTERVENTIONS)


def all_off_prelude(engine: str, intervention_names: list[str]) -> list[str]:
    """Prelude that disables the entire basis at once."""
    if engine == "postgres":
        return [sql for _, sql in POSTGRES_GUC_INTERVENTIONS]
    if engine in _ENGINE_INTERVENTIONS:
        return [sql for _, sql in _ENGINE_INTERVENTIONS[engine]]
    return ["PRAGMA disable_optimizer"]


def prelude_for_names(engine: str, names: list[str]) -> list[str]:
    """Prelude that disables exactly ``names`` (family membership test)."""
    if not names:
        return []
    if engine == "postgres":
        gucs = dict(POSTGRES_GUC_INTERVENTIONS)
        return [gucs[n] for n in names if n in gucs]
    if engine in _ENGINE_INTERVENTIONS:
        preludes = dict(_ENGINE_INTERVENTIONS[engine])
        return [s for n in names for s in preludes.get(n, [])]
    extras = dict(DUCKDB_EXTRA_INTERVENTIONS)
    preludes: list[str] = []
    rules = [n for n in names if n not in extras]
    if rules:
        preludes.append(f"SET disabled_optimizers='{','.join(rules)}'")
    for n in names:
        if n in extras:
            preludes.extend(extras[n])
    return preludes


def _ddmin(
    items: list[str],
    test: Callable[[list[str]], bool],
    budget: list[int],
) -> list[str]:
    """Classic delta debugging: 1-minimal subset where ``test`` holds.

    ``budget`` is a one-element counter shared with the caller; when it
    hits zero the best-known subset is returned (graceful degradation).
    """

    def within() -> bool:
        return budget[0] > 0

    def attempt(subset: list[str]) -> bool:
        budget[0] -= 1
        return test(subset)

    current = list(items)
    n = 2
    while len(current) >= 2 and within():
        chunk = max(1, len(current) // n)
        subsets = [current[i : i + chunk] for i in range(0, len(current), chunk)]
        reduced = False
        for subset in subsets:
            if within() and attempt(subset):
                current, n = subset, 2
                reduced = True
                break
            complement = [x for x in current if x not in set(subset)]
            if complement and within() and attempt(complement):
                current, n = complement, max(n - 1, 2)
                reduced = True
                break
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)
    return current


def compute_fix_set(
    fails: Callable[[list[str]], bool],
    engine: str,
    runner: Any,
    max_executions: int = 120,
) -> dict[str, Any]:
    """Attribute a confirmed discrepancy to the intervention basis.

    ``fails(prelude)`` re-runs the oracle check under the given prelude and
    returns True while the bug still manifests. Returns::

        {"fix_set": [...], "fix_kind": "single"|"minimal_set"|"flaky"|
            "non_optimizer"| "unresolved", "executions": int}
    """
    interventions = interventions_for(engine, runner)
    names = [name for name, _ in interventions]
    prelude_of = dict(interventions)
    executions = 0

    def fails_without(names_off: list[str]) -> bool:
        nonlocal executions
        executions += 1
        prelude: list[str] = []
        for name in names_off:
            prelude.extend(prelude_of[name])
        return fails(prelude)

    def stable_fix(names_off: list[str], rounds: int = 2) -> bool:
        """A fix must hold on repeat: flaky bugs flip results by chance
        under *any* plan perturbation, so an unconfirmed single "resolved"
        observation is not attribution. Require ``rounds`` clean runs."""
        return all(not fails_without(names_off) for _ in range(rounds))

    # Layer 0: baseline stability. Attribution is only meaningful for a
    # failure that manifests consistently under the default config; on a
    # flaky case every intervention randomly "resolves" some runs, so any
    # resulting rule set would attribute noise, not cause.
    baseline_rounds = 5
    baseline_failures = sum(1 for _ in range(baseline_rounds) if fails([]))
    executions += baseline_rounds
    if baseline_failures == 0:
        return {
            "fix_set": [],
            "fix_kind": "unresolved",
            "executions": executions,
        }
    if baseline_failures < baseline_rounds:
        # Flaky: targeted attribution instead of the full bisection —
        # single-threaded execution is the standard intervention for
        # parallelism-dependent wrong results.
        if engine == "duckdb":
            trials = [fails(["PRAGMA threads=1"]) for _ in range(3)]
            executions += len(trials)
            if not any(trials):
                return {
                    "fix_set": ["threads"],
                    "fix_kind": "flaky_parallel",
                    "executions": executions,
                }
        return {
            "fix_set": [],
            "fix_kind": "flaky",
            "executions": executions,
        }

    # Layer 1: single-intervention fix set S1, then confirm each member
    # — on flaky/plan-sensitive cases many single switches coincidentally
    # "fix" the run; unconfirmed members are avoidance, not attribution.
    s1 = sorted(name for name in names if not fails_without([name]))
    if s1:
        confirmed = [n for n in s1 if stable_fix([n])]
        if confirmed:
            # threads=1 as sole confirmed fix means the discrepancy is a
            # parallelism race whose baseline probe happened to pass —
            # attribute to the flaky_parallel class, not a rule.
            if confirmed == ["threads"]:
                return {
                    "fix_set": confirmed,
                    "fix_kind": "flaky_parallel",
                    "executions": executions,
                }
            return {
                "fix_set": confirmed,
                "fix_kind": "single",
                "executions": executions,
            }
        s1 = []  # every member was a coincidence — fall through

    # Still failing with the whole basis off -> bug lives outside the
    # toggleable layer (binder/executor/storage family).
    if fails(all_off_prelude(engine, names)):
        return {
            "fix_set": [],
            "fix_kind": "non_optimizer",
            "executions": executions + 1,
        }
    executions += 1

    # Whole basis off fixes it but no single switch does -> ddmin.
    budget = [max(0, max_executions - executions)]
    minimal = _ddmin(names, lambda subset: not fails_without(subset), budget)
    if minimal and not stable_fix(minimal):
        # The "minimal" set was luck on a flaky case — honest degradation.
        return {
            "fix_set": [],
            "fix_kind": "unstable",
            "executions": max_executions - budget[0],
        }
    return {
        "fix_set": sorted(minimal),
        "fix_kind": "minimal_set",
        "executions": max_executions - budget[0],
    }


# ------------------------------------------------------------- plan diff
def _plan_nodes(payload: Any, engine: str) -> list[dict[str, Any]]:
    """Normalize EXPLAIN (FORMAT JSON) payloads into node dicts."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    nodes: list[Any] = []
    if engine == "postgres":
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and "Plan" in item:
                    nodes.append(item["Plan"])
        elif isinstance(payload, dict) and "Plan" in payload:
            nodes.append(payload["Plan"])
        return nodes
    if isinstance(payload, dict) and "physical_plan" in payload:
        payload = payload["physical_plan"]
        if isinstance(payload, str):
            payload = json.loads(payload)
    if isinstance(payload, list):
        nodes.extend(payload)
    elif isinstance(payload, dict):
        nodes.append(payload)
    return nodes


def plan_op_names(
    runner: Any,
    query: str,
    engine: str,
    prelude: list[str] | None = None,
) -> Counter:
    """Multiset of physical operator names in the query plan."""
    for stmt in prelude or []:
        runner.run(stmt, timeout_s=10.0)
    result = runner.run(f"EXPLAIN (FORMAT JSON) {query}", timeout_s=10.0)
    names: Counter = Counter()
    if not result.ok or not result.rows:
        return names
    try:
        raw = result.rows[0]
        payload = raw[1] if engine != "postgres" and len(raw) > 1 else raw[0]
        nodes = _plan_nodes(payload, engine)
    except (json.JSONDecodeError, TypeError, IndexError) as exc:
        LOGGER.debug("plan parse failed: %s", exc)
        return names

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        name = node.get("name") or node.get("Node Type")
        if name:
            names[str(name).upper()] += 1
        for child in node.get("children", []) or []:
            visit(child)
        for child in node.get("Plans", []) or []:
            visit(child)

    for node in nodes:
        visit(node)
    return names


def plan_diff_ops(
    runner: Any,
    query: str,
    engine: str,
    off_prelude: list[str],
) -> list[str]:
    """Operator names that differ between default and all-off plans."""
    default_ops = plan_op_names(runner, query, engine)
    off_ops = plan_op_names(runner, query, engine, prelude=off_prelude)
    changed = (default_ops - off_ops) | (off_ops - default_ops)
    return sorted(changed)
