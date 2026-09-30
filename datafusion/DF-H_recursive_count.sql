-- DF-H: count(*) over a recursive CTE -> internal error / Rust panic
--
-- Engine : DataFusion 54.0.0 (latest PyPI/crates)
-- Manifestation: internal-error + crash. When the outer query needs no
--   CTE column, DF prunes the working table to an EMPTY schema and the
--   recursive exec breaks.
-- Deterministic 5/5. pg 16.2 + duckdb 1.5.5 both return 5.

WITH RECURSIVE t AS (
  SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 5
)
SELECT count(*) FROM t;
-- -> Arrow error: Schema error: project index 0 out of bounds,
--    max field 0

-- UNION (distinct) variant - actual Rust panic:
WITH RECURSIVE t AS (
  SELECT 1 AS n UNION SELECT n + 1 FROM t WHERE n < 5
)
SELECT count(*) FROM t;
-- -> panic 'index out of bounds: the len is 0 but the index is 0'
--    at datafusion-physical-plan-54.0.0/src/aggregates/group_values/
--    multi_group_by/mod.rs:450  (surfaced as Join Error)

-- Same zero-column scan repro via: SELECT 42 FROM t LIMIT 3;
-- count(*) via subquery; recursive CTE joined to a constant.
-- Control: SELECT count(n) FROM t  -- works (needs the column).
-- Non-recursive CTEs under count(*) are fine.
-- NOTE: writing `WITH RECURSIVE t(x) AS ...` with an alias list first
-- hits DF-E (the alias list is dropped) - use the no-alias form above
-- to reach the count(*) bug.
