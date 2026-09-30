# Window-frame crash fuzz — DuckDB 1.5.5 (release) + debug

Fuzz campaign: `ntile/percent_rank/lead/lag/cume_dist/row_number/...` over
empty / inverted / trailing-empty window frames. Extends family **F10**.

- **Release build**: pip `duckdb==1.5.5` (`venv_duckdb_155/bin/python`)
- **Debug build**: `/home/user/work/db_safety/duckdb_src/build/debug/duckdb`
  (UBSan-enabled)
- **Sweep**: 34 funcs × {ROWS,RANGE,GROUPS} × 10×10 bounds × 4 order-specs
  × 5 EXCLUDE × 3 PARTITION = **612,000 combos**
- Table: `t1(id,ch,v)` 8 rows, `id` has one NULL, `ch`∈{A,B,C},
  `v`∈{10,20,30,40} with ties.
- Harness: `/tmp/release_frame_fuzz3.py` (full sweep, crash-resilient
  supervisor) + `/tmp/release_frame_fuzz4.py` (funcs 22–33 confirm pass).
  Satellites `row_number()`/`count(*)` in each query give true row position /
  partition size for a per-row empty-frame oracle. Hits:
  `/tmp/release_frame_hits.json` (7660), `/tmp/release_frame_hits4.json` (3010).

## Headline result

| kind | count | meaning |
|---|---|---|
| CRASH (process killed, exit −8 = **SIGFPE**) | **1836** | division-by-zero, uncatchable |
| EXC (catchable `INTERNAL Error: Information loss on integer cast`) | 2866 | UINT64 wrap of −1…−7 |
| GARBAGE (silent wrong values) | 2958 | negative counts, 0/−ve row_number, non-NULL on empty frames |

A single root cause — **window frame bounds underflow when the current row
is not inside its own frame (or the frame is empty)** — but it escapes
through **three distinct release-visible symptoms** and two different code
sites, so it should be split for upstream (see §6).

## 1. Root cause

`src/function/window/window_rownumber_function.cpp` (~line 160-195),
`WindowNtileExecutor` — and the analogous ordered-argument executors. When
an in-function `ORDER BY` (`ntile(2 ORDER BY v)`) makes the aggregate use
`use_framing`/`token_tree`, the "partition" bounds become the **frame**
bounds (`FRAME_BEGIN`/`FRAME_END`). For frames that are empty or exclude the
current row:

- `n_total = partition_end[i] − partition_begin[i]` is `0` when the frame is
  legitimately empty → `n_param = 0` → `n_total / n_param` = **0/0 → SIGFPE**
  (release) / UBSan `runtime error: division by zero` at line 176 (debug).
- The row-position index `row_idx − partition_begin` (or the
  `token_tree->Rank()` result) goes **negative** when the frame sits
  entirely after/before the current row → `NumericCast<int64_t>` of the
  wrapped `idx_t` → `INTERNAL Error: Information loss on integer cast:
  value 1844674407370955161x` (x = −1…−7 wrap).
- In the non-crashing cases the same underflowed bound is used to *read*
  rows → returns wrong values silently (0s, −1/−2, shifted values,
  all-1 ranks).

**Validation bypass.** Plain window funcs get `Parser Error: frame starting
from following row cannot have preceding rows` on inverted frames; the
ordered-argument forms skip that legality check and reach the executor.

**Sort-path determines crash primitive.** With a secondary sort
(window `ORDER BY` ≠ in-function `ORDER BY`, e.g. `oc='id'` or `'v DESC'`),
the `token_tree` path engages and the INTERNAL cast error fires even where
the same frame under `oc='v'` only returns garbage. For `ntile` it flips
INTERNAL→SIGFPE.

## 2. Release-visible shape table

| # | function set | frame condition | release symptom | debug symptom |
|---|---|---|---|---|
| A | `ntile(2 ORDER BY v)` | frame can't contain cur row (both bounds PRECEDING, both FOLLOWING, expr-bound, inverted) — 96 distinct (mode,b1,b2) | **SIGFPE** (1836) | UBSan div-by-zero @rownumber_fn:176 |
| B | `percent_rank(ORDER BY v)` | same 96 bound-pairs, all modes | INTERNAL (2104) | INTERNAL (same) |
| C | `ntile(2 ORDER BY v)` | a disjoint set of 44 bound-pairs (frame-after-cur / trailing-empty under `oc='v'`) | INTERNAL (268) | INTERNAL + D_ASSERT |
| D | `cume_dist(ORDER BY v)` | inverted + expr-bound | INTERNAL (234) | INTERNAL |
| E | `lead/lag(v ORDER BY v)` | inverted + expr-bound, **only** when `oc≠v` (secondary sort) | INTERNAL (260) | INTERNAL |
| F | `row_number(ORDER BY v)` | any frame not containing cur row | silent garbage: `0`, `-2`, `-1`, wrong position (1002) | INTERNAL/assert (same family) |
| G | `count(*)` (+ `count(v ORDER BY v)`) | inverted + expr-bound | **negative counts** e.g. `0,0,-1,-2,-3` (250) | same garbage (no assert) |
| H | `rank(ORDER BY v)`, `cume_dist(ORDER BY v)`, `lead/lag(v ORDER BY v)` | any frame ≠ partition | in-domain wrong values (all-1s, 0/1 mixes) — silent (part of 2958) | garbage |
| I | plain `sum/min/max/avg/first_value/last_value/nth_value/list/string_agg` | inverted **RANGE** only | sporadic non-NULL on always-empty frame (e.g. `min(v)`→30 on last row) (~200) | garbage |
| J | plain `lead(v)`,`lag(v)`; frame-ignoring `rank()/dense_rank()/row_number()/ntile(2)/cume_dist()/percent_rank()`; `nth_value(v,2 ORDER BY v)`; `arg_min/arg_max` | all | **clean — zero hits** (correct partition/NULL answers) | — |

### Distinct shape counts (release)
- SIGFPE shapes: **1** (A ntile-ord) — but the most severe; 1836 combos,
  96 distinct (mode,b1,b2)
- INTERNAL shapes: **5** (B percent_rank-ord, C ntile-ord-cast,
  D cume_dist-ord, Ea lead-ord, Eb lag-ord) — 2866 combos; one shared
  message prefix (`Information loss on integer cast`, wraps −1…−7)
- silent-garbage shapes: **4** (F row_number-ord 0/−ve/wrong-pos,
  G negative `count(*)`, H in-domain frame-relative values,
  I empty-frame non-NULL plain aggs) — 2958 combos
- debug-only shapes: **~0 combos are release-clean** — every recorded
  debug-hit combo produces *some* release symptom (INTERNAL, SIGFPE, or
  garbage). What is debug-only is the *symptom class*: `D_ASSERT` failures
  (`window_*.cpp`, e.g. `result_ntile` bounds) and the UBSan
  `division by zero` at `window_rownumber_function.cpp:176` (the debug
  display of shape A — compiled out / SIGFPE in release). Debug fuzz1/2
  recorded 93+110 INTERNAL+assert hits on the same funcs.

## 3. Minimal reproducers (all confirmed 3× on fresh connections)

Setup for all:
```sql
CREATE TABLE t1 (id INTEGER, ch CHAR(1), v INT);
INSERT INTO t1 VALUES (1,'A',10),(2,'B',20),(NULL,'B',30),(3,'A',40),
                      (4,'C',10),(5,'C',20),(6,'A',30),(7,'B',40);
```

```sql
-- A. SIGFPE (process dies, rc=-8) — ntile ordered-arg, leading-empty frame
SELECT ntile(2 ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) FROM t1;
SELECT ntile(2 ORDER BY v) OVER (ORDER BY v RANGE BETWEEN 1 FOLLOWING AND 1 FOLLOWING) FROM t1;  -- RANGE trailing-empty also SIGFPE
SELECT ntile(2 ORDER BY v) OVER (ORDER BY v RANGE BETWEEN 3 FOLLOWING AND 1 FOLLOWING) FROM t1;  -- RANGE inverted SIGFPE
SELECT ntile(2 ORDER BY v) OVER (ORDER BY v GROUPS BETWEEN 1 PRECEDING AND 1 PRECEDING) FROM t1;

-- B. INTERNAL — percent_rank ordered-arg, any frame not containing cur row
SELECT percent_rank(ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING) FROM t1;
SELECT percent_rank(ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) FROM t1;

-- C. INTERNAL — ntile ordered-arg, trailing-empty
SELECT ntile(2 ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING) FROM t1;

-- D. INTERNAL — cume_dist ordered-arg, inverted
SELECT cume_dist(ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 3 FOLLOWING AND 1 FOLLOWING) FROM t1;

-- E. INTERNAL — lead/lag ordered-arg, needs window-order ≠ in-func-order
SELECT lead(v ORDER BY v) OVER (ORDER BY id   ROWS BETWEEN 3 FOLLOWING AND 1 FOLLOWING) FROM t1;
SELECT lag (v ORDER BY v) OVER (ORDER BY v DESC ROWS BETWEEN 1 PRECEDING AND 5 PRECEDING) FROM t1;
SELECT lead(v ORDER BY v) OVER (ORDER BY id   ROWS BETWEEN v FOLLOWING AND 1 FOLLOWING) FROM t1;

-- F. silent garbage — row_number ordered-arg
SELECT row_number(ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 3 FOLLOWING AND 1 FOLLOWING) FROM t1;
--   -> [(-2,),(-2,),(-2,),(-2,),(-2,),(-2,),(-1,),(0,)]
SELECT row_number(ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING) FROM t1;
--   -> all (0,)

-- G. silent garbage — plain count(*), inverted / expr-bound frame
SELECT count(*) OVER (ORDER BY v ROWS BETWEEN 1 PRECEDING AND 5 PRECEDING) FROM t1;
--   -> [(0,),(0,),(-1,),(-2,),(-3,),(-3,),(-3,),(-3,)]
SELECT count(*) OVER (ORDER BY v ROWS BETWEEN v FOLLOWING AND 1 FOLLOWING) FROM t1;
--   -> [(-6,),(-5,),(-4,),(-3,),(-2,),(-1,),(0,),(0,)]

-- H. in-domain-but-wrong ordered-arg
SELECT rank(ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING) FROM t1;  -- all (1,)
SELECT cume_dist(ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING) FROM t1; -- (1.0,0.0,1.0,0.0,...)
SELECT lead(v ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) FROM t1; -- (10,10,20,20,30,30,40,40)

-- I. plain aggregate, sporadic non-NULL over always-empty inverted RANGE frame
SELECT min(v) OVER (ORDER BY id RANGE BETWEEN 1 PRECEDING AND 5 PRECEDING) FROM t1;
--   -> [(None,)x7,(30,)]  (last row leaks a value)
```

## 4. Dimension findings

- **EXCLUDE**: `EXCLUDE CURRENT ROW/TIES/GROUP` are `Parser Error: EXCLUDE is
  not supported` for every crashing/garbage func — **no hit requires an
  EXCLUDE clause**; `EXCLUDE NO OTHERS` (no-op) behaves like absent. So
  EXCLUDE is not a trigger dimension for this family.
- **Expression bounds** (`v PRECEDING`/`v FOLLOWING`): DO trigger — INTERNAL
  for percent_rank/cume_dist/lead/lag/ntile-ord, negative `count(*)`, and
  SIGFPE for ntile-ord. `v FOLLOWING AND k FOLLOWING` and
  `k PRECEDING AND v PRECEDING` are the main offenders — any bound that can
  place the frame outside the current row.
- **PARTITION BY**: not required — all shapes fire unpartitioned; they also
  fire under `PARTITION BY ch` and `PARTITION BY id` (incl. NULL partition).
  Purely frame-shape-driven.
- **ORDER-BY spec**: for lead/lag-ord INTERNAL the window `ORDER BY` must
  differ from the in-function `ORDER BY` (secondary-sort/token_tree path);
  for ntile it flips INTERNAL→SIGFPE.

## 5. Other root causes?

Census of non-hit exceptions across the sweep: only expected
`Parser Error` (frame-legality, EXCLUDE-unsupported, ORDER BY-in-func for
`dense_rank`) and `Binder Error` (RANGE multi-key offset, ROWS expr bound).
**No second root-cause family** in this sweep — every observed anomaly is
the frame-bound underflow family. Note `dense_rank(ORDER BY v)` is a parser
error (ORDER BY unsupported) — different surface, not a bug.

## 6. Recommended upstream issue split

Two issues (one shared root cause, two fix sites / severities):

1. **Ordered-argument window funcs over frames not containing the current
   row → SIGFPE + INTERNAL + wrong values** (shapes A–F, H).
   Headline = the **SIGFPE hard crash** on `ntile(2 ORDER BY v)` — a
   process-killing DoS reachable by plain SQL, new vs F10 (F10 only recorded
   the INTERNAL). One issue because all escape via the same
   `use_framing`/`token_tree` ordered-aggregate executor; list both crash
   primitives (`window_rownumber_function.cpp:176` div-by-zero and the
   `NumericCast<int64_t>` underflow) and the wrong-value variants
   (`row_number` 0/−ve, `rank` all-1). Optionally mention that the frame
   legality check applied to plain funcs is bypassed here — the minimal fix
   may be reinstating that validation for the ordered-arg path.
2. **Plain aggregates over inverted/expr-bound frames → negative `count(*)`
   and sporadic non-NULL on always-empty RANGE frames** (shapes G, I).
   Different executor path (standard frame aggregate, no secondary sort),
   silent wrong-values only, lower severity — separate issue, cross-ref #1
   since the underflowed `FRAME_BEGIN/END` vectors are likely produced by
   the same bound-computation code.

The `row_number`/`rank`/`cume_dist`-ord silent-garbage (F, H) arguably
belongs in issue 1 (same executor) — keep it there but call out that some
hits are *value-domain* violations (negative row_number) not just
wrong-answer, since that's the part most likely to corrupt downstream
results undetected.

## 7. Files

- `/tmp/release_frame_fuzz3.py`, `/tmp/release_frame_fuzz4.py` — fuzzers
- `/tmp/release_frame_hits.json` — 7660 hits (EXC/GARBAGE/CRASH)
- `/tmp/release_frame_hits4.json` — 3010 hits, funcs 22–33 confirm pass
- `/tmp/rff3_hits_*.jsonl`, `/tmp/rff4_hits_*.jsonl` — per-worker JSONL
- `/tmp/repro_confirm.py`, `/tmp/ntileprobe.py`, `/tmp/toktree.py`,
  `/tmp/sigfpe.py` — repro/confirmation harnesses
- Debug hits: `/tmp/frame_fuzz_hits.json` (93), `/tmp/frame_fuzz2_hits.json` (110)
