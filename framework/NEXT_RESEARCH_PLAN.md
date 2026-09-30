# CoevoDB v3：Diagnosis-Conditioned Co-Evolution for Sound DBMS Bug Discovery and Repair

状态日期：2026-09-14。本文件取代旧版 "V2：LLM-Efficient Logical Bug Testing Plan"。旧版把"降低 LLM 成本"当主线，是工程目标而不是研究问题；本版把 **Hunter–Fixer 共同进化机制** 放回中心，成本控制降级为第 8 节的基础设施门槛。

## 0. 两句话

DBMS 逻辑 bug 的发现（fuzzer + 健全 oracle）和修复（coding agent）目前是两条互不学习的流水线：fuzzer 不知道"这个 bug 错在哪条优化规则、触发前提是什么"，repair agent 只看到被丢给它的那一个反例。我们让二者通过一个**可验证的诊断–反例通道**共同进化：Fixer 输出的结构化诊断（最小修复规则集、计划差分、前提假设、补丁）定义 Hunter 下一步的搜索邻域（Diagnosis-Conditioned Exploration，DCE）；Hunter 在该邻域里用健全 oracle 生成的反例反过来加固 Fixer 的补丁并扩展修复课程（Oracle-Hardened Repair，OHR）。两个方向都用确定性证据闭合，LLM 从不参与 bug 判定。

## 1. 审计结论：现状、证据、对研究有效性的影响

下表把用户审计与我对代码/产物的复核合并。"影响"一栏区分"成本问题"和"研究有效性问题"——后者会让现有结论不成立，优先级最高。

| # | 发现 | 证据 | 影响 | 处置（章节） |
|---|---|---|---|---|
| A1 | **reward 信号被不健全判定污染** | 完整 run 的 bandit 根因 arm `rc:binder:*` 记录 `true_bugs=5,3,3,2,…`；这些 verdict 来自 `equiv`/`differential`，由 LLM 判"等价/旧版正确"后升级；`verify_bugs.py` 复核后仅 4 个 sound 签名存活（40→4）。random 对照 `bugs_by_kind = {plan_variant:1, differential:4, equiv:1}` | **研究有效性**：bandit 学到的是 LLM 幻觉的分布，"4 vs 1"不能作为反馈有效的证据 | §3.3 reward 只接受 sound-verified 新 family；§5 P2 |
| A2 | `no_llm` 实际 143 次 LLM 调用 | `hunter_metrics.total_calls=133`（guideline 通过 hunter 调用）+ `fixer_metrics.total_calls=10`；`summary.total_llm_calls=10` | **研究有效性**：零 LLM 对照被污染 | §8 Phase 0：NullLLM + 单账本 |
| A3 | `total_llm_calls` 漏记 guideline | full run：summary 772 vs 组件 509+312=821，差 49≈150/3 | 成本核算失真 | §8 Phase 0 |
| A4 | full/random 预算不等 | full 821 calls/1.45M tok；random 658 calls/1.15M tok；random 仍接收 guideline LLM 反馈、rewrite_memory、pattern_memory 反馈，只是 bandit=None | **研究有效性**：对照既不等预算，也不是"无反馈" | §6 预算匹配协议 + 干净对照定义 |
| A5 | root dedup 只是 SQL 关键词签名 | `oracles/models.py::root_signature` = 函数名 + 19 个关键词 | 同一根因分裂/不同根因合并都会发生；bug 计数不可信 | §3.1 L0 诊断签名 σ |
| A6 | Fixer 不修源码，只缩减 + LLM 猜根因 | `agents/bug_fixer.py::_analyze_root_cause`；`coevo/repair.py::CommandRepairer` 未接入主循环 | "co-evolution"中 Fixer→Hunter 通道只有一段自由文本 hint | §3.2 三级 Fixer ladder |
| A7 | coverage 是一维标签并集 | `coverage/store.py` 集合大小；103 vs 101 | 无法区分"多探索了什么"，AUC 无意义 | §3.5 结构化 coverage |
| A8 | 每轮固定 3 次 LLM（db/query/rewrite） | `bug_hunter.py::execute` | 成本；且 LLM 在热路径上使 valid-execution 预算与 token 预算耦合 | §3.6 事件驱动协议 |
| A9 | guideline 每 3 轮调用且输出不可验证 | 第 149 轮 guideline 引用了"5/8, 3/4"等 LLM 自己编的统计，random run 引用了并不存在于本轮的 "issue17266 pattern" | LLM 生成的"约束"本身是幻觉源 | §3.6：guideline 由账本统计确定性生成，LLM 只写模板 |
| A10 | 176 个 cross-engine divergence 全部过 LLM，但代码规定永不升级 | `bug_fixer.py::triage KIND_CROSS_ENGINE` | 纯浪费 | §3.6 禁止清单 |
| A11 | version divergence 让 LLM 判对错，verifier 再全部降级 | `triage KIND_DIFFERENTIAL` → `_judge_version_divergence` | 浪费 + A1 的污染源之一 | §3.2 L0 时间定位替代 |
| A12 | root dedup 在 LLM triage 之后 | `co_evolution.py` L115 triage → L153 root key | 同一 family 反复被 LLM 分析 | §3.4 loop：先签名再决定是否触发 LLM 事件 |
| A13 | 每次调用新建 HTTP client；统一 `max_tokens=2000` | `base_agent.py::call_llm` | 成本 | §8 Phase 0 |
| A14 | **bandit 只在产生 candidate 时更新** | `learn_from_feedback` 仅由 candidate 触发；一个类别生成 8 条查询零命中时 `tries` 不变 | 零产出类别不被惩罚，UCB 探索项失真 | §3.3 按实例化次数记账 |
| A15 | 种子类别 `tries=0, true_bugs=6` | `pattern_memory.seed_self_join_exists` | 同 A14；seed 流与 LLM 流统计口径不一致 | §3.3 |
| A16 | novelty 按类别总量重复归因给每个 candidate | `co_evolution.py` L129 | reward 重复计数 | §3.5 每 testcase 单独归因 |
| A17 | `equiv` 在 `old_differs`/`unavailable` 时仍可 `true_bug` | `triage` L458–470 "relies on LLM equivalence judgment" | **健全性漏洞**：LLM 判定进入 verdict 路径 | §3.2 "sound promotion" 规则 |
| A18 | 首个 bug 出现在 iteration 0 | 历史记录 | 不能归功于反馈 | §7 主张边界 |

**现有证据的诚实边界**：DuckDB 1.5.2 上 2 个经健全 oracle 确认的行为家族（deliminator `EXISTS` 自连接；`NOT EXISTS` NULL 反连接），PostgreSQL 16.2 上 0 个；关于共同进化因果作用的证据为 **零**（A1、A4、A18 三条各自足以否定）。46 tests passed。

## 2. 研究问题与定位

### 2.1 研究问题

> 在相同的有效执行预算与 token 预算下，**由修复侧诊断定义的搜索邻域**（DCE）是否比查询计划覆盖引导（QPG）、随机类别和无反馈 LLM 生成发现更多**互不相同、经健全 oracle 确认**的 bug 行为家族？反过来，**由发现侧健全反例加固**的补丁（OHR）是否比只用 reproducer + 回归套件验证的补丁更少过拟合？

### 2.2 相对已有工作的位置

| 工作 | 机制 | 与本方案的关系 |
|---|---|---|
| SQLancer TLP / NoREC（Rigger & Su 2020） | 健全 metamorphic oracle | 我们的判定基座；不改 |
| QPG（Ba & Rigger, ICSE'23） | 以唯一查询计划多样性引导 DB 状态变异 | 我们的 **coverage-only 对照**；DCE 必须在控制计划覆盖后仍有增益（H3） |
| TQS（Tang et al., SIGMOD'23 best paper） | DSG 用范式化生成真值；KQE 用图同构避免重复搜索 | 借鉴"用结构等价类去重搜索空间"，但我们的等价类由 **诊断签名** 而非查询图定义 |
| Argus（Mang et al., SIGMOD'26） | LLM 生成 CAQ 等价模板对，prover 证明，再实例化 | 旧 V2 的"typed template"与之重合。本版模板 **由已确认 bug 的诊断派生**，并用 TLP/NoREC/plan-variant 判定，不依赖 prover；Argus CAQ 可作为一个 oracle family 接入 |
| EvoRepair（ICST'24）、Agent-CoEvo（2026）、CoHarden（2026）、Iter-T | 针对**给定单个 bug** 共同进化测试与补丁，测试来自 F→P 标签或 mutation patch | 我们的测试侧是**开放式 bug 发现**且 oracle 健全（无需标签）；新增 **repair→discovery 方向**：诊断驯化搜索。CoHarden 的 Rigorous/Lax 区分启发了 OHR 的度量 |
| GEPA（ICLR'26） | 自然语言反思驯化 prompt 进化 | L1 Fixer 生成"前提假设→模板"的方式类似反思，但只写入结构化模板，不写自由 prompt |

**新颖性主张（可被否定）**：(i) 用确定性 **优化规则二分 + 计划差分** 得到的诊断签名同时充当根因去重和搜索邻域定义；(ii) 修复诊断→发现方向的因果验证（含 shuffled-diagnosis 负对照）；(iii) 用健全 metamorphic oracle 做无标签的补丁加固与过拟合度量。

## 3. 形式化框架

### 3.1 对象与健全性

- 测试用例 $t=(D,Q,o)$：数据库状态 $D$、查询 $Q$、oracle $o\in\mathcal O$。$\mathcal O=\{\text{TLP},\text{NoREC},\text{PlanVariant},\text{Crash},\text{CAQ}\}$。
- $o$ 在条件 $\mathrm{Cond}(o)$ 下健全：$\mathrm{Cond}(o)\wedge o(t)=\mathsf{fail}\Rightarrow$ 目标引擎存在 bug。$\mathrm{Cond}$ 至少包含：$Q$ 确定性（ORDER BY 键唯一或按 bag 比较）、无值依赖错误（溢出/除零/转换）、无非确定函数。不满足 $\mathrm{Cond}$ 的 $t$ 记为 `skipped`，永不进入任何计数。
- **不健全信号**（跨版本差分、跨引擎差分、LLM 等价改写）只能产生 *候选*，且只有通过 **sound promotion** 才能计数：对候选 $(q_1,q_2)$，在目标引擎上对 $q_1$、$q_2$ 分别运行 $\mathcal O$ 中的健全 oracle；任一健全命中即以该命中为 bug 证据，候选本身作废。没有健全见证的候选归档为 `divergence`，不计数、不触发 LLM。这条规则关闭 A17。

**诊断签名 $\sigma(t)$（替代关键词签名，A5）**。设引擎暴露一组可单独开关的**干预基** $R$——每个干预是一次"关掉它重跑、看 bug 是否消失"的确定性实验。按定位能力排序：

- **优化规则**：DuckDB 1.5.2 `duckdb_optimizers()` 给出 33 条，`SET disabled_optimizers`；
- **Plan-shape GUC**（兼管 executor 定位）：PostgreSQL `enable_hashjoin/mergejoin/nestloop/hashagg/sort/seqscan/indexscan/bitmapscan/memoize`、`jit` 等——关掉某算子后 bug 消失即定位到该算子的实现而非优化器；
- **执行设置**：`threads`（并行↔串行）、vector_size、插入顺序等；
- **类型格**：对 reproducer 的列做类型替换（INT→BIGINT→DECIMAL→DOUBLE→VARCHAR），ddmin 求翻转结果的类型赋值——cast/精度族；
- crash 类可加栈指纹分量。

定义

- 单干预修复集 $S_1(t)=\{r\in R:\ o(t\mid R\setminus\{r\})=\mathsf{pass}\}$，逐基求取后合并；
- 若所有基上 $S_1=\varnothing$，用 ddmin 求 1-minimal 修复集 $\Delta^*(t)$；所有干预都无效时标记 `non-optimizer`（binder/executor 深层/存储族，此时 $\sigma$ 退化为 $\pi$+$\nu$+AST 形状，DCE 相应弱化——诚实降级而非假装能定位）；
- 计划差分模式 $\pi(t)$：优化/未优化物理计划树的规范化差分，仅保留发生变化的最小子树，算子名归一、列名/常量抽象；
- 时间定位 $\nu(t)$：在 pip 版本梯（0.9.x…1.5.5）上二分得到首个失败版本，或 `since ≤ v_min`。

$\sigma(t)=(\text{engine},\text{version},S_1\ \text{or}\ \Delta^*,\ \mathrm{class}(\pi),\ \nu)$。**行为家族** $F$ = $\sigma$ 的等价类。范围边界：并发/隔离异常、存储损坏、协议层 bug 无法被本干预基触及，需要另一族 oracle（如隔离历史测试），属明确的 scope 外。已验证实例：deliminator bug 的 $S_1=\{\texttt{deliminator},\texttt{filter\_pushdown}\}$，`join_order` 不在其中；这已经把它与"NOT EXISTS NULL 反连接"区分开（后者需实测填入）。$\sigma$ 是 $(t,\text{version})$ 的确定性函数，因此去重可复现；过合并/欠合并风险用 §6 的人工抽样度量，不假装不存在。

### 3.2 Fixer：三级诊断–修复 ladder

Fixer 的输入是 **已按 $\sigma$ 去重后的新家族** $F$ 及其 1-minimal reproducer（ddmin 删除算子：INSERT 行、SELECT 项、谓词合取项、JOIN 分支）。三级输出逐级增强，每级都有确定性验证：

| 级别 | 输出 $d_F$ | 何时可用 | LLM | 验证 |
|---|---|---|---|---|
| **L0 确定性诊断** | $\sigma(F)$、$\pi$、$\nu$、组件标签（optimizer/non-optimizer） | 始终 | 0 | 重放 20/20；签名跨进程稳定 |
| **L1 前提假设→模板** | 结构化模板 $\tau_F$（§3.3 DSL）：断言"触发前提 $C$ 的边界"，例如"相关子查询自连接 + 一个等值 + ≥2 个不等值谓词 + 内表非空" | 每个新家族 1 次事件 | 1 次（≤600 tok 输出） | 模板必须能实例化并 **重新触发 $F$**（re-hit ≥1/50），否则丢弃并记 `L1_rejected` |
| **L2 源码补丁 + 加固** | git worktree 中的 patch $P_F$，回归测试 | 需可编译源码（本机 g++/16 核/62 GB 可行；DuckDB 全量构建约数十分钟，增量更快） | coding agent | reproducer 通过 ∧ 上游回归套件通过 ∧ **OHR 加固**（§3.4） |

L0 本身就是有研究价值的贡献点：它把"修复定位"从 LLM 文本变成可重放的规则/计划证据，而且对 PostgreSQL（无法关闭全部优化）用 GUC 子集同样定义。L2 是可选增强，不是主线阻塞项。

### 3.3 Hunter：模板空间、健全筛查、调度

- **模板** $\tau\in\mathcal T$：typed constrained query shape（FROM 形状、子查询种类、相关深度、谓词算子多重集、类型/可空约束、数据状态约束 T/F/NULL 三分区非空、兼容 oracle 集）。实例化 $I(\tau)\to t$ 由确定性编译器完成，静态检查作用域/类型/聚合层级/oracle 前提。初始 $\mathcal T_0$ 来自：历史回归种子的规则变异（现有 `seeds/`）、类型化随机、以及 bootstrap 阶段 LLM 生成的少量模板。
- **筛查**：每个 $t$ 过 $\mathrm{Cond}$，再过全部兼容健全 oracle；命中即算 $\sigma$。**先签名、再判重、再决定是否触发任何 LLM 事件**（关闭 A12）。
- **调度**：arm = 模板。reward 在 **每次实例化** 记账（关闭 A14/A15）：
  $$r(\tau)=\mathbb 1[\text{new family}]+\lambda\cdot\text{novel}(t)-c\cdot\text{cost}(t)$$
  其中 `new family` 仅指 **健全 oracle 命中且 $\sigma$ 未出现过**（关闭 A1）；重复实例 reward 为 0；`novel(t)` 按 §3.5 逐 testcase 归因（关闭 A16）。因为家族会被"挖尽"，reward 非平稳，采用 **滑动窗口 UCB**（窗口 $W$ 个实例化），保证见 §5 P4。$\mathcal T$ 随 L1 输出增长：新 arm 以先验 (tries=1, reward=1) 进入竞争，不保送。

### 3.4 耦合算子（共同进化的定义）

- **Fixer→Hunter（DCE）**：新家族 $F$ 产生 L0 诊断后，Hunter 立即获得三个确定性邻域 arm（不需要 LLM）：(a) 同 $S_1$ 规则、变谓词算子多重集；(b) 同 $\pi$ 子树、变 FROM 形状；(c) 同 reproducer、沿 $\nu$ 相邻版本的差分。L1 通过后再加入 $\tau_F$。DCE 的可检验预测：**邻域 arm 的新家族产出率高于全局 arm**，且 **shuffled diagnosis（把 $F$ 的诊断随机配给另一个家族的邻域）不产生同等增益**。
- **Hunter→Fixer（OHR）**：对 L2 补丁 $P_F$，Hunter 在补丁构建上于 $\tau_F$ 与邻域 arm 内做预算为 $B_h$ 的搜索。新健全失败 ⇒ 补丁不完整或引入回归，反例进入 $F$ 的 reproducer 集合，Fixer 重试。度量 **survival rate**（$B_h$ 次实例化内零新失败的补丁比例）和 **held-out generalization**（独立 seed 找到的同族实例是否也被修好）。这是 CoHarden "Rigorous vs Lax" 的无标签版本：判"Lax"的是健全 oracle，不是 mutation patch。
- **课程**：Fixer 处理顺序按家族的 $\nu$（回归优先于历史遗留）、reproducer 大小、L1 是否通过排序；同 $S_1$ 的家族共享检索上下文。

### 3.5 结构化 coverage（替代集合大小，A7）

三张独立的计数表，每个 $t$ 单独产出 novel cells：

1. 规范化物理计划的 **深度-2 子树**（算子名 + 子算子名多重集），QPG 的"唯一计划"是其粗化；
2. oracle × query shape × 相关深度 × 谓词算子 × 类型/可空性 的语义格子；
3. 数据状态：T/F/NULL 三分区各自非空、join fanout 级别、重复多重度。

报告 **coverage AUC**（对有效执行数积分），而不是终点集合大小。DCE 必须在 (1) 上被控制（H3）：如果 DCE 的增益能被计划子树覆盖完全解释，那它只是昂贵的 QPG。

### 3.6 事件驱动 LLM 协议（从属于机制，非主张）

允许的事件与上限（每 campaign）：`bootstrap_templates` ≤4；`new_family_L1` 每家族 1；`stagnation`（最近 2,000 次有效执行无新结构 cell 且 5,000 次无新家族）≤8；`repair_L2` 每家族 ≤3 次 agent 会话；预留 ≤10。禁止：逐轮生成 schema/query/rewrite；任何 verdict；cross-engine/version divergence 的裁定（关闭 A10/A11）；重复家族；`no_llm`/random/QPG 对照的任何调用。guideline 由账本统计确定性生成（关闭 A9）。目标：≤5 calls / 10,000 valid executions，作为 §8 Gate 0/1 的门槛，不作为论文主张。

### 3.7 循环

```text
Archive_B ← ∅ (families), Archive_R ← ∅ (diagnoses/patches), T ← T0
loop until budget exhausted:
  τ ← SW-UCB.select(T)                       # 按实例化记账
  t ← I(τ);  if ¬Cond(t): skip; continue
  for o in compatible(τ): if o(t)=fail: h ← (t,o); break
  if no h: SW-UCB.update(τ, r=λ·novel(t)−c); continue
  t* ← ddmin(h);  s ← σ(t*)                   # L0，确定性
  if s ∈ Archive_B: update(τ, r=λ·novel−c); Archive_B[s].instances += t*; continue
  Archive_B[s] ← new family F;  update(τ, r=1+λ·novel−c)
  T ← T ∪ DCE_L0(F)                            # 三个确定性邻域 arm
  emit event new_family_L1(F) → τ_F;  if re-hit(τ_F) ≥ 1/50: T ← T ∪ {τ_F}
  if L2 available: P_F ← repair(F); if reproducer∧regression: OHR(P_F, B_h) → survival / new reproducers
```

不变量：Archive_B 单调（只增；重分类保留证据链）；每个 family 有可执行 reproducer、oracle 输出、$\sigma$、20/20 重放记录；LLM 从不写入 verdict 字段；每次 LLM 调用有账本条目。

## 4. 假设与预注册预测

| 假设 | 预测 | 否定它的观察 |
|---|---|---|
| **H1 DCE 发现增益** | 等预算下 full（DCE+L1）每 10k 有效执行的新家族数 > coverage-only（QPG 式）> random，配对 bootstrap 95% CI 下界 > 0 | CI 含 0，或 shuffled-diagnosis 达到同等增益 |
| **H2 L0 足够** | 仅 L0 确定性邻域（无 LLM）已获得 H1 大部分增益；L1 增益为增量 | L0-only ≈ random 而 L1 显著 → 贡献在 LLM 模板而非诊断 |
| **H3 不是 QPG 的替身** | 控制计划子树覆盖 AUC 后，DCE 的家族增益仍显著 | 增益随 AUC 匹配消失 |
| **H4 OHR 减少过拟合** | 加固补丁的 held-out 同族实例修复率与 survival 高于仅 reproducer+regression 验证的补丁 | 无差异，或加固后回归套件失败率上升 |
| **H5 健全性** | 报告 bug 的人工复核 FP = 0（sound kinds）；divergence 不计数 | 任一 sound 报告被复核为非 bug → 检查 Cond 漏洞 |
| **H6 签名有效** | 人工抽样 50 个家族对，$\sigma$ 的合并/分裂错误 ≤10%，且显著优于关键词签名 | 错误率 >10% |

## 5. 保证（条件性，标注来源）

- **P1 报告健全性（引用）**：在 $\mathrm{Cond}$ 下 TLP 恒等式 $Q\equiv Q_p\cup Q_{\neg p}\cup Q_{p\ \mathrm{IS\ NULL}}$ 与 NoREC 计数恒等式对任意确定性 $Q,p$ 成立（Rigger & Su 2020）；PlanVariant 的健全性来自"同一查询在同一引擎、不同规则集下必须同结果"。因此 sound kinds 的 FP 率按构造为 0；不满足 $\mathrm{Cond}$ 的用例被排除而不是被报告。
- **P2 verdict 与 LLM 隔离（构造）**：verdict 字段只由 oracle 输出与重放结果写入；LLM 输出只进入 `hypothesis`/`template`/`patch` 字段。可用静态检查（类型系统或测试）证明没有从 LLM 返回值到 verdict 的数据流。
- **P3 reproducer 1-minimality（标准）**：ddmin 对给定删除算子集给出 1-minimal 结果（Zeller & Hildebrandt 2002）；$\Delta^*$ 同理。
- **P4 调度 regret（标准，带条件）**：固定 arm 集、平稳 reward 下 UCB1 regret $O(\sqrt{KT\log T})$；本方案 reward 分段平稳且 arm 动态增加，采用滑动窗口 UCB，regret 界为分段平稳情形的已知结果（Garivier & Moulines 2011），我们只主张"相对每段最优 arm 的次线性 regret"，不主张全局最优。
- **P5 签名确定性（构造）**：$\sigma$ 是 $(t^*,\text{version})$ 的确定性函数；去重结果可由第三方重算。
- **P6 归档单调与可审计（构造）**：Archive_B 只增；每条记录含可执行脚本、oracle 输出、$\sigma$、重放日志、账本引用。
- **P7 预算可核算（构造）**：所有 LLM 调用经唯一 `LLMClient` 与 append-only 账本；`summary` 由账本重算；`NullLLMClient` 在构造期禁网。

P1/P3/P4 为引用或标准结果，P2/P5/P6/P7 为需要用测试守住的构造性质；H1–H6 为经验主张。

## 6. 实验协议

### 6.1 对照臂

| 臂 | LLM | 反馈 | 作用 |
|---|---|---|---|
| A. seed-only | 0 | 无 | 零 LLM 下界 |
| B. typed-random templates | 0 | 无 | 模板 DSL 本身的产出 |
| C. coverage-only（QPG 式：计划子树新颖度驯化） | 0 | 覆盖 | H3 的控制 |
| D. L0-DCE（确定性邻域 arm） | 0 | 诊断 L0 | H2 |
| E. LLM templates, no feedback（bootstrap 后冻结） | bootstrap | 无 | LLM 模板质量本身 |
| F. full：E + L0 + L1 | 事件 | 诊断 L0+L1 | 主方法 |
| G. shuffled-diagnosis | 事件 | 错配诊断 | **负对照**：排除"多一个 arm 就多产出" |
| H. SQLancer TLP/NoREC；I. Argus（可跑部分） | — | — | 外部基线 |
| J. 旧版逐轮 LLM（当前代码） | 逐轮 | 混合 | 历史对比，仅报告 |

repair 侧（L2 可用时）：K. reproducer+regression 验证 vs L. K + OHR 加固；在 held-out 同族实例与上游回归套件上度量。

### 6.2 预算匹配与统计

三个预算分别冻结并 **同时报告**：有效执行数 $B_{exec}$、LLM token $B_{tok}$、墙钟 $B_{wall}$。每臂在其适用预算耗尽时停止；跨臂比较以 $B_{exec}$ 为主轴，$B_{tok}$ 作为效率轴，不互相折算。每臂 ≥10 个配对随机种子；pilot 每 seed 10k 有效执行，通过 Gate 2 后主实验 100k。所有臂结束后统一按 $\sigma$ 跨臂去重，再计数。统计：配对 bootstrap 95% CI、median/IQR、时间到第 k 个家族的生存曲线。

### 6.3 指标

主指标：**每 10k 有效执行的唯一验证家族数**。次级：time-to-k-th family；结构 coverage AUC（三表分别）；calls、tokens / family；DCE 邻域 arm 与全局 arm 的产出率比；补丁 validation / survival / held-out 修复率；人工复核 FP（sound 报告目标 0）；签名合并/分裂错误率。

### 6.4 版本梯作为免费的 Gate 1 基准

pip 提供 DuckDB 0.9–1.5.5 全部版本（本机已有 1.0.0 与 1.5.2）。种子语料 `test/issues/` 中每条回归测试都对应一个"在某版本修复"的历史 bug：**易损版本应触发健全 oracle，修复版本不应**。这给出无需编译的 recall/precision 基准，同时是 $\nu$ 的校准集。Gate 1 要求：≥80% 的可实例化历史 bug 在易损版本上被 oracle 命中，修复版本上 FP = 0，缩减保持每个命中。

### 6.5 外部有效性

按时间切分：训练/调参只用 ≤1.5.2 的 bug 与语料；held-out 用 1.5.3–1.5.5 新修复 bug 与 PostgreSQL；禁止检索 held-out 的 issue/patch。开发者确认单独报告，不与内部验证混计。

## 7. 主张边界（写进论文的诚实条款）

- 迄今 2 个 DuckDB 家族由健全 oracle 发现，其中首个出现在反馈生效前；PostgreSQL 0；共同进化因果作用 **尚无证据**（A1/A4/A18）。
- 若 H1 成立而 H2 显示 L0 已占主要增益，贡献表述为"确定性诊断驯化的搜索"，LLM 为可选模板源。
- 若 D ≈ C ≈ B，则发布结论为"在成熟引擎上诊断/覆盖引导相对类型化随机无显著增益"的负结果，并撤回 co-evolution 主张。
- 若 L2 在预算内不可行，论文只主张 L0/L1 与 DCE；OHR 作为 future work 并给出接口。
- 不把 divergence、重复实例、非确定性用例计入任何数字。

## 8. 实施路线与门槛

### Phase 0 — 测量完整性（约 2 天）

`llm/client.py`（持久 client、任务级 `max_tokens`、结构化 JSON）、`llm/ledger.py`（append-only）、`NullLLMClient`；`summary.json` 从账本重算；run manifest 记录 code/config/prompt/corpus/引擎版本哈希。
**Gate 0**：`no_llm` 真实调用 = 0；账本 = 事件日志 = summary；固定 seed 重跑账本哈希一致。

### Phase 1 — L0 Fixer 与签名（约 4 天）

`diagnosis/rule_bisect.py`（$S_1$、ddmin $\Delta^*$，DuckDB `disabled_optimizers` 与 PG GUC 两个后端）、`diagnosis/plan_diff.py`（$\pi$ 规范化）、`diagnosis/version_ladder.py`（$\nu$，多 venv 子进程）、`oracles/models.py::root_signature` 替换为 $\sigma$；`co_evolution.py` 改为"签名→判重→事件"顺序；删除 cross-engine/version 的 LLM 裁定；实现 sound promotion。
**Gate 1**：§6.4 版本梯 recall ≥80%、修复版 FP = 0；两个已知家族的 $\sigma$ 稳定且互异；50 对人工抽样签名错误 ≤10%。

### Phase 2 — 模板 DSL、结构 coverage、DCE（约 5 天）

`generation/template_schema.py`、`generation/compiler.py`、`coverage/structural.py`（三表 + AUC）、`evolution/bandit.py` 改滑动窗口 UCB 并按实例化记账、`evolution/dce.py`（L0 邻域 arm、L1 事件、re-hit 验证、shuffled 模式）。
**Gate 2（pilot）**：臂 B/C/D/F/G 各 10 seeds × 10k 有效执行；F 相对 C 的家族增益 CI 下界 > 0 且 G 无同等增益，才进入主实验；否则按 §7 收缩主张。

### Phase 3 — L2 修复与 OHR（约 1–2 周，可并行、可裁掉）

`repair/worktree.py`（源码 checkout、增量构建、agent 会话、补丁提取）、`repair/harden.py`（在补丁构建上运行 $\tau_F$ 邻域搜索，产出 survival 与新 reproducer）。先在版本梯已知 bug 上做"金补丁存在"的可行性验证。
**Gate 3**：≥5 个历史 bug 上 agent 补丁通过 reproducer+regression；OHR 至少一次揪出 Lax 补丁。

### Phase 4 — 主实验与写作（约 2–3 周）

全部臂 100k 有效执行 × 10 seeds；held-out 版本与 PostgreSQL；冻结 `evaluation/protocol.yml` 与 manifest 后再看结果。

## 9. 明确不做

- 不让 LLM 判定 bug、等价性、或"哪个版本/引擎正确"；
- 不把 divergence、重复实例、性能差异计为 bug；
- 不在 Gate 0/1 通过前跑更大 campaign；
- 不以"更多 agent"或"更多 prompt 工程"作为改进手段；
- 不把 L2 源码修复当作论文的完成条件；
- 不修改旧 `results/`，新实验写入带 protocol id 的目录。

最短正确路线：**先把 verdict 路径彻底与 LLM 隔离、把根因签名换成可重放的诊断（Phase 0–1），再用配对实验与负对照检验"诊断定义的邻域是否真的更快找到新家族"（Phase 2）**。共同进化是否成立，由 Gate 2 决定，而不是由架构图决定。

## 10. 实施状态与初步实证结果（2026-09-14）

### 10.1 已落地（Phase 0–2 的核心机制）

- **LLM 账本**（`llm/ledger.py`）：进程级 append-only，记录每次调用的 event/prompt-hash/token/状态；`no_llm` 为硬门（`disable_llm` → 零网络，账本记 blocked）。80 迭代 full run：417 调用 / 75 万 token，事件分型 `generate_db/queries/rewrites/guidelines/triage_equiv/root_cause` 全部入账。
- **σ 签名**（`diagnosis/`）：干预基二分（DuckDB 33 条 `disabled_optimizers`，PG GUC 基）→ fix_kind(single/minimal_set/non_optimizer/**pervasive**) + fix_set + plan_diff + ν 版本梯。家族身份 = fix_set（非空时）；宽 fix_set（>max(6, basis/4)）判定为归因失败 → `pervasive`，身份回退 plan_diff+ν——这是实证发现修正的：**回避≠归因**，宽面上关任何规则都"碰巧绕开" bug。
- **健全判定分层**：`true_bug` 仅经 TLP/NoREC/plan-variant/crash 或 sound promotion 可达；equiv/cross-engine/differential 无目标本地健全证据时记 `unverified_bug`/`*_divergence`。
- **DCE**（`evolution/dce.py`）：算子交换/子查询形态/NULL/合取删除/类型替换/数据扰动，逐探针过健全 oracle + 确定性门；命中先经"父 fix_set 快速成员测试"（1 次执行 vs ~120 次全二分），新 σ 入队前沿递归，全局预算封顶。
- **脚本**：`scripts/corpus_hunt.py`（语料×变异→oracle→Fixer→σ→DCE 全管线，零 LLM）、`scripts/verify_bugs.py`（独立复核，`--repeat N` 分 stable/flaky/false_positive）、`scripts/collapse_families.py`（旧 run 的 σ 重收缩）。

### 10.2 初步结果

**DuckDB 1.5.2（当前版）确认家族**：

| σ | 机理 | 证据 |
|---|---|---|
| `f4bdb204` | deliminator+filter_pushdown：相关 EXISTS 自连接 `=`+`<>`+`>` 丢行 | 稳定，32 独立用例，longstanding |
| `a7e5c9f7` | window_self_join：`PARTITION BY a,a`（重复分区键）与 `PARTITION BY a,b` 共存时分区键被错误合并，`SUM OVER` 结果错 | **稳定回归**：1.0.0 正确 `(30,30)`、1.5.2 错 `(10,20)`，已手工确认 |
| `ab630f7b` | non_optimizer：NOT EXISTS/NOT IN + NULL/DOUBLE 混合，default 返回 12 行、全关优化返回 8 行（语义上与 deliminator 面疑同根因） | 稳定，longstanding |
| `59bb4007`/`487d9a5c` | pervasive：CTE 内 `ORDER BY .. OFFSET` + boolean join，no_join_order 变体 ~1/60 概率丢行 | **flaky 错结果类**：16+7 条稳定复现 + 124 条概率复现——计划敏感/疑似并行调度相关的真实现象 |

**版本梯回挖（`--repeat 3` 独立复核）**：

| 版本 | 家族数（收缩后） | 本版复核 | 1.5.2 复核 |
|---|---|---|---|
| 1.0.0 mutations ×2 seeds | 5（两 seed 完全一致） | 10/10 stable | 全部已修 |
| 1.0.0 原始 seed 直扫 | 收缩后 ~182（窄集粒度受限） | 40 stable + 259 flaky | — |
| 1.1.3 mutations + 原始直扫 | 12 | 9 stable + 3 flaky | 全部已修 |

→ 跨版本 σ key 稳定一致（同根因在不同版本复得同 key）；所有版本梯发现的家族均为真实历史 bug 且已在 1.5.2 修复——框架在"已知 bug 重发现"上 recall 表现正常，且 ν/复核正确区分了 live vs fixed。

**总计**：跨版本去重后 **~15 个独立根因家族**（当前版 live 4–5），**>250 条独立复现用例**归档。

**PostgreSQL 16**：308 seeds × 结构变异 → 0 oracle 命中（2,680 变异中 788 可执行；成熟引擎+方言不兼容占主因）。

### 10.3 对主张的初步裁决

- **bug 有结构（核心 bet）**：正面证据强——同一 σ 家族的 DCE 邻域持续命中（deliminator 家族 31 探针 19 命中；`exists→not_exists` 一步即达旧系统靠运气撞到的家族）。
- **诊断引导 vs 盲搜**：定性成立——`--include-original` 直扫 + DCE 在 15 分钟内暴露了一个 1.5.2 稳定回归，而 821 次 LLM 调用的旧系统从未找到。
- **flaky 类是真实的新发现**：pervasive 家族证明"计划敏感型概率错结果"存在且可被 plan-variant oracle 捕获；σ 的 `pervasive` 降级正确处理了它（归因失败 ≠ 不是 bug）。
- **尚缺的**：10–30 个**当前版**独立根因家族未达成（当前 4–5 个）；PG 侧零命中；L2 补丁+足迹探针未实现；Gate 1/2 的配对统计实验未跑。

### 10.4 新 oracle 类：优化器信念审计（2026-09-23，已落地并过召回门槛）

**机制**：优化器每次逻辑改写都依赖一个自己推导的语义事实（"该列不会为 NULL""内层每外行至多匹配一行""该窗口函数单调""被裁分区无满足行""cache key 完备")。这些信念是对所有满足 schema/query 数据的**全称断言**——在具体数据上找到一个反例行即证明推导不健全，**无需参照结果、无需第二次执行**。这是继 relation 类（TLP/NoREC/DQP，比输出）与 absolute 类（PQS/TQS/锚点，比期望值）之后的第三类 oracle：审计**中间语义断言**，按 RIPR 在感染时刻而非传播后观测。理论先例：CGO'20 Taneja/Regehr 对编译器数据流事实的 soundness 审计；DBMS 方向未检到同类（CERT 查基数估计是性能向，DQP/Kangaroo 仍比输出）。

**落地**（约 400 行，全部过验证）：
- `oracles/pg_plan_beliefs.py`：8 种 EXPLAIN 可见信念的通用检测器——`absent_qual`（谓词被删）、`inlined_expr`（函数体被内联）、`run_condition`、`inner_unique`、`oj_reduced`（含 left/right 对换）、`partition_pruned`、`memoize`、`group_key_reduced`(FD)。
- `seeds/pg_belief_cases.py`：15 例语料 = 6 recall + 1 known_live + 6 对照 + 3 selftest；每条信念带反例 audit SQL（返回违例行，空=信念成立）。
- `scripts/pg_belief_audit.py`：版本梯驱动，JSON 报告。

**版本梯结果（9 个 assert build，全部 verdict 正确）**：

| build | #19412 | #19533 | SAOP×2 | memoize（活 bug) |
|---|---|---|---|---|
| 15.19 | — | — | — | wrong_result |
| 16.0 | — | false_belief | false_belief | wrong_result |
| 16.2 | — | false_belief | false_belief | wrong_result |
| 16.6 | — | — | false_belief | wrong_result |
| 17.0 | false_belief | false_belief | false_belief | wrong_result |
| 17.11 | — | — | — | wrong_result |
| 18.0 | false_belief | false_belief | false_belief | wrong_result |
| 18.6 / 20dev | — | — | — | wrong_result |

`false_belief` 精确落在各 bug 生命窗口内（修复版一律 `not_asserted`),**9 build × 15 case 零误报**；对照臂全 `holds`;selftest 臂证明审计器非空（捏造的信念未被断言、反例照常找到）。`b19533_runcond_latent` 是 RIPR 证据点：18.0 上结果碰巧对（任何输出 oracle 判 pass)，信念审计仍报 `false_belief`。

**Corpus sweep（通用发现模式）**：`oracles/pg_plan_beliefs.discover_beliefs` 不依赖用例 spec，直接对任意计划收割被断言信念，并为可机械化的类自动生成反例 audit(inner_unique：内层无 dedup 节点时查基表键重；partition_pruned：把被扫兄弟的 Filter 重定向到被裁子表；oj_reduced 简单情形：检验 null-extended 行上合取可 TRUE)。`scripts/pg_belief_sweep.py` 跑 pg18+pg 语料 439 条 × 3 build(18.0/18.6/20devel):**全部可审计信念成立，零 false_belief**；断言普查：inner_unique 16–17、oj_reduced 5、partition_pruned 3、memoize 2、group_key 1——语料对 run_condition/memoize 几乎无覆盖（诚实的覆盖缺口记录）。踩坑修正：Inner Unique 断言的是**子计划输出**唯一性，内层含 HashAggregate/Unique 时查基表键重是 audit 过强（已修）；分区审计不可混用不同 alias 的兄弟 Filter（已修）。

**与诊断签名的接口**：`false_belief` 自带根因定位——每种信念对应一个具体 planner 函数（`reduce_outer_joins`/`innerrel_is_unique`/run-condition support/`is_strict_saop`)，可直接作为 σ 的 `fix_kind` 维度。

**诚实边界**：只覆盖"优化器基于错误前提做改写"类（18.1–18.6 wrong-result 修复中约 4–5/11)；EXPLAIN 只暴露部分信念（nullingrels 决策、EC 推理等需插桩 build);audit SQL 目前按用例手写，自动生成是 Step 2（主动证伪+生成器）的工程量。报告：`results/belief_audit/run_b|run_ladder/belief_audit.json`。

### 10.5 Step 2 落地：信念导向生成器 `pg_belief_gen.py`（2026-09-23)

8 个生成器，每个产出 `(setup, query, plan-断言谓词, python 独立求值, expected)`——**审计不借引擎**（避免审计查询被同一 bug 优化掉）:runcond（真实 Run Condition 文本解析 × frame 序单调性）、ojr/ojr2（两表/嵌套 LEFT JOIN strictness 边界，13+5 qual 模板）、lat(#19412 族：NOT NULL/CHECK 冗余下推）、inline(STRICT 契约 ×SAOP 体）、iu（唯一性证明 8 variant)、part（边界/跨类型/join-time 传播裁剪）、memoize（共享参数 cache-key,A==C 是已知活 bug 前置条件）。

**实证矩阵（最终建模修正后，零误报）**:

| build | 规模 | hits |
|---|---|---|
| master(20devel) | 800+400+250+350 | **仅 memoize A==C**(=已知活 bug Brazeal/#17213-ext，全版未修，生成器泛化到 8+ 列组合，全部 wrong sum) |
| pg186 | 300+200+200 | 同上 |
| pg180 | 300+250+200+200 | lat/notnull(#19412 族）、inline 空数组（SAOP)、runcond count(#19533 族）、memoize A==C——**全部已知族，全部 wrong_result** |

过程中修掉 4 个审计建模错误（非引擎 bug):eval_window 处理序双重置换、UNION ALL 每 join 行重复发射、`= ALL` 语义方向、memoize key-col 分组过强（param identity ≠ column identity)。

**对"有没有新 bug"的诚实回答**：生成器自动复现了全部 4 个已知 bug 族（含 1 个未修的），并把活 bug 的形状边界系统展开；master 上非 memoize 的 ~500 个被断言信念**全部成立**——在该信念空间的已探部分无新 bug。剩余 frontier:EXPLAIN 不可见的信念（nullingrels/EC 内部，需插桩）、eager-agg/SJE 等 20devel 新代码的 belief、更大种子×更宽数据域。报告：`results/belief_gen/*/belief_gen.json`。

### 10.6 新 oracle 类：执行器驱动模式不变量 `pg_execdiff.py`(2026-09-23，已落地）

**不变量**：同一逻辑查询经不同的执行器驱动/物化/重入路径，必须产出相同结果。与信念审计正交——目标不是优化器前提而是执行器状态机（suspend/resume、tuplestore 回读、rescan、EPQ、ModifyTable 投影）。

**驱动集（10 个）**:direct / spill(work_mem=64kB 落盘）/ parallel（强制 Gather)/ cur1(FETCH 1 逐行挂起恢复）/ scroll(SCROLL 游标 + 随机 FETCH/MOVE 定位，含位置断言）/ prep(PREPARE generic×custom 计划缓存）/ ctas(DestReceiver 路径）/ hold(SCROLL WITH HOLD 跨提交 tuplestore)/ spt（子事务回滚 + 目录变更穿越挂起游标）/ epq（第二连接并发更新——EPQ 重估）。

**族集（20 个）**:latunion/corrsub/reccte/srf/aggspill/sortspill/win/nestloop/multicte/setop/memo（已知 #17213-ext)/partjoin/latpart/fdwlat(postgres_fdw 环回 async-Append 重扫，enable_memoize=off)/incsort/bitmapor/jsontbl/epq(12 op)/merge(3 mode)/dmlcte(4 kind)。

**校准出的执行器语义（模型纪律：所有"差异"先证伪自身模型再算引擎）**:
- 游标位置模型 cur∈{-1..n}:FORWARD 恰好取到末行停在其上，越界才到 n;BACKWARD ALL 永远落 -1;RELATIVE 0 在边界返回空。
- EPQ 按**存储槽位**重估 (a_new, b_orig):同一 b 元组按 TID 重取，多重性保持，重连失败则 null 扩展。
- TM_SelfModified（同事务更新）在 FOR UPDATE 扫描中按已删除处理——nodeLockRows.c 注释的 Halloween 防护。
- 跨分区移动 = 跨 rel 的 delete+insert:FOR UPDATE 合法报 SerializationFailure("moved to another partition")——静默错数据才是 bug。
- REPEATABLE READ 下续读命中并发更新必须报 could-not-serialize——静默返回旧值才是 anomaly。

**guctoggle 驱动（第 11 个）**：每 case 随机抽 3 个 `enable_*` 布尔 GUC + 40% 概率 1 个 JIT 臂（`jit_above_cost=0` / `jit_inline_above_cost=0` / `jit_tuple_deform=on`),`SET LOCAL` 后 bag 必须等于 direct。覆盖 26 个开关：join 算法、memoize、material、sort/hashagg、incremental_sort、partition_pruning、partitionwise join/agg、async/parallel append、gathermerge、tidscan、presorted_aggregate、self_join_elimination、distinct_reordering、eager_aggregate、group_by_reordering、parallel_hash。未识别 GUC（老版本）按 skip 处理，其余错误照常上报。

**实证**:master/186/180 各 200 例 ×20 族 + ASan 120 例——除已知 memoize 活 bug 外**全绿，零 ERR,ASan 无断言崩溃**。EPQ 12 op(plus100/negdrop/del/rekey/rekey2/skip/bmod/bkey/chain/pmove/pdel/ownupd)×多版本全部符合模型。过程中修掉 7 个 harness/模型错误：scroll 边界语义（3 处）、MERGE UPDATE 保留非目标列、EPQ per-slot 重估（非全量重扫）、ownupd 的 TM_SelfModified 语义、FOR UPDATE OF nullable-side 非法、**`conn.rollback()` 在 autocommit 连接上是 no-op 导致死事务泄漏污染后续驱动**（教训记入 AGENTS.md)。

**诚实判定**：该 oracle 在 PG 执行器状态机上仍未发现新 bug——执行器比优化器更难出 wrong-result（状态机被回归测试和现实世界 workload 双重淬炼）。价值:(a) 首个系统化 cursor/EPQ/驱动模式/GUC-toggle fuzzer，覆盖 SQLsmith/SQLancer 原理性盲区；(b) 12-op EPQ 闭式模型本身可复用为其他 oracle 的组件；(c) 过程产出 7 条可写进论文的语义校准记录。

**剩余 frontier（期望产出排序）**:
1. **EXPLAIN 不可见信念的插桩**——给 pgmaster 打信念日志（nullingrels 决策、EC 推导内部状态），是覆盖面最大的原理性盲区
2. **更深的事务/并发交叉**——SSI write-skew(pg_txn_fuzz 已覆盖两条上游调度）、DDL 与长事务交错
3. 更大 seed × 更宽数据域（现 pool 小）

### 10.7 新 oracle 类：扳手对差分 `pg_leverdiff.py`（2026-09-24，已落地并过召回门槛）

**动机**：四路 agent 审计（覆盖审计/上游机制谱/文献谱/仓库盘点）一致指出——2023-26 的 ~40 个上游 wrong-result bug 集中于 nullability/qual 安置簿记（~15）与空子树消除簿记（2025-26 仍在出），且每个上游 repro 天然是"只差一个语义保持扳手"的成对查询。旧 harness 一个扳手都没生成过。

**不变量**：仅差一个语义保持扳手的成对查询必须同结果（pair-differential，自定位）；有闭式的再对绝对期望（防双臂同错）。

**扳手集**：`WHERE false`/`LIMIT 0`/`1=2`/`k IS NULL AND k IS NOT NULL` 四种扫描空写法 + `SELECT const WHERE false`（RTE_RESULT 常量空，不同 planner 路径）；CTE 内联↔`MATERIALIZED`/`NOT MATERIALIZED`；`x=c`↔`x IS NOT DISTINCT FROM c`（NOT NULL 列上可证等价，EC 簿记不同）；`ON false`↔无连接；`JOIN ON true`↔`CROSS JOIN`；UNIQUE 证明下的 join↔IN-半连接↔comma+DISTINCT；自连接消除；GROUP BY 空扩展多重性；LATERAL UNION ALL 冗余 `IS NOT NULL` 下推（#19412 机制泛化）。

**族集（16 个）**:res_hoist(#19553 常量泄漏形状）/empty_oj/qual_survive/rjc(#19560 可移除连接+EC 限制）/lat_notnull(#19412)/full_empty(#19579)/ojr_lever/clonequal/cte_mat/isnd_lever/uniq_in/sje/grp_null/nest_onfalse(missing-quals 形状）/scalar_empty(SELECT-list 空子树）/empty_union（空 UNION ALL 臂）。

**实证矩阵**:

| build | 规模 | 结果 |
|---|---|---|
| pg180_assert(18.0) | 800×2 | `res_hoist`+`lat_notnull` **全 fire**——#19553 常量泄漏（仅 const-empty 臂偏离，scanned-empty 全对）与 #19412 NULL 泄漏精确复现 |
| pg186_assert(18.6) | 3200×2 | **全绿**——两修复均已 backpatch |
| pgmaster_assert(20devel) | 3200×2+3000 | **全绿** |
| pgmaster_asan | 800 | 全绿无崩溃 |

**召回判定**：两个已知 bug 的 fire 窗口精确收敛于 pg180（修复前），修复版零误报——oracle 判别力达 release 粒度。

**过程中修掉的建模错误**(n+1 条：join-then-lateral 多重性——LEFT JOIN 先按匹配复制 t1,LATERAL 对每个 joined 行都发射一次外层列）。

**诚实判定**：master 上 ~5400 扳手对全部满足不变量——该矿脉在已生成形态内无新 bug。与上游 bug 的差距在形状组合的特异性（PHV 包裹点、多级嵌套位置）而非覆盖量。剩余扩展：相关空子查询、3 级连接巢、空子树在 FULL JOIN 内位/聚合下方。

### 10.8 两个新 oracle：GROUPING SETS 差分 `pg_gsdiff.py` + 全谱 TLP `pg_tlp.py`（2026-09-24)

**gsdiff 不变量**:`GROUPING SETS` ≡ UNION ALL 逐组 GROUP BY ≡ CUBE/ROLLUP ≡ 集合重排；`GROUPING()` 位掩码逐行绝对校验；`GROUP BY DISTINCT` 去重集合列表为独立臂。7 族 × Python 闭式求值。

**TLP 不变量**：确定性谓词 p 对任意行恰一真值于 {p, ¬p, p IS NULL}——三分区 UNION ALL 必须等于未分区基查询。6 位点：WHERE / inner-JOIN 输出 / **LEFT JOIN 输出（null 扩展行须落入 p-IS-NULL 臂，missing-quals 形状）** / HAVING / 标量聚集输入 / 集合并集输出。谓词池 14+7 个含 NULL 传播、coalesce、IN、模运算。

**实证矩阵**:

| oracle | master | pg186 | pg180 | asan |
|---|---|---|---|---|
| gsdiff | 2000 全绿 | 2000 全绿 | **8 DIFF**(cube×4/expr×2/flag×1/having×1) | 700 全绿 |
| tlp | 2500+96 全绿 | 2500+96 全绿 | 2500+96 全绿 | 800 全绿 |

**gsdiff 的 8 个 fire 裁决：全部归属已知 bug #19078**(2025-10-18 backpatch REL_18_STABLE,18.0→18.1 窗口）。机制=hashed GROUPING SETS 内部 hash 表迭代器重置错乱——官方报告为崩溃，我们的生成器机械产出**同机制的 wrong-result 实例**:MixedAggregate 计划下 NULL 组合键组在多集合间被错误迭代（行丢失/重复、集合列表顺序相关）。证据链：仅 MixedAggregate 下 fire、fire 行全是 (NULL,NULL,*) 键、版本窗口精确吻合。**覆盖价值：该 oracle 自动复现了一个"崩溃报告实为 wrong-result"的 bug——证明崩溃签名与结果签名同根。**

**过程中修掉的模型错误**(gsdiff 2 条）:SQL `sum(c)` 全 NULL 组返回 NULL 非 0;UNION 臂裸 NULL 字面量解析为 text 须显式 `NULL::int`。

**诚实判定**：三 oracle(leverdiff/gsdiff/tlp）合计 ~17000 变体在 master 全绿。已知 bug 的复现窗口全部精确收敛到修复前版本——oracle 判别力可信；master 上该三个机制空间暂无新信号。

### 10.9 三个新 oracle：非确定 collation `pg_coldiff.py` + DML 一致性 `pg_dqe.py` + 分区 FK/DDL `pg_fkddl.py`(2026-09-24)

**coldiff 不变量**(ICU `deterministic=false` collation,'a'='A' 为真但字节不同):同 collation HAVING 下推、set-op 去重、DISTINCT/GROUP BY、join key、DISTINCT ON、INTERSECT 的等值语义必须一致。表面代表行实现相关→按 `ndkey`(case-fold）投影比较。**pg186_icu 672 例 + pgmaster_icu 1500 例全绿**（模型修正 5 处：GROUP BY 在非确定 collation 下每 nd-key 只出首见代表、NULL 组、零匹配空结果、`count(DISTINCT)` 忽略 NULL、INTERSECT vs IN 的 NULL 语义差异）。

**dqe 不变量**(DML/SELECT qual 一致性）：同谓词的 `SELECT`/`DELETE..RETURNING`/`UPDATE..RETURNING`/ctid-delete 必须命中同行集。master 100 例全绿。

**fkddl 不变量**(ATTACH/DETACH 上 FK 触发器簿记）:4 族——`fkddl`(attaching 分区验证）、`pref`（被引用表本身分区）、`pref_sub`（被 attach 的分区自身分区）、`selfref`（分区表自引用 FK)。探针：attach 须按现有行验证、attach 后违例 insert 必败、**detach 后 detached 分区保留 FK 克隆→被引用侧 DELETE/UPDATE 仍须失败（half-backed-FK 探针）**。

**fkddl 实证矩阵**(600 例/build × 7 build):

| build | 结果 |
|---|---|
| pg160 / pg170 | **93 DIFF**(pref 17 + pref_sub 13 + selfref 63)——全部同一签名 |
| pg1519 / pg166 / pg180 / pg186 / master / master_icu / 186_icu | 全绿 |

**裁决：全部归属已知 bug**——detached 分区引用侧强制保留、被引用侧 action 触发器缺失（karst 2023-04 报告 + Álvaro 2024 秋的侵入式修复系列：detach 时须为被引用表每个分区建 pg_constraint+trigger)。我们的三个族是该修复的**精确存活窗口**(16.0/17.0 fire;15.19/16.6+/18.x/master 干净）和三个表现面的机械复现——含修复线程中 Álvaro 自承"未测"的角落（selfref、detached 分区自身分区）,master 上该修复对三者都完备。

**诚实判定**:fkddl 是目前唯一在多版本上稳定咬到真实完整性违例的 oracle，但咬到的是已修复 bug 窗口。master 上分区 DDL×FK 空间暂无新信号。
