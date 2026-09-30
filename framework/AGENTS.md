# Project Instructions

## DeepSeek API

- The DeepSeek credential is supplied only through `DEEPSEEK_API_KEY`.
- Interactive and new zsh shells load it from `~/.config/coevo/secrets.env` via `~/.zshenv`.
- For a process that did not inherit the environment, run `source ~/.config/coevo/secrets.env` first.
- Check availability without revealing the value: `[[ -n ${DEEPSEEK_API_KEY:-} ]]`.
- The project configuration must reference `${DEEPSEEK_API_KEY}`; never copy the credential into source code, YAML, prompts, tests, command-line arguments, result archives, or logs.
- Never display the secret file or print the variable. Do not enable shell tracing (`set -x`) while it is loaded. Redact `Authorization` headers and API keys from errors and diagnostics.
- Run the existing DeepSeek-backed campaign from this directory with, for example: `python main.py --engine duckdb --mode full --rounds 3 --population-size 5 --queries-per-test 10`.
- If authentication needs checking, make a minimal request using the environment variable and report only the HTTP status or a boolean result, never response headers or the credential.

## PostgreSQL build harness

- `COEVO_PG_PREFIX=<install-prefix>` redirects every embedded-PG entry point
  (`PostgresRunner`, `pg_coldstart`, `pg_mutate_recall`, `pg_txn_fuzz`,
  `pg_error_scan`, `pg_amcheck`) to a different build — no code changes.
- Assert builds live under `$COEVO_PGBLD` (default `../pgbld/`) as `pg{150,1519,160,166,170,1711,180,186,master}_assert`
  plus `pgmaster_asan` (ASan+UBSan+cassert, built 2026-09).
- `COEVO_PG_EXTRA_OPTS="-c guc=val ..."` appends postmaster `-c` options at
  startup (`pg_local_server.LocalPgServer.start`) — use for SIGHUP-context
  GUCs like `max_pred_locks_per_transaction` that cannot be SET per-session.
- Rebuilding a PG tree needs `bison`/`m4` from the tools env on PATH:
  `export PATH=<workspace>/tools/bin:$PATH` before `make`.
- The ASan build needs `ASAN_OPTIONS=detect_leaks=0` (LSan reports at exit
  are initdb noise). `pgmaster_asan` was configured with:
  `CFLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer -O1 -g"
   --enable-cassert --enable-debug --enable-depend`.

## PostgreSQL oracle gotchas

- `PostgresRunner` connections run with `autocommit=True`: `conn.rollback()`
  and `conn.commit()` are NO-OPs. A failed statement inside an explicit
  `BEGIN..COMMIT` block leaves the PG-side txn INERROR and silently poisons
  the NEXT driver on the shared connection — only an explicit `ROLLBACK`
  SQL statement clears it. `pg_execdiff._rb(conn)` is the helper; every
  driver cleanup path must call it.
- A SET that fails INSIDE a transaction (postmaster-context GUC like
  `io_method`, missing GUC on an old build, or a syntax error like
  `SET work_mem = 1MB` without quotes) aborts the txn; psycopg2 silently
  clears the dead transaction so the NEXT statement autocommits to disk —
  `ROLLBACK` becomes a no-op and dirty state leaks into later probes.
  `pg_coldstart.dml_variant_check` now emits `setup_fail` for this; always
  verify a SET's `pg_settings.context` is not `postmaster` before using it
  inside a txn.
- `debug_parallel_query=regress` (master/18.x) with `update ... returning`
  after a prior autocommitted write showed a row-count quirk — confirmed
  harness-artifact-adjacent; do not report without reproducing at top level.
- `pg_mutate_recall.py --seed-module` accepts `pg_live_probes`,
  `pg18_targets`, or a corpus JSON; per-variant teardown RESETs are required
  or GUC leaks mask crashes. `expected_rows` in a probe gives an absolute
  oracle (fires even when both builds are wrong).
- `pg_txn_fuzz.py --ssi-scenario {tidrange_skew,summarize_skew,all}` replays
  two live upstream SSI write-skew schedules; both hit on every tested
  build (16.6->20devel). summarize_skew needs `--ssi-churn >= ~800`
  (OldCommittedSxact summarization observed at ~800 committed txns even
  with `max_connections=10`; check `pg_locks` for `SIReadLock` with
  vxid `-1/0` to confirm summarization actually happened).
- `scripts/pg_fdw_selfloop.py` diffs a foreign table looped back to the
  same server over postgres_fdw (host=<socket dir>, port=<port>, user
  mapping = current_user); deparse/remote-exec blind spot, clean baseline
  on all builds.
- `scripts/pg_plan_cache.py --param-seeds seeds/pg_param_seeds.py` is the
  only way the generic-vs-custom plan differential is non-vacuous (the
  corpus carries no $n params).
- `scripts/pg_eager_agg.py` is the 20devel-only eager-aggregation
  differential: same-build `enable_eager_aggregate` on/off bag-equality,
  gated on EXPLAIN FORMAT JSON showing a `Partial Mode=Partial` aggregate
  below a Join node (run with `max_parallel_workers_per_gather=0` so a
  'Partial' agg means eager, not parallel-partial). `--secondary-gucs`
  applies extra GUCs to both sides (partitionwise axes covered). Boundary:
  eager refuses groupingSets, DISTINCT/ORDER-BY aggs, non-partial aggs,
  SRFs, volatile args, queries where every baserel feeds an aggregate, and
  nullable-side pushes — those arms are logged `not_fired` by design.
- `scripts/pg_am_diff.py` is the non-btree AM differential (GIN/GiST/
  SP-GiST/BRIN/hash/bloom index-scan bag ≡ seqscan bag; EXPLAIN gate for
  the target AM). `scripts/pg_transform_probes.py` + `seeds/pg_transform_
  probes.py` are the 52-case planner-transform suite (ece nullability
  folds, NOT IN→anti, outer-join reductions, SJE, RTE_RESULT, restart
  loop). `scripts/pg_logical_loop.py` is the 3-tier logical-replication
  oracle (test_decoding journal / pgoutput row-filter binary / real
  2-datadir subscription). `scripts/pg_upgrade_mxact.py` tortures the
  18.6→20devel multixact 32→64-bit rewrite. `scripts/pg_inject_sweep.py`
  sweeps mid-mutation injection points on `pgmaster_inject`.
- `scripts/pg_belief_audit.py` + `oracles/pg_plan_beliefs.py` +
  `seeds/pg_belief_cases.py` are the optimizer-belief auditor: EXPLAIN
  (VERBOSE + FORMAT JSON) extracts asserted planner beliefs (dropped
  quals, oj reduction, Inner Unique, Run Condition, partition pruning,
  Memoize key, reduced Group Key), each audited by a counterexample SQL
  on live data — fires inside a bug's lifetime window even when the
  query result is coincidentally right. `oj_reduced` counts left+right
  joins together (planner may commute); audit SQL must return violation
  ROWS (empty = belief holds). Recall gate: #19412/#19533/SAOP-inlining
  fire only on pre-fix builds; the memoize verbatim arm is a known live
  bug (asserted, wrong result on all builds).
- `scripts/pg_belief_sweep.py --corpus <json>` runs discover_beliefs
  over a whole corpus (auto-generates audits for inner_unique /
  partition_pruned / simple oj_reduced). Gotchas: Inner Unique claims
  uniqueness of the inner SUBPLAN output — skip the base-rel audit when
  a dedup node (Unique/HashAggregate/Aggregate/SetOp) sits under the
  inner subtree; partition audits must use ONE scanned sibling's quals +
  alias (siblings can carry different aliases); oj audits need a NOT
  NULL sentinel col (attnotnull) to spot null-extended rows.
- `scripts/pg_belief_gen.py --prefix <p> --cases N --seed S` is the
  belief-directed generator: 8 families (runcond/ojr/ojr2/lat/inline/
  iu/part/memoize) each carrying a plan-assertion predicate + an
  independent python evaluator (never trust a second SQL query — the
  same bug could optimize it). Verdicts: false_belief / fired_clean /
  not_fired / asserted_no_audit; `wrong_result` flags the absolute-
  oracle compare. Gotchas learned the hard way: eval_window output is
  already in frame order (don't re-permute); LATERAL UNION ALL emits
  u=v once PER JOINED ROW (v repeats per match, not once); `x = ALL`
  is x=e1 AND x=e2 (not `not in`); Memoize cache-key audit must reason
  about PARAM identity — two Params sharing one outer column is the
  BUG #17213-ext precondition, not two columns; memoize SETs need the
  `teardown` list or GUCs leak into the next case's plans.
- `scripts/pg_execdiff.py --build <b> --cases N --seed S` is the
  executor-driver invariance oracle: same query through different
  executor driving modes (direct / work_mem=64kB spill / forced parallel
  / FETCH-1 portal / SCROLL+MOVE positioning / PREPARE generic-custom /
  CTAS DestReceiver / WITH HOLD across commit / savepoint-abort mid-fetch)
  must return identical bags (sequence too when ORDER BY is total).
  Families stress rescan machinery: lateral union, corrsub, reccte, srf,
  agg/sort spill, win, nestloop, multicte, setop, memo(known #17213-ext),
  partjoin, latpart, fdwlat (postgres_fdw loopback async-Append rescan —
  `enable_memoize=off` forced), incsort, bitmapor, jsontbl, epq, merge,
  dmlcte. Gotchas: scroll model needs cur∈{-1..n} boundary states
  (`BACKWARD ALL` lands -1 even when it consumes everything; `FORWARD`
  landing exactly on last row stays on it, overshoot lands n); EPQ
  re-evaluates (a_new, b_orig) PER STORED SLOT — same b tuple by TID,
  multiplicity preserved, failed re-join null-extends; `TM_SelfModified`
  (own-txn update) is treated as deleted in FOR UPDATE scans —
  nodeLockRows.c comment; cross-partition move under FOR UPDATE raises
  SerializationFailure "moved to another partition" — legal, not a bug;
  `FOR UPDATE OF <nullable side>` on outer join errors (use inner join
  when locking both); conn2-side updates need `lock_timeout` and must
  only touch UNFETCHED rows or it deadlocks; merge/dmlcte mutate tables —
  closed-form final-state model, not driver equivalence.
- pgmaster_inject needs the injection_points module installed separately:
  `make -C src/test/modules/injection_points install` (it is a test
  module, not contrib). Injection sessions need `statement_timeout=0`
  and a dedicated connection (never share with control queries).
