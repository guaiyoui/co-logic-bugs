-- MDB-B: merged FROM-source (view / derived table / CTE) x window or
--         "aggregate+groupcol" x quantified WHERE predicate -> empty set
--         (12 unreported manifestations of MDEV-40557 / MDEV-40780)
--
-- Engine : MariaDB 11.8.9 — deterministic; failure signature is always
--          the empty set (no partial rows, no wrong values).
--          PREPARE/EXECUTE binary protocol also reproduces.
--
-- Upstream: MDEV-40557 (window + ANY/ALL over a VIEW -> missing rows)
--   and MDEV-40780 (aggregate mixed with group column over a VIEW ->
--   0 rows) are both Confirmed/Unresolved — but only the VIEW form is
--   reported.  This family additionally fires on:
--     derived tables (FROM (SELECT ...) s) — derived_merge=off fixes,
--     CTEs, nested views, view JOIN base, the <> ANY operator,
--     multi-window, ROWS BETWEEN frames, LAG/NTILE,
--     SUM(id)+id aggregate-mixed variants, quantified predicate in the
--     outer HAVING/GROUP BY, SOME spelling.
--   For the VIEW path, derived_merge=off does NOT help (EXPLAIN shows
--   the view is still inlined) — both merge paths produce the same bug.
--
-- Exact trigger: merged FROM source x ANY/ALL quantified predicate in
--   WHERE (<,>,<=,>=,<> ; NOT =, IN, EXISTS) x target list containing a
--   window function or an aggregate+grouping-column expression.
--
-- Expected: same result as the identical query over the base table.
-- Actual  : empty set.

CREATE TABLE b2(id INT, v INT);
INSERT INTO b2 VALUES (1,10),(2,20),(3,30),(4,40);

-- derived-table form (not covered by the upstream reports):
SELECT AVG(id) OVER () FROM (SELECT id, v FROM b2) s
  WHERE s.id < ANY (SELECT id FROM b2);
--  -> empty set   (base table: 4 rows)   WRONG

-- view form (the reported MDEV-40557 shape, still unfixed):
CREATE VIEW v2 AS SELECT id, v FROM b2;
SELECT AVG(id) OVER () FROM v2 WHERE id < ANY (SELECT id FROM b2);
--  -> empty set   WRONG

-- <> ANY variant:
SELECT id FROM v2 WHERE id <> ANY (SELECT id FROM b2);
--  -> empty / wrong vs base

-- aggregate+groupcol form (MDEV-40780 shape):
SELECT SUM(id) + id FROM v2 GROUP BY id
  HAVING id < ANY (SELECT id FROM b2);
--  -> empty set   WRONG

-- Workaround (partial): SET optimizer_switch='derived_merge=off'
-- fixes the derived-table path only; the VIEW path stays broken.
