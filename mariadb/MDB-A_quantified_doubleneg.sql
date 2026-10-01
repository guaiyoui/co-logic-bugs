-- MDB-A: NOT(NOT(<quantified comparison>)) returns the COMPLEMENT —
--         double negation around ANY/ALL subquery drops a negation
--
-- Engine : MariaDB 11.8.9 — 31 of 36 generated variants diverge.
--          Fires under both text protocol and PREPARE/EXECUTE binary
--          protocol; no optimizer_switch arm avoids it
--          (in_to_exists/subquery_cache/materialization/semijoin/
--          exists_to_in all still wrong -> bug is in Item-level
--          negation, pre-optimizer).
--
-- Upstream: MySQL #121079 (fixed in MySQL 9.7 via a subquery rewrite
--   that does NOT port to MariaDB's Item_func_not_all/upper_item
--   design); MariaDB MDEV-40443 OPEN, MDEV-40557 unfixed.
--
-- Oracle: NOT(NOT P) is a 3VL tautology — the engine itself evaluates
--   both sides, so divergence is self-evidencing (no external model).
--
-- Expected: NOT(NOT P) returns the same set as P.
-- Actual  : returns the complement set.

CREATE TABLE a(x BIGINT);
CREATE TABLE d(y BIGINT);
INSERT INTO a VALUES (1),(2),(9),(NULL),(4);
INSERT INTO d VALUES (5),(4);

SELECT x FROM a WHERE x > ALL (SELECT y FROM d);               -- {9} ok
SELECT x FROM a WHERE NOT (NOT (x > ALL (SELECT y FROM d)));  -- {1,2,4} WRONG

-- <> joins the affected operator set (not in the original report):
SELECT x FROM a WHERE x <> ANY (SELECT y FROM d);             -- {1,2,4,9} ok
SELECT x FROM a WHERE NOT (NOT (x <> ANY (SELECT y FROM d))); -- {4} WRONG

-- unaffected paths (controls): x IN (d), x <> ALL / NOT IN,
-- IF(x > ALL,1,0) in target list, plain x < 5.
-- Affected contexts: WHERE, HAVING, correlated subquery,
-- LEFT JOIN ON, derived table, CASE WHEN.
