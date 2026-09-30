#!/usr/bin/env python3
"""pg_upgrade multixact-rewrite torture: 18.6 -> 20devel.

New-code target: src/bin/pg_upgrade/multixact_rewrite.c +
multixact_read_v18.c + slru_io.c on master — conversion of
pg_multixact/{offsets,members} from the pre-v19 32-bit-offset format to
64-bit offsets during pg_upgrade.  Upstream has exactly one TAP test
(007_multixact_conversion.pl); nobody fuzzes member shapes.

Per round:
  1. initdb + start a pg186 cluster, generate heavy multixact pressure
     (pairs, many-member, subxact members, FK-driven lockers, aborted
     members, xmax chains after update).
  2. Capture pre-upgrade state: per-table content bags, pageinspect
     HEAP_XMAX_IS_MULTI bit counts, pg_controldata NextMultiXactId /
     NextMultiOffset, pg_database.datminmxid / relminmxid.
  3. Stop, initdb pgmaster datadir, pg_upgrade --link.
  4. Start upgraded cluster; verify: content bags identical, multi-bit
     counts identical, lock semantics correct (FOR UPDATE NOWAIT /
     FOR SHARE obtainable — old lockers all committed/aborted),
     relminmxid <= nextMultiXactId, datminmxid sane, amcheck
     bt_index_check on every index, pg_get_multixact_stats() sane,
     new DML + a NEW post-upgrade multixact + VACUUM clean.

Oracle notes:
  * nextMultiOffset after rewrite legitimately restarts near offset 1
    ("new members always start from offset 1") — NOT a regression
    signal.  nextMultiXactId must be >= old value (ids preserved).
  * --link mutates the old datadir; never restart the old cluster.

Usage: pg_upgrade_mxact.py [--rounds N] [--seed S] [--keep-all]
                           [--old-prefix P] [--new-prefix P]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from util.paths import pg_build_prefix  # noqa: E402

DEF_OLD = pg_build_prefix("pg186_assert")
DEF_NEW = pg_build_prefix("pgmaster_assert")
DEF_WORK = str(str(Path(__file__).resolve().parent.parent) / "results" / "pg_upgrade_mxact")

_PORT_BASE = 55100


def run(args: list[str], cwd: str | None = None, timeout: int = 300,
        env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, cwd=cwd, env=env)


class RawPg:
    """Minimal socket-only postmaster on a given install prefix."""

    def __init__(self, prefix: str, datadir: Path, sockdir: Path, port: int,
                 extra_opts: str = ""):
        self.prefix = Path(prefix)
        self.bin = self.prefix / "bin"
        self.datadir = Path(datadir)
        self.sockdir = Path(sockdir)
        self.port = port
        self.extra_opts = extra_opts

    def initdb(self) -> None:
        p = run([str(self.bin / "initdb"), "-D", str(self.datadir),
                 "-A", "trust", "-N", "--no-instructions"])
        if p.returncode != 0:
            raise RuntimeError(f"initdb {self.datadir}: {p.stderr[-2000:]}")

    def start(self) -> None:
        self.sockdir.mkdir(parents=True, exist_ok=True)
        self.sockdir.chmod(0o700)
        opts = (f"-k {self.sockdir} -p {self.port} -c listen_addresses='' "
                "-c fsync=off -c autovacuum=off -c max_connections=60 "
                "-c max_locks_per_transaction=512 "
                "-c max_prepared_transactions=10 "
                + self.extra_opts)
        log = self.datadir.parent / (self.datadir.name + ".postmaster.log")
        p = run([str(self.bin / "pg_ctl"), "-D", str(self.datadir),
                 "-l", str(log), "-o", opts, "-w", "start"], timeout=120)
        if p.returncode != 0:
            raise RuntimeError(f"start {self.datadir}: {p.stdout} {p.stderr}")

    def stop(self, mode: str = "fast") -> None:
        run([str(self.bin / "pg_ctl"), "-D", str(self.datadir),
             "-m", mode, "-w", "stop"], timeout=120)

    def connect(self):
        c = psycopg2.connect(host=str(self.sockdir), port=self.port,
                             dbname="postgres")
        c.autocommit = True
        return c

    def controldata(self) -> dict[str, str]:
        p = run([str(self.bin / "pg_controldata"), "-D", str(self.datadir)])
        d = {}
        for line in p.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                d[k.strip()] = v.strip()
        return d


# ---------------------------------------------------------------- workload

def sql(cur, q, args=None, fetch=False):
    cur.execute(q, args)
    return cur.fetchall() if fetch else None


def build_multixact_pressure(conn_factory, rnd_seed: int,
                             scale: float = 1.0) -> dict:
    """Generate diverse multixact shapes on the OLD cluster.

    Returns a manifest dict with sample row ids per category for
    post-upgrade lock-semantics checks.
    """
    manifest: dict = {"pair_rows": [], "many_rows": [], "subx_rows": [],
                      "fk_parents": [], "chain_rows": [], "abort_rows": []}

    # Dedicated locker connections (each owns one open txn at a time).
    lockers = [conn_factory() for _ in range(8)]
    lcur = [c.cursor() for c in lockers]
    for c in lockers:
        c.autocommit = False
    adm = conn_factory()
    cur = adm.cursor()

    cur.execute("CREATE EXTENSION pageinspect")

    # --- table set -----------------------------------------------------
    cur.execute("""CREATE TABLE mx_pair (id int primary key, v text)""")
    cur.execute("""CREATE TABLE mx_many (id int primary key, v text)""")
    cur.execute("""CREATE TABLE mx_subx (id int primary key, v text)""")
    cur.execute("""CREATE TABLE mx_fk_p (id int primary key, v text)""")
    cur.execute("""CREATE TABLE mx_fk_c (id serial primary key, pid int
                   references mx_fk_p(id) on update cascade on delete cascade,
                   v text)""")
    cur.execute("""CREATE TABLE mx_chain (id int primary key, v text)""")
    cur.execute("""CREATE TABLE mx_abort (id int primary key, v text)""")
    # Arm 7 is the decisive one: xmax is a multixact {locker, updater}
    # carrying the UPDATED bit.  pg_upgrade's own `vacuumdb --freeze`
    # consults the *converted* SLRU members to decide each tuple's fate:
    # if the 32->64-bit rewrite corrupts a member's commit status, a
    # committed-updater tuple resurrects or an aborted-updater tuple
    # vanishes.  We record per-row expectation and check it exactly.
    cur.execute("""CREATE TABLE mx_upd (id int primary key, v text)""")

    n_pair = int((60 + (rnd_seed % 3) * 40) * scale)
    n_many = int(24 * scale)
    n_subx = int(20 * scale)
    n_fkp = int(15 * scale)
    n_chain = int(30 * scale)
    n_abort = int(25 * scale)
    n_upd = int(40 * scale)

    for i in range(1, n_pair + 1):
        cur.execute("INSERT INTO mx_pair VALUES (%s, %s)", (i, f"p{i}"))
    for i in range(1, n_many + 1):
        cur.execute("INSERT INTO mx_many VALUES (%s, %s)", (i, f"m{i}"))
    for i in range(1, n_subx + 1):
        cur.execute("INSERT INTO mx_subx VALUES (%s, %s)", (i, f"s{i}"))
    for i in range(1, n_fkp + 1):
        cur.execute("INSERT INTO mx_fk_p VALUES (%s, %s)", (i, f"f{i}"))
    for i in range(1, n_chain + 1):
        cur.execute("INSERT INTO mx_chain VALUES (%s, %s)", (i, f"c{i}"))
    for i in range(1, n_abort + 1):
        cur.execute("INSERT INTO mx_abort VALUES (%s, %s)", (i, f"a{i}"))

    # Advance the xid counter so member xids span a gap, and so later
    # member xids are far apart from earlier ones.
    churn = 150 + (rnd_seed % 4) * 150
    cur.execute("SELECT txid_current() FROM generate_series(1, %s)",
                (churn,))

    # --- arm 1: pair lockers -> one distinct multixact per row ---------
    # Fresh BEGIN/COMMIT per row so every member pair is a distinct
    # MultiXactId -> hundreds of converted multixacts, and members land
    # at increasing 32-bit offsets.
    for i in range(1, n_pair + 1):
        ca, cb = lcur[i % 8], lcur[(i + 3) % 8]
        ca.execute("BEGIN")
        cb.execute("BEGIN")
        ca.execute("SELECT id FROM mx_pair WHERE id=%s FOR KEY SHARE", (i,))
        cb.execute("SELECT id FROM mx_pair WHERE id=%s FOR KEY SHARE", (i,))
        ca.execute("COMMIT")
        cb.execute("COMMIT")
    manifest["pair_rows"] = [1, n_pair // 2, n_pair]

    # --- arm 2: many-member multixacts (3-6 members) -------------------
    for base in range(1, n_many + 1, 4):
        hi = min(base + 3, n_many)
        k = 3 + (base % 4)                      # 3..6 lockers
        chosen = list(range(k))
        for j in chosen:
            lcur[j].execute("BEGIN")
        for j in chosen:
            lcur[j].execute(
                "SELECT id FROM mx_many WHERE id BETWEEN %s AND %s "
                "ORDER BY id FOR KEY SHARE", (base, hi))
        for j in chosen:
            lcur[j].execute("COMMIT")
    manifest["many_rows"] = [1, n_many // 2, n_many]

    # --- arm 3: subxact members via SAVEPOINT lockers ------------------
    for i in range(1, n_subx + 1):
        ca, cb = lcur[6], lcur[7]
        ca.execute("BEGIN")
        ca.execute("SAVEPOINT sp1")
        ca.execute("SELECT id FROM mx_subx WHERE id=%s FOR KEY SHARE", (i,))
        ca.execute("RELEASE SAVEPOINT sp1")
        cb.execute("BEGIN")
        cb.execute("SAVEPOINT sp2")
        cb.execute("SELECT id FROM mx_subx WHERE id=%s FOR SHARE", (i,))
        cb.execute("RELEASE SAVEPOINT sp2")
        ca.execute("COMMIT")
        cb.execute("COMMIT")
    manifest["subx_rows"] = [1, n_subx // 2, n_subx]

    # --- arm 4: FK-driven multixacts on parent rows --------------------
    # Concurrent child inserts from distinct xacts take FOR KEY SHARE on
    # the parent -> multixact on mx_fk_p rows.
    for pid in range(1, n_fkp + 1):
        a, b = lcur[2], lcur[3]
        a.execute("BEGIN")
        b.execute("BEGIN")
        a.execute("INSERT INTO mx_fk_c(pid, v) VALUES (%s, %s)",
                  (pid, f"ca{pid}"))
        b.execute("INSERT INTO mx_fk_c(pid, v) VALUES (%s, %s)",
                  (pid, f"cb{pid}"))
        a.execute("COMMIT")
        b.execute("COMMIT")
    manifest["fk_parents"] = [1, n_fkp // 2, n_fkp]

    # --- arm 5: xmax chains — multixact then UPDATE --------------------
    # Old tuple keeps a multixact xmax; live tuple gets plain xmax.
    for i in range(1, n_chain + 1):
        a, b = lcur[4], lcur[5]
        a.execute("BEGIN")
        b.execute("BEGIN")
        a.execute("SELECT id FROM mx_chain WHERE id=%s FOR KEY SHARE", (i,))
        b.execute("SELECT id FROM mx_chain WHERE id=%s FOR KEY SHARE", (i,))
        a.execute("COMMIT")
        b.execute("COMMIT")
        if i % 2 == 0:
            cur.execute("UPDATE mx_chain SET v=%s WHERE id=%s",
                        (f"c{i}u", i))
    manifest["chain_rows"] = [2, n_chain // 2, n_chain]

    # --- arm 6: aborted members ----------------------------------------
    for i in range(1, n_abort + 1):
        a, b = lcur[0], lcur[1]
        a.execute("BEGIN")
        b.execute("BEGIN")
        a.execute("SELECT id FROM mx_abort WHERE id=%s FOR KEY SHARE", (i,))
        b.execute("SELECT id FROM mx_abort WHERE id=%s FOR KEY SHARE", (i,))
        a.execute("ROLLBACK")           # aborted member
        b.execute("COMMIT")
    manifest["abort_rows"] = [1, n_abort // 2, n_abort]

    # --- arm 7: updater IS a multixact member --------------------------
    # Lockers A and B both FOR KEY SHARE -> xmax = multi{A,B}; then B
    # UPDATEs the row inside the same txn -> xmax stays multi{A,B} with
    # HEAP_XMAX_UPDATED and B recorded as the updater member.
    # Even ids: B commits -> the OLD tuple must be dead post-upgrade.
    # Odd ids:  B aborts   -> the OLD tuple must remain live (the row
    # keeps its original value), and the multi has an aborted member.
    for i in range(1, n_upd + 1):
        cur.execute("INSERT INTO mx_upd VALUES (%s, %s)", (i, f"u{i}"))
        a, b = lcur[5], lcur[6]
        a.execute("BEGIN")
        b.execute("BEGIN")
        a.execute("SELECT id FROM mx_upd WHERE id=%s FOR KEY SHARE", (i,))
        b.execute("SELECT id FROM mx_upd WHERE id=%s FOR KEY SHARE", (i,))
        b.execute("UPDATE mx_upd SET v=%s WHERE id=%s", (f"u{i}NEW", i))
        a.execute("COMMIT")
        if i % 2 == 0:
            b.execute("COMMIT")         # committed updater -> new live ver
            manifest.setdefault("upd_dead", []).append(i)
        else:
            b.execute("ROLLBACK")       # aborted updater -> orig survives
            manifest.setdefault("upd_live", []).append(i)
    manifest["upd_rows"] = [1, n_upd // 2, n_upd]

    # A final long pair on every table's first row: multixacts created
    # at the *largest* xid distance from the earliest ones.
    cur.execute("SELECT txid_current() FROM generate_series(1, %s)",
                (churn // 2,))
    a, b = lcur[0], lcur[7]
    a.execute("BEGIN"); b.execute("BEGIN")
    for t in ("mx_pair", "mx_many", "mx_chain"):
        a.execute(f"SELECT id FROM {t} WHERE id=1 FOR KEY SHARE")
        b.execute(f"SELECT id FROM {t} WHERE id=1 FOR SHARE")
    a.execute("COMMIT"); b.execute("COMMIT")

    cur.execute("CHECKPOINT")
    return manifest


# ---------------------------------------------------------------- oracles

def table_bag(cur, table: str) -> str:
    cur.execute(
        f"SELECT md5(string_agg(t::text, E'\\n' ORDER BY t::text)) "
        f"FROM {table} t")
    return cur.fetchone()[0] or "empty"


def multi_bit_counts(cur) -> dict[str, int]:
    """# of live heap tuples whose t_infomask has HEAP_XMAX_IS_MULTI."""
    out = {}
    for t in ("mx_pair", "mx_many", "mx_subx", "mx_fk_p",
              "mx_chain", "mx_abort", "mx_upd"):
        try:
            cur.execute(
                "SELECT count(*) FROM heap_page_items(get_raw_page(%s,0)) "
                "WHERE t_infomask::int & 4096 <> 0", (t,))  # IS_MULTI=0x1000
            out[t] = cur.fetchone()[0]
        except psycopg2.Error:
            out[t] = -1
    return out


def catalog_sanity(cur, next_multi: int) -> list[str]:
    issues = []
    cur.execute("SELECT datname, datminmxid::text::bigint FROM pg_database")
    for name, dmm in cur.fetchall():
        if int(dmm) > next_multi:
            issues.append(f"db {name} datminmxid {dmm} > nextMulti {next_multi}")
    cur.execute("""SELECT relname, relminmxid::text::bigint FROM pg_class
                   WHERE relkind IN ('r','m','t')
                   AND relminmxid::text::bigint > %s""",
                (next_multi,))
    for rel, rmm in cur.fetchall():
        issues.append(f"rel {rel} relminmxid {rmm} > nextMulti {next_multi}")
    return issues


def amcheck_all(cur) -> list[str]:
    issues = []
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS amcheck")
    except psycopg2.Error as e:
        return [f"amcheck load failed: {e}"]
    cur.execute("""SELECT n.nspname || '.' || quote_ident(c.relname)
                   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   JOIN pg_index i ON i.indexrelid=c.oid
                   JOIN pg_class t ON t.oid=i.indrelid
                   WHERE c.relkind='i' AND n.nspname IN ('public','pg_toast')
                   AND t.relname LIKE 'mx\\_%' ESCAPE '\\'
                   ORDER BY 1""")
    for (rel,) in cur.fetchall():
        try:
            cur.execute(f"SELECT bt_index_check('{rel}'::regclass)")
        except psycopg2.Error as e:
            issues.append(f"amcheck {rel}: {str(e).strip()[:200]}")
    return issues


def lock_semantics(conn_factory, manifest: dict) -> list[str]:
    issues = []
    c = conn_factory()
    cur = c.cursor()
    probes = [
        ("mx_pair", manifest["pair_rows"]),
        ("mx_many", manifest["many_rows"]),
        ("mx_subx", manifest["subx_rows"]),
        ("mx_fk_p", manifest["fk_parents"]),
        ("mx_chain", manifest["chain_rows"]),
        ("mx_abort", manifest["abort_rows"]),
        ("mx_upd", manifest["upd_rows"]),
    ]
    for table, ids in probes:
        for rid in ids:
            try:
                cur.execute(
                    f"SELECT id FROM {table} WHERE id=%s FOR UPDATE NOWAIT",
                    (rid,))
                if cur.fetchone() is None:
                    issues.append(f"{table} id={rid}: row vanished")
            except psycopg2.Error as e:
                issues.append(f"{table} id={rid} FOR UPDATE NOWAIT: "
                              f"{str(e).strip()[:160]}")
            try:
                cur.execute(
                    f"SELECT id FROM {table} WHERE id=%s FOR SHARE NOWAIT",
                    (rid,))
            except psycopg2.Error as e:
                issues.append(f"{table} id={rid} FOR SHARE NOWAIT: "
                              f"{str(e).strip()[:160]}")
    c.close()
    return issues


TABLES = ["mx_pair", "mx_many", "mx_subx", "mx_fk_p", "mx_fk_c",
          "mx_chain", "mx_abort", "mx_upd"]


def upd_liveness(cur, manifest: dict) -> list[str]:
    """Verify per-row visibility of arm-7 tuples post-upgrade.

    Even ids: committed updater -> exactly one row, value 'u<id>NEW'.
    Odd ids:  aborted updater   -> exactly one row, value 'u<id>'.
    Any deviation means the converted multixact's member statuses are
    wrong (data corruption by pg_upgrade's freeze pass).
    """
    issues = []
    for i in manifest.get("upd_dead", []):
        cur.execute("SELECT v FROM mx_upd WHERE id=%s", (i,))
        rows = cur.fetchall()
        if rows != [(f"u{i}NEW",)]:
            issues.append(f"mx_upd id={i}: expected committed update "
                          f"'u{i}NEW', got {rows}")
    for i in manifest.get("upd_live", []):
        cur.execute("SELECT v FROM mx_upd WHERE id=%s", (i,))
        rows = cur.fetchall()
        if rows != [(f"u{i}",)]:
            issues.append(f"mx_upd id={i}: aborted update should leave "
                          f"'u{i}', got {rows}")
    return issues


def one_round(rnd: int, args, workroot: Path) -> dict:
    res: dict = {"round": rnd, "status": "ok", "issues": [],
                 "upgrade_stdout_tail": ""}
    rdir = workroot / f"round_{rnd}"
    old_d, new_d = rdir / "old", rdir / "new"
    # Short socket dirs: unix socket paths are capped at ~107 bytes.
    sd_old = Path(f"/tmp/pgxu_{rnd}_o")
    sd_new = Path(f"/tmp/pgxu_{rnd}_n")
    rdir.mkdir(parents=True, exist_ok=True)
    old = RawPg(args.old_prefix, old_d, sd_old, _PORT_BASE + rnd * 2)
    new = RawPg(args.new_prefix, new_d, sd_new, _PORT_BASE + rnd * 2 + 1)
    new_extra = "-c shared_preload_libraries=''"  # plain

    try:
        # ---- 1. old cluster + pressure -------------------------------
        old.initdb()
        old.start()
        conn = old.connect()
        manifest = build_multixact_pressure(old.connect, rnd,
                                            args.scale)
        cur = conn.cursor()
        pre_bags = {t: table_bag(cur, t) for t in TABLES}
        pre_multi_bits = multi_bit_counts(cur)
        cur.execute("SELECT datminmxid FROM pg_database WHERE datname='postgres'")
        pre_datminmxid = cur.fetchone()[0]
        cur.execute("SELECT pg_current_wal_lsn()")   # force WAL flush point
        conn.close()

        pre_cd = old.controldata()
        pre_nextmulti = int(pre_cd.get(
            "Latest checkpoint's NextMultiXactId", "0").split()[0])
        res["pre_nextmulti"] = pre_nextmulti
        res["pre_nextoffset"] = pre_cd.get("Latest checkpoint's NextMultiOffset")
        res["pre_datminmxid"] = pre_datminmxid
        res["pre_multi_bits"] = pre_multi_bits
        if not any(v > 0 for v in pre_multi_bits.values()):
            res["status"] = "fail"
            res["issues"].append(
                "SETUP BUG: no HEAP_XMAX_IS_MULTI tuples on old cluster "
                "— multixact pressure never materialized")
        if pre_nextmulti <= 1:
            res["status"] = "fail"
            res["issues"].append(
                "SETUP BUG: old cluster has nextMultiXactId<=1 — "
                "no multixacts were generated; upgrade is vacuous")
        old.stop()

        # ---- 2. pg_upgrade -------------------------------------------
        new.initdb()
        up = run([str(new.bin / "pg_upgrade"),
                  "-d", str(old_d), "-D", str(new_d),
                  "-b", str(old.bin), "-B", str(new.bin),
                  "--link"],
                 cwd=str(rdir), timeout=600)
        res["upgrade_rc"] = up.returncode
        res["upgrade_stdout_tail"] = (up.stdout or "")[-3000:]
        if up.returncode != 0:
            res["status"] = "fail"
            res["issues"].append(
                f"pg_upgrade rc={up.returncode}: "
                f"{(up.stdout or up.stderr)[-1200:]}")
            return res

        # scan pg_upgrade log dir for ERROR/PANIC
        for lf in rdir.rglob("*.log"):
            txt = lf.read_text(errors="replace")
            for line in txt.splitlines():
                if re.search(r"\b(ERROR|FATAL|PANIC)\b", line):
                    res["issues"].append(f"{lf.name}: {line.strip()[:200]}")
                    res["status"] = "fail"

        # ---- 3. post-upgrade verification ----------------------------
        new.extra_opts = new_extra
        new.start()
        nconn = new.connect()
        ncur = nconn.cursor()
        ncur.execute("CREATE EXTENSION IF NOT EXISTS pageinspect")

        post_bags = {t: table_bag(ncur, t) for t in TABLES}
        for t in TABLES:
            if pre_bags[t] != post_bags[t]:
                res["status"] = "fail"
                res["issues"].append(
                    f"DATA MISMATCH {t}: pre={pre_bags[t]} post={post_bags[t]}")

        post_multi_bits = multi_bit_counts(ncur)
        res["post_multi_bits"] = post_multi_bits

        live_issues = upd_liveness(ncur, manifest)
        res["issues"] += live_issues
        if live_issues:
            res["status"] = "fail"

        # Decisive member-integrity oracle: VACUUM (FREEZE) on the
        # upgraded cluster must resolve every converted multixact's
        # members as finished and clear IS_MULTI xmax — same as it does
        # for freshly-created multis (verified baseline: 4496 -> 2816).
        # If the 32->64 rewrite wrote wrong member xids/flags, members
        # look "running" forever and xmax can never be invalidated.
        for t in ("mx_pair", "mx_many", "mx_subx", "mx_fk_p",
                  "mx_chain", "mx_abort", "mx_upd"):
            try:
                ncur.execute(f"VACUUM (FREEZE) {t}")
                ncur.execute(
                    "SELECT count(*) FROM heap_page_items("
                    "get_raw_page(%s,0)) WHERE t_infomask::int & 4096 <> 0",
                    (t,))
                left = ncur.fetchone()[0]
                if left > 0:
                    res["status"] = "fail"
                    res["issues"].append(
                        f"{t}: {left} tuples keep IS_MULTI xmax after "
                        f"post-upgrade FREEZE — converted multixact "
                        f"members unreadable/phantom-running")
            except psycopg2.Error as e:
                res["status"] = "fail"
                res["issues"].append(
                    f"post-upgrade FREEZE on {t}: {str(e).strip()[:200]}")

        post_cd = new.controldata()
        post_nextmulti = int(post_cd.get(
            "Latest checkpoint's NextMultiXactId", "0").split()[0])
        res["post_nextmulti"] = post_nextmulti
        res["post_nextoffset"] = post_cd.get(
            "Latest checkpoint's NextMultiOffset")
        if post_nextmulti < pre_nextmulti:
            res["status"] = "fail"
            res["issues"].append(
                f"nextMultiXactId regressed: pre={pre_nextmulti} "
                f"post={post_nextmulti}")

        res["issues"] += catalog_sanity(ncur, post_nextmulti)

        lock_issues = lock_semantics(new.connect, manifest)
        res["issues"] += lock_issues
        if lock_issues:
            res["status"] = "fail"

        ac = amcheck_all(ncur)
        res["issues"] += ac
        if ac:
            res["status"] = "fail"

        # new-code introspection function
        try:
            ncur.execute("SELECT * FROM pg_get_multixact_stats()")
            res["mxact_stats"] = str(ncur.fetchall()[:4])
        except psycopg2.Error as e:
            res["mxact_stats"] = f"err: {str(e).strip()[:120]}"

        # ---- 4. post-upgrade write activity ---------------------------
        try:
            ncur.execute("INSERT INTO mx_pair VALUES (900001,'new'),"
                         "(900002,'new2')")
            ncur.execute("UPDATE mx_pair SET v='x' WHERE id=900001")
            # fresh post-upgrade multixact on upgraded cluster
            c1, c2 = new.connect(), new.connect()
            c1.autocommit = c2.autocommit = False
            q1, q2 = c1.cursor(), c2.cursor()
            q1.execute("BEGIN")
            q2.execute("BEGIN")
            q1.execute("SELECT id FROM mx_pair WHERE id=900002 FOR KEY SHARE")
            q2.execute("SELECT id FROM mx_pair WHERE id=900002 FOR KEY SHARE")
            q1.execute("COMMIT")
            q2.execute("COMMIT")
            c1.close(); c2.close()
            ncur.execute("VACUUM mx_pair")
            ncur.execute("CHECKPOINT")
        except psycopg2.Error as e:
            res["status"] = "fail"
            res["issues"].append(f"post-upgrade DML: {str(e).strip()[:200]}")
        nconn.close()
        new.stop()

        # post-stop log scan
        for lf in (rdir / "new.postmaster.log",):
            if lf.exists():
                for line in lf.read_text(errors="replace").splitlines():
                    if re.search(r"\b(PANIC|assertion|Assert)\b", line,
                                 re.IGNORECASE):
                        res["status"] = "fail"
                        res["issues"].append(
                            f"new pm log: {line.strip()[:200]}")

    except Exception as e:
        res["status"] = "fail"
        res["issues"].append(f"EXC: {type(e).__name__}: {e}")
    finally:
        try:
            old.stop()
        except Exception:
            pass
        try:
            new.stop()
        except Exception:
            pass

    if res["status"] == "fail" or not args.keep_all:
        pass  # keep failing dirs; cleanup below only when ok
    if res["status"] == "ok" and not args.keep_all:
        shutil.rmtree(rdir, ignore_errors=True)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--old-prefix", default=DEF_OLD)
    ap.add_argument("--new-prefix", default=DEF_NEW)
    ap.add_argument("--workdir", default=DEF_WORK)
    ap.add_argument("--keep-all", action="store_true")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="multiply per-table pressure sizes")
    args = ap.parse_args()

    workroot = Path(args.workdir).resolve()
    workroot.mkdir(parents=True, exist_ok=True)
    results = []
    for i in range(args.rounds):
        rnd = args.seed + i
        t0 = time.time()
        r = one_round(rnd, args, workroot)
        r["secs"] = round(time.time() - t0, 1)
        results.append(r)
        flag = "OK " if r["status"] == "ok" else "FAIL"
        print(f"[round {rnd}] {flag} {r['secs']}s "
              f"pre_multi={r.get('pre_nextmulti')} "
              f"post_multi={r.get('post_nextmulti')} "
              f"issues={len(r['issues'])}", flush=True)
        for it in r["issues"][:8]:
            print(f"    - {it}", flush=True)

    out = workroot / "results.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    nfail = sum(1 for r in results if r["status"] != "ok")
    print(f"\n{len(results)} rounds, {nfail} failed -> {out}")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
