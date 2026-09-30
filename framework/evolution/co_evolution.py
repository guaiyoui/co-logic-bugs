"""Co-evolution loop: hunter generates, deterministic oracles screen,
fixer minimizes and triages, both sides learn from the verdicts.

The loop is evidence-gated: only oracle-observed discrepancies become
candidates, and only triaged ``true_bug`` verdicts are archived.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from oracles.models import (
    VERDICT_CROSS_ENGINE_DIVERGENCE,
    VERDICT_FALSE_POSITIVE,
    VERDICT_SKIPPED_NONDETERMINISTIC,
    VERDICT_TRUE_BUG,
    VERDICT_UNVERIFIED,
    VERDICT_VERSION_DIVERGENCE,
    Candidate,
    reproducible_script,
)
from llm.ledger import ledger
from diagnosis.bisect import prelude_for_names
from util.efficiency import eff

LOGGER = logging.getLogger(__name__)


class CoEvolution:
    """Drives the Hunter <-> Fixer co-evolution over iterations."""

    def __init__(
        self,
        hunter: Any,
        fixer: Any,
        guideline_generator: Any,
        results_dir: Path | str,
        stagnation_limit: int = 3,
        signature_engine: Any | None = None,
        expander: Any | None = None,
        dce_budget: int = 2000,
        shuffle_sigma: bool = False,
        playbook: Any | None = None,
        rng: Any | None = None,
    ):
        self.hunter = hunter
        self.fixer = fixer
        self.guidelines = guideline_generator
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.stagnation_limit = stagnation_limit
        # sigma-based diagnosis + DCE family expansion (None = disabled)
        self.signature_engine = signature_engine
        self.expander = expander
        self.dce_budget = dce_budget
        self.dce_executed = 0
        # shuffled-diagnosis ablation: the σ pipeline still runs (same
        # execution cost), but a new family's identity fields are swapped
        # with a random previously-seen family's before conditioning —
        # the hunter gets real-format, wrong-content guidance.
        self.shuffle_sigma = shuffle_sigma
        self._sigma_pool: list[dict[str, Any]] = []
        # co-evolution playbook: distilled per-family lessons injected back
        # into the hunter prompt, with per-rule attribution counters.
        self.playbook = playbook
        import random as _random
        self.rng = rng or _random.Random(0)
        self.history: list[dict[str, Any]] = []
        self.seen_candidates: set[str] = set()
        self.seen_bugs: set[str] = set()
        self.seen_signatures: set[str] = set()
        self.bug_index: dict[str, dict[str, Any]] = {}
        self.true_bugs: list[dict[str, Any]] = []
        self.families: list[dict[str, Any]] = []
        self.version_divergences: list[dict[str, Any]] = []
        self.iteration_logs: list[dict[str, Any]] = []
        self.cross_engine_divergences: list[dict[str, Any]] = []
        self.unverified_bugs: list[dict[str, Any]] = []
        self.root_arms_spawned: list[dict[str, Any]] = []
        self.metrics_path = self.results_dir / "metrics.jsonl"

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _bug_key(candidate: Candidate, minimal: dict[str, Any] | None) -> str:
        """Dedup key: kind + root-cause feature signature of minimized SQL."""
        return candidate.root_key(
            q1=(minimal or {}).get("q1") or candidate.q1,
            q2=(minimal or {}).get("q2") or candidate.q2,
        )

    def check_convergence(self) -> bool:
        """Converged when no *new deduplicated* true bug for N iterations."""
        recent = self.iteration_logs[-self.stagnation_limit :]
        return len(recent) >= self.stagnation_limit and all(
            item["new_true_bugs"] == 0 for item in recent
        )

    # ------------------------------------------------------------------ run
    def run(self, iterations: int, queries_per_iter: int) -> dict[str, Any]:
        for iteration in range(iterations):
            with eff.phase("guidelines"):
                guidelines = self.guidelines.generate(
                    iteration,
                    self.history,
                    self.hunter.pattern_memory,
                    self.hunter.rewrite_memory,
                )
            if self.playbook is not None:
                guidelines["playbook_digest"] = self.playbook.digest()
            with eff.phase("hunter"):
                hunt = self.hunter.execute(
                    {
                        "iteration": iteration,
                        "guidelines": guidelines,
                        "queries_per_iter": queries_per_iter,
                        "history": self.history[-6:],
                    }
                )
            llm_calls = hunt.get("llm_calls", 0)
            iter_stats = {
                "iteration": iteration,
                "guidelines": guidelines,
                "db": hunt.get("db", {}),
                "queries": hunt.get("queries", []),
                "candidates": [],
                "llm_calls": llm_calls,
                "n_candidates": 0,
                "new_true_bugs": 0,
                "false_positives": 0,
                "version_divergences": 0,
                "skipped_nondeterministic": hunt.get("skipped_nondeterministic", 0),
                "duplicates": 0,
                "coverage_size": hunt.get("coverage_size", 0),
                "novel_cells": sum(hunt.get("novel_cells", {}).values()),
                "inspired_queries": 0,
                "inspired_candidates": 0,
                "inspired_families": 0,
                "playbook_size": (
                    len(self.playbook.rules) if self.playbook else 0
                ),
            }
            if self.playbook is not None:
                for q in hunt.get("queries", []):
                    rid = q.get("inspired_by") or ""
                    if rid:
                        iter_stats["inspired_queries"] += 1
                        self.playbook.note_inspired(rid)

            # Charge the bandit one try per instantiated query, not per
            # produced candidate: categories that generated only
            # zero-candidate queries still consumed budget, and without
            # this their UCB exploration term stays artificially high.
            if self.hunter.bandit is not None:
                novel_by_cat = hunt.get("novel_cells") or {}
                seen_novel: set[str] = set()
                for q in hunt.get("queries", []):
                    cat = q.get("category", "unknown")
                    nov = 0
                    if cat not in seen_novel:
                        nov = int(novel_by_cat.get(cat, 0))
                        seen_novel.add(cat)
                    self.hunter.bandit.update(cat, novel_cells=nov)

            for raw in hunt.get("bugs", []):
                candidate = Candidate.from_dict(raw)
                if candidate.dedup_key() in self.seen_candidates:
                    iter_stats["duplicates"] += 1
                    continue
                self.seen_candidates.add(candidate.dedup_key())
                if self.playbook is not None and candidate.inspired_by:
                    iter_stats["inspired_candidates"] += 1
                    self.playbook.note_candidate(candidate.inspired_by)

                triage = self.fixer.execute({"candidate": raw})
                llm_calls += triage.get("llm_calls", 0)
                verdict = triage.get("verdict", "undetermined")
                minimal = triage.get("minimal_case", {})

                record = {
                    "candidate": candidate.to_dict(),
                    "verdict": verdict,
                    "minimal_case": minimal,
                    "analysis": triage.get("analysis", {}),
                    "difficulty": triage.get("difficulty", ""),
                }
                iter_stats["candidates"].append(record)

                novel = (hunt.get("novel_cells") or {}).get(candidate.category, 0)
                self.hunter.learn_from_feedback(
                    {
                        "category": candidate.category,
                        "rewrite_kind": candidate.rewrite_kind,
                        "verdict": verdict,
                        "novel_cells": novel,
                    }
                )
                self.fixer.learn_from_feedback(
                    {
                        "component": triage.get("analysis", {}).get("component"),
                        "verdict": verdict,
                        "kind": candidate.kind,
                    }
                )
                root_arm = triage.get("root_arm")
                if root_arm and self.hunter.bandit is not None:
                    self.hunter.bandit.add_root_arm(
                        root_arm["id"], root_arm["hint"]
                    )
                    self.root_arms_spawned.append(root_arm)
                    LOGGER.info("fixer spawned root-cause arm %s", root_arm["id"])

                if verdict == VERDICT_TRUE_BUG:
                    sigma = None
                    if self.signature_engine is not None:
                        sigma = self.signature_engine.compute(candidate, minimal)
                    key = self._archive_true_bug(
                        candidate, minimal, sigma, record, iteration,
                        origin="hunter",
                    )
                    if key is None:
                        iter_stats["duplicates"] += 1
                        continue
                    iter_stats["new_true_bugs"] += 1
                    if self.playbook is not None:
                        if candidate.inspired_by:
                            iter_stats["inspired_families"] += 1
                            self.playbook.note_family(candidate.inspired_by)
                        # distill the new family into a lesson for the
                        # NEXT iterations — the accumulating genome.
                        self.playbook.distill(
                            {"repro_sql": None,
                             "minimal_case": minimal,
                             "root_key": key,
                             "analysis": record.get("analysis", {})},
                            sigma, iteration,
                            getattr(self.hunter, "call_llm", None),
                        )
                    # DCE: expand the new family along its diagnosed fault
                    # surface; hits are signed and deduped the same way.
                    if self.expander is not None and self.dce_executed < self.dce_budget:
                        dce_stats = self._expand_family(
                            candidate, minimal,
                            self._conditioning_sigma(sigma), iteration,
                        )
                        iter_stats.setdefault("dce", []).append(dce_stats)
                elif verdict == VERDICT_VERSION_DIVERGENCE:
                    iter_stats["version_divergences"] += 1
                    divergence = {**record, "iteration": iteration}
                    self.version_divergences.append(divergence)
                    self._append_jsonl(
                        self.results_dir / "version_divergences.jsonl", divergence
                    )
                elif verdict == VERDICT_CROSS_ENGINE_DIVERGENCE:
                    iter_stats.setdefault("cross_engine_divergences", 0)
                    iter_stats["cross_engine_divergences"] += 1
                    divergence = {**record, "iteration": iteration}
                    self.cross_engine_divergences.append(divergence)
                    self._append_jsonl(
                        self.results_dir / "cross_engine_divergences.jsonl",
                        divergence,
                    )
                elif verdict == VERDICT_SKIPPED_NONDETERMINISTIC:
                    iter_stats["skipped_nondeterministic"] += 1
                elif verdict == VERDICT_UNVERIFIED:
                    iter_stats.setdefault("unverified_bugs", 0)
                    iter_stats["unverified_bugs"] += 1
                    self.unverified_bugs.append({**record, "iteration": iteration})
                    self._append_jsonl(
                        self.results_dir / "unverified_bugs.jsonl",
                        {**record, "iteration": iteration},
                    )
                elif verdict == VERDICT_FALSE_POSITIVE:
                    iter_stats["false_positives"] += 1

            iter_stats["n_candidates"] = len(iter_stats["candidates"])
            iter_stats["llm_calls"] = llm_calls
            if self.hunter.bandit is not None:
                iter_stats["bandit"] = self.hunter.bandit.snapshot()
            self.iteration_logs.append(
                {k: v for k, v in iter_stats.items() if k != "candidates"}
            )
            self._append_jsonl(
                self.metrics_path,
                {k: v for k, v in iter_stats.items() if k != "candidates"},
            )
            self._write_json(
                self.results_dir / f"iter_{iteration}.json", iter_stats
            )
            LOGGER.info(
                "iter %d: candidates=%d true_bugs=%d fp=%d llm_calls=%d",
                iteration,
                iter_stats["n_candidates"],
                iter_stats["new_true_bugs"],
                iter_stats["false_positives"],
                llm_calls,
            )
            if self.check_convergence():
                LOGGER.info("converged after iteration %d", iteration)
                break

        # Rewrite bugs.jsonl so occurrence counts / case ids are final.
        bugs_path = self.results_dir / "bugs.jsonl"
        bugs_path.write_text(
            "".join(json.dumps(b, default=str) + "\n" for b in self.true_bugs),
            encoding="utf-8",
        )
        self._write_families()
        if self.playbook is not None:
            self.playbook.save(self.results_dir / "playbook.jsonl")
        summary = self._summary()
        self._write_json(self.results_dir / "summary.json", summary)
        return summary

    # ------------------------------------------------- DCE family expansion
    def _archive_true_bug(
        self,
        candidate: Candidate,
        minimal: dict[str, Any],
        sigma: dict[str, Any] | None,
        record: dict[str, Any],
        iteration: Any,
        origin: str,
        extra: dict[str, Any] | None = None,
    ) -> str | None:
        """Dedup + archive one confirmed bug; returns family key or None."""
        key = sigma["key"] if sigma else self._bug_key(candidate, minimal)
        if key in self.bug_index or key in self.seen_signatures:
            existing = self.bug_index.get(key)
            if existing is not None:
                existing["occurrences"] += 1
                existing["case_ids"].append(candidate.id)
            return None
        bug_record = {
            **record,
            "iteration": iteration,
            "root_key": key,
            "signature": sigma,
            "origin": origin,
            "occurrences": 1,
            "case_ids": [candidate.id],
            "repro_sql": reproducible_script(
                Candidate(
                    id=candidate.id,
                    kind=candidate.kind,
                    schema_sqls=minimal.get("schema_sqls", candidate.schema_sqls),
                    inserts=minimal.get("inserts", candidate.inserts),
                    q1=minimal.get("q1", candidate.q1),
                    q2=minimal.get("q2", candidate.q2),
                )
            ),
            **(extra or {}),
        }
        self.seen_bugs.add(key)
        self.seen_signatures.add(key)
        self.bug_index[key] = bug_record
        self.true_bugs.append(bug_record)
        self._append_jsonl(self.results_dir / "bugs.jsonl", bug_record)
        self.history.append(
            {
                "verdict": "true_bug",
                "kind": candidate.kind,
                "category": candidate.category,
                "minimal_case": {"q1": bug_record["repro_sql"][:600]},
            }
        )
        return key

    def ingest_hit(
        self, candidate: Candidate, iteration: Any = "sweep"
    ) -> tuple[str, str | None]:
        """Shared pipeline for any oracle hit: fixer triage -> sigma ->
        dedup -> archive -> DCE expansion. Returns ``(verdict, family_key)``.
        """
        with eff.phase("fixer_triage"):
            triage = self.fixer.execute({"candidate": candidate.to_dict()})
        self.dce_executed += getattr(self.fixer.checker, "executions", 0)
        eff.count("fixer_executions", getattr(self.fixer.checker, "executions", 0))
        verdict = triage.get("verdict", "undetermined")
        if verdict != VERDICT_TRUE_BUG:
            return verdict, None
        minimal = triage.get("minimal_case", {})
        sigma = None
        if self.signature_engine is not None:
            with eff.phase("sigma_diagnose"):
                sigma = self.signature_engine.compute(candidate, minimal)
            self.dce_executed += int(sigma.get("bisect_executions", 0))
            eff.count("bisect_executions", int(sigma.get("bisect_executions", 0)))
        record = {
            "candidate": candidate.to_dict(),
            "verdict": verdict,
            "minimal_case": minimal,
            "analysis": triage.get("analysis", {}),
            "difficulty": triage.get("difficulty", ""),
        }
        key = self._archive_true_bug(
            candidate, minimal, sigma, record, iteration, origin="sweep"
        )
        if key is None:
            return "duplicate", None
        if self.expander is not None and self.dce_executed < self.dce_budget:
            with eff.phase("dce_expand"):
                self._expand_family(
                    candidate, minimal,
                    self._conditioning_sigma(sigma), iteration,
                )
        self._write_families()
        return verdict, key

    def _conditioning_sigma(
        self, sigma: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Return the σ that conditions DCE expansion.

        Normal mode: the bug's own σ. Shuffled-diagnosis arm: a copy
        whose fix_set/plan_diff come from a random previously-seen
        family — expansion then probes the wrong fault surface while
        paying identical diagnosis cost. The archived σ stays honest.
        """
        if sigma is None or not self.shuffle_sigma:
            if sigma is not None:
                self._sigma_pool.append(sigma)
            return sigma
        self._sigma_pool.append(sigma)
        donor = self.rng.choice(self._sigma_pool[:-1]) \
            if len(self._sigma_pool) > 1 else sigma
        return {**sigma,
                "fix_set": list(donor.get("fix_set") or []),
                "plan_diff": list(donor.get("plan_diff") or []),
                "fix_kind": donor.get("fix_kind", sigma.get("fix_kind")),
                "shuffled_from": donor.get("key")}

    def _expand_family(
        self,
        candidate: Candidate,
        minimal: dict[str, Any],
        sigma: dict[str, Any] | None,
        iteration: int,
    ) -> dict[str, Any]:
        """Probe the fault surface around a confirmed family on upstream.

        Every probe is a deterministic SQL/setup mutation screened by the
        sound oracles. Each hit goes back through the normal fixer pipeline
        (minimize -> verdict -> sigma); hits whose sigma equals the seed's
        join that family, hits with a new sigma found a new family and are
        queued for their own expansion (bounded by ``dce_budget``).
        """
        stats: dict[str, Any] = {
            "probes": 0,
            "hits": 0,
            "family_members": 0,
            "new_families": 0,
            "families": [],
        }
        queue: list[tuple[Candidate, dict[str, Any], dict[str, Any] | None]] = [
            (candidate, minimal, sigma)
        ]
        while queue and self.dce_executed < self.dce_budget:
            seed, seed_minimal, seed_sigma = queue.pop(0)
            family_key = (
                seed_sigma["key"]
                if seed_sigma
                else self._bug_key(seed, seed_minimal)
            )
            result = self.expander.expand(seed, seed_minimal)
            stats["probes"] += result.probes_generated
            self.dce_executed += result.probes_executed
            seed_fix_set = (seed_sigma or {}).get("fix_set") or []
            seed_fix_kind = (seed_sigma or {}).get("fix_kind", "")
            for hit in result.hits:
                hit_candidate: Candidate = hit["candidate"]
                stats["hits"] += 1
                triage = self.fixer.execute(
                    {"candidate": hit_candidate.to_dict()}
                )
                self.dce_executed += self.fixer.checker.executions
                if triage.get("verdict") != VERDICT_TRUE_BUG:
                    continue
                hit_minimal = triage.get("minimal_case", {})

                # Fast membership test (1 execution vs ~120 for a full
                # sigma): if disabling the seed family's fix set also
                # resolves this hit, it lies on the same fault surface.
                member = False
                if (
                    self.signature_engine is not None
                    and seed_fix_set
                    and seed_fix_kind in ("single", "minimal_set")
                ):
                    prelude = prelude_for_names(
                        self.signature_engine.engine, seed_fix_set
                    )
                    self.dce_executed += 1
                    member = not self.fixer.checker.reproduces(
                        hit_candidate,
                        hit_minimal.get("schema_sqls", hit_candidate.schema_sqls),
                        hit_minimal.get("inserts", hit_candidate.inserts),
                        hit_minimal.get("q1", hit_candidate.q1),
                        hit_minimal.get("q2", hit_candidate.q2),
                        prelude_sqls=prelude,
                    )

                hit_sigma = None
                if not member and self.signature_engine is not None:
                    hit_sigma = self.signature_engine.compute(
                        hit_candidate, hit_minimal
                    )
                    self.dce_executed += int(
                        hit_sigma.get("bisect_executions", 0)
                    )
                key = (
                    hit_sigma["key"]
                    if hit_sigma
                    else (family_key if member
                          else self._bug_key(hit_candidate, hit_minimal))
                )
                if key == family_key or member or key in self.seen_signatures:
                    # Same fault surface — a member of a known family.
                    stats["family_members"] += 1
                    owner = self.bug_index.get(key, self.bug_index.get(family_key))
                    if owner is not None:
                        owner["occurrences"] += 1
                        owner["case_ids"].append(hit_candidate.id)
                    self._append_jsonl(
                        self.results_dir / "family_members.jsonl",
                        {
                            "id": hit_candidate.id,
                            "family": key,
                            "seed_candidate": seed.id,
                            "probe_op": hit.get("probe_op"),
                            "kind": hit_candidate.kind,
                            "iteration": iteration,
                        },
                    )
                    continue
                # New family found on the fault-surface frontier.
                stats["new_families"] += 1
                stats["families"].append(key)
                bug_record = {
                    "candidate": hit_candidate.to_dict(),
                    "verdict": "true_bug",
                    "minimal_case": hit_minimal,
                    "analysis": triage.get("analysis", {}),
                    "difficulty": triage.get("difficulty", ""),
                    "iteration": iteration,
                    "root_key": key,
                    "signature": hit_sigma,
                    "origin": "dce",
                    "seed_candidate": seed.id,
                    "probe_op": hit.get("probe_op"),
                    "occurrences": 1,
                    "case_ids": [hit_candidate.id],
                    "repro_sql": reproducible_script(
                        Candidate(
                            id=hit_candidate.id,
                            kind=hit_candidate.kind,
                            schema_sqls=hit_minimal.get(
                                "schema_sqls", hit_candidate.schema_sqls
                            ),
                            inserts=hit_minimal.get(
                                "inserts", hit_candidate.inserts
                            ),
                            q1=hit_minimal.get("q1", hit_candidate.q1),
                            q2=hit_minimal.get("q2", hit_candidate.q2),
                        )
                    ),
                }
                self.seen_signatures.add(key)
                self.bug_index[key] = bug_record
                self.true_bugs.append(bug_record)
                self._append_jsonl(self.results_dir / "bugs.jsonl", bug_record)
                LOGGER.info(
                    "dce: new family %s via %s (seed %s)",
                    key,
                    hit.get("probe_op"),
                    seed.id,
                )
                # Frontier expansion: the new family gets its own probes,
                # bounded by the same global budget.
                queue.append((hit_candidate, hit_minimal, hit_sigma))
        return stats

    def _write_families(self) -> None:
        """Dump the current family list (signatures + counts)."""
        self.families = [
            {
                "key": k,
                "signature": rec.get("signature"),
                "occurrences": rec.get("occurrences", 1),
                "case_ids": rec.get("case_ids", []),
                "origin": rec.get("origin", "hunter"),
                "iteration": rec.get("iteration"),
            }
            for k, rec in self.bug_index.items()
        ]
        self._write_json(self.results_dir / "families.json", self.families)

    # --------------------------------------------------------------- output
    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    def _summary(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for bug in self.true_bugs:
            kind = bug["candidate"]["kind"]
            by_kind[kind] = by_kind.get(kind, 0) + 1
            cat = bug["candidate"].get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1
        total_occurrences = sum(b.get("occurrences", 1) for b in self.true_bugs)
        return {
            "iterations": len(self.iteration_logs),
            "total_llm_calls": sum(i["llm_calls"] for i in self.iteration_logs),
            "total_candidates": sum(i["n_candidates"] for i in self.iteration_logs),
            "deduped_root_causes": len(self.true_bugs),
            "raw_true_bug_occurrences": total_occurrences,
            "total_true_bugs": len(self.true_bugs),
            "total_false_positives": sum(
                i["false_positives"] for i in self.iteration_logs
            ),
            "total_version_divergences": len(self.version_divergences),
            "total_cross_engine_divergences": len(self.cross_engine_divergences),
            "total_unverified_bugs": len(self.unverified_bugs),
            "total_families": len(self.bug_index),
            "dce_executed": self.dce_executed,
            "efficiency": eff.snapshot(),
            "ledger": ledger.totals(),
            "root_arms_spawned": self.root_arms_spawned,
            "total_skipped_nondeterministic": sum(
                i["skipped_nondeterministic"] for i in self.iteration_logs
            ),
            "per_iteration": self.iteration_logs,
            "bugs_by_kind": by_kind,
            "bugs_by_category": by_category,
            "pattern_memory": self.hunter.pattern_memory,
            "rewrite_memory": self.hunter.rewrite_memory,
            "component_stats": self.fixer.component_stats,
            "parse_failures": getattr(self.hunter, "parse_failures", 0),
            "hunter_metrics": self.hunter.get_metrics(),
            "fixer_metrics": self.fixer.get_metrics(),
            "playbook": (
                self.playbook.snapshot() if self.playbook is not None else None
            ),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
