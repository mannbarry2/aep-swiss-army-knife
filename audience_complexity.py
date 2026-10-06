#!/usr/bin/env python3
"""
audience_complexity.py  (AEP Swiss Army Knife)
==============================================
A crude, first-cut COMPLEXITY SCORE (0-100) for an AEP audience, from its PQL
definition and the fields the audience API returns alongside it. Beta: the
weights are a starting point to be argued with, not a measurement.

Why: the cost of an audience is invisible in its output -- AEP reports how many
profiles qualified, never how much work that took. One badly built audience
can double the nightly batch run (a 3.5 h job went to 7.3 h after a single
segment was published: a 720-day purchase scan, a product-lookup join and a
sum() aggregation, for millions of profiles, every night). The business needs
to see which audiences are that kind before they are published.

The rules follow the two miaprova.com write-ups (Oct 2026) on expensive AEP
segments. Four main cost drivers, each read off the definition:

  1. EVENT SCAN + LOOKBACK   -- an event-sequence / time-window clause over
     ExperienceEvents. The longer the window the more events are read, for
     every profile, every run. 720 days ~ 2x the work of 365, 4x of 180. A
     scan with NO time limit reads the whole history and scores highest.
  2. LOOKUP JOINS            -- a field reached through a relationship to
     another class (the product class via gtin, say). The join runs per event
     per profile: "potentially millions of times across the job run".
  3. AGGREGATIONS            -- sum() / average() / count() cannot short-
     circuit: every qualifying event must be gathered before the threshold
     test. "Occurs at least N times" is a count in disguise.
  4. BASE AUDIENCE SIZE      -- inSegment() on a big audience means every one
     of those profiles gets the full scan before the result is known.

Plus smaller structural signals: event sequences (several steps in order),
sheer number of conditions, huge hard-coded value lists, more than one merge
policy, a negated event step ("never bought"), and Adobe's own performance
warning having been overridden at save time.

Usage (pure functions, no network):
    from audience_complexity import score_audience
    result = score_audience(audience_payload, pql_tree, base_populations)
    result["score"], result["rag"], result["why"], result["features"]

`audience_payload` is one item of GET /data/core/ups/audiences (dataRefPaths,
mergePolicies, dependencies, overridePerformanceWarnings are read);
`pql_tree` is the parsed pql/json expression (or None); `base_populations`
maps audience id -> profile count for the audiences this one depends on.
"""

from __future__ import annotations

SCRIPT_NAME = "audience_complexity"
SCRIPT_VERSION = "0.1-beta"
SCRIPT_DATE = "2026-10-06"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

# ----------------------------------------------------------------------------
# Weights (points). Edit here; the "why" text quotes them so a reader can see
# exactly how a score was reached.
# ----------------------------------------------------------------------------
LOOKBACK_POINTS = [          # (max days, points) -- first band that fits wins
    (30, 6), (90, 12), (180, 18), (365, 25), (730, 30), (float("inf"), 33),
]
UNBOUNDED_SCAN_POINTS = 35   # event scan with no time limit at all
PROFILE_ARRAY_SCAN_POINTS = 3  # select/varDecl over a profile array (no events)

JOIN_FIRST_POINTS = 18
JOIN_EXTRA_POINTS = 4        # each further lookup class / joined path
JOIN_CAP = 25

AGG_HEAVY_POINTS = 15        # sum / average / min / max
AGG_COUNT_POINTS = 10        # count(...)
AGG_OCCURS_N_POINTS = 6      # "occurs at least N times" (a count in disguise)
AGG_FORALL_POINTS = 4        # forall / every-event test
AGG_CAP = 20

DEP_EACH_POINTS = 2          # per inSegment() reference
DEP_EACH_CAP = 6
DEP_POP_POINTS = [(1_000_000, 3), (5_000_000, 6), (20_000_000, 9)]  # >= profiles

SEQUENCE_POINTS = 5          # 2+ ordered steps, or 2+ separate event scans
CONDITIONS_POINTS = [(30, 3), (80, 5)]   # > n comparison conditions
BIG_LIST_POINTS = 3          # a hard-coded list of >= BIG_LIST values
BIG_LIST = 100
BREADTH_CAP = 10

MULTI_MERGE_POINTS = 3
PERF_OVERRIDE_POINTS = 5     # Adobe's performance warning was overridden
NEGATED_SCAN_POINTS = 3      # "never did X": the whole window must be read
OTHER_CAP = 8

RAG_RED = 55                 # score >= RED -> RED, >= AMBER -> AMBER, else GREEN
RAG_AMBER = 30

PROFILE_CLASS = "_xdm.context.profile"
EVENT_CLASS = "_xdm.context.experienceevent"
HEAVY_AGGS = {"sum", "average", "avg", "min", "max"}
COMPARISONS = {"=", "!=", ">", "<", ">=", "<=", "equals", "notEqualTo", "in",
               "notIn", "contains", "doesNotContain", "startsWith", "endsWith",
               "stringCompare", "exists", "isNull", "isNotNull"}
UNIT_DAYS = {"seconds": 1 / 86400, "minutes": 1 / 1440, "hours": 1 / 24,
             "days": 1, "weeks": 7, "months": 30, "years": 365}


# ----------------------------------------------------------------------------
# Tree walking
# ----------------------------------------------------------------------------
def _walk(node):
    """Every dict node in the tree, depth-first."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _duration_days(d: dict):
    cnt = (d.get("count") or {}).get("value")
    unit = (d.get("unit") or {}).get("value")
    mult = UNIT_DAYS.get(unit)
    if isinstance(cnt, (int, float)) and mult:
        return cnt * mult
    return None


def analyse_tree(tree) -> dict:
    """Structural features of one pql/json tree. All counts, no judgement."""
    f = {"chains": 0, "sequence_steps": 0, "unbounded_scan": False,
         "lookback_days": None, "occurs_n": 0, "negated_steps": 0,
         "profile_array_scans": 0, "heavy_aggs": [], "counts": 0, "foralls": 0,
         "joins": set(), "insegment": [], "conditions": 0, "max_list": 0,
         "nodes": 0}
    if not isinstance(tree, (dict, list)):
        return f
    for n in _walk(tree):
        f["nodes"] += 1
        nt = n.get("nodeType")
        if nt == "chain":
            f["chains"] += 1
            els = n.get("elements") or []
            f["sequence_steps"] = max(f["sequence_steps"], len(els))
            bounded = False
            for sub in _walk(n):
                if sub.get("nodeType") in ("timeQualification", "range"):
                    bounded = True
                if sub.get("nodeType") == "duration":
                    days = _duration_days(sub)
                    if days is not None:
                        f["lookback_days"] = max(f["lookback_days"] or 0, days)
            if not bounded:
                f["unbounded_scan"] = True
            for el in els:
                if isinstance(el, dict):
                    if (el.get("count") or 1) > 1:
                        f["occurs_n"] += 1
                    if el.get("negated"):
                        f["negated_steps"] += 1
        elif nt == "select":
            f["profile_array_scans"] += len(n.get("variables") or [])
        elif nt == "fieldLookup" and "@{" in str(n.get("fieldName") or ""):
            f["joins"].add(str(n["fieldName"]).split("@{", 1)[1].rstrip("}"))
        elif nt == "fnApply":
            fn = n.get("fnName")
            if fn in HEAVY_AGGS:
                f["heavy_aggs"].append(fn)
            elif fn == "count":
                f["counts"] += 1
            elif fn == "forall":
                f["foralls"] += 1
            elif fn == "inSegment":
                sid = ((n.get("params") or [{}])[0] or {}).get("value")
                if sid:
                    f["insegment"].append(str(sid))
            elif fn in COMPARISONS:
                f["conditions"] += 1
        elif nt == "literal" and n.get("literalType") == "List":
            v = n.get("value")
            if isinstance(v, list):
                f["max_list"] = max(f["max_list"], len(v))
    return f


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------
def _fmt_pop(n) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n:,}"


def score_audience(aud: dict, tree, base_populations: dict | None = None,
                   names: dict | None = None) -> dict:
    """Score one audience. Returns {score, rag, why, features} where `why` is
    a plain-English breakdown quoting the points, and `features` the raw
    counts the Audiences sheet shows alongside the score."""
    base_populations = base_populations or {}
    names = names or {}
    f = analyse_tree(tree)
    refs = aud.get("dataRefPaths") if isinstance(aud.get("dataRefPaths"), dict) else {}
    classes = set(refs.keys())
    lookup_classes = {c for c in classes if c not in (PROFILE_CLASS, EVENT_CLASS)}
    event_scan = f["chains"] > 0 or EVENT_CLASS in classes
    deps = [str(d) for d in (aud.get("dependencies") or [])] or f["insegment"]
    merge_policies = aud.get("mergePolicies") or {}
    n_merge = len(merge_policies) if isinstance(merge_policies, dict) else 0
    perf_override = bool(aud.get("overridePerformanceWarnings"))

    why, score = [], 0

    # 1. event scan + lookback
    lookback = f["lookback_days"]
    unbounded = event_scan and (f["unbounded_scan"] or (f["chains"] and lookback is None))
    if event_scan:
        if unbounded:
            pts = UNBOUNDED_SCAN_POINTS
            why.append(f"event scan with NO time limit: whole history read every run (+{pts})")
        elif lookback is not None:
            pts = next(p for lim, p in LOOKBACK_POINTS if lookback <= lim)
            why.append(f"event scan over {lookback:g} days (+{pts})")
        else:
            pts = LOOKBACK_POINTS[0][1]
            why.append(f"reads ExperienceEvents (+{pts})")
        score += pts
    elif f["profile_array_scans"]:
        score += PROFILE_ARRAY_SCAN_POINTS
        why.append(f"scans a profile array x{f['profile_array_scans']} (+{PROFILE_ARRAY_SCAN_POINTS})")

    # 2. lookup joins
    joins = len(lookup_classes) or len(f["joins"])
    join_paths = sum(len(v or []) for c, v in refs.items() if c in lookup_classes)
    if joins:
        pts = min(JOIN_CAP, JOIN_FIRST_POINTS
                  + JOIN_EXTRA_POINTS * (joins - 1 + max(0, join_paths - 1)))
        score += pts
        why.append(f"{joins} lookup join(s) to another class, "
                   f"{join_paths or '?'} field(s) resolved per event (+{pts})")

    # 3. aggregations
    agg, bits = 0, []
    if f["heavy_aggs"]:
        agg += AGG_HEAVY_POINTS
        bits.append(f"{'/'.join(sorted(set(f['heavy_aggs'])))}() (+{AGG_HEAVY_POINTS})")
    if f["counts"]:
        agg += AGG_COUNT_POINTS
        bits.append(f"count() x{f['counts']} (+{AGG_COUNT_POINTS})")
    if f["occurs_n"]:
        agg += AGG_OCCURS_N_POINTS
        bits.append(f"'occurs N times' x{f['occurs_n']} (+{AGG_OCCURS_N_POINTS})")
    if f["foralls"]:
        agg += AGG_FORALL_POINTS
        bits.append(f"forall x{f['foralls']} (+{AGG_FORALL_POINTS})")
    agg = min(AGG_CAP, agg)
    if agg:
        score += agg
        why.append("aggregation that cannot short-circuit: " + ", ".join(bits))

    # 4. dependencies and base size
    biggest, biggest_name = 0, ""
    for d in deps:
        p = base_populations.get(d)
        if isinstance(p, (int, float)) and p > biggest:
            biggest, biggest_name = int(p), names.get(d, d[:8])
    if deps:
        pts = min(DEP_EACH_CAP, DEP_EACH_POINTS * len(deps))
        pop_pts = 0
        for lim, p in DEP_POP_POINTS:
            if biggest >= lim:
                pop_pts = p
        score += pts + pop_pts
        text = f"depends on {len(deps)} audience(s) (+{pts})"
        if pop_pts:
            text += (f"; largest base '{biggest_name}' = {_fmt_pop(biggest)} "
                     f"profiles, all of them scanned (+{pop_pts})")
        why.append(text)

    # 5. breadth
    breadth, bits = 0, []
    if f["sequence_steps"] >= 2 or f["chains"] >= 2:
        breadth += SEQUENCE_POINTS
        bits.append(f"event sequence / {max(f['sequence_steps'], f['chains'])} "
                    f"event clauses (+{SEQUENCE_POINTS})")
    cond_pts = 0
    for lim, p in CONDITIONS_POINTS:
        if f["conditions"] > lim:
            cond_pts = p
    if cond_pts:
        breadth += cond_pts
        bits.append(f"{f['conditions']} conditions (+{cond_pts})")
    if f["max_list"] >= BIG_LIST:
        breadth += BIG_LIST_POINTS
        bits.append(f"hard-coded list of {f['max_list']} values (+{BIG_LIST_POINTS})")
    breadth = min(BREADTH_CAP, breadth)
    if breadth:
        score += breadth
        why.append("breadth: " + ", ".join(bits))

    # 6. other
    other, bits = 0, []
    if n_merge > 1:
        other += MULTI_MERGE_POINTS
        bits.append(f"{n_merge} merge policies (+{MULTI_MERGE_POINTS})")
    if perf_override:
        other += PERF_OVERRIDE_POINTS
        bits.append(f"Adobe performance warning OVERRIDDEN at save (+{PERF_OVERRIDE_POINTS})")
    if f["negated_steps"]:
        other += NEGATED_SCAN_POINTS
        bits.append(f"negated event step ('never did X') (+{NEGATED_SCAN_POINTS})")
    other = min(OTHER_CAP, other)
    if other:
        score += other
        why.append(", ".join(bits))

    score = min(100, score)
    rag = "RED" if score >= RAG_RED else "AMBER" if score >= RAG_AMBER else "GREEN"
    if tree is None:
        score, rag, why = "", "", []
    features = {
        "event_scan": "yes" if event_scan else "",
        "lookback_days": ("none (unbounded)" if unbounded
                          else (round(lookback) if lookback is not None else "")),
        "lookup_joins": joins,
        "lookup_fields": join_paths,
        "aggregations": ", ".join(sorted(set(f["heavy_aggs"]))
                                  + (["count"] if f["counts"] else [])
                                  + (["occurs N times"] if f["occurs_n"] else [])
                                  + (["forall"] if f["foralls"] else [])),
        "depends_on": len(deps),
        "largest_base": biggest or "",
        "largest_base_name": biggest_name,
        "sequence_steps": f["sequence_steps"] if f["chains"] else "",
        "conditions": f["conditions"],
        "max_list": f["max_list"] or "",
        "merge_policies": n_merge,
        "perf_override": "yes" if perf_override else "",
        "dependents": len(aud.get("dependents") or []),
    }
    return {"score": score, "rag": rag, "why": "; ".join(why), "features": features}
