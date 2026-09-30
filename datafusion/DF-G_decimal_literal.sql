-- DF-G: high-precision numeric literal silently loses precision
--       through f64 before a DECIMAL cast
--
-- Engine : DataFusion 54.0.0
-- Manifestation: wrong result (silent). With the default
--   datafusion.sql_parser.parse_float_as_decimal=false, a decimal
--   literal with >17 significant digits (or integer literal > i64)
--   binds as Float64 before the cast.
-- Escape hatch (non-default):
--   SET datafusion.sql_parser.parse_float_as_decimal = true
--   restores exactness. The string path is also exact.

SELECT CAST(123456789012345678901234567890.123 AS DECIMAL(38,3));
-- Actual   : 123456789012345686040493921665.024   (wrong)
SELECT CAST('123456789012345678901234567890.123' AS DECIMAL(38,3));
-- Expected path (and pg/duckdb on the literal):
--            123456789012345678901234567890.123   (exact)

-- Repros cover 33-digit, 17-digit, scale-36 and >i64 integer literals;
-- all corrupted. Wrongness is silent - the same input parses exactly
-- via the string path.
