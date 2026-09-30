# Upstream issue material — reportable families

每个族：标题草案 / 最小 repro / expected vs actual / 版本与 main 状态 /
跨引擎佐证 / 已知 issue 关系 / 上报强度。
按可报性排序。**仅 confirmed tier**——excluded 层（F3/F8/F9/F12/F13）
不适合作为 bug 上报。

---

## 第一梯队：可直接上报（dev-confirmed 或最新版复现）

### 1. SQLITE-C — `unistr()` 不合并代理对，输出畸形 UTF-8 ⭐最强
- **Version**: SQLite 3.51.2
- **Repro**:
  ```sql
  SELECT hex(unistr('😀'));          -- → EDA0BDEDB880 (CESU-8, malformed)
  SELECT hex(unistr('\U0001F600'));        -- → F09F9880 (correct UTF-8)
  SELECT hex(char(128512));                -- → F09F9880 (correct)
  SELECT unicode(unistr('😀'));    -- → 65533 (U+FFFD: SQLite's own
                                          --    decoder rejects unistr's output)
  SELECT length(unistr('😀'));     -- → 2 (should be 1)
  ```
- **Expected**: `F09F9880` / `unicode()→128512` / `length()→1`
  (PostgreSQL `unistr(E'\ud83d\ude00')` → 😀; SQLite docs state unistr
  is "intended to work the same as in PostgreSQL, SQL Server, and Oracle")
- **Actual**: raw CESU-8 bytes `EDA0BDEDB880`; lone surrogates and
  out-of-range scalars (`\U00110000` → `F4908080`) emit malformed
  UTF-8 instead of erroring
- **Root cause hypothesis**: escape parser validates syntax only, never
  scalar-value semantics — no surrogate pairing, no range check
- **Verifier**: `scripts/sqlite_unistr_hunt.py` — 10 deviating cases,
  deterministic 10/10 fresh connections
- **Why strongest**: internal inconsistency needs no cross-engine
  adjudication — the engine's own decoder treats its output as invalid

### 2. DF-H — `count(*)` over recursive CTE → internal error / Rust panic
- **Version**: DataFusion 54.0.0 (latest PyPI/crates)
- **Repro**:
  ```sql
  WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t WHERE x<5)
  SELECT count(*) FROM t;      -- 'project index 0 out of bounds, max field 0'
  -- UNION variant: panic 'index out of bounds: len 0 index 0'
  --   at multi_group_by/mod.rs:450
  ```
- **Expected**: 5 (pg 16.2, duckdb 1.5.5 both return 5)
- **Root cause**: zero-column scan of recursive CTE (count(*) projects
  no cols → empty schema panics the grouping path)

### 3. DF-I — `expr op ALL (subquery)` with unprojected column → assertion
- **Version**: DataFusion 54.0.0
- **Repro**:
  ```sql
  CREATE TABLE t(a INT, b INT); CREATE TABLE u(w INT);
  SELECT a FROM t WHERE b > ALL (SELECT w FROM u);
  -- → ProjectionPushdown: Assertion failed: col.name() == matching_name
  ```
- **Expected**: `{1,2,3,NULL}`-class result (pg+duckdb agree)
- **Scope**: all 6 comparison ops repro; ANY/SOME are fine — the
  decorrelation path for ALL references the unprojected column

### 4. DF-D — `GENERATED ALWAYS AS ... STORED` silently returns NULL
- **Version**: DataFusion 54.0.0
- **Repro**: `CREATE TABLE t(a INT, b INT GENERATED ALWAYS AS (a*2)
  STORED); INSERT INTO t(a) VALUES (1),(2),(3); SELECT b FROM t;`
  → all NULL; pg returns 2,4,6. Read/JOIN/WHERE all lose the data.

### 5. DF-A — `EXCEPT ALL` drops duplicate multiplicity and NULL rows
- **Version**: DataFusion 54.0.0
- **Repro**: `{1,1,2,2,2,NULL} EXCEPT ALL {1,2,NULL}` → `[]`;
  expected `{1,2,2}` (pg+duckdb agree). Chained EXCEPT ALL collapses too.

### 6. DF-B — `INTERSECT ALL` returns LHS multiplicity, not min
- **Version**: DataFusion 54.0.0. Upstream: same area as #12955.
- **Repro**: `{1,1,2,2,2} INTERSECT ALL {1,2,NULL}` → `{1,1,2,2,2}`;
  expected `{1,2}` (pg+duckdb agree)

### 7. DF-F — USING/NATURAL merged key NULL on RIGHT/FULL outer rows
- **Version**: DataFusion 54.0.0; duckdb+pg+sqlite all coalesce the
  merged key → DataFusion is the odd engine under ≥3-engine quorum.

### 8. DF-G — high-precision numeric literal silently loses precision
- **Version**: DataFusion 54.0.0
- **Repro**:
  ```sql
  SELECT CAST(123456789012345678901234567890.123 AS DECIMAL(38,3));
  -- → 123456789012345686040493921665.024   (wrong)
  SELECT CAST('123456789012345678901234567890.123' AS DECIMAL(38,3));
  -- → 123456789012345678901234567890.123   (exact — string path fine)
  ```
- **Root cause**: numeric literal binds as f64 before the DECIMAL cast;
  `parse_float_as_decimal=true` (non-default) is exact. Wrongness is
  silent — same input parses exactly via the string path.

### 9. DF-E — correlated subquery rejected outside WHERE; recursive CTE
###         column-alias list dropped
- **Version**: DataFusion 54.0.0; 20 repros.
- Correlated scalar/EXISTS/IN in SELECT list / ORDER BY / JOIN ON →
  "Invalid (non-executable) plan after Analyzer";
  `WITH RECURSIVE t(n) AS ...` ignores the alias list →
  `No field named n`. pg+duckdb execute both.

### 10. DF-J — lateral `UNNEST` unplannable
- **Version**: DataFusion 54.0.0
- **Repro**: `FROM t, UNNEST(t.l) AS e(c)` → `OuterReferenceColumn not
  implemented`; `WITH ORDINALITY` unsupported. pg+duckdb execute;
  projection-form `SELECT unnest(l)` works → lateral path is the gap.

### 11. F1 — correlated EXISTS self-join drops rows (deliminator+
###     filter_pushdown) — **still on main**
- **Version**: duckdb 1.5.5 release AND main `d8cdaa3` (2026-09-15,
  dev-confirmed); also present on 1.0.0/1.1.3 — longstanding.
- **Repro-class**: `SELECT * FROM t a WHERE EXISTS (SELECT 1 FROM t b
  WHERE b.x=a.x AND b.y<>a.y AND b.z>a.z)` — TLP triple-partition gives
  count 6 vs expected 5; disabling deliminator+filter_pushdown restores
  correct result.
- **Upstream**: adjacent to #22267 (closed — covered derived-table
  scope only); this is the residual EXISTS-correlation path.
- 16 curated repros / 240+ occurrences.

### 12. F7 — interval equality is not a congruence (CSE substitution) —
###     **still on main**
- **Version**: duckdb 1.5.5 AND main `d8cdaa3` (dev-confirmed);
  present since 1.0.0.
- **Repro**:
  ```sql
  CREATE TABLE t(d DATE); INSERT INTO t VALUES (DATE '2023-02-28');
  SELECT d + INTERVAL '1 month' a, d + INTERVAL '30 days' b FROM t;
  -- actual: a=2023-03-28, b=2023-03-28  (b carries a's value)
  -- correct (no_cse / no_expr_rw): a=2023-03-28, b=2023-03-30
  ```
- **Mechanism**: `INTERVAL '30 days'` = `INTERVAL '1 month'` under
  normalized equality → CSE dedupes the two `d+interval` expressions
  and substitutes one result into both slots; month arithmetic clamps
  to month-end so the results differ. DuckDB's own docs warn the two
  interval forms differ under addition → substitutability violated.

### 13. F10 — window frame-boundary underflow → **SIGFPE + INTERNAL +
###     silent garbage** (1.5.5; minimal shape fixed on main)
- **Version**: 1.5.5 crashes; main `d8cdaa3` no longer crashes on the
  minimal case — likely covered by upstream window patches; check the
  full stress corpus (828k combos → 10,670 hits) for residual shapes
  before filing.
- **Upstream link found**: PR duckdb/duckdb#24560 ("Fix overflowing
  FOLLOWING and reject negative ROWS window frame offsets", fixes
  issue #24307) — modifies `window_boundaries_state.cpp`
  `FrameBegin`/`FrameEnd` (overflow fallback `partition_begin` →
  `partition_end` + negative-offset rejection). PR is **open/draft**,
  so the minimal-shape fix may have landed separately; residual shapes
  (if any) make it a live issue.
- Report as release-version crash with the fix confirmation; residual
  shapes (if any) make it a live issue.

### 14. F11 — MERGE without INSERT branch → INTERNAL
- `Failed to bind rowid` under build_side_probe_side; residual of
  #20991. 1.5.5 release version.

### 15. F5 — CTE+OFFSET parallel row-drop (flaky_parallel) — **worse on
###     1.5.5 than 1.1.3**
- 40 fresh-connection runs: 1.0.0 → 40/40 correct; 1.1.3 → 38/40;
  1.5.5 → 33/40. `threads=1` eliminates the failures.
- Flaky bugs are legitimate upstream reports when repeatable with a
  stated rate; attach the repeat harness.

### 16. F2 — `PARTITION BY a,a` duplicate key mis-merged (1.5.x only;
###     already fixed on main)
- **Upstream link confirmed**: fixed by merged PR duckdb/duckdb#24685
  ("Compare window partitions as sets", fixes issue #24629) —
  `bound_window_expression.cpp::PartitionsAreEquivalent` compared
  partition lists by raw length + one-directional set membership, so
  `a,a` ≡ `a,b`. Our reproducer is the same defect class.
- Note: follow-up PR #24833 (open) reports #24685 itself introduced a
  new crash — shared partition layout across different partition-count
  window expressions. Sibling-probing around fixes is a real pattern.
- Report against 1.5.5 release with the main-build fix confirmation —
  still useful as a regression note if upstream lacks a regression test.

## 第二梯队：值得上报但措辞要保守

### 17. TIDB-A — TIME 溢出在 TiDB 层不校验（层间语义不一致）
- v8.5.8: `TIME '838:59:59' + INTERVAL 1 HOUR` → `'839:59:59'` where
  MySQL 9.7.1 returns NULL+warning. Sharper: `WHERE (expr) IS NULL`
  (TiKV-pushed) matches the row while `SELECT expr` (TiDB layer)
  returns the out-of-range value — same expression, two layers, two
  semantics.
- Upstream: pingcap/tidb#56865 — still reproduces on v8.5.8; report as
  "still unfixed + here's a sharper inconsistency witness".

### 18. TIDB-B — decimal division extra internal precision
- `1.0/3.0*3.0` → TiDB `1.000000` vs MySQL `0.999990` at matching
  `div_precision_increment=4`. Compatibility divergence; low severity;
  related #21485/#51501. Report only if framed as compat gap, not as
  wrong-result.

### 19. SQLITE-B — USING/NATURAL ambiguity check direction-asymmetric
- Under-rejection (silently binds first duplicate column where
  PG/DuckDB reject) — a spec-compliance gap rather than a crash.
  Deterministic 10/10; pairs with SQLITE-A (same feature area, inverse
  direction) as one coherent "RIGHT/FULL name-resolution" report.
- 12 repros / 19 cases.

### 20. SQLITE-A — `SELECT *` over RIGHT/FULL+USING merged column →
###     spurious ambiguous column
- Over-rejection of legal SQL (PG executes); direction-asymmetric.
  Same report cluster as SQLITE-B.

## 不建议作为 bug 上报（tier-excluded）

- F3 ASOF tie-break plan-dependence → unspecified-semantics
- F8 parallel UNION NOCASE ordering → unspecified-semantics
- F9 debug_window_mode → debug-setting（可作为"debug build 全版本坏"
  的脚注，非用户 bug）
- F12 prefer_range_joins → user-setting（非默认配置）
- F13 `(NULL,NULL) IS NULL` → spec-observation（DuckDB 可能是有意的
  elementwise 设计）
- DF-C → 已驳回（PG 同语义）
- SQLancer FTS5+SUM 候选 → 未确认（state-dependent，可能仅边界语义）

---

# 这些证据能证明"框架有效"吗？——诚实评估

## 已经成立的 claim（证据充分）

1. **端到端发现能力**：21 confirmed 族 / 155 repros / 5 引擎 / 4 引擎
   有虫——包含当前版本、main-branch dev-confirmed（F1/F7 on
   `d8cdaa3`）、SIGFPE 崩溃、Rust panic、层间语义不一致、内部自不
   一致（SQLITE-C）等多种 manifestation。每个族有最小 repro +
   跨引擎或内部 oracle 佐证。
2. **效率优势**：~200k execs → 21 族；SQLancer 强基线 90.3M
   checks / 35 跑 / 5-6 oracle 每引擎 → **0 verified bugs**
   （~1700× 更多执行）。SQLancer grammar 明确够不到 ASOF/
   window-frame/递归CTE/lateral/层间求值/并发这些面——我们的族
   正好分布在其盲区。
3. **发现质量**：版本梯能区分 longstanding（F1/F7）/新引入回归
   （F3/F2）/flaky-恶化（F5: 0%→5%→17.5%）/上游已修（F2/F10-min）
   /debug 层（F9）——σ 归因+版本梯提供了 real triage value。

## Gate 2 支持"组件有效"，但"co-evolution 本身更优"还只是方向性证据

| 被支持的 | 证据 |
|---|---|
| LLM 引导生成 > 随机 | 每个 LLM 臂 = typed_random 的 2–7.7× 族数；coverage 88 vs 53 |
| DCE 邻域扩展是产出引擎 | dce_only 在两版本都是 raw 族数最高臂（1.2 / 4.0） |
| σ 条件化提升精度 | full 在 1.1.3 上 dups 0.2 vs shuffled 5.8（29×）、dce 3.6；nondet-skip 最少（6.6 vs 13.4） |
| 稀疏版上正确引导 > 错误引导 | 1.5.5: full 1.2 > shuffled 0.8 |

| 尚不成立的 | 反例/限制 |
|---|---|
| "full 在发现数量上优于其消融" | 1.1.3 上 shuffled(4.6) > full(1.8)——密集版里乱序引导靠多样性取胜（但 29× dups 说明质量差）；1.5.5 上 full=dce_only |
| 统计显著性 | n=5 seeds、CI 重叠、full 双模态（{0,8,0,1,0}）——方向性而非证明 |
| co-evolution 反馈环的独立贡献 | 还没有直接测"Fixer 诊断改变 Hunter 分布"的因果指标；gate2 各臂只消融调度/扩展/条件化 |

## 要把 claim 钉死还缺什么（按性价比排）

1. **加长 grid**：20 iter × ≥10 seeds，尤其验证"full 的优势随预算
   增长"（诊断越多→引导越准）。查 full 在 1.1.3 的双模态根因
   （是否诊断先烧预算）。
2. **反馈因果证据**：记录每轮 Fixer 产出的 fix_set/plan_diff 是否
   提高了下一轮 Hunter 在该邻域的 hit-rate——这是"co-evolution"
   四个字的直接证据，目前只有间接证据。
3. **真实 bug 口径的 gate2**：现在族数是 run-internal σ key（含
   已知族重现）。对 1.1.3 按"是否命中已知族 vs 新族"分账，能同时
   给出 recall 与 precision。
4. **PG 或第 6 引擎上的迁移性**：oracle 栈已 4 类、5 引擎，但
   "换引擎仍有效"目前最强证据是 TiDB pilot（2 族）+ DF（9 族）。
   PG 诚实阴性本身是覆盖证据（index 面 41k execs），但如果想主张
   框架能攻下成熟引擎，需要更深的存储层交错或更长预算。

## 一句话

**"一个 LLM 驱动的多 oracle 测试系统，用比 SQLancer 少 ~1700× 的
执行在 5 个当前版本引擎上找到 21 个根因级 bug 族，其中两个在
upstream main 上仍可复现"——这句已经成立，且每个数字可复现。
"诊断条件化共进化是这套系统的关键优势"——目前只到"方向性证据 +
精度优势明显"，需要更长预算的配对实验来钉死。**
