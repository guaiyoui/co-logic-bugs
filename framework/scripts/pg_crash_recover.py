#!/usr/bin/env python3
"""Minimal crash-recovery oracle for PostgreSQL.

Journal a deterministic DML/DDL workload, stop the postmaster IMMEDIATE
(kill -9 equivalent), restart, then verify:

  - every committed statement's effects are present (durability)
  - uncommitted work is gone (atomicity)
  - heap and index agree (SELECT via seqscan vs index-only/IndexScan)
  - sequences did not leak committed-then-lost values

Optional: wal_consistency_checking='all' makes recovery itself verify WAL.

Extended crash surfaces (--modes, comma list, default all):

  journal          original workload: committed/uncommitted rows, invariants
  two_pc           PREPARE TRANSACTION, crash, COMMIT/ROLLBACK PREPARED
  unlogged         UNLOGGED table must be EMPTY after crash recovery
  temp_table       session temp objects must not block a same-named table
  wal_churn        many small commits, crash mid-write, count consistency
  checkpoint_race  CHECKPOINT; insert; crash subsecond; no partial/garbage

Crashes are delivered as SIGKILL to the postmaster process GROUP (power
-fail equivalent — no shutdown checkpoint, no backend cleanup) or, on
alternating rounds of wal_churn/checkpoint_race, SIGKILL to one backend
(postmaster then restarts the whole cluster through the same WAL redo).

Usage:
  pg_crash_recover.py --prefix /p/pg166_assert [--rounds 5]
  COEVO_PG_EXTRA_OPTS='-c max_prepared_transactions=10' \
      pg_crash_recover.py --prefix /p/pg186_assert --modes two_pc
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg2  # noqa: E402
from targets.postgres_runner import PostgresRunner  # noqa: E402

DDL = [
    "create table jrnl (id serial primary key, k int, v text, ts timestamptz default now())",
    "create index jrnl_k on jrnl(k)",
    "create index jrnl_v on jrnl(v)",
    "create table acct (id int primary key, bal int)",
    "insert into acct values (1, 100), (2, 100), (3, 0)",
    "create table uncomm (x int)",
]

MODES = (
    "journal",
    "two_pc",
    "unlogged",
    "temp_table",
    "wal_churn",
    "checkpoint_race",
)

# Expected-in-log signatures when WE injected a backend SIGKILL: the victim
# backend's signal-9 death plus postmaster killing the other children is
# the crash we ordered, not a bug. Everything else matching the runner's
# fatal regex (PANIC, TRAP:, signal 11, sanitizer output) is a real find.
_BACKEND_KILL_OK_RE = re.compile(
    r"was terminated by signal 9|terminating any other active server "
    r"processes|terminating connection due to unexpected postmaster exit|"
    r"all server processes terminated; reinitializing",
    re.IGNORECASE)


# ------------------------------------------------------------- primitives
def _postmaster_pid(srv) -> int:
    pidfile = Path(srv.datadir) / "postmaster.pid"
    return int(pidfile.read_text().splitlines()[0].strip())


def _wait_pid_dead(pid: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True
        time.sleep(0.05)
    return False


def crash_postmaster(srv) -> int:
    """SIGKILL the whole postmaster process group (power-fail equivalent).

    pg_ctl daemonizes the postmaster into its own session/process group,
    so killpg takes down checkpointer/walwriter/backends too — no orphan
    backend keeps writing to the datadir across the restart.
    """
    pid = _postmaster_pid(srv)
    try:
        pgid = os.getpgid(pid)
        if pgid != os.getpgrp():  # never shoot our own group
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _wait_pid_dead(pid)
    return pid


def crash_backend_pid(pid: int) -> None:
    """SIGKILL one backend; postmaster re-inits shmem + runs crash recovery."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _wait_pid_dead(pid)


def restart(pg: PostgresRunner, srv, method: str) -> list[str]:
    """Bring the cluster back and return the new server-log lines.

    method='postmaster' needs an explicit srv.start(); method='backend'
    leaves the postmaster alive and self-healing — pg_ctl start would just
    complain about the live pid file, so only reconnect (the runner's retry
    loop rides out the postmaster's crash-reinit window).
    """
    pg.close()
    if method == "postmaster":
        srv.start()
    # Outer retry on top of pg.connect's own loop: postmaster re-init after
    # a backend crash can outlast the inner 15 s window on slow ASan builds.
    last_exc = None
    for _ in range(6):
        try:
            pg.connect()
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(2.0)
    else:
        raise RuntimeError(f"server did not come back after {method} "
                           f"crash: {last_exc}")
    return pg.log_new_lines()


def fatal_log_issues(pg: PostgresRunner, new_lines: list[str],
                     method: str) -> list[str]:
    """Filter fresh log lines for real crash signatures."""
    issues = []
    for ln in pg.log_fatal_lines(new_lines):
        if method == "backend" and _BACKEND_KILL_OK_RE.search(ln):
            continue
        issues.append(f"server-log: {ln.strip()}")
    return issues


def _log_tail(srv, n: int = 30) -> str:
    try:
        lines = (Path(srv.datadir) / "local_pg.log").read_text(
            errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _scalar(pg: PostgresRunner, sql: str):
    r = pg.run(sql)
    if not r.ok or not r.rows:
        return None
    return r.rows[0][0]


# ------------------------------------------------------------- mode: journal
def journal(pg: PostgresRunner, rnd: int) -> dict:
    """Run a deterministic DML batch; return the expected committed state."""
    expected = {"inserted": [], "bal": None, "seq_floor": 0}
    # committed inserts (deterministic k/v)
    for i in range(8):
        k, v = rnd * 100 + i, f"r{rnd}v{i}"
        r = pg.run(f"insert into jrnl (k, v) values ({k}, '{v}') returning id")
        if r.ok and r.rows:
            expected["inserted"].append((k, v, r.rows[0][0]))
    # committed transfer (keeps sum invariant = 200)
    amt = 5 + rnd
    pg.run(f"update acct set bal = bal - {amt} where id = 1")
    pg.run(f"update acct set bal = bal + {amt} where id = 3")
    # uncommitted work that must NOT survive
    pg.run("begin")
    pg.run("insert into uncomm values (999)")
    pg.run("insert into jrnl (k, v) values (-999, 'ghost')")
    # deliberate: leave txn open, then kill the server
    return expected


def verify_journal(pg: PostgresRunner, expected: dict, rnd: int) -> list[str]:
    issues = []
    for k, v, jid in expected["inserted"]:
        r = pg.run(f"select v from jrnl where id = {jid}")
        if not r.ok or r.rows != [[v]]:
            issues.append(f"lost committed row id={jid}: {r.rows} err={r.error}")
    r = pg.run("select coalesce(sum(bal), -1) from acct")
    if r.ok and r.rows and r.rows[0][0] != 200:
        issues.append(f"acct sum invariant broken: {r.rows}")
    r = pg.run("select count(*) from uncomm where x = 999")
    if r.ok and r.rows and r.rows[0][0] != 0:
        issues.append("uncommitted row survived crash")
    r = pg.run("select count(*) from jrnl where k = -999")
    if r.ok and r.rows and r.rows[0][0] != 0:
        issues.append("uncommitted jrnl row survived crash")
    # heap-vs-index agreement
    pg.run("set enable_seqscan to on")
    pg.run("set enable_indexscan to off")
    a = pg.run("select k, v from jrnl order by k, v")
    pg.run("set enable_seqscan to off")
    pg.run("set enable_indexscan to on")
    b = pg.run("select k, v from jrnl order by k, v")
    pg.run("reset enable_seqscan")
    pg.run("reset enable_indexscan")
    if a.ok and b.ok and a.rows != b.rows:
        issues.append(f"heap/index divergence: seq={len(a.rows)} idx={len(b.rows)}")
    return issues


def mode_journal(pg: PostgresRunner, srv, rnd: int, method: str):
    if rnd == 0:
        for s in DDL:
            pg.run(s)
    expected = journal(pg, rnd)
    crash_postmaster(srv)
    lines = restart(pg, srv, "postmaster")
    issues = verify_journal(pg, expected, rnd)
    issues += fatal_log_issues(pg, lines, "postmaster")
    ev = f"{len(expected['inserted'])} committed inserts verified"
    return issues, ev


# ------------------------------------------------------------- mode: two_pc
def mode_two_pc(pg: PostgresRunner, srv, rnd: int, method: str):
    """Crash between PREPARE and COMMIT/ROLLBACK PREPARED."""
    mpt = _scalar(pg, "show max_prepared_transactions")
    try:
        mpt = int(mpt)
    except (TypeError, ValueError):
        mpt = 0
    if mpt <= 0:
        return None, "skip: max_prepared_transactions=0"
    pg.run("create table if not exists tpc (id int primary key, v int)")
    gid = f"coevo_r{rnd}"
    pg.run(f"rollback prepared '{gid}'")  # no-op if absent
    pg.run("begin")
    pg.run(f"insert into tpc values ({rnd}, {100 + rnd})")
    r = pg.run(f"prepare transaction '{gid}'")
    if not r.ok:
        if "prepared transactions are disabled" in (r.error or "") \
                or "max_prepared_transactions" in (r.error or ""):
            return None, f"skip: PREPARE failed: {r.error}"
        return [f"PREPARE TRANSACTION failed: {r.error}"], ""
    # OTHER committed data written after PREPARE, before the crash
    pg.run(f"insert into tpc values ({1000 + rnd}, {200 + rnd})")
    crash_postmaster(srv)
    lines = restart(pg, srv, "postmaster")
    issues = fatal_log_issues(pg, lines, "postmaster")
    # prepared xact must still be pending
    n = _scalar(pg, "select count(*) from pg_prepared_xacts "
                    f"where gid = '{gid}'")
    if n != 1:
        issues.append(f"prepared xact '{gid}' missing after restart "
                      f"(pg_prepared_xacts count={n})")
        return issues, f"gid={gid} lost"
    if rnd % 2 == 0:
        r = pg.run(f"commit prepared '{gid}'")
        if not r.ok:
            issues.append(f"COMMIT PREPARED failed: {r.error}")
        r = pg.run(f"select v from tpc where id = {rnd}")
        if not r.ok or r.rows != [[100 + rnd]]:
            issues.append(f"2pc-committed row missing/wrong: {r.rows} "
                          f"err={r.error}")
        ev = f"gid={gid} commit-prepared, row present"
    else:
        r = pg.run(f"rollback prepared '{gid}'")
        if not r.ok:
            issues.append(f"ROLLBACK PREPARED failed: {r.error}")
        r = pg.run(f"select count(*) from tpc where id = {rnd}")
        if not r.ok or r.rows != [[0]]:
            issues.append(f"rolled-back prepared row present: {r.rows}")
        ev = f"gid={gid} rollback-prepared, row absent"
    # the post-PREPARE committed row must survive either way
    r = pg.run(f"select v from tpc where id = {1000 + rnd}")
    if not r.ok or r.rows != [[200 + rnd]]:
        issues.append(f"post-PREPARE committed row lost: {r.rows} "
                      f"err={r.error}")
    return issues, ev


# ------------------------------------------------------------- mode: unlogged
def mode_unlogged(pg: PostgresRunner, srv, rnd: int, method: str):
    """UNLOGGED table must come back EMPTY; LOGGED sibling keeps its rows."""
    pg.run("drop table if exists unl")
    pg.run("create unlogged table unl (id int)")
    pg.run("insert into unl select generate_series(1, 200)")
    pg.run("create table if not exists logged_t (id int)")
    pg.run(f"insert into logged_t values ({rnd})")
    crash_postmaster(srv)
    lines = restart(pg, srv, "postmaster")
    issues = fatal_log_issues(pg, lines, "postmaster")
    r = pg.run("select count(*) from unl")
    if not r.ok:
        issues.append(f"unlogged table unusable after restart: {r.error}")
    elif r.rows != [[0]]:
        issues.append(f"DURABILITY BUG: {r.rows[0][0]} unlogged rows "
                      "survived crash recovery")
    r = pg.run(f"select count(*) from logged_t where id = {rnd}")
    if not r.ok or r.rows != [[1]]:
        issues.append(f"logged-table row lost across crash: {r.rows} "
                      f"err={r.error}")
    # unlogged table must still accept writes post-recovery
    r = pg.run("insert into unl values (1)")
    if not r.ok:
        issues.append(f"unlogged table not writable after restart: {r.error}")
    return issues, "unlogged emptied, logged row kept"


# ----------------------------------------------------------- mode: temp_table
def mode_temp_table(pg: PostgresRunner, srv, rnd: int, method: str):
    """Temp objects of the dead session must not block a same-named table."""
    pg.run("drop table if exists tmp_perm")
    pg.run("create temp table tmp_perm (id int, tag text)")
    r = pg.run("insert into tmp_perm select generate_series(1,50), 't'")
    if not r.ok:
        return [f"temp setup failed: {r.error}"], ""
    crash_postmaster(srv)
    lines = restart(pg, srv, "postmaster")  # fresh session, old temp gone
    issues = fatal_log_issues(pg, lines, "postmaster")
    r = pg.run("create table tmp_perm (id int, tag text)")
    if not r.ok:
        issues.append(f"permanent create blocked by temp leftovers: {r.error}")
        return issues, ""
    pg.run("insert into tmp_perm values (1, 'perm')")
    r = pg.run("select count(*) from tmp_perm")
    if not r.ok or r.rows != [[1]]:
        issues.append(f"temp rows leaked into permanent table: {r.rows}")
    # Force orphan-temp reaping in our slot, then count leftovers.
    pg.run("create temp table reap_probe (x int)")
    pg.run("drop table reap_probe")
    left = _scalar(pg,
                   "select count(*) from pg_class c join pg_namespace n "
                   "on c.relnamespace = n.oid "
                   "where n.nspname ~ '^pg_temp_'")
    ev = f"permanent table usable; pg_temp leftovers after reap={left}"
    return issues, ev


# ------------------------------------------------------------ mode: wal_churn
def mode_wal_churn(pg: PostgresRunner, srv, rnd: int, method: str):
    """~2000 small commits; crash mid-write; committed count must hold."""
    pg.run("drop table if exists churn")
    pg.run("create table churn (id int primary key, v int, pad text)")
    total = 2000
    state = {"committed": 0, "be_pid": None}
    crash_at = 400 + rnd * 200  # kill mid-stream at a different point/round

    def _worker():
        conn = psycopg2.connect(srv.get_uri())
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("select pg_backend_pid()")
        state["be_pid"] = cur.fetchone()[0]
        for i in range(total):
            try:
                cur.execute(
                    "insert into churn values (%s, %s, %s)",
                    (rnd * 1_000_000 + i, i, "x" * 20))
                state["committed"] += 1
            except Exception:  # noqa: BLE001 - we crashed the server
                break
        conn.close()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    # Leave an uncommitted row on the main conn, then crash mid-churn.
    pg.run("begin")
    pg.run("insert into churn values (-1, -1, 'uncommitted')")
    deadline = time.monotonic() + 60
    while state["committed"] < crash_at and t.is_alive() \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    if method == "backend" and state["be_pid"]:
        crash_backend_pid(state["be_pid"])
        lines = restart(pg, srv, "backend")
    else:
        method = "postmaster"
        crash_postmaster(srv)
        lines = restart(pg, srv, "postmaster")
    t.join(timeout=10)
    issues = fatal_log_issues(pg, lines, method)
    committed = state["committed"]
    base = rnd * 1_000_000
    ack_loss = False
    r = pg.run("select count(*) from churn where id >= 0")
    if not r.ok:
        issues.append(f"count after restart failed: {r.error}")
    else:
        cnt = r.rows[0][0]
        # A backend killed mid-commit may have flushed WAL but never sent
        # the ack: the in-flight row (id == base+committed) can then be
        # present while the journal only counts acked commits. One extra
        # row of exactly that id is the legal commit-ack-loss case.
        if cnt == committed + 1:
            extra = _scalar(pg, "select count(*) from churn "
                                f"where id = {base + committed}")
            if extra == 1:
                ack_loss = True
            else:
                issues.append(f"count={cnt} > committed={committed} but "
                              f"in-flight id {base + committed} absent")
        elif cnt > committed + 1:
            issues.append(f"uncommitted rows visible: {cnt} > "
                          f"{committed} (+1 ack-loss margin)")
        elif cnt < committed:
            # kill -9 keeps OS-buffered WAL; losing acked commits is a bug
            issues.append(f"LOST COMMITS: {committed} acked vs {cnt} present")
        # ids must be a contiguous run [base, base+cnt) — a hole means an
        # acked commit vanished while a later one survived
        mm = pg.run("select min(id), max(id) from churn where id >= 0")
        if mm.ok and mm.rows and cnt:
            lo, hi = mm.rows[0]
            if lo != base or hi != base + cnt - 1:
                issues.append(f"non-contiguous survivors: min={lo} max={hi} "
                              f"cnt={cnt} expected [{base},{base + cnt - 1}]")
    r = pg.run("select count(*) from churn where id = -1")
    if r.ok and r.rows and r.rows[0][0] != 0:
        issues.append("uncommitted row survived churn crash")
    r = pg.run("select count(*) from churn where length(pad) <> 20")
    if r.ok and r.rows and r.rows[0][0] != 0:
        issues.append(f"partial/garbage rows: {r.rows[0][0]}")
    ev = f"{committed} commits acked pre-crash, method={method}"
    if ack_loss:
        ev += " (+1 commit-ack-loss row: in-flight commit landed, ack lost)"
    return issues, ev


# ------------------------------------------------------ mode: checkpoint_race
def mode_checkpoint_race(pg: PostgresRunner, srv, rnd: int, method: str):
    """CHECKPOINT; committed insert; subsecond crash; count must be exact."""
    pg.run("drop table if exists ck")
    pg.run("create table ck (id int primary key, v int)")
    pg.run("insert into ck select generate_series(1, 100), 0")
    pg.run("checkpoint")
    pre = _scalar(pg, "select count(*) from ck") or 0
    rid, rval = 1_000_000 + rnd, rnd
    done = threading.Event()
    state_pid = [None]

    conn = psycopg2.connect(srv.get_uri())
    conn.autocommit = True

    def _racer():
        try:
            cur = conn.cursor()
            cur.execute("select pg_backend_pid()")
            state_pid[0] = cur.fetchone()[0]
            cur.execute(f"insert into ck values ({rid}, {rval})")
        except Exception:  # noqa: BLE001 - killed mid-insert is legal
            pass
        finally:
            done.set()

    t = threading.Thread(target=_racer, daemon=True)
    t.start()
    # subsecond crash: give the racer a sliver of time, then kill
    time.sleep(0.005)
    if method == "backend":
        deadline = time.monotonic() + 2
        while state_pid[0] is None and time.monotonic() < deadline:
            time.sleep(0.001)
        if state_pid[0] is not None:
            crash_backend_pid(state_pid[0])
            lines = restart(pg, srv, "backend")
        else:  # backend never got going — kill postmaster instead
            method = "postmaster"
            crash_postmaster(srv)
            lines = restart(pg, srv, "postmaster")
    else:
        crash_postmaster(srv)
        lines = restart(pg, srv, "postmaster")
    t.join(timeout=10)
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    issues = fatal_log_issues(pg, lines, method)
    post = _scalar(pg, "select count(*) from ck")
    if post is None:
        issues.append("count query failed after restart")
    elif post == pre:
        ev = (f"count={post} == pre-checkpoint committed; post-checkpoint "
              f"insert lost (valid: async-commit window), method={method}")
    elif post == pre + 1:
        r = pg.run(f"select v from ck where id = {rid}")
        if not r.ok or r.rows != [[rval]]:
            issues.append(f"garbage post-checkpoint row: {r.rows} "
                          f"err={r.error}")
        ev = (f"count={post} == pre+1; committed insert survived, "
              f"method={method}")
    else:
        issues.append(f"inconsistent count: pre={pre} post={post}")
        ev = ""
    return issues, ev


MODE_FUNCS = {
    "journal": mode_journal,
    "two_pc": mode_two_pc,
    "unlogged": mode_unlogged,
    "temp_table": mode_temp_table,
    "wal_churn": mode_wal_churn,
    "checkpoint_race": mode_checkpoint_race,
}

# modes that exercise the backend-kill variant on odd rounds
_BACKEND_KILL_MODES = {"wal_churn", "checkpoint_race"}


def run_mode(mode: str, args) -> dict:
    """One fresh datadir per mode; `rounds` crash cycles inside it."""
    datadir = tempfile.mkdtemp(prefix=f"pgcrash_{mode}_")
    pg = PostgresRunner(datadir, pg_prefix=args.prefix)
    srv = pg._server
    result = {"mode": mode, "datadir": datadir, "rounds": [], "status": "OK"}
    try:
        if args.wal_check:
            pg.run("alter system set wal_consistency_checking = 'all'")
            srv.stop()
            srv.start()
            pg.connect()
        for rnd in range(args.rounds):
            method = "postmaster"
            if mode in _BACKEND_KILL_MODES and rnd % 2 == 1:
                method = "backend"
            try:
                issues, evidence = MODE_FUNCS[mode](pg, srv, rnd, method)
            except Exception as exc:  # noqa: BLE001 - harness must not die
                issues, evidence = (
                    [f"harness exception: {type(exc).__name__}: {exc}"], "")
            if issues is None:
                status = "SKIP"
            elif issues:
                status = "ISSUES"
            else:
                status = "OK"
            rec = {"round": rnd, "method": method, "status": status,
                   "issues": issues or [], "evidence": evidence}
            if status == "ISSUES":
                rec["log_tail"] = _log_tail(srv)
                result["status"] = "FAIL"
            result["rounds"].append(rec)
            print(f"  {mode} round {rnd} [{method}]: {status} — {evidence}")
            for i in (issues or []):
                print(f"      {i}")
            # fresh session for the next round (keep on-disk state)
            try:
                pg.close()
                pg.connect()
            except Exception as exc:  # noqa: BLE001
                rec = {"round": rnd, "method": method, "status": "ISSUES",
                       "issues": [f"post-round reconnect failed: {exc}"],
                       "evidence": "", "log_tail": _log_tail(srv)}
                result["rounds"].append(rec)
                result["status"] = "FAIL"
                print(f"  {mode} post-round reconnect: ISSUES — {exc}")
                break
        return result
    finally:
        pg.close()
        pg.cleanup()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--modes", default="all",
                    help="comma list of modes, or 'all' (default)")
    ap.add_argument("--wal-check", action="store_true",
                    help="enable wal_consistency_checking='all'")
    ap.add_argument("--out", help="write a JSON report to this path")
    args = ap.parse_args()

    if args.modes.strip().lower() in ("all", "*"):
        modes = list(MODES)
    else:
        modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODE_FUNCS]
    if unknown:
        ap.error(f"unknown mode(s): {', '.join(unknown)} "
                 f"(choose from {', '.join(MODES)})")

    print(f"prefix={args.prefix} modes={','.join(modes)} "
          f"rounds={args.rounds} wal_check={args.wal_check}")
    report = {"prefix": args.prefix, "modes": {}, "fail": 0, "skip": 0}
    for mode in modes:
        res = run_mode(mode, args)
        report["modes"][mode] = res
        statuses = [r["status"] for r in res["rounds"]]
        if all(s == "SKIP" for s in statuses):
            report["skip"] += 1
        report["fail"] += sum(1 for s in statuses if s == "ISSUES")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"report written to {out}")

    n_issues = report["fail"]
    print(f"\n{n_issues} issue round(s) across "
          f"{sum(len(m['rounds']) for m in report['modes'].values())} "
          "crash rounds")
    return 1 if n_issues else 0


if __name__ == "__main__":
    sys.exit(main())
