"""Build playbook v2: v1 cross-engine rules + rules distilled from the
newly-adjudicated DuckDB families (generation-2 knowledge).

Each new rule carries ``source_key`` = family σ so the curve analysis can
exclude seed-family rediscovery from the "new family" count.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evolution.playbook import Playbook, Rule  # noqa: E402
from repair_bench.run_bench import call_llm, load_key  # noqa: E402

RUN = Path("results/coevo_curve/duckdb_coevo_warm_20260915_233752_curve_coevo_warm_s0")
V1 = Path("results/playbook_seed_xeng.jsonl")
OUT = Path("results/playbook_seed_v2.jsonl")

PROMPT = """A confirmed DuckDB 1.5.5 bug family (auto-discovered):

kind={kind} variant={variant} nu={nu}
Minimal reproducer:
{repro}

Compress into ONE transferable lesson for a bug-hunting agent on ANY
SQL engine, as JSON:
{{"title": "<=12 words", "mechanism": "<=25 words",
  "probe_hint": "<=25 words"}}
Generalize away the exact tables."""


def main() -> int:
    key, base = load_key()
    pb = Playbook.load(V1, max_rules=30)
    adj = {a["key"]: a for a in
           json.loads((RUN / "adjudication.json").read_text())}
    seen_notes = set()
    for line in (RUN / "bugs.jsonl").open():
        b = json.loads(line)
        k = b.get("root_key", "")
        a = adj.get(k, {})
        if a.get("verdict") != "confirmed" or k.startswith("8c31489e"):
            continue
        cand = b.get("candidate", {})
        variant = a.get("variant") or (cand.get("r2_summary") or {}).get(
            "variant") or ""
        sig = f"{cand.get('kind')}:{variant}"
        if sig in seen_notes:
            continue  # one rule per mechanism cluster, not per σ
        seen_notes.add(sig)
        repro = (b.get("repro_sql") or json.dumps(
            b.get("minimal_case", {}), default=str))[:1200]
        resp = call_llm(
            PROMPT.format(kind=cand.get("kind"), variant=variant,
                          nu=(b.get("signature") or {}).get("nu"),
                          repro=repro),
            key, base, max_tokens=500)
        m = re.search(r"\{.*\}", resp or "", re.S)
        if not m:
            print("skip", k[:12])
            continue
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            print("bad json", k[:12])
            continue
        pb._seq += 1
        rule = Rule(id=f"R{pb._seq}",
                    title=str(data.get("title", ""))[:120],
                    mechanism=str(data.get("mechanism", ""))[:300],
                    probe_hint=str(data.get("probe_hint", ""))[:300],
                    source_family=f"AUTO-{k[:8]}",
                    source_key=k)
        pb.rules.append(rule)
        print(f"AUTO-{k[:8]} ->", rule.title)
    with OUT.open("w") as f:
        for r in pb.rules:
            f.write(json.dumps(r.__dict__) + "\n")
    print("wrote", OUT, len(pb.rules), "rules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
