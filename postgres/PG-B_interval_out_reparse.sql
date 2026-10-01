-- PG-B: interval output produces a literal the server refuses to parse
--        back — breaks pg_dump restore (dump forces postgres style)
--
-- Engine : PostgreSQL 17.11 / 18.6 / master (20devel) — all reproduce.
-- Manifestation: serialization contract violation
--   (type_out output not accepted by type_in).  This is not a
--   formatting nitpick: pg_dump.c:1466 does
--       SET INTERVALSTYLE = POSTGRES
--   so a table holding the int64-min-microseconds interval dumps a
--   COPY literal that restore rejects with 22007 — the dump file of a
--   legal table cannot be restored.
--
-- Upstream: BUG #17371 is about interval_out's IMMUTABLE marking;
--   BUG #19670 is arithmetic overflow in timestamp+interval.  Neither
--   covers the output/input asymmetry reported here.  No matching
--   upstream report found.
--
-- Root cause (master src/backend/utils/adt/datetime.c):
--   * interval2itm() decomposes Interval.time (int64 usec) with
--     tm_hour = time / USECS_PER_HOUR  ->  -2562047788  (no bound).
--   * EncodeInterval POSTGRES branch prints i64abs(hour)
--     -> "-2562047788:00:54.775808".
--   * The literal tokenizes as DTK_TZ (leading sign + digits + ':').
--     DecodeTimeForInterval accumulates the MAGNITUDE as a positive
--     int64 via int64_multiply_add, then negates.  |INT64_MIN| = 2^63
--     has no positive int64 representation -> DTERR_FIELD_OVERFLOW
--     -> 22007.  The sign-then-accumulate order would fit exactly.
--   * Verbose branch: AddVerboseIntPart prints i64abs(INT32_MIN day)
--     -> "2147483648"; DecodeNumber reads days with strtoint (int32)
--     -> ERANGE -> 22015.
--
-- Expected: x::text::interval == x for every stored interval and style.
-- Actual  : 22007 / 22015 on the server's own output.

-- ==== case 1: int64-min microseconds, default 'postgres' style ====
SET intervalstyle = 'postgres';
SELECT interval '-9223372036854775808 microseconds'::text;
--  -> '-2562047788:00:54.775808'
SELECT '-2562047788:00:54.775808'::interval;
--  !! ERROR 22007 invalid input syntax for type interval

-- ==== case 2: int32-min days, postgres_verbose style ====
SET intervalstyle = 'postgres_verbose';
SELECT interval '-2147483648 days'::text;
--  -> '@ 2147483648 days ago'
SELECT '@ 2147483648 days ago'::interval;
--  !! ERROR 22015 interval field value out of range

-- ==== case 3: sql_standard style, same int64-min value ====
SET intervalstyle = 'sql_standard';
SELECT interval '-9223372036854775808 microseconds'::text;
--  -> '-2562047788:00:54.775808'   (same broken literal)
SELECT '-2562047788:00:54.775808'::interval;   -- 22007

-- ==== control: iso_8601 style round-trips (per-field signs) ====
SET intervalstyle = 'iso_8601';
SELECT interval '-9223372036854775808 microseconds'::text;
--  -> 'PT-2562047788H-54.775808S'
SELECT 'PT-2562047788H-54.775808S'::interval;   -- OK

-- ==== end-to-end: the pg_dump failure ====
--   CREATE TABLE ivt(i interval);
--   INSERT INTO ivt VALUES ('-9223372036854775808 microseconds');
--   pg_dump -t ivt -> COPY data line:  -2562047788:00:54.775808
--   restore -> ERROR 22007.
