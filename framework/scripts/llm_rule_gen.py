"""LLM-driven per-rule generation: ask the model for queries that stress
each optimizer rule (= each optimization direction), screen the outputs
through the sound oracles, and run the hits through the full pipeline.

This is the directed version of Hunter: instead of free schema/query
generation, every call is conditioned on one optimizer rule, so the
generation space covers *newer and niche directions* systematically
(window_self_join, top_n_window_elimination, materialized_cte, ...).

    DEEPSEEK_API_KEY=... python scripts/llm_rule_gen.py \
        --engine duckdb --per-rule 6 --out results/llm_rules
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base_agent import BaseAgent  # noqa: E402
from agents.json_utils import call_llm_json  # noqa: E402
from agents.bug_fixer import BugFixer  # noqa: E402
from diagnosis.signature import SignatureEngine  # noqa: E402
from evolution.co_evolution import CoEvolution  # noqa: E402
from evolution.dce import FamilyExpander  # noqa: E402
from llm.ledger import ledger  # noqa: E402
from oracles.db_runner import DuckDBRunner  # noqa: E402
from oracles.determinism import is_usable_for_oracles  # noqa: E402
from oracles.norec import NoRECOracle  # noqa: E402
from oracles.plan_variant import PlanVariantOracle  # noqa: E402
from util.efficiency import eff  # noqa: E402

LOGGER = logging.getLogger("llm_rule_gen")

# Per-rule structural hints: what shapes actually reach each rule. Without
# these the model drifts to superficial syntax (e.g. USING SAMPLE) instead
# of the rule's real trigger structure.
RULE_HINTS = {
    "expression_rewriter": "CASE/WHEN chains, De Morgan on NULLs, type coercion in predicates, constant folding edges, BETWEEN expansion",
    "filter_pullup": "filters above UNION/EXCEPT/setops, WHERE over aggregated or windowed subqueries, filters over outer-join output",
    "filter_pushdown": "predicates through joins/CTEs/subqueries, pushdown past window functions, NULL keys on both join sides",
    "empty_result_pullup": "empty tables feeding aggregates (COUNT->0, SUM->NULL), empty join sides, WHERE-false subqueries, LIMIT 0",
    "cte_filter_pusher": "outer predicates pushed into multiply-referenced CTEs, filters on windowed or grouped CTEs",
    "regex_range": "LIKE 'prefix%', ILIKE, ~ regex on scanned columns, prefix patterns over NULLs and case variants",
    "in_clause": "IN lists containing NULL, large IN lists, IN subqueries with empty/NULL source, cross-type IN",
    "join_order": "ASOF/POSITIONAL joins with tied keys, multi-way joins with selective predicates, self-join chains with size skew",
    "deliminator": "correlated EXISTS/IN scalar subqueries, correlation through aggregates, double-nested correlation, EXISTS plus ORDER BY",
    "unnest_rewriter": "UNNEST in WHERE vs SELECT vs lateral, empty/NULL lists, struct arrays, multi-argument unnest",
    "unused_columns": "wide-row tables projecting a small column subset, joins whose output columns get pruned",
    "statistics_propagation": "skewed distributions, integer boundary values, empty ranges, all-NULL columns, estimate-sensitive join order",
    "common_subexpressions": "the same complex expression in SELECT/WHERE/GROUP BY, shared CASE subexpressions",
    "common_aggregate": "identical aggregates at different groupings (ROLLUP/GROUPING SETS), duplicated aggregate calls",
    "column_lifetime": "wide intermediate results projected down late, projections feeding joins",
    "limit_pushdown": "LIMIT through join/union/subquery/window, LIMIT with OFFSET, tie rows at the limit boundary",
    "row_group_pruner": "range predicates on ordered data, BETWEEN boundaries, zone-map candidates with NULLs",
    "top_n": "ORDER BY+LIMIT with tie rows at the boundary, multi-key sorts, top-n inside subqueries under outer filters",
    "top_n_window_elimination": "ROW_NUMBER/RANK filtered rn<=k, QUALIFY equivalents, mixed ranking functions, ties at k",
    "build_side_probe_side": "tiny vs large join sides, joins on all-NULL keys, build/probe swap opportunities",
    "compressed_materialization": "wide payload rows under top-n, dictionary-compressible VARCHAR columns through joins",
    "duplicate_groups": "duplicate GROUP BY expressions, identical aggregate calls, grouping on computed expressions",
    "reorder_filter": "conjuncts of very different cost/selectivity (int compare vs string function), NULL-producing predicates",
    "sampling_pushdown": "TABLESAMPLE/USING SAMPLE only with REPEATABLE(seed) (unseeded sampling is nondeterministic), sampled subqueries inside joins and filters",
    "join_filter_pushdown": "ON-clause vs WHERE-clause predicates on outer joins, selective post-join filters, NULL-safe comparisons",
    "extension": "any planner-visible structure; generic edge combos are fine",
    "materialized_cte": "MATERIALIZED hint with several references, CTE referenced inside subqueries and joins",
    "sum_rewriter": "SUM over CASE, SUM(a)+SUM(b) vs SUM(a+b), FILTERed sums, int/float sum equivalence, negative/positive splits",
    "late_materialization": "top-n over wide rows, self-joins projecting few columns, ORDER BY on unselected columns",
    "cte_inlining": "single-use CTE under outer filters, CTE inside EXISTS, nested CTEs, CTE with window then outer predicate",
    "common_subplan": "the same scalar subquery in multiple SELECT items or in SELECT+WHERE, shared uncorrelated subplans",
    "join_elimination": "PK/FK joins where one side is never projected, joins on unique columns under LIMIT",
    "window_self_join": "several windows over the same or different partitions, DUPLICATE partition keys, mixed frames, PARTITION BY repeated columns",
}

# Under-fuzzed feature areas — an orthogonal generation axis to rules.
FEATURE_TOPICS = {
    "asof_join": "ASOF JOIN with every inequality direction (>=, <=, >, <) on the ON clause, tied boundary keys, NULL keys, extra equality conditions, joining a subquery, mismatched side lengths",
    "positional_iejoin": "POSITIONAL JOIN (unequal lengths, NULLs, combined with filters/other joins) and multi-inequality joins (t1.x < t2.y AND t1.a < t2.b, mixed <,<=,>,>= over ints/dates/strings, self-joins) — IEJoin territory",
    "pivot_unpivot": "PIVOT/UNPIVOT: several aggregates in one PIVOT, generated column-name synthesis and name collisions, NULLs in pivoted data, duplicate keys, unpivot of NULL columns, pivot on expressions",
    "qualify": "QUALIFY: rank ties at the filter boundary, QUALIFY combined with GROUP BY/HAVING, QUALIFY over multiple window specs or a named window (WINDOW w AS ...)",
    "window_named": "named WINDOW clause: WINDOW w AS (...), OVER w, OVER (w ORDER BY ...) extending a named spec, one named window referencing another, frames attached to named windows, mixing named and inline specs",
    "grouping_sets": "ROLLUP/CUBE/GROUPING SETS: NULLs inside grouping keys, GROUPING_ID disambiguation, grouping over expressions",
    "nested_types": "STRUCT/LIST/MAP: struct equality and field access, unnest of struct arrays, empty/NULL collections, nested-type columns as GROUP BY/DISTINCT/JOIN keys, nested types inside IN lists",
    "map_type": "MAP type and map functions: map()/MAP{...} literals, map_keys/map_values/map_entries/map_concat, element_at(m,k) and m[k] lookups incl. missing and NULL keys, duplicate keys, maps as grouping/join keys",
    "lambda_fns": "lambda functions: list_transform/list_filter/list_zip/list_distinct/list_aggregate, map_entries/map_filter lambdas, x -> expr arrow syntax, multi-argument lambdas, lambdas capturing outer columns, lambdas over empty/NULL lists",
    "decimal_hugeint": "DECIMAL(38)/HUGEINT/edge integers: scale boundaries, arithmetic near overflow, mixed-precision comparisons and aggregations",
    "int_overflow": "casts and arithmetic at integer bounds: HUGEINT->INT/BIGINT casts that overflow, -(-9223372036854775808), DECIMAL(38) widening overflow, out-of-range string->int casts, bitwise ops (& | ^ ~ << >>) and get_bit/set_bit/bit_count on HUGEINT min/max/-1, shifts >= the bit width",
    "interval_date": "INTERVAL/DATE/TIME: month-end arithmetic, unit-equivalent intervals ('1 year' vs '12 months'), negative intervals, date comparisons across types",
    "time_edges": "DATE/TIME/TIMESTAMP/TIMETZ: leap-adjacent values, DATE +/- INTERVAL chains, EXTRACT fields at boundaries, epoch 0, TIMETZ literals with fixed offsets compared across offsets",
    "window_frames": "window frame edges: GROUPS frame with peer ties, RANGE with multiple ORDER BY keys, IGNORE NULLS, nth_value/lag/lead offsets, duplicate partition keys across coexisting windows",
    "union_star_sugar": "UNION BY NAME/CORRESPONDING, SELECT * EXCLUDE/REPLACE, COLUMNS() regex — with reordered or disjoint column sets",
    "semi_anti_join": "SEMI/ANTI joins, IN/EXISTS/NOT IN, and row-value tuples (a,b) IN ((1,2),(3,4)) or IN (SELECT x,y ...): NULL join keys, NULLs inside tuples, duplicate keys, correlation through aggregates",
    "filtered_aggs": "FILTER clause aggregates, countif/arg_max/string_agg with ORDER BY, DISTINCT aggregates, nested window-over-aggregate",
    "ordered_aggs": "order-sensitive aggregates: FIRST/LAST/any_value/arg_max/arg_min with an ORDER BY argument and tie rows, mode(), quantile_disc/quantile_cont at q=0/0.5/1 over duplicates and NULLs, string_agg/group_concat with inner ORDER BY and separators",
    "lateral_corr": "CROSS JOIN LATERAL, scalar correlated subqueries in SELECT, multiple correlated items, correlation inside expressions",
    "distinct_exprs": "DISTINCT/GROUP BY over expressions and nested-type keys (GROUP BY on a STRUCT or LIST column), mixed-type grouping keys, NULL grouping semantics, CASE in keys",
    "enum_union": "ENUM (CREATE TYPE ... AS ENUM) and UNION types: enum->varchar and varchar->enum casts incl. invalid labels, enum ORDER BY position vs lexicographic order, joins on enums, union-typed columns compared across member types",
    "variant_type": "VARIANT type: 'lit'::VARIANT and CAST(col AS VARIANT) for scalars/structs/lists, variant comparisons and sort order, variant columns in GROUP BY/JOIN keys, reading variant members back",
    "collate": "COLLATE NOCASE/NOACCENT in comparisons, GROUP BY/DISTINCT keys, joins, LIKE; plus identifier case-sensitivity: quoted \"Col\" vs col vs COL, columns differing only by case in one table or across join sides",
    "bit_blob": "BITSTRING/BLOB and UUID literals: bit ops, bit_and/bit_or aggregates, BLOB comparisons, hex/base64/encode/decode round-trips in GROUP BY keys, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::UUID casts/compares/sort (never call uuid())",
    "generated_cols": "GENERATED ALWAYS AS and VIRTUAL columns, DEFAULT expressions, CHECK constraints — generated/defaulted columns used in WHERE/GROUP BY/window and inserts that rely on the default",
    "multi_array": "multi-dimensional arrays INT[][], list slicing l[a:b] and l[a:] with negative/NULL/out-of-range bounds and step, string slicing 'abc'[2:4], array_agg/list_concat/list_slice boundaries",
    "struct_pack": "struct_pack/row() construction and comparison: {a:1,b:2} = row(1,2), struct < struct ordering, structs inside IN lists, NULL fields in comparisons, bracket/index access s['a'] and s[1], struct_extract, nested structs",
    "setops_edges": "UNION/INTERSECT/EXCEPT across different types and NULL columns, UNION ALL vs UNION dedup with NULLs, UNION ALL of overlapping ranges feeding DISTINCT/GROUP BY, setops inside CTEs and subqueries",
    "recursive_cte": "WITH RECURSIVE edge cases: UNION vs UNION ALL dedup inside recursion, termination via LIMIT or a depth guard in WHERE, multiple recursive references, recursive term containing aggregates or joins, USING KEY / cycle-shaped graphs",
    "attach_crossdb": "ATTACH ':memory:' AS db2 then fully-qualified db2.main.t references: cross-database joins, UNION across databases, INSERT INTO db2.main.t SELECT in setup, information_schema filtered by table_catalog",
    "file_io": "setup_sqls write files via COPY (SELECT ...) TO '/tmp/t_NAME.csv' (HEADER) or '.parquet', the query reads them back with read_csv(...,auto_detect=true) or read_parquet — quoted commas/newlines, all-NULL columns, type-inconsistent rows, DECIMAL/TIMESTAMP/BLOB round-trip fidelity",
    "sample_seeded": "TABLESAMPLE/USING SAMPLE with REPEATABLE(seed) only: bernoulli/system/reservoir, sample sizes 0/1/100 percent or fixed row counts, a sampled subquery inside a join or filter",
    "macros": "CREATE MACRO scalar and table functions, macros taking lambda parameters (x -> x*2), macro calls inside WHERE/aggregates/GROUP BY, macro calling macro, edge-case inputs (NULLs, boundary values)",
}


class RuleGenerator(BaseAgent):
    """Thin BaseAgent shell for rule-conditioned query generation."""

    _event_prefix = "rule_gen"

    def generate_for_rule(
        self, rule: str, count: int, hints: str | None = None,
        intro: str | None = None,
    ) -> list[dict]:
        hints = hints or RULE_HINTS.get(
            rule, "edge cases that exercise this rule")
        intro = intro or (
            f'The DuckDB optimizer has a rule named "{rule}". Write '
            f'{count} diverse, self-contained test cases designed to '
            'stress that rule — especially its edge cases.')
        prompt = f"""
You are a database testing expert targeting DuckDB.
{intro}

Structural hints for "{rule}": {hints}.

The most valuable cases combine the rule's trigger structure with a
second feature (window functions, CTEs, correlated subqueries, set
operations, nested types, NULL/three-valued logic, boundary values).

Output ONLY a ```json fenced block: an array of {count} objects, each:
{{"setup_sqls": ["CREATE TABLE ...", "INSERT INTO ...", ...],
  "query": "SELECT ..."}}
Rules:
- setup_sqls must fully create and populate the tables used by query.
- query must be a single SELECT (writes/DDL go in setup_sqls only).
- The result must be identical on every run and under every planner
  configuration. FORBIDDEN:
  * random(), uuid(), gen_random_uuid(), now(), current_timestamp/
    current_date/current_time, clock_timestamp(), setseed() — any
    function whose value changes per call;
  * TABLESAMPLE or USING SAMPLE without REPEATABLE(seed);
  * ORDER BY on non-unique keys combined with LIMIT/OFFSET — add a
    unique tiebreaker column instead;
  * list(), string_agg(), array_agg(), group_concat() or any other
    order-aggregating call WITHOUT an ORDER BY inside the call;
  * window functions (row_number, rank, lag/lead, first_value,
    last_value, nth_value, ntile) whose OVER(ORDER BY ...) key is not
    unique inside each PARTITION BY group — add a unique tiebreaker;
  * any unspecified ordering that changes which rows/values are
    returned.
- When the query returns several rows, end with a top-level ORDER BY
  on unique keys so the output is fully determined.
- Prefer SMALL schemas (1-2 tables, <=8 rows) — the point is triggering
  the optimizer path, not volume.
- Vary shapes across the {count} cases: different data, different
  clause combinations; do not just rename columns.
- setup_sqls may write files only under /tmp (e.g. COPY ... TO
  '/tmp/x.csv') and may ATTACH ':memory:' databases.
""".strip()
        payload = call_llm_json(
            self, prompt, temperature=0.9, retries=1,
            event=f"{self._event_prefix}:{rule}", max_tokens=4000,
        )
        if not isinstance(payload, list):
            raise ValueError("payload is not a list")
        return [p for p in payload if isinstance(p, dict) and p.get("query")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["duckdb"], default="duckdb")
    ap.add_argument("--per-rule", type=int, default=6)
    ap.add_argument("--rules", default="",
                    help="comma-separated subset; default: all from "
                         "duckdb_optimizers()")
    ap.add_argument("--features", action="store_true",
                    help="generate per under-fuzzed feature area instead "
                         "of per optimizer rule")
    ap.add_argument("--dce-budget", type=int, default=6000)
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    out_dir = args.out or os.path.join(
        "results", f"llm_rules_{time.strftime('%Y%m%d_%H%M%S')}"
    )
    os.makedirs(out_dir, exist_ok=True)
    ledger.configure(os.path.join(out_dir, "llm_ledger.jsonl"))

    runner_factory = lambda: DuckDBRunner(version_tag="llm_rules")  # noqa: E731
    fixer = BugFixer(
        {"model": "none", "api_key": "", "disable_llm": True},
        runner_factory=runner_factory, reference_factory=None,
        target_engine=args.engine,
    )
    signature = SignatureEngine(
        runner_factory=runner_factory, checker=fixer.checker,
        engine=args.engine,
    )
    expander = FamilyExpander(
        runner_factory=runner_factory, engine=args.engine,
        max_probes=200, max_executed=120,
    )
    coevo = CoEvolution(
        hunter=None, fixer=fixer, guideline_generator=None,
        results_dir=out_dir, signature_engine=signature,
        expander=expander, dce_budget=args.dce_budget,
    )

    gen = RuleGenerator(
        {"model": args.model, "api_key": "${DEEPSEEK_API_KEY}",
         "base_url": args.base_url}
    )
    gen._event_prefix = "feat_gen" if args.features else "rule_gen"
    runner = runner_factory()
    if args.features:
        names = sorted(FEATURE_TOPICS)
        hints_of = dict(FEATURE_TOPICS)
        intros = {
            n: (f'Write {args.per_rule} diverse, self-contained test '
                f'cases stressing the DuckDB feature area "{n}" — '
                'especially its edge cases.')
            for n in names
        }
    else:
        names = [
            r[0] for r in runner.run("SELECT * FROM duckdb_optimizers()").rows
        ]
        hints_of = dict(RULE_HINTS)
        intros = {}
    if args.rules:
        wanted = set(args.rules.split(","))
        names = [n for n in names if n in wanted]
    LOGGER.info("generating for %d targets x%d", len(names), args.per_rule)

    norec, pv = NoRECOracle(), PlanVariantOracle(args.engine)
    stats = {"rules": len(names), "generated": 0, "setup_ok": 0,
             "exec_ok": 0, "oracle_hits": 0, "families": 0,
             "duplicates": 0, "nonbug": {}}
    t0 = time.time()
    try:
        for rule in names:
            try:
                with eff.phase("llm_generate"):
                    cases = gen.generate_for_rule(
                        rule, args.per_rule,
                        hints=hints_of.get(rule),
                        intro=intros.get(rule),
                    )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("rule %s generation failed: %s", rule, exc)
                continue
            stats["generated"] += len(cases)
            for ci, case in enumerate(cases):
                setup = case.get("setup_sqls") or []
                q = case["query"]
                outcomes = runner.setup(setup)
                if any(err for _, err in outcomes):
                    continue
                stats["setup_ok"] += 1
                if not is_usable_for_oracles(q, runner, setup):
                    continue
                stats["exec_ok"] += 1
                eff.count("queries_executed")
                with eff.phase("screen"):
                    hits = [
                        h for h in (
                            pv.check(runner, q, setup, [],
                                     category=f"rule:{rule}"),
                            norec.check(runner, q, None, setup, [],
                                        category=f"rule:{rule}"),
                        )
                        if h is not None
                    ]
                for hit in hits:
                    stats["oracle_hits"] += 1
                    with eff.phase("ingest"):
                        verdict, key = coevo.ingest_hit(
                            hit, iteration=f"rule:{rule}#{ci}")
                    if verdict == "true_bug":
                        stats["families"] += 1
                        LOGGER.info("NEW FAMILY %s via rule %s", key, rule)
                    elif verdict == "duplicate":
                        stats["duplicates"] += 1
                    else:
                        stats["nonbug"][verdict] = \
                            stats["nonbug"].get(verdict, 0) + 1
            coevo._write_families()
            LOGGER.info(
                "rule %s done: gen=%d exec_ok=%d hits=%d fam=%d",
                rule, len(cases), stats["exec_ok"],
                stats["oracle_hits"], stats["families"],
            )
    finally:
        runner.close()
        coevo._write_families()
        with open(os.path.join(out_dir, "bugs.jsonl"), "w") as fh:
            for b in coevo.true_bugs:
                fh.write(json.dumps(b, default=str) + "\n")

    summary = {
        **stats,
        "distinct_families": len(coevo.bug_index),
        "dce_executed": coevo.dce_executed,
        "efficiency": eff.snapshot(),
        "ledger": ledger.totals(),
        "elapsed_s": time.time() - t0,
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=2))
    print(f"Artifacts in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
