#!/usr/bin/env python3
"""Cold-start PG explorer: coverage-conditioned generation + sound oracles.

No known-bug seeds and no sigma yet — the search is guided by novelty
pressure over (feature, plan-op, verdict) cells. Cheap sound oracles
(PlanVariant GUC diffs, TLP partitioning, optional cross-version diff)
produce the first verdicts; any non-clean verdict is a candidate whose
sigma hands the campaign off to the warm (diagnostic) regime.

  python scripts/pg_coldstart.py \
      --prefix /p/pg166_assert --rounds 400 [--prefix-b /p/pg186_assert]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dataclasses import asdict, is_dataclass

from evolution.coverage import CoverageTracker, feature_tags, plan_ops
from oracles.db_runner import QueryResult
from oracles.plan_variant import PlanVariantOracle, POSTGRES_VARIANTS
from oracles.tlp import TLPOracle, tlp_applicable
from targets.postgres_runner import PostgresRunner


def _rng_const(rng: random.Random) -> int:
    return rng.choice([0, 1, -1, 2, 10, 42, 100, 1000, 5000, 14653, 15000,
                       32767, 65535, 2147483647, -2147483648,
                       rng.randint(-100, 100)])


# Type-boundary constants: value-consistent overflow/truncation bugs live
# at type edges — the generator must be able to *reach* them for novelty
# pressure to matter.
_EDGE_CONSTS = [
    "92233720368547758.07", "92233720368547758.08",
    "-92233720368547758.08", "-9223372036854775808",
    "9223372036854775807", "2147483647", "-2147483648",
    "99999999999999999999", "1e308", "-1e308",
    "0.000000001", "Infinity", "-Infinity", "NaN",
]


def _pred(rng: random.Random, col: str) -> str:
    c = _rng_const(rng)
    return rng.choice([
        f"{col} < {c}", f"{col} >= {c}", f"{col} = {c}",
        f"{col} <> {c}", f"{col} is null", f"{col} is not null",
        f"{col} in ({c}, {c+1}, {c+10})",
        f"{col} not in ({c}, {c+1})",
        f"{col} between {c-5} and {c+5}",
        f"({col} < {c} or {col} > {c+50})",
        f"{col} is not distinct from {c}",
    ])


# Each family: setup once per activation; gen(rng) -> dict with either
#   {"query": sql}                                  (run + plan-variant)
#   {"select_from": sql, "predicate": sql}          (TLP-eligible)
FAMILIES = {
    "part_prune": {
        "setup": [
            "create table pp (k int, v int) partition by range (k)",
            "create table pp0 partition of pp for values from (minvalue) to (0)",
            "create table pp1 partition of pp for values from (0) to (10)",
            "create table pp2 partition of pp for values from (10) to (100)",
            "create table ppd partition of pp default",
            "insert into pp select i, i*2 from generate_series(-20, 150) i",
            "create table pb (b bool, v int) partition by list (b)",
            "create table pb_t partition of pb for values in (true)",
            "create table pb_f partition of pb for values in (false)",
            "create table pb_n partition of pb default",
            "insert into pb select (i % 3 = 0), i from generate_series(1,60) i",
            "insert into pb values (null, 999)",
        ],
        "gen": lambda r: {
            "select_from": r.choice([
                "select k, v from pp", "select * from pb"]),
            "predicate": _pred(r, r.choice(["k", "v", "b"])),
        },
    },
    "window_frame": {
        "setup": [
            "create table emp (dep text, id int, sal int)",
            "insert into emp select 'd'|| (i%4), i, (i*37)%500 "
            "from generate_series(1, 60) i",
        ],
        "gen": lambda r: {"query": (
            f"select id, {r.choice(['count(*)','sum(sal)','rank()','row_number()'])}"
            f" over (order by sal "
            f"{r.choice(['rows','range','groups'])} between "
            f"{r.choice(['unbounded preceding','current row','1 preceding'])} and "
            f"{r.choice(['current row','unbounded following','2 following'])}"
            f"{r.choice(['',' exclude current row',' exclude group',' exclude ties'])})"
            f" from emp order by id, sal"
        )},
    },
    "join_null": {
        "setup": [
            "create table ja (x int, av int)",
            "create table jb (x int, bv int)",
            "insert into ja values (1,10),(2,20),(null,30),(null,40),(5,50)",
            "insert into jb values (1,100),(null,300),(2,200),(7,700),(null,900)",
        ],
        "gen": lambda r: {
            "select_from": r.choice([
                "select av, bv from ja join jb on ja.x = jb.x",
                "select av, bv from ja left join jb on ja.x = jb.x",
                "select av, bv from ja full join jb on ja.x = jb.x",
                "select av, bv from ja left join jb on ja.x is not distinct from jb.x",
            ]),
            "predicate": _pred(r, r.choice(["av", "bv"])),
        },
    },
    "subq_in": {
        "setup": [
            "create table sa (x int)", "create table sb (y int)",
            "insert into sa values (1),(2),(null),(4),(5)",
            "insert into sb select case when i%7=0 then null else i end "
            "from generate_series(1,20) i",
        ],
        "gen": lambda r: {
            "select_from": "select x from sa",
            "predicate": r.choice([
                f"x in (select y from sb where y < {_rng_const(r)})",
                f"x not in (select y from sb where y < {_rng_const(r)})",
                f"exists (select 1 from sb where y = sa.x)",
                f"not exists (select 1 from sb where y = sa.x)",
                f"x = any (select y from sb)",
                f"x <> all (select y from sb where y is not null)",
            ]),
        },
    },
    "agg_group": {
        "setup": [
            "create table ga (g int, v numeric(8,3), t text)",
            "insert into ga select i%7, (i*1.5)::numeric(8,3), 's'||i%5 "
            "from generate_series(1,80) i",
        ],
        "gen": lambda r: {"query": r.choice([
            f"select g, count(*), sum(v) from ga group by g having count(*) > {_rng_const(r)%5}",
            f"select distinct g from ga where {_pred(r,'v')}",
            f"select g, sum(v) from ga where {_pred(r,'v')} group by g order by 1",
            f"select count(distinct t), avg(v) from ga group by g%2",
        ])},
    },
    "lateral": {
        "setup": [
            "create table la (i int)",
            "insert into la select generate_series(1,5)",
        ],
        "gen": lambda r: {"query": r.choice([
            "select * from la a, lateral (select a.i as i union all "
            "select a.i+10) s",
            "select a.i, s.j from la a left join lateral "
            "(select a.i+1 as j where a.i is not null) s on true",
            f"select * from la a cross join lateral (select * from la b "
            f"where b.i = a.i + {_rng_const(r)%3}) s",
        ])},
    },
    "ltree_ext": {
        "setup": ["create extension if not exists ltree"],
        "gen": lambda r: {"query": r.choice([
            f"select (repeat('a.',{_rng_const(r)})||'a')::ltree > 'a'::ltree",
            f"select (repeat('x.',{_rng_const(r)})||'x')::ltree ~ '*.x'",
            "select 'a.b.c'::ltree <@ 'a.b'::ltree",
        ])},
    },
    "dml_edge": {
        "setup": [
            "create table dt (id int primary key, v int, g int generated always as (v*2) stored)",
            "insert into dt select i, i from generate_series(1,30) i",
        ],
        # DML is stateful: {"dml", "probe"} runs each variant inside
        # begin/rollback so variants observe identical pre-state.
        "gen": lambda r: {"dml": r.choice([
            f"insert into dt values ({_rng_const(r)}, {_rng_const(r)}) "
            f"on conflict (id) do update set v = excluded.v + 1 returning *",
            f"update dt set v = v + {_rng_const(r)%10} "
            f"where id = {_rng_const(r)%30} returning id, v, g",
            f"delete from dt where id > {_rng_const(r)%40} returning id",
            f"merge into dt using (select {_rng_const(r)%35} as k) s "
            f"on dt.id = s.k when matched then update set v = dt.v + 1 "
            f"when not matched then insert values (s.k, 0)",
        ]), "probe": "select id, v, g from dt order by id"},
    },
    "numeric_edge": {
        "setup": [],
        "gen": lambda r: {"query": r.choice([
            f"select ({_rng_const(r)}.{r.randint(0,999)})::numeric({_rng_const(r)%6+1},{_rng_const(r)%4})",
            f"select {_rng_const(r)}::numeric / {_rng_const(r)%9}",
            f"select trunc({_rng_const(r)}.{r.randint(0,9999)}::numeric, {r.randint(-2,4)})",
            f"select '{r.choice(_EDGE_CONSTS)}'::money "
            f"{r.choice(['+','-','*','/'])} "
            f"'{r.choice(_EDGE_CONSTS + ['1','0.01','-1','100'])}'::money",
            f"select '{r.choice(_EDGE_CONSTS)}'::numeric "
            f"{r.choice(['+','-','*','/','%'])} '{r.choice(_EDGE_CONSTS)}'::numeric",
            f"select '{r.choice(_EDGE_CONSTS)}'::float8",
            f"select round('{r.choice(_EDGE_CONSTS)}'::numeric, {r.randint(-3,40)})",
            # scale beyond the +/-2000 clamp window
            f"select (trunc(1 + 1e-{r.choice([1999,2000,2001,2500,3000])}::numeric, "
            f"{r.choice([1999,2000,2001,2500,3000])}) <> "
            f"trunc(1 + 1e-2500::numeric, 2000))::int",
        ])},
    },
    "cte_mat": {
        "setup": [
            "create table ct (i int)",
            "insert into ct select generate_series(1,40)",
        ],
        "gen": lambda r: {"query": r.choice([
            f"with w as {'materialized ' if r.random()<.5 else 'not materialized '}"
            f"(select * from ct where {_pred(r,'i')}) select count(*) from w",
            f"with w as (select i from ct) select a.i, b.i from w a, w b "
            f"where a.i = b.i + {_rng_const(r)%5}",
        ])},
    },
    "grouping_sets": {
        "setup": [
            "create table gs (a int, b int, v numeric(10,2))",
            "insert into gs select i%3, i%5, (i*1.7)::numeric(10,2) "
            "from generate_series(1,60) i",
        ],
        "gen": lambda r: {"query": r.choice([
            "select a, b, count(*), grouping(a, b) from gs "
            "group by rollup(a, b) order by 1, 2, 3",
            "select a, b, sum(v) from gs group by cube(a, b) order by 1, 2",
            "select a, count(distinct b) from gs group by grouping sets "
            "((a), (b), ()) order by 1",
            f"select count(distinct v) filter (where v > {_rng_const(r)}) "
            f"from gs group by a order by 1",
        ])},
    },
    "temporal_edge": {
        "setup": [],
        "gen": lambda r: {"query": r.choice([
            f"select date_bin('{r.choice([-2,-1,1,2])} hours', "
            f"'2001-02-16 20:38:40+00'::timestamptz, "
            f"'2001-02-16 19:05:00+00'::timestamptz)",
            f"select '2001-02-16 20:38:40+00'::timestamptz + "
            f"'{r.choice(_EDGE_CONSTS[:9])} microseconds'::interval",
            f"select age('2001-02-16'::timestamp, '1970-01-01'::timestamp)",
            f"select extract(epoch from '{r.choice([14653,65535,2147483647])} "
            f"seconds'::interval)",
            f"select 'infinity'::timestamptz {r.choice(['+','-'])} "
            f"'{_rng_const(r)} seconds'::interval",
        ])},
    },
    "contrib_ops": {
        "setup": [
            "create extension if not exists intarray",
            "create extension if not exists pg_trgm",
            "create extension if not exists citext",
            "create extension if not exists hstore",
            "create extension if not exists btree_gist",
        ],
        "gen": lambda r: {"query": r.choice([
            f"select '{{1,{_rng_const(r)},{_rng_const(r)}}}'::int[] &&"
            f" '{{{_rng_const(r)}}}'::int[]",
            f"select 'abc{r.randint(0,9)}' % 'abc' ",
            f"select 'AbC{r.randint(0,9)}'::citext = 'abc{r.randint(0,9)}'",
            f"select 'a=>{r.randint(0,9)}, b=>x'::hstore -> 'a'",
            f"select '[{r.randint(0,5)},{r.randint(6,12)}]'::int4range <@ "
            f"'[0,{_rng_const(r)}]'::int4range",
        ])},
    },
    "case_typing": {
        "setup": [
            "create table tc (i int, t text, d date)",
            "insert into tc select i, 'x'||i, '2001-01-01'::date + i "
            "from generate_series(1,20) i",
        ],
        "gen": lambda r: {"query": r.choice([
            f"select case when {_pred(r,'i')} then i else -i end from tc",
            f"select coalesce(null, {r.choice(_EDGE_CONSTS[:9])}::numeric, i) from tc",
            f"select nullif(i, {_rng_const(r)}), nullif(t, 'x5') from tc",
            f"select case when i > 0 then d else null end + '{_rng_const(r)%30} days'::interval from tc",
        ])},
    },
    "dml_from_select": {
        "setup": [
            "create table dfs (id int, v int, primary key (id))",
            "create table dft (id int, v2 int)",
            "insert into dfs select i, i*3 from generate_series(1,25) i",
            "insert into dft select i, i*7 from generate_series(1,25) i",
        ],
        "gen": lambda r: {"dml": r.choice([
            f"insert into dfs select i+{_rng_const(r)}, i from generate_series(1,10) i "
            f"on conflict (id) do nothing returning *",
            f"update dfs set v = dft.v2 from dft where dfs.id = dft.id "
            f"and dft.id < {_rng_const(r)} returning dfs.id, dfs.v",
            f"delete from dfs using dft where dfs.id = dft.id and "
            f"dft.v2 > {_rng_const(r)} returning dfs.id",
            f"insert into dfs (id, v) select id, v2 from dft where "
            f"{_pred(r,'v2')} on conflict (id) do update set v = excluded.v returning *",
        ]), "probe": "select id, v from dfs order by id"},
    },
}


def verdict_of(res) -> str:
    if res.is_internal_error:
        return "crash"
    if res.timed_out:
        return "timeout"
    if not res.ok:
        return "error"
    return "clean"


def explain_ops(pg: PostgresRunner, sql: str) -> frozenset[str]:
    r = pg.run(f"explain (costs off) {sql}", timeout_s=10)
    if not r.ok:
        return frozenset()
    return plan_ops(row[0] for row in r.rows if row and row[0])


def dml_variant_check(pg: PostgresRunner, dml: str, probe: str,
                      variants, timeout_s: float):
    """Run (dml, probe) inside begin/rollback under each GUC variant.

    Rollback restores pre-state, so all variants observe the same input.
    Returns (label, dml_res, probe_res) triples — identical across variants
    on a correct engine.
    """
    outs = []
    for label, setup_stmts, teardown_stmts in variants:
        pg.run("begin", timeout_s=timeout_s)
        setup_err = None
        for s in setup_stmts:
            r = pg.run(s, timeout_s=timeout_s)
            if not r.ok:
                setup_err = r.error
                break
        if setup_err is not None:
            # A failed in-txn SET aborts the txn; the driver then silently
            # autocommits the DML — record setup failure, not divergence.
            pg.run("rollback", timeout_s=timeout_s)
            r_bad = QueryResult()
            r_bad.error = f"setup_fail: {setup_err}"
            outs.append((label, r_bad, r_bad))
            continue
        r_dml = pg.run(dml, timeout_s=timeout_s)
        r_probe = pg.run(probe, timeout_s=timeout_s)
        for s in teardown_stmts:
            pg.run(s, timeout_s=timeout_s)
        pg.run("rollback", timeout_s=timeout_s)
        outs.append((label, r_dml, r_probe))
    return outs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--prefix-b", default="", help="optional 2nd build diff")
    ap.add_argument("--rounds", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--full-variants", action="store_true")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)

    cheap = [v for v in POSTGRES_VARIANTS if v[0] in (
        "collapse_1", "no_seqscan", "no_index", "no_memoize",
        "no_hashjoin", "no_part_prune")]
    pv = PlanVariantOracle("postgres",
                           variants=POSTGRES_VARIANTS if args.full_variants
                           else cheap)
    tlp = TLPOracle()
    tracker = CoverageTracker()

    pg = PostgresRunner(tempfile.mkdtemp(prefix="cold_a_"),
                        pg_prefix=args.prefix)
    pg_b = PostgresRunner(tempfile.mkdtemp(prefix="cold_b_"),
                          pg_prefix=args.prefix_b) if args.prefix_b else None

    candidates: list[dict] = []
    coverage_curve: list[dict] = []
    current_family = None

    def emit(cand, family):
        d = asdict(cand) if is_dataclass(cand) else dict(cand)
        d["family"] = family
        candidates.append(d)
        print(f"  CANDIDATE {d.get('kind')} {d.get('id')} [{family}]")

    try:
        for step in range(args.rounds):
            # novelty-weighted family choice: least-seen families win
            fam_name = min(
                FAMILIES, key=lambda f: tracker.feat_cells[f] + rng.random())
            if fam_name != current_family:
                pg.setup(FAMILIES[fam_name]["setup"])
                if pg_b:
                    pg_b.setup(FAMILIES[fam_name]["setup"])
                current_family = fam_name
            spec = FAMILIES[fam_name]["gen"](rng)
            sql = spec.get("query") or spec.get("select_from") or spec["dml"]
            ops = explain_ops(pg, sql)
            feats = feature_tags(sql) | {f"family:{fam_name}"}

            # cheap oracles; record coverage with the resulting verdict
            if "select_from" in spec and tlp_applicable(
                    spec["select_from"], spec["predicate"]):
                c = tlp.check(pg, spec["select_from"], spec["predicate"],
                              FAMILIES[fam_name]["setup"], [], fam_name,
                              timeout_s=args.timeout)
                tracker.record(feats, ops, "tlp_hit" if c else "tlp_clean")
                if c:
                    emit(c, fam_name)
            elif "dml" in spec:
                outs = dml_variant_check(pg, spec["dml"], spec["probe"],
                                         pv.variants, args.timeout)
                hit = None
                base = outs[0]
                for label, r_dml, r_probe in outs[1:]:
                    if (r_dml.error or "").startswith("setup_fail"):
                        continue  # variant GUC unsupported here — no signal
                    if r_dml.is_internal_error or r_probe.is_internal_error:
                        hit = (label, "crash")
                        break
                    if (verdict_of(r_dml) != verdict_of(base[1])
                            or (r_probe.ok and base[2].ok
                                and r_probe.rows != base[2].rows)):
                        hit = (label, "state_divergence")
                        break
                tracker.record(feats, ops, "pv_hit" if hit else "pv_clean")
                if hit:
                    emit({"kind": "dml_plan_variant", "id":
                          f"dml-{fam_name}-{step}", "q1": spec["dml"],
                          "notes": f"{hit[0]}: {hit[1]}",
                          "r1_summary": str(base[2].rows)[:200],
                          "r2_summary": str(r_probe.rows)[:200]}, fam_name)
            elif "query" in spec:
                c = pv.check(pg, sql, FAMILIES[fam_name]["setup"], [],
                             fam_name, timeout_s=args.timeout)
                tracker.record(feats, ops, "pv_hit" if c else "pv_clean")
                if c:
                    emit(c, fam_name)
            else:
                tracker.record(feats, ops, "skipped")

            # optional cross-version diff on the primary query / dml
            if pg_b and "query" in spec:
                ra = pg.run(sql, timeout_s=args.timeout)
                rb = pg_b.run(sql, timeout_s=args.timeout)
                va, vb = verdict_of(ra), verdict_of(rb)
                if va != vb or (va == "clean" and vb == "clean"
                               and ra.rows != rb.rows):
                    rec = {"kind": "cross_version", "family": fam_name,
                           "query": sql, "A": va, "B": vb,
                           "A_rows": ra.rows[:10], "B_rows": rb.rows[:10]}
                    candidates.append(rec)
                    print(f"  CANDIDATE cross_version [{fam_name}] {va} vs {vb}")
            elif pg_b and "dml" in spec:
                prows = {}
                for tag, runner in (("A", pg), ("B", pg_b)):
                    runner.run("begin", timeout_s=args.timeout)
                    rd = runner.run(spec["dml"], timeout_s=args.timeout)
                    rp = runner.run(spec["probe"], timeout_s=args.timeout)
                    runner.run("rollback", timeout_s=args.timeout)
                    prows[tag] = (verdict_of(rd), rp.rows if rp.ok else rp.error)
                if prows["A"] != prows["B"]:
                    rec = {"kind": "cross_version_dml", "family": fam_name,
                           "dml": spec["dml"], "A": str(prows["A"])[:300],
                           "B": str(prows["B"])[:300]}
                    candidates.append(rec)
                    print(f"  CANDIDATE cross_version_dml [{fam_name}]")

            if step % 50 == 49:
                coverage_curve.append({"step": step + 1,
                                       **tracker.summary()})
                s = tracker.summary()
                print(f"[{step+1}] feats={s['features_seen']} "
                      f"ops={s['plan_ops_seen']} "
                      f"cells={s['feature_x_op_cells']} "
                      f"cands={len(candidates)}")
    finally:
        pg.cleanup()
        if pg_b:
            pg_b.cleanup()

    with open(os.path.join(args.out, "diffs.jsonl"), "w") as fh:
        for c in candidates:
            fh.write(json.dumps(c, default=str) + "\n")
    summary = {"rounds": args.rounds, "candidates": len(candidates),
               "coverage": tracker.summary(), "curve": coverage_curve}
    with open(os.path.join(args.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    print(json.dumps(summary["coverage"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
