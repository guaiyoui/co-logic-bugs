"""Bug Hunter agent: LLM-guided input generation, deterministic oracles.

The LLM proposes databases, queries, and semantically-equivalent rewrites.
Whether a bug exists is decided only by the deterministic oracles in
``oracles/`` — never by the model.

Engine-agnostic: the hunter talks to any runner exposing
``setup/run/explain_plan/close`` (DuckDB, embedded PostgreSQL, ...).
Exploration is scheduled by a UCB1 bandit over categories plus coverage
feedback; zero-LLM seed mutations supply a second, cheap candidate stream.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable

from oracles.determinism import is_usable_for_oracles
from oracles.differential import DifferentialOracle
from oracles.equivalence import EquivalenceOracle
from oracles.index_variant import IndexVariantOracle
from oracles.models import Candidate
from oracles.norec import NoRECOracle, norec_applicable
from oracles.plan_variant import PlanVariantOracle
from oracles.tlp import TLPOracle, inject_predicate

from .base_agent import BaseAgent
from .json_utils import call_llm_json

LOGGER = logging.getLogger(__name__)

CATEGORIES = [
    "join_outer",
    "subquery_exists_in",
    "aggregation_groupby_having",
    "window_function",
    "case_when_null_logic",
    "type_cast_coercion",
    "string_like_regex",
    "date_time_arith",
    "distinct_orderby_limit",
    "set_ops_union_except",
    "cte_with",
]

# Categories that generate LIMIT-heavy queries tend to be nondeterministic;
# they start with a wasted-try prior so UCB does not over-explore them.
DEMOTED_PRIORS = {"distinct_orderby_limit": {"tries": 2, "reward": 0.0}}

DIALECT_NOTES = {
    "duckdb": (
        "Target engine is DuckDB. DuckDB-specific SQL is allowed "
        "(e.g. POSITIONAL JOIN, LIST(), STRUCT) but keep most queries portable."
    ),
    "postgres": (
        "Target engine is PostgreSQL 16. Use ONLY standard SQL — no "
        "DuckDB-only syntax (no * EXCLUDE, no LIST(), no POSITIONAL JOIN, no "
        "QUALIFY). Types: use DOUBLE PRECISION (never bare DOUBLE), NUMERIC, "
        "VARCHAR, BOOLEAN, DATE, TIMESTAMP. INTERVAL '1 day' and :: casts OK."
    ),
}


class BugHunter(BaseAgent):
    """Generates test cases via the LLM and screens them with oracles."""

    def __init__(
        self,
        config: dict[str, Any],
        runner_factory: Callable[[], Any],
        engine: str = "duckdb",
        differential: DifferentialOracle | None = None,
        cross_engine: Any | None = None,
        bandit: Any | None = None,
        coverage: Any | None = None,
        seed_mutator: Any | None = None,
        seed_corpus: Any | None = None,
        mode: str = "full",
        seed_mutations_per_iter: int = 30,
        rng: random.Random | None = None,
    ):
        super().__init__(config)
        self.runner_factory = runner_factory
        self.engine = engine
        self.differential = differential
        self.cross_engine = cross_engine
        self.bandit = bandit
        self.coverage = coverage
        self.seed_mutator = seed_mutator
        self.seed_corpus = seed_corpus
        self.mode = mode
        # coverage_only ablation: bandit selection still runs (charged per
        # instantiation in the loop), but verdict outcomes never reach it —
        # scheduling is driven by coverage novelty alone.
        self.feedback_verdicts = mode != "coverage_only"
        self.seed_mutations_per_iter = seed_mutations_per_iter
        self.rng = rng or random.Random(0)

        self.pattern_memory: dict[str, dict[str, int]] = {
            category: {"tries": 0, "true_bugs": 0, "false_positives": 0}
            for category in CATEGORIES
        }
        for cat, prior in DEMOTED_PRIORS.items():
            self.pattern_memory[cat] = {
                "tries": int(prior["tries"]),
                "true_bugs": 0,
                "false_positives": 0,
            }
            if self.bandit is not None:
                self.bandit.ensure(
                    cat, prior_tries=prior["tries"], prior_reward=prior["reward"]
                )
        for category in CATEGORIES:
            if self.bandit is not None:
                self.bandit.ensure(category)
        self.rewrite_memory: dict[str, dict[str, int]] = {}
        self.tlp = TLPOracle()
        self.norec = NoRECOracle()
        self.plan_variant = PlanVariantOracle(engine=engine)
        # Storage-state oracle: index on/off, covering/IOS, HOT chains.
        # PostgreSQL's wrong-result history lives in access paths, not
        # planner flips — the plan-variant layer alone never reaches it.
        self.index_variant = (
            IndexVariantOracle() if engine == "postgres" else None
        )
        self.equiv = EquivalenceOracle()
        self.parse_failures = 0
        self._novel_by_category: dict[str, float] = {}

    # ------------------------------------------------------------- bandit
    def focus_categories(self, k: int = 3) -> list[str]:
        """Pick k arms for this round; random when in ablation mode."""
        if self.mode == "random_category" or self.bandit is None:
            return self.rng.sample(CATEGORIES, min(k, len(CATEGORIES)))
        return self.bandit.select(k)

    def avoid_rewrite_kinds(self) -> list[str]:
        """Rewrite kinds whose proposals mostly turned out false positives."""
        return [
            kind
            for kind, s in self.rewrite_memory.items()
            if s["tries"] >= 2 and s["false_positives"] > s["true_bugs"]
        ]

    def _root_arm_hints(self, focus: list[str]) -> str:
        """Prompt hints for fixer-spawned root-cause arms in focus."""
        if self.bandit is None:
            return ""
        hints = [
            f"Root-cause arm {arm}: {self.bandit.hints[arm]}"
            for arm in focus
            if arm in self.bandit.hints
        ]
        return "\n".join(hints)

    # ------------------------------------------------------------- prompts
    def _generate_database(self, guidelines: dict[str, Any]) -> dict[str, list[str]]:
        """One LLM call producing a small adversarial database."""
        constraints = "\n".join(f"- {c}" for c in guidelines.get("new_constraints", []))
        dialect = DIALECT_NOTES.get(self.engine, f"Target engine is {self.engine}.")
        prompt = f"""
You are a database testing expert. {dialect}
Generate one small test database designed to stress the query engine.

Hard constraints:
- 2 to 3 tables, each with 3 to 6 columns.
- Mix column types: INTEGER, BIGINT, DOUBLE PRECISION, NUMERIC,
  VARCHAR, BOOLEAN, DATE, TIMESTAMP.
- Each table gets 5 to 15 rows via INSERT statements.
- Data MUST include NULLs, 0, negative numbers, empty strings, extreme values
  (e.g. 2147483647, -1e308), and duplicate values.
- Only CREATE TABLE and INSERT statements. No comments inside statements.

Additional constraints from prior rounds:
{constraints or "- (none)"}

Output ONLY a ```json fenced block of the form:
```json
{{"schema": ["CREATE TABLE ..."], "inserts": ["INSERT INTO ..."]}}
```
""".strip()
        try:
            payload = call_llm_json(
                self, prompt, temperature=0.8,
                event="generate_db", max_tokens=1600,
            )
            schema = [str(s) for s in payload.get("schema", [])]
            inserts = [str(s) for s in payload.get("inserts", [])]
            if not schema:
                raise ValueError("LLM returned no schema statements")
            return {"schema": schema, "inserts": inserts}
        except (ValueError, AttributeError, TypeError) as exc:
            self.parse_failures += 1
            LOGGER.error("database generation failed: %s", exc)
            return {"schema": [], "inserts": []}

    def _generate_queries(
        self,
        schema_sqls: list[str],
        inserts: list[str],
        focus: list[str],
        guidelines: dict[str, Any],
        count: int,
        uncovered_ops: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """One LLM call producing ``count`` query specifications."""
        schema_text = "\n".join(schema_sqls + inserts[:6])
        focus_text = ", ".join(focus)
        extra = guidelines.get("focus_categories") or []
        if extra:
            focus_text += " (guideline also suggests: " + ", ".join(extra) + ")"
        hints = self._root_arm_hints(focus)
        hints_text = f"\n{hints}\n" if hints else ""
        uncovered_text = (
            "\nPlan operators not yet exercised (favor queries that reach them): "
            + ", ".join(uncovered_ops)
            if uncovered_ops
            else ""
        )
        dialect = DIALECT_NOTES.get(self.engine, f"Target engine is {self.engine}.")
        playbook_text = guidelines.get("playbook_digest") or ""
        playbook_block = (
            f"\n{playbook_text}\n"
            "If a lesson inspires a query, set \"inspired_by\" to its rule id "
            "(e.g. \"R2\"); otherwise use null. Generalize lessons to THIS "
            "schema — do not copy their exact SQL.\n"
            if playbook_text else ""
        )
        prompt = f"""
You are a database testing expert. {dialect}
Given this database:

{schema_text}

Generate exactly {count} diverse SELECT queries that may trigger DBMS bugs.
Focus categories for this round: {focus_text}.
Valid categories: {", ".join(CATEGORIES + [a for a in (self.bandit.arms if self.bandit else {}) if a.startswith('rc:')])}.
{hints_text}{uncovered_text}{playbook_block}
Rules:
- Each item is a JSON object: {{"select_from": "...", "predicate": ..., "category": "...", "inspired_by": null}}
- "select_from" is a complete SELECT ... FROM ... query WITHOUT a WHERE clause
  (joins, subqueries, GROUP BY, ORDER BY etc. are allowed inside it).
- "predicate" is a boolean WHERE-clause expression over the selected tables,
  or null when the query's structure makes a separate predicate meaningless.
- Do not use non-deterministic functions (random(), now(), current_timestamp)
  and do not rely on row order without ORDER BY.
- HARD RULE: if a query uses LIMIT or OFFSET, its ORDER BY must include a
  column that uniquely identifies rows (e.g. a primary key like id).
- HARD RULE: a window function's ORDER BY must end with a unique column
  (e.g. `... ORDER BY amount, id`) so ties never affect the result.
- Prefer at least 3 of the {count} queries in the focus categories.

Output ONLY a ```json fenced block containing the array of {count} objects.
""".strip()
        try:
            payload = call_llm_json(
                self, prompt, temperature=0.9,
                event="generate_queries", max_tokens=2600,
            )
            if not isinstance(payload, list):
                raise ValueError("query payload is not a list")
            queries = []
            for item in payload[:count]:
                if not isinstance(item, dict) or "select_from" not in item:
                    continue
                queries.append(
                    {
                        "select_from": str(item["select_from"]).strip().rstrip(";"),
                        "predicate": (
                            str(item["predicate"]).strip()
                            if item.get("predicate")
                            else None
                        ),
                        "category": str(item.get("category", "unknown")),
                        "inspired_by": (
                            str(item["inspired_by"]).strip()
                            if item.get("inspired_by")
                            else ""
                        ),
                    }
                )
            return queries
        except (ValueError, TypeError) as exc:
            self.parse_failures += 1
            LOGGER.error("query generation failed: %s", exc)
            return []

    def _generate_rewrites(
        self, queries: list[dict[str, Any]], schema_sqls: list[str]
    ) -> list[dict[str, Any]]:
        """One LLM call rewriting every generated query equivalently."""
        originals = []
        for index, item in enumerate(queries):
            full = item["select_from"]
            if item.get("predicate"):
                full = inject_predicate(full, item["predicate"])
            originals.append({"index": index, "query": full})
        if not originals:
            return []
        avoid = self.avoid_rewrite_kinds()
        avoid_text = (
            "\nAvoid these rewrite kinds; they previously produced non-equivalent"
            f" queries: {', '.join(avoid)}."
            if avoid
            else ""
        )
        dialect = DIALECT_NOTES.get(self.engine, f"Target engine is {self.engine}.")
        prompt = f"""
You are a database expert. {dialect}
Given a database schema and input SQL queries, rewrite each query so it is
semantically equivalent — it must return exactly the same results on any
valid data — but is likely to trigger a significantly different query plan.

Constraints:
- The rewritten query must be logically equivalent to the original.
- Do not change the schema. The result must remain identical on any data.
- Useful rewrite kinds: join_to_subquery, subquery_to_join, predicate_pushdown,
  demorgan, case_to_coalesce, in_to_exists, union_expansion, cast_reordering,
  exists_to_in, not_exists_anti_join, decorrelated_subquery.
- FORBIDDEN: non-deterministic functions (random(), now(), current_date),
  relying on row order, or adding/removing ORDER BY in a way that changes
  results.
{avoid_text}

Schema:
{chr(10).join(schema_sqls)}

For each input query produce 1-2 rewrites. Output ONLY a ```json fenced block
with an array of objects:
```json
[{{"original_index": 0, "rewrites": ["SELECT ...", "SELECT ..."], "rewrite_kind": "join_to_subquery"}}]
```

Input queries:
{chr(10).join(f"[{o['index']}] {o['query']}" for o in originals)}
""".strip()
        try:
            payload = call_llm_json(
                self, prompt, temperature=0.7,
                event="generate_rewrites", max_tokens=2600,
            )
            if not isinstance(payload, list):
                raise ValueError("rewrite payload is not a list")
            return [item for item in payload if isinstance(item, dict)]
        except (ValueError, TypeError) as exc:
            self.parse_failures += 1
            LOGGER.error("rewrite generation failed: %s", exc)
            return []

    # ------------------------------------------------------- coverage hooks
    def _observe_coverage(
        self, runner: Any, sql: str, setup_sqls: list[str], category: str
    ) -> int:
        if self.coverage is None:
            return 0
        try:
            from coverage.features import extract_cells

            novel = self.coverage.observe(extract_cells(runner, sql, setup_sqls))
        except Exception:  # noqa: BLE001 - coverage must never break a round
            LOGGER.debug("coverage extraction failed", exc_info=True)
            novel = 0
        self._novel_by_category[category] = (
            self._novel_by_category.get(category, 0.0) + novel
        )
        return novel

    def _uncovered_plan_ops(self) -> list[str]:
        """Common physical operators not yet seen in coverage cells."""
        common = [
            "HASH_JOIN", "NESTED_LOOP", "MERGE_JOIN", "DELIM_JOIN",
            "LEFT_DELIM_JOIN", "NESTED_LOOP_JOIN", "SEQ_SCAN", "SEQ SCAN",
            "INDEX_SCAN", "INDEX SCAN", "WINDOW", "SUBQUERY_SCAN",
            "SUBQUERY SCAN", "RECDATA", "UNNEST", "PIECEWISE_MERGE_JOIN",
        ]
        seen = {c.split(":", 1)[1] for c in (self.coverage.cells() if self.coverage else set()) if c.startswith("plan:")}
        return [op for op in common if op not in seen][:5]

    # --------------------------------------------------------- seed stream
    def _seed_candidates(self) -> tuple[list[Candidate], int]:
        """Zero-LLM candidates from rule-mutated regression seeds."""
        if self.seed_mutator is None or self.seed_corpus is None:
            return [], 0
        candidates: list[Candidate] = []
        skipped = 0
        seeds = self.seed_corpus.for_engine(
            "duckdb" if self.engine == "duckdb" else "postgres"
        ).sample(max(1, self.seed_mutations_per_iter // 4), self.rng)
        runner = self.runner_factory()
        try:
            for seed in seeds:
                for mut in self.seed_mutator.mutations_for(seed):
                    outcomes = runner.setup(mut.setup_sqls)
                    if any(err for _, err in outcomes):
                        continue
                    full = inject_predicate(mut.select_from, mut.predicate) if mut.predicate else mut.select_from
                    if not is_usable_for_oracles(full, runner, mut.setup_sqls):
                        skipped += 1
                        continue
                    self._observe_coverage(runner, full, mut.setup_sqls, mut.category)
                    hit = self.tlp.check(
                        runner,
                        mut.select_from,
                        mut.predicate,
                        mut.setup_sqls,
                        [],
                        category=mut.category,
                    )
                    if hit:
                        candidates.append(hit)
                    hit = self.norec.check(
                        runner,
                        mut.select_from,
                        mut.predicate,
                        mut.setup_sqls,
                        [],
                        category=mut.category,
                    )
                    if hit:
                        candidates.append(hit)
                    hit = self.plan_variant.check(
                        runner,
                        full,
                        mut.setup_sqls,
                        [],
                        category=mut.category,
                    )
                    if hit:
                        candidates.append(hit)
        finally:
            runner.close()
        return candidates, skipped

    # -------------------------------------------------------------- execute
    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """Run one hunt round: generate -> screen -> return candidates."""
        count = int(context.get("queries_per_iter", 8))
        guidelines = context.get("guidelines", {})
        calls_before = self.performance_metrics["total_calls"]
        self._novel_by_category = {}

        candidates: list[Candidate] = []
        queries: list[dict[str, Any]] = []
        rewrites: list[dict[str, Any]] = []
        skipped_nondeterministic = 0
        seed_candidates: list[Candidate] = []
        seed_skipped = 0

        # Seed-mutation stream first: zero LLM cost, always available.
        seed_candidates, seed_skipped = self._seed_candidates()
        candidates.extend(seed_candidates)
        skipped_nondeterministic += seed_skipped

        db = {"schema": [], "inserts": []}
        if self.mode != "no_llm":
            # Step 1: database generation (1 LLM call).
            db = self._generate_database(guidelines)

        runner = self.runner_factory()
        try:
            outcomes = runner.setup(db["schema"] + db["inserts"])
            failed = {sql for sql, err in outcomes if err}
            schema_sqls = [s for s in db["schema"] if s not in failed]
            inserts = [s for s in db["inserts"] if s not in failed]
            if failed:
                LOGGER.info("dropped %d failing setup statements", len(failed))

            diff_queries: list[str] = []
            diff_metas: list[dict[str, Any]] = []
            setup_sqls = schema_sqls + inserts
            full_by_index: dict[int, str] = {}
            usable: set[int] = set()

            if schema_sqls and self.mode != "no_llm":
                # Step 2: query generation (1 LLM call).
                queries = self._generate_queries(
                    schema_sqls,
                    inserts,
                    self.focus_categories(3),
                    guidelines,
                    count,
                    uncovered_ops=self._uncovered_plan_ops(),
                )
                for item in queries:
                    self.pattern_memory.setdefault(
                        item["category"],
                        {"tries": 0, "true_bugs": 0, "false_positives": 0},
                    )
                    self.pattern_memory[item["category"]]["tries"] += 1
                    if self.bandit is not None:
                        self.bandit.ensure(item["category"])

                # Step 3: rewrite generation (1 LLM call).
                rewrites = self._generate_rewrites(queries, schema_sqls)

                # Step 4: deterministic oracles over every usable query.
                for q_index, item in enumerate(queries):
                    full = item["select_from"]
                    if item.get("predicate"):
                        full = inject_predicate(full, item["predicate"])
                    full_by_index[q_index] = full
                    if not is_usable_for_oracles(full, runner, setup_sqls):
                        skipped_nondeterministic += 1
                        LOGGER.info(
                            "skipping nondeterministic query: %.120s", full
                        )
                        continue
                    usable.add(q_index)
                    self._observe_coverage(
                        runner, full, setup_sqls, item["category"]
                    )
                    insp = item.get("inspired_by") or ""
                    hit = self.tlp.check(
                        runner,
                        item["select_from"],
                        item.get("predicate"),
                        schema_sqls,
                        inserts,
                        category=item["category"],
                    )
                    if hit:
                        hit.inspired_by = insp
                        candidates.append(hit)
                    hit = self.norec.check(
                        runner,
                        item["select_from"],
                        item.get("predicate"),
                        schema_sqls,
                        inserts,
                        category=item["category"],
                    )
                    if hit:
                        hit.inspired_by = insp
                        candidates.append(hit)
                    hit = self.plan_variant.check(
                        runner,
                        full,
                        schema_sqls,
                        inserts,
                        category=item["category"],
                    )
                    if hit:
                        hit.inspired_by = insp
                        candidates.append(hit)
                    if self.index_variant is not None:
                        hit = self.index_variant.check(
                            runner,
                            full,
                            schema_sqls,
                            inserts,
                            category=item["category"],
                        )
                        if hit:
                            hit.inspired_by = insp
                            candidates.append(hit)
                    if self.cross_engine is not None:
                        hit = self.cross_engine.check(
                            runner,
                            full,
                            schema_sqls,
                            inserts,
                            category=item["category"],
                        )
                        if hit:
                            hit.inspired_by = insp
                            candidates.append(hit)
                    diff_queries.append(full)
                    diff_metas.append(
                        {"category": item["category"], "rewrite_kind": "",
                         "inspired_by": insp}
                    )

                rewrite_index: dict[int, list[dict[str, Any]]] = {}
                for item in rewrites:
                    try:
                        rewrite_index.setdefault(
                            int(item["original_index"]), []
                        ).append(item)
                    except (KeyError, TypeError, ValueError):
                        continue
                for index, item in enumerate(queries):
                    if index not in usable:
                        continue
                    for entry in rewrite_index.get(index, []):
                        kind = str(entry.get("rewrite_kind", ""))
                        self.rewrite_memory.setdefault(
                            kind or "unknown",
                            {"tries": 0, "true_bugs": 0, "false_positives": 0},
                        )
                        for rewritten in entry.get("rewrites", [])[:2]:
                            rewritten = str(rewritten).strip().rstrip(";")
                            if not rewritten:
                                continue
                            self.rewrite_memory[kind or "unknown"]["tries"] += 1
                            if not is_usable_for_oracles(
                                rewritten, runner, setup_sqls
                            ):
                                skipped_nondeterministic += 1
                                continue
                            original_full = full_by_index.get(index)
                            if not original_full:
                                continue
                            hit = self.equiv.check(
                                runner,
                                original_full,
                                rewritten,
                                schema_sqls,
                                inserts,
                                category=item["category"],
                                rewrite_kind=kind,
                            )
                            if hit:
                                hit.inspired_by = item.get("inspired_by") or ""
                                candidates.append(hit)
                            if self.cross_engine is not None:
                                xhit = self.cross_engine.check(
                                    runner,
                                    rewritten,
                                    schema_sqls,
                                    inserts,
                                    category=item["category"],
                                    rewrite_kind=kind,
                                )
                                if xhit:
                                    xhit.inspired_by = item.get("inspired_by") or ""
                                    candidates.append(xhit)
                            diff_queries.append(rewritten)
                            diff_metas.append(
                                {
                                    "category": item["category"],
                                    "rewrite_kind": kind,
                                    "paired_query": original_full,
                                    "inspired_by": item.get("inspired_by") or "",
                                }
                            )

                # Cross-version differential over everything executed this
                # round (DuckDB only; PostgreSQL has no second local version).
                if self.differential is not None and self.differential.available:
                    candidates.extend(
                        self.differential.check(
                            schema_sqls,
                            diff_queries,
                            metas=diff_metas,
                            local_runner=runner,
                            inserts=inserts,
                        )
                    )
        finally:
            runner.close()

        return {
            "bugs": [c.to_dict() for c in candidates],
            "db": {
                "schema": db["schema"],
                "inserts": db["inserts"],
                "failed": [],
            },
            "queries": queries,
            "rewrites": rewrites,
            "llm_calls": self.performance_metrics["total_calls"] - calls_before,
            "skipped_nondeterministic": skipped_nondeterministic,
            "novel_cells": self._novel_by_category,
            "coverage_size": self.coverage.size() if self.coverage else 0,
        }

    # ------------------------------------------------------------- feedback
    def learn_from_feedback(self, feedback: dict[str, Any]) -> None:
        """Update bandit + memories from a triaged candidate verdict."""
        verdict = feedback.get("verdict")
        category = feedback.get("category", "unknown")
        novel = int(feedback.get("novel_cells", 0))
        stats = self.pattern_memory.setdefault(
            category, {"tries": 0, "true_bugs": 0, "false_positives": 0}
        )
        if verdict == "true_bug":
            stats["true_bugs"] += 1
        elif verdict in (
            "false_positive",
            "version_divergence",
            "cross_engine_divergence",
            "skipped_nondeterministic",
        ):
            stats["false_positives"] += 1

        if (self.bandit is not None and self.mode in ("full", "dce_only",
                "shuffled_diag") and self.feedback_verdicts):
            self.bandit.reward(
                category,
                true_bug=(verdict == "true_bug"),
                wasted=verdict
                in (
                    "false_positive",
                    "skipped_nondeterministic",
                ),
            )

        kind = feedback.get("rewrite_kind")
        if kind:
            rstats = self.rewrite_memory.setdefault(
                kind, {"tries": 0, "true_bugs": 0, "false_positives": 0}
            )
            if verdict == "true_bug":
                rstats["true_bugs"] += 1
            elif verdict in (
                "false_positive",
                "version_divergence",
                "cross_engine_divergence",
                "skipped_nondeterministic",
            ):
                rstats["false_positives"] += 1
