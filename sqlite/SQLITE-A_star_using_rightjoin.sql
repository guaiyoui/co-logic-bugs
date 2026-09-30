-- SQLITE-A: SELECT * over a USING-join + RIGHT/FULL JOIN -> spurious
--           "ambiguous column name"
--
-- Engine : SQLite 3.51.2 (deterministic 10/10 fresh connections)
-- Manifestation: prepare-time over-rejection of legal SQL.
--   PostgreSQL executes the same query (SQLite docs claim RIGHT/FULL
--   JOIN uses the same handling as Postgres).
--   Fires iff the right-side table shares the merged column name.
--   LEFT/INNER joins, count(*), qualified a.x, and
--   RIGHT JOIN c USING(x) all work.

CREATE TABLE a(x INT, y INT);
INSERT INTO a VALUES (1, 10);
CREATE TABLE b(x INT, z INT);
INSERT INTO b VALUES (1, 20);
CREATE TABLE c(x INT, w INT);
INSERT INTO c VALUES (1, 30);

SELECT * FROM a JOIN b USING(x) RIGHT JOIN c ON true;
-- -> Error: ambiguous column name: x        (at prepare time)
--
-- Works (controls):
--   SELECT * FROM a JOIN b USING(x) LEFT JOIN c ON true;
--   SELECT count(*) FROM a JOIN b USING(x) RIGHT JOIN c ON true;
--   SELECT a.x FROM a JOIN b USING(x) RIGHT JOIN c ON true;
--   SELECT * FROM a JOIN b USING(x) RIGHT JOIN c USING(x);
--   -- right table with a different column name also works.
