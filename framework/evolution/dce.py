"""Diagnosis-Conditioned Exploration: expand a confirmed family along its
fault surface.

Given one minimized, soundly-confirmed bug, the expander enumerates
neighboring test cases deterministically — predicate-operator swaps,
subquery-form swaps, NULL/3-VAL flips, conjunct drops, column-type
substitutions, and data perturbations — and screens each through the
sound oracles (TLP / NoREC / plan-variant). Hits are minimized, signed
with sigma, and either join the parent family or found a new one.

This is the mechanism-level difference from coverage-guided search: the
neighborhood is defined by the *diagnosed fault surface*, not by plan
novelty. Zero LLM calls on this path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from oracles.determinism import is_usable_for_oracles
from oracles.models import Candidate, KIND_CRASH
from oracles.norec import NoRECOracle
from oracles.plan_variant import PlanVariantOracle
from oracles.tlp import TLPOracle, inject_predicate, tlp_applicable

LOGGER = logging.getLogger(__name__)

_CMP_OPS = ["=", "<>", "<", "<=", ">", ">="]
_CMP_RE = re.compile(r"<>|!=|<=|>=|(?<![<>=!])=(?!=)|<|>")


@dataclass
class Probe:
    """One neighboring test case of a confirmed family."""

    setup_sqls: list[str]
    select_from: str | None = None
    predicate: str | None = None
    query: str | None = None  # full query when no TLP split is available
    op: str = ""  # which mutation operator produced it
    label: str = ""

    def full_query(self) -> str:
        if self.query:
            return self.query
        if self.predicate:
            return inject_predicate(self.select_from or "", self.predicate)
        return self.select_from or ""


@dataclass
class ExpansionResult:
    probes_generated: int = 0
    probes_executed: int = 0
    hits: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------- probe generation
def _split_top_level(text: str, sep: str) -> list[str]:
    """Split on ``sep`` occurring outside parentheses/quotes."""
    parts, depth, in_str, current = [], 0, False, ""
    upper = text.upper()
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            current += ch
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and upper.startswith(sep, i):
            parts.append(current)
            current = ""
            i += len(sep)
            continue
        current += ch
        i += 1
    parts.append(current)
    return parts


def _cmp_positions(text: str) -> list[re.Match[str]]:
    """Comparison operators outside string literals."""
    out, in_str = [], False
    for match in _CMP_RE.finditer(text):
        # crude quote tracking: count quotes before the match
        in_str = (text[: match.start()].count("'") % 2) == 1
        if not in_str:
            out.append(match)
    return out


def _swap_at(text: str, match: re.Match[str], new_op: str) -> str:
    return text[: match.start()] + new_op + text[match.end() :]


def mutate_query_text(query: str) -> list[tuple[str, str]]:
    """Single-point textual mutations of a query/predicate string.

    Returns ``(new_text, op_label)`` pairs, deduplicated.
    """
    out: list[tuple[str, str]] = []
    seen = {query}
    # 1. comparison-operator swaps, one position at a time
    for match in _cmp_positions(query):
        current = match.group(0)
        cur = "=" if current == "==" else current
        for new_op in _CMP_OPS:
            if new_op == cur:
                continue
            cand = _swap_at(query, match, new_op)
            if cand not in seen:
                seen.add(cand)
                out.append((cand, f"cmp:{cur}->{new_op}"))
    # 2. subquery-form swaps
    for pat, repl, tag in [
        (r"\bNOT\s+EXISTS\b", "EXISTS", "form:not_exists->exists"),
        (r"(?<!NOT )EXISTS\b", "NOT EXISTS", "form:exists->not_exists"),
        (r"\bNOT\s+IN\s*\(", "IN (", "form:not_in->in"),
        (r"(?<!NOT )IN\s*\(", "NOT IN (", "form:in->not_in"),
        (r"\bNOT\s+LIKE\b", "LIKE", "form:not_like->like"),
        (r"\bIS\s+NOT\s+NULL\b", "IS NULL", "null:not_null->null"),
        (r"\bIS\s+NULL\b", "IS NOT NULL", "null:null->not_null"),
    ]:
        for match in re.finditer(pat, query, re.IGNORECASE):
            cand = query[: match.start()] + " " + repl + " " + query[match.end() :]
            cand = re.sub(r"\s+", " ", cand).strip()
            if cand not in seen:
                seen.add(cand)
                out.append((cand, tag))
    # 3. top-level conjunct drop / AND<->OR swaps
    conjuncts = _split_top_level(query, " AND ")
    if len(conjuncts) >= 2:
        for i in range(len(conjuncts)):
            cand = " AND ".join(c for j, c in enumerate(conjuncts) if j != i)
            if cand.strip() and cand not in seen:
                seen.add(cand)
                out.append((cand, "conj:drop"))
        or_joined = " OR ".join(conjuncts)
        if or_joined not in seen:
            seen.add(or_joined)
            out.append((or_joined, "conj:and->or"))
    # 3b. conjunct drop inside parenthesized groups (correlated subquery
    # predicates live inside `EXISTS ( ... AND ... AND ... )`).
    for group in re.finditer(r"\(([^()]*)\)", query):
        inner = group.group(1)
        parts = _split_top_level(inner, " AND ")
        if len(parts) < 2:
            continue
        for i in range(len(parts)):
            new_inner = " AND ".join(
                c for j, c in enumerate(parts) if j != i
            )
            if not new_inner.strip():
                continue
            cand = (
                query[: group.start()] + "(" + new_inner + ")"
                + query[group.end() :]
            )
            cand = re.sub(r"\s+", " ", cand).strip()
            if cand not in seen:
                seen.add(cand)
                out.append((cand, "conj:drop_inner"))
        or_inner = " OR ".join(parts)
        cand = query[: group.start()] + "(" + or_inner + ")" + query[group.end() :]
        cand = re.sub(r"\s+", " ", cand).strip()
        if cand not in seen:
            seen.add(cand)
            out.append((cand, "conj:inner_and->or"))
    return out


_DDL_TYPE_SWAPS = [
    (re.compile(r"\bTINYINT\b", re.I), "INTEGER"),
    (re.compile(r"\bSMALLINT\b", re.I), "INTEGER"),
    (re.compile(r"\bINTEGER\b|\bINT\b", re.I), "BIGINT"),
    (re.compile(r"\bBIGINT\b", re.I), "INTEGER"),
    (re.compile(r"\bHUGEINT\b", re.I), "BIGINT"),
    (re.compile(r"\bDECIMAL\s*\([^)]*\)|\bNUMERIC\s*\([^)]*\)", re.I), "DOUBLE"),
    (re.compile(r"\bDOUBLE\b|\bREAL\b|\bFLOAT\b", re.I), "DECIMAL(18,4)"),
    (re.compile(r"\bVARCHAR\b|\bTEXT\b|\bCHAR\b", re.I), "VARCHAR"),
]


def mutate_setup(setup_sqls: list[str]) -> list[tuple[list[str], str]]:
    """Single-statement setup mutations: column-type swaps and NULL/data
    perturbations on INSERT literals."""
    out: list[tuple[list[str], str]] = []
    for idx, stmt in enumerate(setup_sqls):
        for pattern, repl in _DDL_TYPE_SWAPS:
            if pattern.search(stmt) and re.match(
                r"\s*create\s+(or\s+replace\s+)?table", stmt, re.I
            ):
                cand = pattern.sub(repl, stmt, count=1)
                if cand != stmt:
                    new_setup = list(setup_sqls)
                    new_setup[idx] = cand
                    out.append((new_setup, f"ddl:{pattern.pattern}->{repl}"))
        if re.match(r"\s*insert\s+into", stmt, re.I):
            if not re.search(r"\bvalues\b", stmt, re.I):
                continue
            # Each ``(row)`` group; mutate literals inside the first rows.
            row_spans = list(re.finditer(r"\(([^()]*)\)", stmt))[:4]
            for row in row_spans:
                parts = row.group(1).split(",")
                for k, part in enumerate(parts):
                    val = part.strip()
                    if not re.fullmatch(
                        r"-?\d+(\.\d+)?([eE][+-]?\d+)?", val
                    ):
                        continue
                    if re.fullmatch(r"-?\d+", val):
                        neg = str(-int(val))
                    else:
                        neg = repr(-float(val))
                    new_parts = list(parts)
                    new_parts[k] = f" {neg}"
                    cand = (
                        stmt[: row.start()] + "(" + ",".join(new_parts) + ")"
                        + stmt[row.end() :]
                    )
                    new_setup = list(setup_sqls)
                    new_setup[idx] = cand
                    out.append((new_setup, f"data:negate[{k}]"))
                    break
                # NULL injection into first non-NULL literal of the row
                for k, part in enumerate(parts):
                    if part.strip().upper() == "NULL":
                        continue
                    new_parts = list(parts)
                    new_parts[k] = " NULL"
                    cand = (
                        stmt[: row.start()] + "(" + ",".join(new_parts) + ")"
                        + stmt[row.end() :]
                    )
                    new_setup = list(setup_sqls)
                    new_setup[idx] = cand
                    out.append((new_setup, f"data:null[{k}]"))
                    break
            # row duplication (multiplicity)
            new_setup = list(setup_sqls)
            new_setup.insert(idx + 1, stmt)
            out.append((new_setup, "data:dup_row"))
    return out


class FamilyExpander:
    """Generates and screens the fault-surface neighborhood of a family."""

    def __init__(
        self,
        runner_factory: Any,
        engine: str = "duckdb",
        max_probes: int = 250,
        max_executed: int = 150,
    ):
        self.runner_factory = runner_factory
        self.engine = engine
        self.max_probes = max_probes
        self.max_executed = max_executed
        self.tlp = TLPOracle()
        self.norec = NoRECOracle()
        self.plan_variant = PlanVariantOracle(engine=engine)

    # ------------------------------------------------------------ probes
    def probes_for(
        self,
        candidate: Candidate,
        minimal: dict[str, Any],
    ) -> list[Probe]:
        """Deterministic neighborhood around the minimized case."""
        setup = list(minimal.get("schema_sqls", candidate.schema_sqls)) + list(
            minimal.get("inserts", candidate.inserts)
        )
        q1 = minimal.get("q1", candidate.q1)
        predicate = (candidate.r2_summary or {}).get("predicate")
        probes: list[Probe] = []
        seen: set[str] = set()

        def add(setup_list, select_from, pred, query, op):
            probe = Probe(
                setup_sqls=setup_list,
                select_from=select_from,
                predicate=pred,
                query=query,
                op=op,
                label=f"{op}",
            )
            key = probe.full_query() + "||" + "\n".join(setup_list)
            if key in seen or len(probes) >= self.max_probes:
                return
            seen.add(key)
            probes.append(probe)

        # Text mutations on the query; when the case carries a TLP split
        # (q1 is the unpartitioned select_from), keep the split so TLP and
        # NoREC stay applicable.
        for text, op in mutate_query_text(q1):
            if predicate:
                add(setup, text, predicate, None, op)
            else:
                add(setup, None, None, text, op)
        if predicate:
            for text, op in mutate_query_text(predicate):
                add(setup, q1, text, None, op)

        # Setup mutations keep the query fixed.
        for new_setup, op in mutate_setup(setup):
            if predicate:
                add(new_setup, q1, predicate, None, op)
            else:
                add(new_setup, None, None, q1, op)

        return probes

    # ------------------------------------------------------------ screen
    def _check_probe(self, probe: Probe) -> list[Candidate]:
        """Run sound oracles on one probe; return hit candidates."""
        runner = self.runner_factory()
        hits: list[Candidate] = []
        try:
            outcomes = runner.setup(probe.setup_sqls)
            if any(err for _, err in outcomes):
                return hits
            full = probe.full_query()
            if not is_usable_for_oracles(full, runner, probe.setup_sqls):
                return hits
            if probe.predicate and probe.select_from:
                hit = self.tlp.check(
                    runner,
                    probe.select_from,
                    probe.predicate,
                    probe.setup_sqls,
                    [],
                    category="dce_probe",
                )
                if hit is not None:
                    hits.append(hit)
                hit = self.norec.check(
                    runner,
                    probe.select_from,
                    probe.predicate,
                    probe.setup_sqls,
                    [],
                    category="dce_probe",
                )
                if hit is not None:
                    hits.append(hit)
            hit = self.plan_variant.check(
                runner, full, probe.setup_sqls, [], category="dce_probe"
            )
            if hit is not None:
                hits.append(hit)
        except Exception:  # noqa: BLE001 - a probe must never kill the loop
            LOGGER.debug("probe failed", exc_info=True)
        finally:
            runner.close()
        return hits

    def expand(
        self,
        candidate: Candidate,
        minimal: dict[str, Any],
        reproduces: Any = None,
    ) -> ExpansionResult:
        """Generate probes and return oracle hits (caller dedups by sigma)."""
        result = ExpansionResult()
        probes = self.probes_for(candidate, minimal)
        result.probes_generated = len(probes)
        for probe in probes:
            if result.probes_executed >= self.max_executed:
                break
            result.probes_executed += 1
            for hit in self._check_probe(probe):
                result.hits.append(
                    {
                        "probe_op": probe.op,
                        "candidate": hit,
                        "setup_sqls": probe.setup_sqls,
                    }
                )
        return result
