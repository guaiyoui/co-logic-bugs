"""Transformation-equivalence probe runner for planner rewrite arms.

Runs seeds/pg_transform_probes.py-style cases: each case carries a list of
``checks`` using four oracle kinds:

  expected : rows must bag-equal ``expected``
  pair     : q1 and q2 must produce identical bags
  guc      : ``q`` must produce identical bags with ``guc=value`` and default
  no_crash : query must not error/crash (errors recorded verbatim)

Optional per-check ``marker`` verifies via EXPLAIN (COSTS OFF) that the
transformation under test actually fired; a clean-but-not-fired check is
reported as ``ok_notfired`` so vacuous oracles stay visible.

Bag comparison is repr()-keyed multiset equality — NULL ordering and plan-
chosen row order are irrelevant, type fidelity is kept.

Usage:
    python scripts/pg_transform_probes.py \
        --module seeds.pg_transform_probes \
        --prefixes $COEVO_PGBLD/pg186_assert,\
$COEVO_PGBLD/pgmaster_assert \
        --out results/pg_transform_probes/run_a
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner  # noqa: E402


def bag(rows) -> Counter:
    """repr-keyed multiset: exact types, order-insensitive."""
    return Counter(repr(tuple(r)) for r in (rows or []))


def load_cases(module: str) -> list[dict]:
    mod = importlib.import_module(module)
    for attr in ("PG_TRANSFORM_PROBES", "PG_LIVE_PROBES", "PROBES", "CASES"):
        cases = getattr(mod, attr, None)
        if cases is not None:
            return cases
    raise AttributeError(f"{module} exposes no case list")


def plan_text(pg: PostgresRunner, q: str, timeout: float) -> str:
    r = pg.run(f"EXPLAIN (COSTS OFF) {q}", timeout_s=timeout)
    if not r.ok:
        return f"<explain_error: {r.error}>"
    return "\n".join(str(row[0]) for row in r.rows)


def run_check(pg: PostgresRunner, check: dict, timeout: float) -> dict:
    """Execute one check; outcome in {ok, divergent, expected_mismatch,
    crash, timeout, error, ok_notfired}."""
    out = {"kind": check["kind"]}
    fired = None
    marker = check.get("marker")
    if marker is not None:
        q = check.get("q") or check.get("q1")
        pt = plan_text(pg, q, timeout)
        fired = marker in pt
        out["plan_fired"] = fired
        out["plan_head"] = pt.splitlines()[:4]

    def finish(outcome, **kw):
        if fired is False and outcome == "ok":
            outcome = "ok_notfired"
        return {"outcome": outcome, **out, **kw}

    kind = check["kind"]
    if kind == "expected":
        r = pg.run(check["q"], timeout_s=timeout)
        if r.is_internal_error:
            return finish("crash", error=r.error)
        if r.timed_out:
            return finish("timeout", error=r.error)
        if not r.ok:
            return finish("error", error=r.error)
        out["rows"] = (r.rows or [])[:10]
        if bag(r.rows) == bag(check["expected"]):
            return finish("ok")
        return finish("expected_mismatch",
                      expected=check["expected"])

    if kind == "pair":
        r1 = pg.run(check["q1"], timeout_s=timeout)
        r2 = pg.run(check["q2"], timeout_s=timeout)
        for tag, r in (("q1", r1), ("q2", r2)):
            if r.is_internal_error:
                return finish("crash", error=f"{tag}: {r.error}")
            if r.timed_out:
                return finish("timeout", error=f"{tag}: {r.error}")
            if not r.ok:
                return finish("error", error=f"{tag}: {r.error}")
        out["rows1"] = (r1.rows or [])[:10]
        out["rows2"] = (r2.rows or [])[:10]
        if bag(r1.rows) == bag(r2.rows):
            return finish("ok")
        return finish("divergent")

    if kind == "guc":
        g = check["guc"]
        v = check.get("value", "off")
        pg.run(f"SET {g} = {v}", timeout_s=timeout)
        r_on = pg.run(check["q"], timeout_s=timeout)
        pg.run(f"RESET {g}", timeout_s=timeout)
        r_off = pg.run(check["q"], timeout_s=timeout)
        for tag, r in ((f"{g}={v}", r_on), ("default", r_off)):
            if r.is_internal_error:
                return finish("crash", error=f"{tag}: {r.error}")
            if r.timed_out:
                return finish("timeout", error=f"{tag}: {r.error}")
            if not r.ok:
                return finish("error", error=f"{tag}: {r.error}")
        out["rows_on"] = (r_on.rows or [])[:10]
        out["rows_off"] = (r_off.rows or [])[:10]
        if bag(r_on.rows) == bag(r_off.rows):
            return finish("ok")
        return finish("divergent")

    if kind == "no_crash":
        r = pg.run(check["q"], timeout_s=timeout)
        if r.is_internal_error:
            return finish("crash", error=r.error)
        if r.timed_out:
            return finish("timeout", error=r.error)
        if not r.ok:
            return finish("error", error=r.error)
        out["rows"] = (r.rows or [])[:10]
        return finish("ok")

    return {"outcome": "harness_error", "error": f"unknown kind {kind}"}


def run_case(pg: PostgresRunner, case: dict, timeout: float) -> list[dict]:
    setup_out = pg.setup(case["setup_sqls"])
    setup_errs = [f"{s[:60]} -> {str(e)[:120]}" for s, e in setup_out if e]
    results = []
    if setup_errs:
        results.append({"check": "<setup>", "outcome": "setup_error",
                        "errors": setup_errs})
        return results
    for i, check in enumerate(case["checks"]):
        for pre in case.get("pre_sqls", []):
            pg.run(pre, timeout_s=timeout)
        try:
            cell = run_check(pg, check, timeout)
        except Exception as exc:  # noqa: BLE001
            cell = {"outcome": "harness_error",
                    "error": f"{type(exc).__name__}: {exc}"}
            try:
                pg.connect()
            except Exception:
                pass
        cell["check"] = f"{i}:{check['kind']}"
        results.append(cell)
        # reset SET-shaped pre_sqls
        for pre in case.get("pre_sqls", []):
            toks = pre.split()
            if len(toks) >= 2 and toks[0].upper() in ("SET", "RESET"):
                key = toks[1].split("=")[0].split(" to ")[0]
                try:
                    pg.run(f"RESET {key}", timeout_s=timeout)
                except Exception:
                    pass
    fatal = pg.log_fatal_lines(pg.log_new_lines())
    if fatal:
        results.append({"check": "<serverlog>", "outcome": "log_fatal",
                        "errors": fatal[:5]})
    return results


def summarize(report: dict, outdir: Path) -> str:
    """Write SUMMARY.md: per-arm fired/not-fired/hit matrix + divergences."""
    hits = [r for r in report["runs"]
            if r["outcome"] in ("divergent", "expected_mismatch", "crash",
                                "log_fatal")]
    lines = ["# pg_transform_probes SUMMARY", ""]
    lines.append("builds: " + ", ".join(
        f"{t}={v}" for t, v in report["versions"].items()))
    lines.append("")

    # per-case matrix: rows=case.check, cols=prefix
    tags = list(report["versions"])
    cells = {}
    for r in report["runs"]:
        key = (r["case"], r.get("check", ""), r.get("arm", ""))
        cells.setdefault(key, {})[r["prefix"]] = r["outcome"]
    lines.append("| arm | case.check | " + " | ".join(tags) + " |")
    lines.append("|---|---|" + "---|" * len(tags))
    for (case, chk, arm), per in sorted(cells.items()):
        row = " | ".join(per.get(t, "-") for t in tags)
        lines.append(f"| {arm} | {case} `{chk}` | {row} |")
    lines.append("")

    # arm rollup
    by_arm = {}
    for r in report["runs"]:
        by_arm.setdefault(r.get("arm", "?"), Counter())[r["outcome"]] += 1
    lines.append("## arm rollup")
    lines.append("")
    for arm, c in sorted(by_arm.items()):
        lines.append(f"- **{arm}**: {dict(c)}")
    lines.append("")

    nf = [r for r in report["runs"] if r["outcome"] == "ok_notfired"]
    if nf:
        lines.append("## vacuous oracles (marker not in plan)")
        for r in nf:
            lines.append(f"- {r['prefix']} {r['case']} {r.get('check')}: "
                         f"{str(r.get('plan_head'))[:120]}")
        lines.append("")

    lines.append(f"## hits ({len(hits)})")
    lines.append("")
    for r in hits:
        lines.append(f"### {r['prefix']} {r['case']} `{r.get('check')}` "
                     f"-> {r['outcome']}")
        for k in ("error", "errors", "expected", "rows", "rows1", "rows2",
                  "rows_on", "rows_off"):
            if r.get(k) is not None:
                lines.append(f"  - {k}: `{r[k]}`")
        lines.append("")

    other = [r for r in report["runs"]
             if r["outcome"] in ("error", "other_error", "setup_error",
                                 "harness_error", "timeout")]
    if other:
        lines.append(f"## non-hit anomalies ({len(other)})")
        for r in other:
            lines.append(f"- {r['prefix']} {r['case']} `{r.get('check')}` "
                         f"-> {r['outcome']}: {str(r.get('error') or r.get('errors'))[:200]}")
        lines.append("")

    text = "\n".join(lines)
    (outdir / "SUMMARY.md").write_text(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", default="seeds.pg_transform_probes")
    ap.add_argument("--prefixes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--filter", default=None, help="regex on case name")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    if any("asan" in p for p in prefixes):
        os.environ.setdefault("ASAN_OPTIONS", "detect_leaks=0")
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
                cells = run_case(pg, case, args.timeout)
                el = round(time.time() - t0, 2)
                for cell in cells:
                    report["runs"].append({
                        "prefix": tag, "version": ver, "repeat": rep,
                        "case": case["name"], "arm": case.get("arm", "?"),
                        "elapsed_s": el, **cell})
                    extra = ""
                    if cell["outcome"] not in ("ok",):
                        extra = (f" err={str(cell.get('error') or cell.get('errors'))[:120]}"
                                 f" r1={cell.get('rows1')} r2={cell.get('rows2')}"
                                 f" rows={cell.get('rows')} exp={cell.get('expected')}")
                    print(f"{tag:>16} rep{rep} {case['name']:<28} "
                          f"{cell.get('check',''):<14} -> {cell['outcome']}{extra}",
                          flush=True)
        pg.cleanup()

    (out / "probe_run.json").write_text(
        json.dumps(report, indent=2, default=str))
    summarize(report, out)
    print(f"\n-> {out}/probe_run.json + SUMMARY.md")


if __name__ == "__main__":
    main()
