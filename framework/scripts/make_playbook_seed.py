"""Distill confirmed non-DuckDB families into a warm-start playbook seed.

Used by the coevo_warm transfer experiment: lessons learned on other
engines are injected into a DuckDB hunt to test whether accumulated
knowledge generalizes across engines.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evolution.playbook import Playbook, Rule  # noqa: E402
from repair_bench.run_bench import call_llm, load_key  # noqa: E402

FINDINGS = Path("results/manual_findings.jsonl")
OUT = Path("results/playbook_seed_xeng.jsonl")

PROMPT = """A confirmed DBMS bug family on {engine}:

id={fid} manifestation={manifestation} kind={fix_kind}
detail: {note}

Compress it into ONE transferable lesson for a bug-hunting agent working
on ANY SQL engine, as JSON:
{{"title": "<=12 words naming the defect pattern",
  "mechanism": "<=25 words: what code-level assumption is violated",
  "probe_hint": "<=25 words: what query shapes would expose this or siblings"}}
Generalize away engine-specific syntax."""


def main() -> int:
    key, base = load_key()
    pb = Playbook(max_rules=20)
    for line in FINDINGS.open():
        d = json.loads(line)
        if d.get("tier") != "confirmed" or d.get("fid", "").startswith("F"):
            continue
        prompt = PROMPT.format(
            engine=d.get("engine", "?"), fid=d["fid"],
            manifestation=d.get("manifestation", "?"),
            fix_kind=d.get("fix_kind", "?"),
            note=d.get("note", "")[:500],
        )
        resp = call_llm(prompt, key, base, max_tokens=500)
        m = re.search(r"\{.*\}", resp or "", re.S)
        if not m:
            print("skip", d["fid"], resp[:80] if resp else "no resp")
            continue
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            print("bad json", d["fid"])
            continue
        pb._seq += 1
        pb.rules.append(Rule(
            id=f"R{pb._seq}",
            title=str(data.get("title", ""))[:120],
            mechanism=str(data.get("mechanism", ""))[:300],
            probe_hint=str(data.get("probe_hint", ""))[:300],
            source_family=d["fid"],
        ))
        print(d["fid"], "->", pb.rules[-1].title)
    pb.save(OUT)
    print("wrote", OUT, len(pb.rules), "rules")
    print("\n" + pb.digest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
