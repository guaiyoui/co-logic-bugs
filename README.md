# co-logic-bugs

Confirmed logic bugs found by an LLM-driven co-evolutionary DBMS testing
framework (multi-oracle differential + plan-variant + metamorphic testing).

Each directory contains self-contained reproducers: a commented `.sql`
file per bug family (version, expected vs actual, upstream references),
plus standalone verifier scripts where applicable.

## Confirmed families (21)

| engine | version | family | manifestation | status |
|---|---|---|---|---|
| DuckDB | 1.5.5 | [F1](duckdb/F1_correlated_exists_delim.sql) | correlated EXISTS self-join drops rows (deliminator + filter_pushdown) | still on main `d8cdaa3` |
| DuckDB | 1.5.5 | [F2](duckdb/F2_window_dup_partition.sql) | `PARTITION BY a,a` duplicate key mis-merged | fixed on main |
| DuckDB | 1.5.5 | [F5](duckdb/F5_parallel_cte_offset.sql) | CTE+OFFSET parallel row-drop (flaky) | worse on 1.5.5 than 1.1.3 |
| DuckDB | 1.5.5 | [F6](duckdb/F6_null_exists_notin.sql) | NULL/EXISTS (NOT-IN shape) wrong result | longstanding |
| DuckDB | 1.5.5 | [F7](duckdb/F7_interval_cse.sql) | interval equality is not a congruence (CSE substitution) | still on main `d8cdaa3` |
| DuckDB | 1.5.5 | [F10](duckdb/F10_window_frame_underflow.sql) | window frame-bound underflow: SIGFPE + INTERNAL + silent garbage | minimal shape fixed on main |
| DuckDB | 1.5.5 | [F11](duckdb/F11_merge_nmbs_rowid.sql) | MERGE without INSERT branch -> `Failed to bind rowid` INTERNAL | 1.5.5 release |
| DataFusion | 54.0.0 | [DF-A](datafusion/DF-A_except_all.sql) | `EXCEPT ALL` drops multiplicity and NULL rows | confirmed on 54.0.0 |
| DataFusion | 54.0.0 | [DF-B](datafusion/DF-B_intersect_all.sql) | `INTERSECT ALL` returns LHS multiplicity, not min | confirmed on 54.0.0 |
| DataFusion | 54.0.0 | [DF-D](datafusion/DF-D_generated_column.sql) | `GENERATED ALWAYS AS ... STORED` silently returns NULL | confirmed on 54.0.0 |
| DataFusion | 54.0.0 | [DF-E](datafusion/DF-E_correlated_cte_alias.sql) | correlated subquery rejected outside WHERE; recursive CTE alias list dropped | confirmed on 54.0.0 |
| DataFusion | 54.0.0 | [DF-F](datafusion/DF-F_using_key_coalesce.sql) | USING/NATURAL merged key NULL on RIGHT/FULL outer rows | confirmed on 54.0.0 |
| DataFusion | 54.0.0 | [DF-G](datafusion/DF-G_decimal_literal.sql) | high-precision numeric literal silently loses precision | confirmed on 54.0.0 |
| DataFusion | 54.0.0 | [DF-H](datafusion/DF-H_recursive_count.sql) | `count(*)` over recursive CTE -> internal error / Rust panic | confirmed on 54.0.0 |
| DataFusion | 54.0.0 | [DF-I](datafusion/DF-I_all_subquery.sql) | `expr op ALL (subquery)` w/ unprojected column -> assertion | confirmed on 54.0.0 |
| DataFusion | 54.0.0 | [DF-J](datafusion/DF-J_lateral_unnest.sql) | lateral `UNNEST` unplannable | confirmed on 54.0.0 |
| SQLite | 3.51.2 | [SQLITE-A](sqlite/SQLITE-A_star_using_rightjoin.sql) | `SELECT *` over RIGHT/FULL + USING merged column -> spurious ambiguous column | deterministic 10/10 |
| SQLite | 3.51.2 | [SQLITE-B](sqlite/SQLITE-B_using_ambiguity.sql) | USING/NATURAL ambiguity check direction-asymmetric | deterministic 10/10 |
| SQLite | 3.51.2 | [SQLITE-C](sqlite/SQLITE-C_unistr.sql) | `unistr()` emits malformed UTF-8 (no surrogate pairing) | deterministic 10/10 |
| TiDB | v8.5.8 | [TIDB-A](tidb/TIDB-A_time_overflow.sql) | TIME overflow unchecked in TiDB layer (vs MySQL 9.7.1) | upstream pingcap/tidb#56865 still open |
| TiDB | v8.5.8 | [TIDB-B](tidb/TIDB-B_decimal_division.sql) | decimal division extra internal precision (compat gap) | low severity |

## Framework (`framework/`)

The code that found these bugs — the CoevoDB LLM Hunter/Fixer
co-evolution testing framework — lives in
[framework/](framework/README.md). It is a working snapshot of the
research codebase: multi-oracle testing (TLP, NoREC, plan-variant,
cross-engine, crash), coverage-guided generation, σ-signature family
dedup, and per-engine drivers (DuckDB, PostgreSQL, SQLite, DataFusion,
TiDB), plus the repair_bench patch/localization benchmark.

Run artifacts (`results/`, `repair_bench/runs/`) are gitignored — the
published bugs above are the curated outcome of ~200k executions.
Machine-local paths (PG build prefixes, old venvs, the LLM key file)
resolve through environment variables via `framework/util/paths.py` —
see the table in [framework/README.md](framework/README.md#machine-local-paths).
Secrets stay in the environment (`DEEPSEEK_API_KEY`), never in source.

```bash
cd framework && pip install -r requirements.txt
export DEEPSEEK_API_KEY=...
python main.py --engine duckdb --mode full --iterations 8
pytest testing/ -q        # no API key required
```

## Running the reproducers

Each `.sql` file is self-contained: header comments give version,
expected vs actual output, and upstream references.

- **DuckDB**: `python -c "import duckdb; ..."` or the `duckdb` CLI, e.g.
  `duckdb -c ".read duckdb/F7_interval_cse.sql"` (verified on `duckdb==1.5.5`).
- **DataFusion**: `pip install datafusion==54.0.0`, feed each statement
  to `SessionContext().sql(...)`.
- **SQLite**: any `sqlite3` >= 3.51 (the `unistr()` cases need 3.51+).
  A standalone verifier is included: `python sqlite/sqlite_unistr_hunt.py`.
- **TiDB**: `tiup playground` on 127.0.0.1:4000 plus a MySQL reference
  (divergence vs MySQL 9.7.1 is the oracle).

## Documentation

`docs/` carries the full evidence trail:

- [UPSTREAM_ISSUES.md](docs/UPSTREAM_ISSUES.md) — report-ready write-up
  per family, ranked by reportability (including excluded tiers and why)
- [FAMILY_LEDGER_155.md](docs/FAMILY_LEDGER_155.md) — full DuckDB 1.5.5
  ledger: signatures, version ladder, cross-engine adjudication
- [FAMILY_CASE_STUDIES.md](docs/FAMILY_CASE_STUDIES.md) — upstream
  "same-family recurrence" case studies motivating the family model
- [frame_fuzz_report.md](docs/frame_fuzz_report.md) — F10 fuzz campaign
  (828k combos, 10,670 hits: 1,836 SIGFPE + 2,866 INTERNAL + ~2,950 garbage)
- [FINAL_REPORT.md](docs/FINAL_REPORT.md) — campaign totals + SQLancer
  baseline comparison
- [manual_findings.jsonl](docs/manual_findings.jsonl) — curated family
  records; [recall_repros.json](docs/recall_repros.json) — canonical
  setup/query/fix_set per family

## Scope note

Only confirmed-tier families are included. Cases adjudicated as
unspecified-semantics / debug-setting / user-setting / spec-observation
(F3, F8, F9, F12, F13, DF-C) are documented in
[docs/UPSTREAM_ISSUES.md](docs/UPSTREAM_ISSUES.md) but intentionally
not filed as bug reproducers.
