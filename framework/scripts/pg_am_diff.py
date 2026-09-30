#!/usr/bin/env python3
"""Non-btree access-method correctness differential.

For each AM family (GIN / GiST / SP-GiST / BRIN / hash / bloom) the script
builds a dedicated table + index shape and a set of *AM-shaped* predicates
the corpus never had (the index_variant oracle's GIN variant never fired:
0 corpus seeds carry array/jsonb columns, and GiST/SP-GiST/hash variants
do not exist at all).

Oracle
------
truth: query run with index paths disabled (enable_indexscan /
enable_bitmapscan / enable_indexonlyscan off) -> sequential scan.
test : query run with enable_seqscan=off; EXPLAIN must show the target
index name, otherwise the probe is recorded as 'not_fired' (vacuous).
Bag (multiset) equality is required; KNN queries are marked ordered and
carry an `, id` total-order tiebreak so result *lists* are compared.

Between scan pairs, storage-state mutations run (insert/update/delete/
VACUUM, and AM-specific verbs: gin_clean_pending_list, pending-list
growth via small gin_pending_list_limit, brin_summarize_new_values,
brin_desummarize_range).  Assert builds + ASan amplify latent corruption.

    python scripts/pg_am_diff.py --prefix /path/to/pgXXX_assert \
        --out results/pg_am_diff --seed 1 [--families gin,gist] [--scale 1.0]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner  # noqa: E402

TRUTH_GUCS = [
    "SET enable_indexscan = off",
    "SET enable_indexonlyscan = off",
    "SET enable_bitmapscan = off",
    "SET enable_seqscan = on",
    "SET enable_tidscan = off",
]
TEST_GUCS = [
    "SET enable_indexscan = on",
    "SET enable_indexonlyscan = on",
    "SET enable_bitmapscan = on",
    "SET enable_seqscan = off",
]
RESET_GUCS = [
    "RESET enable_indexscan",
    "RESET enable_indexonlyscan",
    "RESET enable_bitmapscan",
    "RESET enable_seqscan",
    "RESET enable_tidscan",
    "RESET gin_pending_list_limit",
]


def _big_array_literal(n: int = 60) -> str:
    return "ARRAY[" + ",".join(str(i) for i in range(n)) + "]"


# ------------------------------------------------------------------ workload
# Each family: extension (contrib .control must exist, else family is
# skipped), setup sqls (extension first — setup() drops public schema),
# index variants [(label, ddl)], queries [(label, sql, ordered)],
# mutations: cumulative list applied between scan rounds.


def families(rng: random.Random, scale: float) -> list[dict]:
    n = int(15000 * scale)
    big = _big_array_literal()
    # seed-derived literals so different seeds exercise different key
    # distributions and predicate selectivities
    amod = rng.choice([89, 97, 101, 113])          # intarray modulus
    akey = rng.randint(0, 15)                       # array probe key
    obase = rng.choice([400, 500, 600])             # range modulus
    rlo, rhi = rng.randint(0, 40), rng.randint(0, 40)
    pmod = rng.choice([2000, 3000, 4000])           # hash modulus
    trgm = rng.choice(["abcdef", "hello", "zzabc"])  # trgm probe stem
    return [
        # ------------------------------------------------------- GIN int[]
        dict(
            name="gin_intarray", ext=None,
            setup=[
                "CREATE TABLE ga (id int PRIMARY KEY, a int[], "
                "b int[], pad text)",
                f"INSERT INTO ga SELECT i, "
                f"ARRAY[i%{amod}, (i*3)%{amod}, (i*7)%{amod}, i%5], "
                # b: near-unique elements -> deep GIN entry tree
                f"ARRAY[i, i%13] ,"
                f"md5(i::text) FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("gin_default",
                 ["CREATE INDEX ga_g ON ga USING gin(a)"]),
                ("gin_nofastupdate",
                 ["CREATE INDEX ga_g ON ga USING gin(a) "
                  "WITH (fastupdate = off)"]),
                ("gin_pending",  # build index, THEN feed pending list
                 ["CREATE INDEX ga_g ON ga USING gin(a)",
                  f"INSERT INTO ga SELECT i, ARRAY[i%{amod},"
                  f"(i*5)%{amod}], ARRAY[i,i%13], 'p' "
                  f"FROM generate_series({n}+1,{n}+4000) i",
                  "SET gin_pending_list_limit = '64MB'"]),
                ("gin_multicol",  # multi-key bitmap AND across attrs
                 ["CREATE INDEX ga_g ON ga USING gin(a, b)"]),
            ],
            queries=[
                ("contains",
                 f"SELECT id FROM ga WHERE a @> ARRAY[{akey}]", False),
                ("overlap",
                 f"SELECT id FROM ga WHERE a && ARRAY[10,11,{akey+40}]",
                 False),
                ("contained_by",
                 f"SELECT id FROM ga WHERE a <@ {big}", False),
                ("eq_arr", "SELECT id FROM ga WHERE a = ARRAY[1,4,7]", False),
                # cross-attribute bitmap AND (deep path on multicol index)
                ("multi_and",
                 f"SELECT id FROM ga WHERE a @> ARRAY[{akey}] "
                 f"AND b && ARRAY[{n}+10]", False),
            ],
            mutations=[
                [f"INSERT INTO ga(id,a,b,pad) SELECT i, "
                 f"ARRAY[i%{amod},(i*2)%{amod}], ARRAY[i,i%13],'m1' "
                 f"FROM generate_series({n}+4001,{n}+6000) i"],
                ["SET gin_pending_list_limit = '4kB'",
                 f"INSERT INTO ga(id,a,b,pad) SELECT i, "
                 f"ARRAY[i%{amod}], ARRAY[i,i%13],'m2' "
                 f"FROM generate_series({n}+6001,{n}+7500) i"],
                ["SELECT gin_clean_pending_list('ga_g')"],
                ["UPDATE ga SET a = a || 99 WHERE id % 13 = 0"],
                ["VACUUM ga"],
                ["DELETE FROM ga WHERE id % 17 = 0",
                 f"INSERT INTO ga(id,a,b,pad) SELECT i, "
                 f"ARRAY[5, 10], ARRAY[i,5],'m3' "
                 f"FROM generate_series({n}+7501,{n}+8000) i"],
            ]),
        # ------------------------------------------------------- GIN jsonb
        dict(
            name="gin_jsonb", ext=None,
            setup=[
                "CREATE TABLE gj (id int PRIMARY KEY, j jsonb)",
                f"INSERT INTO gj SELECT i, jsonb_build_object("
                f"'k', i%100, 'tag', jsonb_build_array(i%7, i%11),"
                f"'s', 'v'||i%50) FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("jsonb_ops", ["CREATE INDEX gj_g ON gj USING gin(j)"]),
                ("jsonb_path_ops",
                 ["CREATE INDEX gj_g ON gj USING gin(j jsonb_path_ops)"]),
            ],
            queries=[
                ("cont_k", "SELECT id FROM gj WHERE j @> '{\"k\":5}'", False),
                ("cont_tag",
                 "SELECT id FROM gj WHERE j @> '{\"tag\":[3]}'", False),
                ("exists_k", "SELECT id FROM gj WHERE j ? 'k'", False),
                ("exists_tag", "SELECT id FROM gj WHERE j ? 'tag'", False),
            ],
            mutations=[
                [f"INSERT INTO gj SELECT i, jsonb_build_object('k',i%100,"
                 f"'tag',jsonb_build_array(3),'s','x') "
                 f"FROM generate_series({n}+1,{n}+2500) i"],
                ["UPDATE gj SET j = j || '{\"extra\":1}' WHERE id % 9 = 0"],
                ["VACUUM gj"],
                ["DELETE FROM gj WHERE id % 23 = 0"],
                ["SET gin_pending_list_limit = '4kB'",
                 f"INSERT INTO gj SELECT i, '{{\"k\":5}}' "
                 f"FROM generate_series({n}+2501,{n}+4000) i"],
            ]),
        # --------------------------------------------------- GIN pg_trgm
        dict(
            name="gin_trgm", ext="pg_trgm",
            setup=[
                "CREATE EXTENSION pg_trgm",
                "CREATE TABLE gt (id int PRIMARY KEY, s text)",
                # plant trigram-rich families so '%' and LIKE hit
                f"INSERT INTO gt SELECT i, "
                f"CASE WHEN i%4=0 THEN 'abcdef'||i "
                f"WHEN i%4=1 THEN 'xabcdefx'||i "
                f"WHEN i%4=2 THEN 'zzabc'||i "
                f"ELSE md5(i::text) END "
                f"FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("gin_trgm",
                 ["CREATE INDEX gt_g ON gt USING gin(s gin_trgm_ops)"]),
                ("gin_trgm_pend",
                 ["CREATE INDEX gt_g ON gt USING gin(s gin_trgm_ops)",
                  f"INSERT INTO gt SELECT i, 'abcdefp'||i "
                  f"FROM generate_series({n}+1,{n}+3000) i"]),
            ],
            queries=[
                ("similar", "SELECT id FROM gt WHERE s % 'abcdef'", False),
                ("like_mid", "SELECT id FROM gt WHERE s LIKE '%abcde%'",
                 False),
                ("like_pre", "SELECT id FROM gt WHERE s LIKE 'abcdef%'",
                 False),
            ],
            mutations=[
                [f"INSERT INTO gt SELECT i,'abcq'||i "
                 f"FROM generate_series({n}+3001,{n}+5000) i"],
                ["UPDATE gt SET s = 'updabc'||id WHERE id % 11 = 0"],
                ["SET gin_pending_list_limit = '4kB'",
                 f"INSERT INTO gt SELECT i,'abcdefz'||i "
                 f"FROM generate_series({n}+5001,{n}+6500) i"],
                ["SELECT gin_clean_pending_list('gt_g')"],
                ["VACUUM gt", "DELETE FROM gt WHERE id % 19 = 0"],
            ]),
        # --------------------------------------------------- GiST ranges
        dict(
            name="gist_range", ext=None,
            setup=[
                "CREATE TABLE gr (id int PRIMARY KEY, r int4range, "
                "ts tsrange)",
                # fixed timestamp base: now() would drift between the
                # truth and test runs and flip boundary overlaps (FP)
                f"INSERT INTO gr SELECT i, "
                f"int4range(i%{obase}, i%{obase} + (i%20)+1), "
                f"tsrange('2024-06-01'::timestamp"
                f"        + ((i%1000)||' hours')::interval,"
                f"        '2024-06-01'::timestamp"
                f"        + ((i%1000)||' hours')::interval"
                f"        + ((i%12)+1||' hours')::interval) "
                f"FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("gist_r", ["CREATE INDEX gr_g ON gr USING gist(r)"]),
                ("gist_ts", ["CREATE INDEX gr_g ON gr USING gist(ts)"]),
            ],
            queries=[
                ("r_overlap",
                 f"SELECT id FROM gr WHERE r && "
                 f"'[{10+rlo},{60+rhi})'::int4range", False),
                ("r_contains_pt",
                 f"SELECT id FROM gr WHERE r @> {42+rlo}", False),
                ("r_left",
                 "SELECT id FROM gr WHERE r << '[100,110)'::int4range",
                 False),
                ("r_right",
                 f"SELECT id FROM gr WHERE r >> "
                 f"'[{obase-20},{obase-10})'::int4range",
                 False),
                ("r_adjacent",
                 "SELECT id FROM gr WHERE r -|- '[25,75)'::int4range", False),
                ("ts_overlap",
                 "SELECT id FROM gr WHERE ts && tsrange("
                 "'2024-06-15'::timestamp, '2024-06-20'::timestamp)",
                 False),
            ],
            mutations=[
                [f"INSERT INTO gr SELECT i, int4range(i%500, i%500+10),"
                 f" tsrange('2024-06-10'::timestamp"
                 f"         + ((i%200)||' minutes')::interval,"
                 f"         '2024-06-10'::timestamp"
                 f"         + (((i%200)+30)||' minutes')::interval) "
                 f"FROM generate_series({n}+1,{n}+3000) i"],
                ["UPDATE gr SET r = int4range(lower(r)+1, upper(r)+1) "
                 "WHERE id % 8 = 0"],
                ["DELETE FROM gr WHERE id % 15 = 0"],
                ["VACUUM gr"],
            ]),
        # ------------------------------------------- GiST trgm (KNN + sim)
        dict(
            name="gist_trgm", ext="pg_trgm",
            setup=[
                "CREATE EXTENSION pg_trgm",
                "CREATE TABLE gtr (id int PRIMARY KEY, s text)",
                f"INSERT INTO gtr SELECT i, "
                f"CASE WHEN i%5=0 THEN 'hello'||i "
                f"WHEN i%5=1 THEN 'shells'||i "
                f"WHEN i%5=2 THEN 'hollow'||i "
                f"ELSE md5(i::text) END FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("gist_trgm",
                 ["CREATE INDEX gtr_g ON gtr USING gist(s gist_trgm_ops)"]),
            ],
            queries=[
                # KNN: total-order tiebreak `, id` -> deterministic LIMIT
                ("knn20",
                 "SELECT id, s <-> 'hellow' AS d FROM gtr "
                 "ORDER BY s <-> 'hellow', id LIMIT 20", True),
                ("knn50",
                 "SELECT id FROM gtr ORDER BY s <-> 'shell', id "
                 "LIMIT 50", True),
                ("similar", "SELECT id FROM gtr WHERE s % 'hello'", False),
            ],
            mutations=[
                [f"INSERT INTO gtr SELECT i,'hellq'||i "
                 f"FROM generate_series({n}+1,{n}+2000) i"],
                ["UPDATE gtr SET s='zhq'||id WHERE id % 7 = 0"],
                ["VACUUM gtr"],
                ["DELETE FROM gtr WHERE id % 13 = 0"],
            ]),
        # --------------------------------------------------- GiST ltree
        dict(
            name="gist_ltree", ext="ltree",
            setup=[
                "CREATE EXTENSION ltree",
                "CREATE TABLE gl (id int PRIMARY KEY, p ltree)",
                f"INSERT INTO gl SELECT i, "
                f"('n'||(i%10)||'.n'||(i%50)||'.n'||i%200)::ltree "
                f"FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("gist_ltree", ["CREATE INDEX gl_g ON gl USING gist(p)"]),
            ],
            queries=[
                ("lt_anc", "SELECT id FROM gl WHERE p <@ 'n3.n7'", False),
                ("lt_desc", "SELECT id FROM gl WHERE p @> 'n3'", False),
                ("lt_lquery", "SELECT id FROM gl WHERE p ~ 'n3.n7.*'", False),
            ],
            mutations=[
                [f"INSERT INTO gl SELECT i, ('n3.n7.m'||i)::ltree "
                 f"FROM generate_series({n}+1,{n}+1500) i"],
                ["DELETE FROM gl WHERE id % 9 = 0", "VACUUM gl"],
            ]),
        # --------------------------------------------- GiST btree_gist int
        dict(
            name="gist_btg", ext="btree_gist",
            setup=[
                "CREATE EXTENSION btree_gist",
                "CREATE TABLE gb (id int PRIMARY KEY, a int, s text)",
                f"INSERT INTO gb SELECT i, i%997, md5(i::text) "
                f"FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("gist_int", ["CREATE INDEX gb_g ON gb USING gist(a)"]),
                ("gist_text", ["CREATE INDEX gb_g ON gb USING gist(s)"]),
            ],
            queries=[
                ("int_eq", "SELECT id FROM gb WHERE a = 42", False),
                ("int_lt", "SELECT id FROM gb WHERE a < 50", False),
                ("text_eq",
                 "SELECT id FROM gb WHERE s = 'no_such_value'", False),
            ],
            mutations=[
                [f"INSERT INTO gb SELECT i, 42, 'x'||i "
                 f"FROM generate_series({n}+1,{n}+1000) i"],
                ["DELETE FROM gb WHERE a = 42 AND id % 3 = 0",
                 "VACUUM gb"],
            ]),
        # -------------------------------------------------- SP-GiST text
        dict(
            name="spgist_text", ext=None,
            setup=[
                "CREATE TABLE st (id int PRIMARY KEY, s text)",
                f"INSERT INTO st SELECT i, "
                f"CASE WHEN i%3=0 THEN 'pre_'||i%500 "
                f"WHEN i%3=1 THEN 'pre_deep_'||i%50 "
                f"ELSE md5(i::text) END FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("spg_text", ["CREATE INDEX st_g ON st USING spgist(s)"]),
            ],
            queries=[
                ("prefix_like", "SELECT id FROM st WHERE s LIKE 'pre_%'",
                 False),
                ("prefix_like2",
                 "SELECT id FROM st WHERE s LIKE 'pre_deep%'", False),
                ("starts_with", "SELECT id FROM st WHERE s ^@ 'pre_'",
                 False),
                ("txt_eq", "SELECT id FROM st WHERE s = 'pre_42'", False),
            ],
            mutations=[
                [f"INSERT INTO st SELECT i,'pre_new_'||i "
                 f"FROM generate_series({n}+1,{n}+2000) i"],
                ["UPDATE st SET s='zzz'||id WHERE id % 6 = 0"],
                ["DELETE FROM st WHERE s LIKE 'pre_deep%' AND id % 5 = 0",
                 "VACUUM st"],
            ]),
        # ------------------------------------------------- SP-GiST range
        dict(
            name="spgist_range", ext=None,
            setup=[
                "CREATE TABLE sr (id int PRIMARY KEY, r int4range)",
                f"INSERT INTO sr SELECT i, int4range(i%400, i%400+(i%15)+1)"
                f" FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("spg_range", ["CREATE INDEX sr_g ON sr USING spgist(r)"]),
            ],
            queries=[
                ("sr_overlap",
                 "SELECT id FROM sr WHERE r && '[20,80)'::int4range", False),
                ("sr_contains", "SELECT id FROM sr WHERE r @> 37", False),
                ("sr_right",
                 "SELECT id FROM sr WHERE r >> '[390,395)'::int4range",
                 False),
            ],
            mutations=[
                [f"INSERT INTO sr SELECT i, int4range(i%400,i%400+8) "
                 f"FROM generate_series({n}+1,{n}+1500) i"],
                ["VACUUM sr", "DELETE FROM sr WHERE id % 7 = 0"],
            ]),
        # -------------------------------------------------- SP-GiST point
        dict(
            name="spgist_point", ext=None,
            setup=[
                "CREATE TABLE sp (id int PRIMARY KEY, p point)",
                f"INSERT INTO sp SELECT i, point(i%100, (i*7)%100) "
                f"FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("spg_point", ["CREATE INDEX sp_g ON sp USING spgist(p)"]),
            ],
            queries=[
                ("pt_box",
                 "SELECT id FROM sp WHERE p <@ box '((10,10),(40,40))'",
                 False),
                ("pt_left",
                 "SELECT id FROM sp WHERE p <<| point '(50,0)'", False),
                ("pt_same",
                 "SELECT id FROM sp WHERE p ~= point '(42,42)'", False),
            ],
            mutations=[
                [f"INSERT INTO sp SELECT i, point(i%100,(i*3)%100) "
                 f"FROM generate_series({n}+1,{n}+1500) i"],
                ["VACUUM sp"],
            ]),
        # --------------------------------------------------------- BRIN
        dict(
            name="brin", ext=None,
            setup=[
                "CREATE TABLE br (id int PRIMARY KEY, a int, n numeric, "
                "ts timestamp)",
                # sorted-ish data so minmax summaries are tight
                f"INSERT INTO br SELECT i, i + (i%7), "
                f"(i*1.5)::numeric(12,2), "
                f"'2024-01-01'::timestamp + (i||' minutes')::interval "
                f"FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("brin_p1",
                 ["CREATE INDEX br_b ON br USING brin(a) "
                  "WITH (pages_per_range = 1)"]),
                ("brin_p128",
                 ["CREATE INDEX br_b ON br USING brin(a) "
                  "WITH (pages_per_range = 128)"]),
                ("brin_num",
                 ["CREATE INDEX br_b ON br USING brin(n)"]),
                ("brin_ts",
                 ["CREATE INDEX br_b ON br USING brin(ts)"]),
                # parallel build (PG17+); GUCs exist everywhere, the
                # planner simply ignores them on older builds
                ("brin_par",
                 ["SET max_parallel_maintenance_workers = 2",
                  "SET min_parallel_table_scan_size = 0",
                  "SET maintenance_work_mem = '64MB'",
                  "CREATE INDEX br_b ON br USING brin(a)",
                  "RESET max_parallel_maintenance_workers",
                  "RESET min_parallel_table_scan_size",
                  "RESET maintenance_work_mem"]),
            ],
            queries=[
                ("int_band",
                 "SELECT id FROM br WHERE a BETWEEN 2000 AND 3500", False),
                ("int_lt", "SELECT id FROM br WHERE a < 800", False),
                ("num_band",
                 "SELECT id FROM br WHERE n BETWEEN 3000 AND 4500", False),
                ("ts_band",
                 "SELECT id FROM br WHERE ts >= '2024-01-03'::timestamp "
                 "AND ts < '2024-01-04'::timestamp", False),
            ],
            mutations=[
                # unsummarized tail: inserts AFTER build, no summarize
                [f"INSERT INTO br SELECT i, i+5, (i*1.5)::numeric(12,2),"
                 f" '2024-01-01'::timestamp+(i||' minutes')::interval "
                 f"FROM generate_series({n}+1,{n}+4000) i"],
                # explicitly summarize the new tail, then scan
                [f"SELECT brin_summarize_new_values('br')"],
                # desummarize a middle range -> stale-range coverage path
                ["SELECT brin_desummarize_range('br_b', 10)"],
                [f"SELECT brin_summarize_range('br_b', 10)"],
                ["UPDATE br SET a = a + 1 WHERE id % 9 = 0", "VACUUM br"],
                ["DELETE FROM br WHERE id % 11 = 0"],
            ]),
        # --------------------------------------------------------- hash
        dict(
            name="hash", ext=None,
            setup=[
                "CREATE TABLE ha (id int PRIMARY KEY, a int, s text)",
                f"INSERT INTO ha SELECT i, i%{pmod}, 'k'||(i%900) "
                f"FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("hash_int", ["CREATE INDEX ha_h ON ha USING hash(a)"]),
                ("hash_text", ["CREATE INDEX ha_h ON ha USING hash(s)"]),
            ],
            queries=[
                ("h_eq", "SELECT id FROM ha WHERE a = 777", False),
                ("hs_eq", "SELECT id FROM ha WHERE s = 'k42'", False),
            ],
            mutations=[
                [f"INSERT INTO ha SELECT i, 777, 'k42' "
                 f"FROM generate_series({n}+1,{n}+1500) i"],
                ["DELETE FROM ha WHERE a = 777 AND id % 2 = 0",
                 "VACUUM ha"],
                [f"INSERT INTO ha SELECT i, i%{pmod}, 'k42' "
                 f"FROM generate_series({n}+1501,{n}+2500) i"],
            ]),
        # -------------------------------------------------------- bloom
        dict(
            name="bloom", ext="bloom",
            setup=[
                "CREATE EXTENSION bloom",
                "CREATE TABLE bl (id int PRIMARY KEY, a int, b int, "
                "c text)",
                f"INSERT INTO bl SELECT i, i%500, (i*3)%700, 'w'||(i%300) "
                f"FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("bloom2",
                 ["CREATE INDEX bl_x ON bl USING bloom(a, b)"]),
                ("bloom3",
                 ["CREATE INDEX bl_x ON bl USING bloom(a, b, c)"]),
            ],
            queries=[
                ("bl_2col",
                 "SELECT id FROM bl WHERE a = 17 AND b = 51", False),
                ("bl_3col",
                 "SELECT id FROM bl WHERE a = 17 AND b = 51 "
                 "AND c = 'w17'", False),
                ("bl_1col", "SELECT id FROM bl WHERE a = 100", False),
            ],
            mutations=[
                [f"INSERT INTO bl SELECT i, 17, 51, 'w17' "
                 f"FROM generate_series({n}+1,{n}+800) i"],
                ["DELETE FROM bl WHERE a = 17 AND id % 2 = 0"],
                ["VACUUM bl"],
            ]),
        # --------------------------------------------- GiST cube (gated)
        dict(
            name="gist_cube", ext="cube",
            setup=[
                "CREATE EXTENSION cube",
                "CREATE TABLE cu (id int PRIMARY KEY, c cube)",
                f"INSERT INTO cu SELECT i, cube(ARRAY[i%50,(i*3)%50,"
                f"(i*7)%50]::float8[]) FROM generate_series(1,{n}) i",
            ],
            indexes=[
                ("gist_cube", ["CREATE INDEX cu_g ON cu USING gist(c)"]),
            ],
            queries=[
                ("cube_cont",
                 "SELECT id FROM cu WHERE c @> cube(ARRAY[10,10,10]::"
                 "float8[])", False),
                ("cube_ov",
                 "SELECT id FROM cu WHERE c && cube(ARRAY[5,5,5]::"
                 "float8[], ARRAY[15,15,15]::float8[])", False),
            ],
            mutations=[
                [f"INSERT INTO cu SELECT i, cube(ARRAY[i%50,9,9]::float8[])"
                 f" FROM generate_series({n}+1,{n}+1000) i"],
                ["VACUUM cu"],
            ]),
    ]


# ------------------------------------------------------------------- driver
def _plan_text(res) -> str:
    return " ".join(str(r) for r in res.rows)


def run_family(pg: PostgresRunner, fam: dict, rng: random.Random,
               timeout: float) -> list[dict]:
    findings: list[dict] = []
    stats = {"ok": 0, "hit": 0, "error": 0, "not_fired": 0,
             "truth_err": 0}
    fam["_stats"] = stats

    out = pg.setup(fam["setup"])
    for stmt, err in out:
        if err:
            findings.append({"family": fam["name"], "kind": "setup_fail",
                             "stmt": stmt[:200], "error": err[:400]})
            return findings

    for vlabel, ddls in fam["indexes"]:
        # fresh index variant on the same table; index name from whichever
        # ddl carries the CREATE INDEX (pre-ddl GUC SETs may precede it)
        import re as _re
        idxname = "?"
        for d in ddls:
            m = _re.search(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(\w+)", d)
            if m:
                idxname = m.group(1)
                break
        pg.run(f"DROP INDEX IF EXISTS {idxname}", timeout_s=timeout)
        ok = True
        for d in ddls:
            r = pg.run(d, timeout_s=timeout)
            if not r.ok:
                findings.append({"family": fam["name"], "kind": "setup_fail",
                                 "variant": vlabel, "stmt": d[:200],
                                 "error": (r.error or "")[:400]})
                ok = False
                break
        if not ok:
            continue

        steps = len(fam["mutations"]) + 1  # round 0 = freshly built index
        for step in range(steps):
            for qlabel, sql, ordered in fam["queries"]:
                # --- truth: forced sequential scan
                for g in TRUTH_GUCS:
                    pg.run(g, timeout_s=timeout)
                truth = pg.run(sql, timeout_s=timeout)
                for g in RESET_GUCS:
                    pg.run(g, timeout_s=timeout)
                if truth.is_internal_error:
                    findings.append({"family": fam["name"], "kind": "error",
                                     "variant": vlabel, "step": step,
                                     "query": qlabel, "side": "truth",
                                     "error": (truth.error or "")[:500],
                                     "sql": sql[:300]})
                    stats["error"] += 1
                    continue
                if not truth.ok:
                    stats["truth_err"] += 1
                    continue
                # --- gate: does the index path actually fire?
                for g in TEST_GUCS:
                    pg.run(g, timeout_s=timeout)
                ex = pg.run(f"EXPLAIN {sql}", timeout_s=timeout)
                plan = _plan_text(ex)
                if idxname not in plan:
                    for g in RESET_GUCS:
                        pg.run(g, timeout_s=timeout)
                    stats["not_fired"] += 1
                    continue
                test = pg.run(sql, timeout_s=timeout)
                for g in RESET_GUCS:
                    pg.run(g, timeout_s=timeout)
                if test.is_internal_error:
                    findings.append({"family": fam["name"], "kind": "error",
                                     "variant": vlabel, "step": step,
                                     "query": qlabel, "side": "test",
                                     "error": (test.error or "")[:500],
                                     "sql": sql[:300]})
                    stats["error"] += 1
                    continue
                if not test.ok:
                    # index path errored where seqscan succeeded -> suspect
                    findings.append({"family": fam["name"], "kind": "hit",
                                     "subtype": "test_error",
                                     "variant": vlabel, "step": step,
                                     "query": qlabel,
                                     "error": (test.error or "")[:500],
                                     "sql": sql[:300],
                                     "truth_rows": len(truth.rows)})
                    stats["hit"] += 1
                    continue
                same = (test.rows == truth.rows) if ordered else \
                    (test.bag() == truth.bag())
                if not same:
                    tb, qb = truth.bag(), test.bag()
                    missing = list((tb - qb).elements())[:6]
                    extra = list((qb - tb).elements())[:6]
                    findings.append({
                        "family": fam["name"], "kind": "hit",
                        "subtype": "bag_diff", "variant": vlabel,
                        "step": step, "query": qlabel, "sql": sql[:300],
                        "truth_rows": len(truth.rows),
                        "test_rows": len(test.rows),
                        "missing": [str(x)[:200] for x in missing],
                        "extra": [str(x)[:200] for x in extra],
                        "plan": plan[:400]})
                    stats["hit"] += 1
                else:
                    stats["ok"] += 1
            # --- mutate storage state between scan pairs
            if step < len(fam["mutations"]):
                for m in fam["mutations"][step]:
                    r = pg.run(m, timeout_s=timeout)
                    if r.is_internal_error:
                        findings.append({"family": fam["name"],
                                         "kind": "error",
                                         "variant": vlabel, "step": step,
                                         "side": "mutation",
                                         "stmt": m[:200],
                                         "error": (r.error or "")[:500]})
                        stats["error"] += 1
                # crash signatures visible only in the server log
                fat = pg.log_fatal_lines(pg.log_new_lines())
                for ln in fat:
                    findings.append({"family": fam["name"], "kind": "error",
                                     "variant": vlabel, "step": step,
                                     "side": "server_log",
                                     "error": ln[:500]})
                    stats["error"] += 1
    return findings


def ext_available(prefix: str, ext: str) -> bool:
    p = Path(prefix) / "share" / "postgresql" / "extension" / \
        f"{ext}.control"
    return p.exists()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--out", default="results/pg_am_diff")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--families", default="")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    tag = Path(args.prefix).name
    pg = PostgresRunner(tempfile.mkdtemp(prefix="amdiff_"),
                        pg_prefix=args.prefix,
                        statement_timeout_ms=int(args.timeout * 1000))
    findings: list[dict] = []
    started = time.time()
    try:
        ver = pg.engine_version
        print(f"build={tag} version={ver}", flush=True)
        wanted = {x.strip() for x in args.families.split(",") if x.strip()}
        fam_stats = {}
        for fam in families(rng, args.scale):
            if wanted and fam["name"] not in wanted:
                continue
            if fam["ext"] and not ext_available(args.prefix, fam["ext"]):
                print(f"[{fam['name']}] skip: {fam['ext']} not installed",
                      flush=True)
                fam_stats[fam["name"]] = {"skipped": f"no {fam['ext']}"}
                continue
            t0 = time.time()
            f = run_family(pg, fam, rng, args.timeout)
            findings.extend(f)
            s = fam["_stats"]
            fam_stats[fam["name"]] = s
            print(f"[{fam['name']}] {time.time()-t0:5.1f}s "
                  f"ok={s['ok']} hit={s['hit']} err={s['error']} "
                  f"not_fired={s['not_fired']} truth_err={s['truth_err']} "
                  f"findings={len(f)}", flush=True)
        summary = {
            "build": tag, "version": ver, "seed": args.seed,
            "scale": args.scale, "elapsed_s": round(time.time()-started, 1),
            "families": fam_stats,
            "findings": findings,
        }
        (out / f"findings_{tag}.json").write_text(
            json.dumps(summary, indent=1, default=str))
        print(f"\nDONE {tag}: {len(findings)} findings, "
              f"{time.time()-started:.0f}s -> {out}/findings_{tag}.json")
        return 1 if findings else 0
    finally:
        pg.cleanup()


if __name__ == "__main__":
    sys.exit(main())
