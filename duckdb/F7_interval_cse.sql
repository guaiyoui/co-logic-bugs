-- F7: Interval equality is not a congruence - CSE substitutes a wrong
--     result into the second column
--
-- Engine : DuckDB
-- Affects: 1.0.0, 1.1.3, 1.5.5 AND main d8cdaa3 (2026-09-15,
--          dev-confirmed). Longstanding.
-- Manifestation: wrong result.
--
-- Mechanism: INTERVAL '30 days' = INTERVAL '1 month' under DuckDB's
-- normalized interval equality, so CSE dedupes the two d+interval
-- expressions and substitutes ONE result into both output slots.
-- But month arithmetic clamps to month-end while day arithmetic does
-- not -> the two expressions are NOT substitutable. DuckDB's own docs
-- warn that the two interval forms differ under addition, i.e. the
-- substitutability precondition is violated per its own spec.
-- No matching upstream issue found.

CREATE TABLE t(d DATE);
INSERT INTO t VALUES (DATE '2023-02-28'), (DATE '2024-01-31'), (DATE '2024-02-29');

SELECT d + INTERVAL '1 month' AS a, d + INTERVAL '30 days' AS b
FROM t ORDER BY d;

-- Actual (buggy):   a and b are EQUAL - b carries a's value:
--   2023-02-28 -> a=2023-03-28, b=2023-03-28   (b should be 2023-03-30)
--   2024-01-31 -> a=2024-02-29, b=2024-02-29   (b should be 2024-03-01)
--   2024-02-29 -> a=2024-03-29, b=2024-03-29   (b should be 2024-03-30)
--
-- Correct (SET disabled_optimizers='common_subexpressions,expression_rewriter'
-- or Postgres): month-arith clamps to month end, day-arith does not.
--
-- One-line witness on main:
--   SELECT DATE '2023-02-28' + INTERVAL '30 days';   -- 2023-03-28 (wrong)
--   -- alone it is fine; the wrong value appears when both interval
--   -- forms co-occur in one projection:
--   SELECT d + INTERVAL '1 month' a, d + INTERVAL '30 days' b FROM t;
--
-- Also fires for d - INTERVAL. The month<->day boundary is the
-- non-congruent case (30d = 1mo normalized, but month-arithmetic
-- clamps to month-end).
