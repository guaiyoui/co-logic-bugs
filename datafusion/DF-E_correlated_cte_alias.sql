-- DF-E: correlated subquery rejected outside WHERE; recursive CTE
--       column-alias list dropped
--
-- Engine : DataFusion 54.0.0 (reverified round 3; 20 repros)
-- Manifestation: unplannable-error on legal SQL. pg + duckdb (+sqlite
--   where applicable) execute all forms.

CREATE TABLE t(a INT);
INSERT INTO t VALUES (1),(2);
CREATE TABLE u(a INT, w INT);
INSERT INTO u VALUES (1,10),(2,20);

-- (1) Correlated scalar subquery in the SELECT list:
SELECT a, (SELECT w FROM u WHERE u.a = t.a) FROM t;
-- -> "Invalid (non-executable) plan after Analyzer"
-- WHERE-position decorrelation works; SELECT-position does not.

-- Same class, also verified on 54.0.0:
--   - correlated scalar/EXISTS/IN in ORDER BY
--     ("In/Exist/SetComparison subquery can only be used...")
--   - EXISTS in SELECT list ("Physical plan does not support logical
--     expression Exists")
--   - scalar subquery w/ LIMIT in SELECT; IN (SELECT ...) in JOIN ON

-- (2) WITH RECURSIVE ignores the column-alias list:
WITH RECURSIVE t(n) AS (
  SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5
) SELECT * FROM t;
-- -> "No field named n" (valid fields are t."Int64(1)")
-- Non-recursive WITH t(n) AS ... binds correctly; the alias list is
-- dropped only on the RECURSIVE path.
