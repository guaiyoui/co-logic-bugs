# Research plan: Verifiable Hunter–Repairer Co-evolution for DBMSs

## Two-sentence pitch

DBMS fuzzers explore enormous input spaces, while coding agents repair only the
failures they are shown; neither side systematically learns from the other.
We couple them through a verified bug/patch archive so repair diagnostics shape
future test constraints and newly discovered failures create an adaptive repair
curriculum, without allowing either LLM to fabricate success.

## Precise research question

Under an equal execution and token budget, does verified repair feedback find
more unique, confirmed DBMS bugs and produce more regression-safe patches than
(a) unguided SQL generation, (b) query-plan-only guidance, and (c) two agents
without cross-feedback?

This is a composition of LLM test generation, DBMS test oracles, feedback-guided
exploration, and coding agents. The possible contribution is the *verified
cross-feedback mechanism*, not the fact that two LLMs exchange messages.

## Falsifiable hypotheses

- **H1 — discovery:** repair-conditioned exploration increases confirmed unique
  bugs per 10,000 valid executions over an LLM-only Hunter.
- **H2 — repair:** a curriculum selected by the Hunter archive increases patches
  passing reproducer plus regression tests over an independent coding agent.
- **H3 — mechanism:** gains remain after controlling for query-plan coverage;
  otherwise the method is only a costly approximation of QPG.
- **H4 — safety:** evidence gating keeps false bug reports and false repair
  claims below 5% in a manually adjudicated sample.

Kill/pivot condition: if H1 has no positive effect across three DBMS/version
targets at equal budget, publish the negative interaction study or pivot to
repair-aware testcase prioritization rather than claiming co-evolution.

## System invariants

1. An LLM proposes candidates and hypotheses, never ground-truth verdicts.
2. Every bug has executable setup, query, target commit/version, oracle output,
   repeated reproduction, and a stable fingerprint.
3. Invalid SQL, flaky behavior, expected errors, and duplicates are not bugs.
4. Every successful repair has a real patch, passing reproducer, and passing
   regression suite; textual plausibility is not evidence.
5. Hunter reward depends on confirmed novelty/severity/coverage, not ease of
   repair. Repair feedback only guides the next search neighborhood.
6. All DBMS execution occurs in a killable worker with a timeout.

## Implemented system (generalized framework)

- **Targets:** `DuckDBRunner` (in-process, 1.5.2; old 1.0.0 via venv for
  cross-version differential) and `PostgresRunner` (pgserver embedded PG 16.2)
  behind a common `setup / run / explain_plan` interface.
- **Oracles:** TLP (with predicate-precedence fix and all-partitions-fail
  rejection), NoREC, plan-variant (optimizer on/off — independently rediscovered
  the deliminator bug), LLM equivalence rewrite, cross-version differential,
  cross-engine (DuckDB↔PG), crash/internal-error.
- **Determinism gate:** syntactic + data-driven uniqueness checks; unjudgeable
  tests get `skipped_nondeterministic`, not bug reports.
- **Coverage:** AST feature × physical plan operator × data-boundary cells,
  persisted per run; novelty feeds bandit reward.
- **Bandit scheduling:** UCB1 over feature categories; Fixer verdicts update
  rewards and spawn root-cause arms for local amplification.
- **Seed corpus:** 1,069 historical regression tests (DuckDB `test/issues`,
  PostgreSQL regress) with rule-based mutators — zero-LLM-cost volume.
- **Fixer:** delta-debugging minimization (1-minimal reproducer), root-signature
  deduplication, verdict taxonomy with strict counting.

Current claim boundary: the pipeline is verified end-to-end on both engines.
First full campaigns (150 iterations × 8 queries + 24 seed mutations/iter,
plus an exhaustive 5,152-mutation seed sweep) produced, after deterministic
re-adjudication (`scripts/verify_bugs.py`):

- **DuckDB 1.5.2:** 2 confirmed unique root causes found by sound oracles —
  (a) deliminator `EXISTS` self-join (`=`+`<>`+`>` correlation drops the `>`
  predicate), rediscovered by 5 independent runs; (b) `NOT EXISTS` with
  `=`+`<>` correlation on NULL data returning a row in *both* the `p` and
  `NOT p` TLP partitions (verified manually). Plus dozens of instances of
  each family.
- **PostgreSQL 16.2:** 0 confirmed bugs; 176 cross-engine divergences
  (dialect-level, not bugs); the single LLM-adjudicated equivalence "bug" was
  re-judged a false positive by per-side cross-engine verification.

Co-evolution's causal benefit is still unproven: the feedback-guided DuckDB
run out-produced the random-category control in raw terms (4 vs 1 verified
bugs at equal budget on one seed), but a single seed and a shared root-cause
family do not establish the claim. Multi-seed Gate-2 ablations are required.

## Paper-grade architecture

### Hunter population

Represent each exploration genome as:

- oracle family: optimizer differential, TLP, NoREC, DQE, CAQ/prover;
- SQL feature constraints and forbidden combinations;
- schema/data-state mutators;
- retrieval set from historical bug and repair artifacts;
- exploration temperature and budget allocation.

Use quality-diversity selection rather than a single scalar: maintain niches by
optimizer component, SQL feature family, oracle, and DBMS. Fitness dimensions
are confirmed unique bugs, new normalized plans, valid-query rate, semantic
feature coverage, and execution cost.

### Repairer population

Repair genomes vary retrieved examples, localization strategy, patch budget,
test-generation strategy, and coding-agent model. Fitness dimensions are
reproducer pass, regression pass, sanitizer pass, patch size, performance delta,
and cross-version generalization.

### Co-evolution protocol

1. Select Hunter genomes using archive novelty and uncertainty.
2. Execute candidates with trusted oracle families.
3. Reduce and deduplicate confirmed failures.
4. Give only confirmed artifacts to Repairers.
5. Validate patches in isolated worktrees.
6. Convert repair diagnostics into structured causal hints, such as optimizer
   rule, expression family, precondition, and nearby boundary cases.
7. Mutate Hunter constraints around both repaired and unresolved bugs.
8. Add validated patches as regression tests and hard examples for later
   Repairers.
9. Retain a hall-of-fame archive to prevent forgetting and agent collusion.

## Experimental gates

### Gate 0 — harness correctness

- At least 95% of 1,000 generated candidates execute in both modes.
- Repeated result agreement at least 99.5% on non-volatile queries.
- Correctly reject injected invalid/multi-statement/adversarial outputs.
- Recover from timeout and worker crash without losing the campaign.

Do not run expensive LLM experiments until this gate passes.

### Gate 1 — seeded historical bugs

- Freeze several vulnerable and fixed DuckDB commits.
- Reproduce at least five historical optimizer bugs on vulnerable commits.
- Produce zero reports for those reproducers on their fixed commits.
- Reduction preserves every seeded failure.

If optimizer on/off cannot expose enough seeds, add TLP/NoREC/Argus CAQ oracle
families before changing the learning algorithm.

### Gate 2 — feedback mechanism

Run at least five seeds with equal query, wall-clock, and token budgets:

1. SQLancer/QPG baseline.
2. Argus baseline.
3. LLM Hunter without feedback.
4. Hunter plus query-plan feedback.
5. Hunter plus repair feedback.
6. Full bidirectional system.

Primary metric: unique confirmed bugs per 10,000 valid executions. Report valid
rate, plan/feature coverage, time-to-first-bug, token cost, reduction success,
patch-validation rate, and bootstrap confidence intervals. Deduplicate across all
methods before comparison.

### Gate 3 — external validity

- Frozen train/dev targets for prompt and policy tuning.
- Held-out DBMS versions and at least one held-out DBMS engine for test.
- No retrieval of held-out bug reports, patches, or post-fix source commits.
- Developer confirmation is reported separately from internal reproduction.

## Immediate next implementation milestones

1. Add a SQLancer campaign adapter and ingest normalized query plans.
2. Add TLP/NoREC and Argus CAQ/prover oracle adapters.
3. Add reducer and historical DuckDB vulnerable/fixed commit benchmark.
4. Replace feature counters with a persisted quality-diversity population.
5. Add isolated git-worktree repair execution and sanitizer/performance gates.
6. Freeze the evaluation manifest before comparing methods.

The decisive milestone is Gate 1, not the number of Agent iterations. Until the
system rediscovers known bugs with low false-positive rate, scaling LLM calls or
adding more agents would not strengthen the research claim.
