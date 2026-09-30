-- F2: Window duplicate PARTITION BY key mis-merged
--
-- Engine : DuckDB
-- Affects: 1.5.x only (regression vs 1.0.0/1.1.3). Already fixed on
--          main: PR duckdb/duckdb#24685 "Compare window partitions as
--          sets" (fixes #24629) - bound_window_expression.cpp
--          PartitionsAreEquivalent compared partition lists by raw
--          length + one-directional set membership, so {a,a} == {a,b}.
--          Worth reporting against 1.5.5 as a regression note /
--          missing regression test.
-- Manifestation: wrong result.

CREATE TABLE src(a INTEGER, b INTEGER, v INTEGER);
INSERT INTO src VALUES (1, 1, 10), (1, 2, 20);

SELECT
    SUM(v) OVER (PARTITION BY a, b)   AS w_ab,
    SUM(v) OVER (PARTITION BY a, a)   AS w_aa
FROM src ORDER BY b;

-- Expected (postgres 16.2 dedupes the key; duckdb 1.0.0 correct):
--   w_ab | w_aa
--   -----+-----
--   30   | 30
--   30   | 30
-- Actual on 1.5.5: the duplicate key partitions are mis-merged,
-- giving (10,10) / (20,20) instead of whole-partition sums.
--
-- Variant that shows the same defect (from recall_repros.json):
SELECT
    SUM(v) OVER (PARTITION BY a, a, b) AS w_aab,
    SUM(v) OVER (PARTITION BY a, a, a) AS w_aaa
FROM src ORDER BY b;
