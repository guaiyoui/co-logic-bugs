"""R2 localization benchmark — does the diagnosis bundle predict WHERE the
upstream fix lands?

For families with a merged upstream fix we have ground truth (files +
functions from the real diff). The Fixer emits top-K file/function
hypotheses under three information tiers; we score overlap.

Usage:
  python3 run_localize.py --attempts 3
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from util.paths import DUCKDB_SRC, KEYS_YML  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"

LOGGER = logging.getLogger("localize_bench")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# Ground truth extracted from real upstream fix diffs (verified above).
GROUND_TRUTH = {
    "F2": {
        "pr": "duckdb/duckdb#24685 (fixes #24629)",
        "files": ["src/planner/expression/bound_window_expression.cpp"],
        "functions": ["BoundWindowExpression::PartitionsAreEquivalent"],
        "repro": (
            "CREATE TABLE src(a INTEGER, b INTEGER, v INTEGER);\n"
            "INSERT INTO src VALUES (1, 1, 10), (1, 2, 20);\n"
            "SELECT SUM(v) OVER (PARTITION BY a, a) AS w_aa,\n"
            "       SUM(v) OVER (PARTITION BY a, b) AS w_ab\n"
            "FROM src ORDER BY b;\n"
            "-- buggy (1.5.5): 30,30 / 30,30   expected: 30,10 / 30,20"
        ),
        "sigma": (
            "manifestation=wrong_result (deterministic); "
            "fix_set=[] (no plan switch flips it; physical window grouping); "
            "plan_diff=[] ; nu=recent (absent on main d8cdaa3)"
        ),
    },
    "F10": {
        "pr": "duckdb/duckdb#24560 (fixes #24307)",
        "files": ["src/function/window/window_boundaries_state.cpp"],
        "functions": [
            "WindowBoundariesState::FrameBegin",
            "WindowBoundariesState::FrameEnd",
        ],
        "repro": (
            "SELECT count(*) OVER (ORDER BY i ROWS BETWEEN "
            "9223372036854775807 FOLLOWING AND 9223372036854775807 FOLLOWING)\n"
            "FROM range(3) t(i);\n"
            "-- buggy (1.5.5): SIGFPE / internal error / garbage values; "
            "expected: 0,0,0 (empty frames)\n"
            "SELECT count(*) OVER (ORDER BY i ROWS BETWEEN -1 PRECEDING AND "
            "UNBOUNDED FOLLOWING) FROM range(3) t(i);\n"
            "-- buggy: accepted / wrong; expected: error"
        ),
        "sigma": (
            "manifestation=crash_or_wrong_result; fix_set=[] ; "
            "plan_diff=[WINDOW]; nu=recent"
        ),
    },
}

LISTING_CMD = (
    "cd {src} && find src -name '*.cpp' | sort"
)


def load_key() -> tuple[str, str]:
    txt = KEYS_YML.read_text()
    key = re.search(r"key:\s*[\"']?([^\s\"']+)", txt).group(1)
    url = re.search(r"completion_url:\s*[\"']?([^\s\"']+)", txt).group(1)
    return key, url.rstrip("/")


def call_llm(prompt: str, key: str, base: str, max_tokens: int = 3000) -> str:
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": max_tokens,
        },
        timeout=300.0,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def src_listing() -> str:
    out = subprocess.run(
        LISTING_CMD.format(src=DUCKDB_SRC), shell=True,
        capture_output=True, text=True,
    )
    return out.stdout


def extract_guesses(text: str) -> list[dict]:
    """Parse `file | function | why` lines from the response."""
    guesses = []
    for line in text.splitlines():
        line = line.strip().strip("|")
        if "/" not in line and "::" not in line:
            continue
        m = re.search(
            r"(src/[A-Za-z0-9_./-]+\.(?:cpp|hpp|h|c))\s*\|\s*([^|]+)", line)
        if m:
            guesses.append(
                {"file": m.group(1).strip(), "function": m.group(2).strip()})
    return guesses


def extract_greps(text: str) -> list[str]:
    """Parse `GREP <pattern>` lines the model asks for."""
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*GREP\s+(.+)", line)
        if m:
            pat = m.group(1).strip().strip("`\"'")
            if pat and len(pat) < 80:
                out.append(pat)
    return out[:4]


def run_grep(pattern: str) -> str:
    p = subprocess.run(
        ["grep", "-rn", "--include=*.cpp", "--include=*.hpp",
         "-m", "6", "--", pattern, "src"],
        cwd=DUCKDB_SRC, capture_output=True, text=True, timeout=30,
    )
    return p.stdout[:3000] or "(no matches)"


def score(fam: str, guesses: list[dict]) -> dict:
    gt = GROUND_TRUTH[fam]
    gt_files = set(gt["files"])
    gt_funcs = {f.split("::")[-1] for f in gt["functions"]}
    top = guesses[:3]
    file_hit = any(g["file"] in gt_files for g in top)
    func_hit = any(
        any(f.split("::")[-1].lower() in g["function"].lower()
            or g["function"].lower() in f.split("::")[-1].lower()
            for f in gt_funcs)
        for g in top
    )
    # also accept "file named anywhere in top-3 functions col" leniently
    return {"file_hit_top3": file_hit, "func_hit_top3": func_hit,
            "n_guesses": len(top)}


def build_prompt(fam: str, tier: str, listing: str, diag: str = "") -> str:
    g = GROUND_TRUTH[fam]
    p = (
        "A bug was found in DuckDB (built from source tree at your disposal "
        "conceptually). Reproducer and behavior:\n\n```sql\n"
        + g["repro"] + "\n```\n\n"
    )
    if tier in ("b_sigma", "c_diag"):
        p += "Diagnostic signature from the testing framework:\n" + g["sigma"] + "\n\n"
    if tier == "c_diag":
        p += "Mechanism hypothesis:\n" + diag + "\n\n"
    p += (
        "Candidate source files (subset of the tree):\n```\n" + listing
        + "\n```\n\nName your TOP-3 most likely buggy locations, one per "
        "line, exactly as:\n`src/path/file.cpp | FunctionName | one-line why`\n"
    )
    return p


def build_grep_prompt(fam: str, tier: str, diag: str = "") -> str:
    g = GROUND_TRUTH[fam]
    p = (
        "A bug was found in DuckDB. Reproducer and behavior:\n\n```sql\n"
        + g["repro"] + "\n```\n\n"
    )
    if tier in ("b_sigma", "c_diag"):
        p += "Diagnostic signature:\n" + g["sigma"] + "\n\n"
    if tier == "c_diag":
        p += "Mechanism hypothesis:\n" + diag + "\n\n"
    p += (
        "Before naming locations, you may request up to 4 grep searches over "
        "the DuckDB src/ tree. Emit lines `GREP <pattern>` for what you want "
        "to search (literal string, e.g. `GREP PartitionsAreEquivalent`), "
        "then stop. Do NOT guess locations yet."
    )
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempts", type=int, default=3)
    args = ap.parse_args()

    key, base = load_key()
    listing = src_listing()
    (RUNS / "src_listing.txt").write_text(listing)
    out_path = RUNS / "localize_results.jsonl"

    for fam in GROUND_TRUTH:
        # mechanism hypothesis for c_diag: one LLM call per family
        diag = ""
        dcache = RUNS / f"diag_{fam}.txt"
        if dcache.exists():
            diag = dcache.read_text()
        else:
            dprompt = (
                "DuckDB bug. Reproducer:\n```sql\n" + GROUND_TRUTH[fam]["repro"]
                + "\n```\nDiagnosis metadata:\n" + GROUND_TRUTH[fam]["sigma"]
                + "\n\nHypothesize the root-cause mechanism precisely "
                "(what code is doing the wrong thing, and where it likely "
                "lives). Terse."
            )
            try:
                diag = call_llm(dprompt, key, base)
                dcache.write_text(diag)
            except Exception as e:  # noqa: BLE001
                LOGGER.error("diag call failed for %s: %s", fam, e)
                diag = "(diagnosis unavailable)"

        for tier in ("a_raw", "b_sigma", "c_diag"):
            for k in range(args.attempts):
                tag = f"{fam}_{tier}_{k}"
                calls = 0
                try:
                    # round 1: model requests greps (agentic source access)
                    gresp = call_llm(build_grep_prompt(fam, tier, diag),
                                     key, base)
                    calls += 1
                    (RUNS / f"loc_{tag}_greps.txt").write_text(gresp)
                    grep_ctx = ""
                    for pat in extract_greps(gresp):
                        grep_ctx += (f"$ grep -rn '{pat}' src/\n"
                                     + run_grep(pat) + "\n\n")
                    # round 2: final localization with grep evidence
                    prompt = build_prompt(fam, tier, listing, diag)
                    if grep_ctx:
                        prompt += (
                            "\nGrep results you requested:\n```\n" + grep_ctx
                            + "```\n")
                    resp = call_llm(prompt, key, base)
                    calls += 1
                except Exception as e:  # noqa: BLE001
                    rec = {"fam": fam, "tier": tier, "attempt": k,
                           "error": str(e), "llm_calls": calls}
                    with out_path.open("a") as f:
                        f.write(json.dumps(rec) + "\n")
                    continue
                (RUNS / f"loc_{tag}.txt").write_text(resp)
                guesses = extract_guesses(resp)
                sc = score(fam, guesses)
                rec = {"fam": fam, "tier": tier, "attempt": k,
                       "guesses": guesses[:3], "llm_calls": calls,
                       "greps": extract_greps(gresp), **sc}
                with out_path.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
                LOGGER.info("%s: file_hit=%s func_hit=%s n=%d greps=%s",
                            tag, sc["file_hit_top3"], sc["func_hit_top3"],
                            sc["n_guesses"], extract_greps(gresp))
    LOGGER.info("done -> %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
