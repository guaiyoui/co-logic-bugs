-- F10: Window frame-bound underflow -> SIGFPE + INTERNAL + silent garbage
--
-- Engine : DuckDB
-- Affects: 1.5.5 release (crashes / INTERNAL / silent garbage).
--          The minimal shape no longer crashes on main d8cdaa3 - likely
--          covered by upstream window patches (open draft PR #24560,
--          "Fix overflowing FOLLOWING and reject negative ROWS window
--          frame offsets", fixes #24307; residual of #24831 fixed by
--          #24933). Check the full stress corpus for residual shapes
--          before filing; report as release-version crash with the
--          fix confirmation.
-- Scale: 828k fuzz combos -> 10,670 hits = 1,836 SIGFPE (process kill,
--   rc=-8) + 2,866 INTERNAL cast-underflow + ~2,950 silent garbage.
--   Full report: docs/frame_fuzz_report.md
-- Root cause: ordered-argument window functions (in-function ORDER BY,
--   e.g. ntile(2 ORDER BY v)) evaluate `pos - frame_start` without
--   guarding frames that exclude the current row; the underflowed bound
--   escapes via SIGFPE (0/0 division), INTERNAL NumericCast of a wrapped
--   idx_t, or silently wrong values. The ordered-arg path also bypasses
--   the parser's frame-legality check.

CREATE TABLE t1(id INT, ch VARCHAR, v INT);
INSERT INTO t1 VALUES
  (1,'A',10),(2,'A',20),(3,'A',20),(4,'B',30),
  (5,'B',10),(6,'B',40),(7,'C',30),(NULL,'C',20);

-- (1) SIGFPE hard crash (process killed, core dump; rc=-8):
--   An ordinary FOLLOWING-only frame suffices; no inversion needed.
SELECT ntile(2 ORDER BY v) OVER
  (ORDER BY v ROWS BETWEEN 2 FOLLOWING AND 1 FOLLOWING) FROM t1;
-- Also: ntile(2 ORDER BY v) OVER (ORDER BY v ROWS BETWEEN 1 FOLLOWING
--   AND 2 FOLLOWING) crashes once the trailing rows' frames become
--   empty -> n_total = 0 -> 0/0 division.

-- (2) INTERNAL cast error (release):
SELECT ntile(2 ORDER BY ch) OVER
  (ORDER BY ch ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) FROM t1;
-- -> INTERNAL Error: Information loss on integer cast: 18446744073709551615
-- (percent_rank(ORDER BY c) / cume_dist over inverted or trailing-empty
--  frames hit the same cast-underflow.)

-- (3) Silent garbage - negative COUNT on same-direction inverted frames:
SELECT count(*) OVER
  (ORDER BY v ROWS BETWEEN 1 PRECEDING AND 5 PRECEDING) FROM t1;
-- Actual: 0,0,-1,-2,-3,... Correct: all 0 (empty frame).
-- debug_window_mode='separate'/'combine' return all-0 -> plan-dependent
-- wrong result.

-- (4) Silent garbage - row_number() <= 0 on a NORMAL frame (strongest
--     minimal shape; no inversion needed):
SELECT row_number(ORDER BY v) OVER
  (PARTITION BY ch ORDER BY v, id ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING)
FROM t1;
-- Actual: all 0s under default/separate/combine; all 1s under
-- disable_optimizer. The ordered-arg path computes a frame-relative
-- index that goes <= 0 whenever the current row is not in its frame.

-- Unaffected (verified): plain ntile(2) w/o in-func ORDER BY,
-- frame-ignoring funcs (lead/lag return partition answers correctly),
-- sum/min/first_value return correct NULLs.
