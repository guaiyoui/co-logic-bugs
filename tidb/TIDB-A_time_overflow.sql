-- TIDB-A: TIME overflow unchecked in the TiDB expression layer
--         (layer-semantic inconsistency)
--
-- Engine : TiDB v8.5.8 (tiup playground), reference = MySQL 9.7.1.
--   TiDB self-declares MySQL 8.0 compat, so a divergence on shared
--   syntax is a candidate bug rather than a majority vote.
-- Manifestation: wrong result + internal inconsistency.
-- Upstream: pingcap/tidb#56865 - still reproduces on v8.5.8; report
--   as "still unfixed + sharper inconsistency witness".

-- Headline: documented max TIME is 838:59:59
SELECT TIME '838:59:59' + INTERVAL 1 HOUR;
-- TiDB : '839:59:59'   (out-of-range value returned, no warning)
-- MySQL: NULL + warning 6527

-- Sharper witness - the SAME expression is NULL in the TiKV-pushed
-- WHERE yet non-NULL out-of-range in the TiDB-layer projection:
CREATE TABLE tt(col1 TIME);
INSERT INTO tt VALUES ('838:59:59');

SELECT col1 + INTERVAL 10 HOUR FROM tt
WHERE (col1 + INTERVAL 10 HOUR) IS NULL;
-- TiDB : returns the row with '848:59:59'  <- a row matching IS NULL
--        comes back carrying a non-NULL value
-- MySQL: empty (NULL consistently in both positions)

-- Related: ADDTIME clamps on MySQL but overflows on TiDB:
SELECT ADDTIME('838:59:59', '1:00:00');
-- TiDB : '839:59:59'   MySQL: '838:59:59' (clamped)
-- (ADDTIME is evaluated in the TiDB layer both ways, so it shows only
--  the out-of-range half.)
