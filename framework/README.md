# CoevoDB — Coverage-Guided Co-Evolutionary DBMS Bug Finding

An LLM Hunter/Fixer co-evolution framework for finding DBMS logic bugs. The LLM
proposes schemas, data, queries, rewrites, and triage hypotheses; it is **never
the bug oracle**. Every reported bug passes a deterministic oracle (TLP, NoREC,
plan-variant, crash) or an adjudicated equivalence/cross-engine check, repeated
reproduction, minimization, and root-cause deduplication.

## Architecture

```text
targets/    DuckDBRunner (in-process) | PostgresRunner (pgserver, PG 16.2)
oracles/    tlp | norec | plan_variant | equivalence | differential | crash
coverage/   AST features × physical plan operators × data boundary values
evolution/  UCB1 bandit over categories + Fixer-spawned root-cause arms
seeds/      1069 historical regression tests (DuckDB test/issues, PG regress)
agents/     bug_hunter (generation) | bug_fixer (triage/minimize/diagnose)
results/    per-run summary.json, metrics.jsonl, bugs.jsonl, candidates.jsonl
```

## Bug verdict taxonomy

| verdict | meaning |
|---|---|
| `true_bug` | sound oracle hit or adjudicated mismatch, minimized + deduped |
| `false_positive` | oracle fired but triage showed the test/rewrite was invalid |
| `version_divergence` | old vs new DuckDB differ; not counted as a bug |
| `cross_engine_divergence` | PG vs DuckDB differ; supporting evidence only |
| `skipped_nondeterministic` | ordering ties / volatile functions make the test unjudgeable |

Only `true_bug` counts toward campaign goals. Version and cross-engine
divergences are reported separately.

## Running

```bash
# API key is read from the environment; never put it in YAML.
export DEEPSEEK_API_KEY=...

# Feedback-guided campaign on DuckDB
python main.py --engine duckdb --mode full --iterations 100 \
    --queries-per-iter 8 --seed-mutations-per-iter 20

# PostgreSQL (pgserver launches an embedded PG 16.2 automatically)
python main.py --engine postgres --mode full --iterations 100 \
    --pg-datadir /tmp/coevo_pg

# Ablations
python main.py --engine duckdb --mode random_category ...   # no feedback
python main.py --engine duckdb --mode no_llm ...            # seed-only
```

## Tests

```bash
pytest testing/ -q     # 36 tests, no API key required
```

## Machine-local paths

A few entry points need resources that live outside the checkout. Every
one is overridable via environment variables (`util/paths.py`); the
fallbacks are the original developer layout (`~/work/db_safety/...`) and
simply won't resolve on a fresh clone — set the variables instead of
editing source.

| env var | resource |
|---|---|
| `DEEPSEEK_API_KEY` | LLM credential (referenced as `${DEEPSEEK_API_KEY}` in config.yml) |
| `COEVO_PGBLD` | root of PG assert/ASan build prefixes (`pg186_assert`, `pgmaster_inject`, ...) |
| `COEVO_PG_PREFIX` | full install prefix used by PostgresRunner (overrides `--build` resolution) |
| `COEVO_PG_EXTRA_OPTS` | extra postmaster `-c` opts for LocalPgServer |
| `COEVO_OLD_VENV` | old-DuckDB venv for the differential oracle (or `--old-duckdb-venv`) |
| `COEVO_DUCKDB_SRC` | DuckDB source checkout for `repair_bench/run_localize.py` |
| `COEVO_ARGUS_DIR` | sibling Argus checkout for `scripts/argus_*` |
| `COEVO_KEYS_YML` | `{key, completion_url}` file for repair_bench drivers |

Script docstrings use `$COEVO_PGBLD/<build>` as the canonical prefix form.

## Honesty boundaries

- The first confirmed bug (DuckDB deliminator `EXISTS` self-join, see
  `results/run_20260913_234645`) was found in iteration 0, before feedback had
  any effect — it validates the oracle pipeline, not the co-evolution claim.
- `version_divergence` and `cross_engine_divergence` are never counted as bugs.
- UCB1 regret bounds assume a fixed arm set and stationary rewards; Fixer-spawned
  root-cause arms break stationarity, so the bandit guarantee is empirical, not
  a theorem we claim for the full system.

See [RESEARCH_PLAN.md](RESEARCH_PLAN.md) for hypotheses and experiment gates,
and [BRIEF_coevo_framework.md](BRIEF_coevo_framework.md) for the engineering spec.
