-- PG-G: pg_input_error_info disagrees with real coercion on 'bit'
--        input — different SQLSTATE/detail for the same bad literal
--        (diagnostic-only inconsistency, low severity)
--
-- Engine : PostgreSQL 18.6 / master (20devel) — consistent mismatch.
-- Manifestation: diagnostic inconsistency ONLY.  Validity verdicts
--   agree (both reject); pg_input_is_valid agrees with INSERT.
--   Only the error code / detail text differ.
--
-- Mechanism: two paths resolve typmod differently.
--   * INSERT 'unknown' literal -> bit_in with typmod = -1
--     (character validation runs FIRST: sees '2', throws 22P02).
--   * pg_input_error_info('25:00:00','bit') resolves 'bit' as bit(1),
--     typmod = 1 (length check runs FIRST -> 22026).
--
-- Expected: the soft-error API should report what the real coercion
--           path would report.
-- Actual  : different SQLSTATE / message for the same input.

CREATE TABLE bit_t(x bit);

INSERT INTO bit_t VALUES ('25:00:00');
--  !! 22P02: "2" is not a valid binary digit

SELECT pg_input_error_info('25:00:00', 'bit');
--  !! 22026: bit string length 8 does not match type bit(1)

-- verdict still agrees:
SELECT pg_input_is_valid('25:00:00', 'bit');   -- false
SELECT pg_input_is_valid('101', 'bit');        -- true

-- Severity: LOW — no wrong acceptance, no wrong result; only the
-- reported SQLSTATE/detail differs between the two paths.  Worth a
-- documentation note or typmod-alignment in the soft-error path.
