"""Recall check: replay known fixed-in-16.x PostgreSQL bugs on each installed
version and verify the buggy signature fires exactly where expected.

This answers "is the harness blind, or is PG actually clean" — if a known bug
is undetectable by our outcome classification, the pipeline is the problem.

Usage:
    python scripts/pg_recall_check.py \
        --prefixes /path/pg160_assert,/path/pg162_assert,... \
        --out results/pg_recall_1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets.postgres_runner import PostgresRunner


def load_cases(module: str) -> list[dict]:
    if module == "pg_live_probes":
        from seeds.pg_live_probes import PG_LIVE_PROBES
        return PG_LIVE_PROBES
    if module == "pg_deferred_probes":
        from seeds.pg_deferred_probes import PG_DEFERRED_PROBES
        return PG_DEFERRED_PROBES
    from seeds.pg_recall import PG_RECALL
    return PG_RECALL


def parse_version(ver: str) -> tuple[int, int]:
    parts = ver.split(".")
    # devel snapshots report e.g. "20devel" — treat as minor 0 of that major
    m = re.match(r"(\d+)", parts[0])
    major = int(m.group(1)) if m else 0
    m2 = re.match(r"(\d+)", parts[1]) if len(parts) > 1 else None
    return major, int(m2.group(1)) if m2 else 0


def is_affected(case: dict, ver: tuple[int, int]) -> bool | None:
    """True if ver is in the buggy range, False if a fixed 16.x, None if other."""
    major, minor = ver
    aff = case["affected"]
    if major in aff:
        lo, hi = aff[major]
        return lo <= minor <= hi
    return None  # different major: observe only


def classify(case: dict, result) -> str:
    """'fired' if buggy signature present, 'clean' if fixed behavior.

    Modes:
    - wrong_rows: fires iff the query succeeds with rows != expected_rows
    - error_substr: fires iff the query fails with buggy_error in the message
    - error_or_crash: fires on backend crash/assert (is_internal_error), on
      buggy_error substring (if given), on timeout (only if timeout_fires),
      or on wrong rows vs expected_rows (if given)
    Crash/timeout outcomes are reported verbatim so they stay visible in the
    matrix even when they don't satisfy the expected signature.
    """
    mode = case["buggy"]
    if mode == "error_or_crash":
        if result.is_internal_error:
            return "fired"
        if result.timed_out:
            return "fired" if case.get("timeout_fires") else "timeout"
        if not result.ok:
            want = case.get("buggy_error")
            if want is not None and want in (result.error or ""):
                return "fired"
            clean_err = case.get("clean_error")
            if clean_err is not None and clean_err in (result.error or ""):
                return "clean"
            return "other_error"
        if "expected_rows" in case:
            return "clean" if result.rows == case["expected_rows"] else "fired"
        return "clean"
    if mode == "error_substr":
        # On assert builds the buggy path often dies on an assertion before
        # the elog reaches the client — a crash is the same signature.
        if result.is_internal_error:
            return "fired"
        if not result.ok and result.error and case["buggy_error"] in result.error:
            return "fired"
        if result.ok:
            return "clean"
        return "timeout" if result.timed_out else "other_error"
    if result.is_internal_error:
        return "crash"
    if result.timed_out:
        return "timeout"
    # wrong_rows
    if not result.ok:
        return "other_error"
    return "clean" if result.rows == case["expected_rows"] else "fired"


def build_capabilities(prefix: str, pg) -> set[str]:
    """Capability probe: which `requires` a build satisfies.

    contrib:<name> is detected from the installed extension control files;
    icu/jit by probe/filesystem; sessions:N>1 and restart are never satisfied
    by this single-session runner (kept explicit so cases report 'skipped').
    """
    caps: set[str] = {"sessions:1"}
    for share in (
        Path(prefix) / "share" / "postgresql" / "extension",
        Path(prefix) / "share" / "extension",
    ):
        if share.is_dir():
            caps.update(f"contrib:{p.stem}" for p in share.glob("*.control"))
            break
    libdir = Path(prefix) / "lib" / "postgresql"
    if (libdir / "llvmjit.so").exists():
        caps.add("jit")
    try:
        res = pg.run("SELECT icu_unicode_version()", timeout_s=5.0)
        if res.ok:
            caps.add("icu")
    except Exception:
        pass
    return caps


def missing_requires(case: dict, caps: set[str]) -> list[str]:
    return [r for r in case.get("requires", []) if r not in caps]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefixes", required=True, help="comma-separated install prefixes")
    ap.add_argument("--seed-module", default="pg_recall",
                    help="seeds.<module> providing the case list (pg_recall|pg_live_probes)")
    ap.add_argument("--out", default="results/pg_recall_1")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    cases = load_cases(args.seed_module)

    matrix: dict[str, dict[str, dict]] = {}
    versions: dict[str, tuple[int, int]] = {}
    caps_report: dict[str, list[str]] = {}

    for prefix in prefixes:
        tag = Path(prefix).name
        pg = PostgresRunner(out / f"data_{tag}", pg_prefix=prefix)
        ver_str = pg.engine_version
        versions[tag] = parse_version(ver_str)
        caps = build_capabilities(prefix, pg)
        caps_report[tag] = sorted(caps)
        for case in cases:
            missing = missing_requires(case, caps)
            if missing:
                matrix.setdefault(case["name"], {})[tag] = {
                    "version": ver_str,
                    "outcome": "skipped",
                    "missing": missing,
                }
                continue
            pg.setup(case["setup_sqls"])
            for pre in case["pre_sqls"]:
                pg.run(pre, timeout_s=args.timeout)
            res = pg.run(case["query"], timeout_s=args.timeout)
            outcome = classify(case, res)
            matrix.setdefault(case["name"], {})[tag] = {
                "version": ver_str,
                "outcome": outcome,
                "error": (res.error or "")[:200] if not res.ok else None,
                "rows_sample": (res.rows or [])[:5],
            }
            # restore default GUCs for the next case on the same session
            for pre in case["pre_sqls"]:
                key = pre.split()[1].split("=")[0].split(" to ")[0]
                try:
                    pg.run(f"RESET {key}", timeout_s=args.timeout)
                except Exception:
                    pass
        pg.cleanup()

    report = {"versions": {k: ".".join(map(str, v)) for k, v in versions.items()},
              "capabilities": caps_report, "matrix": matrix, "verdicts": {}}
    n_pass = n_fail = 0
    for case in cases:
        name = case["name"]
        per_ver = matrix[name]
        checks = []
        for tag, cell in per_ver.items():
            if cell["outcome"] == "skipped":
                checks.append((tag, "skipped", "skipped", None))
                continue
            ver = versions[tag]
            aff = is_affected(case, ver)
            if aff is True:
                ok = cell["outcome"] == "fired"
                checks.append((tag, "expected_fired", cell["outcome"], ok))
            elif aff is False:
                ok = cell["outcome"] == "clean"
                checks.append((tag, "expected_clean", cell["outcome"], ok))
            else:
                checks.append((tag, "observe", cell["outcome"], None))
        n_case_fail = sum(1 for _, _, _, ok in checks if ok is False)
        n_pass += sum(1 for _, _, _, ok in checks if ok is True)
        n_fail += n_case_fail
        report["verdicts"][name] = {
            "checks": checks,
            "status": "PASS" if n_case_fail == 0 else "FAIL",
        }
    report["summary"] = {"checks_passed": n_pass, "checks_failed": n_fail,
                         "cases": len(cases), "versions": len(versions)}
    (out / "recall_report.json").write_text(json.dumps(report, indent=2, default=str))

    print(f"\n{'case':<44}" + "".join(f"{t:>16}" for t in versions))
    for case in cases:
        row = matrix[case["name"]]
        cells = []
        for tag in versions:
            aff = is_affected(case, versions[tag])
            oc = row[tag]["outcome"]
            mark = "*" if aff else ("!" if oc == "fired" else " ")
            cells.append(f"{oc}{mark:>2}".rjust(14))
        print(f"{case['name']:<44}" + "".join(cells))
    print(f"\nverdicts: {n_pass} pass / {n_fail} fail -> {out}/recall_report.json")
    for name, v in report["verdicts"].items():
        print(f"  {v['status']:>4}  {name}")


if __name__ == "__main__":
    main()
