# Absolute Oracle Plan — 针对 value-consistent 缺陷类的 oracle 创新

## Motivation：为什么做这件事

### 现状的尴尬

我们的 oracle 栈全是现成机制的组合（TLP/NoREC/plan-variant/index/equivalence/cross-version/cross-DBMS），novelty 都在验证打包和环上。实测数据把问题钉死了：

- 我们自己的 query 级 pipeline：~800K 次执行，PG 上 0 个确认新 bug
- SQLancer 基线：~0.96M PG 检查 + ~90M 跨引擎检查，0 个确认 bug
- **但 repo 里所有真命中**（fk-snapshot 崩溃、intra-grant catalog 错位、PG-NEW-1 会话锁泄漏、SSI skew、ltree、numeric trunc、partition-prune 缺行）**没有一个是 query 级 oracle 抓的**——全部来自 trace 级探针、回归回放、跨版本差分

结论：自参照 oracle 在 PG 这个面上**已经挖完了**。不是 PG 没 bug，是剩下的 bug 住在现有方法原理上够不到的类里。

### 我们手里已有的那个"类"：value-consistent defects

论文 Figure 1b 的真实案例——PG 16.6 的 `ltree` 比较：

```
'...14653 个标签...'::ltree >  '短标签'::ltree   → false（错）
'...14653 个标签...'::ltree <= '短标签'::ltree   → true （错）
```

六个比较运算符**全部算错，但错得互相一致**：`a>b ≡ ¬(a≤b)` 这类代数关系照常成立。任何 metamorphic oracle（TLP 的三段并、NoREC 的双路径、plan-variant 的换计划）查的都是"两次计算之间的一致性"——**每次计算一起错的时候，关系全满足，值全错**。只有外部参照（另一个版本/expected 文件）能看见它。这类缺陷我们起了名字：value-consistent。

同类的第二个实例：numeric `trunc` scale clamp（scale 2000–3000 时静默截错，coverage mode 靠跨版本才抓到）。

### 一句话叙事（论文立得住的那种）

> 现有所有逻辑 bug oracle 都是 **relation-based**：查两次执行之间的一致性。我们在 PG 上实测发现了它们的结构性盲区——**错得自洽的缺陷类**（value-consistent），并给出该类的刻画（reach bound）。本计划补上第三类 oracle：**absolute anchor**——期望值由声明/构造/求值固定，不经过被测引擎的任何执行路径。

这正是 TLP（"NULL 三值划分"那一类）/TQS（"join 优化"那一类）级别的故事形状：**一类有名有姓的 bug + 一条为什么别人抓不到的原理 + 一个能抓到它的机制**。

## Example Cases：每个 oracle 对应的真实猎物

### 已被我们实测过、但现有 oracle 抓不到的

| Case | 症状 | 为什么现有栈看不见 | 哪个新锚点能抓 |
|---|---|---|---|
| `ltree` 深标签比较（PG 16.6，16.13 修） | 六个比较符全错且自洽 | 所有 metamorphic 关系成立 | **构造的**（域代数： planted 标签数 → 闭式大小关系）/ **求值的**（MiniEval 直接算对答案） |
| numeric `trunc` scale clamp（16.4 修） | scale≈2000-3000 静默截错 | 跨版本外无参照 | **求值的**（解释器算 trunc 的 spec 语义） |
| default partition 缺行（16.6，16.15 修） | `a=ANY(...)` 少返回 2 行，无任何报错 | plan 内部自洽；谓词划分 oracle 可达但我们生成器不生成分区表 | **构造的**（tag 行：`_p=true` 的行必须出现）/ **声明的**（分区边界约束） |
| DF-G 高精度字面量（DuckDB） | literal 绑成 f64 再 cast DECIMAL，**每个等价写法都错得一样** | 只有跨引擎 quorum 抓到 | **求值的**（MiniEval：单引擎内即可判决，不用等第二个引擎） |

### 各新锚点预计覆盖的活面

| 锚点 | 具体猎物（真实 bug 史/活面） |
|---|---|
| 约束契约 | PG master 正在翻修的 `get_relation_notnullatts`/`var_is_nonnullable`/ece `count(x)→count(*)` 折叠/SJE `match_unique_clauses`/join-elimination vs FK；**NOT VALID 约束被误采信**本身就是 canary 探针 |
| join 交换/结合 | EC 构造、USING/NATURAL 合并列、from_collapse 边界——我们 SQLite-A/B 确认过的类 |
| 自描述数据 | BRIN/zone-map/partition-prune 漏行——**所有把 `_p=true` 行弄丢的 access path**，TLP 三段共享同一条坏路径也照抓 |
| 域代数 | join 基数错（键重叠区间已知）、聚合算错（等差数列 SUM 有闭式解）、GROUP BY 基数错 |
| 半/反连接矩阵 | decorrelation、AlternativeSubPlan（recall A14）、mark-join found-labeling（DuckDB #9308/#20410 同类）、NOT IN→anti-join 转换（master-only 新机制） |
| MiniEval | plan-invariant 错误（表达式求值器/binder/literal 解析——**所有 plan 一起错的那类**）、176 个挂起跨引擎分歧的定向裁决 |
| 物理形态守恒 | REFRESH CONCURRENTLY 可空唯一索引 diff-merge（commitfest #6579 **上游正在修**）、CLUSTER/REINDEX 丢行、分区↔继承不等价 |
| 调度竞争 | fk-snapshot（pg160 实崩）、intra-grant catalog 错位、SSI predicate-lock——repo 已证明最肥 |

## 定位

- **要抓的 bug 类**：value-consistent defects——所有执行路径一起错、且错得自洽，所有"关系式" oracle（TLP/NoREC/plan-variant/equivalence/cross-engine 内部变体）原理上不可达。
- **机制一句话**：期望值不来自引擎的另一次执行，而来自**声明**（schema 约束）、**构造**（生成时埋答案）、**求值**（独立迷你解释器）。
- **与框架的关系**：每个新 oracle = 一条新 `Φ_o`，自动接入 CVE（验证/去重/profile/归档）和 reach 矩阵；产出仍是 Candidate → Fixer → cluster。

## Phase 0 — 便宜锚点（先做，验证"加 oracle 能出货"）

### 0.1 声明约束契约（declared-constraint contracts）
- **不变量**：schema 声明的约束是可执行事实
  - FK `f.fk REFERENCES d.pk`：`count(f WHERE fk IS NOT NULL) ≡ count(f WHERE EXISTS(SELECT 1 FROM d WHERE d.pk=f.fk))`；`count(f LEFT JOIN d) ≡ count(f)`
  - `UNIQUE(u)`：`GROUP BY u HAVING count(*)>1` 必空；`count(DISTINCT u)=count(u)`
  - `NOT NULL c`：`count(*) FILTER (WHERE c IS NULL)=0`
  - `CHECK(p)`：`count(*) FILTER (WHERE p IS FALSE)=0`（NULL 合法，注意）
  - canary：`NOT VALID` 约束不得被 planner 采信（反向探针）
- **改动点**：`oracles/index_variant.py:47-50` 的 `parse_tables` 目前**跳过**约束行——先改成保留 PK/UNIQUE/FK/CHECK；新文件 `oracles/constraint_contract.py`
- **候选 kind**：`KIND_CONSTRAINT`
- **假阳性控制**：只用已 VALIDATE 的约束；autocommit 跑（避开 deferred 中间态）；继承/分区约束分开记

### 0.2 join 交换律/结合律（半小时 PoC）
- `A ⋈ B ≡ B ⋈ A`（显式列清单，避免 `SELECT *` 列序差异）；`(A⋈B)⋈C ≡ A⋈(B⋈C)` 内连接；`A⟕B ≡ (B⟖A)` 列互换
- 抓：join 重排序/EC 构造/USING 合并列 bug——SQLite-A/B 这类我们确认过的面
- 实现：纯文本变异 + 行袋比对；挂在 equivalence 族模板下

### 0.3 验收门槛（Phase 0 通过标准）
- 每个契约先在 recall 层自检：手工构造一个"该违例"的 case 确认能触发（否则 oracle 是死的）
- 在 pg166/pg186/duckdb/sqlite 各跑 ≥5k 执行，报告分歧数与验证后命中数
- **Kill 标准**：5k 执行零触发且无 live case → 记录为 bounded 负结果，进入 Phase 1

## Phase 1 — 构造时真值（中成本）

### 1.1 自描述数据（proof-carrying rows）
- 每行带 tag 列：`_p_<i>` = 生成器在插入时**自己算好的**谓词真值；`_grp` = 组 id
- 检查：`SELECT * FROM t WHERE p_i` 返回行集 ≡ `_p_i=true` 的行集（完整性+soundness 双向）
- 抓：丢行/幻影行/错误分区——**包括 access-path 撒谎**（BRIN/zone map/partition prune 漏 `_p=true` 行必被抓）
- 限制：只证成员资格，不证计算值 → 与 1.2 互补

### 1.2 域代数闭式答案
- 数据按闭式结构生成：顺序键 → join 基数 = 重叠区间大小；等差数列 → SUM/AVG/MIN/MAX 闭式解；植入重复向量 → GROUP BY 基数已知
- 已有雏形：`seeds/pg_equalimage_probes.py` 手写 `expected_rows`——此步是把它系统化成一个"可枚举的植入结构代数"
- 抓：**算错的值**（value-consistent 的核心切片）——SUM 算错即使所有 plan 一致也现形

### 1.3 半/反连接等价 × 位置矩阵
- `x IN (SELECT y)` ≡ `EXISTS(y=x)` ≡ `JOIN (SELECT DISTINCT y)`；`NOT IN`+NULL → `count(...NOT IN...)=0` 绝对锚
- 位置矩阵：WHERE/ON/HAVING/SELECT-scalar × NULL 分布 × 相关深度
- 抓：decorrelation/AlternativeSubPlan/mark-join——历史和本 repo 最肥的面之一

## Phase 2 — MiniEval 小域参考解释器（重投入，novelty 主件）

### 2.1 阶段 0：scalar 求值器 + 逆查询审计
- 先只做标量表达式求值（~500 行）：对 `SELECT * FROM t WHERE p` 的结果集做**逐行审计**——每行 p 必须为真、每行不在结果集的 p 必须为假/NULL
- 这是 MiniEval 的最便宜交付物，专抓 access-path 撒谎
- **里程碑**：在 176 个挂起的跨引擎分歧（`results/xengine_4way/divergences.jsonl`）上当第三方裁判演示定向能力

### 2.2 全片段求值器
- 片段纪律（关键）：≤20 行/表；INTEGER/BOOLEAN/VARCHAR≤8/NULL；无 float/cast/collation/date/`/`(整除方言差异)/`%`负数/LIKE/window/LIMIT
- 覆盖：SPJW + IN/EXISTS/标量子查询 + GROUP BY/HAVING + {COUNT,SUM,MIN,MAX,AVG} + DISTINCT + UNION/EXCEPT/INTERSECT[ALL] + ORDER BY
- 生成器直接产 AST → SQL 文本（不解析 SQL，消掉最大复杂度来源）
- ~1.5-2.5k LOC 求值器 + ~600-900 LOC 生成器

### 2.3 信任校准（解释器自己不能是 bug 源）
- bootstrap：~10k 片段 case 在 interpreter + SQLite + DuckDB + PG 上四方对账；interpreter↔consensus 分歧 = 我们的 bug 去修；engine↔engine 分歧 = 方言边界进排除表
- 运行时 quorum：解释器 + ≥1 独立引擎一致才报 bug；全反对 → boundary-review 队列，永不自动报
- 副产品：SQL 方言无关核的实测校准数据（本身可写）

### 2.4 Kill 标准
- bootstrap 对账一致率 <99%（边界修剪后）→ 片段太脏，退回 Phase 1 机制
- quorum 后 FP 率 >5% → 解释器不成熟，暂停扩展

## Phase 3 — 并行支线（不占主线预算）

- **物理形态守恒**：同一逻辑表的 flat/分区/继承/CLUSTER后/REINDEX后/MV 五种存法，bag 必须一致；REFRESH CONCURRENTLY 有可空唯一索引的上游活 bug 脉（commitfest #6579）。~1 天，纯新增
- **调度竞争不变量**（找 bug 而非 novelty 的最强线）：`pg_txn_fuzz.py` 加 FK 表 + journaled reads + catalog flags 断言；先跑 recall 协议（fk-snapshot/intra-grant spec 在 pg160 必响、pg1615 必净）
- **JIT 臂激活**：一个 `--with-llvm` assert build + `EXPLAIN ANALYZE` 的 `JIT: Functions>0` 门控（现有 jit_force 臂是死的：无 LLVM build，0/491）

## 统一验收协议

每个新 oracle 上线前必须过：
1. **自触发测试**：手工构造一个违例 case，确认 oracle 能报（防死 oracle）
2. **recall 窗口**：若覆盖历史 bug 类，加入 `seeds/pg_recall.py` 的 expected 签名矩阵
3. **FP 率**：前 100 个 candidate 人工标注，FP>20% 回炉
4. **reach 登记**：在 §6 reach 表登记新覆盖的 δ 类（`wrong_rows`/`error`/`catalog`/`concurrency`）

## 论文落点

- 新叙事线："relation-based oracles share one structural blind spot: defects that corrupt every execution identically. We name the class (value-consistent), formalize the boundary (reach bound, Prop 3), and add the first **absolute oracle family** — expected values fixed by declaration, construction, or independent evaluation."
- 现有材料全复用：ltree 案例、reach 形式化、bounded-negative 论证、CVE 验证层
- 实验新增：(a) 各锚点 oracle 的 reach 扩展行；(b) MiniEval bootstrap 对账数据；(c) live case（若有）→ case study
