#!/usr/bin/env python3
"""Logical replication correctness oracle for PostgreSQL.

Three tiers, all on one embedded server (or two for tier 3). Server needs
``wal_level=logical`` — set automatically via COEVO_PG_EXTRA_OPTS before the
runner starts (fresh datadir every run, so the postmaster GUC is legal).

TIER 1 — test_decoding journal oracle
    A deterministic journaled workload (inserts/updates/pk-moves/deletes/
    truncates/savepoint rollbacks/full aborts; Random(seed)) runs against
    t1 (REPLICA IDENTITY DEFAULT), t2 (REPLICA IDENTITY FULL), t3. A logical
    slot created BEFORE the workload is drained with
    pg_logical_slot_get_changes('s1','test_decoding'). Oracle: the decoded
    change sequence (kind, table, column->value map) must equal the journaled
    committed writes in commit order; rolled-back txns contribute nothing.
    Framing check: no change line outside BEGIN..COMMIT; empty committed txns
    may legally emit bare BEGIN/COMMIT (skip-empty-xacts=0 default).

TIER 2 — pgoutput row-filter / column-list binary oracle
    Publications with WHERE filters and column lists; changes read via
    pg_logical_slot_get_binary_changes(proto_version=4) and parsed with a
    minimal binary decoder (first-byte kind; 'R' maps relid->cols; 'I'/'U'/'D'
    carry tuples of only-published columns; 'K' old-key vs 'O' old-row depends
    on replica identity; 'T' truncate). Oracle per op (documented semantics):
      INSERT  -> I iff new row matches ANY covering pub's filter (union)
      UPDATE  -> U if match->match, I if nonmatch->match, D if match->nonmatch,
                 nothing if neither; old tuple present per RI
      DELETE  -> D iff old row matched
      TRUNCATE-> T always (filters do not apply to TRUNCATE)
    Documented-error checks: column list not covering REPLICA IDENTITY FULL is
    rejected at CREATE PUBLICATION; two subscribed pubs with DIFFERENT column
    lists on the same table must ERROR at decode time (FEATURE_NOT_SUPPORTED).
    FP guards: empty committed txns are suppressed entirely (legal); compare
    only after a second drain returns empty.

TIER 3 — real subscription apply, two datadirs (stretch)
    CREATE SUBSCRIPTION over the publisher's unix socket (verified feasible).
    Oracle: after catchup, subscriber table state == publisher. Includes a
    writes-during-sync race: rows inserted on the publisher while the
    subscription's initial COPY runs must appear exactly once.

Usage:
    python scripts/pg_logical_loop.py \
        --pg-prefix $COEVO_PGBLD/pgmaster_inject \
        --tier all --trials 6 --out results/pg_logical_loop
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402

from targets.postgres_runner import PostgresRunner  # noqa: E402

EXTRA_OPTS = ("-c wal_level=logical -c max_wal_senders=8 "
              "-c max_replication_slots=8 -c max_logical_replication_workers=8 "
              "-c max_sync_workers_per_subscription=4 "
              "-c autovacuum=off")

PUB_COLS = ("k", "v", "w")   # column universe for tier-2 tables


# ------------------------------------------------------------ sql helpers
def sx(conn, sql, params=None, fetch=False):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall() if fetch else None


# ============================================================ test_decoding
_TD_TAB_RE = re.compile(r"^table\s+((?:[\w.\"]+(?:,\s*[\w.\"]+)*)):\s*"
                        r"(INSERT|UPDATE|DELETE|TRUNCATE):(.*)$", re.S)
_TD_COL_RE = re.compile(r'(\w+|"[^"]+")\[[^\]]+\]:')


def _td_values(text: str) -> dict:
    """Parse ' k[integer]:1 w[text]:'a'' -> {'k':'1','w':'a'}.

    Values: bare token, 'quoted' ('' escape), 'null', 'unchanged-toast-datum'.
    """
    out: dict = {}
    for m in _TD_COL_RE.finditer(text):
        name = m.group(1).strip('"')
        vstart = m.end()
        if text[vstart:vstart + 1] == "'":
            j = vstart + 1
            buf = []
            while j < len(text):
                ch = text[j]
                if ch == "'":
                    if text[j + 1:j + 2] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(ch)
                j += 1
            out[name] = "".join(buf)
        else:
            nxt = _TD_COL_RE.search(text, vstart)
            end = nxt.start() if nxt else len(text)
            out[name] = text[vstart:end].strip()
    return out


def _td_nullify(d: dict) -> dict:
    return {k: (None if v == "null" else v) for k, v in d.items()}


def parse_test_decoding(rows):
    """Parse get_changes rows -> (changes, frame_problems, other_lines)."""
    changes: list[dict] = []
    problems: list[str] = []
    other: list[str] = []
    in_xact = False
    for _lsn, _xid, data in rows:
        if data.startswith("BEGIN"):
            if in_xact:
                problems.append("nested BEGIN")
            in_xact = True
            continue
        if data.startswith(("COMMIT", "PREPARE TRANSACTION",
                            "COMMIT PREPARED", "ROLLBACK PREPARED")):
            if not in_xact:
                problems.append(f"txn-end outside xact: {data[:50]}")
            in_xact = False
            continue
        m = _TD_TAB_RE.match(data)
        if not m:
            other.append(data)
            continue
        if not in_xact:
            problems.append(f"change outside xact: {data[:60]}")
        tables = sorted(t.strip().split(".")[-1].strip('"')
                        for t in m.group(1).split(","))
        kind = m.group(2)
        rest = m.group(3)
        rec: dict = {"kind": kind, "tables": tables}
        if kind == "INSERT":
            rec["new"] = _td_nullify(_td_values(rest))
        elif kind == "UPDATE":
            if " old-key:" in rest:
                parts = rest.split(" new-tuple:", 1)
                rec["old"] = _td_nullify(
                    _td_values(parts[0].replace("old-key:", "", 1)))
                rec["new"] = (_td_nullify(_td_values(parts[1]))
                              if len(parts) > 1 else None)
            else:
                # non-key update on REPLICA IDENTITY DEFAULT: bare new tuple
                rec["old"] = None
                rec["new"] = _td_nullify(_td_values(rest))
        elif kind == "DELETE":
            rec["old"] = _td_nullify(_td_values(rest))
        changes.append(rec)
    if in_xact:
        problems.append("unterminated xact at end of stream")
    return changes, problems, other


def _nv(v):
    return None if v is None else str(v)


def tier1_expected_changes(journal):
    """Project committed journal writes into decoded-change records."""
    exp: list[dict] = []
    for txn in journal:
        for w in txn["writes"]:
            tbl = w["table"]
            if w["kind"] == "insert":
                exp.append({"kind": "INSERT", "tables": [tbl],
                            "new": {c: _nv(w["new"].get(c))
                                    for c in w["new"]}})
            elif w["kind"] == "update":
                key_changed = w["old"].get("k") != w["new"].get("k")
                if tbl == "t2":            # RI FULL: always full old row
                    old = {c: _nv(w["old"].get(c)) for c in w["old"]
                           if w["old"].get(c) is not None}
                elif key_changed:          # RI DEFAULT, key moved: old-key
                    old = {"k": _nv(w["old"].get("k"))}
                else:                      # RI DEFAULT, key same: no old-key
                    old = None
                exp.append({"kind": "UPDATE", "tables": [tbl],
                            "old": old,
                            "new": {c: _nv(w["new"].get(c))
                                    for c in w["new"]}})
            elif w["kind"] == "delete":
                oldcols = list(w["old"]) if tbl == "t2" else ["k"]
                exp.append({"kind": "DELETE", "tables": [tbl],
                            "old": {c: _nv(w["old"].get(c)) for c in oldcols
                                    if w["old"].get(c) is not None}})
            elif w["kind"] == "truncate":
                exp.append({"kind": "TRUNCATE",
                            "tables": sorted(w["tables"])})
    return exp


def changes_equal(got, exp) -> bool:
    if got["kind"] != exp["kind"] or got["tables"] != exp["tables"]:
        return False
    for key in ("new", "old"):
        g, e = got.get(key), exp.get(key)
        if e is None and g is None:
            continue
        if (g is None) != (e is None):
            return False
        if g is not None and {k: _nv(v) for k, v in g.items()} != e:
            return False
    return True


# ------------------------------------------------------- workload generator
TIER1_INIT = {"t1": {k: {"k": k, "v": k * 10, "w": f"init{k}"}
                    for k in range(1, 9)},
              "t2": {k: {"k": k, "v": k * 7 + 3, "w": f"i{k}"}
                     for k in range(1, 9)},
              "t3": {k: {"k": k, "v": k * 3 + 1} for k in range(1, 9)}}


def gen_tier1_trial(rng):
    """Return (txns, journal). Each txn: {"ops":[(sql,params)], "commit":b}.

    Journal lists only COMMITTED txns' write projections in commit order.
    A deliberate duplicate-key insert poisons its txn: the op errors, the
    executor rolls back, and nothing is journaled.
    """
    model = copy.deepcopy(TIER1_INIT)
    txns: list[dict] = []
    journal: list[dict] = []
    for _ in range(rng.randint(8, 14)):
        ops: list[tuple] = []
        writes: list[dict] = []
        working = copy.deepcopy(model)
        sps: list[tuple] = []   # (name, writes_len, working_snapshot)
        sp_ctr = 0
        commit = rng.random() > 0.22
        aborted = False
        for _o in range(rng.randint(1, 6)):
            pool = ["insert", "update", "update_pk", "delete", "select",
                    "truncate", "upsert"]
            if sps:
                pool += ["rollback_to", "release"]
            elif rng.random() < 0.3:
                pool.append("savepoint")
            op = rng.choice(pool)
            tbl = rng.choice(("t1", "t2")) if op != "truncate" else \
                rng.choice(("t1", "t2", "t3"))
            if op == "savepoint":
                sp_ctr += 1
                nm = f"sp{sp_ctr}"
                ops.append((f"SAVEPOINT {nm}", None))
                sps.append((nm, len(writes), copy.deepcopy(working)))
            elif op == "rollback_to":
                nm, wlen, snap = sps.pop(rng.randrange(len(sps)))
                ops.append((f"ROLLBACK TO SAVEPOINT {nm}", None))
                writes = writes[:wlen]
                working = snap
                sps = [m for m in sps if m[0] != nm]
            elif op == "release":
                nm, _, _ = sps.pop(rng.randrange(len(sps)))
                ops.append((f"RELEASE SAVEPOINT {nm}", None))
                sps = [m for m in sps if m[0] != nm]
            elif op == "select":
                ops.append((f"SELECT count(*) FROM {tbl}", None))
            elif op == "insert":
                k = rng.randint(1, 40)
                v = rng.randint(0, 999)
                w = rng.choice([None, f"s{k}", "q'q", "x" * 30])
                if tbl == "t3":
                    ops.append((f"INSERT INTO {tbl} (k,v) VALUES (%s,%s)",
                                (k, v)))
                    row = {"k": k, "v": v}
                else:
                    ops.append((f"INSERT INTO {tbl} (k,v,w) "
                                "VALUES (%s,%s,%s)", (k, v, w)))
                    row = {"k": k, "v": v, "w": w}
                if k in working[tbl]:
                    aborted = True
                    break
                working[tbl][k] = dict(row)
                writes.append({"kind": "insert", "table": tbl, "new": row})
            elif op == "update":
                keys = list(working[tbl])
                if not keys:
                    continue
                k = rng.choice(keys)
                v = rng.randint(0, 999)
                if tbl == "t3":
                    ops.append((f"UPDATE {tbl} SET v=%s WHERE k=%s", (v, k)))
                    new = {"k": k, "v": v}
                else:
                    w = rng.choice([None, f"u{k}", "z'z"])
                    ops.append((f"UPDATE {tbl} SET v=%s, w=%s WHERE k=%s",
                                (v, w, k)))
                    new = {"k": k, "v": v, "w": w}
                writes.append({"kind": "update", "table": tbl,
                               "old": dict(working[tbl][k]), "new": new})
                working[tbl][k] = new
            elif op == "update_pk":
                keys = [k for k in working[tbl]
                        if k + 100 not in working[tbl]]
                if not keys:
                    continue
                k = rng.choice(keys)
                nk = k + 100
                ops.append((f"UPDATE {tbl} SET k=%s WHERE k=%s", (nk, k)))
                new = dict(working[tbl][k]); new["k"] = nk
                writes.append({"kind": "update", "table": tbl,
                               "old": dict(working[tbl][k]), "new": new})
                del working[tbl][k]
                working[tbl][nk] = new
            elif op == "delete":
                keys = list(working[tbl])
                if not keys:
                    continue
                k = rng.choice(keys)
                ops.append((f"DELETE FROM {tbl} WHERE k=%s", (k,),))
                writes.append({"kind": "delete", "table": tbl,
                               "old": dict(working[tbl].pop(k))})
            elif op == "truncate":
                ops.append((f"TRUNCATE {tbl}", None))
                working[tbl] = {}
                writes.append({"kind": "truncate", "table": tbl,
                               "tables": [tbl]})
            elif op == "upsert":
                if tbl == "t3":
                    continue          # t3 has no w; keep upsert on t1/t2
                k = rng.randint(1, 40)
                v = rng.randint(0, 999)
                d = rng.choice([1, 2, 5])
                wv = f"up{k}"
                ops.append((f"INSERT INTO {tbl} (k,v,w) VALUES (%s,%s,%s) "
                            f"ON CONFLICT (k) DO UPDATE SET v = {tbl}.v + %s",
                            (k, v, wv, d)))
                if k in working[tbl]:
                    new = dict(working[tbl][k]); new["v"] += d
                    writes.append({"kind": "update", "table": tbl,
                                   "old": dict(working[tbl][k]),
                                   "new": new})
                    working[tbl][k] = new
                else:
                    row = {"k": k, "v": v, "w": wv}
                    working[tbl][k] = row
                    writes.append({"kind": "insert", "table": tbl,
                                   "new": row})
        if aborted:
            txns.append({"ops": ops, "commit": False})
        else:
            txns.append({"ops": ops, "commit": commit})
            if commit:
                journal.append({"writes": writes})
                model = working
    return txns, journal


def run_tier1(pg, uri, trials, seed, hits, stats):
    adm = psycopg2.connect(uri); adm.autocommit = True
    sx(adm, "DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    sx(adm, "CREATE TABLE t1 (k int primary key, v int, w text)")
    sx(adm, "CREATE TABLE t2 (k int primary key, v int, w text)")
    sx(adm, "ALTER TABLE t2 REPLICA IDENTITY FULL")
    sx(adm, "CREATE TABLE t3 (k int primary key, v int)")
    sx(adm, "SELECT pg_drop_replication_slot('s1') WHERE EXISTS "
            "(SELECT 1 FROM pg_replication_slots WHERE slot_name='s1')")
    sx(adm, "SELECT pg_create_logical_replication_slot('s1','test_decoding')")
    work = psycopg2.connect(uri)   # autocommit=False -> implicit txns

    for trial in range(trials):
        rng = random.Random(seed * 100003 + trial)
        txns, journal = gen_tier1_trial(rng)
        # reset workload tables to canonical state (journaled: truncate+ins)
        sx(adm, "TRUNCATE t1, t2, t3")
        sx(adm, "INSERT INTO t1 SELECT g, g*10, 'init'||g FROM "
                "generate_series(1,8) g")
        sx(adm, "INSERT INTO t2 SELECT g, g*7+3, 'i'||g FROM "
                "generate_series(1,8) g")
        sx(adm, "INSERT INTO t3 SELECT g, g*3+1 FROM generate_series(1,8) g")
        # execute journaled txns on the non-autocommit session
        for txn in txns:
            poisoned = False
            for sql, params in txn["ops"]:
                try:
                    work.cursor().execute(sql, params)
                except psycopg2.Error:
                    work.rollback()
                    stats["aborted_txns"] = stats.get("aborted_txns", 0) + 1
                    poisoned = True
                    break
            if not poisoned:
                if txn["commit"]:
                    work.commit()
                    stats["committed_txns"] = stats.get("committed_txns", 0) + 1
                else:
                    work.rollback()
                    stats["aborted_txns"] = stats.get("aborted_txns", 0) + 1
        # drain the slot — peek first (must equal get; does not consume)
        rows_p = sx(adm, "SELECT lsn, xid, data FROM "
                         "pg_logical_slot_peek_changes('s1', NULL, NULL)",
                    fetch=True)
        rows = sx(adm, "SELECT lsn, xid, data FROM "
                       "pg_logical_slot_get_changes('s1', NULL, NULL)",
                  fetch=True)
        if [r[2] for r in rows_p] != [r[2] for r in rows]:
            hits.append({"kind": "tier1_peek_get_mismatch",
                         "context": {"trial": trial},
                         "detail": f"peek {len(rows_p)} rows vs get "
                                   f"{len(rows)} rows"})
        stats["decoded_rows"] = stats.get("decoded_rows", 0) + len(rows)
        changes, problems, _other = parse_test_decoding(rows)
        stats["decoded_changes"] = stats.get("decoded_changes", 0) \
            + len(changes)
        # expected: reset writes + journaled committed writes
        reset = [{"kind": "TRUNCATE", "tables": ["t1", "t2", "t3"]}]
        reset += [{"kind": "INSERT", "tables": [t],
                   "new": {c: _nv(v) for c, v in
                           TIER1_INIT[t][k].items()}}
                  for t in ("t1", "t2", "t3") for k in sorted(TIER1_INIT[t])]
        expected = reset + tier1_expected_changes(journal)
        stats["expected_changes"] = stats.get("expected_changes", 0) \
            + len(expected)
        ctx = {"trial": trial, "seed": seed, "journal_txns": len(journal)}
        for p in problems:
            hits.append({"kind": "tier1_frame", "context": ctx, "detail": p})
        n = min(len(changes), len(expected))
        diff = next((i for i in range(n)
                     if not changes_equal(changes[i], expected[i])), None)
        if diff is None and len(changes) != len(expected):
            diff = n
        if diff is not None:
            hits.append({
                "kind": "tier1_divergence", "context": ctx,
                "detail": f"decoded[{len(changes)}] vs expected"
                          f"[{len(expected)}] first diff @{diff}",
                "decoded_window": changes[max(0, diff - 2):diff + 3],
                "expected_window": expected[max(0, diff - 2):diff + 3]})
        # catch-up barrier: second drain must be empty
        n2 = sx(adm, "SELECT count(*) FROM pg_logical_slot_get_changes"
                     "('s1', NULL, NULL)", fetch=True)[0][0]
        if n2 != 0:
            hits.append({"kind": "tier1_nondeterministic_drain",
                         "context": ctx,
                         "detail": f"second drain returned {n2} rows"})
    work.close(); adm.close()


# ================================================================= pgoutput
def _u16(b, i):
    return struct.unpack("!H", b[i:i + 2])[0], i + 2


def _u32(b, i):
    return struct.unpack("!I", b[i:i + 4])[0], i + 4


def _cstr(b, i):
    j = b.index(0, i)
    return b[i:j].decode("utf-8", "replace"), j + 1


def _tuple(b, i):
    """ncols u16 + per-col marker (n/u/t/b). Returns (values, i)."""
    n, i = _u16(b, i)
    vals = []
    for _ in range(n):
        m = chr(b[i]); i += 1
        if m == "n":
            vals.append(None)
        elif m == "u":
            vals.append("<unchanged-toast>")
        elif m in ("t", "b"):
            ln, i = _u32(b, i)
            raw = b[i:i + ln]; i += ln
            vals.append(raw.decode("utf-8", "replace") if m == "t"
                        else f"<bin:{raw.hex()[:24]}>")
        else:
            vals.append(f"<badmarker:{m}>")
    return vals, i


def parse_pgoutput(rows):
    """Parse get_binary_changes blobs -> (msgs, relmap)."""
    msgs: list[dict] = []
    relmap: dict[int, dict] = {}
    for _lsn, _xid, data in rows:
        d = bytes(data)
        if not d:
            continue
        kind = chr(d[0])
        i = 1
        m: dict = {"kind": kind}
        try:
            if kind == "R":
                # get_binary_changes decode passes InvalidTransactionId, so
                # logicalrep_write_rel sends NO xid — first u32 is the relid.
                relid, i = _u32(d, i)
                ns, i = _cstr(d, i)
                rel, i = _cstr(d, i)
                ri = d[i]; i += 1
                natts, i = _u16(d, i)
                cols = []
                for _a in range(natts):
                    i += 1                   # flags byte
                    nm, i = _cstr(d, i)
                    i += 8                   # typoid u32 + typmod i32
                    cols.append(nm)
                relmap[relid] = {"rel": rel, "ns": ns, "cols": cols,
                                 "ri": ri}
                m["relid"], m["rel"] = relid, rel
            elif kind in ("I", "U", "D"):
                relid, i = _u32(d, i)
                info = relmap.get(relid, {"rel": f"relid{relid}",
                                          "cols": []})
                m["relid"], m["rel"] = relid, info["rel"]
                if kind != "I" and chr(d[i]) in ("K", "O"):
                    m["oldkind"] = chr(d[i]); i += 1
                    vals, i = _tuple(d, i)
                    m["old"] = dict(zip(info["cols"], vals))
                if kind in ("I", "U"):
                    assert chr(d[i]) == "N"
                    i += 1
                    vals, i = _tuple(d, i)
                    m["new"] = dict(zip(info["cols"], vals))
            elif kind == "T":
                nrel, i = _u32(d, i)
                i += 1                        # flags
                rels = []
                for _r in range(nrel):
                    rid, i = _u32(d, i)
                    rels.append(relmap.get(rid, {}).get("rel", f"r{rid}"))
                m["rels"] = sorted(rels)
            # B/C/Y/O/M etc: no payload needed for the oracle
        except (IndexError, AssertionError, struct.error,
                ValueError) as exc:
            m["parse_error"] = f"{type(exc).__name__}: {exc} @{i}"
        msgs.append(m)
    return msgs, relmap


# ---- tier-2 scenario model -------------------------------------------------
@dataclass
class Pub:
    name: str
    table: str
    filt: object = None          # fn(rowdict)->bool
    filt_sql: str = ""
    cols: tuple | None = None    # None => all columns
    expect_create_err: bool = False


@dataclass
class Step:
    sql: str
    expect: dict | None = None   # {"kind","rel","new"/"old","oldkind","rels"}
    expect_error: bool = False


def _estep(steps, st, sql, pubs, tbl, ri_full, pubcols,
           ins=None, upd=None, dele=None, trunc=None, keycols=("k",)):
    """Apply op to model st, derive expected emission, append Step."""
    def proj(row):
        return {c: _nv(row.get(c)) for c in pubcols}

    exp = None
    if ins is not None:
        if any(p.filt is None or p.filt(ins) for p in pubs):
            exp = {"kind": "I", "rel": tbl, "new": proj(ins)}
        st[tbl][ins["k"]] = dict(ins)
    elif upd is not None:
        k, ch = upd
        old = dict(st[tbl][k])
        new = dict(old); new.update(ch)
        om = any(p.filt is None or p.filt(old) for p in pubs)
        nm = any(p.filt is None or p.filt(new) for p in pubs)
        if om and nm:
            exp = {"kind": "U", "rel": tbl, "new": proj(new)}
        elif not om and nm:
            exp = {"kind": "I", "rel": tbl, "new": proj(new)}
        elif om and not nm:
            exp = {"kind": "D", "rel": tbl}
        if exp and exp["kind"] in ("U", "D"):
            if ri_full:
                exp["oldkind"] = "O"
                exp["old"] = proj(old)
            elif exp["kind"] == "D" or new["k"] != old["k"]:
                exp["oldkind"] = "K"
                exp["old"] = {c: (_nv(old.get(c)) if c in keycols else None)
                              for c in pubcols}
        if new["k"] != old["k"]:
            del st[tbl][k]
            st[tbl][new["k"]] = new
        else:
            st[tbl][k] = new
    elif dele is not None:
        old = dict(st[tbl][dele])
        if any(p.filt is None or p.filt(old) for p in pubs):
            exp = {"kind": "D", "rel": tbl}
            if ri_full:
                exp["oldkind"] = "O"
                exp["old"] = proj(old)
            else:
                exp["oldkind"] = "K"
                exp["old"] = {c: (_nv(old.get(c)) if c in keycols else None)
                              for c in pubcols}
        del st[tbl][dele]
    elif trunc is not None:
        exp = {"kind": "T", "rels": sorted(trunc)}
        for t in trunc:
            st[t] = {}
    steps.append(Step(sql, exp))
    return exp


def tier2_scenarios():
    scen = []

    # A: non-key filter (v>15), REPLICA IDENTITY FULL
    pa = Pub("pa", "fa", filt=lambda r: r["v"] > 15, filt_sql="v > 15")
    st = {"fa": {1: {"k": 1, "v": 10, "w": "a"}}}
    steps: list[Step] = []
    _estep(steps, st, "INSERT INTO fa VALUES (2,40,'b')", [pa], "fa", True, PUB_COLS,
           ins={"k": 2, "v": 40, "w": "b"})
    _estep(steps, st, "INSERT INTO fa VALUES (3,5,'c')", [pa], "fa", True, PUB_COLS,
           ins={"k": 3, "v": 5, "w": "c"})
    _estep(steps, st, "UPDATE fa SET v=50 WHERE k=3", [pa], "fa", True, PUB_COLS,
           upd=(3, {"v": 50}))                       # nm->m : I
    _estep(steps, st, "UPDATE fa SET v=1 WHERE k=2", [pa], "fa", True, PUB_COLS,
           upd=(2, {"v": 1}))                        # m->nm : D
    _estep(steps, st, "UPDATE fa SET v=60 WHERE k=3", [pa], "fa", True, PUB_COLS,
           upd=(3, {"v": 60}))                       # m->m : U
    _estep(steps, st, "UPDATE fa SET w='zz' WHERE k=3", [pa], "fa", True, PUB_COLS,
           upd=(3, {"w": "zz"}))                     # m->m (v still 60): U
    _estep(steps, st, "UPDATE fa SET w='no' WHERE k=2", [pa], "fa", True, PUB_COLS,
           upd=(2, {"w": "no"}))                     # nm->nm : nothing
    _estep(steps, st, "DELETE FROM fa WHERE k=3", [pa], "fa", True, PUB_COLS, dele=3)
    _estep(steps, st, "DELETE FROM fa WHERE k=2", [pa], "fa", True, PUB_COLS, dele=2)
    _estep(steps, st, "TRUNCATE fa", [pa], "fa", True, PUB_COLS, trunc=["fa"])
    scen.append({"name": "rowfilter_nonkey_ri_full", "slot": "sa",
                 "setup": ["CREATE TABLE fa (k int primary key, v int, "
                           "w text)",
                           "ALTER TABLE fa REPLICA IDENTITY FULL",
                           "INSERT INTO fa VALUES (1,10,'a')"],
                 "pubs": [pa], "steps": steps})

    # B: key-col filter (k>=5), RI DEFAULT
    pb = Pub("pb", "fb", filt=lambda r: r["k"] >= 5, filt_sql="k >= 5")
    stb = {"fb": {}}
    stepsb: list[Step] = []
    _estep(stepsb, stb, "INSERT INTO fb VALUES (1,10,'x')", [pb], "fb", False, PUB_COLS,
           ins={"k": 1, "v": 10, "w": "x"})
    _estep(stepsb, stb, "INSERT INTO fb VALUES (7,70,'y')", [pb], "fb", False, PUB_COLS,
           ins={"k": 7, "v": 70, "w": "y"})
    _estep(stepsb, stb, "UPDATE fb SET k=9 WHERE k=1", [pb], "fb", False, PUB_COLS,
           upd=(1, {"k": 9}))                        # nm->m : I
    _estep(stepsb, stb, "UPDATE fb SET v=71 WHERE k=7", [pb], "fb", False, PUB_COLS,
           upd=(7, {"v": 71}))                       # m->m : U
    _estep(stepsb, stb, "DELETE FROM fb WHERE k=9", [pb], "fb", False, PUB_COLS, dele=9)
    scen.append({"name": "rowfilter_key_ri_default", "slot": "sb",
                 "setup": ["CREATE TABLE fb (k int primary key, v int, "
                           "w text)"],
                 "pubs": [pb], "steps": stepsb})

    # C: column list (k,v), RI DEFAULT — emitted tuples carry only k,v
    pc = Pub("pc", "fc", cols=("k", "v"))
    stc = {"fc": {}}
    stepsc: list[Step] = []
    _estep(stepsc, stc, "INSERT INTO fc VALUES (1,10,'hide')", [pc], "fc",
           False, ("k", "v"), ins={"k": 1, "v": 10, "w": "hide"})
    _estep(stepsc, stc, "UPDATE fc SET w='hid2' WHERE k=1", [pc], "fc",
           False, ("k", "v"), upd=(1, {"w": "hid2"}))            # U, w not emitted
    _estep(stepsc, stc, "UPDATE fc SET v=11 WHERE k=1", [pc], "fc",
           False, ("k", "v"), upd=(1, {"v": 11}))
    _estep(stepsc, stc, "DELETE FROM fc WHERE k=1", [pc], "fc",
           False, ("k", "v"), dele=1)
    scen.append({"name": "columnlist_ri_default", "slot": "sc",
                 "setup": ["CREATE TABLE fc (k int primary key, v int, "
                           "w text)"],
                 "pubs": [pc], "steps": stepsc})

    # D: column list not covering RI FULL — CREATE succeeds; the check fires
    # at DML time (execReplication.c): UPDATE/DELETE must error, INSERT ok.
    scen.append({"name": "columnlist_vs_ri_full_err", "slot": None,
                 "setup": ["CREATE TABLE fd (k int primary key, v int, "
                           "w text)",
                           "ALTER TABLE fd REPLICA IDENTITY FULL",
                           "INSERT INTO fd VALUES (1,10,'a')"],
                 "pubs": [Pub("pd", "fd", cols=("k", "v"))],
                 "steps": [Step("INSERT INTO fd VALUES (2,20,'b')"),
                           Step("UPDATE fd SET v=99 WHERE k=1",
                                expect_error=True),
                           Step("DELETE FROM fd WHERE k=1",
                                expect_error=True)]})

    # E: two filter pubs on fe — union semantics
    pe1 = Pub("pe1", "fe", filt=lambda r: r["v"] > 15, filt_sql="v > 15")
    pe2 = Pub("pe2", "fe", filt=lambda r: r["w"] is not None,
              filt_sql="w IS NOT NULL")
    ste = {"fe": {}}
    stepse: list[Step] = []
    _estep(stepse, ste, "INSERT INTO fe VALUES (1,40,NULL)", [pe1, pe2],
           "fe", True, PUB_COLS, ins={"k": 1, "v": 40, "w": None})     # e1 only
    _estep(stepse, ste, "INSERT INTO fe VALUES (2,5,'x')", [pe1, pe2],
           "fe", True, PUB_COLS, ins={"k": 2, "v": 5, "w": "x"})        # e2 only
    _estep(stepse, ste, "INSERT INTO fe VALUES (3,1,NULL)", [pe1, pe2],
           "fe", True, PUB_COLS, ins={"k": 3, "v": 1, "w": None})       # neither
    _estep(stepse, ste, "UPDATE fe SET v=1, w='x' WHERE k=1", [pe1, pe2],
           "fe", True, PUB_COLS, upd=(1, {"v": 1, "w": "x"}))           # e1->e2: U
    _estep(stepse, ste, "UPDATE fe SET w=NULL WHERE k=2", [pe1, pe2],
           "fe", True, PUB_COLS, upd=(2, {"w": None}))                  # e2->none: D
    _estep(stepse, ste, "DELETE FROM fe WHERE k=3", [pe1, pe2],
           "fe", True, PUB_COLS, dele=3)                                # none
    scen.append({"name": "rowfilter_union_two_pubs", "slot": "se",
                 "setup": ["CREATE TABLE fe (k int primary key, v int, "
                           "w text)",
                           "ALTER TABLE fe REPLICA IDENTITY FULL"],
                 "pubs": [pe1, pe2], "steps": stepse})

    # F: conflicting column lists across subscribed pubs -> decode-time error
    scen.append({"name": "columnlist_conflict_decode_err", "slot": "sf",
                 "setup": ["CREATE TABLE ff (k int primary key, v int, "
                           "w text)"],
                 "pubs": [Pub("pf1", "ff", cols=("k", "v")),
                          Pub("pf2", "ff", cols=("k",))],
                 "steps": [Step("INSERT INTO ff VALUES (1,1,'a')")],
                 "expect_decode_error": True})

    # G: REPLICA IDENTITY NOTHING + pub that publishes updates -> UPDATE must
    # error at DML time ("does not have a replica identity")
    scen.append({"name": "ri_nothing_update_err", "slot": None,
                 "setup": ["CREATE TABLE fg (k int primary key, v int)",
                           "ALTER TABLE fg REPLICA IDENTITY NOTHING",
                           "INSERT INTO fg VALUES (1,10)"],
                 "pubs": [Pub("pg", "fg")],
                 "steps": [Step("INSERT INTO fg VALUES (2,20)"),
                           Step("UPDATE fg SET v=11 WHERE k=1",
                                expect_error=True)]})

    # H: generated column + publish_generated_columns (18+/20devel)
    ph1 = Pub("ph1", "fh")      # default: gencols NOT published
    sth = {"fh": {}}
    stepsh: list[Step] = []
    _estep(stepsh, sth, "INSERT INTO fh (k,v) VALUES (1,10)", [ph1], "fh",
           False, ("k", "v"), ins={"k": 1, "v": 10})      # emitted cols: k,v only
    scen.append({"name": "gencols_default_off", "slot": "sh1",
                 "setup": ["CREATE TABLE fh (k int primary key, v int, "
                           "g int GENERATED ALWAYS AS (v*2) STORED)"],
                 "pubs": [ph1], "steps": stepsh,
                 "pubcols": ("k", "v")})

    ph2 = Pub("ph2", "fi")
    sti = {"fi": {}}
    stepsi: list[Step] = []
    _estep(stepsi, sti, "INSERT INTO fi (k,v) VALUES (1,10)", [ph2], "fi",
           False, ("k", "v", "g"), ins={"k": 1, "v": 10, "g": 20})  # emitted: k,v,g
    _estep(stepsi, sti, "UPDATE fi SET v=11 WHERE k=1", [ph2], "fi",
           False, ("k", "v", "g"), upd=(1, {"v": 11, "g": 22}))
    scen.append({"name": "gencols_published", "slot": "sh2",
                 "setup": ["CREATE TABLE fi (k int primary key, v int, "
                           "g int GENERATED ALWAYS AS (v*2) STORED)"],
                 "pubs": [ph2], "steps": stepsi,
                 "pubcols": ("k", "v", "g"),
                 "pub_gencols": True, "min_version": 18})
    return scen


_PROTO_CACHE: list[str] = ["4"]


def _get_bin_changes(conn, slot, pubnames):
    """get_binary_changes with proto 4, falling back to 3 (<=PG17)."""
    for pv in list(_PROTO_CACHE) + ["3"]:
        try:
            rows = sx(conn,
                      "SELECT lsn, xid, data FROM "
                      "pg_logical_slot_get_binary_changes(%s, NULL, NULL, "
                      "'proto_version',%s,'publication_names',%s)",
                      (slot, pv, pubnames), fetch=True)
            _PROTO_CACHE[0] = pv
            return rows
        except psycopg2.Error as exc:
            if "out of range" in str(exc):
                continue
            raise


def _msg_matches(got, exp) -> bool:
    if got["kind"] != exp["kind"]:
        return False
    if exp["kind"] == "T":
        return got.get("rels") == exp.get("rels")
    if got.get("rel") != exp.get("rel"):
        return False
    if "oldkind" in exp and got.get("oldkind") != exp["oldkind"]:
        return False
    for key in ("new", "old"):
        if key in exp:
            g = {k: _nv(v) for k, v in (got.get(key) or {}).items()}
            if g != exp[key]:
                return False
    return True


def run_tier2(pg, uri, hits, stats):
    conn = psycopg2.connect(uri); conn.autocommit = True
    vmaj = int(sx(conn, "SHOW server_version_num", fetch=True)[0][0]) // 10000
    stats["server_major"] = vmaj
    for scen in tier2_scenarios():
        if vmaj < scen.get("min_version", 0):
            stats.setdefault("tier2_skipped", []).append(scen["name"])
            continue
        name = scen["name"]
        stats["tier2_scenarios"] = stats.get("tier2_scenarios", 0) + 1
        ok = True
        for sql in scen["setup"]:
            try:
                sx(conn, sql)
            except psycopg2.Error as exc:
                hits.append({"kind": "tier2_setup_fail", "scenario": name,
                             "detail": f"{sql[:50]} -> {exc}"})
                ok = False
        if not ok:
            continue
        pubs_ok = []
        for p in scen["pubs"]:
            if p.cols is not None:
                ddl = (f"CREATE PUBLICATION {p.name} FOR TABLE {p.table} "
                       f"({', '.join(p.cols)})")
            elif p.filt_sql:
                ddl = (f"CREATE PUBLICATION {p.name} FOR TABLE {p.table} "
                       f"WHERE ({p.filt_sql})")
            else:
                ddl = f"CREATE PUBLICATION {p.name} FOR TABLE {p.table}"
            if scen.get("pub_gencols"):
                # 20devel: enum 'stored'; <=18: boolean 'true'
                ddl += " WITH (publish_generated_columns = 'stored')"
            err = None
            try:
                sx(conn, ddl)
            except psycopg2.Error as exc:
                if scen.get("pub_gencols") and "'stored'" in ddl:
                    ddl = ddl.replace("'stored'", "true")
                    try:
                        sx(conn, ddl)
                    except psycopg2.Error as exc2:
                        err = str(exc2)
                else:
                    err = str(exc)
            if err is not None:
                if p.expect_create_err:
                    stats["expected_errors"] = \
                        stats.get("expected_errors", 0) + 1
                else:
                    hits.append({"kind": "tier2_pub_create_fail",
                                 "scenario": name,
                                 "detail": f"{ddl} -> {err}"})
                continue
            if p.expect_create_err:
                hits.append({"kind": "tier2_expected_error_missing",
                             "scenario": name,
                             "detail": f"{ddl} succeeded; must fail"})
            else:
                pubs_ok.append(p)
        if scen["slot"] is None:
            # slot-less scenario (documented DML-time errors): run steps,
            # expect_error steps must raise
            for s in scen["steps"]:
                try:
                    sx(conn, s.sql)
                    if s.expect_error:
                        hits.append({"kind": "tier2_expected_error_missing",
                                     "scenario": name,
                                     "detail": f"{s.sql} succeeded; "
                                               "must error"})
                except psycopg2.Error as exc:
                    if s.expect_error:
                        stats["expected_errors"] = \
                            stats.get("expected_errors", 0) + 1
                    else:
                        hits.append({"kind": "tier2_step_fail",
                                     "scenario": name,
                                     "detail": f"{s.sql} -> {exc}"})
            continue
        slot = scen["slot"]
        try:
            sx(conn, f"SELECT pg_create_logical_replication_slot"
                     f"('{slot}','pgoutput')")
        except psycopg2.Error as exc:
            hits.append({"kind": "tier2_slot_fail", "scenario": name,
                         "detail": str(exc)})
            continue
        for s in scen["steps"]:
            try:
                sx(conn, s.sql)
                if s.expect_error:
                    hits.append({"kind": "tier2_expected_error_missing",
                                 "scenario": name,
                                 "detail": f"{s.sql} succeeded; must error"})
            except psycopg2.Error as exc:
                if s.expect_error:
                    stats["expected_errors"] = \
                        stats.get("expected_errors", 0) + 1
                else:
                    hits.append({"kind": "tier2_step_fail",
                                 "scenario": name,
                                 "detail": f"{s.sql} -> {exc}"})
        pubnames = ",".join(p.name for p in scen["pubs"])
        try:
            rows = _get_bin_changes(conn, slot, pubnames)
        except psycopg2.Error as exc:
            if scen.get("expect_decode_error"):
                stats["expected_errors"] = stats.get("expected_errors", 0) + 1
                stats.setdefault("decode_errors", []).append(
                    f"{name}: {exc}")
                continue
            hits.append({"kind": "tier2_decode_error", "scenario": name,
                         "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if scen.get("expect_decode_error"):
            hits.append({"kind": "tier2_expected_error_missing",
                         "scenario": name,
                         "detail": "decode succeeded; column-list conflict "
                                   "must error"})
            continue
        msgs, _relmap = parse_pgoutput(rows)
        for m in msgs:
            if m.get("parse_error"):
                hits.append({"kind": "tier2_parse_error", "scenario": name,
                             "detail": m["parse_error"]})
        body = [m for m in msgs if m["kind"] in ("I", "U", "D", "T")]
        expected = [s.expect for s in scen["steps"] if s.expect]
        stats["tier2_msgs"] = stats.get("tier2_msgs", 0) + len(body)
        stats["tier2_expected"] = stats.get("tier2_expected", 0) \
            + len(expected)
        n = min(len(body), len(expected))
        diff = next((i for i in range(n)
                     if not _msg_matches(body[i], expected[i])), None)
        if diff is None and len(body) != len(expected):
            diff = n
        if diff is not None:
            hits.append({
                "kind": "tier2_divergence", "scenario": name,
                "detail": f"got {len(body)} msgs vs {len(expected)} "
                          f"expected; first diff @{diff}",
                "got_window": body[max(0, diff - 2):diff + 3],
                "expected_window": expected[max(0, diff - 2):diff + 3]})
        n2 = len(_get_bin_changes(conn, slot, pubnames))
        if n2 != 0:
            hits.append({"kind": "tier2_nondeterministic_drain",
                         "scenario": name,
                         "detail": f"second drain {n2} rows"})
    conn.close()


# ================================================================ tier 3
def run_tier3(pg_prefix, outdir, hits, stats):
    os.environ["COEVO_PG_EXTRA_OPTS"] = EXTRA_OPTS
    pub = PostgresRunner(os.path.join(outdir, "dd_pub3"),
                         pg_prefix=pg_prefix)
    sub = PostgresRunner(os.path.join(outdir, "dd_sub3"),
                         pg_prefix=pg_prefix)
    try:
        cp = psycopg2.connect(pub._server.get_uri()); cp.autocommit = True
        cs = psycopg2.connect(sub._server.get_uri()); cs.autocommit = True
        sx(cp, "DROP TABLE IF EXISTS rt CASCADE")
        sx(cs, "DROP SUBSCRIPTION IF EXISTS rs")
        sx(cs, "DROP TABLE IF EXISTS rt CASCADE")
        sx(cp, "DROP PUBLICATION IF EXISTS rp")
        sx(cp, "CREATE TABLE rt (k int primary key, v int)")
        sx(cs, "CREATE TABLE rt (k int primary key, v int)")
        sx(cp, "INSERT INTO rt SELECT g, g*5 FROM generate_series(1,2000) g")
        sx(cp, "CREATE PUBLICATION rp FOR TABLE rt")
        srv = pub._server
        conninfo = f"host={srv.sockdir} port={srv.port} dbname=postgres"
        stop = threading.Event()
        writer_state = {"k": 5000, "err": None}

        def writer():
            wc = psycopg2.connect(pub._server.get_uri())
            wc.autocommit = True
            while not stop.is_set():
                try:
                    sx(wc, "INSERT INTO rt VALUES (%s,%s)",
                       (writer_state["k"], writer_state["k"]))
                    writer_state["k"] += 1
                except psycopg2.Error as exc:
                    writer_state["err"] = str(exc)
                    break
            wc.close()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        time.sleep(0.2)
        try:
            sx(cs, f"CREATE SUBSCRIPTION rs CONNECTION '{conninfo}' "
                   "PUBLICATION rp")
        except psycopg2.Error as exc:
            hits.append({"kind": "tier3_sub_create_fail",
                         "detail": str(exc)})
            return
        finally:
            time.sleep(0.3)
            stop.set()
            t.join(10)
        stats["tier3_writer_last_key"] = writer_state["k"]
        if writer_state["err"]:
            hits.append({"kind": "tier3_writer_error",
                         "detail": writer_state["err"]})
        deadline = time.monotonic() + 120
        converged = False
        n_pub = n_sub = None
        while time.monotonic() < deadline:
            n_pub = sx(cp, "SELECT count(*), sum(v) FROM rt", fetch=True)[0]
            n_sub = sx(cs, "SELECT count(*), sum(v) FROM rt", fetch=True)[0]
            if n_pub == n_sub:
                converged = True
                break
            time.sleep(0.5)
        stats["tier3_pub_count_sum"] = list(n_pub) if n_pub else None
        stats["tier3_sub_count_sum"] = list(n_sub) if n_sub else None
        if not converged:
            hits.append({"kind": "tier3_no_convergence",
                         "detail": f"pub={n_pub} sub={n_sub} after 120s"})
        else:
            rows_p = sx(cp, "SELECT k,v FROM rt ORDER BY k", fetch=True)
            rows_s = sx(cs, "SELECT k,v FROM rt ORDER BY k", fetch=True)
            if rows_p != rows_s:
                hits.append({"kind": "tier3_row_divergence",
                             "detail": f"{len(rows_p)} pub vs {len(rows_s)} "
                                       "sub rows differ"})
            stats["tier3_rows"] = len(rows_p)
            stats["tier3_converged"] = True
        cp.close(); cs.close()
        for lg, tag in ((pub, "pub"), (sub, "sub")):
            for ln in lg.log_fatal_lines(lg.log_new_lines()):
                hits.append({"kind": f"tier3_{tag}_log_fatal",
                             "detail": ln})
    finally:
        pub.cleanup(); sub.cleanup()


# ================================================================ driver
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-prefix", required=True)
    ap.add_argument("--pg-datadir", default=None)
    ap.add_argument("--tier", default="all", choices=("1", "2", "3", "all"))
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    hits: list[dict] = []
    stats: dict = {}

    if args.tier == "3":
        t0 = time.time()
        run_tier3(args.pg_prefix, args.out, hits, stats)
        stats["elapsed_s"] = round(time.time() - t0, 2)
    else:
        os.environ["COEVO_PG_EXTRA_OPTS"] = EXTRA_OPTS
        datadir = args.pg_datadir or os.path.join(args.out, "dd_logical")
        pg = PostgresRunner(datadir, pg_prefix=args.pg_prefix)
        uri = pg._server.get_uri()
        t0 = time.time()
        try:
            if args.tier in ("1", "all"):
                run_tier1(pg, uri, args.trials, args.seed, hits, stats)
            if args.tier in ("2", "all"):
                run_tier2(pg, uri, hits, stats)
            for ln in pg.log_fatal_lines(pg.log_new_lines()):
                hits.append({"kind": "server_log_fatal", "detail": ln})
        finally:
            pg.cleanup()
        stats["elapsed_s"] = round(time.time() - t0, 2)

    summary = {"tier": args.tier, "prefix": args.pg_prefix,
               "stats": stats, "n_hits": len(hits),
               "hit_kinds": sorted({h["kind"] for h in hits})}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    with open(os.path.join(args.out, "hits.json"), "w") as fh:
        json.dump(hits, fh, indent=1, default=str)
    print(json.dumps(summary, indent=1))
    return 0 if not hits else 1


if __name__ == "__main__":
    sys.exit(main())
