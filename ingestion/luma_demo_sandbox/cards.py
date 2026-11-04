"""The rule builder's card layout (ansibleDataModel) for the demo audiences.

The AEP UI only opens an audience in Rule view when it carries this layout;
without it the UI says "can't be updated in the rule builder, edits must be
made in the API". The layout is undocumented: the shapes here were copied from
audiences built in the UI (profile attributes, "exists", audience membership,
events with an "in last N days" window, two events) and checked by opening the
result in the UI.

The layout's "hash" is the Java/JS String.hashCode of the stored pql/json rule
(checked against UI-built audiences). The UI only trusts a layout whose hash
matches the rule; otherwise it re-parses the rule itself, which works for
profile rules and fails for any event rule.

Each event, and each excluded event (not(CHAIN(...))), is its own card. A
window shared by every event goes on the Events section, as the UI puts it;
otherwise each card carries its own. card_model() returns None for a rule it
can't lay out (e.g. a window other than "in last N"); that one stays
code-view only rather than get cards that show something else.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

EQUALS_ANY = re.compile(r'^\(?((?:[\w.]+\.equals\("[^"]*", false\)(?: or )?)+)\)?$')
EQUALS_ONE = re.compile(r'([\w.]+)\.equals\("([^"]*)", false\)')
COMPARE = re.compile(r"^([\w.]+) (>=|<=|>|<) (-?\d+(?:\.\d+)?)$")
EXISTS = re.compile(r"^([\w.]+)\.isNotNull\(\)$")
LIST_STARTS = re.compile(r'^([\w.]+)\.exists\((\w+)\.startsWith\("([^"]*)", false\)\)$')
LIST_COMPARE = re.compile(r"^([\w.]+)\.exists\((\w+) (>=|<=|>|<) (-?\d+(?:\.\d+)?)\)$")
IN_SEGMENT = re.compile(r'^inSegment\("([^"]+)"\)$')
EVENT = re.compile(r"^CHAIN\(xEvent, timestamp, \[C0: WHAT\((.+?)\)(?: WHEN\((.+?)\))?\]\)$")
NEVER = re.compile(r"^not\((CHAIN\(.+\))\)$")
IN_LAST = re.compile(r"^<= (\d+) (days|hours) before now$")


def _number(text: str) -> int | float:
    return float(text) if "." in text else int(text)


def _rule(component: str, comparison: str, value: Any, **extra: Any) -> dict[str, Any]:
    return {"value": value, "itemType": "segmentRule",
            "component": {"id": component, "type": "hC", "__entity__": True},
            "isPlaceholder": False, "comparisonType": comparison, **extra, "originalLocation": []}


def _attribute(cond: str, prefix: str) -> dict[str, Any] | None:
    """One condition on a profile (prefix 'profile') or event ('xEvent') field."""
    m = EQUALS_ANY.match(cond)
    if m:
        pairs = EQUALS_ONE.findall(m.group(1))
        fields = {f for f, _ in pairs}
        return _rule(f"{prefix}.{fields.pop()}", "equals", [v for _, v in pairs]) if len(fields) == 1 else None
    m = COMPARE.match(cond)
    if m:
        return _rule(f"{prefix}.{m.group(1)}", m.group(2), _number(m.group(3)))
    m = EXISTS.match(cond)
    if m:      # the UI's "exists"
        return _rule(f"{prefix}.{m.group(1)}", "isNotNull", [], isCaseSensitive=False)
    m = LIST_STARTS.match(cond)
    if m:      # an item of an array, e.g. productListItems SKU starts with
        return _rule(f"{prefix}.{m.group(1)}.{m.group(2)}", "startsWith", [m.group(3)])
    m = LIST_COMPARE.match(cond)
    if m:
        return _rule(f"{prefix}.{m.group(1)}.{m.group(2)}", m.group(3), _number(m.group(4)))
    return None


def split_top(pql: str, op: str) -> list[str]:
    """Top-level ' and ' / ' or ' parts (not inside brackets or quotes), outer brackets dropped."""
    pql = pql.strip()
    while pql.startswith("(") and pql.endswith(")") and _closes_at_end(pql):
        pql = pql[1:-1].strip()
    sep = f" {op} "
    parts, depth, quoted, start, i = [], 0, False, 0, 0
    while i < len(pql):
        c = pql[i]
        if c == '"':
            quoted = not quoted
        elif not quoted and c in "([":
            depth += 1
        elif not quoted and c in ")]":
            depth -= 1
        elif not quoted and depth == 0 and pql.startswith(sep, i):
            parts.append(pql[start:i])
            start = i + len(sep)
            i += len(sep)
            continue
        i += 1
    return parts + [pql[start:]]


def _closes_at_end(pql: str) -> bool:
    depth, quoted = 0, False
    for i, c in enumerate(pql):
        if c == '"':
            quoted = not quoted
        elif not quoted and c == "(":
            depth += 1
        elif not quoted and c == ")":
            depth -= 1
            if depth == 0 and i < len(pql) - 1:
                return False
    return True


def _conditions(cond: str, prefix: str) -> tuple[list[dict[str, Any]], str] | None:
    """The rules of an and/or of conditions; a nested and/or becomes a group."""
    whole = _attribute(cond.strip(), prefix)       # e.g. tier equals gold or platinum: one card
    if whole:
        return [whole], "and"
    for op in ("and", "or"):
        parts = split_top(cond, op)
        if len(parts) > 1:
            break
    else:
        one = _attribute(parts[0].strip(), prefix)
        return ([one], "and") if one else None
    items = []
    for part in parts:
        if len(split_top(part, "and")) > 1 or len(split_top(part, "or")) > 1:
            group = _conditions(part, prefix)
            if group is None:
                return None
            items.append({"items": group[0], "exclude": False, "itemType": "segmentContainer",
                          "logicalOperator": group[1]})
            continue
        one = _attribute(split_top(part, "and")[0].strip(), prefix)
        if one is None:
            return None
        items.append(one)
    return items, op


def _membership(segment_id: str, name: str) -> dict[str, Any]:
    value = {"id": segment_id, "audienceId": segment_id, "name": name, "namespace": "AEPSegments",
             "status": "PUBLISHED", "schema": {"name": "_xdm.context.profile"}}
    return _rule("profile.segmentMembership.segmentID._id", "in", value)


def _window(when: str | None) -> dict[str, Any] | None | bool:
    """WHEN(...) -> the UI's in-last window; None when there is none; False when it isn't one."""
    if not when:
        return None
    m = IN_LAST.match(when)
    if not m:
        return False
    return {"type": "in-last", "unit": m.group(2), "subType": "in-last-constraint", "quantity": int(m.group(1))}


def _event_card(rules: list[dict[str, Any]], operator: str, n: int, exclude: bool) -> dict[str, Any]:
    return {"items": [{
        "id": str(uuid.uuid4()).upper(), "itemType": "eventTypeCard",
        "eventType": {"id": "__ansibleAnyEventType", "type": "Iz", "__entity__": True},
        "container": {
            "items": rules, "mapKey": "", "exclude": False, "subType": "array",
            "itemType": "multiValueParentContainer", "xdmPathIds": ["xEvent"],
            "variableRef": {"id": str(uuid.uuid4()).upper(), "name": f"_Any{n}", "type": "event",
                            "itemType": "variable", "friendlyName": f"Any{n}", "isMapVariable": False,
                            "payloadExports": []},
            "useAnyMapKey": False, "isPlaceholder": False, "logicalOperator": operator,
            "occurrenceCount": 1, "originalLocation": [], "occurrenceOperator": "atLeast",
            "occurrenceCountUpper": 2,
            "multiValuedParentsAllData": [{"id": "xEvent", "xdmPathIds": ["xEvent"]}],
            "multiValuedParentFriendlyName": "XDM ExperienceEvent"}}],
        "exclude": exclude, "itemType": "eventTypeCardContainer", "logicalOperator": "and"}


def rule_hash(stored_rule: str) -> int:
    """String.hashCode of the stored pql/json rule, as a signed 32-bit int."""
    h = 0
    for ch in stored_rule:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def card_model(pql: str, segment_names: dict[str, str], stored_rule: str = "") -> dict[str, Any] | None:
    """The card layout for this rule, or None when it can't be laid out exactly.

    segment_names maps audience id -> name, for inSegment(...) cards;
    stored_rule is the audience's pql/json value, which the hash is taken from.
    """
    profile: list[dict[str, Any]] = []
    events: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for part in split_top(pql, "and"):
        part = part.strip()
        m = IN_SEGMENT.match(part)
        if m:
            profile.append(_membership(m.group(1), segment_names.get(m.group(1), m.group(1))))
            continue
        never = NEVER.match(part)
        m = EVENT.match(never.group(1) if never else part)
        if m:
            laid = _conditions(m.group(1), "xEvent")
            window = _window(m.group(2))
            if laid is None or window is False:
                return None
            events.append((_event_card(*laid, len(events) + 1, bool(never)), window))
            continue
        laid = _conditions(part, "profile")
        if laid is None:
            return None
        profile += laid[0] if laid[1] == "and" else [{"items": laid[0], "exclude": False,
                                                      "itemType": "segmentContainer", "logicalOperator": "or"}]
    xevents: dict[str, Any] = {"items": [card for card, _ in events], "exclude": False,
                               "itemType": "eventTypeCardContainer",
                               "logicalOperator": "then" if len(events) < 2 else "and"}
    windows = [w for _, w in events]
    if windows and all(w == windows[0] for w in windows):
        if windows[0]:
            xevents["temporalConstraint"] = windows[0]
    else:
        for card, w in events:
            if w:
                card["temporalConstraint"] = w
    return {"hash": rule_hash(stored_rule), "version": "1.0.0", "dataModel": {"expression": {
        "isValid": True, "itemType": "segmentDefinition", "logicalOperator": "and",
        "xEventAttributesContainer": xevents,
        "profileAttributesContainer": {"items": profile, "exclude": False, "itemType": "segmentContainer",
                                       "logicalOperator": "and"}}}}
