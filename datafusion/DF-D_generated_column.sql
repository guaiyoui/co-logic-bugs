-- DF-D: GENERATED ALWAYS AS ... STORED silently returns NULL
--
-- Engine : DataFusion 54.0.0 (reverified round 3)
-- Manifestation: wrong result - the generated column parses without
--   error but every read returns NULL, so JOIN/WHERE silently lose
--   data. (duckdb rejects STORED generated cols outright - n/a; pg is
--   the reference.)

CREATE TABLE t(a INT, b INT GENERATED ALWAYS AS (a * 2) STORED);
INSERT INTO t(a) VALUES (1), (2), (3);

SELECT a, b FROM t ORDER BY a;

-- Expected (postgres): (1,2), (2,4), (3,6)
-- Actual (datafusion): (1,NULL), (2,NULL), (3,NULL)
--
-- The NULLs propagate: SELECT b FROM t WHERE b > 0 -> empty;
-- JOIN h ON t.b = h.b silently matches nothing.
