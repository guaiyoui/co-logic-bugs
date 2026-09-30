-- DF-A: EXCEPT ALL drops duplicate multiplicity and NULL rows
--
-- Engine : DataFusion 54.0.0 (latest PyPI/crates, reverified round 3)
-- Manifestation: wrong result. DF evaluates ALL as a plain set-op.
-- References: duckdb 1.5.x, postgres 16.2, sqlite - all correct.

CREATE TABLE t(v INT);
INSERT INTO t VALUES (1),(1),(2),(2),(2),(NULL);
CREATE TABLE u(v INT);
INSERT INTO u VALUES (1),(2),(NULL);

SELECT v FROM t EXCEPT ALL SELECT v FROM u;

-- Expected: {1,2,2}   (pg + duckdb + sqlite agree)
-- Actual   : {}       (empty - ALL semantics ignored entirely)
--
-- Chained EXCEPT ALL collapses the same way.
