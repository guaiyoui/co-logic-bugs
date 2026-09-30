"""Optimizer belief extraction from EXPLAIN output (belief auditing).

Every planner rewrite rests on a derived fact — a *belief* — that the
optimizer claims holds for all data satisfying the schema/query.  This
module walks a JSON plan (``EXPLAIN (FORMAT JSON, VERBOSE)``) plus the
``EXPLAIN (VERBOSE, COSTS OFF)`` text and emits one record per belief the
plan *asserts*.  Each record carries the semantic claim so an auditor can
check it against the concrete data: a single counterexample row falsifies
the belief even when the query result happens to be right (RIPR: the fault
is caught at infection time, not after propagation to the output).

Detector kinds (``belief["kind"]`` in the case spec):

  absent_qual      needle missing from VERBOSE plan text
                   -> planner believes the qual is implied/unnecessary
  inlined_expr     `present` needle in plan text while `absent` is not
                   -> a body was inlined believing it preserves semantics
                      (e.g. STRICT SQL function with a non-strict body)
  run_condition    WindowAgg node carrying "Run Condition"
                   -> predicate believed monotone in frame order
  inner_unique     join node with "Inner Unique": true
                   -> <=1 inner match per outer row believed
  oj_reduced       query declares LEFT/RIGHT/FULL JOIN but the plan
                   contains no such join type
                   -> every WHERE qual believed strict on the nullable side
  partition_pruned children(parent) minus scanned relation names nonempty
                   -> pruned partitions believed to contain no matching rows
  memoize          Memoize node present
                   -> inner result believed functionally determined by
                      the cache key
  group_key_reduced an Aggregate's "Group Key" shorter than `query_keys`
                   -> dropped keys believed functionally determined

A belief record: {"id", "kind", "asserted", "evidence", "claim", "audit"}.
``asserted`` False means the plan did not rely on the claim (fixed builds,
or rewrites not selected) — the audit is then skipped by the driver.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

_DECLARED_JOIN_RE = re.compile(
    r"\b(left|right|full)(?:\s+outer)?\s+join\b", re.IGNORECASE)
_PLAN_JOINTYPE = {"Left": "left", "Right": "right", "Full": "full"}


def walk_plan(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield every node in a FORMAT JSON plan tree (preorder)."""
    yield node
    for sub in node.get("Plans") or []:
        yield from walk_plan(sub)


def _plan_nodes(plan_json: Any) -> list[dict[str, Any]]:
    try:
        root = plan_json[0]["Plan"]
    except (KeyError, IndexError, TypeError):
        return []
    return list(walk_plan(root))


def _scanned_relations(nodes: Iterable[dict[str, Any]]) -> set[str]:
    return {n["Relation Name"] for n in nodes if "Relation Name" in n}


def extract_beliefs(
    case: dict,
    plan_text: str,
    plan_json: Any,
    children_of: Callable[[str], list[str]],
) -> list[dict]:
    """Evaluate each belief spec of ``case`` against the plan.

    ``children_of`` maps a partitioned parent relname to its child relnames
    (the driver supplies a catalog lookup).
    """
    nodes = _plan_nodes(plan_json)
    out: list[dict] = []
    for spec in case.get("beliefs", []):
        kind = spec["kind"]
        rec = {"id": spec["id"], "kind": kind,
               "claim": spec.get("claim", ""),
               "audit": spec.get("audit"),
               "asserted": False, "evidence": ""}

        if kind == "absent_qual":
            needle = spec["needle"]
            if needle not in plan_text:
                rec.update(asserted=True,
                           evidence=f"qual {needle!r} dropped from plan")

        elif kind == "inlined_expr":
            present, absent = spec["present"], spec["absent"]
            if present in plan_text and absent not in plan_text:
                rec.update(asserted=True,
                           evidence=f"plan shows {present!r}, not {absent!r}")

        elif kind == "run_condition":
            hits = [n["Run Condition"] for n in nodes
                    if "Run Condition" in n]
            if hits:
                rec.update(asserted=True, evidence=f"Run Condition {hits}")

        elif kind == "inner_unique":
            hits = [
                f'{n["Node Type"]} cond='
                f'{n.get("Hash Cond") or n.get("Merge Cond") or n.get("Join Filter")}'
                for n in nodes if n.get("Inner Unique")]
            if hits:
                rec.update(asserted=True, evidence="; ".join(hits))

        elif kind == "oj_reduced":
            # A LEFT JOIN B == B RIGHT JOIN A: the planner may commute a
            # one-sided outer join, so count the union of left/right.
            declared = [m.group(1).lower()
                        for m in _DECLARED_JOIN_RE.finditer(case["q"])]
            planned = [_PLAN_JOINTYPE[n["Join Type"]] for n in nodes
                       if n.get("Join Type") in _PLAN_JOINTYPE]
            decl_1s = declared.count("left") + declared.count("right")
            plan_1s = planned.count("left") + planned.count("right")
            missing = []
            if plan_1s < decl_1s:
                missing.append(f"{decl_1s - plan_1s} one-sided")
            if planned.count("full") < declared.count("full"):
                missing.append("full")
            if missing:
                rec.update(
                    asserted=True,
                    evidence=f"declared {declared} -> plan outer joins "
                             f"{planned}; {', '.join(missing)} reduced")

        elif kind == "partition_pruned":
            parent = spec["parent"]
            children = set(children_of(parent))
            scanned = _scanned_relations(nodes)
            pruned = sorted(children - scanned)
            if children and scanned & children and pruned:
                rec.update(asserted=True,
                           evidence=f"children={sorted(children)} "
                                    f"scanned={sorted(scanned & children)} "
                                    f"pruned={pruned}")

        elif kind == "memoize":
            hits = [n.get("Cache Key", "?") for n in nodes
                    if n.get("Node Type") == "Memoize"]
            if hits:
                rec.update(asserted=True,
                           evidence=f"Memoize Cache Key {hits}")

        elif kind == "group_key_reduced":
            want = spec["query_keys"]
            hits = [n.get("Group Key") for n in nodes
                    if n.get("Group Key")
                    and len(n["Group Key"]) < want]
            if hits:
                rec.update(asserted=True,
                           evidence=f"Group Key {hits} < query keys {want}")

        out.append(rec)
    return out


# ---------------------------------------------------------------------
# generic discovery: harvest asserted beliefs from ANY plan and
# auto-generate counterexample audits where the claim is mechanically
# translatable to SQL.
# ---------------------------------------------------------------------

_QUALIFIED_COL_RE = re.compile(r"\b(\w+)\.(\w+)\b")
_EQ_PAIR_RE = re.compile(
    r"\(?\s*(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)\s*\)?")


def _rels_under(node: dict[str, Any]) -> set[str]:
    return _scanned_relations(walk_plan(node))


def _aliases_under(node: dict[str, Any]) -> dict[str, str]:
    """alias -> relation name for every scan in the subtree."""
    return {n.get("Alias", n["Relation Name"]): n["Relation Name"]
            for n in walk_plan(node) if "Relation Name" in n}


def _filters_under(node: dict[str, Any]) -> list[str]:
    """Own-relation quals inside a subtree (skip param'd/subplan conds)."""
    out = []
    for n in walk_plan(node):
        for key in ("Filter", "Index Cond", "Recheck Cond"):
            f = n.get(key)
            if f and "$" not in f and "SubPlan" not in f:
                out.append(f)
    return out


_DEDUP_NODES = {"Unique", "HashAggregate", "Aggregate", "SetOp"}


def _inner_key_audit(join_node: dict[str, Any],
                     inner_root: dict[str, Any]) -> str | None:
    """Build 'inner key has duplicates' counterexample SQL, if simple.

    Inner Unique asserts the inner SUBPLAN OUTPUT is unique on the join
    key.  Only a bare scan (+ quals) delegates that claim to the base
    relation — if the subtree contains a dedup node the output is unique
    by executor construction and the base-rel check would overreach.
    """
    if any(n.get("Node Type") in _DEDUP_NODES
           for n in walk_plan(inner_root)):
        return None
    aliases = _aliases_under(inner_root)
    if len(aliases) != 1:
        return None
    alias, rel = next(iter(aliases.items()))
    cond = (join_node.get("Hash Cond") or join_node.get("Merge Cond")
            or join_node.get("Join Filter") or "")
    keys = []
    for m in _EQ_PAIR_RE.finditer(cond):
        for side in ((m.group(1), m.group(2)), (m.group(3), m.group(4))):
            other = (m.group(3), m.group(4)) if side == (
                m.group(1), m.group(2)) else (m.group(1), m.group(2))
            if side[0] in aliases and other[0] not in aliases:
                keys.append(f"{side[0]}.{side[1]}")
    if not keys:
        return None
    quals = " AND ".join(_filters_under(inner_root))
    where = f" WHERE {quals}" if quals else ""
    cols = ", ".join(sorted(set(keys)))
    from_ = rel if alias == rel else f"{rel} AS {alias}"
    return (f"SELECT {cols} FROM {from_}{where} GROUP BY {cols} "
            f"HAVING count(*) > 1")


def _oj_audit(join_node: dict[str, Any],
              inner_root: dict[str, Any],
              nonnull_col: Callable[[str], str | None]) -> str | None:
    """'reduction qual rejects null-extension' counterexample SQL.

    Reduction is sound iff the conjunction of WHERE quals pushed onto the
    formerly-nullable side is never TRUE on a null-extended row.  Audit
    the un-reduced LEFT JOIN: a null-extended row (sentinel NOT NULL col
    IS NULL) on which every pushed conjunct is TRUE falsifies the claim.
    """
    aliases = _aliases_under(inner_root)
    if len(aliases) != 1:
        return None
    alias, rel = next(iter(aliases.items()))
    sentinel = nonnull_col(rel)
    if sentinel is None:
        return None
    quals = [f for f in _filters_under(inner_root)
             if _QUALIFIED_COL_RE.search(f)]
    if not quals:
        return None
    cond = (join_node.get("Hash Cond") or join_node.get("Merge Cond")
            or join_node.get("Join Filter"))
    if not cond:
        return None
    outer_map = _aliases_under(join_node["Plans"][0])
    if len(outer_map) != 1:
        return None
    oalias, orel = next(iter(outer_map.items()))
    ofrom = orel if oalias == orel else f"{orel} AS {oalias}"
    ifrom = rel if alias == rel else f"{rel} AS {alias}"
    all_true = " AND ".join(f"({q}) IS TRUE" for q in sorted(set(quals)))
    return (f"SELECT 1 FROM {ofrom} LEFT JOIN {ifrom} "
            f"ON {cond} WHERE {alias}.{sentinel} IS NULL "
            f"AND {all_true} LIMIT 1")


def discover_beliefs(
    query_text: str,
    plan_json: Any,
    children_of: Callable[[str], list[str]],
    parent_of: Callable[[str], str | None],
    nonnull_col: Callable[[str], str | None] = lambda _r: None,
) -> list[dict]:
    """Emit every belief the plan asserts, with generated audits.

    Audit convention: SQL returns violation rows; empty = belief holds.
    Beliefs without a mechanical audit are recorded with audit=None
    (assertion-only census data).
    """
    nodes = _plan_nodes(plan_json)
    recs: list[dict] = []

    # inner_unique on every join node
    for i, n in enumerate(nodes):
        if n.get("Inner Unique") and len(n.get("Plans") or []) >= 2:
            recs.append({
                "id": f"inner_unique[{i}]", "kind": "inner_unique",
                "asserted": True,
                "evidence": f'{n["Node Type"]} '
                            f'cond={n.get("Hash Cond") or n.get("Merge Cond")}',
                "claim": "<=1 inner match per outer row",
                "audit": _inner_key_audit(n, n["Plans"][1]),
            })

    # oj_reduced: declared outer joins vs plan join types
    declared = [m.group(1).lower()
                for m in _DECLARED_JOIN_RE.finditer(query_text)]
    planned = [_PLAN_JOINTYPE[n["Join Type"]] for n in nodes
               if n.get("Join Type") in _PLAN_JOINTYPE]
    if declared:
        decl_1s = declared.count("left") + declared.count("right")
        plan_1s = planned.count("left") + planned.count("right")
        if plan_1s < decl_1s or planned.count("full") < declared.count("full"):
            audit = None
            # simple case: exactly one reduced one-sided join and an Inner
            # join node whose inner side is a single base rel
            if decl_1s - plan_1s == 1:
                for n in nodes:
                    if (n.get("Join Type") == "Inner"
                            and len(n.get("Plans") or []) >= 2):
                        audit = _oj_audit(n, n["Plans"][1], nonnull_col)
                        if audit:
                            break
            recs.append({
                "id": "oj_reduced", "kind": "oj_reduced", "asserted": True,
                "evidence": f"declared {declared} -> plan {planned}",
                "claim": "WHERE quals strict on the nullable side",
                "audit": audit,
            })

    # run conditions (assertion census; audit needs window reconstruction)
    for i, n in enumerate(nodes):
        if "Run Condition" in n:
            recs.append({
                "id": f"run_condition[{i}]", "kind": "run_condition",
                "asserted": True,
                "evidence": n["Run Condition"],
                "claim": "predicate monotonic in frame order",
                "audit": None,
            })

    # memoize cache keys (assertion census)
    for i, n in enumerate(nodes):
        if n.get("Node Type") == "Memoize":
            recs.append({
                "id": f"memoize[{i}]", "kind": "memoize",
                "asserted": True,
                "evidence": f"Cache Key {n.get('Cache Key')}",
                "claim": "cache key determines inner result",
                "audit": None,
            })

    # partition pruning: scanned children vs full child set
    scanned = _scanned_relations(nodes)
    parents = {parent_of(r) for r in scanned} - {None}
    for p in sorted(parents):
        children = set(children_of(p))
        pruned = sorted(children - scanned)
        if children & scanned and pruned:
            # apply ONE scanned sibling's quals to each pruned child,
            # re-aliased to that sibling's plan alias (siblings may use
            # different aliases, so never mix their quals)
            quals, sib_alias = [], "p"
            for n in nodes:
                if n.get("Relation Name") in children & scanned:
                    quals = _filters_under(n)
                    sib_alias = n.get("Alias", "p")
                    if quals:
                        break
            audits = {}
            if quals:
                cond = " AND ".join(sorted(set(quals)))
                for child in pruned:
                    audits[child] = (
                        f"SELECT * FROM {child} AS {sib_alias} "
                        f"WHERE {cond} LIMIT 5")
            recs.append({
                "id": f"pruned[{p}]", "kind": "partition_pruned",
                "asserted": True,
                "evidence": f"scanned={sorted(children & scanned)} "
                            f"pruned={pruned}",
                "claim": "pruned partitions contain no qual-matching row",
                "audits": audits or None,
            })

    # group key reduction via FD
    gby = re.search(r"\bgroup\s+by\b(.*?)(?:\border\s+by\b|\bhaving\b|"
                    r"\blimit\b|$)", query_text, re.IGNORECASE | re.DOTALL)
    if gby:
        qkeys = [k.strip() for k in gby.group(1).split(",") if k.strip()]
        for i, n in enumerate(nodes):
            gk = n.get("Group Key")
            if gk and len(gk) < len(qkeys):
                recs.append({
                    "id": f"group_key[{i}]", "kind": "group_key_reduced",
                    "asserted": True,
                    "evidence": f"Group Key {gk} < query {qkeys}",
                    "claim": "dropped keys functionally determined",
                    "audit": None,
                })
                break

    return recs
