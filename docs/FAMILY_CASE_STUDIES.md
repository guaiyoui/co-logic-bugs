# DuckDB upstream "same-family recurrence" case studies

Official duckdb/duckdb issues where a bug was reported and fixed, then a
*sibling* bug in the same root-cause family reappeared in a later version.
Each family was empirically re-verified on our local version ladder
(1.0.0 / 1.1.3 / 1.5.5; `results/family_case_study/probe.py`).

These cases motivate the σ-signature ledger design: a fix addresses one
plan shape, the *mechanism* survives and produces a new trigger shape.

---

## F1 — Correlated-subquery decorrelation (delim join / CTE in subquery)

| issue | date | version | status | symptom |
|---|---|---|---|---|
| #14305 | Oct 2024 | v1.1.0 | closed stale, **never fixed** | nested macros sharing column names bind wrong → wrong result |
| #20188 | Dec 2025 | 1.4/1.5 era | closed, fixed by PR #20368 | CTE inside scalar subquery → wrong result |
| #23639 | Jul 2026 | main only (not 1.5.4) | **open** | filter_pushdown + decorrelated aggregate → NULL rows |

Mechanism: decorrelation rewrites correlated subqueries into delim
joins; every fix patches one binding/plan shape and the next shape
re-breaks. #20188's report literally says *"Related to #14305 but now
even the working query from there produces the wrong result"* — the
sibling is a strict superset of the ancestor.

**Ladder evidence**

```sql
-- #14305 minimal: nested macro column capture
select simpler_maximal_palindrome('babaccd');   -- expect 'bab'
```

- 1.0.0: feature-gated (binder rejects lateral `range` params)
- 1.1.3: **FIRES → `'d'`**
- 1.5.5: still anomalous → `NULL` (≠ `'bab'`)

```sql
-- #20188 minimal: scalar subquery containing a CTE
SELECT (WITH nt AS (SELECT length(s) AS n)
        SELECT * FROM (SELECT * FROM nt) WHERE 'a' = s[n]) AS ends_in_a
FROM unnest(['a','ab']) _(s);                  -- expect {1, NULL}
```

- 1.0.0 / 1.1.3 / 1.5.5: all correct (fix #20368 landed pre-1.5.5)

```sql
-- #23639 minimal: filter_pushdown over decorrelated aggregate
WITH src(k,v) AS (VALUES (5,20),(4,30)),
calc AS (SELECT SUM(v) OVER () AS w,
  (SELECT SUM(i.v) FROM src i WHERE i.k >= o.k - 1) AS b FROM src o)
SELECT w, b FROM calc WHERE w IS DISTINCT FROM b;  -- expect []
```

- 1.1.3 / 1.5.5: clean — the newest generation exists only on main

---

## F2 — SEMI/ANTI join with non-equality predicates

| issue | date | version | status | symptom |
|---|---|---|---|---|
| #9308 | Oct 2023 | 0.9.1 | closed, fixed | NOT EXISTS + correlated `<=` → wrong empty result |
| PR #12916 → #13404 | 2024–25 | — | closed→#17294 | decorrelated NOT EXISTS inside correlated *recursive* CTE; author: "continuation of #12916" |
| #20410 | Jan 2026 | v1.4 + main | closed, fixed by #20435 | ANTI JOIN over `MATERIALIZED` CTE planned as RIGHT_ANTI → non-eq predicates dropped → empty result |

Mechanism: mark/anti-join "found" labeling handled only the equality
conjunct; each new plan shape (correlated non-eq, recursive CTE
correlation, RIGHT_ANTI) re-exposed the same hole.

**Ladder evidence**

```sql
-- #9308
create table t1(c1 int); insert into t1 values (1);
create table t2(c1 int);   -- empty
select c1 from t1 where not exists
  (select 1 from t2 where t1.c1 <= t2.c1);       -- expect 1 row
```

- 1.0.0 / 1.1.3 / 1.5.5: all correct (fixed before 1.0.0)

```sql
-- #20410
WITH cte1 AS MATERIALIZED (
  SELECT 'col1' col1, UNNEST([TIMESTAMP '2025-01-01 00:00:11',
                             TIMESTAMP '2025-01-01 00:00:41']) col2),
cte2 AS (SELECT 'col1' col1, TIMESTAMP '2025-01-01 00:00:40' col2, 'col3' col3)
SELECT * FROM cte1 ANTI JOIN cte2
  ON cte1.col1 = cte2.col1 AND cte1.col2 > cte2.col2;   -- expect 1 row
```

- 1.0.0: correct · **1.1.3: FIRES → `[]`** (bug latent since ≤1.1.x,
  only reported on v1.4!) · 1.5.5: correct (fixed by #20435)

The 1.1.3 firing is notable: the bug existed years before it was
reported — the reported version window understates the true window.

---

## F3 — Window frame / ordering semantics

| issue | date | version | status | symptom |
|---|---|---|---|---|
| #9416 | Oct 2023 | 0.9.0 regression | closed, fixed #9425 | peer-dependent window fns read uninitialized memory → different answer per run |
| #10885 | Feb 2024 | 1.0 era | closed (clamped) | negative `RANGE PRECEDING` silently clamped |
| #21592 | Mar 2026 | ≥v1.5.0 | **open** | new `WindowSelfJoinOptimizer` rewrites ROWS-frame windows to GROUP BY + INNER JOIN → every row gets full-partition aggregate |
| #23448 | Jul 2026 | 1.5.4 + main | **open** | `SimplifyWindowedAggregate` strips aggregate-local `ORDER BY` when prefix of window order → `LIST(DISTINCT c ORDER BY c)` returns reversed list |

Mechanism: window semantics keeps being re-broken by *new* code
(parallelization, then a new optimizer, then an aggregate
simplification). Each trigger is a different shape but the invariant
"frame/order semantics must survive rewriting" is one family.

**Ladder evidence**

```sql
-- #23448 (verified on our 1.5.5)
WITH src(c) AS (SELECT range % 2 FROM range(33))
SELECT LIST(DISTINCT c ORDER BY c) OVER
  (ORDER BY c ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
FROM src LIMIT 1;                              -- expect [0,1]
```

- 1.0.0 / 1.1.3: feature-gated (`ORDER BY` in window agg not implemented)
- **1.5.5: FIRES → `[1,0]`** — open upstream, live on our install

```sql
-- #21592
SELECT seq, sum(seq) OVER (PARTITION BY grp
  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumsum
FROM (VALUES (1,'A'),(2,'A'),(3,'A'),(1,'B'),(2,'B')) t(seq,grp);
-- expect cumulative 1,3,6 / 1,3; buggy = all 6 / all 3
```

- 1.0.0 / 1.1.3: correct (optimizer didn't exist)
- 1.5.5: correct — `EXPLAIN` shows plain WINDOW, optimization not
  applied (already patched in 1.5.5, or trigger shape narrower); issue
  still open upstream

---

## F4 — DISTINCT semantics vs. filter pushdown / join

| issue | date | version | status | symptom |
|---|---|---|---|---|
| #9241 | Oct 2023 | 0.9.x | closed, fixed #9270 | outer filter pushed *through* `DISTINCT ON` subquery → wrong rows |
| #21719 | Mar 2026 | v1.5.1 | closed | `DISTINCT` in CTE + LEFT JOIN (empty side) → *intermittently* missing rows (~2-3%); `GROUP BY` equivalent is clean |

Mechanism: DISTINCT/delim semantics vs. predicate/join reordering —
the 2023 fix covered the DISTINCT ON pushdown path; the 2026 sibling
fires through a different (empty-relation/statistics) path and is
nondeterministic.

**Ladder evidence**

```sql
select * from (select distinct on (a) a, b from foo order by a, b desc) sub
where b <> 2;     -- #9241: buggy pushed filter inside, wrong rows
```

- clean on 1.0.0+ (fixed by #9270)

- #21719 probabilistic repro (300 fresh connections, 5-row UUID data):
  **0/300 failures on 1.5.5** — reported ~2-3% rate on 1.5.1, so fixed
  or trigger rate below our sample on 1.5.5

---

## Cross-cutting observations

1. **Siblings explicitly acknowledge ancestry**: #20188 links #14305;
   #13404 says "continuation of #12916". The mechanism-level family is
   real in maintainers' own mental model — our σ/family ledger is the
   machine version of that.
2. **Reported windows understate true windows**: #20410 reported on
   v1.4 but fires on 1.1.3 — latent years before discovery. Version-
   ladder replay (our ν field) is the only way to measure this.
3. **Fixes are shape-local, mechanisms are global**: every fix above
   patches one trigger shape; the family persists because the
   vulnerable component (decorrelation, mark-join labeling, window
   rewriting, DISTINCT pushdown) is a *recurring design assumption*.
4. **Newest siblings live on main**: #23639 and #23448 reproduce on
   current main / latest release — the families are still producing.

---

## Appendix: repro harness

Runs every case above on a local venv. Usage:

```bash
for v in venv_duckdb_old venv_duckdb_113 venv_duckdb_155; do
  echo "== $v =="; $v/bin/python this_snippet.py; done
```

```python
"""FAMILY_CASE_STUDIES repro matrix. `FIRES` = observed != expected."""
import sys
import uuid

import duckdb


def run(sqls, con=None):
    own = con is None
    if own:
        con = duckdb.connect()
    try:
        out = None
        for s in sqls:
            out = con.execute(s).fetchall()
        return ("ok", out)
    except Exception as e:  # noqa: BLE001
        return ("err", str(e)[:160])
    finally:
        if own:
            con.close()


def p(label, expect, got):
    fire = ""
    if got[0] == "ok" and got[1] != expect:
        fire = "  <-- FIRES"
    print(f"{label} (expect {expect}): {got}{fire}")


# ---- F1: decorrelation family ---------------------------------------
p("#14305 nested macro", [("bab",)], run([
    """create or replace function is_palindrome(s) as (
         with nt as (select length(s) as n)
         select not exists (SELECT i FROM
           (select n, unnest(range(1, n+1 // 2)) as i from nt)
           WHERE substr(s,i,1) != substr(s, 1 + n - i,1)))""",
    """create or replace function max_palindrome(s) as (
         select max_by(sub,i) from (SELECT i, substr(s,1,i) as sub
           FROM range(1, length(s)+1) t(i) WHERE is_palindrome(sub)))""",
    """create or replace function simpler_maximal_palindrome(s) as (
         select max_by(p, length(p)) from
           (SELECT max_palindrome(substr(s, i)) as p
            FROM range(1, length(s)) t(i)))""",
    "select simpler_maximal_palindrome('babaccd')",
]))

p("#20188 cte-in-subquery", [(1,), (None,)], run([
    "SELECT (WITH nt AS (SELECT length(s) AS n) "
    "SELECT * FROM (SELECT * FROM nt) WHERE 'a' = s[n]) AS ends_in_a "
    "FROM unnest(['a','ab']) _(s)",
]))

p("#23639 filter_pushdown+decorr", [], run([
    "WITH src(k,v) AS (VALUES (5,20),(4,30)), "
    "calc AS (SELECT SUM(v) OVER () AS w, "
    " (SELECT SUM(i.v) FROM src i WHERE i.k >= o.k - 1) AS b FROM src o) "
    "SELECT w, b FROM calc WHERE w IS DISTINCT FROM b",
]))

# ---- F2: ANTI/SEMI + non-equality ------------------------------------
p("#9308 NOT EXISTS non-eq", [(1,)], run([
    "create or replace table t1(c1 int)", "insert into t1 values (1)",
    "create or replace table t2(c1 int)",
    "select c1 from t1 where not exists "
    "(select 1 from t2 where t1.c1 <= t2.c1)",
]))

p("#20410 ANTI+MATERIALIZED", 1, (lambda g: (
    "ok", len(g[1]) if g[0] == "ok" else g))(run([
    """WITH cte1 AS MATERIALIZED (
         SELECT 'col1' col1, UNNEST([TIMESTAMP '2025-01-01 00:00:11',
                                    TIMESTAMP '2025-01-01 00:00:41']) col2),
       cte2 AS (SELECT 'col1' col1, TIMESTAMP '2025-01-01 00:00:40' col2,
                'col3' col3)
       SELECT * FROM cte1 ANTI JOIN cte2
         ON cte1.col1 = cte2.col1 AND cte1.col2 > cte2.col2""",
])))

# ---- F3: window family ------------------------------------------------
p("#23448 LIST(DISTINCT ORDER BY)", [([0, 1],)], run([
    "WITH src(c) AS (SELECT range % 2 FROM range(33)) "
    "SELECT LIST(DISTINCT c ORDER BY c) OVER "
    "(ORDER BY c ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) "
    "FROM src LIMIT 1",
]))

p("#21592 window_self_join ROWS", [(1, 1), (2, 3), (3, 6), (1, 1), (2, 3)],
  run([
    "SELECT seq, sum(seq) OVER (PARTITION BY grp ROWS BETWEEN "
    "UNBOUNDED PRECEDING AND CURRENT ROW) AS cumsum "
    "FROM (VALUES (1,'A'),(2,'A'),(3,'A'),(1,'B'),(2,'B')) t(seq,grp) "
    "ORDER BY grp, seq",
]))

# ---- F4: DISTINCT family ----------------------------------------------
# correct = []: DISTINCT ON picks (1,2),(2,2) first, outer filter then
# removes both. Buggy (filter pushed inside) would give [(1,1),(2,1)].
p("#9241 DISTINCT ON + outer filter", [], run([
    "create table foo(a int, b int)",
    "insert into foo values (1,1),(1,2),(2,1),(2,2)",
    "select * from (select distinct on (a) a, b from foo "
    "order by a, b desc) sub where b <> 2 order by a",
]))

fails = 0
N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
for _ in range(N):
    ids = [str(uuid.uuid4()) for _ in range(5)]
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE items (id VARCHAR, category VARCHAR)")
        con.executemany(
            "INSERT INTO items VALUES (?, ?)",
            [(ids[0], "A"), (ids[1], "A"), (ids[2], "B"),
             (ids[3], "B"), (ids[4], "A")],
        )
        con.execute("CREATE TABLE mappings (id VARCHAR, parent_id VARCHAR)")
        con.execute("INSERT INTO mappings VALUES (?, ?)", [ids[3], ids[4]])
        con.execute("CREATE TABLE groups (id VARCHAR, group_id VARCHAR)")
        res = con.execute(
            """WITH resolved AS (
                 SELECT DISTINCT i.id,
                   COALESCE(g.group_id, m.parent_id, i.id) AS resolved_id
                 FROM items i LEFT JOIN mappings m ON i.id = m.id
                 LEFT JOIN groups g ON COALESCE(m.parent_id, i.id) = g.id)
               SELECT DISTINCT r.resolved_id
               FROM items i JOIN resolved r ON i.id = r.id
               WHERE i.category = 'A'"""
        ).fetchall()
        if len(res) != 3:
            fails += 1
    finally:
        con.close()
print(f"#21719 DISTINCT+LEFTJOIN intermittent: {fails}/{N} failures "
      f"(~2-3% reported on v1.5.1)")
```

## Observed matrix (2026-09-18, local venvs)

| case | 1.0.0 | 1.1.3 | 1.5.5 |
|---|---|---|---|
| #14305 nested macro | feature-gated | **FIRES `'d'`** | **anomalous `NULL`** |
| #20188 cte-in-subquery | clean | clean | clean (fixed #20368) |
| #23639 pushdown+decorr | clean | clean | clean (main-only sibling) |
| #9308 NOT EXISTS non-eq | clean | clean | clean (fixed pre-1.0) |
| #20410 ANTI+MATERIALIZED | clean | **FIRES `[]`** | clean (fixed #20435) |
| #23448 LIST window ORDER | feature-gated | feature-gated | **FIRES `[1,0]`** |
| #21592 window_self_join | n/a | n/a | clean (plan shows WINDOW; already patched or narrower trigger) |
| #9241 DISTINCT ON | clean | clean | clean (fixed #9270) |
| #21719 intermittent DISTINCT | clean | clean | 0/300 |
