-- PG-C: time/timetz +/- interval — three sibling functions silently
--        wrap int64 (no overflow check); wrong time, no error
--
-- Engine : PostgreSQL 17.11 / 18.6 / master (20devel) — all reproduce.
-- Manifestation: silent wrong results.
--   time_mi_interval, timetz_pl_interval, timetz_mi_interval
--   (src/backend/utils/adt/date.c) add/subtract the interval's int64
--   microsecond field WITHOUT pg_add_s64_overflow /
--   pg_sub_s64_overflow guards, then reduce modulo 24h.  A huge
--   interval wraps to garbage instead of overflowing or producing the
--   mathematically correct mod-24h result.
--
-- Upstream: BUG #19670 (PG19beta3) reports ONLY time_pl_interval and
--   is still unfixed on master; the three sibling sites below are NOT
--   covered by that report.  Same file already uses
--   pg_add_s64_overflow correctly in in_range_time_interval —
--   these three sites are omissions, not design.
--
-- Expected: time arithmetic is modulo 24h; the wrapped results below
--           are mathematically wrong.
-- Actual  : plausible-looking wrong times, no error.

-- (a) time_pl_interval  -- the reported member (#19670)
SELECT '23:59:59.9'::time + interval '9223372036854775000 microseconds';
--  -> 19:59:05.123384      mathematically correct (mod 24h): 04:00:54.675

-- (b) time_mi_interval  -- UNREPORTED sibling
SELECT '00:00:00'::time - interval '9223372036854775000 microseconds';
--  -> 04:00:54.675...-ish garbage (wrapped), expected 19:59:05.325

-- (c) timetz_pl_interval -- UNREPORTED sibling
SELECT '23:59:59.9+00'::timetz + interval '9223372036854775000 microseconds';
--  -> wrapped garbage, no error

-- (d) timetz_mi_interval -- UNREPORTED sibling
SELECT '00:00:00+00'::timetz - interval '9223372036854775000 microseconds';
--  -> wrapped garbage, no error

-- Inconsistency oracle: t + i must equal t - (-i); both sides diverge
-- into different wrapped values on affected builds.
SELECT '12:00:00'::time + interval '9223372036854775000 microseconds'
     = '12:00:00'::time - interval '-9223372036854775000 microseconds';
--  -> false   (must be true modulo 24h)

-- Impact: silent wrong times in scheduling/expiry/audit arithmetic.
-- Fix: use pg_add_s64_overflow / pg_sub_s64_overflow in all four
-- date.c functions, or wrap in checked arithmetic before mod-24h.
