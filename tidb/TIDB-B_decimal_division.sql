-- TIDB-B: decimal division carries extra internal precision
--
-- Engine : TiDB v8.5.8, reference = MySQL 9.7.1, both at
--   @@div_precision_increment = 4.
-- Manifestation: wrong result (precision divergence). Compatibility
--   gap, low severity - frame as a compat divergence, not a hard
--   wrong-result. Related upstream: pingcap/tidb#21485, #51501
--   (div_precision_increment plumbing).

SET div_precision_increment = 4;   -- align both sides

SELECT 1.0/3.0*3.0;
-- TiDB : 1.000000
-- MySQL: 0.999990
--   TiDB's intermediate quotient keeps more digits than scale+4, so
--   (a/b)*b rounds back to a where MySQL's declared semantics truncate
--   the quotient to scale + div_precision_increment first.

SELECT 1.00/3.00*3.00;
-- TiDB : 1.00000000    MySQL: 0.99999900

-- Note: the displayed single quotient agrees (SELECT 1.0/3.0 -> 0.33333
-- on both) - only the carried internal precision differs.
