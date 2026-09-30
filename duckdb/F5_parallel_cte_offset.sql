-- F5: CTE + OFFSET parallel row-drop (flaky parallel executor race)
--
-- Engine : DuckDB
-- Affects: flaky since ~1.1.3, worse in 1.5.5.
--   40 fresh-connection runs: 1.0.0 -> 40/40 correct;
--   1.1.3 -> 38/40; 1.5.5 -> 33/40.  threads=1 eliminates failures.
-- Manifestation: flaky wrong result (all rows dropped at ~10-37% rate
--   across mutants). Flaky bugs are legitimate reports when a rate and
--   a repeat harness are attached - see note at bottom.

CREATE TABLE ordered_probe(ord INT, flag INT);
INSERT INTO ordered_probe SELECT range, range % 2 FROM range(10);
CREATE TABLE boolean_keys(flag_key INT);
INSERT INTO boolean_keys VALUES (0), (1);

WITH c AS (SELECT flag FROM ordered_probe ORDER BY ord OFFSET 2)
SELECT * FROM c INNER JOIN boolean_keys ON flag <= flag_key;

-- Expected: 8 rows (flags of rows 2..9 = {0,1,0,1,0,1,0,1}, each <= some
--   flag_key). Actual under parallel default: frequently returns [].
-- SET threads=1 makes it deterministic-correct.

-- ---- repeat harness (python) -------------------------------------------
-- import duckdb
-- fails = 0
-- for _ in range(40):
--     con = duckdb.connect()          # fresh connection per run
--     con.execute("CREATE TABLE ordered_probe(ord INT, flag INT)")
--     con.execute("INSERT INTO ordered_probe SELECT range, range % 2 FROM range(10)")
--     con.execute("CREATE TABLE boolean_keys(flag_key INT)")
--     con.execute("INSERT INTO boolean_keys VALUES (0),(1)")
--     n = len(con.execute(
--         "WITH c AS (SELECT flag FROM ordered_probe ORDER BY ord OFFSET 2) "
--         "SELECT * FROM c INNER JOIN boolean_keys ON flag <= flag_key").fetchall())
--     fails += (n != 8)
--     con.close()
-- print(f"{fails}/40 runs returned wrong row count")   # ~7/40 on 1.5.5
