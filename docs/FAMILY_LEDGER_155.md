# DuckDB 1.5.5 live family ledger

All families reproduced on the current release (1.5.5). σ key = signature
from `diagnosis/signature.py`. Stability = verify_bugs --repeat / manual.

## Stable deterministic families

### F1 `f4bdb204` — deliminator + filter_pushdown, correlated EXISTS self-join
- repro: `SELECT * FROM t a WHERE EXISTS (SELECT 1 FROM t b WHERE b.x = a.x AND b.y <> a.y AND b.z > a.z)`-class
- evidence: default drops rows vs all-off; 78+ occurrences; longstanding
- upstream: adjacent to #22267 (closed, derived-table scope only) — residual
- **dev-confirmed**: still reproduces on main build `d8cdaa3`
  (2026-09-15; TLP triple-partition count 6 vs expected 5)

### F2 `a7e5c9f7` — window_self_join, duplicate partition key
- repro: `SELECT SUM(v) OVER (PARTITION BY a,b), SUM(v) OVER (PARTITION BY a,a) FROM src`
- evidence: default (10,20) vs all-off (30,30); 1.0.0 correct → 1.5.x regression
- upstream: same rule as #21592 (ROWS frame) but different trigger — likely new
- main `d8cdaa3` (2026-09-15): `PARTITION BY a,a` now dedupes correctly
  (20,20,10) — **appears fixed upstream post-1.5.5**; keep as
  current-release bug on 1.5.5 with the fix note.

### F3 `44b71a9c` — ASOF tie-break is executor-path dependent
- repro:
  `CREATE TABLE ev(ts INT); CREATE TABLE px(ts INT, p INT);
   INSERT INTO ev VALUES (5),(5),(10); INSERT INTO px VALUES (5,10),(5,20),(9,30);
   SELECT px.p FROM ev ASOF JOIN px ON ev.ts >= px.ts ORDER BY ev.ts, px.p`
- default: p=10 (first tie); join_order off / all off / IEJoin path: p=20 (last)
- 1.1.3: p=20 both ways → 1.5.x sort-rewrite regression
- upstream: not found among #9183/#13899/#17046/#19027 (all OR/filter shapes)

### ~~F4 `74beb0e9`+`d9477375`+`14afee1c`+`e24816e4`~~ — REJECTED
- `SET debug_asof_iejoin=true` → `Invalid Type: Infinity requires numeric`
  on ASOF joins over INT keys. Demoted 2026-09-15: this is a *deliberate
  guard rail* — IEJoin needs ±Infinity sentinels which integer domains
  cannot represent; the engine intentionally refuses. Forcing-flag
  errors are not bugs. Oracle now filters via `is_guard_rail_error`
  (oracles/errors.py). Not counted as a family.

### F5 `6ce51ef7` — parallel CTE+OFFSET drops all rows
- repro: `WITH c AS (SELECT flag FROM ordered_probe ORDER BY ord OFFSET 2)
          SELECT * FROM c INNER JOIN boolean_keys ON flag <= flag_key`
- default: `[]` at ~37% rate (eq-join variant ~10–37% across mutants,
  full_verify_155); threads=1: 30/30 correct
- attribution: flaky_parallel (threads=1 resolves); 358 merged occurrences
- class: parallel executor race in ORDER BY..OFFSET pipeline
- collapse caveat: fragment keys {59bb4007, bf9aa3b5, b20eb4dc, and
  fragment a4f609b6} are the same case re-bisected under different
  coincidental triggers (window_separate / prefer_range / no_preserve_order
  / verify_vseq) — all share plan_diff [CTE,CTE_SCAN,PROJECTION]

### F6 `ab630f7b` — non_optimizer NULL/EXISTS (NOT-IN shape)
- default returns 12 rows vs all-off 8 rows; stable, longstanding
- likely same deliminator surface as F1, different entry shape

### F7 `b7178506` — interval equality is not a congruence (CSE fold bug)
- repro:
  `SELECT d + INTERVAL '1 month' AS a, d + INTERVAL '30 days' AS b, a = b
   FROM t`  (d = DATE '2024-01-31')
- default: a = b = 2024-02-29, eq = TRUE — the +30d column carries the
  +1mo VALUE (CSE dedupes `d+i1`,`d+i2` because i1=i2 under normalized
  interval equality, then substitutes one result into both output slots)
- correct (off / no_cse / no_expr_rw): a=2024-02-29, b=2024-03-01, eq=FALSE
- also fires for `d - INTERVAL ...`; month↔day boundary is the non-congruent
  case (30d=1mo normalized, but month-arithmetic clamps to month-end)
- **dev-confirmed**: still reproduces on main `d8cdaa3` (2026-09-15):
  `DATE '2023-02-28' + INTERVAL '30 days'` returns `2023-03-28`
  (should be `2023-03-30`) when both interval forms co-occur —
  the normalized-equality substitution is still live.
- upstream: docs document the normalized equality and explicitly warn that
  addition differs — i.e. substitutability is violated per DuckDB's own
  spec; no matching issue found; STRONG, reportable

## Flaky / nondeterminism families

### F8 `a88e4241` — parallel UNION + COLLATE NOCASE nondeterminism
- `SELECT s COLLATE NOCASE FROM t8a UNION SELECT s COLLATE NOCASE FROM t8b`
- default: 6 different row-sets in 20 runs; threads=1: stable
- same query returns observably different rows run-to-run

## Executor debug-mode families (documented settings)

### F9 `c74e4121`+`f62475b3` — debug_window_mode='combine' breaks COUNT
- `SELECT DISTINCT x, COUNT(*) OVER () FROM t` → combine returns 0 (true: 8)
- also COUNT(*) OVER (PARTITION BY a,v) → 0 under combine; MIN unaffected
- deterministic; debug flag but a real alternative exec path

### F12 — prefer_range_joins → "Unimplemented join type for merge join" on legal SQL
- repro:
  `CREATE TABLE items(iid INT, cat VARCHAR, price INT);
   CREATE TABLE promos(cat VARCHAR, min_price INT); ...
   SELECT i.iid FROM items i WHERE NOT EXISTS
     (SELECT 1 FROM promos p WHERE p.cat = i.cat
      AND i.price BETWEEN p.min_price AND p.min_price + 15)`
- default/no_optimizer: correct (1,5,6); `prefer_range_joins=true`:
  `Not implemented Error: Unimplemented join type for merge join`
- correlated NOT EXISTS + range predicate decorrelates to a mark-join;
  prefer_range then routes it to a merge-range join that cannot handle
  the shape — user-facing setting produces an unplannable query
- found independently by full_exec_155 (issue14398 shape) and
  llm_feat6_155 (items/promos shape) — σ over-merged them into
  the F3 key; re-keyed manually by mechanism

## Crash / internal-error families

### F11 — MERGE `WHEN NOT MATCHED BY SOURCE` under build_side_probe_side → INTERNAL bind error
- repro: `MERGE INTO t USING s ON t.a=s.a WHEN NOT MATCHED BY SOURCE
  THEN DELETE` (or UPDATE) — with NO `WHEN NOT MATCHED THEN INSERT` clause
- default plan: works (1.5.5 fixed the #20991 default-plan shape);
  `SET disabled_optimizers='build_side_probe_side'` →
  `INTERNAL Error: Failed to bind column reference "rowid"`
- deterministic, bisected to `build_side_probe_side` alone; residual
  surface of fixed issue #20991 — the default-path patch did not cover
  the swapped-sides plan (OHR evidence), reportable
- found by dml_hunt after fixing a harness bug that had made earlier
  DML runs vacuous (variant preludes were discarded on reconnect)

### F10 — window frame-bound underflow: negative COUNT / SIGFPE / INTERNAL / garbage
- **count(*) negative on same-direction inverted frames** (release 1.5.5):
  `SELECT count(*) OVER (ORDER BY v ROWS BETWEEN 1 PRECEDING AND
   5 PRECEDING) FROM t` → 0,0,-1,-2,-3. Correct = all 0 (empty frame).
  `debug_window_mode='separate'`/`'combine'` return all-0 → plan-dependent
  wrong result. RESIDUAL of #23589: the parser check only rejects
  cross-direction inversions (FOLLOWING-then-PRECEDING); same-direction
  (1P..5P, 2F..1F) passes the parser and underflows.
- **SIGFPE hard crash (release, core dump)**:
  `ntile(2 ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 2 FOLLOWING AND
   1 FOLLOWING)` — 1-off inversion → division by wrapped frame size;
  16 ROWS-mode crash shapes incl. expression bounds. Residual of #24831.
- **INTERNAL cast error (release)**: `ntile(N ORDER BY c)` /
  `percent_rank(ORDER BY c)` over trailing-empty or deeper-inverted
  frames → `Information loss on integer cast: 18446744073709551615`
- **silent garbage (release)**: `row_number(ORDER BY c)` over inverted
  frames → 0s and negative row numbers (e.g. -2,-2,-1)
- unaffected: plain `ntile(2)` w/o in-func ORDER BY, frame-ignoring
  functions (lead/lag return partition answers correctly), sum/min/first_
  value return correct NULLs — the underflow escapes only through
  count/ordered-arg paths
- release-affected: `ntile(N ORDER BY c)`, `percent_rank(ORDER BY c)`
  over trailing-empty / inverted frames in ROWS, RANGE, GROUPS modes
- debug build additionally asserts (`window_*.cpp`) and INTERNALs on a
  wider set (lead/lag/cume_dist); frame-ignoring funcs return correct
  partition answers in release — the underflowed bound only escapes via
  the ordered-argument evaluation path
- upstream: NTILE future-frame reported as #24831 (fixed by #24933) but
  trailing-empty/reversed frames still crash on 1.5.5 — residual surface
  beyond the landed fix (OHR evidence)
- **fuzz3 (612k combos, release 1.5.5) — strongest minimal shape**: an
  ordinary FOLLOWING-only frame suffices; no inversion needed:
  `SELECT row_number(ORDER BY v) OVER (PARTITION BY ch ORDER BY v,id
   ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING) FROM t1` → all 0s under
  default/separate/combine, all 1s under disable_optimizer. The
  ordered-argument path computes a frame-relative index that goes ≤0
  whenever the current row ∉ frame. Root cause: ordered-arg window
  functions evaluate `pos - frame_start` without guarding frames that
  exclude the current row.
- scale: fuzz3+fuzz4 828k combos → 10,670 hits = **1,836 SIGFPE
  (process-kill, rc=-8)** on ntile-ord + 2,866 INTERNAL cast underflow
  (percent_rank/cume_dist/ntile/lead/lag-ord) + ~2,950 domain-violation
  garbage (row_number ≤0 on normal frames incl. FOLLOWING-only, count<0,
  empty-frame non-NULL). Report: results/frame_fuzz_report.md
- upstream issue split (recommended, per report §6): (1) ordered-arg
  window funcs over frames excluding current row → SIGFPE+INTERNAL+garbage,
  one executor path (`use_framing`/`token_tree`), bypasses the plain-func
  frame-legality parser check — headline: ntile(2 ORDER BY v) SIGFPE DoS;
  (2) plain aggregates over inverted/expr-bound frames → negative count,
  sporadic non-NULL on empty RANGE — different path, lower severity.
  Counted as ONE root-cause family here; TWO reportable issues upstream.

### F13 — ROW/composite `IS NULL` / `IS NOT NULL` uses container semantics
- repro: `SELECT (NULL,NULL) IS NULL` → DuckDB **FALSE** (should be TRUE);
  `SELECT (NULL,NULL) IS NOT NULL` → TRUE; `(NULL,1) IS NOT NULL` → TRUE
- consequence: `WHERE (a,b) IS NOT NULL` keeps mixed-null rows that SQL
  standard + PG 16.2 filter out (all-fields-non-null semantics); and
  `WHERE r IS NULL` never matches an all-NULL row
- PG 16.2 cross-check: all four forms follow the standard
- adjudication (4-way, xengine_4way): **duckdb + sqlite + datafusion all
  use container semantics; only PG is field-wise standard-strict.** The
  majority vote sides with DuckDB — the standard text favors PG but the
  mainstream reading is DuckDB's. Downgraded: spec-reading divergence,
  not a bug claim; keep as a standards-question data point.

## Cross-engine adjudication (results/xengine_full, 1494 cases → 18 divs)

PG 16.2 as independent reference. Verdicts:

- **test_23979 EXISTS×NULL <>** — DuckDB default returns `true`, no_opt
  and PG return `false`. **Known bug #23979** (closed via PR #24235,
  merged 2026-07-29; merge commit is 195 commits ahead of v1.5.5 tag —
  fix not yet released). Same deliminator+filter_pushdown surface as F1;
  `SET delim_join_as_cte` mechanism used by that fix no longer exists.
  → member of F1-class, not a new family.
- **window_dup_part2 ×3** — PG: `PARTITION BY a,a` ≡ `PARTITION BY a`
  (sums 30); DuckDB mis-partitions (10/20). Independent corroboration
  of F2.
- **interval ×3** — PG keeps `INTERVAL '1 month'` ≠ `'30 days'`;
  DuckDB normalizes to equal AND substitutes one for the other in
  `d + INTERVAL` (2023-02-28+30d → 03-28 instead of 03-30).
  Independent corroboration of F7's non-congruence.
- **misc#216 `(a,b) IS NOT NULL`** — mixed-null row `(NULL,1)`:
  PG follows SQL-standard row nullability (both IS NULL and IS NOT NULL
  false → row filtered); DuckDB uses container semantics → row passes.
  → promoted to candidate family **F13** (spec-deviation; see entry).
- **min/max text ×4** — PG locale collation (a, G) vs DuckDB binary
  (A, d). Spec difference.
- **decimal v/3** — rounding-mode difference. Spec.
- **like_escape** — PG default `\` escape vs DuckDB standard-conformant
  no-default-escape. Spec difference (DuckDB is the standard side here).
- **array_agg composite** — serialization shape only. Not a bug.
- **0 new families from cross-engine**; value = independent reference
  confirmation for F2/F7 and one known-issue catch.

## DataFusion 54.0.0 (new engine — first sweep)

Found by 4-engine majority vote (`results/xengine_4way`, 1494 cases):
each confirmed on fresh sessions vs duckdb+postgres+sqlite references.

- **DF-A `EXCEPT ALL` drops multiplicity**: `t={1,1,2,2,2,NULL}`
  `EXCEPT ALL` `u={1,2,NULL}` → DF `[]`, expected `{1,2,2}`
  (duckdb/pg/sqlite all correct). DF evaluates ALL as plain set-op.
  Related: apache/datafusion#12955 documents the same ALL-semantics gap
  for INTERSECT (RHS copies) — still open.
- **DF-B `INTERSECT ALL` = semi-join, not min-multiplicity**:
  `t={1,1,2,2,2,NULL}` ∩ `u={1,2,NULL}` → DF `{1,1,2,2,2}` (all LHS
  copies), expected `{1,2,NULL}`. Same root area as #12955.
- ~~**DF-C `array || NULL` → array**~~ **REJECTED** (review
  2026-09-15): PostgreSQL defines array `||` NULL propagation the same
  way — `ARRAY[1,2] || NULL::int[]` → `{1,2}`, verified on PG 16.2.
  DuckDB's strict NULL propagation is the minority reading, not DF's
  bug. The divergence also sat on a 2-engine quorum edge (C1).
- **DF-D `GENERATED ALWAYS AS` silently NULL**: `b INT GENERATED ALWAYS
  AS (a*2) STORED` → all reads of `b` return NULL, so
  `JOIN h ON g.b = h.b` silently loses rows. Parses without error.
- **DF-E correlated scalar subquery in SELECT → non-executable plan**:
  `SELECT a, (SELECT w FROM u WHERE u.a = t.a) FROM t` →
  `Invalid (non-executable) plan after Analyzer` (WHERE-position
  decorrelation works, SELECT-position does not). Same class:
  `WITH RECURSIVE t(n) AS (...)` ignores the column-name binding —
  `No field named n`, valid fields are `t."Int64(1)"`. Legal SQL
  rejected at the analyzer; limitation-class bugs, not wrong results.
- Cross-check note: `POSITIONAL JOIN` divergence was DuckDB-extension
  syntax silently parsed as a table alias by sqlite/datafusion —
  dialect footgun, not a bug.

## SQLite 3.51.2 (agent: xeng_sqlite, 100 templates + 26 metamorphic)

- **SQLITE-A `SELECT *` over USING-join + RIGHT/FULL JOIN → spurious
  "ambiguous column name"**:
  `SELECT * FROM a JOIN b USING(x) RIGHT JOIN c ON true` →
  `ambiguous column name: x` at prepare time, iff right-side table
  shares the merged column name. LEFT/INNER joins, `count(*)`,
  qualified `a.x`, and `RIGHT JOIN c USING(x)` all work; right table
  with a different column name works. Postgres runs the same query.
  Over-rejection of legal SQL — deterministic, reproduced on 3.51.2.
  (SQLite claims RIGHT/FULL JOIN uses the same handling as Postgres.)
- **DF-F merged USING/NATURAL key not coalesced on RIGHT/FULL rows**
  (4 divergences, rj_using_star/nj_right/nj_full/fj_using_coalesce):
  DataFusion returns NULL instead of the preserved side's key for
  outer-join rows; duckdb/postgres/sqlite all coalesce correctly.

## PostgreSQL 16.2 — index-presence sweep (results/pg_idx3, pg_idx_targets_run)

New oracle `oracles/index_variant.py`: same query under storage-state
variants (btree/hash/partial/expr/covering+VACUUM→index-only scan/BRIN/
GIN/HOT-chain-update), `DROP INDEX` state-restoring teardown,
`enable_seqscan=off` forcing + EXPLAIN applicability gate (a variant
only counts when the plan really routes through an index node).

- Corpus sweep: 680 screened seeds, **1671 applied variants** (3279
  honestly no-op'd by the gate), 41022 sql_executions → 2 divergences,
  both adjudicated **not bugs**: `JSON_OBJECTAGG`/`::text` key order
  shifts with access path — order-dependent aggregate text output on
  unspecified ordering, not wrong data. (Oracle limitation noted:
  text-serialized aggregates are order-sensitive.)
- Index-predicate-dense seeds (`seeds/pg_idx_targets.py`: 3k-row tables,
  range/IN/OR/NULL-heavy/LIKE-prefix/GIN array ops/HOT+IOS):
  16 screened, 84 applied variants → **0 divergences**.
- Verdict: PG's access-path layer is clean on this coverage; earlier
  hit `merge#251` was an oracle false positive (self-UPDATE re-fired
  a trigger) — fixed via DISABLE TRIGGER ALL around the no-op write.
- contrib modules (pg_trgm/intarray/hstore/ltree/cube) unavailable in
  the embedded pgserver build — surface unreachable, documented.

## Rejected / noise

- unseeded TABLESAMPLE / USING SAMPLE → intrinsic nondeterminism, gated
- `LIST(DISTINCT n)` intra-list order → unspecified, gated
- UPDATE..RETURNING replay diffs → oracle non-idempotent, removed
- xengine DECIMAL last-digit / INTERVAL serialization → spec differences

## TiDB pilot (new engine — first sweep, agent: tidb_pilot)

**Instance**: `tiup playground --tag coevo --db 1 --pd 1 --kv 1
--without-monitor` → TiDB **v8.5.8** (self-declares `8.0.11-TiDB`,
i.e. MySQL 8.0 compat) on 127.0.0.1:4000. No sudo needed; tiup mirror
reachable. **Reference**: real **MySQL 9.7.1** (conda-forge
`mysql-server`, user-space datadir, port 3307) — the strongest possible
oracle since TiDB-vs-MySQL divergences on shared syntax are candidate
bugs, not majority votes. sql_mode aligned across both (only diff:
TiDB lists legacy no-op `NO_AUTO_CREATE_USER`).

**Runner**: `targets/tidb_runner.py` — `TiDBRunner(dsn)` +
`MySQLRunner` subclass, mysql-connector-python, drop/recreate
`coevotest` DB per setup, `KILL QUERY` on wall-clock timeout.
**Probe**: `scripts/tidb_probe.py` — 61 semantic cases (TiDB vs MySQL)
+ 8 TiDB-internal plan-variant pairs (tidb_enable_vectorized_expression,
opt_distinct_agg_push_down, opt_insubq_to_join_and_agg,
opt_agg_push_down, enable_index_merge, non_prepared_plan_cache,
opt_projection_push_down, enable_outer_join_reorder). Loose-bag compare;
raw report `results/tidb_probe_report.jsonl`.

**Result**: 59/61 semantic + 8/8 plan-variant agreed. Two confirmed
deterministic divergences (>=5 fresh-schema runs each):

- **TIDB-A — duration/TIME overflow unchecked in TiDB-layer expr eval
  (bug; known upstream pingcap/tidb#56865, still reproduces v8.5.8)**:
  `TIME '838:59:59' + INTERVAL 1 HOUR` → TiDB `'839:59:59'` (past
  documented max) vs MySQL `NULL`+warn 6527. Sharper: with
  `tt(col1 TIME)={'838:59:59'}`,
  `SELECT col1 + INTERVAL 10 HOUR FROM tt
   WHERE (col1 + INTERVAL 10 HOUR) IS NULL` → TiDB returns
  `('848:59:59')` — the same expression is NULL in the TiKV-pushed
  WHERE yet non-NULL out-of-range in the TiDB-layer projection.
  MySQL: `NULL` consistently in both positions. (The ADDTIME() shape
  is *not* pushed to TiKV — evaluated in TiDB both ways — so it shows
  only the out-of-range half.) Also `ADDTIME('838:59:59','1:00:00')`
  → TiDB '839:59:59' vs MySQL clamps to '838:59:59'.
- **TIDB-B — decimal division carries extra internal precision
  (candidate bug / compat divergence, low severity)**: under equal
  `@@div_precision_increment=4`,
  `SELECT 1.0/3.0*3.0` → TiDB `1.000000` vs MySQL `0.999990`
  (`1.00/3.00*3.00` → `1.00000000` vs `0.99999900`). Displayed
  `1.0/3.0` is `0.33333` on both, but TiDB's intermediate quotient
  keeps more digits, so `(a/b)*b == a` where MySQL's spec mandates
  truncation to scale+4 first. Related upstream: #21485/#51501
  (div_precision_increment plumbing); numerically *more* accurate but
  diverges from declared MySQL semantics.

Pairwise-only/spec notes: identical error classes on shared failures
(unsigned wraparound/subtraction, int overflow, ABS(minint),
POW(10,400), multi-row scalar subq, COUNT(DISTINCT NULL) rejection);
TiDB's utf8mb4_bin default vs MySQL ai_ci did NOT surface because the
probes' string literals inherit each side's default collation
consistently ('a'='A' agreed: both 0 under _bin defaults in effect).

## DataFusion 54.0.0 (upgrade + round 3)

**Upgrade**: `pip install -U datafusion` → **already at 54.0.0**, which is
the latest released version on PyPI/crates.io (datafusion-python 54.0.0,
published 2026-06-29; PyPI LATEST confirms). Old = 54.0.0, new = 54.0.0 —
no version bump available. Round-3 reverification therefore ran on the
same build; runner env = `python3` → miniconda3 `py310` env (same env
`scripts/cross_engine.py` uses).

**Reverify** (fresh `SessionContext` per case; refs duckdb 1.5.2 +
postgres 16.2, sqlite 3.51.2 where applicable):

| family | repro | DF 54.0.0 | refs | status |
|--------|-------|-----------|------|--------|
| DF-A | `{1,1,2,2,2,NULL} EXCEPT ALL {1,2,NULL}` | `[]` | `{1,2,2}` (pg+duckdb) | **still-present, confirmed on 54.0.0** |
| DF-B | `… INTERSECT ALL …` | `{1,1,2,2,2}` (LHS mult) | `{1,2,NULL}` (pg+duckdb) | **still-present, confirmed on 54.0.0** |
| DF-D | `b INT GENERATED ALWAYS AS (a*2) STORED` | `b` reads NULL | pg: `2,4,6` (duckdb rejects STORED cols — n/a) | **still-present, confirmed on 54.0.0** |
| DF-E | `(SELECT w FROM u WHERE u.a=t.a)` in SELECT | non-executable plan | pg+duckdb OK | **still-present, confirmed on 54.0.0** |
| DF-E | `WITH RECURSIVE t(n) AS …` | `No field named n` | pg+duckdb+sqlite OK | **still-present, confirmed on 54.0.0** |
| DF-F | `a RIGHT JOIN b USING(x)` | merged key NULL on outer rows | pg+duckdb+sqlite coalesce | **still-present, confirmed on 54.0.0** |

No family was fixed by an upgrade (none available). DF-E scope widened:
the column-alias list `t(n)` is dropped **only** on the RECURSIVE path —
`WITH t(n) AS (…)` (non-recursive) binds correctly, and
`WITH RECURSIVE t AS (SELECT 1 AS n …)` works. 12+ alias-list repros now
verified. Additionally confirmed unplannable on 54.0.0 (same
decorrelation/analyzer class): correlated scalar/EXISTS/IN in
**ORDER BY** (`In/Exist/SetComparison subquery can only be used…`),
`EXISTS` in SELECT list (`Physical plan does not support logical
expression Exists`), scalar subquery w/ LIMIT in SELECT, `IN (SELECT …)`
in JOIN ON, and non-canonical recursive shapes (EXCEPT-step,
left-branch recursive term) falling back to `table 'public.t' not found`
instead of a proper recursion error.

**New families**

- **DF-G `CAST(<high-precision numeric literal> AS DECIMAL)` loses
  precision through f64** (wrong-result, confirmed). By default
  (`datafusion.sql_parser.parse_float_as_decimal=false`) a decimal
  literal with >17 significant digits (or integer literals > i64) is
  bound as Float64 before the cast:
  `SELECT CAST(123456789012345678901234567890.123 AS DECIMAL(38,3))`
  → DF `Decimal('123456789012345686040493921665.024')`; pg + duckdb
  (raw psycopg2/duckdb fetch, avoiding runner float normalization)
  return `Decimal('123456789012345678901234567890.123')` exactly.
  Repros: 33-digit, 17-digit, scale-36, >i64-integer literals — all
  corrupted; `CAST('…string…' AS DECIMAL(38,3))` is exact (string path
  unaffected). Non-default escape hatch exists:
  `SET datafusion.sql_parser.parse_float_as_decimal = true` restores
  exactness. Silent corruption on an *explicit* DECIMAL cast = confirmed
  wrong-result.
- **DF-H zero-column scan of a recursive CTE → error/panic**
  (internal-error + crash, confirmed). When the outer query needs no
  CTE column, DF prunes the working table to an empty schema and the
  recursive exec breaks:
  `WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM t
  WHERE n < 5) SELECT count(*) FROM t`
  → `Arrow error: Schema error: project index 0 out of bounds,
  max field 0` (deterministic, 5/5). UNION (distinct) variant → Rust
  panic `index out of bounds: the len is 0 but the index is 0` at
  `datafusion-physical-plan-54.0.0/src/aggregates/group_values/
  multi_group_by/mod.rs:450`, surfaced as `Join Error` (5/5).
  pg+duckdb return `5`. Control `SELECT count(n) FROM t` works.
  Same repro from `SELECT 42 FROM t LIMIT 3`, count(*) via subquery,
  and recursive CTE joined to a constant — zero-column scans only;
  non-recursive CTEs under count(*) are fine.
- **DF-I `expr <op> ALL (SELECT …)` uncorrelated, compared column not
  projected → ProjectionPushdown internal assertion** (internal-error,
  confirmed). `SELECT a FROM t WHERE b > ALL (SELECT w FROM u)` →
  `ProjectionPushdown … Assertion failed: col.name() == matching_name:
  Input field name w does not match`. All six ops {>,>=,<,<=,=,<>}
  reproduce while `a` alone is projected; adding `b` to the SELECT list
  (or correlating the subquery) dodges it. ANY/SOME unaffected.
  Name-matched variant `b > ALL (SELECT b FROM u)` instead surfaces
  `Exists … not implemented` — same ALL-decorrelation root area.
  pg+duckdb return `{1,2,3,NULL}`.
- **DF-J lateral UNNEST in FROM unplannable** (unplannable class,
  confirmed). `SELECT t.id, e.c FROM t, UNNEST(t.l) AS e(c)` →
  `Physical plan does not support logical expression
  OuterReferenceColumn(Field { name: "l" …})`; the bare-alias form
  `UNNEST(t.l) AS e` binds no usable column (`No field named e —
  valid: e."UNNEST(outer_ref(t.l))"`); `UNNEST(…) WITH ORDINALITY`
  → `not supported yet`; `CROSS JOIN LATERAL` → `table function
  'unnest' not found`. pg + duckdb execute all forms
  (`{1,10},{1,20},{2,30}`). Only the projection form
  `SELECT id, unnest(l) FROM t` works in DF.

**Round-3 observations (not filed as families)**

- `SELECT a, a` / `SELECT 1, 1` / duplicate output names →
  "Projections require unique expression names" — longstanding,
  documented DF design restriction (apache/datafusion#6543; asserted
  as expected behavior in sql_integration.rs). Spec-deviation; work-
  around via explicit aliases.
- Recursive CTE with type-changing step (`SELECT 1 AS x UNION ALL
  SELECT x + 0.5 …`) never terminates in DF (>25 s) where
  sqlite/duckdb finish; the query is type-illegal per spec (pg rejects
  it) — DF neither rejects nor terminates. Aggregate-in-recursive-term
  similarly hangs instead of erroring. Robustness gap on illegal SQL.
- Recursive reference inside a subquery → `Internal error: Unexpected
  empty work table` (self-described bug message) rather than a clean
  rejection; spec-illegal anyway (pg/sqlite reject).
- `INT UNSIGNED` accepted in DDL (`CREATE TABLE u(v INT UNSIGNED)`
  works) but `CAST(x AS UNSIGNED)` → `Unsupported SQL type UNSIGNED` —
  inconsistent dialect edge.
- Executor-config sweep clean: 500 combos × 20 queries — toggling
  `target_partitions {1,2,4}`, `batch_size {1,2,16,8175}`,
  `coalesce_batches`, `repartition_{joins,aggregations,windows,sorts}`,
  `enable_round_robin_repartition`, `prefer_hash_join`,
  `enable_sort_pushdown`, `prefer_existing_sort`, `filter_null_join_keys`,
  `hash_join_single_partition_threshold{,_rows}`,
  `enforce_batch_size_in_joins`, `join_reordering`,
  `top_down_join_key_reordering`, `collect_statistics`,
  `enable_{,join_}dynamic_filter_pushdown` — **0 bag diffs**.
- `approx_median`/`approx_distinct`/`approx_percentile_cont` all within
  expected error (several returned exact answers); timestamp unit
  coercions and ns-precision literals compare correctly vs pg.
- NOT IN + NULL three-valued logic correct (NULL-poisoned RHS → empty
  result, matching pg/duckdb/sqlite).
- decimal arithmetic (`/`, `*`, sum, avg) values agree with pg/duckdb —
  only display formatting (trailing zeros, interval text) differs.

## SQLite 3.51.2 round 2 (agent: sqlite_r2)

Tools: `seeds/sqlite_round2.py` (717 generated seeds),
`scripts/sqlite_plan_sweep.py`, `scripts/sqlite_plan_fuzz.py`,
`scripts/sqlite_rj_flip.py`, `scripts/sqlite_jsonb_pairs.py`,
`scripts/sqlite_unistr_hunt.py`. Cross-engine check
(`results/xeng_sqlite_r2`, duckdb+postgres+sqlite): 496 comparable
cases, 40 divergences — all adjudicated as documented spec
differences or unspecified ordering (see near-misses).

### SQLITE-B — USING/NATURAL ambiguity check is direction- and operand-asymmetric
- repro (3 tables, `a(x,y)`, `b(x,z)`, `c(x,w)` — operand `(a J b)` has
  two columns named `x`):
  `SELECT count(*) FROM a RIGHT JOIN b ON a.x=b.x JOIN c USING(x)` →
  `ambiguous reference to x in USING()`
  while the same ambiguous operand is silently accepted when it is
  (a) on the RIGHT: `c JOIN (b RIGHT JOIN a ON a.x=b.x) USING(x)` → 1;
  (b) a subquery: `(SELECT a.x, b.x FROM a JOIN b ON a.x=b.x)
   RIGHT JOIN c USING(x)` → 2; or
  (c) reached through an INNER/LEFT-only chain:
  `a JOIN b ON a.x=b.x JOIN c USING(x)` → 1.
- accepted forms silently bind the FIRST column named `x`.
- boundary (verified): the strict check fires only on the LEFT operand
  of a USING/NATURAL join, only when that operand is a flattened join
  tree AND some RIGHT/FULL join appears in the chain (any of
  prior-RIGHT/FULL or current-RIGHT/FULL: INNER+RIGHT, INNER+FULL,
  RIGHT+INNER, FULL+INNER, FULL+LEFT, LEFT+RIGHT, LEFT+FULL all err;
  INNER+INNER, INNER+LEFT accepted). `NATURAL` inherits the same
  asymmetry (`NATURAL RIGHT JOIN` errs, mirror `NATURAL JOIN` accepts).
- cross-engine: PostgreSQL 16.2 rejects every form
  (`common column name "x" appears more than once in left/right table`);
  DuckDB 1.5.x rejects every form (`Ambiguous reference to column
  name "x"`). SQL spec requires rejection on duplicate common names.
- verdict: two-sided defect — the ambiguity diagnostic (a byproduct of
  the RIGHT/FULL name-resolution machinery added in 3.39) is enforced
  only on one code path; everywhere else ambiguous USING silently
  picks the first match. Deterministic 10/10 fresh connections.
- dedup vs SQLITE-A: same feature area (RIGHT/FULL name resolution)
  but distinct trigger (USING-clause lookup vs `SELECT *` expansion of
  a merged column), distinct error (`ambiguous reference to x in
  USING()` vs `ambiguous column name: x`), and inverse direction of
  wrongness (A over-rejects legal SQL; B under-rejects illegal SQL).

### SQLITE-C — unistr() does not combine surrogate pairs and emits
###            malformed UTF-8 for invalid scalar values
- repro: `SELECT hex(unistr('\ud83d\ude00'))` → `EDA0BDEDB880`
  (CESU-8: two raw 3-byte surrogate encodings). Expected UTF-8 for
  U+1F600 is `F09F9880` — which SQLite itself produces for the same
  character via `char(128512)` and via the 8-digit form
  `unistr('\U0001F600')`. So `unistr('\ud83d\ude00') =
  unistr('\U0001F600')` → 0 (should be 1).
- internal oracle (engine-independent): SQLite's own UTF-8 decoder
  treats the emitted bytes as invalid — `unicode(unistr('\ud83d\ude00'))`
  → 65533 (U+FFFD), `length(...)` → 2; `unistr('\U00110000')` →
  `F4908080` while `char(1114112)` sanitizes the same out-of-range
  input to `EFBFBD`. Lone surrogates `\ud83d`/`\udc00` encode raw
  (`EDA0BD`/`EDB080`) instead of erroring.
- cross-engine: PostgreSQL `unistr(E'\ud83d\ude00')` → `😀` (4 bytes);
  PG rejects lone surrogates and out-of-range scalars. SQLite docs say
  unistr "is intended to work the same as in PostgreSQL, SQL Server,
  and Oracle" — surrogate-pair combination is the feature's purpose.
- deterministic 10/10 fresh connections; `scripts/sqlite_unistr_hunt.py`
  reproduces all 10 deviating cases. One root cause: the escape parser
  validates only the escape syntax, never scalar-value semantics —
  no surrogate-pairing, no range/scalar check. (`\u0000` → `00` matches
  `char(0)` → `00`, so the NUL case is consistent-with-char and is
  excluded from the repro set.)

### Near-misses adjudicated (rejected, documented/unspecified)
- Compound-SELECT precedence: `a UNION b INTERSECT c` groups
  left-to-right `((a UNION b) INTERSECT c)` where PG applies
  INTERSECT-binds-tighter — explicitly documented SQLite behavior.
- LIKE: `v LIKE 'a%'` matching `A`/`APPLE` is the documented
  case-insensitive-for-ASCII default (`case_sensitive_like` off);
  `BETWEEN 'a' AND 'c' COLLATE NOCASE` binds COLLATE to the upper
  bound only (per-comparison collation — defensible desugaring);
  `GLOB ... COLLATE NOCASE` stays case-sensitive per docs.
- `ORDER BY x LIMIT` NULL placement: SQLite NULLS-FIRST vs PG NULLS
  LAST default — spec difference.
- Plan-invariance sweep (`sqlite_plan_sweep.py`, 4000-case fuzz,
  504-pair pushdown oracle): 23 apparent diffs all reduced to
  unspecified row order (no ORDER BY / `reverse_unordered_selects`)
  or arbitrary representative choice among NOCASE-equal ties in
  `min()`/`max()`. NOT IN + NULL 3VL correct under index variants;
  INDEXED BY / NOT INDEXED clean.
- JSONB pairs (`sqlite_jsonb_pairs.py`): `jsonb_extract` returns BLOB
  for containers where `json_extract` returns TEXT — documented
  representation difference; `json_valid(jsonb(...))`=0 per docs;
  `jsonb_quote` does not exist (invalid pair assumption, removed).
- DML/attach probes: `WITH x AS (INSERT...)` unsupported (PG-only
  feature); `old.`/`new.` in RETURNING correctly rejected; AFTER-
  trigger effects not visible to scalar subqueries inside RETURNING
  (per-row ordering, self-consistent); partial-index UPSERT conflict
  targets work incl. `WHERE`+`RETURNING`; cross-database FK
  references are an unsupported restriction (clean error).
- FTS5: `offsets()` not compiled into this build (clean
  `no such function`, not a logic bug); `fts5vocab` requires
  `CREATE VIRTUAL TABLE` (module form); empty/NULL/zeroblob docs,
  rank/bm25/snippet/highlight, `integrity-check` after DELETE+UPDATE
  all clean.
- `IN`/`EXISTS`/scalar-subquery/recursive-CTE/VALUES/`json_each`-
  lateral positions containing RIGHT/FULL joins all behave correctly.

## Version-ladder recall (2026-09-15, canonical repro per family)

| fam | 1.0.0 | 1.1.3 | 1.5.5 | main d8cdaa3 | reading |
|---|---|---|---|---|---|
| F1 | BUG | BUG | BUG | BUG | longstanding; dev-confirmed on main |
| F2 | n/a* | n/a* | BUG | fixed | recent 1.5.x (*rule name absent pre-1.5) |
| F3 | absent | absent | BUG | — | recent regression 1.1.3→1.5.5 |
| F5 | absent (40/40) | flaky 5% | flaky 17.5% | — | flaky row-drop since ~1.1.3, worse in 1.5.5 |
| F7 | BUG | BUG | BUG | BUG | longstanding; dev-confirmed on main |
| F8 | unstable rep. | unstable rep. | BUG (unstable NOCASE rep.) | — | longstanding instability; dedup semantics changed across versions |
| F9 | BUG | BUG | BUG | — | debug_window_mode='combine' broken on every version (debug-setting tier) |
| F10 | — | — | SIGFPE | fixed (minimal) | current-release crash, upstream-patched shape |
| F6 | — | — | — | — | non-optimizer family; default/off ladder not applicable |

F5 repeat evidence: 40 fresh-connection runs per version →
1.0.0: 40/40 correct; 1.1.3: 38/40 + 2 empty; 1.5.5: 33/40 + 7 empty.
