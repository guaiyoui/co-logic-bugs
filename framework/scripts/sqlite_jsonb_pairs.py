"""JSON vs JSONB function-pair equivalence oracle for SQLite 3.51.2.

Correct canonicalization: jsonb_extract()/->> return SQL scalars for
scalar leaves and jsonb BLOB for object/array leaves; the text
variants return scalars and JSON text. Canon:
  bytes (jsonb blob)      -> json(blob)            (canonical text)
  str starting { or [     -> json(str)             (canonical text)
  other                   -> repr(value)
"""
from __future__ import annotations

import itertools
import sqlite3
import sys

DOCS = [
    '{"a":1,"b":[1,2,3],"c":{"d":"x"}}',
    '{"a":1,"a":2}',
    '{"a":[1,{"b":[2,3]},null],"z":{"y":true}}',
    '[1,2,[3,4,{"k":"v"}],"x"]',
    '{"n":9007199254740993,"m":-9223372036854775808}',
    '{"f":5.0,"g":-0.0,"h":1e400,"i":1e-400}',
    '{"u":"\\u0041\\u00e9\\u4e2d","e":"\\ud83d\\ude00"}',
    '{}', '[]', 'null', 'true', '5', '"s"',
    '{"deep":{"a":{"b":{"c":{"d":[0,{"e":1}]}}}}}',
    '{"big":' + '9' * 30 + '}',
    '{"arr":[' + ','.join(str(i) for i in range(40)) + ']}',
    '{"sp":"a b\\tc\\n\\\\d"}',
    '{"k":"v","l":null,"m":false,"n":1.5e300}',
    '[null,true,false,0,0.0,"0","",[],{}]',
    '{"x":2.5e-1,"y":2500e-4,"z":25e-2}',
    '{"mi":9223372036854775807,"mo":9223372036854775808}',
    '{"neg":-0,"fl":-0.0,"nz":-1e-999}',
    '{"dup":1,"dup":"two","dup":[3]}',
    '{"a":{"x":{"y":{"z":{"w":[1,2,{"v":3}]}}}}}',
]

PATHS = [
    '$', '$.a', '$.b', '$.c', '$.a[0]', '$.a[1]', '$.a[2]', '$.a[9]',
    '$.b[0]', '$.b[2]', '$.b[5]', '$.c.d', '$.z.y', '$.n', '$.m',
    '$.f', '$.g', '$.h', '$.i', '$.u', '$.e', '$.deep.a.b.c.d[1].e',
    '$.big', '$.arr[0]', '$.arr[39]', '$.arr[40]', '$.arr[#-1]',
    '$.sp', '$.k', '$.l', '$.missing', '$.dup', '$.x', '$.y', '$.z',
    '$.mi', '$.mo', '$.neg', '$.fl', '$.nz',
    '$[0]', '$[1]', '$[3]', '$[4]', '$[5]', '$[8]', '$[9]', '$[#-1]',
    '$.a.b', '$.nope[0]', '$.a[0].b', '$.c.d[0]', '$.a.x.y.z.w[2].v',
]


def lit(s):
    return "'" + s.replace("'", "''") + "'"


def gen_pairs():
    for doc, path in itertools.product(DOCS, PATHS):
        d, p = lit(doc), lit(path)
        # raw leaf-value comparison (scalars + canonical docs)
        yield ("x->>", f"SELECT {d}->>{p}",
               f"SELECT jsonb({d})->>{p}")
        yield ("xtype", f"SELECT json_type({d},{p}),"
                        f"typeof(json_extract({d},{p}))",
               f"SELECT json_type(jsonb({d}),{p}),"
                    f"typeof(jsonb_extract(jsonb({d}),{p}))")
    for doc in DOCS:
        d = lit(doc)
        yield ("valid", f"SELECT json_valid({d})",
               f"SELECT json_valid(jsonb({d}))")
        yield ("each", f"SELECT group_concat(key||'='||value,'|') FROM json_each({d})",
               f"SELECT group_concat(key||'='||value,'|') FROM json_each(jsonb({d}))")
        for fn in ("patch", "remove", "set", "insert", "replace"):
            extra = "'{\"NEW\":1}'" if fn == "patch" else "'$.a',99" \
                if fn in ("set", "insert", "replace") else "'$.a'"
            yield (fn, f"SELECT json_{fn}({d},{extra})",
                   f"SELECT json(jsonb_{fn}(jsonb({d}),{extra}))")
        yield ("minify", f"SELECT json({d})",
               f"SELECT json(jsonb({d}))")
        yield ("quote", f"SELECT json_quote({d})",
               f"SELECT json(jsonb_quote(jsonb({d})))")


def canon_val(con, v):
    if isinstance(v, bytes):
        r = con.execute("SELECT json(?)", (v,)).fetchone()
        return "J:" + r[0]
    if isinstance(v, str) and v[:1] in ("{", "["):
        try:
            r = con.execute("SELECT json(?)", (v,)).fetchone()
            return "J:" + r[0]
        except Exception:
            return "T:" + v
    return "V:" + repr(v)


def canon_row(con, row):
    return tuple(canon_val(con, v) for v in row)


def run(con, sql):
    try:
        rows = con.execute(sql).fetchall()
        return ("ok", [canon_row(con, r) for r in rows])
    except Exception as e:
        return ("err", f"{type(e).__name__}: {e}")


def main():
    con = sqlite3.connect(":memory:")
    findings = []
    n = 0
    for tag, q1, q2 in gen_pairs():
        n += 1
        r1 = run(con, q1)
        r2 = run(con, q2)
        if r1 != r2:
            findings.append((tag, q1, r1, q2, r2))
            print(f"[DIFF:{tag}]\n   {q1}\n   -> {r1}\n   {q2}\n   -> {r2}")
    print(f"{len(findings)} diffs of {n} pairs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
