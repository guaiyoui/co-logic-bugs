-- MYSQL-A: NOT(NOT(<quantified comparison>)) returns wrong result —
--           the MySQL-side sibling of MDB-A (upstream bug #121079)
--
-- Engine : MySQL 8.4.2 — deterministic, fires on INT/REAL/string
--          columns, WHERE and HAVING, correlated subqueries.
--          NULLs in the subquery are NOT required.
--
-- Upstream: MySQL bug #121079 (fixed in MySQL 9.7); MariaDB
--   MDEV-40443 / MDEV-40566.  Present in the 8.4.x LTS line.
--
-- Oracle: NOT(NOT P) is a three-valued-logic identity
--   (NOT(NOT TRUE)=TRUE, NOT(NOT FALSE)=FALSE,
--    NOT(NOT UNKNOWN)=UNKNOWN).  MySQL drops/mishandles one negation on
--   x <op> ANY|ALL (subquery) for <,<=,>,>= ; =ANY / IN are unaffected
--   (different code path).
--
-- Expected: NOT(NOT P) returns the same set as P.
-- Actual  : returns the complement / wrong set.

CREATE TABLE a(x BIGINT);
CREATE TABLE d(y BIGINT);
INSERT INTO a VALUES (1),(2),(9),(NULL),(4);
INSERT INTO d VALUES (5),(4);

SELECT x FROM a WHERE x > ALL (SELECT y FROM d);               -- {9} ok
SELECT x FROM a WHERE NOT (NOT (x > ALL (SELECT y FROM d)));  -- {1,2,4} WRONG

-- full matrix on this dataset (all WRONG except =ANY/IN):
--   x < ANY :  P={1,2,4}  NN={4,9}
--   x <= ANY:  P={1,2,4}  NN={9}
--   x > ALL :  P={9}      NN={1,2,4}
--   x >= ALL:  P={9}      NN={1,2}
--   x IN (d):  both {4}   (ok)
