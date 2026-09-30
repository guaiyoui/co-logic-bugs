# PostgreSQL 零产出：诊断、七方辩论与行动方案
## （兼评估：CoevoDB novelty vs SIGMOD/VLDB bar）

> 日期：2026-09-17。方法：本人复核代码/构建配置 + 7 个独立 agent 分立场调研
> （attack-surface / 历史挖掘 / oracle 怀疑 / 经济怀疑 / 能力审计 /
> mine-the-fixes / 并发恢复），本文是辩论的裁决与合成。
>
> **先说结论**：(1) novelty 本身够 bar，缺的是统计证据强度与修复侧闭环；
> (2) PG=0 的主要成因不是"PG 太干净"——而是**我们从未真正测过 PG 的大部分
> bug 面**：所有 PG 测试跑在裁剪构建上（无 contrib/ICU/LLVM/XML/压缩），
> PG 自带的两大 bug 工具（isolation tester、pg_regress）**早已装在每个
> prefix 里却从未运行**，crash-recovery/复制/18-new 特性面零覆盖。

---

## Part I — Novelty 评估

### 1.1 2026 年的赛道密度

LLM×DBMS 测试已经拥挤：Argus（SIGMOD'26，CAQ 等价对 + prover，
41 bugs/5 引擎）、SmartFuzz（OOPSLA'26，文档特征→种子）、FuzzySQL
（64 vulns 含 PG）、MIST（MCTS 测试生成）、SQLaser（clause-guided）——
加上经典栈 SQLancer（TLP/NoREC/PQS/CERT）、QPG、TQS/DSG、SQLRight。
**"LLM 生成 SQL 找 bug"本身不再有新意**，差异点必须在机制或证据上。

### 1.2 CoevoDB 真实的、别人没有的卖点

| # | 卖点 | 独家性论证 |
|---|---|---|
| N1 | **诊断条件化探索（DCE）**：确定性 σ（干预二分 fix_set + plan_diff + 版本梯 ν）定义下一步搜索邻域 | 此前反馈只有 coverage（QPG）与 metamorphic oracle；"根因签名→搜索邻域"的闭环无人做过 |
| N2 | **σ 作为 bug 身份**：可重放的根因级去重（不是文本签名/LLM 判定） | TQS 用结构等价类；干预二分签名是新的，且天然携带定位信息 |
| N3 | **Playbook**：跨 run/跨引擎规则库 + `inspired_by` 因果归因 | LLM-agent 论文普遍无法展示因果链；warm/frozen 配对消融（32 vs 18 族）是罕见的机制级证据 |
| N4 | **健全性纪律**：LLM 永不写 verdict；sound oracle only；tiered 计数（unspecified/user-setting/debug-setting 单列） | 对抗"LLM 幻觉当 bug"类工作的直接差异点；审稿人最爱攻击的软肋已自堵 |
| N5 | **OHR**（oracle-hardened repair）：健全 oracle 无标签加固补丁 | 反方向（发现→修复）——目前只有 repair_bench R1/R2 小证据，**未闭环** |

### 1.3 与 bar 的差距（证据，不是想法）

- **H1 只有方向性证据**：Gate2 n=5 seeds、CI 重叠、full 在 1.1.3 双模态
  {0,8,0,1,0}。SIGMOD 审稿标配追问：n≥10、配对 bootstrap CI 分离、
  更长预算下优势是否增长（coevo 的核心预测）。
- **"co-evolution"目前主要是单向**：诊断→发现（DCE/playbook）有证据；
  发现→修复（L2 patch + OHR）基本没跑。标题叫 co-evolution 就两个方向都要。
- **bug 口径**：headline 21 族里保守可辩护 ~13-14；gen-2 收割的 19 族中
  11 个同属 window_combine debug-forced 一个机理簇。主表必须用保守数 +
  附表分层（REVIEW 已指出，方向正确）。
- **PG=0** 是证据弱点但不是致命的——见 Part II/III。
- **dev-confirmed 尚少**：F1/F7 main 复现是自查；upstream issue 材料备齐
  但未提交（UPSTREAM_ISSUES.md）。SIGMOD 发现类论文里 dev confirmation
  是最强货币，趁 F1/F7 仍活在 main 应尽快报。

### 1.4 判定

**概念新颖性：够 bar**（N1+N2+N3 组合在文献中没有先例）。
**证据强度：暂不够**。最短路径不是再找 bug，而是：
(a) Gate2/curve 扩到 n≥10 seeds × 20 iter 钉死 H1；
(b) L2/OHR 至少在一个家族上闭环（哪怕 SQLite 源补丁）；
(c) dev-confirmed 数量上去；
(d) PG 按 Part III 方案补——找到则锦上添花，找不到则升级为
"全 oracle 类 + 全构建配置下的 validated negative"，同样是结果。

---

## Part II — 为什么 PG 是 0：三层诊断（均已实测）

### L1 构建层：所有 PG 测试跑在"阉割版"上（新发现，最关键）

| 构建 | configure | 缺什么 |
|---|---|---|
| pgserver 16.2 | `--without-readline --without-icu` | 无 contrib（只 plpgsql+vector）、**无 llvmjit.so（JIT 不存在）**、无 ICU |
| pgbld/pg*_assert ×6 | `--enable-cassert --enable-debug --without-readline --without-zlib --without-icu --without-libxml` | 无 contrib、无 ICU、无 LLVM、**无 zlib/lz4/zstd（TOAST/WAL 压缩全关）**、无 XML |

后果：**之前所有 "jit forced"、"wal_compression" 轴全是空转**；
ICU 非确定性排序、XMLTABLE、压缩 TOAST、contrib 索引 AM
（GIN/GiST/Bloom 全家）从未被执行过一条 SQL。
而 `~/miniconda3` base env 里 icu/zlib/zstd/lz4/libxml2/uuid/openssl
的**头文件和 pkg-config 全在**——rebuild 只要 ~15min/版本（实测
configure≈23s/make≈65s/install≈4s per version）。

### L2 Harness 表达层：oracle 栈的表达范围有洞（C 的逐行审计）

- **语句词汇表的天花板**：管线里 LLM 只产 `{select_from, predicate}`
  （`bug_hunter.py:33-45,497-568`）；SeedMutator 只产 SELECT 骨架
  （`seeds/mutate.py:184-444`）。`seeds/parse.py:16-27,158-163` 在导入
  回归测试时**静默丢弃** COPY、事务控制、LISTEN/NOTIFY、PREPARE/EXECUTE、
  `CREATE EXTENSION/FUNCTION/TRIGGER`、GRANT、VACUUM/CHECKPOINT/REINDEX、
  ALTER SYSTEM——一条夹在 DDL 之间的 UPDATE 直接丢失，所有"序列中间
  的变更序"信息没了。
- `pg_txn_fuzz` op 池 = SELECT/pair-read/UPDATE/DELETE/INSERT/
  SAVEPOINT/SET-ISO/COMMIT，3 张小表、只有 PK 无 FK：**没有 FOR
  UPDATE/NO KEY UPDATE/FOR SHARE/SKIP LOCKED（EPQ 是 PG 历史最密
  bug 区）、没有 ON CONFLICT/MERGE/FK 级联/DDL 并发/NOTIFY/
  advisory/2PC**；且 rendezvous 调度禁止真并发——deadlock 只能
  在已持有锁间形成（400 trials→deadlocks:0）；**读历史不入账**
  （只 replay 写）——读了非串行可达状态但不写的事务不可见。
- crash-recovery：`stop("immediate")`+`start()` 基础设施已就绪
  （`pg_local_server.py:163`），但没有调用者；`pg_terminate_backend`
  杀 backend ≠ postmaster 崩溃恢复。
- **cancel 后状态从不探测**：runner 真 cancel（`postgres_runner.py:
  168-175`），但所有 oracle 把 timeout 记 inconclusive 后丢弃——
  cancel-during-CIC/sort-spill/commit 类 bug 结构性不可见。
- **确定性闸门过度过滤**（`oracles/determinism.py`）：`NOW()`/
  `current_date`/uuid/random 等词法整类禁掉——而非用专门不变量去测
  （CHECK 约束里的 `now()`、DEFAULT、trigger、statement_ vs
  transaction_timestamp 不变量从未被专项测试）；
  `json_objectagg/json_arrayagg` 不在 `_ORDER_AGG`——这正是 pg_idx3
  那 2 个 FP 的来源。
- **bag 归一化的假阴性风险**（`oracles/normalize.py:17-79`）：
  行序丢弃 + 列名/类型不比较（`SELECT *` 列映射错位且值恰好相同的
  bug 不可见）；float/Decimal→round(6) 掩掉亚精度错；DATE==TIMESTAMP
  midnight；±NaN 合并；json 对象键序被 sort_keys 消掉。
- initdb 从不变化（locale/encoding/checksum 全默认）；
  `max_prepared_transactions`/`wal_level` 默认值直接禁掉 2PC 和
  逻辑解码。
- recall harness 6 例**全是 planner 单会话 SELECT**——"validated
  negative"目前只验证了 ~1/8 个 bug 类轴。

### L3 目标成熟度：真实的部分

PG 16.2 是被 SQLancer/SQLsmith/regress 打得最透的引擎；
~800k execs（D 的完整账目：133k index + 255k assert hunt +
222k coevo + 160k warm + 50k misc）在**已测面上**零产出是真实信号；
SQLancer 在同版本 10.15M checks 也是 0。但 L1/L2 说明"已测面"只是
PG 的子集——成熟的是核心单会话结果等价面，不是整个引擎。

**旁证（sqlancer/docs/PAPERS.md:102 原文）**："PostgreSQL and SQLite
are DBMSs that we comprehensively tested... We believe these two systems
to be the most challenging test targets. Finding bugs that the
approaches implemented in SQLancer overlooked in these systems might
thus best demonstrate a new approach's effectiveness."——两条含义：
(a) 我们的 SQLite 3 族已经满足 SQLancer 自设的"最难目标"判据；
(b) D 低估了 PG bug 的论文价值：按 SQLancer 自己的说法，一个
SQLancer 漏掉的 PG bug 是证明方法有效性的最强单一证据。

**诊断归因（两方独立判断）**：C 估 maturity ~60% / expressivity ~30% /
build ~10%；我倾向 maturity ~40% / build ~35% / expressivity ~25%——
分歧在"裁剪构建缺失的面有多大历史 bug 密度"。无论哪方，
**"管道在已测面不漏" ≠ "管道覆盖了 PG 的 bug 面"** 是共识。

---

## Part III — 七方辩论

### 立场与核心证据

**A. Attack-surface specialist**（"测错了面"派）
- PG18-new 面从未进过语料：virtual generated columns（18 默认开启，
  我们所有 gen-col 种子都写 STORED=物理路径）、WITHOUT OVERLAPS
  时态约束、NOT ENFORCED、OLD/NEW in RETURNING、
  `enable_self_join_elimination`、`enable_distinct_reordering`、
  nbtree skip-scan、`io_method=worker/sync`、MERGE NMBS（只在 16.2 上
  当过语法错误路径，17/18 上从没语义执行过）。
- 18.x release notes 里修复仍集中在这些从未被测的代码上
  （virtual gencol 已 4+ fix、self-join elim 已有 1 fix）。

**B. 历史证据派**（"谁找到了 PG bug / 在什么构建上"调查）
- **谁找到的**：Lakhin（最大外部报告者）几乎全部打在 **master/devel
  assert 构建**上（#18550 对 17beta2、#18608 对 master），方法=SQLsmith
  系+定制 fuzz+gdb/sleep 竞态复现；Seltenreich 同款；SQLancer 的
  `PostgresBugs.java` **只有 1 个 flag**（bug18643），且生成器按
  oracle 可用性裁剪面（CONCURRENTLY 被禁、window 曾无 oracle 不生成）
  ——**oracle 有无决定覆盖有无**，与我们的结构同构。
- **推论**：我们最新的 build 是 18.6=post-fix 快照；Lakhin 类战果
  要复刻需 **master/devel assert build** + 17.0/18.0-era 中间版
  （多个 bug 的窗口在我们的 build 之间是空的）。
- **权威 release-note 计数**（`<listitem>` per `<sect1>`）：16.x
  小版本合计 302（16.1=59/16.2=70/16.3=57/16.4=54/16.5=54/16.6=8），
  17.x 小版本 545，18.x 小版本 370——与 F 的 ~1,150 总量互洽。
- 修正一处过强的说法：harness 并非"严格单会话"——dml_hunt 有
  MERGE/partitioned DML/8 GUC 组，txn_fuzz 有 2-3 会话+串行化回放，
  ddl_stress 有兄弟连接并发 DDL+CIC；窄在**没跑上游 spec 瞄准的
  具体交错**（merge-join.spec 的 EPQ+强制 join 方法组合），不是
  没有多会话。
- 新增 verbatim 复现（并入下方 B′ 库）：#18550 前继承父跨分区
  UPDATE→assert（fires pg160/162）、#18468 CREATE STATISTICS+drop
  col+LIKE INCLUDING ALL→crash、**GiST IOS range_ops 误解码**
  （REL_14–18 全回移，core-only 单会话，fires 所有 build）、
  **PL/pgSQL scrollable cursor on simple SELECT**→"unexpected plan
  node type"（REL_13–17 回移，fires 全部 16.x+17.11 若 pre-17.5）、
  range-DEFAULT 多键剪枝（REL_14 回移，fires 所有 build）、
  MVNDistinct 忽 nulling bit（fires 16.x）。
- 提议 recall schema 加 `requires` 字段（icu/contrib:<name>/
  sessions:2/restart）——被挡 case 报 **skipped 而非 absent**，
  recall 集同时变成能力审计表。

**B′. Recall-库审计派**（逐条核对 release notes + regress 源文件后返回，
产出最实干的增量；完整表在 `results/PG_RECALL_V2_LIBRARY.md`）
- **34 个已确认 correctness bug 库**，29 个可表达为单会话 case，
  **20 个在 pg166 上可触发**（pg166 目前零覆盖——晚于 2024-11-15 且
  back-patch 到 REL_16 的 fix 全在其上存活）。
- **发现一个阻断性 harness 缺陷**：`classify()` 从不查
  `result.is_internal_error`——assert 崩溃到达 client 时表现为
  `server closed the connection`，被归 `other_error`→FAIL。
  即现有 6 例之外的所有 crash 类 recall 对当前 runner **不可见**。
  一个 `error_or_crash` 模式（<30 行）解锁全部。
- **免费增益**：`is_affected` 本就支持多 branch 区间——把 fixed
  branch 列进 `affected` 可让 15.19/17.11/18.6 从"observe"变
  "expected_clean"，检查数翻倍零成本。
- 多会话 bug（MERGE EPQ、lock-chain 等 5 个）有 commit 确认但需
  双会话 harness——**禁止降格成单会话**（单会话跑不出≠干净）。

**C. Oracle 怀疑派**（"表达力不够"派，逐行审计完成）
- 判词："0 PG bugs" is real but narrow——已测面约 500k execs 的阴性
  可信，但那只是确定性单会话结果等价这一个切片。
- 独家发现（别人没提的）：**oracle 本身的假阴性面**——bag 归一化
  丢列名/列类型（SELECT * 列错位不可见）、亚精度被 round(6) 掩掉、
  `json_objectagg` 不在 order-agg 名单（已产 FP）；确定性闸门把
  `NOW()`/uuid 整类禁掉而非专项测；txn_fuzz 只 replay 写不 replay
  读；cancel 后状态不探测；initdb 从不变化 locale/checksum；
  `CREATE EXTENSION` 被 parse.py 丢弃（所以就算 contrib 装上了，
  种子导入器也会把它扔掉——**这是两个独立缺陷的复合**）。
- 归因判断：maturity ~60% / expressivity ~30% / build ~10%——
  比 A/E 更悲观，但承认已测面里 oracle 还有自伤性盲区。

**D. 经济学怀疑派**（"minimal PG"派）
- 账：PG 已烧 ~800k execs >其他引擎总和；0 yield → 95% 上界
  ~0.004 fam/1k vs DuckDB 实测 0.26–6.11。
- 一个侥幸低危 PG bug 对论文的边际价值 < recall 集扩大
  （6→25，把 0 变成"多面 validated negative"）+ funnel 数据
  （350 个 PG 候选全被裁决驳回=精度证据，白捡）。
- **判词：minimal PG**——停止大规模撒网，3 天做 recall 扩展 +
  coverage 矩阵 + funnel 图；保留唯一例外=contrib build（成本 5 分钟，
  唯一真·fuzz 史短的面）。PG 的长期角色 = quorum 参照引擎。
- 翻转条件：contrib 一扫就中 / 出现"我们 oracle 本该抓到"的已发表复现 /
  审稿人明确要求。

**E. 能力审计派**（两路独立审计，结论一致）
- contrib 源码 56+ 模块完好，PGXS 在位，`make -C contrib/<m> install`
  直接可用，**~20 分钟全部 6 个 prefix**；
  STATUS §4.1 "contrib 不可达"的结论已过时（那是 pgserver 的限制）。
- **pg_regress 和 isolationtester+pg_isolation_regress 已安装在全部
  6 个 prefix**（pgxs 自带），~110-124 个 spec 文件在树里——
  从未跑过。TAP 只差 `perl-ipc-run`（conda 5 分钟）。
- crash-recovery / 逻辑复制（pgoutput+libpqwalreceiver 已装）/
  物理复制（pg_basebackup/pg_rewind/pg_verifybackup 全在 bin/）/
  **pg_upgrade 跨版本链**——全部今天可用，只差 conf 追加
  （wal_level=logical）和小胶水代码。
- 唯一真 blocked：JIT 需 `conda install llvmdev`（~30min）；sepgsql
  需 selinux；master 需 snapshot tarball（autoconf 仍在，~30min）。

**F. Mine-the-fixes 派**（"挖修复邻域"派；两路独立调研收敛）
- release-*.sgml 是机器可解析的 fix 库：**总计 ~1,150 条 fix items**
  （16.x 302、17.x ~500、18.x ~370），每条带 Branch+commit hash；
  按 correctness 过滤后 ~300-400 条，聚成 ~30 个 vein。
- **聚簇实测**：MERGE+跨分区 DML ~20 条横跨 3 majors；
  PHV/varnullingrels ~16 条（同一机理修 7+ 次）；
  partition pruning/partitionwise ~19；domain/gencol/ON CONFLICT ~15；
  SQL/JSON+JSON_TABLE ~11；Memoize 6；EC/join-removal ~7……
  PG 自己的 note 承认残余兄弟（"All versions have a similar hazard…"、
  "other cases may exist"）。
- **regress-diff 技巧实测有效**：16.0→16.6 的 regress diff 直接给出
  8 个带 `bug #NNNNN` 注释的新测试（#18522/#18550/#18465/#18576/
  #18652/#18657/#18568/#18468）+ 4 个新 isolation specs
  （merge-join.spec 等）——每个 fix 自带 reproducer，可机械提取。
- **版本窗口套利已验证（本人复核）**：join-removal `d610d8e8b`、
  LATERAL-UNION-ALL `ec20a4552`、MERGE `e7391bbf1/3611794af`、
  SQL/JSON `485527190` 的 commit 均出现在 17.11/18.6 的 notes 而
  **不在 16.6 自己的 notes 里** → pg166_assert 仍携带 ≥5 个
  已写入文档的 bug = 保证的 recall 级命中（tier-R）；同机理的
  未修兄弟 = 新 bug 候选（tier-N）。
- 完整协议：`pg_fixmine.py`（SGML→fix_db+vulnerable-set）、
  fix→playbook rule 蒸馏（复用 `evolution/playbook.py` Rule schema）、
  oracle 路由表、top-10 vein + probe 样例、3-tier 执行
  （vulnerable/fixed/forward——"fixed 层命中=疑似活 bug"是免费的
  判别式）。
- **成本核算（修正后）**：~25 LLM calls / ~50k tokens + ~6-30k execs；
  预期：tier-R ≥8/10 veins 在 vulnerable build 复现（recall 集 6→20+），
  tier-N ~6-12 个裁决候选 → 1-4 个真实族。
- 诚实边界：若所有邻域都修干净了 → 0 新族，但 recall 集照样扩大、
  "PG 干净"从盲猜升级为"对 N 个已验证邻域实测干净"。

**G. 并发/恢复专家**（"真 bug 在并发/恢复/复制"派）
- crash-recovery oracle 设计就绪：committed-state=serial replay
  （复用现有 journal 模型）+ index-vs-heap 等价（复用 cic 验证器）+
  orphaned relfilenode 检查 + sequence 回归 + **`wal_consistency_
  checking='all'`**（assert-only GUC，guc_tables.c:4907 实测在——
  WAL vs page 不一致直接 PANIC，自带 assert oracle 从没人开过）。
  fsync=off 不影响进程 kill 后的 WAL replay 正确性。
- isolation 走 Option A：直接驱动已装的 isolationtester binary
  （stdin 喂 spec，diff `expected/*.out`）；**删掉所有 permutation
  行 → 自动跑全部交错**——上游只 curate 了部分交错，全排列空间
  是历史 bug 的藏身地。~0.5 天。
- txn_fuzz 扩 op 池 top-4：FOR UPDATE 系列/EPQ、ON CONFLICT
  （speculative insertion，含**明确未修的 partition 变体**）、MERGE、
  **SSI summarization churn——有一个 2026 年仍活的 write-skew bug
  （SummarizeOldestCommittedSxact 后仍能提交）在 17.10/18.4/master
  上可复现，pg186_assert 很可能还带**——现有 serial oracle 直接能抓。
- 排序：crash-recovery 先建（oracle 已写好且自验证过），
  isolation runner 紧随其后，op 池第三，逻辑复制最后（最重）。

### 辩论的共识与残余分歧

**共识（≥5/7 派别独立得出）**：
1. 之前"PG 测得很透了"的叙事不成立——build gap + expressivity gap
   是事实，不是借口。
2. 最便宜的高价值动作几乎都不需要新代码：contrib make install、
   跑已装的 isolation specs、`stop('immediate')` 恢复循环、
   加 3 个 GUC 轴（self_join_elimination/distinct_reordering/
   io_method）、18-new 种子包。
3. "挖修复"是结构性占优的：tier-R 命中是保底收益（recall 证据），
   tier-N 是上行收益（新 bug）——期望值下有保底，上不封顶。
4. fsync=off 的 crash 测试是合法的（sync commit 仍写 WAL 到 page
   cache；torn-page 类才是它排除的，那本来就需要 dm-flakey）。

**分歧**：
- **投入上限**：D 主张 2-3 天封顶；A/F/G 主张一轮"解锁+定向"
  （~1 周）。裁决：采用**阶段门控**——每一步有可观察的
  kill/continue 信号（见 Part IV），不是一锤子买卖。
- **PG bug 对论文值多少**：D 认为边际近零；A/F 认为一个
  default-path PG bug 对"成熟引擎可攻性"主张是决定性的。
  **SQLancer 自己的判据（PAPERS.md:102）站在 A/F 一边**：
  "找到 SQLancer 漏掉的 PG/SQLite bug 最能证明新方法有效性"。
  裁决：取决于 Story A/B 的选择——若走 B（discovery paper），
  5/5 引擎显著强于 4/5 且 PG 命中自带"打败了最难目标"叙事；
  若走 A，PG 只做 recall/validation 角色，D 的 minimal 路线成立。

---

## Part IV — 行动方案（按性价比排序，带 kill criteria）

### Phase P0 — 解锁已有资产（~1 天，零风险）

| # | 动作 | 成本 | 产出 |
|---|---|---|---|
| P0.1 | `make -C contrib/<m> install`（16.2/16.6/17.11/18.6 四树）：amcheck、pageinspect、pg_visibility、pg_walinspect、pg_surgery、pg_trgm、intarray、hstore、ltree、cube、seg、bloom、btree_gin、btree_gist、citext、isn、tablefunc、fuzzystrmatch、unaccent、dict_*、intagg、earthdistance、tsm_*、dblink、postgres_fdw、file_fdw、test_decoding、pg_stat_statements | ~20min 机器 | contrib 面 + amcheck 腐败 oracle |
| P0.2 | 跑全部 ~124 个 isolation specs（pg_isolation_regress，pg186/16.2 两 prefix）+ `make check` 级 pg_regress 基线 | ~2h | assert build 端到端 vs 上游 oracle 验证；spec 语料入库 |
| P0.3 | spec 变异：删 permutation 行 → 全交错；步骤 shuffle/splice/SAVEPOINT 注入；internal-error+hang+server-death 作 oracle | ~0.5d 代码 | 全交错空间（上游未 curate 区） |
| P0.4 | POSTGRES_VARIANTS 补轴：`enable_self_join_elimination=off`、`enable_distinct_reordering=off`、`enable_presorted_aggregate=off`、`io_method=worker`、`debug_parallel_query=regress` | ~30 行 | plan-variant 在 18.6 上恢复真实干预面 |
| P0.5 | `conda install perl-ipc-run` → TAP 可用（复制/恢复/PITR 测试栈） | ~10min | 后续 P3/P4 的前置 |
| P0.6 | **oracle 自伤盲区修补**（B/C 的发现，全是小改）：(a) `classify()` 加 `error_or_crash` 模式查 `is_internal_error`——**阻断性**，否则所有 crash 类 recall 不可见；(b) timeout 后加一次 cancel-后一致性探测（不复核=丢弃）；(c) bag 归一化加 strict 模式（比列名+类型）；(d) `json_objectagg/json_arrayagg` 补进 `_ORDER_AGG`；(e) `parse.py` 放行 `CREATE EXTENSION`（否则 P0.1 白装）；(f) initdb 变体：`--data-checksums`、非默认 locale | ~0.5d | 关掉已测面上的假阴性通道——否则"覆盖证据"站不住 |

**Gate P0 → P1**：isolation 全交错变异跑出任何 assert/internal-error
候选 → 直接进 triage；全清则继续（说明 spec 面干净，值一条覆盖证据）。

### Phase P1 — PG18-new + mine-the-fixes（~3-4 天，最高期望产出）

| # | 动作 | 依据 |
|---|---|---|
| P1.1 | `scripts/pg_fixmine.py`：解析 3 个 release-*.sgml → fix_db.jsonl + 每 build vulnerable-set（fix 不在该 build 自己的 notes 里=仍带 bug） | F 的协议；~150 行 |
| P1.2 | recall 集 v2：B 已备好 ~29 个新 case（绝大部分 verbatim + 已核实 affected 窗口 + expected_rows 写法——见 `results/PG_RECALL_V2_LIBRARY.md`），~20 个在 pg166 上应触发；recall 集 6→~35，pg166 覆盖 0→20+，横跨 executor/DML/datatype/JSON/Memoize/分区 | 保底收益；每个 case 先在 fixed build 取真实期望行再上 buggy build |
| P1.3 | fix→rule 蒸馏 → `playbook_seed_pg_fixmine.jsonl` → `coevo_warm --engine postgres`（已验证的 warm 路径）+ DCE 扩邻域 | tier-N 上行 |
| P1.4 | `seeds/pg18_targets.py`：virtual gencol（默认路径！）、WITHOUT OVERLAPS、NOT ENFORCED、OLD/NEW RETURNING、MERGE NMBS、self-join elim、skip-scan、grouping-RTE pullup | A 的第一优先面 |
| P1.5 | F10 类比：window `RANGE/GROUPS` 非数值 offset/infinity、反向 frame（`1 PRECEDING..5 PRECEDING`）、ordered-set aggs——现有 oracle 直用 | A: F10-analog |

**Gate P1 → P2**：tier-R 命中 ≥5（recall 扩大即成功）；
tier-N 出 ≥1 confirmed 新族 → 加投一轮 mine-the-fixes；
tier-N=0 且 tier-R 全中 → PG 转"validated negative"叙事，
剩余预算转 D 路线（Gate2 扩样 + chdb 第六引擎 + DF 深扫）。

### Phase P2 — crash-recovery + 并发 op 池（~2-3 天）

| # | 动作 | 依据 |
|---|---|---|
| P2.1 | `pg_local_server.py` 加 `extra_opts`（~10 行）；`initdb -k` 选项（data checksums）；`log_tail()` 刮 local_pg.log 找 TRAP/PANIC | G 的 gap 表 |
| P2.2 | `scripts/pg_crash_fuzz.py`：journaled 负载 → `stop('immediate')` → `start()` → serial-replay + index-vs-heap + relfilenode 孤儿 + sequence 回归 + `wal_consistency_checking='all'`；kill 点按 rendezvous 确定性调度 | G Extension 1 |
| P2.3 | txn_fuzz op 池 + FOR UPDATE/NO KEY UPDATE/FOR SHARE（EPQ）、ON CONFLICT、MERGE、**SSI churn**（长 SIREAD 持有者 + 短事务群，`max_pred_locks_per_transaction` 调低——冲着那个 2026 仍活的 write-skew bug 去） | G Extension 3 |
| P2.4 | contrib 面接入语料：pg_trgm `%`/GIN、intarray query_int、bloom 假阳性方向（假阴性=bug）、postgres_fdw 自环差分（外表结果必须=本地 bag——白送的 oracle） | A/E |

### Phase P3 — 功能构建解锁（~0.5-1 天，网络依赖）

- `pg162_full`/`pg186_full` rebuild：`--with-icu --with-zlib --with-zstd
  --with-lz4 --with-libxml --with-ssl=openssl`（PKG_CONFIG_PATH 指
  ~/miniconda3/lib/pkgconfig）→ ICU-vs-libc/provider 内排序差分、
  非确定性排序 + UNIQUE/hashagg、XMLTABLE-vs-xpath 等价、
  lz4-vs-pglz TOAST 等价 + wal_compression 崩溃恢复组合。
- `conda install llvmdev` → `pg186_jit`：jit=on/off 成为真轴
  （之前是空转）。
- **master/19devel snapshot tarball build**（autoconf 仍在）：
  Lakhin 式战果基本全在 devel；上游上报价值最高。
- **中间小版本补窗**（B 的观察：多个 bug 窗口落在现有 build 的
  缝隙里）——16.7/17.0/17.5/18.0 assert 各 ~15min，让
  version-ladder 从"端点采样"变"窗口分辨"，也给 recall 集的
  `affected` 区间提供实测边界。

### Phase P4 — 复制（~1.5-2 天，仅当 P1/P2 有产出或要冲"全 oracle 类覆盖"）

- 逻辑复制：两 LocalPgServer，`wal_level=logical`+pgoutput；
  oracle=pub↔sub bag 等价（行过滤/列清单/`publish_via_partition_
  root`/TRUNCATE/流式进行中事务/subscriber crash 恢复）。
- 物理：pg_basebackup→standby→promotion/timeline；17+ 增量备份
  `pg_walsummary`+`pg_combinebackup`+`pg_verifybackup`。
- pg_upgrade 16.0→16.6→17.11→18.6 数据目录链 + 全表 checksum。

### 决策树总结

```
P0 解锁 ──> P1 mine-the-fixes + 18-new ─┬─ 新族 ≥1 → P2 全量 + 论文加 "PG 也中" 
                                        ├─ tier-R 中、tier-N 0 → P2 限量(2天) → 仍0 → 转 D 路线
                                        └─ tier-R 都不中 → harness 有问题，先修 harness
P2 crash/并发 ──> P3 功能构建 ──> (P4 可选)
任意点产出 confirmed PG 族 → 立即做 σ 归因 + 版本窗 + 上游确认
```

**预算纪律**：每 Phase 结束更新 exec 计数；PG 总追加预算上限 ~2 周
机器时 / ~300 LLM calls。超过即按 D 路线封存（recall 扩展 +
coverage 矩阵 + funnel 图写进论文）。

---

## Part V — 如果最后还是 0：怎么写进论文

1. **Validated-negative 一节**：coverage 矩阵（oracle 类 × 面 ×
   build 配置 × execs）+ recall 集（≥20 例跨 planner/executor/
   concurrency/recovery，标注每个在哪版命中哪版 clean）+
   adjudication funnel（350+ 候选全驳回=精度证据）。
   这是文献里少见的"工具被证明能抓、目标确实干净"的负结果。
2. **PG 的正当角色**：≥3 引擎 quorum 的参照系（C1 修复后的
   跨引擎裁决靠它）+ version-ladder 方言基线。
3. **写作边界**：不再说"PG 不可达/contrib 测不了"（已过时）；
   要说"在包含 contrib/JIT/ICU/恢复/复制/并发规格的完整构建与
   oracle 集下，预算 B 内 PG 产出 0，recall 集命中率 100%"——
   无法被"你测了吗"攻击的零。

## 附：本次辩论各 agent 的方法学备忘

- 每个 agent 独立调研、持固定立场、可互相矛盾；本文裁决已标注
  证据强弱。两路独立能力审计（E）结论一致，可信度高。
- 所有"已验证"级事实均可复核：config.status:435（configure flags）、
  pg_local_server.py:135-139（GUC 行）、163（immediate stop）、
  guc_tables.c:4907（wal_consistency_checking）、
  pgbld/*/lib/postgresql/pgxs/src/test/isolation/（二进制在位）、
  release-*.sgml（fix 库）。
