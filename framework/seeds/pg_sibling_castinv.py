"""Sibling-mutation probes around the cast-invalidation stale-plan bug
(Samokhvalov 2026-09, pgsql-hackers): PREPAREd plan for $1::int is not
invalidated when the cast's underlying function is swapped ->
EXECUTE returns the stale result.  FLAKY upstream — the stale plan
survives iff the sinval catchup has not run between CREATE CAST and
EXECUTE.

These cases quantify the race and probe the invalidation window:
  * verbatim (autocommit drop+create as separate statements),
  * drop+create inside ONE multi-statement (implicit txn),
  * explicit BEGIN/COMMIT around the swap,
  * an intervening SELECT 1 (sinval catchup),
  * pg_sleep between swap and execute,
  * EXECUTE in the gap between DROP and CREATE,
  * DEALLOCATE+PREPARE (replan control -> expected clean 101),
  * DISCARD PLANS control,
  * CREATE OR REPLACE FUNCTION instead of cast swap (Tom Lane's
    function-overload sibling: same OID, new body -> inlined SQL
    function body may stay stale in the cached plan).

cast_repro schema is dropped inside setup_sqls (public schema reset
alone does not remove it).
"""

SETUP = [
    "DROP SCHEMA IF EXISTS cast_repro CASCADE",
    "CREATE SCHEMA cast_repro",
    "CREATE TYPE cast_repro.key_t AS (v int)",
    "CREATE FUNCTION cast_repro.cast_old(cast_repro.key_t) "
    "RETURNS int LANGUAGE sql IMMUTABLE STRICT AS 'select ($1).v'",
    "CREATE FUNCTION cast_repro.cast_new(cast_repro.key_t) "
    "RETURNS int LANGUAGE sql IMMUTABLE STRICT AS 'select ($1).v + 100'",
    "CREATE CAST (cast_repro.key_t AS int) "
    "WITH FUNCTION cast_repro.cast_old(cast_repro.key_t) AS IMPLICIT",
]
SP = "SET search_path = cast_repro, pg_catalog"
# drop any leftover prepared statement from a prior case
DA = "DEALLOCATE ALL"


def _case(name, source, pre, query="EXECUTE q(row(1)::key_t)"):
    return {
        "name": name,
        "source": source,
        "setup_sqls": SETUP,
        "pre_sqls": pre,
        "query": query,
        "buggy": "wrong_rows",
        "expected_rows": [[101]],
        "buggy_marker": "stale cached plan => returns 1",
        "affected": {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                     20: (0, 0)},
    }


PROBES = [
    _case("castinv_verbatim",
          "verbatim: autocommit DROP CAST; CREATE CAST as separate "
          "statements — flaky upstream.",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "DROP CAST (key_t AS int)",
           "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
           "AS IMPLICIT"]),
    _case("castinv_implicit_txn",
          "DROP+CREATE in ONE multi-statement execute -> single implicit "
          "txn; sinval handled at its commit boundary.",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "DROP CAST (key_t AS int); "
           "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
           "AS IMPLICIT"]),
    _case("castinv_explicit_txn",
          "BEGIN; DROP; CREATE; COMMIT -> invalidations processed at "
          "COMMIT; EXECUTE after.",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "BEGIN",
           "DROP CAST (key_t AS int)",
           "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
           "AS IMPLICIT",
           "COMMIT"]),
    _case("castinv_catchup_select1",
          "intervening SELECT 1 between swap and EXECUTE -> forces "
          "CatchupInterrupt sinval processing -> expected clean.",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "DROP CAST (key_t AS int)",
           "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
           "AS IMPLICIT",
           "SELECT 1"]),
    _case("castinv_sleep",
          "pg_sleep(0.2) between swap and EXECUTE.",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "DROP CAST (key_t AS int)",
           "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
           "AS IMPLICIT",
           "SELECT pg_sleep(0.2)"]),
    _case("castinv_exec_in_gap",
          "EXECUTE between DROP and CREATE — plan should still serve "
          "the dropped cast's function (proves staleness window opens "
          "at DROP, not CREATE).",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "DROP CAST (key_t AS int)",
           "EXECUTE q(row(1)::key_t)",
           "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
           "AS IMPLICIT"]),
    _case("castinv_reprepare",
          "DEALLOCATE + PREPARE after the swap -> fresh plan must use "
          "the new cast: expected clean 101.",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "DROP CAST (key_t AS int)",
           "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
           "AS IMPLICIT",
           "DEALLOCATE q",
           "PREPARE q(key_t) AS SELECT $1::int"]),
    _case("castinv_discard_plans",
          "DISCARD PLANS after the swap -> cache flushed: expected "
          "clean 101.",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "DROP CAST (key_t AS int)",
           "CREATE CAST (key_t AS int) WITH FUNCTION cast_new(key_t) "
           "AS IMPLICIT",
           "DISCARD PLANS"]),
    _case("castinv_func_replace",
          "Tom Lane sibling: CREATE OR REPLACE FUNCTION keeps the "
          "function OID (no cast DDL at all) — cached plan inlined "
          "the old SQL body -> stale result if not invalidated.",
          [DA, SP, "PREPARE q(key_t) AS SELECT $1::int",
           "EXECUTE q(row(1)::key_t)",
           "CREATE OR REPLACE FUNCTION cast_repro.cast_old"
           "(cast_repro.key_t) RETURNS int LANGUAGE sql IMMUTABLE "
           "STRICT AS 'select ($1).v + 100'"]),
]
