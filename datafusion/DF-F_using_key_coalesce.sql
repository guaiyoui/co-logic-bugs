-- DF-F: USING/NATURAL merged key is NULL on RIGHT/FULL outer rows
--
-- Engine : DataFusion 54.0.0 (reverified round 3)
-- Manifestation: wrong result. duckdb + postgres + sqlite all coalesce
--   the merged key from the preserved side -> DF is the odd engine
--   under a >=3-engine quorum.

CREATE TABLE a(x INT, y INT);
INSERT INTO a VALUES (1, 10);
CREATE TABLE b(x INT, z INT);
INSERT INTO b VALUES (2, 20);

SELECT * FROM a RIGHT JOIN b USING (x);

-- Expected (pg/duckdb/sqlite): (x=2, y=NULL, z=20)
--   the merged key column takes the preserved (right) side's value
-- Actual (datafusion):       (x=NULL, y=NULL, z=20)
--   merged key is NULL on the outer-join row
--
-- Same defect on NATURAL JOIN and FULL JOIN (4 divergent repros:
-- rj_using_star / nj_right / nj_full / fj_using_coalesce).
