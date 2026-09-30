-- F6: NULL/EXISTS (NOT-IN shape) returns extra rows
--
-- Engine : DuckDB
-- Affects: 1.5.5, longstanding. Non-optimizer family (fires without
--          optimizer toggles; same deliminator surface as F1, different
--          entry shape).
-- Manifestation: wrong result - default returns 12 rows where the
--   correct answer is 8.

-- Shape: correlated NOT EXISTS / NOT IN over nullable columns.
-- Canonical witness (σ ab630f7b): default mode returns 12 rows,
-- all-optimizers-off returns 8.

CREATE TABLE f6_a(x INT, y INT);
INSERT INTO f6_a VALUES (1,1),(2,2),(3,NULL),(NULL,4);
CREATE TABLE f6_b(x INT, y INT);
INSERT INTO f6_b VALUES (1,1),(2,9),(NULL,3);

SELECT * FROM f6_a a
WHERE NOT EXISTS (SELECT 1 FROM f6_b b WHERE b.x = a.x AND b.y <> a.y)
   AND a.x NOT IN (SELECT b.y FROM f6_b b WHERE b.x IS NULL)
ORDER BY 1, 2;

-- Cross-check the expected bag with:
--   SET disabled_optimizers='deliminator,filter_pushdown';
-- or against PostgreSQL (all-off result is the correct one).
