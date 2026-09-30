"""Co-evolution playbook — the accumulating shared knowledge.

Each confirmed family is distilled into a terse *rule* (mechanism +
probe hint). The digest is injected into the Hunter's prompt, and the
Hunter cites which rule inspired each query (``inspired_by``). That
makes the diagnosis->query causal edge measurable: per-rule and
per-iteration hit rates show whether accumulated knowledge actually
steers discovery — the "rising curve" co-evolution claim.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

LOGGER = logging.getLogger("playbook")


@dataclass
class Rule:
    id: str
    title: str
    mechanism: str
    probe_hint: str
    source_family: str = ""
    source_key: str = ""   # σ of the family this rule was distilled from
    created_iter: int = 0
    inspired: int = 0      # queries citing this rule
    candidates: int = 0    # those queries produced candidates
    families: int = 0      # those queries produced confirmed families


class Playbook:
    def __init__(self, max_rules: int = 12, demote_after: int = 30):
        self.demote_after = demote_after
        self.rules: list[Rule] = []
        self.max_rules = max_rules
        self._seq = 0

    # --------------------------------------------------------- distillation
    _DISTILL_TMPL = """A DBMS bug family was just confirmed and diagnosed.

Minimal reproducer:
{repro}

Diagnosis metadata: {sigma}
Verifier analysis: {analysis}

Compress it into ONE transferable lesson for a bug-hunting agent, as JSON:
{{"title": "<=12 words naming the defect pattern",
  "mechanism": "<=25 words: what code-level assumption is violated",
  "probe_hint": "<=25 words: what new query shapes would expose siblings/generalizations"}}
Be concrete and generalizable (feature classes, not the exact tables)."""

    def distill(
        self,
        record: dict[str, Any],
        sigma: dict[str, Any] | None,
        iteration: int,
        llm_fn: Callable[..., str | None],
    ) -> Rule | None:
        """Turn a confirmed family's record into a playbook rule."""
        repro = record.get("repro_sql") or json.dumps(
            record.get("minimal_case", {}), default=str)[:1200]
        analysis = record.get("analysis", {})
        prompt = self._DISTILL_TMPL.format(
            repro=repro[:1500],
            sigma=json.dumps(sigma or {}, default=str)[:600],
            analysis=json.dumps(analysis, default=str)[:400],
        )
        try:
            resp = llm_fn(prompt, temperature=0.4, max_tokens=600,
                          event="playbook_distill")
        except Exception as e:  # noqa: BLE001
            LOGGER.warning("distill call failed: %s", e)
            return None
        if not resp:
            return None
        m = re.search(r"\{.*\}", resp, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        self._seq += 1
        rule = Rule(
            id=f"R{self._seq}",
            title=str(data.get("title", ""))[:120],
            mechanism=str(data.get("mechanism", ""))[:300],
            probe_hint=str(data.get("probe_hint", ""))[:300],
            source_family=str(record.get("root_key", ""))[:24],
            created_iter=int(iteration) if isinstance(iteration, int) else 0,
        )
        self.rules.append(rule)
        if len(self.rules) > self.max_rules:
            # evict least-productive (not least-recent): zero-hit rules first
            self.rules.sort(
                key=lambda r: (r.families, r.candidates, r.inspired))
            self.rules.pop(0)
        LOGGER.info("playbook +%s: %s", rule.id, rule.title)
        return rule

    # ------------------------------------------------------------ digest
    def _demoted(self, r: "Rule") -> bool:
        """A rule is demoted when it attracted many citations but never
        converted one into a candidate or family: it burns prompt space."""
        return (r.families == 0 and r.candidates == 0
                and r.inspired >= self.demote_after)

    def digest(self, k: int | None = None) -> str:
        """Compact rule list for prompt injection.

        Order by productivity (families > candidates > citations) rather
        than recency, and soft-demote chronically misfiring rules.
        """
        if not self.rules:
            return ""
        ranked = sorted(
            self.rules,
            key=lambda r: (self._demoted(r), -r.families, -r.candidates,
                           -r.inspired))
        rules = [r for r in ranked if not self._demoted(r)]
        rules = rules[: (k or self.max_rules)]
        if not rules:  # everything demoted: keep top of the pile anyway
            rules = ranked[:1]
        lines = [
            f"{r.id} {r.title} — {r.mechanism} Probe: {r.probe_hint}"
            for r in rules
        ]
        return "Confirmed-bug lessons so far (generalize, do not copy):\n" + \
            "\n".join(lines)

    # --------------------------------------------------------- attribution
    def note_inspired(self, rule_id: str) -> None:
        for r in self.rules:
            if r.id == rule_id:
                r.inspired += 1

    def note_candidate(self, rule_id: str) -> None:
        for r in self.rules:
            if r.id == rule_id:
                r.candidates += 1

    def note_family(self, rule_id: str) -> None:
        for r in self.rules:
            if r.id == rule_id:
                r.families += 1

    def known_ids(self) -> set[str]:
        return {r.id for r in self.rules}

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        return {
            "size": len(self.rules),
            "rules": [asdict(r) for r in self.rules],
            "total_inspired": sum(r.inspired for r in self.rules),
            "total_candidates": sum(r.candidates for r in self.rules),
            "total_families": sum(r.families for r in self.rules),
        }

    def save(self, path) -> None:
        with open(path, "w") as f:
            for r in self.rules:
                f.write(json.dumps(asdict(r)) + "\n")

    @classmethod
    def load(cls, path, max_rules: int = 12) -> "Playbook":
        pb = cls(max_rules=max_rules)
        with open(path) as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    pb.rules.append(Rule(**d))
                    m = re.match(r"R(\d+)", d.get("id", ""))
                    if m:
                        pb._seq = max(pb._seq, int(m.group(1)))
        return pb
