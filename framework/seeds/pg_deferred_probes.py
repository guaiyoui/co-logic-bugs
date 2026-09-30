"""Semantics probes for never-tested PostgreSQL surfaces.

Three families, all expressed as multi-statement scripts in ``query`` (the
runner ships the whole string through psycopg2's simple protocol, so only
the LAST statement's result set is captured):

A. DEFERRABLE constraints — FK / UNIQUE / PK / exclusion INITIALLY DEFERRED,
   SET CONSTRAINTS IMMEDIATE, savepoint interaction, trigger-vs-deferred
   ordering, and the documented no-DEFERRABLE-CHECK / no-FK-to-deferrable-PK
   restrictions.
B. Trigger ordering — generated-column recompute vs BEFORE triggers,
   alphabetical same-event ordering, partition-routing exactly-once firing,
   ON CONFLICT trigger paths, INSTEAD OF + RETURNING, exception/subtxn.
C. Row-Level Security under a NON-superuser — every prior probe ran as
   superuser (RLS inert).  Role-scoped work uses
   ``BEGIN; SET LOCAL ROLE <r>; ...; COMMIT;`` so the role auto-reverts at
   txn end even on error; role-visible results are parked in TEMP tables
   and read back as superuser after COMMIT.  (Session-level SET ROLE would
   leak; the aborted-txn residue is healed by the runner's reconnect.)

Oracle = documented/standard semantics.  ``buggy: "error_or_crash"`` is
used throughout: ``clean_error`` marks the EXPECTED error for violation
probes, ``buggy_error`` marks the predicted error signature when the bug
itself would surface as an error (e.g. an eager uniqueness check), and
``expected_rows`` catches silent wrong behavior.  ``affected`` is empty —
these are not recall probes; any "fired" is a candidate finding.

Conventions verified empirically on pg186_assert before encoding:
  - BEFORE ROW triggers run before ExecComputeStoredGenerated (18.6 src
    nodeModifyTable.c:911->935 / 2119->2152) => gencol sees post-trigger
    values.
  - Same-event triggers fire alphabetically by name; on partitions the
    parent's cloned row-triggers merge into the child's set.
  - ON CONFLICT DO UPDATE fires BEFORE INSERT -> BEFORE UPDATE -> AFTER
    UPDATE; non-conflict fires BEFORE INSERT -> AFTER INSERT.
  - RETURNING applies the SELECT policy to the inserted row.
  - Recreated ``public`` schema grants nothing to PUBLIC; RLS probes must
    ``GRANT USAGE ON SCHEMA public`` explicitly.
"""

PG_DEFERRED_PROBES = [
    # ============================================================
    # A. DEFERRABLE constraints
    # ============================================================
    {
        "name": "defer_fk_insert_order",
        "source": "FK DEFERRABLE INITIALLY DEFERRED: child inserted before "
                  "parent inside one txn is legal — check runs at COMMIT on "
                  "txn-final state. fired = eager per-statement check (FK "
                  "error signature) or lost/wrong rows.",
        "setup_sqls": [
            "CREATE TABLE p(id int PRIMARY KEY)",
            "CREATE TABLE c(pid int REFERENCES p(id) DEFERRABLE "
            "INITIALLY DEFERRED)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO c VALUES (5); INSERT INTO p VALUES (5); "
            "COMMIT; SELECT (SELECT count(*) FROM p WHERE id=5), "
            "(SELECT count(*) FROM c WHERE pid=5)"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1, 1]],
        "buggy_error": "violates foreign key constraint",
        "affected": {},
    },
    {
        "name": "defer_fk_orphan_commit",
        "source": "Deferred FK must fire at COMMIT: an orphan never resolved "
                  "in-txn must fail COMMIT with an FK violation. fired = "
                  "COMMIT silently succeeds (final SELECT reports the "
                  "orphan) or crash.",
        "setup_sqls": [
            "CREATE TABLE p(id int PRIMARY KEY)",
            "CREATE TABLE c(pid int REFERENCES p(id) DEFERRABLE "
            "INITIALLY DEFERRED)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO c VALUES (99); COMMIT; "
            "SELECT count(*) FROM c WHERE pid=99"
        ),
        "buggy": "error_or_crash",
        "clean_error": "violates foreign key constraint",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "defer_unique_swap",
        "source": "UNIQUE DEFERRABLE INITIALLY DEFERRED: a transient "
                  "duplicate inside the txn (value swap a<->b) is legal. "
                  "fired = eager uniqueness check error or wrong final "
                  "state.",
        "setup_sqls": [
            "CREATE TABLE u (id int PRIMARY KEY, v int UNIQUE DEFERRABLE "
            "INITIALLY DEFERRED)",
            "INSERT INTO u VALUES (1, 10), (2, 20)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; UPDATE u SET v = 20 WHERE id = 1; "
            "UPDATE u SET v = 10 WHERE id = 2; COMMIT; "
            "SELECT id, v FROM u ORDER BY id"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1, 20], [2, 10]],
        "buggy_error": "violates unique constraint",
        "affected": {},
    },
    {
        "name": "defer_pk_dup_resolved",
        "source": "DEFERRABLE PRIMARY KEY INITIALLY DEFERRED: a duplicate "
                  "created and removed inside one txn must commit. "
                  "(Deferrable PKs cannot be FK targets — tested separately "
                  "in defer_fk_to_deferrable_pk.) fired = eager check.",
        "setup_sqls": [
            "CREATE TABLE dp (id int PRIMARY KEY DEFERRABLE "
            "INITIALLY DEFERRED)",
            "INSERT INTO dp VALUES (1)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO dp VALUES (1); "
            "DELETE FROM dp WHERE ctid = (SELECT ctid FROM dp LIMIT 1); "
            "COMMIT; SELECT count(*) FROM dp"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1]],
        "buggy_error": "violates unique constraint",
        "affected": {},
    },
    {
        "name": "defer_fk_to_deferrable_pk",
        "source": "Documented restriction: an FK cannot reference a "
                  "DEFERRABLE PK/UNIQUE — 'cannot use a deferrable unique "
                  "constraint for referenced table'. fired = constraint "
                  "accepted (a deferrable-PK FK row would appear in "
                  "pg_constraint).",
        "setup_sqls": [
            "CREATE TABLE p (id int PRIMARY KEY DEFERRABLE "
            "INITIALLY DEFERRED)",
        ],
        "pre_sqls": [],
        "query": (
            "CREATE TABLE c(pid int REFERENCES p(id)); "
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'c'::regclass AND contype = 'f'"
        ),
        "buggy": "error_or_crash",
        "clean_error": "cannot use a deferrable unique constraint",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "defer_fk_pk_update",
        "source": "Deferred FK on the referencing side + PK UPDATE: "
                  "re-pointing the child in the same txn keeps txn-final "
                  "state consistent — legal. fired = eager check on the "
                  "PK update.",
        "setup_sqls": [
            "CREATE TABLE p(id int PRIMARY KEY)",
            "CREATE TABLE c(pid int REFERENCES p(id) DEFERRABLE "
            "INITIALLY DEFERRED)",
            "INSERT INTO p VALUES (1),(2)",
            "INSERT INTO c VALUES (1)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; UPDATE p SET id = 10 WHERE id = 1; "
            "UPDATE c SET pid = 10 WHERE pid = 1; COMMIT; "
            "SELECT (SELECT count(*) FROM p WHERE id=10), "
            "(SELECT count(*) FROM c WHERE pid=10)"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1, 1]],
        "buggy_error": "violates foreign key constraint",
        "affected": {},
    },
    {
        "name": "defer_set_immediate",
        "source": "SET CONSTRAINTS ALL IMMEDIATE mid-txn must run every "
                  "pending deferred check NOW — the orphan must surface "
                  "immediately as an FK violation, not wait for COMMIT. "
                  "fired = check skipped (orphan commits) or crash.",
        "setup_sqls": [
            "CREATE TABLE p(id int PRIMARY KEY)",
            "CREATE TABLE c(pid int REFERENCES p(id) DEFERRABLE "
            "INITIALLY DEFERRED)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO c VALUES (77); "
            "SET CONSTRAINTS ALL IMMEDIATE; COMMIT; "
            "SELECT count(*) FROM c WHERE pid=77"
        ),
        "buggy": "error_or_crash",
        "clean_error": "violates foreign key constraint",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "defer_trigger_order",
        "source": "An AFTER ROW trigger fires at statement time and sees "
                  "txn state THEN (parent still absent), while the deferred "
                  "FK check runs at COMMIT on the final state. Trigger must "
                  "log 'absent' and the txn must still commit. fired = "
                  "trigger deferred to commit-time (logs 'present') or "
                  "deferred check ran eagerly.",
        "setup_sqls": [
            "CREATE TABLE p(id int PRIMARY KEY)",
            "CREATE TABLE c(pid int REFERENCES p(id) DEFERRABLE "
            "INITIALLY DEFERRED)",
            "CREATE TABLE tlog(seen text)",
            "CREATE FUNCTION cf() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF EXISTS (SELECT 1 FROM p WHERE id = NEW.pid) THEN "
            "INSERT INTO tlog VALUES ('present'); ELSE "
            "INSERT INTO tlog VALUES ('absent'); END IF; RETURN NEW; END $$",
            "CREATE TRIGGER ct AFTER INSERT ON c FOR EACH ROW "
            "EXECUTE FUNCTION cf()",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO c VALUES (9); INSERT INTO p VALUES (9); "
            "COMMIT; SELECT (SELECT seen FROM tlog), "
            "(SELECT count(*) FROM p WHERE id=9)"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [["absent", 1]],
        "affected": {},
    },
    {
        "name": "defer_check_unsupported",
        "source": "CHECK constraints are NOT deferrable — 'misplaced "
                  "DEFERRABLE clause' SyntaxError on all current branches "
                  "(NOT ENFORCED exists since 18, but DEFERRABLE is still "
                  "rejected). fired = a build accepting a deferrable CHECK "
                  "(condeferrable row appears).",
        "setup_sqls": [],
        "pre_sqls": [],
        "query": (
            "CREATE TABLE dc (a int CHECK (a>0) DEFERRABLE); "
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'dc'::regclass AND condeferrable"
        ),
        "buggy": "error_or_crash",
        "clean_error": "misplaced DEFERRABLE clause",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "defer_fk_savepoint",
        "source": "SAVEPOINT rollback must re-arm the deferred check: the "
                  "parent insert that resolved the orphan is rolled back, "
                  "so COMMIT must fail. fired = check state lost across "
                  "subtxn rollback (orphan silently commits).",
        "setup_sqls": [
            "CREATE TABLE p(id int PRIMARY KEY)",
            "CREATE TABLE c(pid int REFERENCES p(id) DEFERRABLE "
            "INITIALLY DEFERRED)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO c VALUES (55); SAVEPOINT s; "
            "INSERT INTO p VALUES (55); ROLLBACK TO s; COMMIT; "
            "SELECT count(*) FROM c WHERE pid=55"
        ),
        "buggy": "error_or_crash",
        "clean_error": "violates foreign key constraint",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "defer_exclusion_violate",
        "source": "EXCLUDE ... DEFERRABLE INITIALLY DEFERRED: overlapping "
                  "ranges left at COMMIT must fail with an exclusion "
                  "violation. fired = silent commit of conflicting rows.",
        "setup_sqls": [
            "CREATE TABLE ex (r int4range, EXCLUDE USING gist (r WITH &&) "
            "DEFERRABLE INITIALLY DEFERRED)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO ex VALUES ('[1,5)'), ('[3,8)'); COMMIT; "
            "SELECT count(*) FROM ex"
        ),
        "buggy": "error_or_crash",
        "clean_error": "violates exclusion constraint",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "defer_exclusion_resolved",
        "source": "Deferred exclusion: transient overlap resolved inside "
                  "the txn is legal. fired = eager exclusion check.",
        "setup_sqls": [
            "CREATE TABLE ex (r int4range, EXCLUDE USING gist (r WITH &&) "
            "DEFERRABLE INITIALLY DEFERRED)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO ex VALUES ('[1,5)'), ('[3,8)'); "
            "DELETE FROM ex WHERE r = '[3,8)'; COMMIT; "
            "SELECT count(*) FROM ex"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1]],
        "buggy_error": "violates exclusion constraint",
        "affected": {},
    },
    # ============================================================
    # B. Trigger ordering
    # ============================================================
    {
        "name": "trig_gencol_order",
        "source": "BEFORE UPDATE trigger modifying NEW.a runs BEFORE the "
                  "stored generated column is recomputed "
                  "(ExecBRUpdateTriggers -> ExecComputeStoredGenerated), so "
                  "gen must reflect the post-trigger value (70*2=140), not "
                  "the pre-trigger one (7*2=14). fired = pre-trigger gen.",
        "setup_sqls": [
            "CREATE TABLE g (a int, gen int GENERATED ALWAYS AS (a*2) "
            "STORED)",
            "CREATE FUNCTION gf() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN NEW.a := NEW.a*10; RETURN NEW; END $$",
            "CREATE TRIGGER gt BEFORE UPDATE ON g FOR EACH ROW "
            "EXECUTE FUNCTION gf()",
            "INSERT INTO g (a) VALUES (5)",
        ],
        "pre_sqls": [],
        "query": "UPDATE g SET a = 7; SELECT a, gen FROM g",
        "buggy": "error_or_crash",
        "expected_rows": [[70, 140]],
        "affected": {},
    },
    {
        "name": "trig_after_alphabetical",
        "source": "Same-event/same-timing row triggers fire in alphabetical "
                  "order by trigger name (documented). fired = any other "
                  "order or missing/extra fires.",
        "setup_sqls": [
            "CREATE TABLE lt(x int)",
            "CREATE TABLE lg(n text)",
            "CREATE FUNCTION lf() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN INSERT INTO lg VALUES (TG_NAME); RETURN NULL; END $$",
            "CREATE TRIGGER z_tr AFTER INSERT ON lt FOR EACH ROW "
            "EXECUTE FUNCTION lf()",
            "CREATE TRIGGER a_tr AFTER INSERT ON lt FOR EACH ROW "
            "EXECUTE FUNCTION lf()",
            "CREATE TRIGGER m_tr AFTER INSERT ON lt FOR EACH ROW "
            "EXECUTE FUNCTION lf()",
        ],
        "pre_sqls": [],
        "query": "INSERT INTO lt VALUES (1); SELECT n FROM lg",
        "buggy": "error_or_crash",
        "expected_rows": [["a_tr"], ["m_tr"], ["z_tr"]],
        "affected": {},
    },
    {
        "name": "trig_partition_once",
        "source": "A row routed into a partition fires the partition's own "
                  "trigger plus the parent trigger's clone exactly once "
                  "each, alphabetically across the merged set (a_chd before "
                  "z_prt). The tp_2 row fires only the parent clone. "
                  "fired = missing, duplicated or mis-ordered fires.",
        "setup_sqls": [
            "CREATE TABLE tp (a int) PARTITION BY RANGE (a)",
            "CREATE TABLE tp_1 PARTITION OF tp FOR VALUES FROM (0) "
            "TO (1000)",
            "CREATE TABLE tp_2 PARTITION OF tp FOR VALUES FROM (1000) "
            "TO (2000)",
            "CREATE TABLE tlog(ev text)",
            "CREATE FUNCTION tf() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN INSERT INTO tlog VALUES (TG_NAME || '@' || "
            "TG_TABLE_NAME); RETURN NULL; END $$",
            "CREATE TRIGGER z_prt AFTER INSERT ON tp FOR EACH ROW "
            "EXECUTE FUNCTION tf()",
            "CREATE TRIGGER a_chd AFTER INSERT ON tp_1 FOR EACH ROW "
            "EXECUTE FUNCTION tf()",
        ],
        "pre_sqls": [],
        "query": "INSERT INTO tp VALUES (5), (1500); SELECT ev FROM tlog",
        "buggy": "error_or_crash",
        "expected_rows": [["a_chd@tp_1"], ["z_prt@tp_1"], ["z_prt@tp_2"]],
        "affected": {},
    },
    {
        "name": "trig_on_conflict_update",
        "source": "INSERT ... ON CONFLICT DO UPDATE fires BEFORE INSERT, "
                  "then BEFORE UPDATE and AFTER UPDATE on the conflict path "
                  "— no AFTER INSERT (documented). fired = extra AFTER "
                  "INSERT or missing update-path fires.",
        "setup_sqls": [
            "CREATE TABLE oc(id int PRIMARY KEY, v int)",
            "CREATE TABLE ocl(ev text)",
            "CREATE FUNCTION ocf() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN INSERT INTO ocl VALUES (TG_WHEN || ' ' || TG_OP); "
            "RETURN COALESCE(NEW, OLD); END $$",
            "CREATE TRIGGER oc_bi BEFORE INSERT ON oc FOR EACH ROW "
            "EXECUTE FUNCTION ocf()",
            "CREATE TRIGGER oc_ai AFTER INSERT ON oc FOR EACH ROW "
            "EXECUTE FUNCTION ocf()",
            "CREATE TRIGGER oc_bu BEFORE UPDATE ON oc FOR EACH ROW "
            "EXECUTE FUNCTION ocf()",
            "CREATE TRIGGER oc_au AFTER UPDATE ON oc FOR EACH ROW "
            "EXECUTE FUNCTION ocf()",
            "INSERT INTO oc VALUES (1, 0)",
        ],
        "pre_sqls": [],
        "query": (
            "DELETE FROM ocl; INSERT INTO oc VALUES (1, 9) "
            "ON CONFLICT (id) DO UPDATE SET v = EXCLUDED.v; "
            "SELECT ev FROM ocl"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [["BEFORE INSERT"], ["BEFORE UPDATE"],
                          ["AFTER UPDATE"]],
        "affected": {},
    },
    {
        "name": "trig_insteadof_returning",
        "source": "INSTEAD OF INSERT trigger on a view: RETURNING yields "
                  "the view's NEW row (5), not the base row the trigger "
                  "actually wrote (10). fired = base-row leak or missing "
                  "row.",
        "setup_sqls": [
            "CREATE TABLE bt(a int)",
            "CREATE VIEW bv AS SELECT a FROM bt",
            "CREATE FUNCTION bif() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN INSERT INTO bt VALUES (NEW.a*2); RETURN NEW; END $$",
            "CREATE TRIGGER bit INSTEAD OF INSERT ON bv FOR EACH ROW "
            "EXECUTE FUNCTION bif()",
        ],
        "pre_sqls": [],
        "query": "INSERT INTO bv VALUES (5) RETURNING a",
        "buggy": "error_or_crash",
        "expected_rows": [[5]],
        "affected": {},
    },
    {
        "name": "trig_exception_savepoint",
        "source": "A trigger RAISE EXCEPTION aborts its statement; a "
                  "plpgsql EXCEPTION block (subtxn) contains it and the txn "
                  "continues — the failed statement's effects (incl. the "
                  "trigger's own log write) roll back with the subtxn. "
                  "Expected: et=1, 'fired'=1 (v=1 only), 'caught'=1. "
                  "fired = exception escape, leaked partial effects, or "
                  "lost commit.",
        "setup_sqls": [
            "CREATE TABLE et(v int)",
            "CREATE TABLE elog(ev text)",
            "CREATE FUNCTION ef() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN INSERT INTO elog VALUES ('fired'); IF NEW.v = 2 THEN "
            "RAISE EXCEPTION 'boom'; END IF; RETURN NEW; END $$",
            "CREATE TRIGGER et_tr BEFORE INSERT ON et FOR EACH ROW "
            "EXECUTE FUNCTION ef()",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; INSERT INTO et VALUES (1); DO $$ BEGIN "
            "INSERT INTO et VALUES (2); EXCEPTION WHEN OTHERS THEN "
            "INSERT INTO elog VALUES ('caught'); END $$; COMMIT; "
            "SELECT (SELECT count(*) FROM et), "
            "(SELECT count(*) FROM elog WHERE ev='fired'), "
            "(SELECT count(*) FROM elog WHERE ev='caught')"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1, 1, 1]],
        "affected": {},
    },
    # ============================================================
    # C. Row-Level Security under non-superuser
    # ============================================================
    {
        "name": "rls_select_filter",
        "source": "USING(owner=current_user) must filter SELECT for the "
                  "non-superuser role (superuser sessions bypass RLS "
                  "entirely). fired = unfiltered rows visible.",
        "setup_sqls": [
            "CREATE USER rl1",
            "GRANT USAGE ON SCHEMA public TO rl1",
            "CREATE TABLE t(owner text, v int)",
            "INSERT INTO t VALUES ('rl1',1),('rl1',2),('x',3),('x',4),"
            "('x',5)",
            "GRANT SELECT ON t TO rl1",
            "ALTER TABLE t ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY tp ON t USING (owner = current_user)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl1; "
            "SELECT count(*) AS c INTO TEMP _rq1 FROM t; COMMIT; "
            "SELECT c FROM _rq1"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[2]],
        "affected": {},
    },
    {
        "name": "rls_delete_filter",
        "source": "DELETE under RLS removes only policy-visible rows; "
                  "invisible rows must survive untouched. fired = "
                  "invisible rows deleted.",
        "setup_sqls": [
            "CREATE USER rl2",
            "GRANT USAGE ON SCHEMA public TO rl2",
            "CREATE TABLE t(owner text, v int)",
            "INSERT INTO t VALUES ('rl2',1),('rl2',2),('x',3),('x',4),"
            "('x',5)",
            "GRANT DELETE ON t TO rl2",
            "ALTER TABLE t ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY tp ON t USING (owner = current_user)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl2; DELETE FROM t; COMMIT; "
            "SELECT count(*) FROM t"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[3]],
        "affected": {},
    },
    {
        "name": "rls_update_withcheck",
        "source": "USING(a<100) WITH CHECK(a<50): updating a visible row "
                  "to a check-failing value must raise the RLS violation. "
                  "fired = silent acceptance.",
        "setup_sqls": [
            "CREATE USER rl3",
            "GRANT USAGE ON SCHEMA public TO rl3",
            "CREATE TABLE ut(a int)",
            "GRANT SELECT, UPDATE ON ut TO rl3",
            "ALTER TABLE ut ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY up ON ut USING (a < 100) WITH CHECK (a < 50)",
            "INSERT INTO ut VALUES (10), (150)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl3; "
            "UPDATE ut SET a = 75 WHERE a = 10; COMMIT; "
            "SELECT count(*) FROM ut WHERE a = 75"
        ),
        "buggy": "error_or_crash",
        "clean_error": "violates row-level security",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "rls_update_invisible",
        "source": "Rows outside USING are invisible to UPDATE — a silent "
                  "no-op, not an error; the hidden row must remain "
                  "untouched. fired = hidden row modified or spurious "
                  "error.",
        "setup_sqls": [
            "CREATE USER rl4",
            "GRANT USAGE ON SCHEMA public TO rl4",
            "CREATE TABLE ut(a int)",
            "GRANT SELECT, UPDATE ON ut TO rl4",
            "ALTER TABLE ut ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY up ON ut USING (a < 100) WITH CHECK (a < 50)",
            "INSERT INTO ut VALUES (10), (150)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl4; "
            "UPDATE ut SET a = 20 WHERE a = 150; COMMIT; "
            "SELECT count(*) FROM ut WHERE a = 150"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1]],
        "affected": {},
    },
    {
        "name": "rls_qual_order_spy",
        "source": "Docs: the policy expression is 'evaluated for each row "
                  "prior to any conditions or functions coming from the "
                  "user's query' (leakproof functions excepted). A cheap "
                  "VOLATILE SECURITY DEFINER spy must never be invoked on "
                  "policy-excluded rows. fired = spy_log holds hidden rows "
                  "(qual-ordering info leak — the CVE-2017-7484 class).",
        "setup_sqls": [
            "CREATE USER rl5",
            "GRANT USAGE ON SCHEMA public TO rl5",
            "CREATE TABLE st(a int)",
            "CREATE TABLE spy_log(a int)",
            "CREATE FUNCTION spy(x int) RETURNS bool LANGUAGE plpgsql "
            "SECURITY DEFINER VOLATILE COST 1 AS $$ BEGIN "
            "INSERT INTO spy_log VALUES (x); RETURN true; END $$",
            "GRANT SELECT ON st TO rl5",
            "GRANT EXECUTE ON FUNCTION spy(int) TO rl5",
            "ALTER TABLE st ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY sp ON st USING (a < 100)",
            "INSERT INTO st SELECT g FROM generate_series(1,200) g",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl5; "
            "SELECT count(*) AS c INTO TEMP _rq5 FROM st WHERE spy(a); "
            "COMMIT; SELECT (SELECT c FROM _rq5), "
            "(SELECT count(*) FROM spy_log)"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[99, 99]],
        "affected": {},
    },
    {
        "name": "rls_error_leak",
        "source": "Non-foldable user qual 1/(a-150) must evaluate only on "
                  "policy-visible rows; evaluating it on the hidden a=150 "
                  "row would leak via division-by-zero. fired = "
                  "division-by-zero or other error (leak before filter).",
        "setup_sqls": [
            "CREATE USER rl6",
            "GRANT USAGE ON SCHEMA public TO rl6",
            "CREATE TABLE et(a int)",
            "GRANT SELECT ON et TO rl6",
            "ALTER TABLE et ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY ep ON et USING (a < 100)",
            "INSERT INTO et VALUES (50), (150)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl6; "
            "SELECT count(*) AS c INTO TEMP _rq6 FROM et "
            "WHERE 1/(a-150) IS NOT NULL; COMMIT; SELECT c FROM _rq6"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1]],
        "buggy_error": "division by zero",
        "affected": {},
    },
    {
        "name": "rls_barrier_view_spy",
        "source": "security_barrier view: its qual must be pushed inside "
                  "the barrier and evaluated before outside user quals — "
                  "the classic pre-RLS view leak shape. fired = spy sees "
                  "rows the barrier filters out.",
        "setup_sqls": [
            "CREATE USER rl7",
            "GRANT USAGE ON SCHEMA public TO rl7",
            "CREATE TABLE bt2(a int)",
            "CREATE TABLE spy_log2(a int)",
            "CREATE FUNCTION spy2(x int) RETURNS bool LANGUAGE plpgsql "
            "SECURITY DEFINER VOLATILE COST 1 AS $$ BEGIN "
            "INSERT INTO spy_log2 VALUES (x); RETURN true; END $$",
            "CREATE VIEW bv2 WITH (security_barrier) AS "
            "SELECT a FROM bt2 WHERE a < 100",
            "GRANT SELECT ON bv2 TO rl7",
            "GRANT EXECUTE ON FUNCTION spy2(int) TO rl7",
            "INSERT INTO bt2 SELECT g FROM generate_series(1,200) g",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl7; "
            "SELECT count(*) AS c INTO TEMP _rq7 FROM bv2 WHERE spy2(a); "
            "COMMIT; SELECT (SELECT c FROM _rq7), "
            "(SELECT count(*) FROM spy_log2)"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[99, 99]],
        "affected": {},
    },
    {
        "name": "rls_partition",
        "source": "A policy on the partitioned parent applies to scans "
                  "through the parent (quals pushed into the appendrel), "
                  "but NOT to direct access of a child partition — "
                  "per-relation RLS is the implemented semantic on every "
                  "version (documented footgun). fired = parent scan "
                  "unfiltered or child scan filtered by parent policy.",
        "setup_sqls": [
            "CREATE USER rl8",
            "GRANT USAGE ON SCHEMA public TO rl8",
            "CREATE TABLE pt(a int, o text) PARTITION BY RANGE (a)",
            "CREATE TABLE pt_1 PARTITION OF pt FOR VALUES FROM (0) "
            "TO (1000)",
            "INSERT INTO pt VALUES (1,'rl8'),(2,'other'),(3,'rl8')",
            "GRANT SELECT ON pt TO rl8",
            "GRANT SELECT ON pt_1 TO rl8",
            "ALTER TABLE pt ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY pp ON pt USING (o = current_user)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl8; "
            "SELECT count(*) AS c INTO TEMP _rq8a FROM pt; "
            "SELECT count(*) AS c INTO TEMP _rq8b FROM pt_1; COMMIT; "
            "SELECT (SELECT c FROM _rq8a), (SELECT c FROM _rq8b)"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[2, 3]],
        "affected": {},
    },
    {
        "name": "rls_insert_withcheck",
        "source": "FOR INSERT WITH CHECK (a<150): a violating insert must "
                  "raise 'new row violates row-level security policy'. "
                  "fired = silent acceptance.",
        "setup_sqls": [
            "CREATE USER rl9",
            "GRANT USAGE ON SCHEMA public TO rl9",
            "CREATE TABLE ct(a int)",
            "GRANT SELECT, INSERT ON ct TO rl9",
            "ALTER TABLE ct ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY cp ON ct FOR INSERT WITH CHECK (a < 150)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl9; INSERT INTO ct VALUES (200); "
            "COMMIT; SELECT count(*) FROM ct"
        ),
        "buggy": "error_or_crash",
        "clean_error": "violates row-level security",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "rls_returning_gate",
        "source": "RETURNING applies the SELECT policy to the inserted "
                  "row: an insert passing WITH CHECK (a<150) but not "
                  "SELECT-visible (a<100) must error — not echo the hidden "
                  "row back. fired = RETURNING leaks the row.",
        "setup_sqls": [
            "CREATE USER rl10",
            "GRANT USAGE ON SCHEMA public TO rl10",
            "CREATE TABLE ct(a int)",
            "GRANT SELECT, INSERT ON ct TO rl10",
            "ALTER TABLE ct ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY cp_sel ON ct FOR SELECT USING (a < 100)",
            "CREATE POLICY cp_ins ON ct FOR INSERT WITH CHECK (a < 150)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl10; "
            "WITH ins AS (INSERT INTO ct VALUES (120) RETURNING a) "
            "SELECT a INTO TEMP _rq10 FROM ins; COMMIT; "
            "SELECT a FROM _rq10"
        ),
        "buggy": "error_or_crash",
        "clean_error": "violates row-level security",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "rls_insert_returning",
        "source": "RETURNING under RLS echoes only the inserted row "
                  "(itself SELECT-visible). fired = base-table leak or "
                  "missing row.",
        "setup_sqls": [
            "CREATE USER rl11",
            "GRANT USAGE ON SCHEMA public TO rl11",
            "CREATE TABLE ct(a int)",
            "GRANT SELECT, INSERT ON ct TO rl11",
            "ALTER TABLE ct ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY cp_sel ON ct FOR SELECT USING (a < 100)",
            "CREATE POLICY cp_ins ON ct FOR INSERT WITH CHECK (a < 150)",
            "INSERT INTO ct VALUES (60), (170)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl11; "
            "WITH ins AS (INSERT INTO ct VALUES (50) RETURNING a) "
            "SELECT a INTO TEMP _rq11 FROM ins; COMMIT; "
            "SELECT a FROM _rq11"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[50]],
        "affected": {},
    },
    {
        "name": "rls_force_owner",
        "source": "FORCE ROW LEVEL SECURITY subjects the (non-superuser) "
                  "table owner to its own policies. fired = owner bypasses "
                  "policy (sees all 3 rows).",
        "setup_sqls": [
            "CREATE USER rl12",
            "GRANT USAGE ON SCHEMA public TO rl12",
            "CREATE TABLE ft(o text, v int)",
            "ALTER TABLE ft OWNER TO rl12",
            "INSERT INTO ft VALUES ('rl12',1),('x',2),('rl12',3)",
            "ALTER TABLE ft ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE ft FORCE ROW LEVEL SECURITY",
            "CREATE POLICY fp ON ft USING (o = current_user)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl12; "
            "SELECT count(*) AS c INTO TEMP _rq12 FROM ft; COMMIT; "
            "SELECT c FROM _rq12"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[2]],
        "affected": {},
    },
    {
        "name": "rls_policy_recursion",
        "source": "A policy whose USING references its own table must trip "
                  "the recursion guard — 'infinite recursion detected in "
                  "policy'. fired = unguarded recursion (crash/hang) or "
                  "returned rows.",
        "setup_sqls": [
            "CREATE USER rl13",
            "GRANT USAGE ON SCHEMA public TO rl13",
            "CREATE TABLE rt(a int)",
            "GRANT SELECT ON rt TO rl13",
            "ALTER TABLE rt ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY rp ON rt USING (EXISTS "
            "(SELECT 1 FROM rt t2 WHERE t2.a = rt.a))",
            "INSERT INTO rt VALUES (1)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl13; SELECT count(*) FROM rt; COMMIT"
        ),
        "buggy": "error_or_crash",
        "clean_error": "infinite recursion",
        "expected_rows": [[0]],
        "affected": {},
    },
    {
        "name": "rls_insert_select",
        "source": "INSERT ... SELECT under RLS: the source is filtered by "
                  "its own policy ({10} of {10,120,200}) and the target's "
                  "WITH CHECK gates what arrives. fired = unfiltered copy "
                  "or check bypass.",
        "setup_sqls": [
            "CREATE USER rl14",
            "GRANT USAGE ON SCHEMA public TO rl14",
            "CREATE TABLE src(a int)",
            "CREATE TABLE dst(a int)",
            "GRANT SELECT ON src TO rl14",
            "GRANT INSERT ON dst TO rl14",
            "ALTER TABLE src ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY sp2 ON src USING (a < 100)",
            "ALTER TABLE dst ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY dp ON dst FOR INSERT WITH CHECK (a < 150)",
            "INSERT INTO src VALUES (10), (120), (200)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl14; "
            "INSERT INTO dst SELECT a FROM src; COMMIT; "
            "SELECT count(*) FROM dst"
        ),
        "buggy": "error_or_crash",
        "expected_rows": [[1]],
        "affected": {},
    },
    {
        "name": "rls_insert_select_check",
        "source": "INSERT ... SELECT where a policy-visible source row "
                  "violates the target's WITH CHECK must error, and the "
                  "failed statement must leave nothing behind. fired = "
                  "silent partial insert.",
        "setup_sqls": [
            "CREATE USER rl15",
            "GRANT USAGE ON SCHEMA public TO rl15",
            "CREATE TABLE src(a int)",
            "CREATE TABLE dst(a int)",
            "GRANT SELECT ON src TO rl15",
            "GRANT INSERT ON dst TO rl15",
            "ALTER TABLE src ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY sp2 ON src USING (a < 100)",
            "ALTER TABLE dst ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY dp ON dst FOR INSERT WITH CHECK (a < 5)",
            "INSERT INTO src VALUES (10), (120), (200)",
        ],
        "pre_sqls": [],
        "query": (
            "BEGIN; SET LOCAL ROLE rl15; "
            "INSERT INTO dst SELECT a FROM src; COMMIT; "
            "SELECT count(*) FROM dst"
        ),
        "buggy": "error_or_crash",
        "clean_error": "violates row-level security",
        "expected_rows": [[0]],
        "affected": {},
    },
]
