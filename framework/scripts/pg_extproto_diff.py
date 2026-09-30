#!/usr/bin/env python3
"""Extended query protocol differential for PostgreSQL.

Every harness entry point in this repo drives psycopg2, which mogrifies
parameters *client-side* and then ships a literal query — the server never
sees ``$n`` parameters. This script instead drives the build's own
``libpq.so`` through ctypes so we control the actual wire protocol:

- simple:   ``PQsendQuery``          (Query message)
- extended: ``PQsendQueryParams``    (Parse/Bind/Describe/Execute/Sync,
                                      real ``$n`` params, text or binary)
- prepared: ``PQsendPrepare`` + ``PQdescribePrepared`` +
            ``PQsendQueryPrepared``  (named statement, describe output)

Oracle per case: the extended-protocol result bag (and column type OIDs)
must equal the simple-protocol literal result bag (or a hand-computed
``expect_rows``). Divergence => candidate bug in parse/bind. Internal
errors on either side and server-log TRAP/PANIC are always hits.

Repetitions (``reps``) exercise the generic-plan switchover: an unnamed
or prepared statement replanned under a different bind value must still
return correct rows.

Usage:
    python scripts/pg_extproto_diff.py \
        --prefix $COEVO_PGBLD/pg186_assert \
        --out results/pg_extproto_186
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import select
import struct
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from targets.postgres_runner import PostgresRunner  # noqa: E402

LOGGER = logging.getLogger("pg_extproto_diff")

# ------------------------------------------------------------ result codes
PGRES_EMPTY_QUERY = 0
PGRES_COMMAND_OK = 1
PGRES_TUPLES_OK = 2
PGRES_FATAL_ERROR = 7
PGRES_SINGLE_TUPLE = 9

# ------------------------------------------------------------- column OIDs
BOOL, BYTEA, INT8, INT2, INT4, TEXT, OID_T = 16, 17, 20, 21, 23, 25, 26
FLOAT4, FLOAT8, VARCHAR, JSON, NUMERIC, JSONB = 700, 701, 1043, 114, 1700, 3802
INT_OIDS = {INT2, INT4, INT8, OID_T}


def dec_numeric(data: bytes) -> str:
    """Decode binary-format numeric (base-10000 groups) to its text form."""
    if len(data) < 8:
        return f"<short:{data.hex()}>"
    nd, weight, sign, dscale = struct.unpack("!hhhh", data[:8])
    if sign == 0xC000:
        return "NaN"
    if sign == 0xD000:
        return "Infinity"
    if sign == 0xF000:
        return "-Infinity"
    digits = struct.unpack(f"!{nd}h", data[8:8 + 2 * nd])
    chars = "".join(f"{d:04d}" for d in digits)
    point = 4 * (weight + 1)  # digits before the decimal point
    if point <= 0:
        ip, fp = "0", "0" * (-point) + chars
    elif point >= len(chars):
        ip, fp = chars + "0" * (point - len(chars)), ""
    else:
        ip, fp = chars[:point], chars[point:]
    ip = ip.lstrip("0") or "0"
    fp = fp[:dscale].ljust(dscale, "0") if dscale > 0 else ""
    out = ip + ("." + fp if fp else "")
    return ("-" if sign == 0x4000 else "") + out


def decode_binary_cell(oid: int, data: bytes) -> str:
    """Best-effort decode of a binary result cell into its text form."""
    try:
        if oid == BOOL:
            return "t" if data[0] else "f"
        if oid == INT2:
            return str(struct.unpack("!h", data)[0])
        if oid == INT4:
            return str(struct.unpack("!i", data)[0])
        if oid == OID_T:
            return str(struct.unpack("!I", data)[0])
        if oid == INT8:
            return str(struct.unpack("!q", data)[0])
        if oid == FLOAT4:
            return repr(struct.unpack("!f", data)[0])
        if oid == FLOAT8:
            return repr(struct.unpack("!d", data)[0])
        if oid == NUMERIC:
            return dec_numeric(data)
        if oid == BYTEA:
            return "\\x" + data.hex()
        if oid == JSONB:
            return data[1:].decode("utf-8", "replace")  # leading version byte
        if oid in (TEXT, VARCHAR, JSON):
            return data.decode("utf-8", "replace")
    except (struct.error, IndexError, UnicodeDecodeError) as exc:
        return f"<decode_err:{exc}:{data.hex()[:40]}>"
    return f"<no_decoder:{oid}:{data.hex()[:40]}>"


def canon_cell(oid: int, raw: bytes | None, binary: bool):
    """Canonical comparable value for one result cell.

    Floats round-trip through their storage width so binary f4 0.1 and
    text '0.1' compare equal; numerics compare by value not scale.
    """
    if raw is None:
        return None
    text = decode_binary_cell(oid, raw) if binary else raw.decode(
        "utf-8", "replace")
    if oid in INT_OIDS:
        try:
            return ("num", int(text))
        except ValueError:
            return ("str", text)
    if oid == NUMERIC:
        try:
            dec = Decimal(text)
            return ("num", "NaN") if dec.is_nan() else ("num", dec)
        except Exception:  # noqa: BLE001
            return ("str", text)
    if oid == FLOAT4:
        try:
            return ("num", struct.unpack(
                "!f", struct.pack("!f", float(text)))[0])
        except (ValueError, struct.error, OverflowError):
            return ("str", text)
    if oid == FLOAT8:
        try:
            return ("num", float(text))
        except ValueError:
            return ("str", text)
    return ("str", text)


# ------------------------------------------------------------- ctypes glue
class LibPQ:
    """Minimal async-capable libpq wrapper."""

    def __init__(self, libdir: Path):
        lib_path = Path(libdir) / "libpq.so"
        lib = ctypes.CDLL(str(lib_path))
        self.lib = lib
        v = ctypes.c_void_p
        i = ctypes.c_int
        cp = ctypes.c_char_p

        lib.PQconnectdb.restype = v
        lib.PQconnectdb.argtypes = [cp]
        lib.PQstatus.restype = i
        lib.PQstatus.argtypes = [v]
        lib.PQerrorMessage.restype = cp
        lib.PQerrorMessage.argtypes = [v]
        lib.PQfinish.argtypes = [v]
        lib.PQsocket.restype = i
        lib.PQsocket.argtypes = [v]
        lib.PQflush.restype = i
        lib.PQflush.argtypes = [v]
        lib.PQisBusy.restype = i
        lib.PQisBusy.argtypes = [v]
        lib.PQconsumeInput.restype = i
        lib.PQconsumeInput.argtypes = [v]
        lib.PQgetResult.restype = v
        lib.PQgetResult.argtypes = [v]
        lib.PQsendQuery.restype = i
        lib.PQsendQuery.argtypes = [v, cp]
        lib.PQsendQueryParams.restype = i
        lib.PQsendQueryParams.argtypes = [
            v, cp, i, ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(cp), ctypes.POINTER(i), ctypes.POINTER(i), i]
        lib.PQsendPrepare.restype = i
        lib.PQsendPrepare.argtypes = [v, cp, cp, i,
                                    ctypes.POINTER(ctypes.c_uint)]
        lib.PQsendQueryPrepared.restype = i
        lib.PQsendQueryPrepared.argtypes = [
            v, cp, i, ctypes.POINTER(cp), ctypes.POINTER(i),
            ctypes.POINTER(i), i]
        lib.PQsendDescribePrepared.restype = i
        lib.PQsendDescribePrepared.argtypes = [v, cp]
        lib.PQresultStatus.restype = i
        lib.PQresultStatus.argtypes = [v]
        lib.PQresStatus.restype = cp
        lib.PQresStatus.argtypes = [i]
        lib.PQresultErrorMessage.restype = cp
        lib.PQresultErrorMessage.argtypes = [v]
        lib.PQresultErrorField.restype = cp
        lib.PQresultErrorField.argtypes = [v, i]
        lib.PQntuples.restype = i
        lib.PQntuples.argtypes = [v]
        lib.PQnfields.restype = i
        lib.PQnfields.argtypes = [v]
        lib.PQfname.restype = cp
        lib.PQfname.argtypes = [v, i]
        lib.PQftype.restype = ctypes.c_uint
        lib.PQftype.argtypes = [v, i]
        lib.PQnparams.restype = i
        lib.PQnparams.argtypes = [v]
        lib.PQparamtype.restype = ctypes.c_uint
        lib.PQparamtype.argtypes = [v, i]
        lib.PQgetvalue.restype = v
        lib.PQgetvalue.argtypes = [v, i, i]
        lib.PQgetlength.restype = i
        lib.PQgetlength.argtypes = [v, i, i]
        lib.PQgetisnull.restype = i
        lib.PQgetisnull.argtypes = [v, i, i]
        lib.PQbinaryTuples.restype = i
        lib.PQbinaryTuples.argtypes = [v]
        lib.PQclear.argtypes = [v]
        lib.PQgetCancel.restype = v
        lib.PQgetCancel.argtypes = [v]
        lib.PQcancel.restype = i
        lib.PQcancel.argtypes = [v, cp, i]
        lib.PQfreeCancel.argtypes = [v]

    def connect(self, conninfo: str):
        conn = self.lib.PQconnectdb(conninfo.encode())
        if not conn or self.lib.PQstatus(conn) != 0:
            msg = self.err_msg(conn) if conn else "alloc fail"
            if conn:
                self.lib.PQfinish(conn)
            raise RuntimeError(f"PQconnectdb failed: {msg}")
        return conn

    def err_msg(self, conn) -> str:
        raw = self.lib.PQerrorMessage(conn)
        return (raw or b"").decode("utf-8", "replace").strip()

    # ---- async drain with wall-clock timeout + server cancel ----
    def _wait(self, conn, for_write: bool, deadline: float) -> bool:
        sock = self.lib.PQsocket(conn)
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0 or sock < 0:
                return False
            rd = [] if for_write else [sock]
            wr = [sock] if for_write else []
            try:
                r, w, x = select.select(rd, wr, [sock], min(rem, 0.2))
            except (OSError, ValueError):
                return False
            if r or w or x:
                return True

    def _cancel(self, conn) -> None:
        cancel = self.lib.PQgetCancel(conn)
        if cancel:
            buf = ctypes.create_string_buffer(256)
            self.lib.PQcancel(cancel, buf, 256)
            self.lib.PQfreeCancel(cancel)

    def drain(self, conn, timeout_s: float):
        """Collect all pending PGresults; cancel+flag on timeout."""
        out: list[int] = []
        deadline = time.monotonic() + timeout_s
        timed_out = False
        while True:
            flush = self.lib.PQflush(conn)
            if flush == -1:
                break
            if flush == 1:
                if not self._wait(conn, True, deadline):
                    self._cancel(conn)
                    timed_out = True
                    deadline = time.monotonic() + 3.0
                continue
            if self.lib.PQisBusy(conn):
                if not self._wait(conn, False, deadline):
                    if not timed_out:
                        self._cancel(conn)
                        timed_out = True
                        deadline = time.monotonic() + 3.0
                        continue
                    break
                if self.lib.PQconsumeInput(conn) == 0:
                    break
                continue
            res = self.lib.PQgetResult(conn)
            if not res:
                break
            out.append(res)
        return out, timed_out

    # ------------------------------------------------ result -> record
    def result_record(self, res) -> dict:
        lib = self.lib
        status = lib.PQresultStatus(res)
        status_name = (lib.PQresStatus(status) or b"").decode()
        rec: dict = {"status": status_name}
        err = lib.PQresultErrorMessage(res)
        rec["error"] = (
            (err or b"").decode("utf-8", "replace").strip() or None)
        sqlstate = lib.PQresultErrorField(res, ord("C"))
        rec["sqlstate"] = (
            sqlstate.decode() if sqlstate else None)
        np = lib.PQnparams(res)
        if np:
            rec["param_oids"] = [lib.PQparamtype(res, i) for i in range(np)]
        if status in (PGRES_TUPLES_OK, PGRES_SINGLE_TUPLE) or (
                status == PGRES_COMMAND_OK and lib.PQnfields(res)):
            nf = lib.PQnfields(res)
            binary = bool(lib.PQbinaryTuples(res))
            cols = [(lib.PQfname(res, i) or b"").decode("utf-8", "replace")
                    for i in range(nf)]
            types = [lib.PQftype(res, i) for i in range(nf)]
            rows = []
            for t in range(lib.PQntuples(res)):
                row = []
                for f in range(nf):
                    if lib.PQgetisnull(res, t, f):
                        row.append(None)
                        continue
                    ln = lib.PQgetlength(res, t, f)
                    ptr = lib.PQgetvalue(res, t, f)
                    row.append(ctypes.string_at(ptr, ln))
                rows.append(row)
            rec.update(columns=cols, coltypes=types,
                       rows=rows, binary=binary)
        lib.PQclear(res)
        return rec


# ------------------------------------------------------------- case model
@dataclass
class P:
    """One bind parameter: value, explicit OID (0=infer), wire format."""
    v: bytes | str | None
    oid: int = 0
    binary: bool = False

    def enc(self) -> bytes | None:
        if self.v is None:
            return None
        if isinstance(self.v, bytes):
            return self.v
        return self.v.encode("utf-8")


@dataclass
class Case:
    name: str
    simple: str                # literal SQL, simple protocol (oracle)
    ext: str                   # $n SQL, extended protocol
    params: tuple = ()
    setup: tuple = ()
    pre: tuple = ()            # session SETs applied before ext runs
    post: tuple = ()           # RESETs applied after
    reps: int = 1              # ext executions (generic-plan switchover)
    prepare: bool = False      # named prepare + describe + exec
    result_binary: bool = False
    expect_rows: tuple | None = None     # absolute oracle (rows of cells)
    expect_error: bool = False           # an error IS the correct outcome
    allow_diff: bool = False             # legit divergence (record only)
    note: str = ""


def i2(v): return P(struct.pack("!h", v), INT2, True)
def i4(v): return P(struct.pack("!i", v), INT4, True)
def i8(v): return P(struct.pack("!q", v), INT8, True)
def f8(v): return P(struct.pack("!d", v), FLOAT8, True)
def f4(v): return P(struct.pack("!f", v), FLOAT4, True)
def bol(v): return P(b"\x01" if v else b"\x00", BOOL, True)
def raw(v, oid=0): return P(v, oid, True)      # binary bytes, generic oid
def txt(v, oid=0): return P(v, oid, False)     # text value


CASES: list[Case] = [
    # ------------------------------------------- type inference edges
    Case("infer_unknown_text", "SELECT '5'", "SELECT $1", (txt("5"),)),
    Case("infer_int_cast", "SELECT 5::int4 + 1", "SELECT $1::int4 + 1",
         (txt("5"),), expect_rows=(("6",),)),
    Case("infer_numeric_scale", "SELECT 3.50::numeric",
         "SELECT $1::numeric", (txt("3.50"),)),
    Case("infer_ambiguous_add", "SELECT 1 + 2", "SELECT $1 + $2",
         (txt("1"), txt("2")), expect_error=True,
         note="unknown+unknown is unresolvable"),
    Case("explicit_oid_int", "SELECT 5", "SELECT $1", (txt("5", INT4),)),
    Case("explicit_oid_text", "SELECT '5'::text", "SELECT $1",
         (txt("5", TEXT),)),
    Case("explicit_oid_wrong", "SELECT 'abc'::int4", "SELECT $1",
         (txt("abc", INT4),), expect_error=True,
         note="int4recv on 'abc' errors like the literal cast"),
    Case("infer_greatest_type", "SELECT greatest(1,5,3)",
         "SELECT greatest($1,$2,$3)", (txt("1"), txt("5"), txt("3")),
         allow_diff=True,
         note="unknown params infer text: coltype 25 vs 23 expected"),
    Case("infer_concat_op", "SELECT 'a' || 'b'", "SELECT $1 || $2",
         (txt("a"), txt("b")), allow_diff=True,
         note="'a'||'b' unknown-unknown literal vs text params"),
    # ------------------------------------------------------- NULL params
    Case("null_param_isnull", "SELECT NULL IS NULL", "SELECT $1 IS NULL",
         (P(None),), expect_error=True,
         note="untyped NULL param cannot be inferred"),
    Case("null_typed_coalesce", "SELECT coalesce(NULL::int, 7)",
         "SELECT coalesce($1::int, $2::int)", (P(None), txt("7"))),
    Case("null_concat", "SELECT 'a'::text || NULL::text",
         "SELECT $1::text || $2::text", (txt("a"), P(None))),
    Case("null_untyped_coalesce", "SELECT coalesce(NULL, 'x')",
         "SELECT coalesce($1, $2)", (P(None), txt("x")), allow_diff=True),
    Case("null_in_case", "SELECT CASE WHEN NULL::bool THEN 1 ELSE 2 END",
         "SELECT CASE WHEN $1::bool THEN 1 ELSE 2 END", (P(None),)),
    # --------------------------------------------- unnamed statement use
    Case("two_params_add", "SELECT 1 + 2", "SELECT $1::int + $2::int",
         (txt("1"), txt("2"))),
    Case("param_used_twice", "SELECT 5::int + 5::float8",
         "SELECT $1::int + $1::float8", (txt("5"),)),
    Case("high_param_index", "SELECT 1::int + 10::int",
         "SELECT $1::int + $10::int",
         tuple(txt(str(i)) for i in range(1, 11)), expect_error=True,
         note="unused $2..$9 must still type-resolve -> error"),
    Case("sixteen_params",
         "SELECT 1+2+3+4+5+6+7+8+9+10+11+12+13+14+15+16",
         "SELECT " + "+".join(f"${i}::int" for i in range(1, 17)),
         tuple(txt(str(i)) for i in range(1, 17))),
    # ------------------------------------------------- domain/enum casts
    Case("domain_param", "SELECT 5::posint", "SELECT $1::posint",
         (txt("5"),),
         setup=("CREATE DOMAIN posint AS int CHECK (VALUE > 0)",)),
    Case("domain_param_violate", "SELECT '-3'::posint",
         "SELECT $1::posint", (txt("-3"),), expect_error=True,
         setup=("CREATE DOMAIN posint AS int CHECK (VALUE > 0)",)),
    Case("enum_param", "SELECT 'ok'::mood", "SELECT $1::mood",
         (txt("ok"),),
         setup=("CREATE TYPE mood AS ENUM ('sad','ok','happy')",)),
    # ------------------------------------------------------ arrays/rows
    Case("array_param", "SELECT '{1,2,3}'::int[]", "SELECT $1::int[]",
         (txt("{1,2,3}"),)),
    Case("array_unnest", "SELECT * FROM unnest(ARRAY[1,2])",
         "SELECT * FROM unnest($1::int[])", (txt("{1,2}"),)),
    Case("array_agg_param", "SELECT array_agg(v) FROM (VALUES(1),(2)) v",
         "SELECT array_agg(v) FROM (VALUES($1::int),($2::int)) v",
         (txt("1"), txt("2"))),
    Case("record_param", "SELECT ROW(1,'x')",
         "SELECT ROW($1::int, $2::text)", (txt("1"), txt("x"))),
    Case("array_slice_param", "SELECT (ARRAY[1,2,3,4])[2:3]",
         "SELECT (ARRAY[1,2,3,4])[$1:$2]", (txt("2"), txt("3")),
         allow_diff=True, note="slice bounds from params must normalize"),
    Case("any_array_param", "SELECT 2 = ANY(ARRAY[1,2,3])",
         "SELECT $1::int = ANY($2::int[])", (txt("2"), txt("{1,2,3}"))),
    # ------------------------------------------------- temporal params
    Case("interval_param", "SELECT '1 day 2 hours'::interval",
         "SELECT $1::interval", (txt("1 day 2 hours"),)),
    Case("interval_mult", "SELECT '1.5 hours'::interval * 2",
         "SELECT $1::interval * 2", (txt("1.5 hours"),)),
    Case("date_leap_param", "SELECT '2024-02-29'::date + 1",
         "SELECT $1::date + $2::int", (txt("2024-02-29"), txt("1"))),
    Case("ts_infinity", "SELECT 'infinity'::timestamp",
         "SELECT $1::timestamp", (txt("infinity"),)),
    Case("ts_neg_infinity", "SELECT '-infinity'::timestamp",
         "SELECT $1::timestamp", (txt("-infinity"),)),
    Case("ts_epoch", "SELECT 'epoch'::timestamp",
         "SELECT $1::timestamp", (txt("epoch"),)),
    Case("date_bin_param",
         "SELECT date_bin('15 min', TIMESTAMP '2020-01-01 00:14', "
         "TIMESTAMP '2001-01-01')",
         "SELECT date_bin($1::interval, $2::timestamp, $3::timestamp)",
         (txt("15 min"), txt("2020-01-01 00:14"), txt("2001-01-01"))),
    Case("timetz_param", "SELECT '12:00+05'::timetz",
         "SELECT $1::timetz", (txt("12:00+05"),)),
    # ------------------------------------------------------- json/jsonb
    Case("jsonb_arrow", "SELECT '{\"a\":1}'::jsonb -> 'a'",
         "SELECT $1::jsonb -> 'a'", (txt('{"a":1}'),)),
    Case("jsonb_path_param",
         "SELECT jsonb_path_query('{\"a\":[1,2]}', '$.a[*]')",
         "SELECT jsonb_path_query($1::jsonb, $2)",
         (txt('{"a":[1,2]}'), txt("$.a[*]"))),
    Case("jsonb_path_badpath", "SELECT jsonb_path_query('{\"a\":1}', '$.a[')",
         "SELECT jsonb_path_query($1::jsonb, $2)",
         (txt('{"a":1}'), txt("$.a[")), expect_error=True),
    Case("json_param", "SELECT '{\"a\": [1,2]}'::json",
         "SELECT $1::json", (txt('{"a": [1,2]}'),)),
    # ------------------------------------------------------ unicode/text
    Case("unicode_param", "SELECT 'héllo 🎉'", "SELECT $1",
         (txt("héllo 🎉"),)),
    Case("emoji_eq", "SELECT '🎉' = '🎉'", "SELECT $1 = $2",
         (txt("🎉"), txt("🎉")), allow_diff=True,
         note="unknown-unknown '=' resolves to text equality"),
    Case("like_params", "SELECT 'abc' LIKE 'a%'", "SELECT $1 LIKE $2",
         (txt("abc"), txt("a%"))),
    Case("collate_params", "SELECT 'a' COLLATE \"C\" < 'B' COLLATE \"C\"",
         "SELECT $1 COLLATE \"C\" < $2 COLLATE \"C\"", (txt("a"), txt("B"))),
    Case("position_param", "SELECT position('b' in 'abc')",
         "SELECT position($1 in $2)", (txt("b"), txt("abc"))),
    Case("format_param", "SELECT format('%s-%I', 'x', 'tbl')",
         "SELECT format($1, $2, $3)", (txt("%s-%I"), txt("x"), txt("tbl")),
         expect_error=True,
         note="format() variadic-any params cannot be inferred"),
    Case("long_param", "SELECT length('x' || repeat('y',9999))",
         "SELECT length($1)", (txt("x" + "y" * 9999),)),
    Case("empty_param", "SELECT ''::text", "SELECT $1",
         (txt(""),), allow_diff=True),
    Case("nul_byte_param", "SELECT 'a'::text",
         "SELECT $1", (P(b"a\x00b"),), allow_diff=True,
         note="length-delimited bind smuggles NUL into text datum"),
    # ------------------------------------------------------- numerics
    Case("numeric_precision_param",
         "SELECT 1.00000000000000000001::numeric + 0.001::numeric",
         "SELECT $1::numeric + $2::numeric",
         (txt("1.00000000000000000001"), txt("0.001"))),
    Case("numeric_div_param", "SELECT 1::numeric / 3",
         "SELECT $1::numeric / $2::numeric", (txt("1"), txt("3"))),
    Case("int_overflow_param", "SELECT '9999999999'::int4",
         "SELECT $1::int4", (txt("9999999999"),), expect_error=True),
    Case("int8_min_param", "SELECT '-9223372036854775808'::int8",
         "SELECT $1::int8", (txt("-9223372036854775808"),)),
    Case("float_param", "SELECT 3.5::float8 * 2", "SELECT $1::float8 * 2",
         (txt("3.5"),)),
    Case("numeric_nan_param", "SELECT 'NaN'::numeric",
         "SELECT $1::numeric", (txt("NaN"),)),
    # -------------------------------------------------------- bytea/bit
    Case("bytea_param", "SELECT '\\xdeadbeef'::bytea",
         "SELECT $1::bytea", (txt("\\xdeadbeef"),)),
    Case("bit_param", "SELECT B'1010' & B'1100'", "SELECT $1::bit(4) & $2::bit(4)",
         (txt("1010"), txt("1100"))),
    # ------------------------------------------------- misc scalar types
    Case("uuid_param",
         "SELECT 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid",
         "SELECT $1::uuid", (txt("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),)),
    Case("inet_param", "SELECT '10.0.0.1/8'::inet", "SELECT $1::inet",
         (txt("10.0.0.1/8"),)),
    Case("range_param", "SELECT '[1,5)'::int4range",
         "SELECT $1::int4range", (txt("[1,5)"),)),
    Case("multirange_param", "SELECT '{[1,3],[5,9]}'::int4multirange",
         "SELECT $1::int4multirange", (txt("{[1,3],[5,9]}"),)),
    Case("bool_param_t", "SELECT true", "SELECT $1::bool", (txt("true"),)),
    Case("bool_case_param", "SELECT CASE WHEN true THEN 'y' ELSE 'n' END",
         "SELECT CASE WHEN $1::bool THEN 'y' ELSE 'n' END", (txt("yes"),)),
    Case("money_param", "SELECT '12.50'::money", "SELECT $1::money",
         (txt("12.50"),), allow_diff=True, note="locale-dependent output"),
    Case("pg_lsn_param", "SELECT '0/1A0'::pg_lsn", "SELECT $1::pg_lsn",
         (txt("0/1A0"),)),
    Case("macaddr_param", "SELECT '08:00:2b:01:02:03'::macaddr",
         "SELECT $1::macaddr", (txt("08:00:2b:01:02:03"),)),
    # ------------------------------------------- params in odd positions
    Case("param_in_subquery", "SELECT (SELECT 7)",
         "SELECT (SELECT $1::int)", (txt("7"),)),
    Case("param_in_cte", "WITH w AS (SELECT 3::int v) SELECT * FROM w",
         "WITH w AS (SELECT $1::int v) SELECT * FROM w", (txt("3"),)),
    Case("param_in_agg_filter",
         "SELECT sum(x) FILTER (WHERE x > 2) FROM (VALUES(1),(3)) v(x)",
         "SELECT sum(x) FILTER (WHERE x > $1) FROM (VALUES(1),(3)) v(x)",
         (txt("2"),)),
    Case("param_in_between", "SELECT 5 BETWEEN 1 AND 10",
         "SELECT $1::int BETWEEN $2::int AND $3::int",
         (txt("5"), txt("1"), txt("10"))),
    Case("param_in_inlist", "SELECT 3 IN (1,2,3)",
         "SELECT $1::int IN ($2::int,$3::int,$4::int)",
         (txt("3"), txt("1"), txt("2"), txt("3"))),
    Case("param_limit", "SELECT x FROM (VALUES(1),(2),(3)) v(x) "
         "ORDER BY x LIMIT 2",
         "SELECT x FROM (VALUES(1),(2),(3)) v(x) ORDER BY x LIMIT $1",
         (txt("2"),)),
    Case("param_offset_limit", "SELECT x FROM (VALUES(1),(2),(3)) v(x) "
         "ORDER BY x OFFSET 1 LIMIT 1",
         "SELECT x FROM (VALUES(1),(2),(3)) v(x) ORDER BY x "
         "OFFSET $1 LIMIT $2", (txt("1"), txt("1"))),
    Case("param_in_window_order",
         "SELECT x, row_number() OVER (ORDER BY x) FROM "
         "(VALUES(3),(1)) v(x) ORDER BY 2",
         "SELECT x, row_number() OVER (ORDER BY x) FROM "
         "(VALUES($1),(1)) v(x) ORDER BY 2", (txt("3"),)),
    Case("param_distinct_on", "SELECT DISTINCT ON (k) k, x FROM "
         "(VALUES(1,'a'),(1,'b')) v(k,x) ORDER BY k, x",
         "SELECT DISTINCT ON (k) k, x FROM "
         "(VALUES(1,$1),(1,'b')) v(k,x) ORDER BY k, x", (txt("a"),)),
    Case("param_case_else", "SELECT CASE 2 WHEN 1 THEN 'a' ELSE 'b' END",
         "SELECT CASE $1::int WHEN 1 THEN 'a' ELSE 'b' END", (txt("2"),)),
    Case("param_exists", "SELECT EXISTS(SELECT 1)",
         "SELECT EXISTS(SELECT $1::int)", (txt("1"),)),
    Case("param_order_by_num", "SELECT 1 AS a ORDER BY 1",
         "SELECT $1::int AS a ORDER BY 1", (txt("1"),)),
    Case("param_group_by", "SELECT v, count(*) FROM (VALUES(1),(1)) t(v) "
         "GROUP BY v ORDER BY v",
         "SELECT v, count(*) FROM (VALUES($1::int),($2::int)) t(v) "
         "GROUP BY v ORDER BY v", (txt("1"), txt("1"))),
    # --------------------------------- plan-switchover on real table data
    Case("plan_switch_indexed",
         "SELECT a, b FROM t WHERE a = 5 ORDER BY b",
         "SELECT a, b FROM t WHERE a = $1 ORDER BY b",
         (txt("5"),),
         setup=(
             "CREATE TABLE t(a int, b text)",
             "INSERT INTO t SELECT i, 'v'||i FROM generate_series(1,2000) i",
             "INSERT INTO t VALUES (5,'x1'),(5,'x2'),(5,'x3')",
             "CREATE INDEX ti ON t(a)", "ANALYZE t"),
         reps=6, note="repeat bind of selective value"),
    Case("plan_switch_nonhit",
         "SELECT a FROM t WHERE a = 50000",
         "SELECT a FROM t WHERE a = $1",
         (txt("50000"),),
         setup=(
             "CREATE TABLE t(a int)",
             "INSERT INTO t SELECT i FROM generate_series(1,2000) i",
             "CREATE INDEX ti ON t(a)", "ANALYZE t"),
         reps=6, note="bind value outside data range"),
    Case("plan_switch_generic_forced",
         "SELECT a, b FROM t WHERE a = 5 ORDER BY b",
         "SELECT a, b FROM t WHERE a = $1 ORDER BY b",
         (txt("5"),),
         setup=(
             "CREATE TABLE t(a int, b text)",
             "INSERT INTO t SELECT i, 'v'||i FROM generate_series(1,2000) i",
             "INSERT INTO t VALUES (5,'x1'),(5,'x2'),(5,'x3')",
             "CREATE INDEX ti ON t(a)", "ANALYZE t"),
         pre=("SET plan_cache_mode = force_generic_plan",),
         post=("RESET plan_cache_mode",),
         reps=3, note="generic plan must still see bind value 5"),
    Case("plan_switch_custom_forced",
         "SELECT a, b FROM t WHERE a = 5 ORDER BY b",
         "SELECT a, b FROM t WHERE a = $1 ORDER BY b",
         (txt("5"),),
         setup=(
             "CREATE TABLE t(a int, b text)",
             "INSERT INTO t SELECT i, 'v'||i FROM generate_series(1,2000) i",
             "INSERT INTO t VALUES (5,'x1'),(5,'x2'),(5,'x3')",
             "CREATE INDEX ti ON t(a)", "ANALYZE t"),
         pre=("SET plan_cache_mode = force_custom_plan",),
         post=("RESET plan_cache_mode",),
         reps=3),
    Case("plan_switch_mixed_vals",
         "SELECT a FROM t WHERE a = 7 ORDER BY a",
         "SELECT a FROM t WHERE a = $1 ORDER BY a",
         (txt("7"),),
         setup=(
             "CREATE TABLE t(a int)",
             "INSERT INTO t SELECT i FROM generate_series(1,2000) i",
             "CREATE INDEX ti ON t(a)", "ANALYZE t"),
         reps=8),
    Case("agg_param_qual", "SELECT count(*) FROM t WHERE a > 1900",
         "SELECT count(*) FROM t WHERE a > $1", (txt("1900"),),
         setup=(
             "CREATE TABLE t(a int)",
             "INSERT INTO t SELECT i FROM generate_series(1,2000) i")),
    # ----------------------------------------- protocol edge / error path
    Case("ext_empty_query", "SELECT 1", "",
         (), expect_error=True, allow_diff=True,
         note="EMPTY_QUERY via extended protocol"),
    Case("ext_multi_stmt", "SELECT 1; SELECT 2",
         "SELECT 1; SELECT 2", (), expect_error=True, allow_diff=True,
         note="extended forbids multi commands"),
    Case("ext_missing_param", "SELECT 1", "SELECT $1",
         (), expect_error=True, allow_diff=True,
         note="no $1 supplied"),
    Case("ext_extra_param", "SELECT 'x'", "SELECT $1",
         (txt("x"), txt("y")), expect_error=True, allow_diff=True,
         note="2 binds for 1 param"),
    Case("ext_binary_short_len", "SELECT 5", "SELECT $1",
         (P(b"\x00\x01", INT4, True),), expect_error=True,
         note="2-byte int4 -> insufficient data error path"),
    Case("ext_utility_stmt", "SELECT 1", "SET work_mem = '4MB'",
         (), allow_diff=True, post=("RESET work_mem",),
         note="utility through extended protocol"),
    Case("ext_declare_cursor", "SELECT 1",
         "DECLARE c CURSOR FOR SELECT $1::int", (txt("1"),),
         allow_diff=True, post=("CLOSE c",),
         note="utility + param: record actual outcome"),
    # ------------------------------------------------- binary wire params
    Case("bin_int4_param", "SELECT 41 + 1", "SELECT $1 + 1",
         (i4(41),)),
    Case("bin_int8_param", "SELECT 1000000000000::int8 + 1",
         "SELECT $1 + 1", (i8(1000000000000),)),
    Case("bin_float8_param", "SELECT 3.25::float8 * 2",
         "SELECT $1::float8 * 2", (f8(3.25),)),
    Case("bin_float4_param", "SELECT 1.5::float4 * 2",
         "SELECT $1::float4 * 2", (f4(1.5),)),
    Case("bin_bool_param", "SELECT true AND true",
         "SELECT $1::bool AND true", (bol(True),)),
    Case("bin_bytea_param", "SELECT '\\x00ff'::bytea",
         "SELECT $1::bytea", (raw(b"\x00\xff", BYTEA),)),
    Case("bin_int4_neg", "SELECT (-2147483648)::int4", "SELECT $1",
         (i4(-2147483648),)),
    Case("bin_text_param", "SELECT 'txt'::text || '!'", "SELECT $1 || '!'",
         (raw(b"txt", TEXT),), allow_diff=True),
    Case("bin_vs_text_same", "SELECT 42::int4", "SELECT $1",
         (i4(42),)),
    Case("bin_int2_param", "SELECT 300::int2 + 1", "SELECT $1 + 1",
         (i2(300),)),
    # ------------------------------------------------- binary results
    Case("res_binary_scalars", "SELECT 42::int4",
         "SELECT $1::int4", (txt("42"),), result_binary=True),
    Case("res_binary_mixed",
         "SELECT 1::int2, 300::int4, 70000::int8, true, 2.5::float8, 'x'",
         "SELECT $1::int2, $2::int4, $3::int8, $4::bool, $5::float8, $6::text",
         (txt("1"), txt("300"), txt("70000"), txt("true"), txt("2.5"),
          txt("x")),
         result_binary=True),
    Case("res_binary_numeric", "SELECT 3.14::numeric",
         "SELECT $1::numeric", (txt("3.14"),), result_binary=True),
    Case("res_binary_numeric_neg", "SELECT -1234.5678::numeric",
         "SELECT $1::numeric", (txt("-1234.5678"),), result_binary=True),
    Case("res_binary_jsonb", "SELECT '{\"a\":1}'::jsonb",
         "SELECT $1::jsonb", (txt('{"a":1}'),), result_binary=True),
    Case("res_binary_null_mix", "SELECT 1::int4, NULL::int4, 3::int4",
         "SELECT $1::int4, $2::int4, $3::int4",
         (txt("1"), P(None, INT4), txt("3")), result_binary=True),
    Case("res_binary_float4", "SELECT 0.1::float4",
         "SELECT $1::float4", (txt("0.1"),), result_binary=True),
    # -------------------------------------------- named prepare/describe
    Case("prep_add", "SELECT 5::int4 + 1", "SELECT $1::int4 + 1",
         (txt("5"),), prepare=True, reps=3),
    Case("prep_infer_type", "SELECT 5 + 1", "SELECT $1 + 1",
         (txt("5"),), prepare=True,
         note="describe should show inferred int4"),
    Case("prep_reuse_table",
         "SELECT a FROM t WHERE a = 7 ORDER BY a",
         "SELECT a FROM t WHERE a = $1 ORDER BY a",
         (txt("7"),),
         setup=(
             "CREATE TABLE t(a int)",
             "INSERT INTO t SELECT i FROM generate_series(1,2000) i",
             "CREATE INDEX ti ON t(a)", "ANALYZE t"),
         prepare=True, reps=8,
         note="named stmt replanned across 8 binds"),
    Case("prep_no_params", "SELECT 1", "SELECT 1", (), prepare=True),
    Case("prep_describe_only", "SELECT 'a'::text", "SELECT $1::text",
         (txt("a"),), prepare=True, reps=2),
    Case("prep_null_bind", "SELECT NULL::int IS NULL",
         "SELECT $1::int IS NULL", (P(None),), prepare=True),
    # ------------------------------------ wider type-I/O through binds
    Case("tsvector_param", "SELECT 'a b c'::tsvector",
         "SELECT $1::tsvector", (txt("a b c"),)),
    Case("tsquery_param", "SELECT 'a&b'::tsquery", "SELECT $1::tsquery",
         (txt("a&b"),)),
    Case("tsquery_prefix", "SELECT 'a:*'::tsquery", "SELECT $1::tsquery",
         (txt("a:*"),)),
    Case("ts_match_params", "SELECT 'a b'::tsvector @@ 'a'::tsquery",
         "SELECT $1::tsvector @@ $2::tsquery", (txt("a b"), txt("a"))),
    Case("jsonpath_param", "SELECT '$.a[*]'::jsonpath",
         "SELECT $1::jsonpath", (txt("$.a[*]"),)),
    Case("regtype_param", "SELECT 'int4'::regtype", "SELECT $1::regtype",
         (txt("int4"),)),
    Case("regclass_param", "SELECT 'pg_class'::regclass",
         "SELECT $1::regclass", (txt("pg_class"),)),
    Case("regclass_bad_param", "SELECT 'nosuch_tbl'::regclass",
         "SELECT $1::regclass", (txt("nosuch_tbl"),), expect_error=True),
    Case("regproc_param", "SELECT 'abs'::regproc", "SELECT $1::regproc",
         (txt("abs"),)),
    Case("regoper_param", "SELECT '+(int4,int4)'::regoper",
         "SELECT $1::regoper", (txt("+(int4,int4)"),)),
    Case("regprocedure_param", "SELECT 'abs(numeric)'::regprocedure",
         "SELECT $1::regprocedure", (txt("abs(numeric)"),)),
    Case("regrole_param", "SELECT 'postgres'::regrole",
         "SELECT $1::regrole", (txt("postgres"),)),
    Case("regnamespace_param", "SELECT 'pg_catalog'::regnamespace",
         "SELECT $1::regnamespace", (txt("pg_catalog"),)),
    Case("regconfig_param", "SELECT 'english'::regconfig",
         "SELECT $1::regconfig", (txt("english"),)),
    Case("aclitem_param", "SELECT 'u=r/g'::aclitem", "SELECT $1::aclitem",
         (txt("u=r/g"),)),
    Case("int2vector_param", "SELECT '1 2 3'::int2vector",
         "SELECT $1::int2vector", (txt("1 2 3"),)),
    Case("oidvector_param", "SELECT '1 2 3'::oidvector",
         "SELECT $1::oidvector", (txt("1 2 3"),)),
    Case("tid_param", "SELECT '(0,1)'::tid", "SELECT $1::tid",
         (txt("(0,1)"),)),
    Case("xid_param", "SELECT '5'::xid", "SELECT $1::xid", (txt("5"),)),
    Case("cid_param", "SELECT '5'::cid", "SELECT $1::cid", (txt("5"),)),
    Case("char1_param", "SELECT 'a'::\"char\"", "SELECT $1::\"char\"",
         (txt("a"),)),
    Case("name_param", "SELECT 'n'::name", "SELECT $1::name", (txt("n"),)),
    Case("bpchar_param", "SELECT 'x'::bpchar", "SELECT $1::bpchar",
         (txt("x"),)),
    Case("varchar_trunc", "SELECT 'abcde'::varchar(3)",
         "SELECT $1::varchar(3)", (txt("abcde"),)),
    Case("point_param", "SELECT '(1,2)'::point", "SELECT $1::point",
         (txt("(1,2)"),)),
    Case("lseg_param", "SELECT '[(0,0),(1,1)]'::lseg", "SELECT $1::lseg",
         (txt("[(0,0),(1,1)]"),)),
    Case("box_param", "SELECT '(0,0),(1,1)'::box", "SELECT $1::box",
         (txt("(0,0),(1,1)"),)),
    Case("path_param", "SELECT '[(0,0),(1,1),(2,0)]'::path",
         "SELECT $1::path", (txt("[(0,0),(1,1),(2,0)]"),)),
    Case("polygon_param", "SELECT '((0,0),(1,1),(2,0))'::polygon",
         "SELECT $1::polygon", (txt("((0,0),(1,1),(2,0))"),)),
    Case("circle_param", "SELECT '<(0,0),1>'::circle", "SELECT $1::circle",
         (txt("<(0,0),1>"),)),
    Case("line_param", "SELECT '{1,2,3}'::line", "SELECT $1::line",
         (txt("{1,2,3}"),)),
    Case("cidr_param", "SELECT '10.0.0.0/8'::cidr", "SELECT $1::cidr",
         (txt("10.0.0.0/8"),)),
    Case("macaddr8_param", "SELECT '08:00:2b:01:02:03:04:05'::macaddr8",
         "SELECT $1::macaddr8", (txt("08:00:2b:01:02:03:04:05"),)),
    Case("daterange_param", "SELECT '[2020-01-01,2021-01-01)'::daterange",
         "SELECT $1::daterange", (txt("[2020-01-01,2021-01-01)"),)),
    Case("numrange_param", "SELECT '[1.5,2.5)'::numrange",
         "SELECT $1::numrange", (txt("[1.5,2.5)"),)),
    Case("tstzrange_param", "SELECT '[2020-01-01,)'::tstzrange",
         "SELECT $1::tstzrange", (txt("[2020-01-01,)"),)),
    Case("xml_param", "SELECT '<a/>'::xml", "SELECT $1::xml", (txt("<a/>"),),
         allow_diff=True, note="no libxml: error or pass-through"),
    # ------------------------------------ params in odder grammar slots
    Case("param_overlaps",
         "SELECT (1,5) OVERLAPS (3,7)",
         "SELECT ($1::int,$2::int) OVERLAPS ($3::int,$4::int)",
         (txt("1"), txt("5"), txt("3"), txt("7"))),
    Case("param_between_symmetric", "SELECT 5 BETWEEN SYMMETRIC 10 AND 1",
         "SELECT $1::int BETWEEN SYMMETRIC $2::int AND $3::int",
         (txt("5"), txt("10"), txt("1"))),
    Case("param_distinct_from", "SELECT 5 IS DISTINCT FROM NULL",
         "SELECT $1::int IS DISTINCT FROM $2::int",
         (txt("5"), P(None, INT4))),
    Case("param_is_unknown", "SELECT true IS UNKNOWN",
         "SELECT $1::bool IS UNKNOWN", (txt("true"),)),
    Case("param_is_of", "SELECT 'x' IS OF (text)",
         "SELECT $1 IS OF (text)", (txt("x"),), allow_diff=True),
    Case("param_ilike", "SELECT 'AbC' ILIKE 'a%'",
         "SELECT $1 ILIKE $2", (txt("AbC"), txt("a%"))),
    Case("param_similar", "SELECT 'abc' SIMILAR TO '%b%'",
         "SELECT $1 SIMILAR TO $2", (txt("abc"), txt("%b%"))),
    Case("param_regex", "SELECT 'abc' ~ 'a.*'",
         "SELECT $1 ~ $2", (txt("abc"), txt("a.*"))),
    Case("param_regex_neg", "SELECT 'abc' !~ 'z'",
         "SELECT $1 !~ $2", (txt("abc"), txt("z"))),
    Case("param_substring_triple", "SELECT substring('abcdef' from 2 for 3)",
         "SELECT substring($1 from $2 for $3)",
         (txt("abcdef"), txt("2"), txt("3")), allow_diff=True,
         note="untyped params resolve to (text,text,text) regex form"),
    Case("param_trim", "SELECT trim('x' from 'xax')",
         "SELECT trim($1 from $2)", (txt("x"), txt("xax"))),
    Case("param_overlay", "SELECT overlay('Txxxxas' placing 'hom' "
         "from 2 for 4)",
         "SELECT overlay($1 placing $2 from $3 for $4)",
         (txt("Txxxxas"), txt("hom"), txt("2"), txt("4"))),
    Case("param_extract_err", "SELECT extract(year from now())",
         "SELECT extract($1 from now())", (txt("year"),),
         expect_error=True, note="extract unit cannot be a param"),
    Case("param_date_part", "SELECT date_part('year', '2020-06-15'::date)",
         "SELECT date_part($1, $2::date)", (txt("year"), txt("2020-06-15"))),
    Case("param_make_date", "SELECT make_date(2024,2,29)",
         "SELECT make_date($1,$2,$3)", (txt("2024"), txt("2"), txt("29"))),
    Case("param_make_interval", "SELECT make_interval(secs => 1.5)",
         "SELECT make_interval(secs => $1)", (txt("1.5"),)),
    Case("param_at_time_zone",
         "SELECT '2020-01-01 12:00'::timestamp AT TIME ZONE 'UTC'",
         "SELECT $1::timestamp AT TIME ZONE $2",
         (txt("2020-01-01 12:00"), txt("UTC"))),
    Case("param_to_char", "SELECT to_char('2020-01-01'::date, 'YYYY')",
         "SELECT to_char($1::date, $2)", (txt("2020-01-01"), txt("YYYY"))),
    Case("param_mod_zero", "SELECT 7::numeric % 0",
         "SELECT $1::numeric % $2::numeric", (txt("7"), txt("0")),
         expect_error=True),
    Case("param_shift_ub", "SELECT 1::int4 << 33",
         "SELECT $1::int4 << $2::int4", (txt("1"), txt("33")),
         note="shift>=width: UB per UBSan, x86 wraps to 2"),
    Case("param_union_same", "SELECT 5 UNION SELECT 5",
         "SELECT $1 UNION SELECT $1", (txt("5"),), allow_diff=True,
         note="same $1 in union arms; unknown type resolves once"),
    Case("param_except_same", "SELECT 5 EXCEPT SELECT 5",
         "SELECT $1 EXCEPT SELECT $1", (txt("5"),), allow_diff=True,
         note="untyped $1 resolves to text vs int4 literal"),
    Case("param_in_values", "SELECT 2 IN (VALUES(1),(2))",
         "SELECT $1::int IN (VALUES(1),(2))", (txt("2"),)),
    Case("param_array_ctor", "SELECT ARRAY[1,2]", "SELECT ARRAY[$1,$2]",
         (txt("1"), txt("2")), allow_diff=True,
         note="untyped elements infer text[]"),
    Case("param_row_eq", "SELECT ROW(1,'x') = ROW(1,'x')",
         "SELECT ROW($1::int,$2::text) = ROW($1::int,$2::text)",
         (txt("1"), txt("x"))),
    Case("param_greatest_mixed", "SELECT greatest(5, NULL::int)",
         "SELECT greatest($1::int, $2)", (txt("5"), P(None))),
    Case("param_xml_agg", "SELECT xmlagg(x) FROM (SELECT '<a/>' x) s",
         "SELECT xmlagg(x) FROM (SELECT $1::xml x) s", (txt("<a/>"),),
         allow_diff=True, note="no libxml"),
    # --------------------------------------------------- DML with params
    Case("dml_insert_returning", "INSERT INTO t VALUES (7) RETURNING a",
         "INSERT INTO t VALUES ($1::int) RETURNING a", (txt("7"),),
         setup=("CREATE TABLE t(a int)",)),
    Case("dml_update_returning",
         "UPDATE t SET b = 'z' WHERE a = 5 RETURNING *",
         "UPDATE t SET b = $2 WHERE a = $1 RETURNING *",
         (txt("5"), txt("z")),
         setup=("CREATE TABLE t(a int, b text)",
                "INSERT INTO t VALUES (5,'x'),(6,'y')")),
    Case("dml_delete_returning", "DELETE FROM t WHERE a = 6 RETURNING a",
         "DELETE FROM t WHERE a = $1 RETURNING a", (txt("6"),),
         setup=("CREATE TABLE t(a int)",
                "INSERT INTO t VALUES (5),(6)")),
    Case("dml_on_conflict",
         "INSERT INTO t VALUES (1,'a') ON CONFLICT (a) DO UPDATE SET b = "
         "'u' RETURNING *",
         "INSERT INTO t VALUES ($1,$2) ON CONFLICT (a) DO UPDATE SET b = "
         "$3 RETURNING *", (txt("1"), txt("a"), txt("u")),
         setup=("CREATE TABLE t(a int PRIMARY KEY, b text)",
                "INSERT INTO t VALUES (1,'old')")),
    Case("dml_merge",
         "MERGE INTO t USING (SELECT 1 a) s ON t.a = s.a WHEN MATCHED THEN "
         "UPDATE SET b = 'x' WHEN NOT MATCHED THEN INSERT VALUES (s.a,'n')",
         "MERGE INTO t USING (SELECT $1::int a) s ON t.a = s.a WHEN "
         "MATCHED THEN UPDATE SET b = 'x' WHEN NOT MATCHED THEN INSERT "
         "VALUES (s.a,'n')", (txt("1"),),
         setup=("CREATE TABLE t(a int, b text)",
                "INSERT INTO t VALUES (1,'o')")),
    Case("dml_insert_multi_param", "INSERT INTO t VALUES (1),(2),(3)",
         "INSERT INTO t VALUES ($1),($2),($3)",
         (txt("1"), txt("2"), txt("3")),
         setup=("CREATE TABLE t(a int)",)),
    Case("dml_ctas_param", "CREATE TABLE s AS SELECT 5",
         "CREATE TABLE s AS SELECT $1", (txt("5"),), allow_diff=True,
         post=("DROP TABLE IF EXISTS s",),
         note="CTAS with param"),
    Case("dml_insert_default", "INSERT INTO t DEFAULT VALUES",
         "INSERT INTO t DEFAULT VALUES", (),
         setup=("CREATE TABLE t(a int DEFAULT 9)",)),
    # ------------------------------------------- prepare lifecycle edges
    Case("prep_exec_twice", "SELECT 2::int * 3", "SELECT $1::int * 3",
         (txt("2"),), prepare=True, reps=4,
         note="exec same named stmt 4x"),
    Case("prep_param_oid_forced", "SELECT '5'::int + 1",
         "SELECT $1 + 1", (txt("5", INT4),), prepare=True,
         note="declare int4 explicitly for $1+1"),
]


def looks_internal(error: str | None) -> bool:
    if not error:
        return False
    low = error.lower()
    return any(m in low for m in (
        "xx000", "assert", "panic", "unexpected", "cache lookup failed",
        "unrecognized node", "variable not found in subplan",
        "no relation entry", "could not find pathkey",
        "server closed the connection", "terminating connection",
        "connection not open", "could not receive data from server",
        "internalerror", "segv", "signal"))


class ProtoRunner:
    """Drives one libpq connection for simple + extended calls."""

    def __init__(self, lib: LibPQ, conninfo: str, timeout_s: float):
        self.lib = lib
        self.conninfo = conninfo
        self.timeout_s = timeout_s
        self.conn = None
        self.connect()

    def connect(self):
        if self.conn:
            try:
                self.lib.lib.PQfinish(self.conn)
            except Exception:  # noqa: BLE001
                pass
        self.conn = self.lib.connect(self.conninfo)
        self.exec_simple("SET client_encoding = 'UTF8'")
        self.exec_simple("SET statement_timeout = '12s'")

    def dead(self) -> bool:
        return self.lib.lib.PQstatus(self.conn) != 0

    def _collect(self, results, timed_out):
        recs = [self.lib.result_record(r) for r in results]
        return {"results": recs, "timed_out": timed_out,
                "conn_bad": self.dead()}

    def exec_simple(self, sql: str) -> dict:
        if self.lib.lib.PQsendQuery(self.conn, sql.encode("utf-8")) != 1:
            return {"results": [], "send_error": self.lib.err_msg(self.conn),
                    "timed_out": False, "conn_bad": self.dead()}
        results, timed_out = self.lib.drain(self.conn, self.timeout_s)
        return self._collect(results, timed_out)

    def _param_arrays(self, params):
        n = len(params)
        encs = [p.enc() for p in params]
        types = (ctypes.c_uint * n)(*[p.oid for p in params])
        vals = (ctypes.c_char_p * n)(
            *[e if e is not None else None for e in encs])
        lens = (ctypes.c_int * n)(
            *[len(e) if e is not None else 0 for e in encs])
        fmts = (ctypes.c_int * n)(*[1 if p.binary else 0 for p in params])
        keep = (encs, types, vals, lens, fmts)
        return n, types, vals, lens, fmts, keep

    def exec_params(self, sql: str, params, result_binary: bool) -> dict:
        n, types, vals, lens, fmts, keep = self._param_arrays(params)
        ok = self.lib.lib.PQsendQueryParams(
            self.conn, sql.encode("utf-8"), n, types, vals, lens, fmts,
            1 if result_binary else 0)
        del keep
        if ok != 1:
            return {"results": [], "send_error": self.lib.err_msg(self.conn),
                    "timed_out": False, "conn_bad": self.dead()}
        results, timed_out = self.lib.drain(self.conn, self.timeout_s)
        return self._collect(results, timed_out)

    def exec_prepared(self, sql: str, params, name: str,
                      result_binary: bool) -> dict:
        """PQprepare + PQdescribePrepared + PQexecPrepared."""
        out = {"describe": None}
        param_types = tuple(p.oid for p in params)
        declared = len([o for o in param_types])
        n_decl = len(param_types)
        oids = (ctypes.c_uint * n_decl)(*param_types) if n_decl else None
        ok = self.lib.lib.PQsendPrepare(
            self.conn, name.encode(), sql.encode("utf-8"), n_decl, oids)
        if ok != 1:
            return {"results": [], "send_error": self.lib.err_msg(self.conn),
                    "timed_out": False, "conn_bad": self.dead(), **out}
        results, timed_out = self.lib.drain(self.conn, self.timeout_s)
        prep = self._collect(results, timed_out)
        out["prepare"] = prep
        if timed_out or self.dead():
            out.update(prep)
            return out
        if prep["results"] and prep["results"][-1]["error"]:
            out.update(prep)
            return out
        # describe
        if self.lib.lib.PQsendDescribePrepared(self.conn, name.encode()) != 1:
            out["send_error"] = self.lib.err_msg(self.conn)
            return out
        results, timed_out = self.lib.drain(self.conn, self.timeout_s)
        out["describe"] = self._collect(results, timed_out)
        if self.dead():
            return out
        # execute
        n, types, vals, lens, fmts, keep = self._param_arrays(params)
        ok = self.lib.lib.PQsendQueryPrepared(
            self.conn, name.encode(), n, vals, lens, fmts,
            1 if result_binary else 0)
        del keep
        if ok != 1:
            out.update({"results": [], "send_error": self.lib.err_msg(
                self.conn), "timed_out": False, "conn_bad": self.dead()})
            return out
        results, timed_out = self.lib.drain(self.conn, self.timeout_s)
        out.update(self._collect(results, timed_out))
        return out


def canon_rows(rec: dict) -> list | None:
    if not rec or "rows" not in rec:
        return None
    types = rec.get("coltypes") or [0] * len(rec["rows"][0] if rec["rows"]
                                            else [])
    binary = rec.get("binary", False)
    bag = []
    for row in rec["rows"]:
        bag.append(tuple(canon_cell(types[i] if i < len(types) else 0,
                                    cell, binary)
                         for i, cell in enumerate(row)))
    return sorted(bag, key=repr)


def primary(recs: list[dict]) -> dict | None:
    """Last non-empty result record (the statement result)."""
    for r in reversed(recs):
        if r.get("status") != "PGRES_EMPTY_QUERY":
            return r
    return recs[-1] if recs else None


def compare(simple: dict, ext: dict) -> tuple[str, dict]:
    """Classify divergence between simple and extended primary results."""
    detail: dict = {}
    sp, ep = primary(simple.get("results", [])), primary(
        ext.get("results", []))
    if sp is None or ep is None:
        return "no_result", detail
    detail["simple_status"] = sp["status"]
    detail["ext_status"] = ep["status"]
    s_err, e_err = sp.get("error"), ep.get("error")
    detail["simple_error"] = s_err
    detail["ext_error"] = e_err
    if looks_internal(e_err) or looks_internal(s_err):
        return "internal", detail
    if s_err and e_err:
        return "both_error", detail
    if s_err or e_err:
        return "error_diff", detail
    if sp["status"] != ep["status"]:
        return "status_diff", detail
    s_rows, e_rows = canon_rows(sp), canon_rows(ep)
    if s_rows is None or e_rows is None:
        return "ok" if sp["status"] == ep["status"] else "status_diff", detail
    detail["simple_rows"] = sp["rows"]
    detail["ext_rows"] = ep["rows"]
    detail["simple_coltypes"] = sp.get("coltypes")
    detail["ext_coltypes"] = ep.get("coltypes")
    if s_rows != e_rows:
        return "value_diff", detail
    if sp.get("coltypes") != ep.get("coltypes"):
        return "coltype_diff", detail
    return "ok", detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, help="PG install prefix")
    ap.add_argument("--pg-datadir", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--max-cases", type=int, default=0)
    ap.add_argument("--only", default="", help="comma list of case names")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prefix = Path(args.prefix)

    pg = PostgresRunner(
        args.pg_datadir or (out / "datadir"), pg_prefix=prefix)
    LOGGER.info("server up: %s", pg._server.get_uri())
    lib = LibPQ(prefix / "lib")
    runner = ProtoRunner(lib, pg._server.get_uri(), args.timeout)

    cases = CASES
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.name in wanted]
    if args.max_cases:
        cases = cases[: args.max_cases]
    LOGGER.info("%d cases", len(cases))

    hits: list[dict] = []
    stats = {"cases": 0, "ok": 0, "coltype_diff": 0, "value_diff": 0,
             "error_diff": 0, "both_error": 0, "internal": 0,
             "expected_errors": 0, "allowed_diffs": 0, "setup_fail": 0,
             "timeouts": 0, "log_fatal": 0}
    t0 = time.time()
    try:
        for case in cases:
            stats["cases"] += 1
            pg.log_new_lines()  # drain stale log before the case
            rec: dict = {"name": case.name, "simple_sql": case.simple,
                         "ext_sql": case.ext,
                         "params": [str(p.v)[:80] for p in case.params],
                         "note": case.note}
            # --- schema reset + setup (simple protocol) ---
            setup_out = runner.exec_simple(
                "DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            if runner.dead():
                runner.connect()
            setup_err = None
            for stmt in case.setup:
                r = runner.exec_simple(stmt)
                p = primary(r["results"])
                if p and p["error"]:
                    setup_err = p["error"]
                    break
            if setup_err:
                stats["setup_fail"] += 1
                rec["verdict"] = "setup_fail"
                rec["setup_error"] = setup_err
                hits.append({"kind": "setup_fail", **rec}) if looks_internal(
                    setup_err) else None
                with open(out / "cases.jsonl", "a") as fh:
                    fh.write(json.dumps(rec, default=str) + "\n")
                continue

            # --- simple protocol oracle ---
            simple = runner.exec_simple(case.simple)
            if simple["timed_out"]:
                stats["timeouts"] += 1
            if runner.dead():
                runner.connect()

            # --- extended protocol runs ---
            # Write statements change state: reset + re-setup so the ext
            # run observes the same pre-state the simple run did.
            ext_verb = case.ext.lstrip().split(None, 1)[0].upper() \
                if case.ext.strip() else ""
            if ext_verb not in ("SELECT", "WITH", "TABLE", "VALUES",
                                "SET", "RESET", "DECLARE", ""):
                runner.exec_simple(
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public")
                if runner.dead():
                    runner.connect()
                for stmt in case.setup:
                    runner.exec_simple(stmt)
            for stmt in case.pre:
                runner.exec_simple(stmt)
            ext_runs = []
            pname = f"st_{case.name}"
            for rep in range(max(1, case.reps)):
                if case.prepare:
                    if rep == 0:
                        r = runner.exec_prepared(
                            case.ext, case.params, pname,
                            case.result_binary)
                        ext_runs.append(r)
                    else:
                        n, t_, v_, l_, f_, keep = runner._param_arrays(
                            case.params)
                        ok = runner.lib.lib.PQsendQueryPrepared(
                            runner.conn, pname.encode(), n, v_, l_, f_,
                            1 if case.result_binary else 0)
                        del keep
                        if ok != 1:
                            ext_runs.append(
                                {"results": [], "send_error":
                                 runner.lib.err_msg(runner.conn),
                                 "timed_out": False,
                                 "conn_bad": runner.dead()})
                        else:
                            res, to = runner.lib.drain(
                                runner.conn, args.timeout)
                            ext_runs.append(runner._collect(res, to))
                else:
                    ext_runs.append(runner.exec_params(
                        case.ext, case.params, case.result_binary))
                if runner.dead():
                    runner.connect()
                    break
            for stmt in case.post:
                runner.exec_simple(stmt)

            # describe output for prepare-path cases (inferred param OIDs)
            if case.prepare and ext_runs and ext_runs[0].get("describe"):
                dres = ext_runs[0]["describe"].get("results") or []
                if dres:
                    rec["describe"] = {
                        "param_oids": dres[0].get("param_oids"),
                        "result_coltypes": dres[0].get("coltypes")}

            # --- server-log fatal scan ---
            fatal = pg.log_fatal_lines(pg.log_new_lines())
            if fatal:
                stats["log_fatal"] += 1
                rec["log_fatal"] = fatal[:30]
                hits.append({"kind": "log_fatal", **rec})

            # --- verdict ---
            if case.expect_error:
                ep = primary(ext_runs[-1]["results"]) if ext_runs else None
                got_err = bool(ep and (
                    ep["error"] or ep["status"] in (
                        "PGRES_FATAL_ERROR", "PGRES_EMPTY_QUERY",
                        "PGRES_BAD_RESPONSE"))) or any(
                    looks_internal(r.get("send_error")) for r in ext_runs)
                bad_internal = any(
                    looks_internal(pr["error"])
                    for r in ext_runs for pr in r["results"])
                if bad_internal:
                    verdict = "internal"
                elif got_err:
                    verdict = "expected_error"
                    stats["expected_errors"] += 1
                else:
                    verdict = "missing_expected_error"
                rec["verdict"] = verdict
                rec["ext_error"] = ep["error"] if ep else None
                if verdict == "internal":
                    hits.append({"kind": "internal", **rec})
            else:
                per_run = []
                verdict = "ok"
                for ext in ext_runs:
                    v, detail = compare(simple, ext)
                    per_run.append({"verdict": v, **{
                        k: detail[k] for k in
                        ("ext_error", "ext_status") if k in detail}})
                    if v == "internal":
                        verdict = "internal"
                        rec["detail"] = detail
                        break
                    if v in ("value_diff", "error_diff", "status_diff"):
                        if case.allow_diff:
                            if verdict == "ok":
                                verdict = f"allowed_{v}"
                                stats["allowed_diffs"] += 1
                        else:
                            verdict = v
                            rec["detail"] = detail
                            break
                    if v == "coltype_diff" and verdict == "ok":
                        if case.allow_diff:
                            verdict = "allowed_coltype_diff"
                            stats["allowed_diffs"] += 1
                        else:
                            verdict = "coltype_diff"
                        rec["detail"] = detail
                    if v == "both_error":
                        verdict = "both_error" if verdict == "ok" else verdict
                # absolute oracle: hand-computed expected rows
                if verdict == "ok" and case.expect_rows is not None:
                    ep = primary(ext_runs[-1]["results"])
                    types = ep.get("coltypes") or []
                    binary = ep.get("binary", False)
                    got = sorted(
                        (tuple(canon_cell(
                            types[i] if i < len(types) else 0, c, binary)
                            for i, c in enumerate(row))
                         for row in ep.get("rows", [])), key=repr)
                    want = sorted(
                        (tuple(canon_cell(
                            types[i] if i < len(types) else 0,
                            str(c).encode(), False)
                            for i, c in enumerate(row))
                         for row in case.expect_rows), key=repr)
                    if got != want:
                        verdict = "expect_mismatch"
                        rec["expected_rows"] = case.expect_rows
                        rec["got_rows"] = ep.get("rows")
                stats[verdict] = stats.get(verdict, 0) + 1
                rec["verdict"] = verdict
                rec["runs"] = per_run
                if verdict in ("value_diff", "error_diff", "status_diff",
                               "internal", "expect_mismatch"):
                    hits.append({"kind": verdict, **rec})
            if stats["cases"] % 20 == 0:
                LOGGER.info("case %d/%d verdict=%s hits=%d",
                            stats["cases"], len(cases), rec["verdict"],
                            len(hits))
            with open(out / "cases.jsonl", "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
    finally:
        try:
            lib.lib.PQfinish(runner.conn)
        except Exception:  # noqa: BLE001
            pass
        pg.cleanup()

    with open(out / "hits.json", "w") as fh:
        json.dump(hits, fh, indent=1, default=str)
    summary = {**stats, "elapsed_s": round(time.time() - t0, 1),
               "prefix": str(prefix)}
    with open(out / "summary.json", "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
