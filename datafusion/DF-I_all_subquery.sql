-- DF-I: expr <op> ALL (subquery) with the compared column unprojected
--       -> ProjectionPushdown internal assertion
--
-- Engine : DataFusion 54.0.0
-- Manifestation: internal-error on legal SQL.
--   All six comparison ops {>,>=,<,<=,=,<>} reproduce; ANY/SOME are
--   fine - the decorrelation path for ALL references the unprojected
--   column. pg + duckdb return {1,2,3,NULL}-class results.

CREATE TABLE t(a INT, b INT);
INSERT INTO t VALUES (1,5),(2,15),(3,25),(NULL,10);
CREATE TABLE u(w INT);
INSERT INTO u VALUES (10),(20);

SELECT a FROM t WHERE b > ALL (SELECT w FROM u);
-- -> ProjectionPushdown ... Assertion failed: col.name() == matching_name:
--    Input field name w does not match
--
-- Workarounds that dodge it: adding b to the SELECT list, or
-- correlating the subquery.
-- Name-matched variant  b > ALL (SELECT b FROM u)  instead surfaces
-- "Exists ... not implemented" - same ALL-decorrelation root area.
