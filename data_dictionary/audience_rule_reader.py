"""
data_dictionary/audience_rule_reader.py  (AEP Swiss Army Knife)
==========================================
Library: WHAT AN AUDIENCE RULE READS -- the fields it tests and the other
audiences it builds on. Used by the Data Dictionary for the Audiences tab's
"Fields it reads" and "Builds on (audience ids)" columns.

Fields come out in the Data Dictionary's own notation, so they match the Field
Index: dot paths, an array's items as 'list[].field'. A rule reads profile
fields from the profile itself, from a profile array through select(), and
event fields from a sequence over xEvent or an array inside an event. A lookup
join (field@{class}) leaves the schema, so only the joining field is kept. The
event timestamp a sequence is ordered by is left out: every event rule has it.

Another audience is built on through inSegment(id) or through
segmentMembership[namespace][id] -- the second is how uploaded audiences are
usually referenced.

Stdlib only.
"""

from __future__ import annotations

import re

_NEXT = "\0next"            # env key: what the next lambda's variable stands for


def _lookup_path(node, env):
    """(root, [names]) for a chain of fieldLookups, root 'profile' or 'event';
    None when it does not start from the profile or a known variable."""
    names = []
    while isinstance(node, dict) and node.get("nodeType") == "fieldLookup":
        names.append(str(node.get("fieldName") or ""))
        node = node.get("object")
    if not isinstance(node, dict):
        return None
    if node.get("nodeType") == "parameterReference":
        root, prefix = "profile", []
    elif node.get("nodeType") == "varRef" and node.get("varName") in env:
        root, prefix = env[node["varName"]]
    else:
        return None
    names = prefix + names[::-1]
    if root == "profile" and names[:1] == ["xEvent"]:
        root, names = "event", names[1:]
    # A lookup join (field@{class}) leaves this schema: keep the joining field only.
    for n, name in enumerate(names):
        if "@{" in name:
            names = names[:n] + [name.split("@{", 1)[0]]
            break
    return root, names


def _element(p):
    """What a variable over the array `p` stands for: one item of it."""
    root, names = p
    return (root, names[:-1] + [names[-1] + "[]"]) if names else (root, names)


def field_refs(tree) -> set[tuple[str, str]]:
    """Every (root, dotted path) a pql/json rule reads; root is 'profile' or 'event'."""
    refs = set()

    def walk(node, env):
        if isinstance(node, list):
            for x in node:
                walk(x, env)
            return
        if not isinstance(node, dict):
            return
        nt = node.get("nodeType")
        if nt == "fieldLookup":
            p = _lookup_path(node, env)
            if p and p[1]:
                refs.add((p[0], ".".join(p[1])))
            base = node
            while isinstance(base, dict) and base.get("nodeType") == "fieldLookup":
                base = base.get("object")
            if isinstance(base, dict) and base.get("nodeType") not in ("parameterReference", "varRef"):
                walk(base, env)         # a lookup on a function's result, e.g. get(...).status
            return
        if nt == "lambda":
            env = dict(env)
            bound = env.pop(_NEXT, None)
            for v in node.get("varNames") or []:
                if bound is not None:
                    env[v] = bound
            walk(node.get("body"), env)
            return
        if nt == "select":
            env = dict(env)
            decls = [v for v in node.get("variables") or [] if isinstance(v, dict)]
            for v in decls:
                p = _lookup_path(v.get("from"), env)
                if p and v.get("varName"):
                    env[v["varName"]] = _element(p)
            for v in decls:
                walk(v.get("where"), env)
            walk([v for k, v in node.items() if k != "variables"], env)
            return
        if nt == "chain":
            p = _lookup_path(node.get("array"), env)
            walk(node.get("elements"), {**env, _NEXT: _element(p) if p and p[1] else ("event", [])})
            return
        params = node.get("params") if nt == "fnApply" else None
        if params and any(isinstance(x, dict) and x.get("nodeType") == "lambda" for x in params):
            arr = next((x for x in params if isinstance(x, dict) and x.get("nodeType") == "fieldLookup"), None)
            p = _lookup_path(arr, env) if arr else None
            inner = {**env, _NEXT: _element(p)} if p and p[1] else env
            for x in params:
                walk(x, inner if isinstance(x, dict) and x.get("nodeType") == "lambda" else env)
            return
        walk(list(node.values()), env)

    walk(tree, {})
    return refs


def membership_ids(tree) -> list[str]:
    """Audience ids a rule reads through segmentMembership[namespace][id]."""
    ids = []
    stack = [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(n)
        elif isinstance(n, dict):
            stack.extend(n.values())
            ps = n.get("params") or []
            inner = ps[0] if n.get("fnName") == "get" and len(ps) == 2 else None
            if (isinstance(inner, dict) and inner.get("fnName") == "get" and inner.get("params")
                    and (inner["params"][0] or {}).get("fieldName") == "segmentMembership"
                    and isinstance(ps[1], dict) and ps[1].get("value")):
                ids.append(str(ps[1]["value"]))
    return ids


_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_INSEG = re.compile(r'inSegment\(\s*"([^"]+)"')


def text_refs(text: str) -> tuple[set[tuple[str, str]], list[str]]:
    """Best effort for the few rules stored as PQL text: dotted names and
    inSegment() ids."""
    refs = set()
    for m in _DOTTED.findall(text or ""):
        refs.add(("event", m[len("xEvent."):]) if m.startswith("xEvent.") else ("profile", m))
    return refs, _INSEG.findall(text or "")


def fields_read(tree=None, text: str = "") -> list[str]:
    """The dotted paths a rule reads, sorted. segmentMembership is an audience,
    not a field (membership_ids() has it); an array or object is left out when
    a field inside it is read (exists(productListItems, x -> x.SKU ...))."""
    refs = field_refs(tree) if tree is not None else text_refs(text)[0]
    paths = {p for _, p in refs if p.split(".")[0] != "segmentMembership"}
    return sorted(p for p in paths
                  if not any(q.startswith(p + ".") or q.startswith(p + "[].") for q in paths))
