-- DF-J: lateral UNNEST in FROM is unplannable
--
-- Engine : DataFusion 54.0.0
-- Manifestation: unplannable-error on legal SQL. pg + duckdb execute
--   all forms ({1,10},{1,20},{2,30}); only the projection form
--   SELECT id, unnest(l) FROM t works in DF -> lateral path is the gap.

CREATE TABLE t(id INT, l INT[]);
INSERT INTO t VALUES (1, [10, 20]), (2, [30]);

SELECT t.id, e.c FROM t, UNNEST(t.l) AS e(c);
-- -> Physical plan does not support logical expression
--    OuterReferenceColumn(Field { name: "l" ... })

-- Bare-alias form binds no usable column:
SELECT t.id, e FROM t, UNNEST(t.l) AS e;
-- -> No field named e (valid: e."UNNEST(outer_ref(t.l))")

-- Other lateral forms, all unsupported:
--   UNNEST(...) WITH ORDINALITY        -> not supported yet
--   CROSS JOIN LATERAL UNNEST(...)     -> table function 'unnest' not found
