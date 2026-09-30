# PG planner-transformation bug hunt — recon plan (2026-09-20)

Goal: find NEW semantic bugs in planner/optimizer rewrites (transformations that must
preserve semantics). Reconnaissance was done against `pg186_assert` and
`pgmaster_assert`; sources diffed between `pgbld/src/postgresql-18.6` and
`pgbld/src/master_extract/postgresql-20devel`.

## Recon outcome (what was tried and what was learned)

~140 live probes across the fresh machinery produced **zero semantic diffs and zero
crashes** on baseline shapes. Baseline shapes are covered by upstream regression
(`eager_aggregate.sql` etc.). Yield is therefore in *combinatorial interaction arms* —
the same class that produced #19560/#19626/#19649/#19653.

Diff sizes 18.6 → master (planner-relevant, lines changed):
`analyzejoins.c` 2018, `clauses.c` 1453, `prepjointree.c` 949, `createplan.c` 740,
`initsplan.c` 713, `plancat.c` 550, `subselect.c` 370, `planmain.c` 111.

New machinery found only in master:

- **Eager aggregation** (`enable_eager_aggregate` GUC, default **on**; GUC `min_eager_agg_group_size`).
  Partial HashAggregate pushed below joins (`initsplan.c:setup_eager_aggregation`,
  `relnode.c:create_rel_agg_info` incl. the `IS_OTHER_REL` arm that runs
  `adjust_appendrel_attrs_multilevel` on `RelAggInfo` — the same translation
  machinery family as #19626).
- **NOT IN → anti join** (`subselect.c:convert_ANY_sublink_to_join` `under_not` arm;
  `sublink_testexpr_is_not_nullable`; `query_outputs_are_not_nullable`;
  `op_is_safe_index_member`; `find_safe_quals`; `EstimateSubplanHashTableSpace` +
  `unknownEqFalse` hashed-subplan executor support).
- **Outer-join strength reduction to ANTI** (`prepjointree.c:reduce_outer_joins_pass2`,
  `forced_null_var_is_nonnullable`, `remove_redundant_nullability_quals`;
  LEFT/FULL → ANTI/RIGHT_ANTI, plus RIGHT_ANTI→swap+ANTI).
- **eval_const_expressions nullability folds** (`clauses.c`): DistinctExpr→`NOT(a=b)`/
  `a=b` when both args provably nonnull (lines ~3074-3255); `x IS DISTINCT FROM NULL`
  →`x IS NOT NULL`; COALESCE/MinMax arg filtering by nonnullability (~3806-3870);
  NullTest/BooleanTest → Const when arg nonnull (~4036-4210); `count(x)→count(*)`
  via new `simplify_aggref`/`SupportRequestSimplifyAggref` (`int8.c` prosupport).
- **`restriction_is_always_true`/`restriction_is_always_false`** (initsplan.c) used by
  `add_base_clause_to_rel`/`add_join_clause_to_rels` (joininfo.c) to drop or
  FALSE-replace quals; clone clauses excluded via `has_clone/is_clone`.
- **SJE rewrite** (`analyzejoins.c:remove_self_joins_one_group`/`remove_self_join_rel`):
  qual hoisting (`fixup_selfjoin_jointree`, `orphan_quals`), `ChangeVarNodes` over
  `parse`/`processed_tlist`/`append_rel_list`, `match_unique_clauses`/`uclauses`,
  rowmark transfer.
- **`remove_useless_result_rtes` LEFT/SEMI arms** (prepjointree.c:4044+) with
  `substitute_phv_relids` retargeting PHVs and `remove_nulling_relids` on
  `dropped_outer_joins`.
- **Nullability provenance** (`plancat.c:get_relation_notnullatts` → global
  `rel_notnullatts_hash` keyed by relation OID; `var_is_nonnullable` with
  NOTNULL_SOURCE_{RELOPT,HASHTABLE,CATALOG}; `attnullability==VALID` only).

Same-build oracles (USERSET toggles; results must be identical with feature on/off):
`enable_eager_aggregate`, `enable_self_join_elimination`. These are **feature toggles
under test**, not plan-differential hacks — the oracle is bag-equality of results.

Verified dead ends (do not re-propose):
- FDW system-column nullability: `ctid IS NULL` → 0 rows on both postgres_fdw and
  file_fdw (they synthesize non-NULL values); `varattno<0 → true` is fine in practice.
- SJE on inheritance parents: refused (no elimination; correct results).
- `ALTER TABLE .. ADD CONSTRAINT .. NOT NULL .. NOT VALID`: pre-existing NULLs are
  correctly NOT folded (INVALID attnullability respected by all consumers tested).
- ATTACH PARTITION requires child NOT NULL — no bypass.
- Inheritance children inherit parent NOT NULL — no bypass.
- Simple OJ-reduction / NOT-IN / SJE / min-max / winrun / RTE_RESULT / IS DISTINCT
  arms — all clean on both builds.

## Ranked top-8 proposals

### 1. Eager aggregation × appendrels + multi-rel/join-rel grouping  (highest priority)
- **Target**: `create_rel_agg_info` IS_OTHER_REL arm (`adjust_appendrel_attrs_multilevel`
  on `RelAggInfo`), `init_grouping_targets` for join rels (agg args spanning multiple
  rels), `eager_aggregation_possible_for_relation` (nullable-side gating, PHV
  blocking, BTEQUALIMAGE group-expr check), partial-agg targets carrying HAVING vars.
- **Oracle**: same-build `SET enable_eager_aggregate=off` + expected_rows. Use
  int/numeric aggregates only (float8 sum/avg may legally reorder → FP). Assert firing
  by capturing an EXPLAIN marker (`Partial HashAggregate` below a join) in pre_sqls.
- **Why now**: brand-new default-on feature; the OTHER_REL/appendrel arm is literally
  the same adjust_appendrel_attrs family that produced #19626. Partitioned parents +
  partition-key group exprs + FILTER/HAVING are untested upstream with NULLs.
- **Size**: ~200 LOC, new `seeds/pg_eager_agg.py`; maybe ~30 LOC runner tweak to
  support a per-case `SET ... ; query ; RESET` paired form.
- **FP risk**: medium — eager firing is cost-based; mitigate with large tables +
  `min_eager_agg_group_size=0`, and verify the partial path fired. Float aggs must
  use exact-type or tolerant compare.

### 2. ece-level nullability folds (DistinctExpr→op, COALESCE arg-drop, NullTest/BooleanTest→const, count(x)→count(*))
- **Target**: `clauses.c` eval_const_expressions arms using `expr_is_nonnullable` with
  NOTNULL_SOURCE_HASHTABLE; `simplify_aggref` → `int8inc_support`.
- **Oracle**: expected_rows + paired reformulations (`x IS NOT DISTINCT FROM 5` vs
  `x = 5`; `coalesce(nn,x)` vs `nn` for provably-nonnull nn). Key edges: vars under
  OJ (varnullingrels — must NOT fold), NOT VALID NOT NULL (must NOT fold — verified
  clean but belongs in matrix), partition-parent vs inheritance-parent divergence,
  whole-row args (varattno==0 → never proven), sysattrs (always nonnull), CASE/
  COALESCE/MinMax/RelabelType nesting, join-alias vars post-flatten_join_alias_vars,
  `count(x)` where x is nullable-side var, `count(x) FILTER(...)`, folded clauses
  inside index predicates/appendrel-translated child quals.
- **Why now**: these folds run inside EVERY qual/tlist — widest blast radius of the
  new machinery; relies on the new HASHTABLE whose population timing
  (`preprocess_relation_rtes`) precedes all ece calls.
- **Size**: ~160 LOC `seeds/pg_ece_nn.py`. **FP risk**: low.

### 3. NOT IN → anti-join conversion matrix
- **Target**: `subselect.c:convert_ANY_sublink_to_join(under_not)`,
  `sublink_testexpr_is_not_nullable` (OpExpr / AND / OR / RowCompareExpr forms),
  `query_outputs_are_not_nullable` (safe_quals through the subquery's own joins,
  `flatten_group_exprs` on hasGroupRTE, `flatten_join_alias_vars`),
  hashed NOT IN executor (`unknownEqFalse`).
- **Oracle**: exact expected_rows under 3VL, plus `NOT EXISTS` equivalence ONLY where
  both sides proven nonnull. Edge arms: multi-col row-IN with partial NULLs;
  correlated NOT IN; subquery containing LEFT JOIN (nullable output), SEMI inside
  subquery's jointree (find_safe_quals descends rarg — vars unreachable above);
  set-op outputs (punted); VALUES; views; `SELECT count(*)`/`max()` scalar outputs;
  NOT IN nested under a pulled-up subquery; NOT IN inside OJ ON qual
  (available_rels).
- **Why now**: NOT IN is the single most NULL-sensitive construct; the conversion is
  master-only and the guard is a multi-arm proof with several subtle pieces.
- **Size**: ~140 LOC `seeds/pg_notin_anti.py`. **FP risk**: low (strict 3VL
  discipline; never equate NOT IN with NOT EXISTS when NULLs possible).

### 4. reduce_outer_joins pass2 reduction matrix incl. ANTI
- **Target**: `prepjointree.c` pass2 state machine; `forced_null_var_is_nonnullable`
  (safe quals + extra_join_quals + attnotnull); `remove_redundant_nullability_quals`.
- **Oracle**: paired formulations (original outer join + IS NULL vs hand-written
  ANTI/INNER) + expected_rows. Arms: LEFT→ANTI via attnotnull vs via own ON-qual
  strictness (`s.m = t.a` proving `s.m IS NULL` reduces); FULL→ANTI vs FULL→LEFT/
  RIGHT partial reductions; RIGHT_ANTI normalization (swap); nested OJ trees where
  upper quals prove lower forced-null vars; whole-row `IS NULL`; `IS UNKNOWN` on bool
  NOT NULL; OR-blocked reduction controls; pushed-down quals (RINFO_IS_PUSHED_DOWN)
  on nullable sides; reduction interacting with RTE_RESULT children.
- **Why now**: LEFT/FULL→ANTI is master-only; the two-pass state propagation over
  nested jointrees is intricate.
- **Size**: ~150 LOC. **FP risk**: low.

### 5. SJE residual arms (orthogonal to #19626)
- **Target**: `remove_self_joins_one_group`, `fixup_selfjoin_jointree` qual hoisting
  when a FromExpr/JoinExpr empties (orphan quals only valid at inner joins — test
  emptied subtrees adjacent to OJ), `match_unique_clauses`/`uclauses` (base quals
  matching across sides: `a.v=10 AND b.v=10` vs `a.v=10 AND b.v=20`), clone quals
  under OJ identity-3 commutation, multi-copy chains (a,b,c pairwise), rowmark
  strength combos (FOR UPDATE/NO KEY UPDATE/SHARE/KEY SHARE asymmetric + prti),
  SJE under EXISTS under OJ, SJE where same rel is also referenced via a DIFFERENT
  alias inside a lateral subquery.
- **Oracle**: `enable_self_join_elimination` toggle + expected_rows.
- **Why now**: the whole module was rewritten for master (2018-line diff); #19626 was
  an appendrel sibling — qual-hoist and uclauses arms remain unmined.
- **Size**: ~140 LOC. **FP risk**: low-medium (locking-level diffs are real bugs but
  subtle — compare row results AND `ctid`-level lock behavior is out of scope; keep
  to value-level oracles).

### 6. remove_useless_result_rtes LEFT/SEMI arms + substitute_phv_relids
- **Target**: `prepjointree.c:4372-4440` (LEFT arm drops qual'd join when no
  dependent PHVs), `find_dependent_phvs(_in_jointree)` (phrels∩baserels == {varno}
  exact-match), `substitute_phv_relids` (retarget to larg's relid set incl. join
  relids), `remove_nulling_relids` on dropped OJ relids, `fix_append_rel_relids`.
- **Oracle**: expected_rows. Arms: RTE_RESULT under LEFT join with PHV referenced
  ONLY in upper ON qual (not tlist); PHV whose phrels partially overlap the
  RTE_RESULT; lateral-dependent PHVs; nested OJ stripping; single-row subquery
  `(SELECT expr)` (not just VALUES) under LEFT/SEMI; two chained RTE_RESULT drops;
  RTE_RESULT inside FromExpr-with-quals pushed up through commutable LEFT joins.
- **Why now**: the LEFT-with-quals arm is new; PHV retargeting touches
  ph_eval_at/phnullingrels bookkeeping that #19560-family bugs live in.
- **Size**: ~130 LOC. **FP risk**: low.

### 7. planmain restart-loop / cross-pass staleness
- **Target**: `planmain.c:285-301` restart loop (remove_useless_outer_joins →
  reduce_unique_semijoins → remove_useless_self_joins, each `goto restart`);
  `root->join_domains` truncation; RestrictInfo/clone rebuild; append_rel_list after
  ChangeVarNodes; `last_rinfo_serial` monotonicity across rebuilds.
- **Oracle**: expected_rows. Arms: single queries chaining ≥2 removals (OJ-removal
  making an inner subtree eligible for SJE; semi→inner then SJE on the formerly-semi
  rel; SJE after subquery pullup produced RTE_RESULT then result-RTE removal);
  attrs referenced only via EC-implied quals.
- **Why now**: every stale-derived-data bug in the recent batch (#19560/#19626)
  lives in exactly this loop; the passes were heavily reworked.
- **Size**: ~110 LOC. **FP risk**: low.

### 8. restriction_is_always_{true,false} + joininfo FALSE-replacement under clones/appendrels
- **Target**: `initsplan.c:restriction_is_always_true/false` (has_clone/is_clone
  guard, OR arm, argisrow rejection), `joininfo.c:add_join_clause_to_rels` (drop
  always-true, replace always-false with FALSE — including on OJ quals),
  `apply_child_basequals`/`get_relation_notnullatts(childrel)` path (inherit.c:510)
  for per-child attnotnull divergence.
- **Oracle**: expected_rows. Arms: `IS NULL`/`IS NOT NULL` on vars under OJ
  commutation clones; OR of mixed null-tests; whole-row/sysattr args; child quals
  translated through appendrels where children differ in attnotnull (traditional
  inheritance — parent skipped via rte->inh, children get own notnullattnums);
  pushed-down quals on nullable sides; NOT VALID constraints.
- **Why now**: new functions in master; cheap to express.
- **Size**: ~90 LOC. **FP risk**: low.

## Rejected candidates (with reasons)

- **FDW system-column nullability** — empirically dead (FDWs synthesize ctid).
- **SJE on inheritance parents** — verified refused; unique index on parent doesn't
  cover children and the planner knows it.
- **preprocess_minmax_aggregates / winrun / extract_restriction_or_clauses /
  OR→SAOP** — mature code, ≤4-line diffs, probes clean.
- **SupportRequestInlineFunction inlining** — mechanism exists but zero in-core
  implementations; dead code for stock builds.
- **grouping_conflict_walker CaseTestExpr** — affects error-detection only.
- **Simple-shape differential probing** of any of the above machinery — exhausted by
  this recon (~140 queries, zero diffs). All future probes must target interaction
  arms: appendrel translation, PHVs at removed rels, clone clauses, grouping RTEs,
  NOT VALID constraints, collation determinism.

## Harness notes

- New probes go in `seeds/pg_<name>.py` exposing `PG_LIVE_PROBES`; run via
  `scripts/pg_probe_run.py --module seeds.<name> --prefixes .../pg186_assert,.../pgmaster_assert`.
- For toggle oracles (proposals 1 and 5), either use per-case pre/post SET/RESET in
  `pre_sqls`+a paired second case, or add a small `oracle_mode: "guc_toggle"` field to
  the runner (~30 LOC in `scripts/pg_probe_run.py`) that runs the query once with the
  GUC set and once reset and requires identical bags.
- Verify eager paths actually fired (EXPLAIN marker in a companion case) — otherwise
  the oracle is vacuous.
- `/tmp` filled during recon (`postmaster.opts`: No space left) — clean runner
  datadirs between batches.
