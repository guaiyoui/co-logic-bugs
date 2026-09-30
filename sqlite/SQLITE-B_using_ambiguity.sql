-- SQLITE-B: USING/NATURAL ambiguity check is direction- and
--           operand-asymmetric
--
-- Engine : SQLite 3.51.2 (deterministic 10/10 fresh connections)
-- Manifestation: inconsistent ambiguity enforcement - under-rejection:
--   ambiguous USING silently binds the FIRST duplicate column.
--   SQL spec + PostgreSQL 16.2 + DuckDB 1.5.x all reject every form
--   ("common column name x appears more than once in left/right table").
--
-- Setup: three tables; the operand (a J b) has TWO columns named x.

CREATE TABLE a(x INT, y INT);
INSERT INTO a VALUES (1, 10);
CREATE TABLE b(x INT, z INT);
INSERT INTO b VALUES (1, 20);
CREATE TABLE c(x INT, w INT);
INSERT INTO c VALUES (1, 30);

-- (a) Rejected - strict check fires on the LEFT operand of a USING join
--     when a RIGHT/FULL join appears in the chain:
SELECT count(*) FROM a RIGHT JOIN b ON a.x = b.x JOIN c USING(x);
-- -> ambiguous reference to x in USING()

-- (b) Silently accepted - same ambiguous operand on the RIGHT:
SELECT count(*) FROM c JOIN (b RIGHT JOIN a ON a.x = b.x) USING(x);
-- -> 1   (binds the first x; PG/DuckDB reject)

-- (c) Silently accepted - same ambiguity through a subquery operand:
SELECT count(*) FROM (SELECT a.x, b.x FROM a JOIN b ON a.x = b.x)
  RIGHT JOIN c USING(x);
-- -> 2

-- (d) Silently accepted - INNER/LEFT-only chain, no RIGHT/FULL:
SELECT count(*) FROM a JOIN b ON a.x = b.x JOIN c USING(x);
-- -> 1

-- Boundary (verified): the strict check fires only on the LEFT operand
-- of a USING/NATURAL join, only when that operand is a flattened join
-- tree AND some RIGHT/FULL join appears in the chain (INNER+RIGHT,
-- INNER+FULL, RIGHT+INNER, FULL+INNER, FULL+LEFT, LEFT+RIGHT,
-- LEFT+FULL all err; INNER+INNER, INNER+LEFT accepted). NATURAL
-- inherits the same asymmetry.
--
-- Same feature area as SQLITE-A (RIGHT/FULL name resolution) but the
-- inverse direction of wrongness: A over-rejects legal SQL,
-- B under-rejects illegal SQL. Report as one cluster.
