-- PG-F: Memoize cache key omits the join parameter -> wrong results
--        (param-in-key family)
--
-- Engine : PostgreSQL 16.2 / 17.11 / 18.6 / unpatched master — wrong
--          result.  master + CF7175 patch (ee2bbec) is correct
--          (verified A/B; same fuzzer 0 divergences after patch).
--
-- Mechanism: the Memoize node keys its cache on the evaluated join-key
--   expression (t2.hundred + t0.ten).  Different (t2.hundred, t0.ten)
--   pairs collide on the same key, while the `t1.twenty = t0.ten`
--   filter result is cached under the FIRST param value — subsequent
--   params reuse the stale cache entry -> wrong row counts.
--
-- Upstream: pgsql-hackers "query returns different result with and
--   without memoization" (Jacob Brazeal); fix ee2bbec under CF 7175,
--   not yet merged into any release at time of verification.
--
-- Expected: memoize on/off must return identical results.
-- Actual  : memoize on returns stale counts.

CREATE TABLE t0(ten int PRIMARY KEY);
CREATE TABLE t1(unique1 int, hundred int, twenty int);
CREATE TABLE t2(hundred int, x int);
CREATE INDEX t1u ON t1(unique1);
CREATE INDEX t2h ON t2(hundred);
INSERT INTO t0 SELECT generate_series(0,9);
INSERT INTO t1 SELECT i, i%10, i%5 FROM generate_series(0,999) i;
INSERT INTO t2 SELECT i%10, i  FROM generate_series(0,999) i;

SET enable_seqscan = off;
SET enable_mergejoin = off;
SET enable_hashjoin = off;
SET work_mem = '64kB';

SET enable_memoize = on;
SELECT t0.ten, (SELECT count(*) FROM t1 JOIN t2
    ON t2.hundred + t0.ten = t1.unique1    -- param buried in key expr
    WHERE t1.twenty = t0.ten)              -- same param, second site
  FROM t0 ORDER BY 1;
-- memoize ON : (1,100)...(5,100)     <- wrong (stale cache hit)

SET enable_memoize = off;
SELECT t0.ten, (SELECT count(*) FROM t1 JOIN t2
    ON t2.hundred + t0.ten = t1.unique1
    WHERE t1.twenty = t0.ten)
  FROM t0 ORDER BY 1;
-- memoize OFF: (1,200)...(4,200),(5..9,0)  <- correct

-- Variants verified (all diverge unpatched, all fixed by ee2bbec):
--   param in join qual (t1.twenty - p = 0),
--   param in inner reltarget (sum(unique1 - p*0)).
-- Sites that do NOT diverge (param flushed correctly): unpulled
-- subplan IN(...), HAVING min(w)=p, join-clause-inner param only.
