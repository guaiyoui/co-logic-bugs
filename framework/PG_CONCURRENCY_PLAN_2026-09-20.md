# PG concurrency/protocol/replication bug-hunt plan — 2026-09-20

Ranked, defended plan for NEW bugs on surfaces the harness has not touched.
Feasibility probes run 2026-09-20 against pgmaster_inject (20devel):

| Probe | Result |
|---|---|
| libpq pipeline via ctypes | WORKS. `PQenterPipelineMode`/`PQsendQueryParams`/`PQsendPipelineSync` on `pgmaster_inject/lib/libpq.so.5.20`. `[SELECT 1, bad, SELECT 2] + sync` → TUPLES_OK '1', FATAL, `PGRES_PIPELINE_ABORTED`, `PGRES_PIPELINE_SYNC`; post-sync query runs. NOTE: `PQsendQuery` is REFUSED in pipeline mode — everything funnels through extended protocol. |
| `wal_level=logical` via COEVO_PG_EXTRA_OPTS | WORKS (`-c wal_level=logical -c max_wal_senders=4 -c max_replication_slots=4`). |
| test_decoding via `pg_logical_slot_get_changes` | WORKS, no CREATE EXTENSION needed. BEGIN/change/COMMIT text stream; rolled-back txn emits nothing. |
| pgoutput via `pg_logical_slot_get_binary_changes` | WORKS incl. row filters: `WHERE (v>15)` pub → stream `B R I C | B I C | B D C` for insert-match / insert-nonmatch (empty txn suppressed) / update→match / update→nonmatch. First byte = message kind; binary parseable. REPLICA IDENTITY FULL required for non-key filter columns; column lists conflict with RI FULL (use second table). |
| Real `CREATE SUBSCRIPTION` between two local datadirs | WORKS over unix socket: `CONNECTION 'host=<sockdir> port=<port> dbname=postgres'`; initial sync + live catchup verified (3 rows, sum 60). |
| psycopg2 walsender | `psycopg2.extras.LogicalReplicationConnection` + `ReplicationCursor` present — true START_REPLICATION streaming reachable without a real subscriber. |
| injection_points on pgmaster_inject | attach/run/wakeup/detach/set_local; actions `wait`/`error`/`notice`; ~90 named points incl. `exec-insert-before-insert-speculative`, `heap_lock_updated_tuple`, `check-exclusion-or-unique-constraint-conflict`/`no-conflict`, `transaction-end-process-inval`, `invalidate-catalog-snapshot-end`, `idle-in-transaction-session-timeout`, `transaction-timeout`, `idle-session-timeout`, `deadlock-timeout-fired`, `restartpoint-before-slot-invalidation`, `slot-timeout-inval`, `logical-decoding-activation`, `subscription-refresh-before-origin-check`, `ri-before-pk-lock`, `define-index-before-set-valid`, `nbtree-leave-*-incomplete`. `pg_repack_probe.py` already has `wait_for_injection_point`/`wakeup_retry`/`RepackThread` machinery. |
| test_decoding/pgoutput on plain assert builds | present in pg1711/pg180/pg186/pgmaster_assert too (plugins ship in lib/postgresql); only injection_points is inject-only. |

## Ranked top-8

### 1. Logical replication self-loop differential (3 tiers)
Densest documented upstream bug vein the harness has never touched (row-filter
fixes in every release 15→18; sync-race and apply fixes ditto).
- **1a. test_decoding journal oracle** (new file `scripts/pg_logdec_journal.py`).
  Drive a journaled workload (reuse `pg_txn_fuzz` op pool: inserts/updates/
  deletes/truncate/savepoints/subxacts/2PC-prep, autocommit + txns). Decode
  `pg_logical_slot_get_changes('s','test_decoding','include-xids','1',...)`.
  ORACLE: decoded change multiset per committed txn == journaled committed
  writes; aborted txns emit zero changes; `peek` == `get` re-decode. Replicas:
  run same journal under REPLICA IDENTITY DEFAULT/INDEX/FULL/NOTHING — old-tuple
  fields must match RI semantics exactly. ~0.5d. Builds: all (inject + one stable).
  Yield: M-H (decode correctness bugs; toast/chunked values; catalog-txn edges).
- **1b. pgoutput row-filter / column-list oracle** (`scripts/pg_pgoutput_filter.py`).
  Matrix over (op ∈ insert/update/delete) × (row matches filter before, after).
  Parse `pg_logical_slot_get_binary_changes` by first-byte kind (B/R/I/U/D/C)
  — and a minimal tuple decoder for I/U/D payloads. ORACLE: emitted kind ==
  filter-transition table (nonmatch→match ⇒ I; match→nonmatch ⇒ D; etc.),
  payload cols == column list, RI fields present/absent per spec. Axes:
  multiple pubs (union semantics — row sent if ANY pub matches), FOR ALL
  TABLES vs FOR TABLE, `pub_via_partition_root`, partitioned tables,
  generated columns (`publish_generated_columns` 18+), column list ∪
  REPLICA IDENTITY INDEX. ~1d. Yield: HIGH — this is the exact code with the
  densest fix history; union-semantics and RI-NOTHING edges are thinnest upstream.
- **1c. Real subscription apply, two datadirs** (`scripts/pg_subapply_diff.py`).
  Two PostgresRunners (distinct datadirs → distinct ports), unix-socket conninfo
  (proven). Run txn-fuzz journal on publisher; barrier; compare final tables +
  per-table checksums. Variants: writes DURING initial sync (sync-race: rows
  inserted while COPY runs must appear exactly once), `streaming=parallel`,
  `two_phase=on` + PREPARE on publisher, `disable_on_error`, conflicting writes
  on subscriber (apply error → clean SQLSTATE, no crash), TRUNCATE replication,
  partitioned publisher → plain subscriber table. ~1d. Yield: HIGH on
  sync-race/apply-error paths; apply-worker asserts are invisible to client —
  oracle must include publisher+subscriber `log_fatal_lines` scans.
- Needs: new code throughout; pg_txn_fuzz journal format reusable as op source.

### 2. Three-session SSI: read-only anomaly, long fork, DEFERRABLE
Cheapest path to a headline artifact. `pg_txn_fuzz` already supports
`--sessions 3` + `gen_ssi_trial` monkeypatch (pg_ssi_sibling pattern).
Schedule (dangerous structure T1 -rw→ T2 -rw→ T3, T3 commits before T1's
snapshot — the documented SSI hole for RO txns):
  T2: BEGIN; R(A).  T3: BEGIN; W(A); W(B); COMMIT.  T1 RO: BEGIN; R(A); R(B);
  COMMIT — snapshot post-T3 ⇒ sees B=T3 but A=pre-T2.  T2: W(A); COMMIT.
ORACLE: T1's observed (A,B) must equal some serial-order replay of committed
T2,T3 writes; no witness + no abort ⇒ anomaly. RO-T1 hit = documented
limitation repro (still a real serializability violation artifact); same
schedule with RW-T1 that ALSO slips = genuine new bug. Also test: SERIALIZABLE
READ ONLY DEFERRABLE T1 — must block to a safe snapshot; does deferrable
actually prevent this anomaly? (upstream docs claim yes; never verified
interactively.) Plus pivot-order permutations (T3 aborts; T2 aborts; T1
reads-only-B). ~0.5-1d. Builds: all (SSI unchanged since 9.1 — run master +
pg160 for age contrast). Yield: guaranteed artifact + real-hole chance.

### 3. Injection-point error/wait sweep on txn/storage paths
pgmaster_inject only. Two drivers on existing RepackProbe machinery:
- **3a. 'error' sweep**: for each named point, run the minimal SQL that reaches
  it with `injection_points_attach(p,'error')` local; ORACLE = session gets
  clean ERROR (not crash), server log free of TRAP/"resource wasn't
  released"/leak warnings, catalog invariants hold after, next txn clean.
  Highest-value points (mid-mutation, cleanup-assumes-no-elog):
  `transaction-end-process-inval`, `invalidate-catalog-snapshot-end`,
  `exec-insert-before-insert-speculative`, `heap_update-before-pin`,
  `check-exclusion-or-unique-constraint-*`, `ri-before-pk-lock`,
  `define-index-before-set-valid`, nbtree-leave-*-incomplete + subsequent
  scan/insert correctness, vacuum-truncate-*.
- **3b. 'wait' + cancel/terminate**: attach wait; backend parks; then
  pg_cancel_backend / pg_terminate_backend / client close mid-wait. ORACLE:
  no leaked locks (pg_locks empty), no stale speculative tokens, waiter slot
  cleanup (the wait_cleanup.spec bug class — leaked wait slots break later
  wakeups), no PANIC.
- **3c. deterministic timeouts**: 'error' at `idle-in-transaction-session-timeout`,
  `transaction-timeout`, `idle-session-timeout`, `deadlock-timeout-fired`
  (fires inside ProcSleep → error inside lock-wait path). ORACLE: correct
  FATAL/SQLSTATE class, session vs txn termination semantics, clean
  post-timeout state.
~1d for driver table + sweep. Yield: HIGH for assert/leak findings — this is
the classic assert-finder and the harness has never error-injected.

### 4. EPQ × partition row movement — made deterministic by injection
The known bug vein (cross-partition UPDATE under concurrent lock/update; past
fixes for lost rows / wrong RETURNING) is normally probabilistic; on
pgmaster_inject `heap_lock_updated_tuple` (wait) parks T2 inside
heap_lock_tuple mid-chain while T1 does the partition-moving UPDATE →
deterministic EPQ-on-moved-tuple. Scenarios: T1 UPDATE moves row across
partitions vs T2 {FOR UPDATE, FOR NO KEY UPDATE, UPDATE, DELETE, MERGE};
T2's own write also crosses partitions; RETURNING old.*/new.* fidelity;
SERIALIZABLE and RC. ORACLE: final partitioned state == serial replay; row
conserved (count over all partitions); RETURNING cols match committed values;
no "unexpected chunk" errors. Fallback for non-inject builds: tight rendezvous
+ warm buffer tricks (less deterministic, still useful on pg1519–186 for
backpatch-diff hunting). ~1d. Yield: M-H.

### 5. Speculative insertion lifecycle
Deterministic via `exec-insert-before-insert-speculative` (fires between
SpeculativeInsertionLockAcquire and heap insert) + `check-exclusion-or-
unique-constraint-{conflict,no-conflict}` (post-probe windows — the same
window upstream's on_conflict_probe_window.spec uses; we extend axes upstream
didn't): partial unique index arbiters (predicate boundary rows entering/
leaving the partial index concurrently), multi-index arbiter inference
ambiguity, ON CONFLICT DO UPDATE vs plain UPDATE same key, DO UPDATE vs
DO UPDATE (loser must re-derive from committed tuple), cancel/terminate
while blocked on a speculative token (WaitsForSpeculativeInsertion cleanup),
savepoint rollback after spec-wait, ON CONFLICT on partitioned table with
cross-partition conflict. ORACLE: unique invariant in final state
(`SELECT k,count(*) … HAVING count(*)>1` must be empty), serial-replay
membership, no hung speculative-insertion locks in pg_locks, RETURNING sane.
~1d on inject; scripted-race variant on others. Yield: M-H (token lifecycle
under cancel is thin upstream).

### 6. Pipeline-mode differential
Confirmed working. New `scripts/pg_pipeline_diff.py` extending the LibPQ
ctypes class (add PQenterPipelineMode/PQsendQueryParams/PQsendPipelineSync/
PQexitPipelineMode — note PQsendQuery is refused in pipeline mode; everything
is extended protocol). ORACLE = pipeline-vs-sequential equivalence: same op
stream on two databases; compare per-statement outcome class + rows + final
state. Key edges upstream's libpq_pipeline tests cover thinly:
- txn commands inside pipeline: `[BEGIN; bad; COMMIT; sync]` — COMMIT is
  skipped-to-sync, leaving backend in failed txn; post-sync behavior must
  match sequential `[BEGIN; bad; COMMIT]` (=rollback) semantics.
- implicit-txn boundaries: statements between syncs each auto-commit; error
  in statement N must not undo committed N-1 (differential vs autocommit seq).
- DDL mid-pipe: CREATE TABLE then INSERT into it before sync; SET mid-pipe;
  NOTIFY/LISTEN mid-pipe; savepoints; PREPARE/EXECUTE via PQsendPrepare in
  pipe; binary params; double-sync / empty sections; huge pipe (buffer
  exhaustion → PQflush mid-queue correctness).
- pipeline + concurrent second session doing DML on same rows (lock wait
  inside a pipelined statement vs sync boundaries — blocked-statement vs
  skipped-queue interplay).
~1d. Yield: M — server-side pipeline loop (postgres.c) + implicit-txn
semantics are real but moderately exercised; strongest expected finds are
txn-state-machine edges.

### 7. Replication-slot invalidation races
PG17+ `idle_replication_slot_timeout` is new code; invalidation-while-active
is thin. On inject: `slot-timeout-inval` (slot.c), `restartpoint-before-
slot-invalidation`, `logical-decoding-activation`. Scenarios: invalidate slot
while a session has it active mid-decode; `idle_replication_slot_timeout` low
+ decode paused mid-txn; `pg_drop_replication_slot` racing
`pg_logical_slot_get_changes`; `pg_replication_slot_advance` vs active reader;
two_phase slot + prepared txn + invalidate. ORACLE: invalidated slot ⇒
documented SQLSTATE + `invalidation_reason` set correctly; never crash;
`pg_replication_slots` consistent; decoding session dies cleanly.
~0.5d, folds naturally into suite-1 datadir. Builds: pg1711+/inject.
Yield: M.

### 8. LISTEN/NOTIFY ordering + txn-boundary lock release
Cheap psycopg2 oracle (`conn.notifies` after `select`+`poll`): delivered
notification multiset/order == commit order of committed txns; aborted txns
deliver zero; NOTIFY vs pg_notify() equivalence; self-delivery; LISTEN inside
aborted txn must not subscribe; payload round-trip (UTF8/binary edges); queue
overflow path (large payload flood → documented error, not corruption).
Same suite checks txn-scope primitives as commit-boundary markers:
pg_advisory_xact_lock released exactly at commit (second session try-lock),
pg_locks state across savepoint rollback. ~0.5d. Yield: M-L — async.c
ordering had real bugs historically; cheap enough to justify as a tail suite.

## Kill list (do NOT build)
- `debug_parallel_query=regress` DML differential — AGENTS.md already flags a
  row-count quirk as harness-artifact-adjacent; triage cost per hit too high.
- COPY protocol error-path fuzz — well-trodden by sqlsmith-class fuzzers.
- 2PC / crash recovery — exhausted (37+ rounds clean); only revisit as
  publisher-side `two_phase` variant inside 1c.
- AIO/io-worker injection points — no crisp oracle; skip.
- Physical standby surfaces (hot-standby feedback, recovery conflicts,
  failover slots) — needs pg_basebackup/archive infra we don't have; future
  infra investment, not this round.
- Standalone MERGE-shape fuzz — already in op pool; MERGE only earns its
  place inside #4's EPQ scenarios.
- Standalone advisory-lock suite — folded into #8.
- postgres_fdw/RLS/trigger/deferred — explicitly exhausted.

## Ordering rationale
#1 first: untouched surface + strongest historical bug density + all three
tiers proven feasible today (decode, filtered binary, real apply). #2 next:
half-day cost for a guaranteed documented-anomaly artifact plus a genuine
open question (RW-T1 variant, DEFERRABLE correctness). #3: mechanical sweep
on existing machinery with the assert-finder profile; cheap per-point once
the driver table exists. #4/#5 pair: injection points convert historically
flaky race bugs into deterministic schedules — do them together since both
hang off `heap_lock_updated_tuple`-class waits. #6 has clean infra but
upstream coverage is not empty — middle. #7/#8 are tail suites sharing
suite-1/fuzzer infra.
