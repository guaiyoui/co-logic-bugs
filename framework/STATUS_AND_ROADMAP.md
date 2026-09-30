# CoevoDB 进度与路线图

> 目标：在**当前版本**上发现 10–30 个 verified bug / 引擎（DuckDB 1.5.5、
> PostgreSQL 16.2、SQLite 3.51、DataFusion 54），构成 diagnosis-conditioned
> co-evolution 框架的 evaluation，对标 SIGMOD 级完整度。
>
> 更新：2026-09-15。明细见 `results/FINAL_REPORT.md`（`scripts/final_report.py`
> 生成）、`results/FAMILY_LEDGER_155.md`（人工裁决账本）。
>
> **Review 2026-09-15 后的保守口径**：headline 只计 `tier=confirmed`
> （默认路径或用户可见缺陷）；user-setting / debug-setting /
> unspecified-semantics / spec-observation 分层单列。DF-C 已驳回
> （PG 16.2 实测 `ARRAY||NULL` 同语义），F13 降为 spec-observation，
> F3/F8 降 unspecified-semantics，F9 降 debug-setting，F12 降
> user-setting。
>
> **2026-09-15 深夜更新**：DF round-3 +4 族（G/H/I/J，54.0.0=最新版
> 复核全存活）、SQLite round-2 +2 族（B/C）、TiDB pilot +2 族
> （v8.5.8 vs 真实 MySQL 9.7.1）。**当前 21 confirmed 族 / 155
> repros，5 引擎中 4 个有虫**；仅 PG 诚实为 0（索引面 1671 个
> EXPLAIN 验证 applied 变体阴性）。

---

## 1. 当前战果总账（保守口径）

| 引擎 | confirmed 族 | distinct repros | 表现型覆盖 |
|---|---|---|---|
| DuckDB 1.5.5 | 7 | 74 | 稳定错结果、flaky 并行竞态、SIGFPE 崩溃、INTERNAL 错误 |
| DataFusion 54.0.0 | 9 | 54 | 稳定错结果、internal-error、Rust panic、unplannable |
| SQLite 3.51.2 | 3 | 21 | 过度拒绝、欠拒绝（方向不对称）、畸形 UTF-8 |
| TiDB v8.5.8 | 2 | 6 | 层间语义不一致（TIME 溢出）、精度分歧 |
| PostgreSQL 16.2 | 0 | 0 | —（诚实阴性，见 §4.1：索引面已实战覆盖） |
| **合计** | **21** | **155** | **8+ 表现型** |

另有 5 个记录在册但不计入 confirmed 的族（F3/F8 unspecified-semantics、
F9 debug-setting、F12 user-setting、F13 spec-observation）——它们是证据，
不是 bug 计数。

计数规则：一个 bug = 族内一个 distinct minimal reproducer（setup+query 哈希去重）；
族 = 根因级去重（σ 签名 + 手工机制合并）。nondeterminism、dialect 差、
serialization artifact、guard-rail 报错不计。跨引擎裁决需 ≥3 引擎
quorum，2 引擎分歧一律记 pairwise（C1）。

### 1.1 DuckDB 1.5.5 家族（F1–F13，F4 已拒绝）

| # | 机理 | 类型 | repros/cases |
|---|---|---|---|
| F1 `f4bdb204` | deliminator+filter_pushdown 相关 EXISTS 自连接丢行 | 稳定错结果 | 16/240 |
| F2 `a7e5c9f7` | window_self_join 重复分区键错误合并（1.5.x 回归） | 稳定 | 5/6 |
| F3 `44b71a9c` | ASOF tie-break 随计划翻转（1.1.3→1.5.5 回归） | 稳定 | 15/64 |
| F5 `6ce51ef7`+4 碎片 | CTE+OFFSET 并行丢行；threads=1 全消 | flaky_parallel | 9/36 |
| F6 `ab630f7b` | non_optimizer NULL/EXISTS（疑同 F1 面） | 稳定 | 1/2 |
| F7 `b7178506` | interval 相等非同余→CSE 折错（PG 佐证） | 稳定 | 4/11 |
| F8 `a88e4241` | 并行 UNION+COLLATE NOCASE 行集不稳定 | flaky_parallel | 1/1 |
| F9 `c74e4121`+`f62475b3` | debug_window_mode='combine' → COUNT(*) OVER 归 0 | 稳定 | 2/2 |
| F10 | 窗口 frame 边界下溢：**SIGFPE**+INTERNAL+静默垃圾值（828k combos → 10670 hits） | crash+INTERNAL+错值 | 34/10670 |
| F11 | MERGE NMBS 无 INSERT 分支 → INTERNAL（#20991 残余面） | INTERNAL | 2/2 |
| F12 | prefer_range_joins 使合法相关 SQL 报 merge-join 错 | unplannable | 2/2 |
| F13 | `(NULL,NULL) IS NULL`→FALSE（违 SQL 标准；PG/SQLite=TRUE） | spec 分歧 | 1/1 |

### 1.2 DataFusion 54.0.0 家族（54.0.0=PyPI 最新版，round-3 复核全存活）

| # | bug | repros |
|---|---|---|
| DF-A | `EXCEPT ALL` 丢重数+丢 NULL；链式同塌 | 3 |
| DF-B | `INTERSECT ALL` 返 LHS 重数非 min（upstream #12955 同区） | 2 |
| ~~DF-C~~ | ~~`list \|\| NULL` → rhs~~ **已驳回**：PG 同语义，2 引擎无 quorum | — |
| DF-D | `GENERATED ALWAYS AS ... STORED` 静默 NULL，读/JOIN/WHERE 全丢行 | 3 |
| DF-E | SELECT 位相关标量子查询→non-executable plan；递归 CTE 列名绑定失效（round-3 scope 扩至 20 repros） | 20 |
| DF-F | USING/NATURAL 合并键在 RIGHT/FULL 外表行不 coalesce→NULL | 4 |
| DF-G | 高精度数字字面量先绑 f64 再 CAST→DECIMAL 丢精度；字符串 cast 精确 | 5 |
| DF-H | `count(*)` over 递归 CTE（零投影列）→ project index OOB / Rust panic @multi_group_by:450 | 5 |
| DF-I | `expr op ALL (subq)` 且比较列未投影 → ProjectionPushdown 断言失败（6 op 全中，ANY/SOME 正常） | 8 |
| DF-J | lateral `FROM t, UNNEST(t.l)` → `OuterReferenceColumn not implemented` | 4 |

干净面（阴性证据）：500 executor-config 组合×20 查询 0 分歧；
NOT IN+NULL 三值正确；approx_* 有界；timestamp 单位正确。

### 1.3 SQLite 3.51.2（round-2 深扫后 3 族）

- **SQLITE-A**：`SELECT *` + RIGHT/FULL JOIN over USING 合并列 →
  误报 ambiguous column（过度拒绝；方向不对称）。
- **SQLITE-B**：复合 join 操作数带重名列时 `USING(x)`/`NATURAL` 的
  歧义检查方向不对称——歧义在左侧扁平 join 树且链上有 RIGHT/FULL 才
  报错，在右侧/子查询后/纯 INNER 链静默绑第一列（欠拒绝；PG/DuckDB
  对所有形式都拒绝）。
- **SQLITE-C**：`unistr('\ud83d\ude00')` 不合并代理对→输出 CESU-8
  `EDA0BDEDB880`，而 `char(128512)`/`unistr('\U0001F600')` 得正确
  `F09F9880`；`unicode()`/`length()` 把 unistr 自身输出当非法——
  **引擎内部自不一致**（不依赖外部参照系的最强形态）。文档自承诺
  "与 PostgreSQL 相同"。

### 1.4 TiDB v8.5.8（pilot 完成，vs 真实 MySQL 9.7.1）

- **TIDB-A**：TIME 溢出在 TiDB 层不校验——`WHERE`（TiKV 下推）见 NULL
  而 `SELECT`（TiDB 层）见越界值 `839:59:59`；同行同表达式两层语义
  不一致。上游 #56865 相关，v8.5.8 仍复现。
- **TIDB-B**：decimal 除法内部精度超 MySQL 语义
  （`1.0/3.0*3.0`→`1.000000` vs MySQL `0.999990`，同
  `div_precision_increment=4`）——低 severity 兼容性分歧，
  #21485/#51501 相关。
- pilot 覆盖面：61 语义例 + 8 个 `tidb_enable_*` plan 变体对；
  59/61 一致，8/8 变体对一致。

---

## 2. SQLancer 基线对比（paper 对照组）

`results/sqlancer_baseline/`：SQLancer 2.0.0 @9eb1db82，900s/引擎、4 线程、
seed 固定、JDBC 升到 1.5.5 同版本对照。

| 引擎 | oracle | checks | bugs |
|---|---|---|---|
| duckdb 1.3.0 / 1.5.5 | TLP | ~1.78M | 0 / 0 |
| sqlite3 3.49.1 | NoREC | ~5.5M | 0 |
| postgres 16.2 | TLP | ~556k | 0 |
| **total** | | **~7.8M** | **0** |

**我们 ~52k execs → 19 族/115 repros；SQLancer 7.8M checks → 0。**
差距机理：SQLancer 的 grammar 不产 ASOF join、window frame 压力、并行
flaky、set-op ALL 重数、generated col、RIGHT JOIN——我们全部命中点都在
其覆盖之外。

---

## 3. 已建成的框架资产

- **Oracle 栈**：plan-variant（DuckDB 33 规则+执行模式开关）、4 引擎多数
  表决差分（`scripts/cross_engine.py --engines duckdb,postgres,sqlite,
  datafusion`）、DML 快照 oracle、flaky-miner（重复执行归因
  flaky_parallel）。
- **σ 归因**：`diagnosis/signature.py`——干预集二分 + plan_diff + 版本梯
  → 族 key；稳定性闸门（flaky 不进规则归因）、非确定性词法闸门
  （TABLESAMPLE/USING SAMPLE/random/`LIST()` 无内序）。
- **Runner**：duckdb(1.5.5 venv)/postgres(pgserver 16.2)/sqlite/stdlib/
  datafusion 54。
- **报告机**：`scripts/final_report.py` → 每族 repro 数、表现型、fix_kind、
  plan_diff、效率表、unmapped 审计直方图。
- **长尾审计结论**：749 个未入账 σ key 全为归因噪声（F5 碎片 + gate 前
  采样泄漏 + 旧版回归数据），无隐藏新族。

---

## 4. 路线图：各引擎如何达到目标

### 4.1 PostgreSQL（0 → 诚实阴性）：索引面已实战覆盖

**已完成的 index-presence oracle 实战**（`oracles/index_variant.py`
+ `scripts/pg_index_hunt.py`，已接入 Hunter PG 路径）：

- 1398 seeds → **1671 applied 变体**（EXPLAIN 验证真实走 Index Scan/
  Index Only Scan/Bitmap Heap，非空转）+ 3279 noop（如实记账）→
  41,022 SQL executions → **2 发散均裁决为非 bug**
  （`JSON_OBJECTAGG` 无 ORDER BY 键序随访问路径变——unspecified
  ordering 假阳）
- 19 个索引稠密定向种子 → 84 applied → 0 发散
- 覆盖的变体类：btree/partial/expression/covering→IOS/BRIN/GIN/
  HOT-chain update/ANALYZE，`enable_seqscan=off` 强制 + `DROP INDEX`
  状态复原
- **不可达面**：pgserver 内嵌 PG 不带 contrib（pg_trgm/intarray/
  hstore/ltree/cube/btree_gin 全不可用）——已记为覆盖限制

**解读**：PG 16.2 在 plan-variant、index-presence、DML/cursor/并发
probe 各面都干净。要么再砸更深的存储层交错（HOT+IOS 已覆盖），要么
接受"成熟引擎+此预算下零产出"作为诚实结果——两种写法 paper 都站得住。

**剩余可打**（预期低-中）：统计操纵 oracle（CREATE STATISTICS/手工
pg_statistic）、JIT 深面（`jit_above_cost=0`）、plan 失效
（PREPARE→DDL→EXECUTE）、REFRESH MV CONCURRENTLY。

### 4.2 SQLite（1 → 目标 5–10）

1. **索引 oracle 直接可移植**：partial/expression index、`INDEXED BY`/
   `NOT INDEXED` 强制、**手工写 `sqlite_stat1` 换 plan**（SQLite 特有零
   成本 plan-variant）、`PRAGMA automatic_index/reverse_unordered_selects`。
2. **RIGHT/FULL JOIN 继续扫**（SQLITE-A 出处；3.39 新代码）：方向混合
   join 链、进 subquery/view/CTE、ON 非等值、USING×NATURAL×ON 混排。
3. **JSONB 函数对 oracle**：`jsonb_*` 二进制路径 vs `json_*` 文本路径
   同输入应同值（3.45+ 新代码）。
4. RETURNING+trigger/CTE；UPSERT partial conflict target；ATTACH 双库
   +savepoint+FK；FTS5 边界（rank/offsets/aux）。

### 4.3 DataFusion（6 → 目标 10–15，矿脉仍活）

1. **递归 CTE**（`enable_recursive_ctes` 全新代码，列名绑定已证坏）：
   UNION/UNION ALL 终止语义、跨迭代类型变化、深度边界。
2. **子查询去相关矩阵**：correlated {EXISTS,NOT EXISTS,IN,
   **NOT IN+NULL**（三值陷阱）,scalar} × 位置 {WHERE,HAVING,SELECT,ON,
   ORDER BY}——DF-E 已证 SELECT 位未实现，其余格大概率还有。
3. UNNEST/嵌套 list/struct/NULL 元素；approx 聚合 vs 精确值；
   Arrow 类型严格性（unsigned×signed、timestamp 单位、decimal 精度）；
   QUALIFY+window。
4. 注意：DF plan-variant oracle（26 toggle×查询）494 execs **0 hit**——
   DF bug 在执行语义层，不走计划依赖路线。

### 4.4 TiDB（第 5 引擎）—— pilot 已完成，2 族入帐

- `tiup playground` 起 v8.5.8 + 真实 MySQL 9.7.1 参照（违标即 bug，
  比多数表决更强）。产出 TIDB-A（层间 TIME 溢出不一致）、TIDB-B
  （decimal 精度分歧）。见 §1.4。
- 扩产方向（若继续）：`tidb_enable_*` 开关集大扫、TiKV 协处理器下推
  vs TiDB 层求值的更多表达式类、pessimistic/optimistic txn 交错。

---

## 5. 框架层增量（对 paper contribution 也值钱）

新增两类可移植 oracle 后，框架共 **4 类 oracle**：

| oracle | 已验证产出 |
|---|---|
| plan/executor variant | DuckDB F1/F2/F3/F7/F9/F11/F12 |
| N-engine majority 差分 | DF-A~J、SQLITE-A/B、F2/F7 佐证 |
| **index-presence** | `oracles/index_variant.py` + `pg_index_hunt.py` 实战：PG 1398 seeds/1671 applied/41k execs → 2 假阳（unspecified order）已裁决；已接入 Hunter PG 路径 |
| **违标差分**（TiDB vs MySQL） | TIDB-A/B——"自承诺兼容"比多数表决更强的 oracle 类 |
| **stats-manipulation** | 并入 index-presence（ANALYZE/stat 变体）+ PG `pg_index_hunt` |

外加 flaky_parallel 归因（threads=1 复判定）+ 非确定性闸门 +
σ 家族去重 = 方法论完整度。

## 5.1 Review 2026-09-15 修复落账（C1–C7）

| 项 | 修复 |
|---|---|
| C1 跨引擎 quorum | `classify_divergence()`：≥3 引擎且 plurality≥2 才记 majority；否则 pairwise（`div_majority`/`div_pairwise` 分账） |
| C2 压缩轴空转 | SET 后补 `FORCE CHECKPOINT`（实测 pragma_storage_info 出现 RLE/DICT_FSST） |
| C3 teardown 污染 | 全部改 `RESET <setting>`，不再覆盖调用方会话 |
| C4 σ 过合并 | single/minimal_set 的 identity 加 plan_diff 类；baseline 复判 3→5 轮；S1 仅 `threads` → `flaky_parallel` |
| C5 bandit 记账 | 新增 `reward()`（不增 tries）；实例级 `update()` 在 co_evolution 循环按生成查询数记账 |
| C6 执行口径 | `sql_executions` 统一计数器落在 4 个 runner 的 run/setup；DCE/bisect/fixers 全部同一口径 |
| C7 版本梯 | 旧版 Parser/Binder/Catalog 错 → `nu=unknown`（特性不存在 ≠ regression） |

回归测试 22 个全过（`pytest tests/`）。

## 5.2 Gate 2 配对消融（框架有效性验证 — priority 2）

`scripts/gate2.py`：5 臂 × 多 seed × 等预算配对实验——
`typed_random`(no_llm) / `coverage_only`(bandit 只见 novelty) /
`dce_only`(扩展无 σ) / `shuffled_diag`(DCE 用打乱 σ 条件化) /
`full`。指标：每千次执行新族数、time-to-first-family、duplicates、
nondet-skip、bootstrap CI95。冒烟（5臂×1seed×2iter）全通；
正式网格 5×5×8iter 双版本**已完成**——见 `results/GATE2_RESULTS.md`
与 FINAL_REPORT 末节。结论摘要：

- LLM 臂（coverage/dce/shuffled/full）族数为 typed_random 的
  2–7.7×；coverage ≈88 vs ≈53
- **dce_only 是 raw 发现率最高的臂**（1.5.5: 1.2, 1.1.3: 4.0）——
  诊断邻域扩展是产出来源
- **σ 条件化的价值在精度不在数量**：full 在 1.1.3 上 dups 0.2 vs
  shuffled 5.8（29×）/dce_only 3.6；nondet-skip 最少
- 稀疏版上 full(1.2) > shuffled(0.8)——正确引导在 bug 稀少时胜出
- 诚实限制：n=5、高方差、CI 重叠；full 在 1.1.3 双模态（0,8,0,1,0）

## 5.3 Co-evolution 耦合机制：Playbook（迭代积累证据）

Gate 2 只证明"单次诊断有价值"，没证明**迭代复利**。新增的
playbook 是双 agent 之间的持久化知识载体——这是"曲线增长"主张的
物质基础：

- `evolution/playbook.py`：每条 confirmed 族 → LLM 蒸馏成可迁移
  rule（title/mechanism/probe_hint），超容量时按产出淘汰最弱规则
- 下一轮 Hunter prompt 注入 `playbook_digest`；模型须给每条查询标
  `inspired_by`（规则 id）——**诊断→查询的因果边变得可测量**
- `Candidate.inspired_by` 全 oracle 路径传播（TLP/NoREC/plan/index/
  equiv/x-engine/x-version），`from_dict` 经 `__dataclass_fields__`
  自动往返
- `iter_stats` 每轮记 `inspired_queries/candidates/families/
  playbook_size`；`summary` 含规则级归因（每规则 q→c→fam 转化）
- `main.py` 新模式：`coevo`（full+积累）、`coevo_warm`
  （`--playbook-seed` 预热）、`coevo_frozen`（种子+冻结蒸馏）
- 种子：`scripts/make_playbook_seed.py` 把 14 个非-DuckDB confirmed
  族（DF/SQLite/TiDB）蒸馏成 `results/playbook_seed_xeng.jsonl`——
  测**跨引擎知识迁移**

**曲线实验** `scripts/coevo_curve.py`：3 臂 × 5 seed × 12 iter ×
8 q/iter，`full` vs `coevo`(冷启动) vs `coevo_warm`(14条跨引擎规则
预热)。判定标准：(a) coevo 臂累计族曲线与 full 分离；(b) 臂内
inspired 查询命中率 > 非 inspired——后者是诊断因果边的直接测量，
不受预算/运气混淆。冒烟验证：warm 跑 18 查询中 11 条带规则引用
（R3~R13 分布广），inspired 查询确实产出 candidates。

**跨引擎 warm 转移（LOO 种子）**：sqlite/datafusion/postgres 三
引擎各跑 full vs coevo_warm——三者在播种臂下均 0 族（PG：
2 臂 × 2 seed × 12 iter，warm 臂 72% 查询引用规则、候选 +63%
但全部裁决为 FP/方言分歧）。PG 16.2 维持诚实阴性，与 §4.1
一致——迁移放大产出的前提是目标存在对应 bug 面。
详见 `results/COEVO_CURVE.md` "PG warm 转移" 节。

**与 R1/R2 的衔接**：R1 证 repro 集让修复 0/3→3/3；R2 证错误诊断
锚定伤害定位（F2 c_diag 1/3 vs a/b 3/3）。playbook 把这两个方向
接成闭环：Fixer 蒸馏→Hunter 受启发→新族再蒸馏→规则库进化。

**main-build 复核**（`duckdb_src` debug `d8cdaa3`）：F1/F7 仍复现
（dev-confirmed）；F2/F10-minimal 已修——1.5.5 族分"仍存活"与
"上游已修"两类，都是有效证据。版本梯 recall 表见 ledger 末节
（F1/F7 longstanding；F3 新引入；F5 flaky 自 1.1.3 且恶化；
F9 debug 层全版本坏）。

## 6. 立即执行清单

| # | 任务 | 预期产出 | 状态 |
|---|---|---|---|
| 1 | PG index-presence oracle | 已完成：1671 applied/41k execs/0 confirmed（诚实阴性） | ✅ |
| 2 | SQLite 深扫 | +2 族（B/C），717-seed 扫尾 40 分歧全裁决 | ✅ |
| 3 | DF 最新版复核+深扫 | 54.0.0=最新；+4 族（G/H/I/J） | ✅ |
| 4 | Gate 2 配对消融 5×5×2 版本 | **完成**：LLM 臂 2-7.7×随机；σ 条件化=精度（dups 0.2 vs 5.8） | ✅ |
| 5 | TiDB pilot | +2 族（A/B） | ✅ |
| 6 | upstream issue 素材：F1/F7(main 存活)/F10/DF-A~J/SQLITE-B,C/TIDB-A | paper 素材 | 待做 |
| 7 | recall 量化：版本梯已知 bug 复现率 | **完成**：版本存活表在 ledger 末节 | ✅ |
| 8 | SQLancer 强基线 | **完成**：35 跑/90.3M checks/0 bug（agent f042d1a2） | ✅ |
| 9 | Gate2 后续：20-iter 长跑验证 co-evolution 优势随预算增长；查 full 在 1.1.3 的双模态 | evaluation 强化 | 待做 |
| 10 | SQLancer FTS5+SUM 候选：最小化裁决（state-dependent，或仅为边界语义） | 待裁决 | 待做 |
| 11 | TiDB/MySQL 进程 + pgserver:55432 清理 | 环境 | 待做 |
| 12 | **co-evolution 曲线实验**（coevo_curve.py：4 臂 × 5 seed × 12 iter） | **完成**：warm 2.4 / frozen 1.6 / full=cold 0.4；seed0 同种子同初始规则下 10 vs 4，+6 族含 4 个可归因于运行中蒸馏的子规则、2 个默认模式强 bug——`results/COEVO_CURVE.md` | ✅ |
| 13 | playbook 扩展：`coevo_frozen` 消融 + 规则质量过滤（坏规则淘汰已在 eviction 里） | **frozen 消融完成**；规则过滤待做 | 部分 |
