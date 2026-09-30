-- F1: Correlated EXISTS self-join drops rows (deliminator + filter_pushdown)
--
-- Engine : DuckDB
-- Affects: 1.0.0, 1.1.3, 1.5.5 (release) AND main d8cdaa3 (2026-09-15,
--          dev-confirmed). Longstanding.
-- Upstream: adjacent to duckdb/duckdb#22267 (closed - covered the
--           derived-table scope only); this is the residual
--           EXISTS-correlation path. Same surface as known bug #23979
--           (fixed by PR #24235, not yet in a release).
-- Manifestation: wrong result (rows silently dropped).
--
-- Repro class: SELECT * FROM t a WHERE EXISTS
--   (SELECT 1 FROM t b WHERE b.x=a.x AND b.y<>a.y AND b.z>a.z)
-- TLP triple-partition gives count 6 vs expected 5; disabling
-- deliminator + filter_pushdown restores the correct result.
--
-- Canonical repro below is the concrete #23979-style witness:
-- the two EXISTS expressions are logically equivalent rewrites and
-- must return the same truth value per row; under the bug they differ.

CREATE TABLE issue23979_l(o INTEGER PRIMARY KEY);
INSERT INTO issue23979_l VALUES (1), (2);
CREATE TABLE issue23979_r(o INTEGER PRIMARY KEY, wh INTEGER NOT NULL);
INSERT INTO issue23979_r VALUES (1, 1);
CREATE TABLE issue23979_s2(o INTEGER, wh INTEGER);
INSERT INTO issue23979_s2 VALUES (1, 2), (2, 3);

SELECT
    EXISTS (SELECT 1 FROM issue23979_s2 x
            WHERE x.o = l.o AND r.wh <> x.wh)            AS correlated_exists,
    EXISTS (SELECT 1 FROM issue23979_s2 x
            WHERE x.o = l.o AND (r.wh + 0) <> x.wh)      AS equivalent_rewrite
FROM issue23979_l l
LEFT JOIN issue23979_r r USING (o)
JOIN issue23979_s2 s2 USING (o)
ORDER BY l.o;

-- Expected (postgres 16.2, and duckdb with the two optimizers off):
--   correlated_exists | equivalent_rewrite
--   ------------------+-------------------
--   true              | true      (r.wh=1, x.wh=2: 1<>2 legit)
--   false             | false     (r.wh=NULL -> NULL<>3 is NULL)
--
-- Actual on buggy versions: second row returns (true, false) - the
-- plain `r.wh <> x.wh` EXISTS evaluates the NULL comparison as TRUE
-- while the equivalent rewrite correctly returns false. Equivalent
-- expressions in the same query disagree.
--
-- Oracle check: SET disabled_optimizers='deliminator,filter_pushdown'
-- restores the correct result.
