"""Generic probe-matrix runner for PG_LIVE_PROBES-style case lists.

Unlike scripts/pg_recall_check.py this accepts ANY python module exposing a
case list (PG_LIVE_PROBES / CASES / PROBES), so ad-hoc sibling-mutation probe
modules under seeds/ can be run without touching load_cases().

Per case the runner does: pg.setup(setup_sqls) -> [optional expected_sql ->
expected_rows oracle] -> pre_sqls -> query -> classify (reuses
pg_recall_check.classify). With --repeat N the whole matrix is replayed N
times per prefix (for flaky-signature quantification).

Usage:
    python scripts/pg_probe_run.py \
        --module seeds.pg_sibling_19649 \
        --prefixes $COEVO_PGBLD/pg186_assert,\
$COEVO_PGBLD/pgmaster_assert \
        --out results/sibling_sweeps/sweep_a --repeat 1
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner  # noqa: E402
from scripts.pg_recall_check import classify  # noqa: E402


def load_cases(module: str) -> list[dict]:
    mod = importlib.import_module(module)
    for attr in ("PG_LIVE_PROBES", "PROBES", "CASES"):
        cases = getattr(mod, attr, None)
        if cases is not None:
            return cases
    raise AttributeError(f"{module} exposes no PG_LIVE_PROBES/PROBES/CASES")


def run_case(pg: PostgresRunner, case: dict, timeout: float) -> dict:
    setup_out = pg.setup(case["setup_sqls"])
    setup_errs = [e for _, e in setup_out if e]
    # dynamic oracle: expected_sql runs after setup, before pre_sqls
    expected = case.get("expected_rows")
    if "expected_sql" in case:
        er = pg.run(case["expected_sql"], timeout_s=timeout)
        if er.ok:
            expected = er.rows
        else:
            return {"outcome": "oracle_error", "error": er.error,
                    "setup_errors": [str(e)[:160] for e in setup_errs]}
    for pre in case["pre_sqls"]:
        pg.run(pre, timeout_s=timeout)
    res = pg.run(case["query"], timeout_s=timeout)
    eff_case = dict(case)
    if expected is not None:
        eff_case["expected_rows"] = expected
    outcome = classify(eff_case, res)
    # restore default GUCs for the next case on the same session —
    # only for SET-shaped pre_sqls (PREPARE/BEGIN/DEALLOCATE etc. have
    # no GUC key and would only produce noise).
    for pre in case["pre_sqls"]:
        toks = pre.split()
        if len(toks) < 2 or toks[0].upper() not in ("SET", "RESET"):
            continue
        key = toks[1].split("=")[0].split(" to ")[0]
        try:
            pg.run(f"RESET {key}", timeout_s=timeout)
        except Exception:
            pass
    # also scan server log for deaths invisible to the client
    fatal = pg.log_fatal_lines(pg.log_new_lines())
    return {
        "outcome": outcome,
        "rows": (res.rows or [])[:8],
        "error": (res.error or "")[:300] if res.error else None,
        "expected": expected,
        "timed_out": res.timed_out,
        "log_fatal": fatal[:5],
        "setup_errors": [str(e)[:160] for e in setup_errs],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", required=True)
    ap.add_argument("--prefixes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--filter", default=None, help="regex on case name")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    cases = load_cases(args.module)
    if args.filter:
        rx = re.compile(args.filter)
        cases = [c for c in cases if rx.search(c["name"])]

    report = {"module": args.module, "repeat": args.repeat,
              "runs": [], "versions": {}}
    for prefix in prefixes:
        tag = Path(prefix).name
        pg = PostgresRunner(out / f"data_{tag}", pg_prefix=prefix)
        ver = pg.engine_version
        report["versions"][tag] = ver
        for rep in range(args.repeat):
            for case in cases:
                t0 = time.time()
                try:
                    cell = run_case(pg, case, args.timeout)
                except Exception as exc:  # noqa: BLE001
                    cell = {"outcome": "harness_error",
                            "error": f"{type(exc).__name__}: {exc}"}
                    try:
                        pg.connect()
                    except Exception:
                        pass
                cell["elapsed_s"] = round(time.time() - t0, 2)
                report["runs"].append({
                    "prefix": tag, "version": ver, "repeat": rep,
                    "case": case["name"], **cell,
                })
                extra = ""
                if cell["outcome"] not in ("clean",):
                    extra = (f" rows={cell.get('rows')}"
                             f" err={cell.get('error')}")
                print(f"{tag:>18} rep{rep} {case['name']:<46} "
                      f"-> {cell['outcome']}{extra}", flush=True)
        pg.cleanup()

    (out / "probe_run.json").write_text(
        json.dumps(report, indent=2, default=str))
    # aggregate table
    print(f"\n{'case':<46}" + "".join(f"{t:>20}"
                                      for t in report["versions"]))
    by_case: dict[str, dict[str, list[str]]] = {}
    for r in report["runs"]:
        by_case.setdefault(r["case"], {}).setdefault(
            r["prefix"], []).append(r["outcome"])
    for name, per in by_case.items():
        cells = []
        for tag in report["versions"]:
            outs = per.get(tag, [])
            if not outs:
                cells.append("-")
            elif len(set(outs)) == 1:
                cells.append(outs[0])
            else:
                cells.append(str(dict(Counter(outs))))
        print(f"{name:<46}" + "".join(f"{c:>20}" for c in cells))
    print(f"\n-> {out}/probe_run.json")


if __name__ == "__main__":
    main()
