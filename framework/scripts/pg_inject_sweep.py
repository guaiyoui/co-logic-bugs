#!/usr/bin/env python3
"""Injection-point error/wait sweep on pgmaster_inject (PostgreSQL 20devel,
--enable-injection-points).

For each (point, workload, action) triple:

  error     attach(point,'error') on the session that will reach the point,
            run the op, expect a clean "error triggered for injection point"
            ERROR.  Post-failure invariants: server reachable, no unexpected
            TRAP/PANIC/assert in the log, catalog coherence, retry works.
            Points inside critical sections legitimately PANIC (e.g.
            multixact-create-from-members) -> classified 'documented-panic'
            if crash recovery comes back cleanly.
            Points post-commit (transaction-end-process-inval,
            invalidate-catalog-snapshot-end) mask the commit -> classified
            'commit-masked' (txn committed but client saw ERROR).

  wake      attach(point,'wait'); op parks at the point (wait_event_type =
            'InjectionPoint'); injection_points_wakeup releases it; op must
            complete cleanly.

  cancel    park at the point then pg_cancel_backend; expect
            "canceling statement due to user request" and clean invariants.

  terminate park at the point then pg_terminate_backend; backend death is
            EXPECTED (not a hit); server + catalogs must stay coherent.

  immediate park at the point then postmaster -m immediate; restart; crash
            recovery must bring the cluster back with coherent catalogs.

A 'hit' = assert/TRAP/PANIC not whitelisted by the point's class, a wedge
that survives cancel+terminate, unexpected connection death, or a
post-failure invariant violation (orphan transient rels beyond the
documented REINDEX-CONCURRENTLY ccnew/ccold remnants, pg_index incoherence,
amcheck failure, data divergence, retry failure).

Harness rules honored (from prior repack probe work):
  * every session gets statement_timeout=0;
  * a psycopg2 connection is never shared between a blocked thread and
    control queries - each thread/session owns its conn; engine control
    queries (pg_stat_activity polls, wakeup, cancel, detach) run on the
    dedicated `mon` conn, while `ctl` belongs to the workload;
  * waiters are released with wakeup_retry (a wakeup sent before the
    backend registers its wait is lost, so retry until the thread ends).

Results: --out dir gets results.json (every case) + SUMMARY.md.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from util.paths import pg_build_prefix  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402

PG_PREFIX = pg_build_prefix("pgmaster_inject")
OUT_DIR = Path(__file__).resolve().parent.parent / "results" / "pg_inject_sweep"

# log lines that are expected noise for crash-style actions
_CRASH_NOISE = re.compile(
    r"immediate shutdown request|fast shutdown|"
    r"terminating any other active|was not properly shut down|"
    r"was interrupted|crash recovery|redo (starts|done)|"
    r"consistent recovery state|recovery has paused|"
    r"database system is ready|PANIC:|was terminated by signal|"
    r"all server processes terminated|reinitializing|"
    r"automatic recovery in progress|recovery in progress",
    re.IGNORECASE)


# ------------------------------------------------------------------ helpers


def _exec(conn, sql, fetch=False):
    cur = conn.cursor()
    cur.execute(sql)
    if fetch and cur.description is not None:
        return cur.fetchall()
    return None


def wait_for_injection_point(conn, name, timeout_s=60.0, thread=None):
    """Poll pg_stat_activity until a backend waits in the named point.

    If `thread` is given, bail out early when the op thread finishes without
    ever parking (point genuinely not reached) - keeps best-effort points
    from burning the full timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            rows = _exec(
                conn,
                "SELECT pid FROM pg_stat_activity "
                "WHERE wait_event_type = 'InjectionPoint' "
                f"AND wait_event = '{name}'",
                fetch=True,
            )
        except psycopg2.Error:
            rows = []
        if rows:
            return rows[0][0]
        if thread is not None and thread.done.is_set():
            raise TimeoutError(
                f"op finished without reaching injection point {name}")
        time.sleep(0.05)
    raise TimeoutError(f"no backend reached injection point {name}")


def wakeup_retry(conn, thread, name, timeout_s=30.0):
    """Keep waking `name` until `thread` finishes or timeout."""
    if thread is None:
        return True
    deadline = time.monotonic() + timeout_s
    while not thread.done.is_set() and time.monotonic() < deadline:
        try:
            _exec(conn, f"SELECT injection_points_wakeup('{name}')")
        except psycopg2.Error:
            pass  # no waiter registered yet
        time.sleep(0.2)
    return thread.done.is_set()


def detach_safe(conn, name):
    try:
        _exec(conn, f"SELECT injection_points_detach('{name}')")
    except psycopg2.Error:
        pass


class OpThread(threading.Thread):
    """Run a callable(conn) or a SQL string in the background."""

    def __init__(self, conn, work):
        super().__init__(daemon=True)
        self.conn = conn
        self.work = work
        self.error = None
        self.done = threading.Event()

    def run(self):
        try:
            if callable(self.work):
                self.work(self.conn)
            else:
                _exec(self.conn, self.work)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()


_CRASH_MSG = (
    "server closed the connection",
    "terminating connection",
    "connection not open",
    "could not receive data from server",
    "could not send data to server",
    "no connection to the server",
)


def conn_died(err):
    return err is not None and any(m in err for m in _CRASH_MSG)


def inj_error(err):
    # "checkpoint request failed" is how an injected error in the
    # checkpointer bgworker surfaces to the CHECKPOINT caller
    return (err is not None
            and ("error triggered for injection point" in err
                 or "checkpoint request failed" in err))


def first_line(err, n=300):
    return (err or "").strip().splitlines()[0][:n] if err else ""


# ====================================================================
# Sweep engine
# ====================================================================


class Sweep:
    def __init__(self, datadir, pg_prefix, extra_opts=""):
        os.environ["COEVO_PG_EXTRA_OPTS"] = extra_opts
        self.results = []
        self.runner = PostgresRunner(
            datadir, pg_prefix=pg_prefix, statement_timeout_ms=0
        )
        self.uri = self.runner._server.get_uri()
        self.mon = psycopg2.connect(self.uri)
        self.mon.autocommit = True
        _exec(self.mon, "SET statement_timeout = 0")
        _exec(self.mon, "CREATE EXTENSION IF NOT EXISTS injection_points")
        try:
            _exec(self.mon, "CREATE EXTENSION IF NOT EXISTS amcheck")
            self.has_amcheck = True
        except psycopg2.Error:
            self.has_amcheck = False

    # ----------------------------------------------------------- sessions
    def session(self):
        conn = psycopg2.connect(self.uri)
        conn.autocommit = True
        conn.cursor().execute("SET statement_timeout = 0")
        return conn

    def alive(self, conn):
        try:
            conn.cursor().execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_mon(self):
        """Re-create the monitor conn if it died (panic/immediate)."""
        try:
            if self.alive(self.mon):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            self.mon.close()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                self.mon = psycopg2.connect(self.uri)
                self.mon.autocommit = True
                _exec(self.mon, "SET statement_timeout = 0")
                return True
            except psycopg2.Error:
                time.sleep(0.5)
        return False

    def wait_for_server(self, timeout_s=120):
        """After PANIC/immediate: wait until the postmaster accepts conns."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                c = psycopg2.connect(self.uri)
                c.autocommit = True
                c.cursor().execute("SELECT 1")
                c.close()
                return self.ensure_mon()
            except psycopg2.Error:
                time.sleep(0.5)
        return False

    # ------------------------------------------------------------- attach
    def attach(self, sess, point, action, local=True):
        """Attach action to point on `sess` conn.  local=True: only that
        backend fires (set_local); local=False: global attach - needed for
        points fired by background workers (checkpointer, datachecksums)."""
        if local:
            _exec(sess, "SELECT injection_points_set_local()")
        else:
            detach_safe(sess, point)  # stale global attach -> replace
        _exec(sess, f"SELECT injection_points_attach('{point}','{action}')")

    # ------------------------------------------------------------- oracle
    def scan_log(self):
        return self.runner.log_fatal_lines(self.runner.log_new_lines())

    def amcheck(self, index, kind="btree"):
        """Return issue string or None."""
        if not self.has_amcheck:
            return None
        fn = "bt_index_check" if kind == "btree" else "gin_index_check"
        try:
            _exec(self.mon, f"SELECT {fn}('{index}')")
            return None
        except psycopg2.Error as exc:
            return f"{fn}({index}): {str(exc).splitlines()[0][:200]}"

    def catalog_check(self, det, allow_cc=True):
        """Shared post-failure catalog invariants.  Returns issue list."""
        issues = []
        if not self.ensure_mon():
            return ["server unreachable"]
        rows = _exec(
            self.mon,
            "SELECT relname FROM pg_class WHERE relname LIKE 'pg\\_temp\\_%'"
            " OR relname LIKE '%\\_ccnew' OR relname LIKE '%\\_ccold'",
            fetch=True,
        ) or []
        det["cc_leftovers"] = [r[0] for r in rows]
        if rows and not allow_cc:
            issues.append(f"transient leftovers: {[r[0] for r in rows]}")
        rows = _exec(
            self.mon,
            "SELECT count(*) FROM pg_index i LEFT JOIN pg_class c"
            " ON c.oid = i.indexrelid WHERE c.oid IS NULL",
            fetch=True,
        )
        if rows and rows[0][0]:
            issues.append(f"pg_index orphans: {rows[0][0]}")
        # duplicated live index entries for the same (indrelid,indkey);
        # *_ccnew/*_ccold are the documented REINDEX/CIC failure remnants
        rows = _exec(
            self.mon,
            "SELECT i.indrelid::regclass::text, i.indkey, count(*)"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE i.indislive"
            " AND c.relname NOT LIKE '%\\_ccnew%'"
            " AND c.relname NOT LIKE '%\\_ccold%'"
            " GROUP BY 1,2 HAVING count(*) > 1",
            fetch=True,
        )
        if rows:
            issues.append(f"duplicate live indexes: {rows[:4]}")
        return issues

    def report(self, point, action, rep, outcome, det, fatal, hit,
               fatal_full=None):
        rec = {
            "point": point, "action": action, "rep": rep,
            "outcome": outcome, "hit": bool(hit),
            "details": det,
            # verbatim filtered log lines for the hit/noise decision; the
            # unfiltered fatal set is kept for verbatim reporting so that
            # e.g. a documented PANIC line survives the crash-noise filter
            "fatal_log": fatal,
            "fatal_full": fatal_full if fatal_full is not None else fatal,
        }
        self.results.append(rec)
        tag = "HIT " if hit else "ok  "
        print(f"[{tag}] {point} {action}#{rep} -> {outcome}  "
              f"{json.dumps(det, default=str)[:380]}", flush=True)
        for ln in fatal:
            print(f"      LOG: {ln}", flush=True)
        return rec

    # ------------------------------------------------------------ runner
    def reset_schema(self):
        """DROP/CREATE public schema; survives panic/dead mon.  Also drains
        the log so a late-arriving crash line from the previous case is not
        attributed to this one."""
        self.ensure_mon()
        self.scan_log()
        for _ in (1, 2):
            try:
                _exec(self.mon, "DROP SCHEMA public CASCADE")
                _exec(self.mon, "CREATE SCHEMA public")
                _exec(self.mon,
                      "CREATE EXTENSION IF NOT EXISTS injection_points")
                _exec(self.mon,
                      "CREATE EXTENSION IF NOT EXISTS amcheck")
                return
            except psycopg2.Error:
                if not self.wait_for_server():
                    raise

    def cleanup_point(self, cfg, sess=None):
        """Detach the point.  Global attaches persist across backends."""
        try:
            detach_safe(self.mon, cfg["name"])
        except Exception:  # noqa: BLE001
            pass
        if sess is not None:
            try:
                detach_safe(sess, cfg["name"])
            except Exception:  # noqa: BLE001
                pass

    # ---- engines ---------------------------------------------------------
    def case_error(self, cfg, rep, rng):
        point = cfg["name"]
        det = {}
        s = ctl = None
        try:
            ctl = self.session()
            cfg["setup"](self, ctl, rep, rng, det)
            s = self.session()
            self.attach(s, point, "error", local=not cfg.get("global"))
            cfg["op"](self, s, ctl, rng, det)
            err = det.get("op_error")
            det["op_error_first"] = first_line(err)
            if inj_error(err):
                outcome = "clean-error"
            elif conn_died(err):
                # FATAL-class points (timeouts) legitimately kill the conn
                outcome = ("clean-fatal" if cfg.get("expect_fatal")
                           else "panic-or-death")
            elif err is None:
                outcome = "not-reached"
            else:
                outcome = "op-error"
            # global points fire inside bgworkers: the injected error is
            # only observable in the server log, never via op_error
            if outcome == "not-reached" and cfg.get("global"):
                for ln in self.runner.log_new_lines():
                    if f"error triggered for injection point {point}" in ln:
                        det["bg_error"] = ln.strip()[:300]
                        outcome = "clean-error"
                        break
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            outcome = "exception"
        finally:
            for c in (s, ctl):
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self.cleanup_point(cfg)
        return outcome, det

    def case_wait(self, cfg, rep, rng, mode):
        point = cfg["name"]
        det = {}
        s = ctl = None
        t = None
        try:
            ctl = self.session()
            cfg["setup"](self, ctl, rep, rng, det)
            s = self.session()
            self.attach(s, point, "wait", local=not cfg.get("global"))
            op = cfg.get("op_wait") or cfg["op"]

            def work(c):
                try:
                    cfg_op = op
                    cfg_op(self, c, ctl, rng, det)
                except Exception as exc:  # noqa: BLE001
                    det["op_error"] = f"{type(exc).__name__}: {exc}"

            t = OpThread(s, work)
            t.start()
            if cfg.get("pre_wait"):
                cfg["pre_wait"](self, ctl, det)
            if cfg.get("probe_wakeup"):
                # Waiter parks before pgstat reports it (e.g. the checksums
                # launcher pre-BackgroundWorkerInitializeConnection), so it
                # never appears in pg_stat_activity.  wakeup() errors when
                # no waiter is registered - its success IS the probe, and
                # it performs the wake at the same time.
                deadline = time.monotonic() + cfg.get("wait_to", 45)
                while time.monotonic() < deadline:
                    try:
                        _exec(self.mon,
                              f"SELECT injection_points_wakeup('{point}')")
                        det["reached"] = True
                        break
                    except psycopg2.Error:
                        time.sleep(0.1)
                if not det.get("reached"):
                    raise TimeoutError(
                        f"no backend reached injection point {point}")
                pid = None
            else:
                pid = wait_for_injection_point(
                    self.mon, point, timeout_s=cfg.get("wait_to", 45),
                    thread=None if cfg.get("global") else t)
            det["waiter_pid"] = pid
            det["reached"] = True

            if mode == "wake":
                wakeup_retry(self.mon, t, point)
            elif mode == "cancel":
                _exec(self.mon, f"SELECT pg_cancel_backend({pid})")
                if not t.done.wait(15):
                    det["cancel_wedge"] = True
                    wakeup_retry(self.mon, t, point, timeout_s=10)
            elif mode == "terminate":
                _exec(self.mon, f"SELECT pg_terminate_backend({pid})")
                if not t.done.wait(15):
                    det["terminate_wedge"] = True
                    wakeup_retry(self.mon, t, point, timeout_s=10)
            elif mode == "immediate":
                self.runner._server.stop("immediate")
                t.done.wait(10)
                self.runner._server.start()
                self.wait_for_server()
            t.join(timeout=90)
            # workload ops capture their own error into det['op_error'];
            # OpThread.error covers errors raised outside the op callable
            err = det.get("op_error") or t.error
            det["op_error"] = err
            det["op_error_first"] = first_line(err)

            if mode == "wake":
                if err is None:
                    outcome = "clean-wake"
                elif conn_died(err) and cfg.get("expect_fatal"):
                    outcome = "clean-fatal"
                else:
                    outcome = "op-error"
            elif mode == "cancel":
                if t.is_alive():
                    outcome = "hit:wedge-cancel"
                elif conn_died(err):
                    outcome = "panic-or-death"
                elif err and ("user request" in err
                              or "cancel" in err.lower()):
                    outcome = "clean-cancel"
                elif err and cfg.get("expect_fatal"):
                    # cancel preempted the pending FATAL; session survived
                    # with e.g. "current transaction is aborted"
                    outcome = "clean-cancel"
                elif err is None:
                    outcome = "clean-cancel"  # raced to completion
                else:
                    outcome = "op-error"
            elif mode == "terminate":
                if t.is_alive():
                    outcome = "hit:wedge-terminate"
                elif err:
                    outcome = "clean-terminate"
                elif cfg.get("post_commit") and det.get("committed"):
                    # backend parked past the commit boundary - SIGTERM is
                    # deferred/lost there; wakeup released it cleanly
                    outcome = "clean-terminate"
                else:
                    outcome = "op-error"
            else:  # immediate
                outcome = "clean-immediate"
        except TimeoutError as exc:
            det["exception"] = str(exc)
            outcome = "not-reached"
            try:
                if t is not None:
                    t.join(timeout=30)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            det["exception"] = f"{exc}\n{traceback.format_exc()}"
            outcome = "exception"
        finally:
            try:
                wakeup_retry(self.mon, t, point, timeout_s=8)
            except Exception:  # noqa: BLE001
                pass
            for c in (s, ctl):
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self.cleanup_point(cfg)
        return outcome, det

    # ------------------------------------------------------- case driver
    def run_case(self, cfg, action, rep, rng):
        if action == "error":
            outcome, det = self.case_error(cfg, rep, rng)
        else:
            outcome, det = self.case_wait(cfg, rep, rng, action)

        fatal = self.scan_log()
        panic_seen = any(
            "PANIC" in ln or "TRAP" in ln or "signal 6" in ln
            or "terminating any other active" in ln
            for ln in fatal
        )
        if outcome == "panic-or-death" or \
                (panic_seen and outcome != "clean-immediate"):
            ok = self.wait_for_server()
            det["recovered"] = ok
            if panic_seen:
                if cfg.get("panic_ok"):
                    outcome = ("documented-panic" if ok
                               else "hit:panic-wedge")
                else:
                    outcome = "hit:panic" if ok else "hit:panic-wedge"
            else:
                # backend died without a crash cycle (e.g. FATAL protocol
                # desync) - suspicious unless the point is FATAL-class
                outcome = ("clean-fatal" if cfg.get("expect_fatal")
                           else "hit:conn-death")
            # drain recovery chatter so the next case sees a clean log
            time.sleep(0.4)
            extra = self.scan_log()
            fatal.extend(extra)
        elif outcome == "clean-error" and cfg.get("post_commit") \
                and det.get("committed"):
            outcome = "commit-masked"
        elif outcome == "clean-error" and cfg.get("expect_fatal") \
                and det.get("session_alive") is False:
            # injected error surfaced but the FATAL still killed the conn
            outcome = "clean-fatal"

        # invariants
        det.setdefault("issues", [])
        if not outcome.startswith("hit") and outcome != "not-reached":
            try:
                issues = cfg["oracle"](self, det) or []
                det["issues"] = issues
                if issues:
                    outcome = "hit:invariant"
            except Exception as exc:  # noqa: BLE001
                det["oracle_exc"] = str(exc)[:300]
                outcome = "hit:oracle"

        # crash lines that landed during the oracle still belong to this
        # case - merge them before deciding hit/noise
        late = self.scan_log()
        if late:
            fatal.extend(late)
            if outcome in ("clean-error", "clean-cancel", "op-error",
                           "clean-wake") and any(
                               "TRAP" in ln or "PANIC" in ln
                               or "signal 6" in ln for ln in late):
                # assertion fired on backend exit after the op returned
                ok = self.wait_for_server()
                det["recovered"] = ok
                outcome = ("documented-panic" if cfg.get("panic_ok") and ok
                           else "hit:panic" if ok else "hit:panic-wedge")

        # fatal log lines: whitelisted for documented-panic / immediate
        if outcome in ("documented-panic", "clean-immediate"):
            noisy = [ln for ln in fatal if not _CRASH_NOISE.search(ln)]
        else:
            noisy = fatal
        # documented-panic keeps its verbatim log lines but is not a hit -
        # it is reported in its own section (an abort/commit-critical
        # crash is the designed backstop for those points)
        hit = outcome.startswith("hit") or \
            (bool(noisy) and outcome != "documented-panic")
        self.report(cfg["name"], action, rep, outcome, det, noisy, hit,
                    fatal_full=fatal)
        return outcome


# ====================================================================
# Workloads
#   setup(sw, ctl, rep, rng, det)
#   op(sw, sess, ctl, rng, det)  - must reach the point; sets det['op_error']
#   oracle(sw, det) -> [issues]
# ====================================================================


def wl_onconflict_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE oc_t(k int PRIMARY KEY, v int)")
    _exec(ctl,
          "INSERT INTO oc_t SELECT g, g*10 FROM generate_series(1, 300) g")


def wl_onconflict_op(hit_conflict, serializable):
    def op(sw, sess, ctl, rng, det):
        key = rng.randint(1, 300) if hit_conflict else rng.randint(
            10000, 99999)
        try:
            if serializable:
                _exec(sess, "BEGIN ISOLATION LEVEL SERIALIZABLE")
            _exec(sess,
                  f"INSERT INTO oc_t VALUES ({key}, {rng.randint(0, 999)})"
                  " ON CONFLICT (k) DO UPDATE SET v = oc_t.v + 1")
            if serializable:
                _exec(sess, "COMMIT")
        except psycopg2.Error as exc:
            det["op_error"] = f"{type(exc).__name__}: {exc}"
            try:
                sess.rollback()
            except Exception:  # noqa: BLE001
                pass
    return op


def wl_onconflict_oracle(sw, det):
    issues = sw.catalog_check(det)
    rows = _exec(sw.mon,
                 "SELECT count(*) = count(DISTINCT k), count(*) FROM oc_t",
                 fetch=True)
    if not rows or not rows[0][0]:
        issues.append("duplicate PK rows in oc_t")
    am = sw.amcheck("oc_t_pkey")
    if am:
        issues.append(am)
    return issues


# ---- heap_update-before-pin ------------------------------------------------
def wl_update_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE u_t(i int PRIMARY KEY, v int, pad text)")
    _exec(ctl,
          "INSERT INTO u_t SELECT g, g, repeat('x',20)"
          " FROM generate_series(1, 400) g")


def wl_update_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess,
              f"UPDATE u_t SET v = v + 1 WHERE i = {rng.randint(1, 400)}")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_update_oracle(sw, det):
    issues = sw.catalog_check(det)
    rows = _exec(sw.mon, "SELECT count(*) FROM u_t", fetch=True)
    if not rows or rows[0][0] != 400:
        issues.append(f"u_t row count {rows}")
    return issues


# ---- heap_lock_updated_tuple: FOR UPDATE follows an update chain ----------
def wl_lockchain_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE lc_t(i int PRIMARY KEY, v int)")
    _exec(ctl, "INSERT INTO lc_t VALUES (1, 0)")
    _exec(ctl, "BEGIN")                      # blocker txn (commit/rollback
    _exec(ctl, "UPDATE lc_t SET v = v + 1 WHERE i = 1")   # releases it)


def wl_lockchain_op(sw, sess, ctl, rng, det):
    """error-action path: run FOR UPDATE on a private thread while ctl
    commits; the lock then walks the update chain -> point."""
    def work(c):
        try:
            _exec(c, "SELECT * FROM lc_t WHERE i = 1 FOR UPDATE")
        except psycopg2.Error as exc:
            det["op_error"] = f"{type(exc).__name__}: {exc}"

    t = OpThread(sess, work)
    t.start()
    time.sleep(0.6)            # let sess block on ctl's uncommitted xmax
    try:
        _exec(ctl, "COMMIT")
    except psycopg2.Error:
        pass
    t.join(timeout=60)
    det["committed"] = True
    return det


def wl_lockchain_op_wait(sw, sess, ctl, rng, det):
    """wait-action path: the op itself is the blocking FOR UPDATE; the
    engine's pre_wait hook commits the blocker so sess reaches the point."""
    try:
        _exec(sess, "SELECT * FROM lc_t WHERE i = 1 FOR UPDATE")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_lockchain_pre_wait(sw, ctl, det):
    time.sleep(0.6)
    try:
        _exec(ctl, "COMMIT")
    except psycopg2.Error:
        pass


def wl_lockchain_oracle(sw, det):
    issues = sw.catalog_check(det)
    try:
        rows = _exec(sw.mon, "SELECT v FROM lc_t WHERE i = 1", fetch=True)
        if not rows:
            issues.append("lc_t row missing")
    except psycopg2.Error as exc:
        issues.append(f"lc_t unreadable: {exc}")
    return issues


# ---- post-commit invalidation points ----------------------------------------
def wl_ddl_setup(sw, ctl, rep, rng, det):
    pass


def wl_ddl_op(sw, sess, ctl, rng, det):
    """DDL txn that (a) queues transactional inval messages -> hits
    'transaction-end-process-inval' and (b) does systable list scans via
    partition attachment -> takes a catalog snapshot -> hits
    'invalidate-catalog-snapshot-end'.  Both fire post-commit."""
    p = f"swp{rng.randint(0, 10**9)}"
    c = f"{p}c"
    det["ddl_table"] = p
    try:
        _exec(sess, "BEGIN")
        _exec(sess, f"CREATE TABLE {p}(i int) PARTITION BY RANGE (i)")
        _exec(sess, f"CREATE TABLE {c} PARTITION OF {p} DEFAULT")
        _exec(sess, f"INSERT INTO {p} VALUES (1)")
        # ~1/3 of reps take the abort path (AtEOXact_Inval(false))
        if rng.random() < 0.33:
            det["xact_outcome"] = "rollback"
            _exec(sess, "ROLLBACK")
        else:
            det["xact_outcome"] = "commit"
            _exec(sess, "COMMIT")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass
    try:
        rows = _exec(sw.mon,
                     "SELECT count(*) FROM pg_class WHERE relname = "
                     f"'{p}' AND relkind = 'p'", fetch=True)
        det["committed"] = bool(rows and rows[0][0])
    except psycopg2.Error:
        det["committed"] = None
    return det


def wl_ddl_oracle(sw, det):
    issues = sw.catalog_check(det, allow_cc=False)
    if det.get("committed") and det.get("ddl_table"):
        try:
            _exec(sw.mon, f"SELECT count(*) FROM {det['ddl_table']}")
        except psycopg2.Error as exc:
            issues.append(
                f"committed table unreadable: "
                f"{str(exc).splitlines()[0][:160]}")
    return issues


# ---- multixact-create-from-members (inside a critical section -> PANIC) ------
def wl_multixact_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE mx_t(i int PRIMARY KEY, v int)")
    _exec(ctl, "INSERT INTO mx_t VALUES (1, 1)")
    _exec(ctl, "BEGIN")
    _exec(ctl, "SELECT * FROM mx_t WHERE i = 1 FOR KEY SHARE")


def wl_multixact_op(sw, sess, ctl, rng, det):
    # second locker on the same row -> MultiXactIdCreateFromMembers inside
    # a critical section (error/cancel here legitimately PANICs)
    try:
        _exec(sess, "BEGIN")
        _exec(sess, "SELECT * FROM mx_t WHERE i = 1 FOR SHARE")
        _exec(sess, "COMMIT")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass
    try:
        _exec(ctl, "ROLLBACK")
    except psycopg2.Error:
        pass


def wl_multixact_oracle(sw, det):
    issues = sw.catalog_check(det)
    try:
        rows = _exec(sw.mon, "SELECT v FROM mx_t WHERE i = 1", fetch=True)
        if not rows or rows[0][0] != 1:
            issues.append("mx_t row lost")
    except psycopg2.Error as exc:
        issues.append(f"mx_t unreadable: {exc}")
    return issues


# ---- REINDEX CONCURRENTLY state machine ---------------------------------------
def wl_reindex_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE rx_t(i int PRIMARY KEY, v text)")
    _exec(ctl,
          "INSERT INTO rx_t SELECT g, md5(g::text)"
          " FROM generate_series(1, 4000) g")
    _exec(ctl, "CREATE INDEX rx_v ON rx_t(v)")
    _exec(ctl, "CREATE INDEX rx_expr ON rx_t((v || 'x'))")
    det["index"] = "rx_t_pkey"


def wl_reindex_op(sw, sess, ctl, rng, det):
    idx = det.get("index", "rx_t_pkey")
    try:
        _exec(sess, f"REINDEX INDEX CONCURRENTLY {idx}")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_reindex_notsafe_op(sw, sess, ctl, rng, det):
    det["index"] = "rx_expr"
    try:
        _exec(sess, "REINDEX INDEX CONCURRENTLY rx_expr")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_reindex_oracle(sw, det):
    issues = sw.catalog_check(det, allow_cc=True)
    rows = _exec(sw.mon,
                 "SELECT i.indisvalid, i.indisready, i.indislive"
                 " FROM pg_index i"
                 " WHERE i.indexrelid = 'rx_t_pkey'::regclass",
                 fetch=True)
    if rows and not all(rows[0]):
        issues.append(f"old index state invalid: {rows[0]}")
    am = sw.amcheck("rx_t_pkey")
    if am:
        issues.append(am)
    # documented remnant: not-ready ccnew index; drop it to keep sweeping
    left = _exec(sw.mon,
                 "SELECT indexrelid::regclass::text FROM pg_index"
                 " WHERE indrelid = 'rx_t'::regclass AND NOT indisready",
                 fetch=True) or []
    det["notready_idx"] = [r[0] for r in left]
    for (iname,) in left:
        try:
            _exec(sw.mon, f"DROP INDEX {iname}")
        except psycopg2.Error:
            pass
    try:
        _exec(sw.mon, "REINDEX INDEX CONCURRENTLY rx_t_pkey")
    except psycopg2.Error as exc:
        issues.append(f"retry reindex: {str(exc).splitlines()[0][:200]}")
    return issues


# ---- CREATE INDEX CONCURRENTLY ------------------------------------------------
def wl_cic_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE cic_t(i int, v int)")
    _exec(ctl,
          "INSERT INTO cic_t SELECT g, g*3 FROM generate_series(1, 3000) g")
    det["index"] = f"cic_i_{rep}_{rng.randint(0, 10**6)}"


def wl_cic_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess, f"CREATE INDEX CONCURRENTLY {det['index']} ON cic_t(v)")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_cic_oracle(sw, det):
    issues = sw.catalog_check(det, allow_cc=True)
    idx = det.get("index", "cic_i")
    rows = _exec(sw.mon,
                 "SELECT i.indisvalid, i.indisready FROM pg_index i"
                 " JOIN pg_class c ON c.oid = i.indexrelid"
                 f" WHERE c.relname = '{idx}'", fetch=True)
    if rows:
        det["idx_state"] = rows[0]  # invalid remnant = documented CIC mode
        try:
            _exec(sw.mon, f"DROP INDEX IF EXISTS {idx}")
            _exec(sw.mon, f"CREATE INDEX CONCURRENTLY {idx} ON cic_t(v)")
        except psycopg2.Error as exc:
            issues.append(f"retry cic: {str(exc).splitlines()[0][:200]}")
        am = sw.amcheck(idx)
        if am:
            issues.append(am)
    return issues


# ---- VACUUM (truncate / index_cleanup option points + inplace updates) -------
def wl_vacuum_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE vt(i int, v text)")
    _exec(ctl, "CREATE INDEX vt_i ON vt(i)")
    _exec(ctl,
          "INSERT INTO vt SELECT g, repeat('x',40)"
          " FROM generate_series(1, 2000) g")
    _exec(ctl, "DELETE FROM vt WHERE i > 50")   # dead tail -> truncate cand


def wl_vacuum_op(opt_sql):
    def op(sw, sess, ctl, rng, det):
        try:
            _exec(sess, opt_sql)
        except psycopg2.Error as exc:
            det["op_error"] = f"{type(exc).__name__}: {exc}"
    return op


def wl_vacuum_oracle(sw, det):
    issues = sw.catalog_check(det)
    rows = _exec(sw.mon, "SELECT count(*) FROM vt", fetch=True)
    if not rows or rows[0][0] != 50:
        issues.append(f"vt rows {rows}")
    try:
        _exec(sw.mon, "VACUUM vt")
    except psycopg2.Error as exc:
        issues.append(f"retry vacuum: {str(exc).splitlines()[0][:120]}")
    return issues


# ---- GIN split points ----------------------------------------------------------
GIN_DDL = (
    "CREATE TABLE gin_t(i int4[]) WITH (autovacuum_enabled = off)",
    "CREATE INDEX gin_i ON gin_t USING gin(i) WITH (fastupdate = off)",
    """CREATE FUNCTION range_array(int, int) RETURNS int[]
       LANGUAGE sql IMMUTABLE AS $$
       SELECT array_agg(g) FROM generate_series($1, $2 - 1) g $$""",
)


def wl_gin_setup(sw, ctl, rep, rng, det):
    for sql in GIN_DDL:
        _exec(ctl, sql)
    _exec(ctl,
          "INSERT INTO gin_t SELECT range_array(g, g + 5)"
          " FROM generate_series(1, 400) g")


def wl_gin_deep_setup(sw, ctl, rep, rng, det):
    """Entry-tree internal splits need >~800 leaf pages; like the upstream
    gin_incomplete_splits test, keep appending contiguous ranges until an
    internal page overflows."""
    for sql in GIN_DDL:
        _exec(ctl, sql)
    _exec(ctl,
          "INSERT INTO gin_t SELECT range_array(g * 100, g * 100 + 99)"
          " FROM generate_series(1, 3000) g")


def wl_gin_deep_op(sw, sess, ctl, rng, det):
    j = 0
    try:
        for j in range(2500):
            _exec(sess,
                  f"INSERT INTO gin_t VALUES (range_array({310000 + j * 100},"
                  f" {310000 + j * 100 + 100}))")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass
    det["batches"] = j + 1


def wl_gin_op(sw, sess, ctl, rng, det):
    base = rng.randint(100000, 900000)
    j = 0
    try:
        for j in range(40):
            _exec(sess,
                  "INSERT INTO gin_t"
                  f" VALUES (range_array({base + j * 7},"
                  f" {base + j * 7 + 5}))")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass
    det["batches"] = j + 1


def wl_gin_finish_op(sw, sess, ctl, rng, det):
    """Seed an incomplete split via a leaf-split error, detach it, then
    keep inserting: the first insert meeting the incomplete page hits
    'gin-finish-incomplete-split' (already attached by the engine)."""
    try:
        _exec(sess, "SELECT injection_points_attach"
                    "('gin-leave-leaf-split-incomplete','error')")
    except psycopg2.Error:
        pass
    base = rng.randint(100000, 900000)
    seeded = False
    for j in range(25):
        try:
            _exec(sess,
                  "INSERT INTO gin_t VALUES"
                  f" (range_array({base + j * 7}, {base + j * 7 + 5}))")
        except psycopg2.Error:
            seeded = True
            try:
                sess.rollback()
            except Exception:  # noqa: BLE001
                pass
            break
    det["seeded_split"] = seeded
    detach_safe(sess, "gin-leave-leaf-split-incomplete")
    # now hit the incomplete split via fresh inserts
    try:
        for j in range(25, 60):
            _exec(sess,
                  "INSERT INTO gin_t VALUES"
                  f" (range_array({base + j * 7}, {base + j * 7 + 5}))")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_gin_oracle(sw, det):
    issues = sw.catalog_check(det)
    am = sw.amcheck("gin_i", kind="gin")
    if am:
        issues.append(am)
    try:
        _exec(sw.mon, "INSERT INTO gin_t VALUES (range_array(1, 5))")
    except psycopg2.Error as exc:
        issues.append(f"gin retry insert: {str(exc).splitlines()[0][:120]}")
    return issues


# ---- btree points --------------------------------------------------------------
def wl_btree_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE bt_t(i int, v int)")
    _exec(ctl, "CREATE INDEX bt_i ON bt_t(i)")
    _exec(ctl, "CREATE INDEX bt_v ON bt_t(v)")
    _exec(ctl,
          "INSERT INTO bt_t SELECT g, g*2 FROM generate_series(1, 300) g")


def wl_btree_op(sw, sess, ctl, rng, det):
    base = rng.randint(100000, 999999)
    try:
        for j in range(30):
            _exec(sess,
                  f"INSERT INTO bt_t SELECT {base} + g, {base} + g"
                  " FROM generate_series(1, 40) g")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_btree_finish_op(sw, sess, ctl, rng, det):
    """Seed an incomplete btree split, then keep inserting to hit the
    'finish' point when a descent meets the incomplete page."""
    try:
        _exec(sess, "SELECT injection_points_attach"
                    "('nbtree-leave-leaf-split-incomplete','error')")
    except psycopg2.Error:
        pass
    base = rng.randint(100000, 999999)
    seeded = False
    for j in range(30):
        try:
            _exec(sess,
                  f"INSERT INTO bt_t SELECT {base} + g, {base} + g"
                  " FROM generate_series(1, 40) g")
        except psycopg2.Error:
            seeded = True
            try:
                sess.rollback()
            except Exception:  # noqa: BLE001
                pass
            break
    det["seeded_split"] = seeded
    detach_safe(sess, "nbtree-leave-leaf-split-incomplete")
    try:
        for j in range(30, 70):
            _exec(sess,
                  f"INSERT INTO bt_t SELECT {base} + g, {base} + g"
                  " FROM generate_series(1, 40) g")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_btree_scan_setup(sw, ctl, rep, rng, det):
    # many leaf pages so a backward scan steps left repeatedly
    _exec(ctl, "CREATE TABLE bt_t(i int, v int)")
    _exec(ctl, "CREATE INDEX bt_i ON bt_t(i)")
    _exec(ctl, "CREATE INDEX bt_v ON bt_t(v)")
    _exec(ctl,
          "INSERT INTO bt_t SELECT g, g*2 FROM generate_series(1, 30000) g")


def wl_btree_scan_op(sw, sess, ctl, rng, det):
    # _bt_lock_and_validate_left (nbtree-walk-left*) is only called by
    # backward index scans - force index usage, small tables prefer seqscan
    try:
        _exec(sess, "SET enable_seqscan = off")
        for _ in range(40):
            _exec(sess,
                  "SELECT i FROM bt_t WHERE i < "
                  f"{rng.randint(0, 100000)} ORDER BY i DESC LIMIT 900")
            _exec(sess,
                  f"DELETE FROM bt_t WHERE i = {rng.randint(1, 300)}")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_btree_race_op(sw, sess, ctl, rng, det):
    """Backward scans on `sess` while a helper connection deletes contiguous
    ranges + VACUUMs and inserts - exercises the concurrent split/delete
    branches of _bt_lock_and_validate_left (walk-left-deleted/-restart/
    -step-right)."""
    stop = threading.Event()

    def churn():
        try:
            c = psycopg2.connect(sw.uri)
            c.autocommit = True
            for i in range(200):
                if stop.is_set():
                    break
                lo = rng.randint(1, 28000)
                try:
                    _exec(c, f"DELETE FROM bt_t WHERE i BETWEEN {lo}"
                             f" AND {lo + 300}")
                    _exec(c,
                          f"INSERT INTO bt_t SELECT {29000 + i * 400} + g, g"
                          " FROM generate_series(1, 400) g")
                    if i % 4 == 0:
                        _exec(c, "VACUUM bt_t")
                except psycopg2.Error:
                    pass
            c.close()
        except psycopg2.Error:
            pass

    ct = threading.Thread(target=churn, daemon=True)
    ct.start()
    try:
        _exec(sess, "SET enable_seqscan = off")
        for _ in range(120):
            _exec(sess,
                  "SELECT i FROM bt_t WHERE i < "
                  f"{rng.randint(0, 30000)} ORDER BY i DESC LIMIT 800")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        stop.set()
        ct.join(timeout=30)


def wl_btree_empty_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE be_t(i int)")
    _exec(ctl, "CREATE INDEX be_i ON be_t(i)")
    # stays empty: the empty-index points only fire on an index with no
    # leaf page yet


def wl_btree_empty_op(sw, sess, ctl, rng, det):
    try:
        for _ in range(25):
            # _bt_first on an empty index under serializable isolation
            _exec(sess, "BEGIN ISOLATION LEVEL SERIALIZABLE")
            _exec(sess, "SELECT count(*) FROM be_t WHERE i = 42")
            _exec(sess, "SELECT min(i), max(i) FROM be_t")
            _exec(sess, "COMMIT")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_btree_empty_oracle(sw, det):
    return sw.catalog_check(det)


def wl_btree_deep_setup(sw, ctl, rep, rng, det):
    # ~780 packed leaf pages -> 3-level tree; appended leaf downlinks then
    # split the rightmost internal page almost immediately
    _exec(ctl, "CREATE TABLE bt_t(i int, v int)")
    _exec(ctl,
          "INSERT INTO bt_t SELECT g, g*2 FROM generate_series(1, 400000) g")
    _exec(ctl, "CREATE INDEX bt_i ON bt_t(i)")


def wl_btree_deep_op(sw, sess, ctl, rng, det):
    try:
        for j in range(80):
            base = 400000 + j * 1000
            _exec(sess,
                  f"INSERT INTO bt_t SELECT {base} + g, g"
                  " FROM generate_series(1, 1000) g")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_btree_vac_setup(sw, ctl, rep, rng, det):
    # enough rows that a contiguous delete empties several leaf pages
    _exec(ctl, "CREATE TABLE bt_t(i int, v int)")
    _exec(ctl, "CREATE INDEX bt_i ON bt_t(i)")
    _exec(ctl, "CREATE INDEX bt_v ON bt_t(v)")
    _exec(ctl,
          "INSERT INTO bt_t SELECT g, g*2 FROM generate_series(1, 6000) g")


def wl_btree_vac_op(sw, sess, ctl, rng, det):
    try:
        # delete a contiguous swath covering whole leaf pages, then VACUUM
        # so page deletion (half-dead/unlink) actually runs
        lo = rng.randint(50, 800)
        _exec(sess, f"DELETE FROM bt_t WHERE i BETWEEN {lo} AND {lo + 900}")
        _exec(sess, "VACUUM bt_t")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_btree_vac_finish_op(sw, sess, ctl, rng, det):
    """Seed a half-dead leaf with an injected error at
    nbtree-leave-page-half-dead, then a second VACUUM reaches
    nbtree-finish-half-dead-page-vacuum when it re-encounters the page."""
    seeded = False
    try:
        _exec(sess, "SELECT injection_points_attach"
                    "('nbtree-leave-page-half-dead','error')")
        _exec(sess, "DELETE FROM bt_t WHERE i BETWEEN 600 AND 2000")
        _exec(sess, "VACUUM bt_t")
    except psycopg2.Error as exc:
        seeded = inj_error(f"{exc}")
    detach_safe(sess, "nbtree-leave-page-half-dead")
    det["seeded_halfdead"] = seeded
    try:
        _exec(sess, "DELETE FROM bt_t WHERE i BETWEEN 2500 AND 3400")
        _exec(sess, "VACUUM bt_t")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_btree_oracle(sw, det):
    issues = sw.catalog_check(det)
    try:
        have = {r[0] for r in _exec(
            sw.mon,
            "SELECT relname FROM pg_class"
            " WHERE relname IN ('bt_i', 'bt_v')", fetch=True) or []}
    except psycopg2.Error:
        have = set()
    for idx in sorted(have):
        am = sw.amcheck(idx)
        if am:
            issues.append(am)
            break
    return issues


# ---- deadlock-timeout-fired ----------------------------------------------------
def wl_deadlock_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE dl_t(i int PRIMARY KEY, v int)")
    _exec(ctl, "INSERT INTO dl_t VALUES (1, 0), (2, 0)")


def wl_deadlock_op(sw, sess, ctl, rng, det):
    """sess (attach target) and ctl deadlock on rows 1/2.  ctl holds row 1;
    sess takes row 2, then ctl requests row 2 (blocks), then sess requests
    row 1 -> detector fires -> sess parks/errors at the point."""
    _exec(ctl, "SET deadlock_timeout = 50")
    _exec(ctl, "BEGIN")
    _exec(ctl, "UPDATE dl_t SET v = 1 WHERE i = 1")
    _exec(sess, "SET deadlock_timeout = 50")
    _exec(sess, "BEGIN")
    _exec(sess, "UPDATE dl_t SET v = 2 WHERE i = 2")

    def ctl_req(c):
        try:
            _exec(c, "UPDATE dl_t SET v = 3 WHERE i = 2")
            _exec(c, "COMMIT")
        except psycopg2.Error:
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass

    ct = OpThread(ctl, ctl_req)
    ct.start()
    time.sleep(0.3)            # ensure ctl blocks on sess's lock first
    try:
        _exec(sess, "UPDATE dl_t SET v = 4 WHERE i = 1")
        _exec(sess, "COMMIT")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass
    ct.join(timeout=30)


def wl_deadlock_oracle(sw, det):
    issues = sw.catalog_check(det)
    rows = _exec(sw.mon, "SELECT count(*) FROM dl_t", fetch=True)
    if not rows or rows[0][0] != 2:
        issues.append(f"dl_t rows {rows}")
    return issues


# ---- transaction / idle-in-transaction timeouts ----------------------------------
def wl_timeout_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE to_t(i int)")


def wl_txntimeout_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess, "SET transaction_timeout = 60")
        _exec(sess, "BEGIN")
        _exec(sess, "SELECT pg_sleep(0.4)")
        _exec(sess, "COMMIT")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass
    det["session_alive"] = sw.alive(sess)


def wl_idletimeout_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess, "SET idle_in_transaction_session_timeout = 60")
        _exec(sess, "BEGIN")
        _exec(sess, "SELECT 1")
        time.sleep(0.4)          # backend idles in txn -> timeout fires
        _exec(sess, "SELECT 2")  # surfaces pending FATAL
        _exec(sess, "COMMIT")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass
    det["session_alive"] = sw.alive(sess)


def wl_timeout_oracle(sw, det):
    return sw.catalog_check(det)


# ---- ri-before-pk-lock (FK fastpath) ----------------------------------------------
def wl_ri_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE ri_pk(id int PRIMARY KEY)")
    _exec(ctl, "INSERT INTO ri_pk SELECT g FROM generate_series(1, 200) g")
    _exec(ctl,
          "CREATE TABLE ri_fk(id int PRIMARY KEY, pid int"
          " REFERENCES ri_pk(id))")
    _exec(ctl, "INSERT INTO ri_fk SELECT g, g FROM generate_series(1, 50) g")


def wl_ri_op(sw, sess, ctl, rng, det):
    # first RI check primes the fastpath; later inserts hit ri-before-pk-lock
    try:
        for j in range(6):
            _exec(sess,
                  f"INSERT INTO ri_fk VALUES ({500 + j},"
                  f" {rng.randint(1, 200)})")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_ri_oracle(sw, det):
    issues = sw.catalog_check(det)
    rows = _exec(sw.mon,
                 "SELECT count(*) FROM ri_fk f LEFT JOIN ri_pk p"
                 " ON f.pid = p.id WHERE p.id IS NULL",
                 fetch=True)
    if rows and rows[0][0]:
        issues.append(f"orphan FK rows: {rows[0][0]}")
    return issues


# ---- exec-init-partition -----------------------------------------------------------
def wl_part_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE pp(i int, v int) PARTITION BY RANGE (i)")
    _exec(ctl,
          "CREATE TABLE pp1 PARTITION OF pp FOR VALUES FROM (0) TO (500)")
    _exec(ctl,
          "CREATE TABLE pp2 PARTITION OF pp FOR VALUES"
          " FROM (500) TO (1000000)")
    _exec(ctl, "CREATE UNIQUE INDEX pp_i ON pp(i)")
    _exec(ctl, "INSERT INTO pp SELECT g, g FROM generate_series(1, 200) g")


def wl_part_op(sw, sess, ctl, rng, det):
    try:
        for j in range(30):
            # arbiter-index ancestor walk reaches the injection point
            _exec(sess,
                  f"INSERT INTO pp VALUES ({rng.randint(0, 999)}, {j})"
                  " ON CONFLICT (i) DO UPDATE SET v = excluded.v")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_part_oracle(sw, det):
    return sw.catalog_check(det)


# ---- catcache list miss ------------------------------------------------------------
def wl_catcache_setup(sw, ctl, rep, rng, det):
    wl_part_setup(sw, ctl, rep, rng, det)


def wl_catcache_op(sw, sess, ctl, rng, det):
    try:
        for _ in range(30):
            _exec(sess,
                  "SELECT count(*) FROM pg_inherits"
                  " WHERE inhparent = 'pp'::regclass")
            _exec(sess,
                  "SELECT relname FROM pg_class WHERE oid = 'pp'::regclass")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


wl_catcache_oracle = wl_part_oracle


# ---- typecache rel-type insert ------------------------------------------------------
def wl_typecache_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE tc_t(a int, b text)")


def wl_typecache_op(sw, sess, ctl, rng, det):
    try:
        for j in range(20):
            _exec(sess, f"CREATE TABLE tc_{j}(a int, b text)")
            _exec(sess, f"SELECT ROW(1, 'x')::tc_{j}")
            _exec(sess, f"SELECT array_agg(t) FROM tc_{j} t")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_typecache_oracle(sw, det):
    return sw.catalog_check(det)


# ---- createdb / dropdb -----------------------------------------------------------------
def wl_createdb_setup(sw, ctl, rep, rng, det):
    det["db"] = f"sweepdb_{rep}_{rng.randint(0, 10**6)}"


def wl_createdb_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess, f"CREATE DATABASE {det['db']}")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_createdb_oracle(sw, det):
    issues = []
    db = det.get("db")
    if db:
        try:
            _exec(sw.mon, f"DROP DATABASE IF EXISTS {db}")
        except psycopg2.Error as exc:
            issues.append(
                f"cleanup db: {str(exc).splitlines()[0][:120]}")
    issues += sw.catalog_check(det)
    return issues


def wl_dropdb_setup(sw, ctl, rep, rng, det):
    db = f"swdrop_{rep}_{rng.randint(0, 10**6)}"
    det["db"] = db
    _exec(ctl, f"CREATE DATABASE {db}")


def wl_dropdb_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess, f"DROP DATABASE {det['db']}")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_dropdb_oracle(sw, det):
    issues = []
    db = det.get("db")
    if db:
        rows = _exec(sw.mon,
                     "SELECT datconnlimit FROM pg_database"
                     f" WHERE datname = '{db}'", fetch=True)
        det["leftover_db"] = rows
        if rows:
            try:
                _exec(sw.mon, f"DROP DATABASE {db}")
            except psycopg2.Error as exc:
                issues.append(
                    f"re-drop invalid db: "
                    f"{str(exc).splitlines()[0][:160]}")
    issues += sw.catalog_check(det)
    return issues


# ---- checkpoints (points fire in the checkpointer bgworker) ---------------------------
def wl_checkpoint_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE ck_t(i int)")
    _exec(ctl, "INSERT INTO ck_t SELECT g FROM generate_series(1, 100) g")


def wl_checkpoint_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess, "CHECKPOINT")
        _exec(sess, "SELECT 1")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"


def wl_checkpoint_oracle(sw, det):
    return sw.catalog_check(det)


# ---- hash aggregate spill ---------------------------------------------------------
def wl_hashagg_setup(sw, ctl, rep, rng, det):
    _exec(ctl,
          "CREATE TABLE ha_t AS SELECT g % 50000 AS a, g AS b"
          " FROM generate_series(1, 200000) g")


def wl_hashagg_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess, "SET work_mem = '64kB'")
        _exec(sess,
              "SELECT a, count(*), sum(b) FROM ha_t GROUP BY a")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_hashagg_oracle(sw, det):
    return sw.catalog_check(det)


# ---- datachecksums enable/disable (points fire in launcher/worker bgworkers) ----
def wl_cksum_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE cs_t(i int)")
    _exec(ctl, "INSERT INTO cs_t SELECT g FROM generate_series(1, 500) g")
    try:
        _exec(ctl, "SELECT pg_disable_data_checksums()")
    except psycopg2.Error:
        pass
    # pg_enable_data_checksums() no-ops unless the state has settled 'off';
    # wait for the disable to finish so the op walks the whole enable path
    for _ in range(80):
        st = _exec(ctl, "SHOW data_checksums", fetch=True)[0][0]
        if st == "off":
            break
        time.sleep(0.25)
    det["state_at_start"] = st


def wl_cksum_op(sw, sess, ctl, rng, det):
    try:
        _exec(sess, "SELECT pg_enable_data_checksums()")
        st = "?"
        for _ in range(80):
            st = _exec(sess, "SHOW data_checksums", fetch=True)[0][0]
            if st == "on":
                break
            time.sleep(0.25)
        det["state_after_enable"] = st
        _exec(sess, "SELECT pg_disable_data_checksums()")
        for _ in range(60):
            st = _exec(sess, "SHOW data_checksums", fetch=True)[0][0]
            if st == "off":
                break
            time.sleep(0.25)
        det["state_final"] = st
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_cksum_oracle(sw, det):
    issues = sw.catalog_check(det)
    try:
        st = _exec(sw.mon, "SHOW data_checksums", fetch=True)[0][0]
        det["checksums_state"] = st
        if st not in ("on", "off"):
            try:
                _exec(sw.mon, "SELECT pg_disable_data_checksums()")
            except psycopg2.Error as exc:
                issues.append(
                    f"checksums stuck at {st}: "
                    f"{str(exc).splitlines()[0][:160]}")
    except psycopg2.Error as exc:
        issues.append(f"checksums state unreadable: {exc}")
    return issues


# ---- WAIT FOR LSN (primary flush) ---------------------------------------------------
def wl_waitlsn_setup(sw, ctl, rep, rng, det):
    _exec(ctl, "CREATE TABLE wl_t(i int)")


def wl_waitlsn_op(sw, sess, ctl, rng, det):
    try:
        lsn = _exec(sw.mon,
                    "SELECT pg_current_wal_insert_lsn() + 65536",
                    fetch=True)[0][0]
        _exec(sess,
              f"WAIT FOR LSN '{lsn}' WITH (MODE 'PRIMARY_FLUSH',"
              " TIMEOUT 2000)")
    except psycopg2.Error as exc:
        det["op_error"] = f"{type(exc).__name__}: {exc}"
        try:
            sess.rollback()
        except Exception:  # noqa: BLE001
            pass


def wl_waitlsn_oracle(sw, det):
    return sw.catalog_check(det)


# ====================================================================
# Registry
# ====================================================================
P = dict

POINTS = [
    # ------------------------------------------------- mid-mutation
    # fires only on the no-committed-conflict (speculative insert) path
    P(name="exec-insert-before-insert-speculative",
      wl="onconflict_noconflict",
      actions=dict(error=10, wake=3, cancel=3, terminate=2)),
    P(name="check-exclusion-or-unique-constraint-conflict",
      wl="onconflict_serializable",
      actions=dict(error=10, wake=3, cancel=3, terminate=2)),
    P(name="check-exclusion-or-unique-constraint-no-conflict",
      wl="onconflict_noconflict",
      actions=dict(error=10, wake=2, cancel=3, terminate=2)),
    P(name="heap_update-before-pin", wl="update",
      actions=dict(error=10, wake=2, cancel=3, terminate=2)),
    P(name="heap_lock_updated_tuple", wl="lockchain",
      actions=dict(error=8, wake=3, cancel=3, terminate=2)),
    # AtEOXact_Inval(true) is post-commit: an injected error unwinds into
    # AbortTransaction on an already-committed xid -> "cannot abort
    # transaction ... already committed" PANIC is the designed backstop.
    P(name="transaction-end-process-inval", wl="ddl_commit",
      post_commit=True, panic_ok=True,
      actions=dict(error=8, wake=3, cancel=3, terminate=2)),
    # InvalidateCatalogSnapshot runs mid-txn on sinval catchup AND
    # post-commit from AtEOXact_Snapshot - the latter PANICs on error.
    P(name="invalidate-catalog-snapshot-end", wl="ddl_commit",
      post_commit=True, panic_ok=True,
      actions=dict(error=8, wake=3, cancel=3, terminate=2)),
    P(name="multixact-create-from-members", wl="multixact", panic_ok=True,
      actions=dict(error=3, wake=3, cancel=2, terminate=2)),
    P(name="inplace-before-pin", wl="inplace",
      actions=dict(error=8, wake=2, cancel=3, terminate=2)),
    # -------------------------------------------- maintenance state machines
    P(name="reindex-relation-concurrently-before-swap", wl="reindex",
      actions=dict(error=8, wake=3, cancel=3, terminate=2, immediate=1)),
    P(name="reindex-relation-concurrently-before-set-dead", wl="reindex",
      actions=dict(error=8, wake=2, cancel=3, terminate=2)),
    P(name="reindex-relation-concurrently-before-drop", wl="reindex",
      actions=dict(error=8, wake=2, cancel=3, terminate=2)),
    P(name="reindex-conc-index-built", wl="reindex",
      actions=dict(error=6, wake=2, cancel=2, terminate=2)),
    P(name="reindex-conc-index-safe", wl="reindex",
      actions=dict(error=4, wake=1, cancel=2, terminate=1)),
    P(name="reindex-conc-index-not-safe", wl="reindex_notsafe",
      actions=dict(error=4, wake=1, cancel=2, terminate=1)),
    P(name="define-index-before-set-valid", wl="cic",
      actions=dict(error=8, wake=3, cancel=3, terminate=2, immediate=1)),
    P(name="vacuum-truncate-enabled", wl="vacuum_trunc_on",
      actions=dict(error=5, wake=2, cancel=3, terminate=1)),
    P(name="vacuum-truncate-disabled", wl="vacuum_trunc_off",
      actions=dict(error=5, wake=2, cancel=3, terminate=1)),
    P(name="vacuum-truncate-auto", wl="vacuum",
      actions=dict(error=5, wake=2, cancel=3, terminate=1)),
    P(name="vacuum-index-cleanup-enabled", wl="vacuum_idx_on",
      actions=dict(error=4, wake=1, cancel=2)),
    P(name="vacuum-index-cleanup-disabled", wl="vacuum_idx_off",
      actions=dict(error=4, wake=1, cancel=2)),
    P(name="vacuum-index-cleanup-auto", wl="vacuum",
      actions=dict(error=4, wake=1, cancel=2)),
    P(name="gin-leave-leaf-split-incomplete", wl="gin",
      actions=dict(error=10, wake=3, cancel=3, terminate=2)),
    P(name="gin-leave-internal-split-incomplete", wl="gin_deep",
      actions=dict(error=8, wake=2, cancel=3), best_effort=True),
    P(name="gin-finish-incomplete-split", wl="gin_finish",
      actions=dict(error=6, wake=3, cancel=2), best_effort=True),
    # the launcher parks before registering in pg_stat_activity, so only
    # error (observed via log) and wakeup-probe wake are meaningful
    P(name="datachecksumsworker-launcher-delay", wl="cksum",
      global_=True, probe_wakeup=True,
      actions=dict(error=4, wake=3)),
    P(name="datachecksumsworker-startup-delay", wl="cksum",
      global_=True, actions=dict(error=3, wake=2, terminate=2)),
    P(name="datachecksums-enable-checksums-delay", wl="cksum",
      global_=True, actions=dict(error=3, wake=2, terminate=2)),
    P(name="datachecksums-on-before-checkpoint", wl="cksum",
      global_=True, actions=dict(error=3, wake=2, terminate=2)),
    P(name="datachecksums-on-after-checkpoint", wl="cksum",
      global_=True, actions=dict(error=3, wake=2, terminate=2)),
    # ------------------------------------------------------ timeout / lock
    P(name="deadlock-timeout-fired", wl="deadlock",
      actions=dict(error=10, wake=3, cancel=3, terminate=2)),
    P(name="transaction-timeout", wl="txntimeout", expect_fatal=True,
      actions=dict(error=8, wake=3, cancel=3, terminate=2)),
    P(name="idle-in-transaction-session-timeout", wl="idletimeout",
      expect_fatal=True,
      actions=dict(error=8, wake=3, cancel=3, terminate=2)),
    P(name="ri-before-pk-lock", wl="ri",
      actions=dict(error=10, wake=3, cancel=3, terminate=2)),
    # ------------------------------------------------- bonus mid-mutation
    P(name="nbtree-leave-leaf-split-incomplete", wl="btree",
      actions=dict(error=8, wake=2, cancel=3, terminate=1)),
    P(name="nbtree-leave-internal-split-incomplete", wl="btree_deep",
      actions=dict(error=8, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-finish-incomplete-split", wl="btree_finish",
      actions=dict(error=6, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-leave-page-half-dead", wl="btree_vac",
      actions=dict(error=6, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-finish-half-dead-page-vacuum", wl="btree_vac_finish",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-walk-left", wl="btree_scan",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-walk-left-deleted", wl="btree_scan_race",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-walk-left-restart", wl="btree_scan_race",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-walk-left-step-right", wl="btree_scan_race",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-first-empty", wl="btree_empty",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="nbtree-endpoint-empty", wl="btree_empty",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="exec-init-partition-after-get-partition-ancestors", wl="part",
      actions=dict(error=5, wake=2, cancel=2)),
    P(name="catcache-list-miss-systable-scan-started", wl="catcache",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="typecache-before-rel-type-cache-insert", wl="typecache",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="createdb-before-catalog-insert", wl="createdb",
      actions=dict(error=4, wake=2, cancel=2, terminate=1)),
    P(name="dropdb-after-invalid-marker", wl="dropdb",
      actions=dict(error=4, wake=2, cancel=2, terminate=1)),
    # checkpoint points fire in the checkpointer bgworker - it cannot be
    # cancelled, so only error/wake are meaningful
    P(name="create-checkpoint-initial", wl="checkpoint", global_=True,
      actions=dict(error=4, wake=3)),
    P(name="checkpoint-before-old-wal-removal", wl="checkpoint",
      global_=True, actions=dict(error=4, wake=3)),
    P(name="hash-aggregate-enter-spill-mode", wl="hashagg",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="hash-aggregate-process-batch", wl="hashagg",
      actions=dict(error=4, wake=2, cancel=2), best_effort=True),
    P(name="wait-for-lsn-after-register", wl="waitlsn",
      actions=dict(error=3, wake=2, cancel=2), best_effort=True),
]


def _wl_table():
    return {
        "onconflict": (wl_onconflict_setup,
                       wl_onconflict_op(hit_conflict=True,
                                        serializable=False),
                       wl_onconflict_oracle, {}),
        "onconflict_serializable": (wl_onconflict_setup,
                                    wl_onconflict_op(hit_conflict=True,
                                                     serializable=True),
                                    wl_onconflict_oracle, {}),
        "onconflict_noconflict": (wl_onconflict_setup,
                                  wl_onconflict_op(hit_conflict=False,
                                                   serializable=False),
                                  wl_onconflict_oracle, {}),
        "update": (wl_update_setup, wl_update_op, wl_update_oracle, {}),
        "lockchain": (wl_lockchain_setup, wl_lockchain_op,
                      wl_lockchain_oracle,
                      {"op_wait": wl_lockchain_op_wait,
                       "pre_wait": wl_lockchain_pre_wait}),
        "ddl_commit": (wl_ddl_setup, wl_ddl_op, wl_ddl_oracle, {}),
        "multixact": (wl_multixact_setup, wl_multixact_op,
                      wl_multixact_oracle, {}),
        "reindex": (wl_reindex_setup, wl_reindex_op, wl_reindex_oracle, {}),
        "reindex_notsafe": (wl_reindex_setup, wl_reindex_notsafe_op,
                            wl_reindex_oracle, {}),
        "cic": (wl_cic_setup, wl_cic_op, wl_cic_oracle, {}),
        "vacuum": (wl_vacuum_setup, wl_vacuum_op("VACUUM vt"),
                   wl_vacuum_oracle, {}),
        "vacuum_trunc_on": (wl_vacuum_setup,
                            wl_vacuum_op("VACUUM (TRUNCATE TRUE) vt"),
                            wl_vacuum_oracle, {}),
        "vacuum_trunc_off": (wl_vacuum_setup,
                             wl_vacuum_op("VACUUM (TRUNCATE FALSE) vt"),
                             wl_vacuum_oracle, {}),
        "vacuum_idx_on": (wl_vacuum_setup,
                          wl_vacuum_op("VACUUM (INDEX_CLEANUP ON) vt"),
                          wl_vacuum_oracle, {}),
        "vacuum_idx_off": (wl_vacuum_setup,
                           wl_vacuum_op("VACUUM (INDEX_CLEANUP OFF) vt"),
                           wl_vacuum_oracle, {}),
        "inplace": (wl_vacuum_setup, wl_vacuum_op("VACUUM vt"),
                    wl_vacuum_oracle, {}),
        "gin": (wl_gin_setup, wl_gin_op, wl_gin_oracle, {}),
        "gin_deep": (wl_gin_deep_setup, wl_gin_deep_op, wl_gin_oracle,
                     {}),
        "gin_finish": (wl_gin_setup, wl_gin_finish_op, wl_gin_oracle, {}),
        "btree": (wl_btree_setup, wl_btree_op, wl_btree_oracle, {}),
        "btree_finish": (wl_btree_setup, wl_btree_finish_op,
                         wl_btree_oracle, {}),
        "btree_scan": (wl_btree_scan_setup, wl_btree_scan_op,
                       wl_btree_oracle, {}),
        "btree_scan_race": (wl_btree_scan_setup, wl_btree_race_op,
                            wl_btree_oracle, {}),
        "btree_empty": (wl_btree_empty_setup, wl_btree_empty_op,
                        wl_btree_empty_oracle, {}),
        "btree_deep": (wl_btree_deep_setup, wl_btree_deep_op,
                       wl_btree_oracle, {}),
        "btree_vac": (wl_btree_vac_setup, wl_btree_vac_op,
                      wl_btree_oracle, {}),
        "btree_vac_finish": (wl_btree_vac_setup, wl_btree_vac_finish_op,
                             wl_btree_oracle, {}),
        "deadlock": (wl_deadlock_setup, wl_deadlock_op,
                     wl_deadlock_oracle, {}),
        "txntimeout": (wl_timeout_setup, wl_txntimeout_op,
                       wl_timeout_oracle, {}),
        "idletimeout": (wl_timeout_setup, wl_idletimeout_op,
                        wl_timeout_oracle, {}),
        "ri": (wl_ri_setup, wl_ri_op, wl_ri_oracle, {}),
        "part": (wl_part_setup, wl_part_op, wl_part_oracle, {}),
        "catcache": (wl_catcache_setup, wl_catcache_op,
                     wl_catcache_oracle, {}),
        "typecache": (wl_typecache_setup, wl_typecache_op,
                      wl_typecache_oracle, {}),
        "createdb": (wl_createdb_setup, wl_createdb_op,
                     wl_createdb_oracle, {}),
        "dropdb": (wl_dropdb_setup, wl_dropdb_op, wl_dropdb_oracle, {}),
        "checkpoint": (wl_checkpoint_setup, wl_checkpoint_op,
                       wl_checkpoint_oracle, {}),
        "hashagg": (wl_hashagg_setup, wl_hashagg_op, wl_hashagg_oracle, {}),
        "cksum": (wl_cksum_setup, wl_cksum_op, wl_cksum_oracle, {}),
        "waitlsn": (wl_waitlsn_setup, wl_waitlsn_op, wl_waitlsn_oracle, {}),
    }


def resolve(cfg):
    setup, op, oracle, extra = _wl_table()[cfg["wl"]]
    cfg = dict(cfg)
    cfg["setup"], cfg["op"], cfg["oracle"] = setup, op, oracle
    cfg.update(extra)
    cfg["global"] = cfg.get("global_", False)
    return cfg


# ------------------------------------------------------------------ summary


def write_summary(results, outdir, started, elapsed_s):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "results.json").write_text(
        json.dumps(results, indent=2, default=str))

    hits = [r for r in results if r["hit"]]
    panics = [r for r in results if r["outcome"] == "documented-panic"]
    masked = [r for r in results if r["outcome"] == "commit-masked"]
    unreached = [r for r in results if r["outcome"] == "not-reached"]

    lines = [
        "# pg_inject_sweep summary",
        "",
        f"- build: `{PG_PREFIX}` (PostgreSQL 20devel,"
        " --enable-injection-points)",
        f"- started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}",
        f"- elapsed: {elapsed_s / 60:.1f} min   cases: {len(results)}",
        f"- **hits: {len(hits)}**   documented-panic: {len(panics)}   "
        f"commit-masked: {len(masked)}   not-reached: {len(unreached)}",
        "",
        "## Per-point outcomes",
        "",
        "| point | error | wake | cancel | terminate | immediate |",
        "|---|---|---|---|---|---|",
    ]
    bypt = {}
    for r in results:
        bypt.setdefault(r["point"], {}).setdefault(
            r["action"], []).append(r["outcome"])
    for pt in POINTS:
        name = pt["name"]
        row = bypt.get(name, {})
        cells = []
        for a in ("error", "wake", "cancel", "terminate", "immediate"):
            outs = row.get(a)
            if not outs:
                cells.append("-")
                continue
            c = Counter(outs)
            cells.append(", ".join(f"{k}x{n}" for k, n in c.most_common()))
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")

    if hits:
        lines += ["", "## Hits (verbatim)", ""]
        for r in hits:
            lines.append(
                f"### {r['point']} {r['action']}#{r['rep']} ->"
                f" {r['outcome']}")
            lines.append("```")
            lines.append(json.dumps(r["details"], default=str)[:1500])
            for ln in r.get("fatal_full", r["fatal_log"])[:10]:
                lines.append(f"LOG: {ln}")
            lines.append("```")
    if panics:
        lines += ["", "## Documented panics (commit/abort-critical points;",
                  "expected crash on injection; recovery verified)", ""]
        for r in panics:
            fl = r.get("fatal_full", r["fatal_log"])
            plog = next((ln for ln in fl
                         if "TRAP" in ln or "PANIC" in ln), "")
            lines.append(
                f"- `{r['point']}` {r['action']}#{r['rep']}:"
                f" recovered={r['details'].get('recovered')}"
                f"  `{plog.split('] ', 1)[-1][:220]}`")
    if masked:
        lines += [
            "", "## Commit-masked errors (txn committed despite ERROR)", ""]
        for r in masked[:30]:
            lines.append(
                f"- `{r['point']}` {r['action']}#{r['rep']}:"
                f" {r['details'].get('op_error_first', '')}")
    if unreached:
        lines += ["", "## Not reached", ""]
        seen = Counter(r["point"] for r in unreached)
        for n, cnt in sorted(seen.items()):
            lines.append(f"- `{n}` x{cnt}")
    (outdir / "SUMMARY.md").write_text("\n".join(lines) + "\n")


# ------------------------------------------------------------------ main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default=PG_PREFIX)
    ap.add_argument("--datadir", default=None)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--points", default=None,
                    help="comma list of point names (default: all)")
    ap.add_argument("--actions", default=None,
                    help="comma list: error,wake,cancel,terminate,immediate")
    ap.add_argument("--reps", type=int, default=None,
                    help="override reps for every action")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=20260922)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    datadir = args.datadir or tempfile.mkdtemp(prefix="pg_inject_sweep_")
    sw = Sweep(datadir, args.prefix)
    print(f"server: {sw.runner.engine_version}  uri: {sw.uri}")
    print(f"datadir: {datadir}  amcheck: {sw.has_amcheck}")

    want_actions = (set(args.actions.split(",")) if args.actions else None)
    cfgs = []
    for pt in POINTS:
        if args.points and pt["name"] not in args.points.split(","):
            continue
        cfg = resolve(pt)
        acts = {}
        for a, n in pt["actions"].items():
            if want_actions and a not in want_actions:
                continue
            acts[a] = args.reps if args.reps is not None else n
        cfg["actions"] = acts
        cfgs.append(cfg)

    started = time.time()
    deadline = started + args.minutes * 60
    stop = False
    try:
        for cfg in cfgs:
            for action, reps in cfg["actions"].items():
                for rep in range(reps):
                    if time.time() > deadline:
                        print("time budget exhausted; stopping")
                        stop = True
                        break
                    try:
                        sw.reset_schema()
                    except Exception as exc:  # noqa: BLE001
                        print(f"schema reset failed: {exc}")
                        if sw.wait_for_server():
                            try:
                                sw.reset_schema()
                            except Exception:  # noqa: BLE001
                                pass
                    t0 = time.monotonic()
                    try:
                        sw.run_case(cfg, action, rep, rng)
                    except Exception as exc:  # noqa: BLE001
                        sw.report(
                            cfg["name"], action, rep, "exception",
                            {"exception": f"{exc}\n"
                             f"{traceback.format_exc()}"},
                            sw.scan_log(), True)
                    took = time.monotonic() - t0
                    if took > 20:
                        print(f"    (slow case {took:.1f}s)")
                if stop:
                    break
            if stop:
                break
    finally:
        try:
            sw.ensure_mon()
            rows = _exec(sw.mon,
                         "SELECT point_name FROM injection_points_list()",
                         fetch=True) or []
            for (p,) in rows:
                detach_safe(sw.mon, p)
        except Exception:  # noqa: BLE001
            pass
        fatal = sw.scan_log()
        if fatal:
            sw.results.append({"point": "_final", "action": "logscan",
                               "rep": 0, "outcome": "hit:fatal-at-end",
                               "hit": True, "details": {},
                               "fatal_log": fatal})
            print("final log scan found fatal lines:")
            for ln in fatal:
                print(f"  {ln}")
        try:
            sw.runner.cleanup()
        except Exception:  # noqa: BLE001
            pass

    elapsed = time.time() - started
    write_summary(sw.results, args.out, started, elapsed)
    nhits = sum(1 for r in sw.results if r["hit"])
    print(f"\n{len(sw.results)} cases, {nhits} hits"
          f" ({elapsed / 60:.1f} min) -> {args.out}")
    return 1 if nhits else 0


if __name__ == "__main__":
    sys.exit(main())
