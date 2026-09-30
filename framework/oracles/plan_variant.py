"""Plan-variant oracle: same query under different planner configurations.

A query's result must be independent of the physical plan chosen. DuckDB
offers ``PRAGMA disable_optimizer`` / ``SET disabled_optimizers=...`` and
PostgreSQL offers planner GUCs (``enable_*``, ``join_collapse_limit``,
``geqo``). Any bag difference across configurations is a sound signal of an
optimizer bug — no LLM involved in the verdict.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import is_guard_rail_error, is_value_dependent_error
from .models import KIND_CRASH, KIND_PLAN_VARIANT, Candidate, new_candidate_id

LOGGER = logging.getLogger(__name__)

# (label, [setup statements], [teardown statements])
DUCKDB_VARIANTS: list[tuple[str, list[str], list[str]]] = [
    ("no_optimizer", ["PRAGMA disable_optimizer"], ["PRAGMA enable_optimizer"]),
    ("no_deliminator", ["SET disabled_optimizers='deliminator'"], ["RESET disabled_optimizers"]),
    ("no_filter_pushdown", ["SET disabled_optimizers='filter_pushdown'"], ["RESET disabled_optimizers"]),
    ("no_join_order", ["SET disabled_optimizers='join_order,build_side_probe_side'"], ["RESET disabled_optimizers"]),
    ("no_statistics", ["SET disabled_optimizers='statistics_propagation'"], ["RESET disabled_optimizers"]),
    # Executor-mode variants (SET-level switches, not optimizer rules):
    # each forces a different execution strategy — a separate bug surface
    # from the optimizer-rule layer.
    ("window_combine", ["SET debug_window_mode='combine'"],
     ["RESET debug_window_mode"]),
    ("window_separate", ["SET debug_window_mode='separate'"],
     ["RESET debug_window_mode"]),
    ("external_exec", ["SET debug_force_external=true"],
     ["RESET debug_force_external"]),
    ("asof_iejoin", ["SET debug_asof_iejoin=true"],
     ["RESET debug_asof_iejoin"]),
    ("no_perfect_ht", ["SET perfect_ht_threshold=0"],
     ["RESET perfect_ht_threshold"]),
    ("no_preserve_order", ["SET preserve_insertion_order=false"],
     ["RESET preserve_insertion_order"]),
    ("prefer_range", ["SET prefer_range_joins=true"],
     ["RESET prefer_range_joins"]),
    ("no_dyn_or", ["SET dynamic_or_filter_threshold=0"],
     ["RESET dynamic_or_filter_threshold"]),
    ("no_late_mat", ["SET late_materialization_max_rows=0"],
     ["RESET late_materialization_max_rows"]),
    ("threads1", ["PRAGMA threads=1"], ["RESET threads"]),
    ("scan_taskexec", [
        "SET debug_physical_table_scan_execution_strategy='TASK_EXECUTOR'"],
     ["RESET debug_physical_table_scan_execution_strategy"]),
    ("no_cross_prod", ["SET debug_force_no_cross_product=true"],
     ["RESET debug_force_no_cross_product"]),
    # Vector-verification and storage-encoding axes: each forces a
    # different physical representation / validation path during
    # execution, orthogonal to the optimizer-rule layer.
    ("verify_vconst", ["SET debug_verify_vector='CONSTANT_OPERATOR'"],
     ["RESET debug_verify_vector"]),
    ("verify_vdict", ["SET debug_verify_vector='DICTIONARY_OPERATOR'"],
     ["RESET debug_verify_vector"]),
    ("verify_vseq", ["SET debug_verify_vector='SEQUENCE_OPERATOR'"],
     ["RESET debug_verify_vector"]),
    # Storage-encoding variants need FORCE CHECKPOINT: the setting only
    # applies to newly written data, and on :memory: nothing is written
    # until a checkpoint — without it these variants are silent no-ops.
    # Teardown resets the flag AND re-checkpoints so the next variant
    # starts from the default encoding again.
    ("bitpack_const", ["SET force_bitpacking_mode='CONSTANT'", "FORCE CHECKPOINT"],
     ["RESET force_bitpacking_mode", "FORCE CHECKPOINT"]),
    ("bitpack_dfor", ["SET force_bitpacking_mode='DELTA_FOR'", "FORCE CHECKPOINT"],
     ["RESET force_bitpacking_mode", "FORCE CHECKPOINT"]),
    ("compress_rle", ["SET force_compression='rle'", "FORCE CHECKPOINT"],
     ["RESET force_compression", "FORCE CHECKPOINT"]),
    ("compress_dict", ["SET force_compression='dictionary'", "FORCE CHECKPOINT"],
     ["RESET force_compression", "FORCE CHECKPOINT"]),
    ("compress_fsst", ["SET force_compression='fsst'", "FORCE CHECKPOINT"],
     ["RESET force_compression", "FORCE CHECKPOINT"]),
    ("compress_alp", ["SET force_compression='alp'", "FORCE CHECKPOINT"],
     ["RESET force_compression", "FORCE CHECKPOINT"]),
    ("compress_zstd", ["SET force_compression='zstd'", "FORCE CHECKPOINT"],
     ["RESET force_compression", "FORCE CHECKPOINT"]),
    ("ordered_agg_off", ["SET ordered_aggregate_threshold=0"],
     ["RESET ordered_aggregate_threshold"]),
]

POSTGRES_VARIANTS: list[tuple[str, list[str], list[str]]] = [
    ("collapse_1", ["SET join_collapse_limit = 1", "SET from_collapse_limit = 1"],
     ["RESET join_collapse_limit", "RESET from_collapse_limit"]),
    ("no_geqo", ["SET geqo = off"], ["RESET geqo"]),
    ("no_hashjoin", ["SET enable_hashjoin = off"], ["RESET enable_hashjoin"]),
    ("no_mergejoin", ["SET enable_mergejoin = off"], ["RESET enable_mergejoin"]),
    ("no_nestloop", ["SET enable_nestloop = off"], ["RESET enable_nestloop"]),
    ("no_seqscan", ["SET enable_seqscan = off"], ["RESET enable_seqscan"]),
    ("no_memoize", ["SET enable_memoize = off"], ["RESET enable_memoize"]),
    ("no_parallel", ["SET max_parallel_workers_per_gather = 0"], ["RESET max_parallel_workers_per_gather"]),
    # Second-layer axes: JIT, partitionwise planning, scan-method groups,
    # and work_mem pressure (forces external sort/hash like debug_force_external).
    ("jit_force", ["SET jit = on", "SET jit_above_cost = 0",
                   "SET jit_inline_above_cost = 0",
                   "SET jit_optimize_above_cost = 0"],
     ["RESET jit", "RESET jit_above_cost", "RESET jit_inline_above_cost",
      "RESET jit_optimize_above_cost"]),
    ("partwise_join", ["SET enable_partitionwise_join = on"],
     ["RESET enable_partitionwise_join"]),
    ("partwise_agg", ["SET enable_partitionwise_aggregate = on"],
     ["RESET enable_partitionwise_aggregate"]),
    ("no_index", ["SET enable_indexscan = off",
                  "SET enable_indexonlyscan = off",
                  "SET enable_bitmapscan = off"],
     ["RESET enable_indexscan", "RESET enable_indexonlyscan",
      "RESET enable_bitmapscan"]),
    ("no_sort", ["SET enable_sort = off",
                 "SET enable_incremental_sort = off"],
     ["RESET enable_sort", "RESET enable_incremental_sort"]),
    ("no_gathermerge", ["SET enable_gathermerge = off"],
     ["RESET enable_gathermerge"]),
    ("no_material", ["SET enable_material = off"], ["RESET enable_material"]),
    ("no_par_append", ["SET enable_parallel_append = off"],
     ["RESET enable_parallel_append"]),
    ("no_par_hash", ["SET enable_parallel_hash = off"],
     ["RESET enable_parallel_hash"]),
    ("no_part_prune", ["SET enable_partition_pruning = off"],
     ["RESET enable_partition_pruning"]),
    ("tiny_workmem", ["SET work_mem = '64kB'"], ["RESET work_mem"]),
    ("geqo4", ["SET geqo_threshold = 4", "SET geqo_seed = 1.0"],
     ["RESET geqo_threshold", "RESET geqo_seed"]),
    # Third-layer axes (feat2): force parallel plans even on tiny tables,
    # disable hash aggregation, constraint-exclusion pruning, and low-GEQO.
    # NOTE: geqo_low is stochastic by design — hits on it are flaky-class.
    ("force_parallel",
     ["SET parallel_setup_cost=0", "SET parallel_tuple_cost=0",
      "SET min_parallel_table_scan_size=0",
      "SET min_parallel_index_scan_size=0",
      "SET max_parallel_workers_per_gather=2"],
     ["RESET parallel_setup_cost", "RESET parallel_tuple_cost",
      "RESET min_parallel_table_scan_size",
      "RESET min_parallel_index_scan_size",
      "RESET max_parallel_workers_per_gather"]),
    ("no_hashagg", ["SET enable_hashagg=off"], ["RESET enable_hashagg"]),
    ("constraint_excl", ["SET constraint_exclusion=on"],
     ["RESET constraint_exclusion"]),
    ("geqo_low", ["SET geqo_threshold=2", "SET geqo_seed=0.5"],
     ["RESET geqo_threshold", "RESET geqo_seed"]),
    # Fourth-layer axes: PG16-18 planner/executor rewrites that were never
    # exercised.  On builds lacking a GUC the SET fails and the variant
    # degrades to a no-op (verified safe — the runner just records an error
    # result and the query runs on the default plan).
    ("no_sje", ["SET enable_self_join_elimination=off"],
     ["RESET enable_self_join_elimination"]),
    ("no_distinct_reorder", ["SET enable_distinct_reordering=off"],
     ["RESET enable_distinct_reordering"]),
    ("no_gb_reorder", ["SET enable_group_by_reordering=off"],
     ["RESET enable_group_by_reordering"]),
    ("no_presorted_agg", ["SET enable_presorted_aggregate=off"],
     ["RESET enable_presorted_aggregate"]),
    ("no_async_append", ["SET enable_async_append=off"],
     ["RESET enable_async_append"]),
    ("debug_par", ["SET debug_parallel_query='regress'"],
     ["RESET debug_parallel_query"]),
    # Fifth-layer axes (gucfill): planner GUCs never flipped by earlier
    # sweeps.  enable_groupagg closes the asymmetry with no_hashagg (the
    # GroupAgg arm forces hash aggregation); on builds lacking the GUC
    # (<=PG18) the SET errors and the variant degrades to a recorded
    # no-op — same contract as the fourth-layer axes.  The tidscan arm
    # only bites on ctid predicates; it is registered regardless so the
    # coverage gate can report its no-op rate.  geqo_on4 pins the seed
    # for deterministic GEQO plans on >=4-table FROM clauses.
    ("no_groupagg", ["SET enable_groupagg=off"], ["RESET enable_groupagg"]),
    ("no_tidscan", ["SET enable_tidscan=off"], ["RESET enable_tidscan"]),
    ("no_bounded_sort", ["SET optimize_bounded_sort=off"],
     ["RESET optimize_bounded_sort"]),
    ("geqo_on4", ["SET geqo=on", "SET geqo_threshold=4",
                  "SET geqo_seed=0.5"],
     ["RESET geqo", "RESET geqo_threshold", "RESET geqo_seed"]),
]

# SQLite: plan-affecting pragmas only (never semantics flags like
# case_sensitive_like — a defined-behavior difference is not a bug).
SQLITE_VARIANTS: list[tuple[str, list[str], list[str]]] = [
    ("no_auto_index", ["PRAGMA automatic_index=OFF"],
     ["PRAGMA automatic_index=ON"]),
    ("rev_scan", ["PRAGMA reverse_unordered_selects=ON"],
     ["PRAGMA reverse_unordered_selects=OFF"]),
    ("threads1", ["PRAGMA threads=1"], ["PRAGMA threads=8"]),
]

# DataFusion: SQL-SET physical-plan toggles (verified on 54.0.0).
DATAFUSION_VARIANTS: list[tuple[str, list[str], list[str]]] = [
    ("no_repart_joins",
     ["SET datafusion.optimizer.repartition_joins = false"],
     ["SET datafusion.optimizer.repartition_joins = true"]),
    ("no_repart_aggs",
     ["SET datafusion.optimizer.repartition_aggregations = false"],
     ["SET datafusion.optimizer.repartition_aggregations = true"]),
    ("no_repart_windows",
     ["SET datafusion.optimizer.repartition_windows = false"],
     ["SET datafusion.optimizer.repartition_windows = true"]),
    ("no_round_robin",
     ["SET datafusion.optimizer.enable_round_robin_repartition = false"],
     ["SET datafusion.optimizer.enable_round_robin_repartition = true"]),
    ("target_par1",
     ["SET datafusion.execution.target_partitions = 1"],
     ["SET datafusion.execution.target_partitions = 4"]),
    ("no_coalesce",
     ["SET datafusion.execution.coalesce_batches = false"],
     ["SET datafusion.execution.coalesce_batches = true"]),
    ("no_pref_sort",
     ["SET datafusion.optimizer.prefer_existing_sort = false"],
     ["SET datafusion.optimizer.prefer_existing_sort = true"]),
]

_ENGINE_VARIANTS = {
    "duckdb": DUCKDB_VARIANTS,
    "postgres": POSTGRES_VARIANTS,
    "sqlite": SQLITE_VARIANTS,
    "datafusion": DATAFUSION_VARIANTS,
}


class PlanVariantOracle:
    """Re-executes a query under planner GUCs; bags must stay identical."""

    def __init__(self, engine: str = "duckdb", variants: list | None = None):
        self.engine = engine
        self.variants = variants if variants is not None else _ENGINE_VARIANTS.get(
            engine, []
        )

    def check(
        self,
        runner: Any,
        query: str,
        schema_sqls: list[str],
        inserts: list[str],
        category: str = "unknown",
        rewrite_kind: str = "",
        timeout_s: float = 10.0,
    ) -> Candidate | None:
        """Compare baseline vs each variant; return a Candidate on mismatch."""
        baseline = runner.run(query, timeout_s=timeout_s)
        if baseline.is_internal_error:
            return Candidate(
                id=new_candidate_id("pv-crash", query),
                kind=KIND_CRASH,
                schema_sqls=schema_sqls,
                inserts=inserts,
                q1=query,
                r1_summary=baseline.summary(),
                category=category,
                rewrite_kind=rewrite_kind,
                notes="internal error on baseline plan-variant query",
            )
        if not baseline.ok:
            return None
        base_bag = baseline.bag()

        for label, setup_stmts, teardown_stmts in self.variants:
            for stmt in setup_stmts:
                runner.run(stmt, timeout_s=timeout_s)
            variant = runner.run(query, timeout_s=timeout_s)
            for stmt in teardown_stmts:
                runner.run(stmt, timeout_s=timeout_s)

            if variant.timed_out:
                # A slower plan hitting the timeout is a perf difference,
                # not a correctness bug.
                continue
            if variant.is_internal_error:
                return Candidate(
                    id=new_candidate_id("pv-crash", query, label),
                    kind=KIND_CRASH,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=query,
                    r1_summary=baseline.summary(),
                    r2_summary={**variant.summary(), "variant": label},
                    category=category,
                    rewrite_kind=rewrite_kind,
                    notes=f"internal error under planner variant {label}",
                )
            if not variant.ok:
                if baseline.ok and is_value_dependent_error(variant.error):
                    LOGGER.debug(
                        "plan-variant skip: value-dependent error: %s",
                        variant.error,
                    )
                    continue
                if baseline.ok and is_guard_rail_error(variant.error, label):
                    LOGGER.debug(
                        "plan-variant skip: guard-rail refusal under %s: %s",
                        label, variant.error,
                    )
                    continue
                if baseline.ok:
                    return Candidate(
                        id=new_candidate_id("pv-err", query, label),
                        kind=KIND_PLAN_VARIANT,
                        schema_sqls=schema_sqls,
                        inserts=inserts,
                        q1=query,
                        r1_summary=baseline.summary(),
                        r2_summary={**variant.summary(), "variant": label},
                        category=category,
                        rewrite_kind=rewrite_kind,
                        notes=f"variant {label} errors while baseline succeeds",
                    )
                continue
            if variant.bag() != base_bag:
                missing = base_bag - variant.bag()
                extra = variant.bag() - base_bag
                return Candidate(
                    id=new_candidate_id("pv", query, label),
                    kind=KIND_PLAN_VARIANT,
                    schema_sqls=schema_sqls,
                    inserts=inserts,
                    q1=query,
                    r1_summary=baseline.summary(),
                    r2_summary={
                        **variant.summary(),
                        "variant": label,
                        "rows_missing_in_variant": [
                            list(r) for r in list(missing.elements())[:8]
                        ],
                        "rows_extra_in_variant": [
                            list(r) for r in list(extra.elements())[:8]
                        ],
                    },
                    category=category,
                    rewrite_kind=rewrite_kind,
                    notes=(
                        f"plan-variant mismatch under {label}: baseline "
                        f"{baseline.summary()['row_count']} rows vs variant "
                        f"{variant.summary()['row_count']} rows"
                    ),
                )
        return None
