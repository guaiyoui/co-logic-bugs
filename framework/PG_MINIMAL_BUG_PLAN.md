# PG 找 Bug 最小方案（现有 harness 上，不动架构）

> 2026-09-17。原则：**只补检测漏洞 + 喂已备好的语料**，不建新系统。
> 全部素材已在 `results/PG_RECALL_V2_LIBRARY.md`（~43 个带 commit/窗口/
> verbatim 位置的已知 bug）和 debate 文档里备齐。

## Step 0 — 一个 ~30 行的阻断修复（前提，先做）

`scripts/pg_recall_check.py` 的 `classify()` 从不查
`result.is_internal_error`——assert 崩溃到达 client 时只显示
`server closed the connection`，全被归 `other_error`→FAIL。
加 `error_or_crash` 模式：`fired iff is_internal_error or buggy_error in error`。
**不做这步，库里一半以上的 case（crash 类）对 runner 不可见。**

顺手在同一处加 `requires` 字段（`contrib:<name>`/`sessions:2`/`restart`），
被挡 case 报 skipped 而非 absent。

## Step 1 — recall v2 数据录入（保底命中，~半天纯录入）

按置信度从库文件挑 ~20 个 verbatim case 编码进 `seeds/pg_recall.py`：

| 批次 | case | 类型 |
|---|---|---|
| 单 SELECT 零 setup | A24 LATERAL-UNION-ALL（期望 `[[1,1],[1,1],[2,2]]` 已核实）、A29 union 容器哈希、A32 range-DEFAULT 剪枝、A11 numeric clamp、A8 date_bin | wrong_rows |
| error_substr | A25 FULL JOIN #19460、A31 junk-ctid、A17 MERGE DO NOTHING、A14 AlternativeSubPlan、A23 winref、A19 agg div0 | 特征错误串 |
| crash（需 Step0） | A35 #18550 继承父跨分区 UPDATE、A36 #18468 STATISTICS+LIKE、A2/A21 MERGE/触发器 | assert |
| 全版本可触发 | A37 GiST IOS range_ops（core-only）、A38 plpgsql scroll cursor（一行 DO）、A39 range-DEFAULT 多键 | wrong_rows/error |

`affected` 照库里的多 branch 区间写（如 `{15:(0,18),16:(0,6),17:(0,10),18:(0,5)}`）
→ 15.19/17.11/18.6 自动从 observe 变 expected_clean。
`expected_rows` 必须写归一化后的值；大精度 numeric 用 `<>` 比较不断言原值。

**产出**：recall 6→~35，pg166 覆盖 0→20+。
**红利**：任何 case 在 pg1711/pg186 上触发 = "修了没修干净"的活 bug
候选 → 直接 triage，不要等。

## Step 2 — 白捡的上游 oracle（零代码，~2h）

`pg_isolation_regress` 全部 ~124 个 spec × {pg162_assert, pg186_assert}；
`make check` 级 regress 基线。二进制早已装在每个 prefix。
任何 diff/assert/hang = **上游自己的 oracle 判定的异常**，直接进 triage。
有产出再考虑 spec 变异（删 permutation 行跑全交错）；没产出也是一条
"spec 面干净"的覆盖证据。

## Step 3 — contrib 解锁（~30min 机器 + ~10 行 parse.py）

`make -C contrib/{ltree,btree_gist,pg_trgm,intarray,hstore,amcheck,pageinspect,pg_visibility,bloom} install`（pg166+pg186 两树）；
`seeds/parse.py` 放行 `CREATE EXTENSION`（否则白装）。

- pg166+contrib：库里 contrib verbatim（ltree 深路径、`btree_gist`
  NaN/varlena `<>`）的 fix 全晚于 16.6 → **保底 recall 命中**
- pg186+contrib：同 case 若仍触发 = 活 bug；不触发则 contrib 面变成
  可 fuzz 的新语料（现有 SELECT oracle 直接可用）

## Step 4 — 兄弟变异（真·新 bug 引擎，~1 天，复用现有管线）

这是唯一系统性产**新** bug 的步骤。对每个 verbatim repro 做机械变异
（不用 LLM 也能先跑一轮）：

- 常数扰动：NULL/边界值/重复值/`-0.0`
- 分区边界换位、child 列重排（A21 的 tupdesc 机理）
- join 方法 × 现有 8 组 plan-variant GUC
- EXCLUDE frame 换档、MERGE WHEN 子句组合枚举、opclass 换族

**双跑 pg166 + pg186 判别**：
- 仅 166 中 = recall（预期，不计新发现）
- **186 也中 = fix 邻域残余 = 新 bug 候选**
- 1711+186 同中 = 跨版本活 bug，最强证据

可选叠加：fix 描述蒸馏成 playbook rules 喂 `coevo_warm --engine postgres`
（已验证的 warm 路径），让 DCE 沿 σ 扩邻域。

## Step 5 — crash-recovery 最小版（~半天，唯一值得加的新模式）

`LocalPgServer` 已支持 `stop('immediate')`+`start()`，只差调用者：
journaled DML（复用 txn_fuzz 的 journal）→ immediate stop → start →
serial-replay 校验 + `wal_consistency_checking='all'`（assert-only GUC，
自带 PANIC oracle）+ amcheck。恢复类在 PG 历史上 bug 密度不低、
我们零覆盖。

## 不做（=大改，明确推迟）

复制拓扑、ICU/JIT 重建、双会话 spec 语义引擎、pg_upgrade 链。
可选便宜项：master/17.0/18.0 assert build 各 ~15-30min——给 Step4 的
`affected` 窗口定边界用，有空窗就顺手建。

## 预期与诚实边界

| 步骤 | 命中预期 | 中的什么 |
|---|---|---|
| 0+1 | 几乎必中 | **已知 bug**——oracle 验证+负结果升级，不算新发现 |
| 2 | 低-中 | spec diff = 上游判定的异常 |
| 3 | 保底 recall + 低概率新 | contrib 面是 fuzz 史最短的 |
| 4 | **1-4 个真实族**（F 派估计，不保证） | fix 邻域残余/不完整修复 |
| 5 | 0-2 | 恢复类首版 oracle |

**Gate**：Step4 跑完仍 0 新族 → 按 debate 文档 D 路线封存 PG
（recall 扩展 + coverage 矩阵写进论文），预算转 Gate2 扩样。
总预算上限 ~3 天机器时，到线即停。
