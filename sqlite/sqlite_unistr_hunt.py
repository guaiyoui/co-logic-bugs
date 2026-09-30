#!/usr/bin/env python3
"""Probe sqlite unistr() Unicode-escape handling (3.51.2).

Family candidate SQLITE-C: unistr() does not combine UTF-16 surrogate
pairs and does not validate Unicode scalar values, emitting CESU-8 /
out-of-range pseudo-UTF-8 bytes.  Internal oracle (no cross-engine
needed): the same code point reachable via \\U 8-digit form or char()
produces different bytes than the surrogate-pair \\u form.

Usage: python scripts/sqlite_unistr_hunt.py
"""
import sqlite3
import sys

REPEATS = 10

# (label, sql, expected_oracle_note)
CASES = [
    # headline: surrogate pair must combine to U+1F600 (UTF-8 F09F9880)
    ("pair->hex", "SELECT hex(unistr('\\ud83d\\ude00'))", "F09F9880"),
    ("pair vs \\U-form", "SELECT unistr('\\ud83d\\ude00') = unistr('\\U0001F600')", "1"),
    ("pair vs char()", "SELECT unistr('\\ud83d\\ude00') = char(128512)", "1"),
    ("pair unicode()", "SELECT unicode(unistr('\\ud83d\\ude00'))", "128512"),
    ("pair length()", "SELECT length(unistr('\\ud83d\\ude00'))", "1"),
    # lone surrogates should be rejected (PG: ERROR); sqlite encodes raw
    ("lone-high hex", "SELECT hex(unistr('\\ud83d'))", "error or empty"),
    ("lone-low hex", "SELECT hex(unistr('\\udc00'))", "error or empty"),
    # out-of-range scalars: char(1114112) sanitizes to FFFD; unistr must not
    # emit a longer invalid sequence
    ("oorange vs char()", "SELECT unistr('\\U00110000') = char(1114112)", "1"),
    ("oorange hex", "SELECT hex(unistr('\\U00110000'))", "EFBFBD"),
    ("oobig hex", "SELECT hex(unistr('\\UFFFFFFFF'))", "EFBFBD"),
    # sanity baselines that must keep working
    ("ascii ok", "SELECT hex(unistr('a\\u0041b'))", "614162"),
    ("\\U-form ok", "SELECT hex(unistr('\\U0001F600'))", "F09F9880"),
]


def run_once():
    con = sqlite3.connect(":memory:")
    out = {}
    try:
        for label, q, _ in CASES:
            try:
                rows = con.execute(q).fetchall()
                out[label] = ("ok", tuple(rows))
            except Exception as e:  # noqa: BLE001
                out[label] = ("err", str(e))
    finally:
        con.close()
    return out


def main() -> int:
    print(f"sqlite {sqlite3.sqlite_version}, {REPEATS} fresh connections")
    all_runs = [run_once() for _ in range(REPEATS)]
    bad = 0
    for label, q, expect in CASES:
        vals = {r[label] for r in all_runs}
        stable = len(vals) == 1
        v = next(iter(vals))
        got = repr(v[1][0][0]) if v[0] == "ok" else f"err:{v[1]}"
        flag = "OK " if stable else "UNSTABLE"
        dev = "" if str(v[1][0][0]) == expect and v[0] == "ok" else "  <-- deviates"
        if dev:
            bad += 1
        print(f"{flag} {got:<16} expect {expect:<14} | {q}{dev}")
    print(f"{bad} deviating cases; all runs deterministic: "
          f"{all(len({r[l] for r in all_runs}) == 1 for l,_,_ in CASES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
