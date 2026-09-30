-- F11: MERGE WHEN NOT MATCHED BY SOURCE without an INSERT branch ->
--      INTERNAL bind error under build_side_probe_side
--
-- Engine : DuckDB 1.5.5 release
-- Manifestation: internal-error
--   "INTERNAL Error: Failed to bind column reference \"rowid\""
-- Residual of fixed issue #20991: the default-plan shape was patched
-- but the swapped-sides plan was not covered (OHR evidence -
-- orthogonal-to-fix residual).
-- Deterministic, bisected to the single optimizer
-- build_side_probe_side.

CREATE TABLE t(a INT, b INT);
INSERT INTO t VALUES (1,10),(2,20);
CREATE TABLE s(a INT, b INT);
INSERT INTO s VALUES (1,100);

-- Default plan: works on 1.5.5 (the #20991 default-path fix covers it).
MERGE INTO t USING s ON t.a = s.a
WHEN NOT MATCHED BY SOURCE THEN DELETE;

-- Same statement under the swapped join sides -> INTERNAL:
SET disabled_optimizers = 'build_side_probe_side';
MERGE INTO t USING s ON t.a = s.a
WHEN NOT MATCHED BY SOURCE THEN DELETE;
-- -> INTERNAL Error: Failed to bind column reference "rowid"
-- Also fires with WHEN NOT MATCHED BY SOURCE THEN UPDATE and any
-- MERGE that lacks a WHEN NOT MATCHED THEN INSERT clause.
