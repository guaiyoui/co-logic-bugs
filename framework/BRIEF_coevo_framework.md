# 任务：把 co_evolution_db_testing 通用化为双引擎框架并跑大规模找 bug 实验

工作目录：`framework/ (this directory)`（已有：oracles/{db_runner,normalize,determinism,tlp,equivalence,differential,reproduce,models}.py、agents/{bug_hunter,bug_fixer,json_utils}.py、guidelines/llm_guideline.py、evolution/co_evolution.py、evolution/fitness.py、main.py、testing/）。Python 环境 = 系统 python3（duckdb 1.5.2, psycopg2-binary, pgserver 0.1.4, sqlparse 已装）。DeepSeek `deepseek-chat` 在 config.yml 里可用。无 sudo/docker。

## 已验证的环境事实
- `import pgserver; pgserver.get_server(path)` 能起 PostgreSQL 16.2，`db.psql(sql)` 可执行 SQL；`db.get_uri()` 返回 unix-socket 形式的 psycopg2 连接串（如 `postgresql://postgres:@/postgres?host=<datadir>`，**不是 TCP**——psycopg2 直连要用 `psycopg2.connect(db.get_uri())` 或 `psycopg2.connect(host=<datadir>, dbname='postgres', user='postgres')`）。每个 run 用独立 datadir（如 `/tmp/coevo_pg_<ts>`），跑完 `db.cleanup()`。已实测冒烟通过。
- DuckDB 旧版 venv 在 `$COEVO_OLD_VENV`（duckdb 1.0.0），differential.py 已可用。
- DuckDB `EXPLAIN (FORMAT JSON) <q>` 返回 JSON plan；PG `EXPLAIN (FORMAT JSON) <q>` 同样返回 JSON plan（`db.psql` 返回文本，可用 psycopg2 直连）。

## 架构改造（保持 Argus 代码风格：类型提示、docstring、logging、配置驱动）

### 1. `targets/`（新包，替代 oracles/db_runner.py 的 DuckDB 特化；把 DuckDBRunner 移过来）
- `targets/base.py`：`BaseRunner` 抽象，`setup(schema_sqls) -> list[str]`（返回每条语句错误）、`run(sql, timeout_s=10) -> QueryResult`（复用现有 QueryResult/normalize）、`explain_plan(sql) -> dict|None`、`close()`、`engine_name`、`engine_version`。
- `targets/duckdb_runner.py`：现有 DuckDBRunner 改名移入，加 `explain_plan`（EXPLAIN FORMAT JSON）。
- `targets/postgres_runner.py`：`PostgresRunner`，内部用 pgserver 起嵌入式实例 + psycopg2 连接 autocommit；`run()` 用 `SET statement_timeout` + 线程超时；DDL 失败语句记录并跳过；`explain_plan` 用 `EXPLAIN (FORMAT JSON)`。注意 PG 方言差异（`TRUE/FALSE`、`::` cast、`INTERVAL '1 day'` 均可；`EXPLAIN` 报错就返回 None）。每次 `setup` 前 `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` 清库。
- runner 也要暴露 `clone_fresh()`：返回一个全新 runner（用于最小化/复现，避免状态污染）。

### 2. 新 oracle：`oracles/norec.py`
NoREC（No-Reference oracle，SQLancer 的）：对 `SELECT <cols> FROM <from> WHERE <p>`（无 GROUP BY/DISTINCT/聚合/LIMIT），比较 `optimized = runner.run("SELECT COUNT(*) FROM <from> WHERE <p>")` 与 `unoptimized = runner.run("SELECT COUNT(*) FROM <from> WHERE <p> IS TRUE")`——在标准三值逻辑下两者应相等；不等即 candidate（kind=`norec`，sound）。另外对带 GROUP BY 的查询可用变体：`SELECT <cols> FROM <from> GROUP BY <g>` vs 同查询加 `WHERE p` 的分区计数之和。实现先只做前者（无 GROUP BY 版本），detect 到 GROUP BY/聚合/LIMIT/DISTINCT 跳过。沿用 determinism gate 与 inject_predicate 处理已有 WHERE 的逻辑。

### 3. `coverage/`（新包）
- `coverage/features.py`：`extract_cells(runner, sql, schema_sqls) -> set[str]`，格子 = 三维拼接：
  - `ast:<FEATURE>`：用 sqlparse + 正则抽取 {SELECT, DISTINCT, JOIN, LEFT JOIN, RIGHT JOIN, FULL JOIN, CROSS JOIN, EXISTS, IN (subquery), NOT IN, CASE, COALESCE, CAST, UNION, EXCEPT, INTERSECT, GROUP BY, HAVING, ORDER BY, LIMIT, OFFSET, window fns (ROW_NUMBER/RANK/SUM OVER/LAG/LEAD/FIRST_VALUE...), CTE (WITH), RECURSIVE, subquery-in-SELECT, subquery-in-FROM, correlated-subquery, LIKE, REGEXP, string fns, date fns, INTERVAL}。
  - `plan:<OP>`：`explain_plan` 递归收集算子名（DuckDB: `PROJECTION/FILTER/HASH_JOIN/SEQ_SCAN/LEFT_DELIM_JOIN/...`；PG: `Seq Scan/Hash Join/Nested Loop/Merge Join/Subquery Scan/...`）。explain 失败则该维为空集。
  - `data:<TYPE>`：schema 里出现的列类型规范化（INT/FP/DECIMAL/BOOL/STRING/DATE/TIMESTAMP）× {has_null, has_zero, has_negative, has_empty_str, has_extreme(>1e15 或 <-1e15)}——由 setup 时扫描 inserts 文本判定即可，不必逐值执行。
- `coverage/store.py`：`CoverageStore`，`observe(cells) -> int`（返回新格子数）、`size()`、`save()/load()`（JSON 持久化到 run 目录）。

### 4. `seeds/`（新包）——历史 bug 语料变异
- `seeds/fetch.py`：从 GitHub 下载种子（用 raw.githubusercontent.com，**不要 git clone 整个仓库**）：
  - DuckDB：`https://api.github.com/repos/duckdb/duckdb/contents/test/issues/general` 列出 `.test` 文件，取最近修改的 ~40 个，`raw` 下载；另外 `test/sql/subquery/`、`test/sql/aggregate/`、`test/sql/window/`、`test/sql/join/` 各取 ~15 个 `.test`。
  - PG：从 `https://api.github.com/repos/postgres/postgres/contents/src/test/regress/sql` 取 `select.sql, join.sql, subselect.sql, aggregates.sql, case.sql, window.sql, with.sql, union.sql, arrays.sql, numerology?`（按实际文件名取 ~12 个）。
- `seeds/parse.py`：解析 `.test`/`regress .sql`：`.test` 格式是 `statement ok|error|query <cols> [sort] ... ----` 块；抽出 `CREATE/INSERT` 作 setup、`query` 块作 seed 查询。过滤：跳过 `COPY`、外部文件、`EXPLAIN`、engine-specific pragma、包含 `require`/`skip` 的；PG regress 文件里 `\d`、`SET`、psql 元命令也过滤。
- `seeds/store.py`：`Seed{setup_sqls, query, source, engine}` 列表 JSON 化存 `seeds/corpus.json`（离线缓存，重复运行不重复下载）。
- `seeds/mutate.py`：给 hunter 用的两类变异：
  - 规则变异（零 LLM）：给 seed 查询注入 TLP/NoREC 谓词——从 setup 里随机取数值/字符串列，生成 `p`（`col > const`、`col IS NOT NULL`、`col <> const AND col2 >= const`、三谓词 `a=b AND c<>d AND e>f` 组合），构造成 TLP 三分区与 NoREC 对。**这类变异直接绕过 LLM 跑 oracle，便宜量大**。
  - LLM 变异：prompt 给 1 个 seed + schema，让 LLM 输出 3 个"语义扰动但保持可执行"的变体 + 1 个等价改写。
- 运行策略：每轮迭代 = 60% LLM 生成 + 40% seed 规则变异（后者零 LLM 成本，是产量主力）。

### 5. `evolution/bandit.py`
`CategoryBandit`：arm = 查询类别 + **Fixer 动态生成的根因 arm**（见下）。UCB1：`score = (2*true_bugs + 0.5*novel_cells_avg) / tries + c*sqrt(2 ln total / tries)`；`verdict=false_positive/skipped_nondeterministic` 都计入分母降权。提供 `select(k)`、`update(arm, reward, novel_cells)`、`snapshot()`。替换 bug_hunter 里现有手写 UCB。

### 6. `agents/bug_hunter.py` 升级
- `execute(context)` 中 context 新增：`runner`（BaseRunner）、`coverage_store`、`bandit`、`seeds`。
- 生成数据库时按 bandit.select(3) 选类别；LLM prompt 里加"本轮重点覆盖未触及的 plan 算子：<列出 coverage_store 未覆盖的常见算子 top5>"。
- **查询生成两条路**：(a) LLM 生成（现有流程，每轮 3 次调用：schema/queries/rewrites）；(b) seed 规则变异生成 TLP/NoREC 对（每轮 ≥30 个，零 LLM）。
- 所有查询先过 `is_usable_for_oracles`；可用的记录 coverage cells；跑全部 oracles。
- `learn_from_feedback` 更新 bandit。

### 7. `agents/bug_fixer.py` 升级（根因 arm 生成）
- 现有 triage 保留。当 verdict=true_bug 且 analysis.component 给出时，**spawn 一个根因 arm**：`root_arm_id = f"rc:{component}:{root_signature(q1,q2)}"`，其 prompt hint = `analysis.hypothesis + minimal_case`，注入 hunter 的可用类别表（hunter 生成查询时可被选中，选中时 prompt 里附上"围绕此根因生成变体：换谓词组合/换等价形态 EXISTS↔IN↔JOIN↔NOT EXISTS↔标量子查询/换相关列类型"）。
- 这是论文里"Fixer 反向指导 Hunter"的关键机制，必须在 co_evolution.py 和 summary 里显式记录每次 arm 的产生与后续收益。

### 8. `evolution/co_evolution.py` 升级
- `CoevolutionLoop(target: BaseRunner, ...)`：把 DuckDB 写死的地方参数化；每轮输出 coverage_size、新增格子数、各 oracle 的命中/跳过数、bandit snapshot。
- verdict 分级最终表：`true_bug`（TLP/NoREC/crash 直接 sound 命中，或 equiv/differential 经 LLM+交叉验证）、`version_divergence`、`cross_engine_divergence`、`false_positive`、`skipped_nondeterministic`、`skipped_unusable`。**只有 true_bug 进 bugs.jsonl**。
- 每轮把 (iter, candidate, verdict, novel_cells, llm_calls) 追加到 `results/<run>/metrics.jsonl` 供画图。
- 加 `--engine {duckdb,postgres}` 与 `--seed-corpus` 开关到 main.py。

### 9. PG 特有注意
- PG 对 `SELECT ... FROM t WHERE p` 的 TLP/NoREC 同样成立（三值逻辑一致）。
- PG 方言：hunter 生成查询时 prompt 指定"标准 SQL，避免 DuckDB-only 语法（如 `* EXCLUDE`、`LIST()`、`POSITIONAL JOIN`）"；反过来跨引擎种子要过滤方言。
- PG runner 的 `is_internal_error`：PG 崩溃表现为连接断开/`server closed the connection`，重新起 runner 并记 crash candidate。
- **跨引擎差分**：`oracles/cross_engine.py`：同一 schema/query 同时在 DuckDB 与 PG 执行（schema 需方言兼容：hunter 生成时要求"两引擎通用 DDL"，列类型限 INTEGER/BIGINT/DOUBLE PRECISION/NUMERIC/VARCHAR/BOOLEAN/DATE/TIMESTAMP）。不一致→`cross_engine_divergence` 候选，LLM 裁定哪个引擎对才升级 true_bug（参考 SQL 标准语义）。

### 10. 实验跑法（分阶段，后台执行，全部结果落盘）
LLM 预算总上限 ~1500 次调用（DeepSeek 便宜但慢，注意用 `nohup` + 每轮落盘，允许中断续跑——`main.py` 加 `--resume`）。

阶段A（冒烟，必须先后台跑通再放量）：每引擎 2 轮，确认 runner/oracle/coverage/seed 管线无异常、无 PG 方言大面积失败。
阶段B（主实验）：
- `duckdb`：3 个 campaign × 20 轮，`--queries-per-iter 8`，其中 campaign1 无 seed（纯 LLM）、campaign2 有 seed 规则变异、campaign3 = with-feedback 但 bandit 的 reward 里 coverage 项置 0（消融）。
- `postgres`：同样 3×20 轮。
阶段C（对照，证明 co-evolution 贡献）：
- 固定 LLM 预算 = 阶段B campaign1 的总调用数，跑两个基线：(i) `random_category`：类别均匀随机、bandit 不更新、无根因 arm；(ii) `no_llm`：零 LLM，纯 seed 规则变异 × 同等执行次数。
- 指标：deduped true_bug 数、coverage 终值、候选→true_bug 转化率、LLM call/bug。

所有 run 目录：`results/<engine>_<mode>_<ts>/`，内含 bugs.jsonl、metrics.jsonl、summary.json。

### 11. 验证要求
- `pytest -q` 全绿；新增测试：`test_postgres_runner`（连 pgserver 冒烟）、`test_norec`（手工构造正确/错误案例）、`test_coverage`（cells 抽取与新格子计数）、`test_bandit`（UCB 更新与 select 分布）、`test_seed_parse`（至少解析 5 个真实 .test 文件）。
- 如实报告：每个引擎的 true_bug 去重数（目标 >50，但**达不到就如实报数字，绝不允许把 version_divergence 或 FP 计入**）、top5 bug 的最小复现、coverage 曲线数据、三个模式的对比表、LLM 总调用数与耗时。
- 若中途某阶段失败率（LLM JSON 解析失败 / runner 崩溃 / oracle 异常）>30%，停下来报告具体错误而不是继续烧预算。
- 最后更新 `RESEARCH_PLAN.md`：把方法写成"Coverage-Guided Co-Evolutionary DBMS Testing (CoevoDB)"一节，包含架构图（ASCII）、oracle 健全性分级表、bandit 定义、根因 arm 生成机制、实验协议。

## 输出
完成后报告：文件清单、每引擎统计表（candidates / true_bugs / divergences / FP / skipped / coverage / llm_calls / wall_time）、top bug 复现、阶段B vs C 对比结论、未达成目标的差距分析。所有中间 run 目录保留。