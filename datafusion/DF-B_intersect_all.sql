-- DF-B: INTERSECT ALL returns LHS multiplicity, not min-multiplicity
--
-- Engine : DataFusion 54.0.0 (reverified round 3)
-- Manifestation: wrong result - behaves as a semi-join, returning all
--   LHS copies instead of min(lhs,rhs) copies.
-- Upstream: same area as apache/datafusion#12955 (still open), which
--   documents the ALL-semantics gap for INTERSECT RHS copies.

CREATE TABLE t(v INT);
INSERT INTO t VALUES (1),(1),(2),(2),(2),(NULL);
CREATE TABLE u(v INT);
INSERT INTO u VALUES (1),(2),(NULL);

SELECT v FROM t INTERSECT ALL SELECT v FROM u;

-- Expected: {1,2}   (min multiplicity per side; pg + duckdb agree)
-- Actual   : {1,1,2,2,2}   (all LHS rows)
