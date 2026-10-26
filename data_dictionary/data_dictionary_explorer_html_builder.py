#!/usr/bin/env python3
"""
data_dictionary/data_dictionary_explorer_html_builder.py  (AEP Swiss Army Knife)
==========================================================
PROTOTYPE. Turns a Data Dictionary workbook into ONE web page people can use
without training: a search box, a page for every field, schema, dataset and
audience, and lineage you can click along --

    dataset -> schema -> field -> audiences that read it -> audiences built on those

Reads only the Data Dictionary's own output (the workbook and its PQL file),
so it makes no Adobe calls and takes seconds. The page is a single .html file
with everything inside it: no server, no install, no internet needed. Open it
in Edge or Chrome from the synced OneDrive folder (SharePoint and Teams
download .html files rather than show them).

Where the data came from (sources, dataflows) and where audiences go
(destinations, journeys) are not in the workbook yet; the page shows those
columns as "next step".

Stdlib + openpyxl only.

Usage:
    python data_dictionary/data_dictionary_explorer_html_builder.py --sandbox=roi-prod
    python data_dictionary/data_dictionary_explorer_html_builder.py --workbook="...xlsx" [--pql="...jsonl"] [--out="...html"]
The newest "Data Dictionary - <sandbox> - *.xlsx" in AEP_OUTPUT_DIR/data_dictionary
(else output/data_dictionary) is used; the page is written next to it as
"Data Dictionary Explorer - <sandbox> - <date>.html".
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from data_dictionary import audience_complexity_score as acs  # noqa: E402

SCRIPT_NAME = "data_dictionary_explorer_html_builder"
SCRIPT_VERSION = "0.1.0"
SCRIPT_DATE = "2026-10-25"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

RAW_PQL_MAX = 60_000        # characters of raw rule kept per audience (the rest is in the PQL file)
_NEXT = "\0next"            # env key: what the next lambda's variable stands for


def opt(name, default=None):
    return next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith(f"--{name}=")), default)


# ----------------------------------------------------------------------------
# Reading the workbook
# ----------------------------------------------------------------------------
def _clean(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _table(ws, first_header: str) -> list[dict]:
    """Rows of a table sheet as dicts. The header is the first row whose first
    cell is `first_header`; blank rows and one-cell section rows are skipped."""
    rows = ws.iter_rows(values_only=True)
    for r in rows:
        if r and _clean(r[0]) == first_header:
            head = [str(c or "").strip() for c in r]
            break
    else:
        return []
    out = []
    for r in rows:
        vals = [_clean(v) for v in r]
        if sum(v is not None for v in vals) < 2:
            continue
        out.append({h: v for h, v in zip(head, vals) if h})
    return out


def _num(v):
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        f = float(str(v).replace(",", ""))
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


def read_summary(ws) -> dict:
    meta = {"retention": [], "gaps": []}
    lines = [[_clean(v) for v in r] for r in ws.iter_rows(values_only=True)]
    in_gaps = False
    for r in lines:
        a = str(r[0] or "") if r else ""
        if a.startswith("Data Dictionary v"):
            meta["title"] = a
        elif a.startswith("Generated "):
            meta["generated"] = a
        elif "Profile keeps" in a and not a.startswith("PROFILE RETENTION"):
            meta["retention"].append(a)
        elif a.startswith("DATA COMPLETENESS"):
            in_gaps = True
        elif in_gaps and a == "Schema":
            continue
        elif in_gaps and a and r[3]:
            meta["gaps"].append({"schema": a, "tab": r[1], "status": r[3]})
        elif in_gaps and not a:
            in_gaps = False
    return meta


def read_workbook(path: Path) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    meta = read_summary(wb["Summary"])
    schemas = _table(wb["Schemas"], "Tab")
    datasets = _table(wb["Datasets"], "Sandbox")
    fields = _table(wb["Field Index"], "Field (dot notation)")
    hist_ws = wb["Field History"] if "Field History" in wb.sheetnames else None
    hist = _table(hist_ws, "Field (dot notation)") if hist_ws else []
    hist_head = []
    if hist_ws:
        for r in hist_ws.iter_rows(values_only=True):
            if r and r[0] == "Field (dot notation)":
                hist_head = [str(c) for c in r[6:] if c]
                break
    audiences = _table(wb["Audiences"], "Sandbox") if "Audiences" in wb.sheetnames else []
    tabs = {}
    for s in schemas:
        tab = s.get("Tab")
        if tab in wb.sheetnames:
            tabs[tab] = {r.get("Field (dot notation)"): r for r in _table(wb[tab], "Field (dot notation)")}
    wb.close()
    return {"meta": meta, "schemas": schemas, "datasets": datasets, "fields": fields,
            "hist": hist, "months": hist_head, "audiences": audiences, "tabs": tabs}


def read_pql(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r.get("audience_id")] = r
    return out


# ----------------------------------------------------------------------------
# Which fields an audience rule reads
# ----------------------------------------------------------------------------
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


def pql_field_refs(tree) -> set[tuple[str, str]]:
    """Every (root, dotted path) a pql/json rule reads, in the Data Dictionary's
    notation (array items as 'list[].field'). The event timestamp a sequence is
    ordered by is left out: every event rule has it."""
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


def pql_membership_ids(tree) -> list[str]:
    """Audience ids a rule reads through segmentMembership[namespace][id] --
    the other way, besides inSegment(), of building on an audience."""
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


def pql_text_refs(text: str) -> tuple[set[tuple[str, str]], list[str]]:
    """Best effort for the few rules stored as PQL text: dotted names and
    inSegment() ids."""
    refs = set()
    for m in _DOTTED.findall(text or ""):
        refs.add(("event", m[len("xEvent."):]) if m.startswith("xEvent.") else ("profile", m))
    return refs, _INSEG.findall(text or "")


# ----------------------------------------------------------------------------
# The model the page draws
# ----------------------------------------------------------------------------
def _yes(v) -> bool:
    return str(v or "").strip().lower() in ("yes", "y", "true")


def build_model(wbk: dict, pql: dict, workbook_name: str) -> dict:
    schemas, s_ix = [], {}
    for r in wbk["schemas"]:
        title = str(r.get("Schema") or "").strip()
        s_ix[title] = len(schemas)
        schemas.append({
            "tab": r.get("Tab"), "title": title, "name": re.sub(r"\s+schema$", "", title, flags=re.I) or title,
            "desc": r.get("Description"),
            "cls": r.get("Class"), "kind": r.get("Kind"), "profile": _yes(r.get("In Profile")),
            "gb": _num(r.get("Data lake GB")), "rows": _num(r.get("Data lake rows")),
            "ttl": _num(r.get("Data lake TTL (days)")), "pgb": _num(r.get("Profile GB")),
            "pttl": _num(r.get("Profile TTL (days)")), "tables": r.get("SQL table names"),
            "nid": _num(r.get("Identities")), "nrel": _num(r.get("Relationships")),
            "mod": r.get("Last Modified"), "ds": [], "f": []})

    datasets = []
    for r in wbk["datasets"]:
        s = s_ix.get(str(r.get("Schema") or "").strip(), -1)
        if s >= 0:
            schemas[s]["ds"].append(len(datasets))
        datasets.append({
            "name": r.get("Friendly Name (dataset)"), "table": r.get("Table Name (SQL / system)"),
            "desc": r.get("Description"), "schema": r.get("Schema"), "s": s, "kind": r.get("Kind"),
            "profile": r.get("Profile"), "gb": _num(r.get("Data lake GB")),
            "rows": _num(r.get("Data lake rows")), "ttl": _num(r.get("Data lake TTL (days)")),
            "pgb": _num(r.get("Profile GB")), "pttl": _num(r.get("Profile TTL (days)")),
            "id": r.get("Dataset ID")})

    month_col = next((k for k in (wbk["fields"][0] if wbk["fields"] else {}) if k.startswith("Month by month")), None)
    months = wbk["months"]
    hist = {(h.get("Field (dot notation)"), str(h.get("Schema") or "").strip()): h for h in wbk["hist"]}
    fields, f_ix = [], {}
    for r in wbk["fields"]:
        path = r.get("Field (dot notation)")
        title = str(r.get("Schema") or "").strip()
        s = s_ix.get(title, -1)
        if not path or s < 0:
            continue
        if path not in f_ix:
            f_ix[path] = len(fields)
            fields.append({"path": path, "name": r.get("Friendly Name"), "fpath": r.get("Friendly Path"),
                           "desc": r.get("Description"), "type": r.get("Data Type"), "on": [], "aud": []})
        f = fields[f_ix[path]]
        f["name"] = f["name"] or r.get("Friendly Name")
        f["desc"] = f["desc"] or r.get("Description")
        h = hist.get((path, title), {})
        tab_row = wbk["tabs"].get(schemas[s]["tab"], {}).get(path, {})
        f["on"].append({
            "s": s, "ident": r.get("Identity"), "req": _yes(tab_row.get("Required")),
            "rel": tab_row.get("Relationship → target"),
            "cov": _num(r.get("Coverage %")), "top": r.get("Top values (count)"),
            "months": _num(r.get("Have we got it? (months)")), "hdet": r.get("History detail"),
            "pdays": _num(r.get("In Profile now (days)")), "pdet": r.get("Profile detail"),
            "hist": [h.get(m) for m in months] if h else [],
            "spark": r.get(month_col) if month_col else None,
            "sql": r.get("SQL to check it (paste into Query Service)")})
        if f_ix[path] not in schemas[s]["f"]:
            schemas[s]["f"].append(f_ix[path])

    containers = {p.rsplit(".", 1)[0] for p in f_ix if "." in p}
    containers |= {c.removesuffix("[]") for c in containers}

    audiences, a_ix = [], {}
    for r in wbk["audiences"]:
        aid = r.get("Audience id")
        side = pql.get(aid, {})
        raw = side.get("raw_pql") or r.get("PQL (raw pql/json)") or ""
        fmt = side.get("format", r.get("PQL format")) or ""
        refs, deps = set(), []
        if fmt == "json" and raw:
            try:
                tree = json.loads(raw)
                refs = pql_field_refs(tree)
                deps = acs.analyse_tree(tree)["insegment"] + pql_membership_ids(tree)
            except (ValueError, TypeError):
                pass
        elif raw:
            refs, deps = pql_text_refs(raw)
        hit = sorted({f_ix[p] for _, p in refs if p in f_ix})
        missing = sorted({p for _, p in refs if p not in f_ix and p not in containers
                          and p.split(".")[0] != "segmentMembership"})
        if fmt != "json":
            missing = []            # text rules: only keep names we could match
        a_ix[aid] = len(audiences)
        audiences.append({
            "id": aid, "name": r.get("Audience name"), "desc": r.get("Description"),
            "eval": r.get("Evaluation"), "life": r.get("Lifecycle"), "tags": r.get("Tags"),
            "origin": r.get("Origin"), "by": r.get("Created by"), "mod": r.get("Last modified by"),
            "rule": side.get("readable_pql") or r.get("PQL (readable)"),
            "raw": raw[:RAW_PQL_MAX], "rawcut": len(raw) > RAW_PQL_MAX, "fmt": fmt,
            "score": _num(r.get("Complexity score (0-100)")), "rag": r.get("Complexity RAG"),
            "why": r.get("Why this score"), "lookback": _num(r.get("Lookback (days)")),
            "base": r.get("Largest base audience"), "basen": _num(r.get("Largest base (profiles)")),
            "f": hit, "miss": missing, "depids": deps, "dep": [], "used": []})
    for i, a in enumerate(audiences):
        a["dep"] = sorted({a_ix[d] for d in a.pop("depids") if d in a_ix and a_ix[d] != i})
        for d in a["dep"]:
            audiences[d]["used"].append(i)
        for f in a["f"]:
            fields[f]["aud"].append(i)

    meta = wbk["meta"]
    title = meta.get("title") or ""
    label = title.split("–", 1)[1].strip() if "–" in title else ""
    return {"meta": {"title": title, "label": label, "generated": meta.get("generated"),
                     "workbook": workbook_name, "months": months, "retention": meta["retention"],
                     "gaps": meta["gaps"], "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
                     "builder": f"{SCRIPT_NAME} v{SCRIPT_VERSION}"},
            "schemas": schemas, "datasets": datasets, "fields": fields, "audiences": audiences}


def render_html(model: dict) -> str:
    data = json.dumps(model, ensure_ascii=False, separators=(",", ":"), default=str)
    data = data.replace("</", "<\\/")
    label = model["meta"]["label"] or "Data Dictionary"
    return (PAGE.replace("{{TITLE}}", html.escape(f"Data Dictionary Explorer – {label}"))
                .replace("{{AUTHOR}}", html.escape(SCRIPT_AUTHOR))
                .replace("/*{{DATA}}*/", data))


def main():
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:] or not (opt("sandbox") or opt("workbook")):
        print(__doc__)
        return
    out_dir = Path(os.environ.get("AEP_OUTPUT_DIR") or REPO / "output") / "data_dictionary"
    sandbox = opt("sandbox")
    wb_path = Path(opt("workbook")) if opt("workbook") else max(
        out_dir.glob(f"Data Dictionary - {sandbox} - *.xlsx"), key=lambda p: p.stat().st_mtime, default=None)
    if not wb_path or not wb_path.exists():
        sys.exit(f"No Data Dictionary workbook for {sandbox} in {out_dir} -- run the Data Dictionary first.")
    m = re.match(r"Data Dictionary - (.+) - (\d{4}-\d{2}-\d{2})", wb_path.stem)
    sandbox = sandbox or (m.group(1) if m else "")
    pql_path = Path(opt("pql")) if opt("pql") else (
        wb_path.parent / "pql" / f"audiences_pql_{sandbox}_{m.group(2).replace('-', '')}.jsonl" if m else None)
    out = Path(opt("out")) if opt("out") else wb_path.with_name(
        wb_path.name.replace("Data Dictionary - ", "Data Dictionary Explorer - ", 1)).with_suffix(".html")

    print(f"{SCRIPT_NAME} v{SCRIPT_VERSION} ({SCRIPT_DATE})  by {SCRIPT_AUTHOR}")
    print(f"workbook {wb_path.name}  |  PQL {pql_path.name if pql_path and pql_path.exists() else '(none)'}")
    model = build_model(read_workbook(wb_path), read_pql(pql_path), wb_path.name)
    page = render_html(model)
    out.write_text(page, encoding="utf-8")
    linked = sum(1 for a in model["audiences"] if a["f"])
    print(f"{len(model['schemas'])} schemas, {len(model['datasets'])} datasets, {len(model['fields'])} fields, "
          f"{len(model['audiences'])} audiences ({linked} linked to the fields they read)")
    print(f"written: {out}  ({len(page.encode('utf-8')) / 1e6:.1f} MB)")


# ----------------------------------------------------------------------------
# The page. Plain HTML, CSS and JavaScript; the data goes where /*{{DATA}}*/ is.
# ----------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="author" content="{{AUTHOR}}">
<title>{{TITLE}}</title>
<style>
:root{
  --bg:#f5f6f8; --panel:#ffffff; --ink:#1c2230; --muted:#5d6678; --faint:#8a93a3; --line:#e2e5eb;
  --accent:#2457c5; --accent-weak:#e9effc; --focus:#ffd257;
  --k-profile:#7a4cc2; --k-event:#2a72c8; --k-other:#21857a; --k-aud:#c4572a; --k-ds:#5d6678;
  --red:#c0392b; --red-bg:#fbe9e7; --amber:#b9770e; --amber-bg:#fdf3e1; --green:#23854a; --green-bg:#e6f4ea;
  --m-none:#eceef2; --m-unk:#d5d9e0; --m-zero:#f6d4c8; --m-hi:#2e9a57;
  --edge:#b8c0cc; --edge-hot:#2457c5; --code-bg:#f2f4f7;
  --mono:Consolas,"Cascadia Mono",ui-monospace,monospace;
  --sans:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#14171d; --panel:#1c2028; --ink:#e6e9ef; --muted:#a5adbb; --faint:#7d8696; --line:#2e3440;
    --accent:#7aa2ff; --accent-weak:#22304d; --focus:#8a6d1f;
    --k-profile:#b08cf0; --k-event:#6fa8f0; --k-other:#4cc2b2; --k-aud:#f08a5d; --k-ds:#a5adbb;
    --red:#ff7b6b; --red-bg:#3a2020; --amber:#f0b44c; --amber-bg:#3a2f18; --green:#5fd08a; --green-bg:#173323;
    --m-none:#262b35; --m-unk:#3a404c; --m-zero:#5a3328; --m-hi:#4cc27a;
    --edge:#465063; --edge-hot:#7aa2ff; --code-bg:#232833;
  }
}
:root[data-theme="dark"]{
    --bg:#14171d; --panel:#1c2028; --ink:#e6e9ef; --muted:#a5adbb; --faint:#7d8696; --line:#2e3440;
    --accent:#7aa2ff; --accent-weak:#22304d; --focus:#8a6d1f;
    --k-profile:#b08cf0; --k-event:#6fa8f0; --k-other:#4cc2b2; --k-aud:#f08a5d; --k-ds:#a5adbb;
    --red:#ff7b6b; --red-bg:#3a2020; --amber:#f0b44c; --amber-bg:#3a2f18; --green:#5fd08a; --green-bg:#173323;
    --m-none:#262b35; --m-unk:#3a404c; --m-zero:#5a3328; --m-hi:#4cc27a;
    --edge:#465063; --edge-hot:#7aa2ff; --code-bg:#232833;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
code,.mono{font-family:var(--mono);font-size:13px}
.conf{background:#7d1a1a;color:#fff;font-size:11px;letter-spacing:.12em;text-align:center;padding:2px 0;font-weight:600}
header.top{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line)}
.bar{max-width:1280px;margin:0 auto;padding:10px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;white-space:nowrap;color:var(--ink)} .brand small{display:block;font-weight:400;color:var(--muted);font-size:12px}
.proto{font-size:11px;background:var(--amber-bg);color:var(--amber);border-radius:10px;padding:1px 8px;margin-left:6px;font-weight:600;vertical-align:middle}
.searchwrap{position:relative;flex:1;min-width:240px;max-width:560px}
.searchwrap input{width:100%;font:inherit;padding:8px 12px 8px 34px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
.searchwrap input:focus{outline:2px solid var(--accent);border-color:transparent}
.searchwrap svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--faint)}
.drop{position:absolute;left:0;right:0;top:calc(100% + 4px);background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.12);max-height:70vh;overflow:auto;display:none}
.drop a{display:flex;gap:10px;padding:7px 12px;color:var(--ink);align-items:baseline} .drop a.sel,.drop a:hover{background:var(--accent-weak);text-decoration:none}
.drop .sub{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drop .all{border-top:1px solid var(--line);color:var(--accent);font-size:13px}
nav.main{display:flex;gap:4px;flex-wrap:wrap} nav.main a{padding:6px 10px;border-radius:6px;color:var(--muted);font-size:14px} nav.main a.on,nav.main a:hover{background:var(--accent-weak);color:var(--accent);text-decoration:none}
main{max-width:1280px;margin:0 auto;padding:22px 20px 60px;overflow-wrap:anywhere}
h1{font-size:26px;margin:4px 0 6px;line-height:1.25} h2{font-size:18px;margin:30px 0 10px} h3{font-size:15px;margin:18px 0 8px}
.lede{color:var(--muted);max-width:820px;margin:0 0 14px}
.crumb{font-size:13px;color:var(--muted);margin-bottom:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.grid{display:grid;gap:14px} .g4{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))} .g3{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))} .g2{grid-template-columns:repeat(auto-fit,minmax(380px,1fr))}
.tile{display:block;color:var(--ink)} .tile:hover{text-decoration:none;border-color:var(--accent)}
.tile .n{font-size:30px;font-weight:700;line-height:1.1} .tile .l{color:var(--muted);font-size:13px}
.card h3{margin-top:0} .card p{margin:4px 0 10px;color:var(--muted);font-size:14px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px 18px;margin:12px 0 4px}
.facts div{font-size:13px;color:var(--muted)} .facts b{display:block;color:var(--ink);font-size:15px;font-weight:600;word-break:break-word}
.badge{display:inline-block;font-size:12px;font-weight:600;border-radius:10px;padding:1px 9px;margin-right:4px;vertical-align:middle;white-space:nowrap}
.b-profile{background:color-mix(in srgb,var(--k-profile) 16%,transparent);color:var(--k-profile)}
.b-event{background:color-mix(in srgb,var(--k-event) 16%,transparent);color:var(--k-event)}
.b-other{background:color-mix(in srgb,var(--k-other) 16%,transparent);color:var(--k-other)}
.b-aud{background:color-mix(in srgb,var(--k-aud) 16%,transparent);color:var(--k-aud)}
.b-in{background:var(--green-bg);color:var(--green)} .b-out{background:var(--m-none);color:var(--muted)}
.RED{background:var(--red-bg);color:var(--red)} .AMBER{background:var(--amber-bg);color:var(--amber)} .GREEN{background:var(--green-bg);color:var(--green)}
.say{font-size:16px;margin:10px 0;max-width:900px} .say b{font-weight:600}
.big{font-size:28px;font-weight:700;line-height:1} .big small{font-size:14px;font-weight:400;color:var(--muted)}
.copyrow{display:flex;gap:8px;align-items:flex-start}
pre.code{flex:1;min-width:0;overflow-wrap:anywhere;margin:0;background:var(--code-bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;white-space:pre-wrap;word-break:break-word;font:12.5px/1.45 var(--mono);max-height:340px;overflow:auto}
#theme{white-space:nowrap}
button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:5px 10px;cursor:pointer} button:hover{border-color:var(--accent);color:var(--accent)}
details summary{cursor:pointer;color:var(--accent);font-size:14px;margin:6px 0}
table.t{width:100%;border-collapse:collapse;font-size:14px;background:var(--panel)}
table.t th{position:sticky;top:0;background:var(--panel);text-align:left;font-size:12px;color:var(--muted);font-weight:600;border-bottom:2px solid var(--line);padding:7px 8px;cursor:pointer;white-space:nowrap;user-select:none}
table.t th.num,table.t td.num{text-align:right} table.t th:hover{color:var(--accent)} table.t th .arr{opacity:.6}
table.t td{border-bottom:1px solid var(--line);padding:7px 8px;vertical-align:top}
table.t tr:hover td{background:color-mix(in srgb,var(--accent-weak) 45%,transparent)}
table.t td .sub{color:var(--muted);font-size:12px;word-break:break-all}
.tablewrap{border:1px solid var(--line);border-radius:10px;overflow:auto;max-height:75vh}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0 10px}
.tools input{font:inherit;font-size:14px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);min-width:260px}
.chip{font-size:13px;border:1px solid var(--line);border-radius:14px;padding:3px 11px;background:var(--panel);cursor:pointer;color:var(--muted)} .chip.on{background:var(--accent-weak);border-color:var(--accent);color:var(--accent)}
.count{color:var(--muted);font-size:13px}
.covbar{display:inline-block;width:70px;height:8px;border-radius:4px;background:var(--m-none);vertical-align:middle;margin-right:6px;overflow:hidden} .covbar i{display:block;height:100%;background:var(--m-hi)}
/* month strip */
.strip{display:inline-grid;grid-auto-flow:column;grid-auto-columns:15px;gap:2px;vertical-align:middle}
.strip i{height:18px;border-radius:2px;background:var(--m-none);font-style:normal;font-size:10px;line-height:18px;text-align:center;color:var(--muted)}
.strip i.u{background:var(--m-unk)} .strip i.z{background:var(--m-zero)}
.striplab{display:flex;justify-content:space-between;font-size:11px;color:var(--faint);margin-top:2px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:8px} .legend span i{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-2px;margin-right:4px}
/* lineage */
.linwrap{position:relative;overflow-x:auto;padding:6px 2px 10px}
.lin{position:relative;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(190px,1fr);column-gap:56px;min-width:min-content}
.lin svg{position:absolute;left:0;top:0;pointer-events:none;overflow:visible}
.lin .col{position:relative;z-index:1;min-width:0}
.lin h4{margin:0 0 8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);font-weight:700}
.node{display:block;position:relative;background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--k-ds);border-radius:7px;padding:5px 9px;margin-bottom:7px;color:var(--ink);font-size:13px;line-height:1.3}
.node:hover{text-decoration:none;border-color:var(--accent);box-shadow:0 2px 8px rgba(0,0,0,.08)}
.node .l{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:10px}
.node .s{display:block;color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vals{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px} .vals span{font-size:12px;background:var(--code-bg);border:1px solid var(--line);border-radius:5px;padding:1px 7px;font-family:var(--mono)} .vals b{font-family:var(--sans);font-weight:400;color:var(--muted);margin-left:5px}
.node.k-profile{border-left-color:var(--k-profile)} .node.k-event{border-left-color:var(--k-event)} .node.k-other{border-left-color:var(--k-other)} .node.k-aud{border-left-color:var(--k-aud)}
.node.focus{border:2px solid var(--accent);border-left-width:5px;background:var(--accent-weak);font-weight:600}
.node .r{position:absolute;right:6px;top:5px;width:8px;height:8px;border-radius:50%}
.node .r.RED{background:var(--red)} .node .r.AMBER{background:var(--amber)} .node .r.GREEN{background:var(--green)}
.node.dim{opacity:.55}
.col .more{display:block;font-size:12px;padding:2px 4px}
.col .empty{font-size:12px;color:var(--faint);border:1px dashed var(--line);border-radius:7px;padding:8px 9px}
.col.ph .empty{background:repeating-linear-gradient(45deg,transparent 0 6px,color-mix(in srgb,var(--line) 50%,transparent) 6px 12px)}
path.e{fill:none;stroke:var(--edge);stroke-width:1.4} path.e.hot{stroke:var(--edge-hot);stroke-width:2.2}
.hint{font-size:12px;color:var(--faint);margin-top:4px}
.term{border-bottom:1px dotted var(--faint);cursor:help}
.foot{color:var(--faint);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:12px}
.warn{background:var(--amber-bg);color:var(--ink);border-radius:8px;padding:10px 14px;font-size:14px;margin:10px 0}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;font-size:14px} .kv dt{color:var(--muted)} .kv dd{margin:0}
.hero{padding:10px 0 6px} .hero .searchbig{max-width:720px;margin-top:12px}
.hero .searchbig input{font-size:18px;padding:13px 16px 13px 42px;border-radius:10px}
.hero .searchbig svg{left:14px}
.list-plain{margin:0;padding-left:18px} .list-plain li{margin:3px 0}
@media (max-width:760px){
  .lin{grid-auto-flow:row;grid-auto-columns:auto;row-gap:18px;min-width:0}
  .lin svg{display:none}
  .lin .col:not(:last-child)::after{content:"↓";display:block;text-align:center;color:var(--faint);font-size:18px}
  .bar{padding:8px 12px} main{padding:16px 12px 50px}
  .g2{grid-template-columns:1fr}
}
@media print{header.top{position:static} .drop,button{display:none}}
</style>
</head>
<body>
<div class="conf">STRICTLY CONFIDENTIAL</div>
<header class="top"><div class="bar">
  <a class="brand" href="#/">Data Dictionary Explorer<span class="proto">PROTOTYPE</span><small id="brand-sub"></small></a>
  <div class="searchwrap" id="topsearch"></div>
  <nav class="main" id="nav">
    <a href="#/">Home</a><a href="#/fields">Fields</a><a href="#/schemas">Schemas</a><a href="#/datasets">Datasets</a><a href="#/audiences">Audiences</a><a href="#/help">How to read this</a>
  </nav>
  <button id="theme" title="Light, dark, or follow this computer">Theme: auto</button>
</div></header>
<main id="app"></main>
<script type="application/json" id="dd-data">/*{{DATA}}*/</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("dd-data").textContent);
const M = D.meta;
const app = document.getElementById("app");
const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = v => (v === null || v === undefined || v === "") ? null : (typeof v === "number" ? v : (isNaN(+v) ? null : +v));
const fmt = (v, dp) => { const n = num(v); return n === null ? "–" : n.toLocaleString("en-GB", {maximumFractionDigits: dp ?? 1}); };
const plural = (n, one, many) => `${fmt(n, 0)} ${n === 1 ? one : (many || one + "s")}`;
const enc = encodeURIComponent;
const SEARCH_ICON = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="7" cy="7" r="5"/><path d="M11 11l3.5 3.5"/></svg>';

/* ---------- lookups ---------- */
const by = {field: {}, schema: {}, dataset: {}, audience: {}};
D.fields.forEach((f, i) => by.field[f.path] = i);
D.schemas.forEach((s, i) => by.schema[s.tab] = i);
D.datasets.forEach((d, i) => by.dataset[d.table || d.id] = i);
D.audiences.forEach((a, i) => by.audience[a.id] = i);
const href = {
  f: i => "#/field/" + enc(D.fields[i].path),
  s: i => "#/schema/" + enc(D.schemas[i].tab),
  d: i => "#/dataset/" + enc(D.datasets[i].table || D.datasets[i].id),
  a: i => "#/audience/" + enc(D.audiences[i].id),
};
const kindOf = k => ({Profile: "profile", Event: "event"})[k] || "other";
const kindWord = k => ({Profile: "Profile record", Event: "Event"})[k] || "Lookup / other";
const fieldKind = f => kindOf(D.schemas[f.on[0].s].kind);
const fieldLabel = f => f.name || f.path.split(".").pop().replace("[]", "");
const profileWord = p => p ? '<span class="badge b-in">In Profile</span>' : '<span class="badge b-out">Not in Profile</span>';
const dsInProfile = d => /^profile/i.test(String(d.profile || ""));
const TYPE_WORDS = {"string": "text", "int": "whole number", "long": "whole number", "integer": "whole number",
  "number": "number", "double": "number", "boolean": "yes / no", "date": "date", "date-time": "date and time",
  "map": "set of named values", "object": "group of fields"};
const typeWords = t => !t ? "–" : (TYPE_WORDS[t] || (/^array<(.+)>$/.test(t) ? "list of " + (TYPE_WORDS[t.slice(6, -1)] || t.slice(6, -1)) : t));
const GLOSS = {
  schema: "The blueprint for one kind of data: which fields it has and what type each is.",
  dataset: "Where data of one schema is actually stored. A schema can have several datasets.",
  field: "One piece of information, such as an email address or an order total.",
  profile: "Unified Profile: the live, merged view of each customer that audiences are built from.",
  lake: "The data lake: Adobe's long-term store, which SQL (Query Service) reads.",
  audience: "A rule that picks customers, e.g. \"ordered in the last 30 days\".",
  coverage: "Out of a sample of recent records, the share that had this field filled in.",
};
const term = (word, key) => `<span class="term" title="${h(GLOSS[key])}">${h(word)}</span>`;

/* ---------- search ---------- */
const IDX = [];
D.fields.forEach((f, i) => IDX.push({t: "f", i, title: fieldLabel(f), sub: f.path,
  hay: [f.name, f.path, f.fpath, f.desc, ...f.on.map(o => D.schemas[o.s].title)].join(" ").toLowerCase()}));
D.schemas.forEach((s, i) => IDX.push({t: "s", i, title: s.name, sub: `${kindWord(s.kind)} schema · ${plural(s.f.length, "field")}`,
  hay: [s.tab, s.title, s.desc, s.cls, s.tables].join(" ").toLowerCase()}));
D.datasets.forEach((d, i) => IDX.push({t: "d", i, title: d.name || d.table, sub: `dataset · ${d.table || ""}`,
  hay: [d.name, d.table, d.desc, d.schema, d.id].join(" ").toLowerCase()}));
D.audiences.forEach((a, i) => IDX.push({t: "a", i, title: a.name, sub: `audience · ${a.eval || ""} ${a.life || ""}`,
  hay: [a.name, a.desc, a.tags, a.id, a.by].join(" ").toLowerCase()}));
const TYPE_LABEL = {f: "Field", s: "Schema", d: "Dataset", a: "Audience"};
const TYPE_BADGE = {f: "b-event", s: "b-profile", d: "b-out", a: "b-aud"};
function search(q, limit) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const out = [];
  for (const e of IDX) {
    if (!words.every(w => e.hay.includes(w))) continue;
    const t = e.title.toLowerCase(), s = (e.sub || "").toLowerCase(), q0 = q.toLowerCase().trim();
    let sc = 0;
    if (t === q0 || s === q0) sc += 100; else if (t.startsWith(q0)) sc += 60; else if (t.includes(q0)) sc += 40;
    if (words.every(w => t.includes(w))) sc += 20;
    sc += {s: 6, d: 4, f: 3, a: 2}[e.t];
    out.push([sc, e]);
  }
  out.sort((a, b) => b[0] - a[0] || a[1].title.length - b[1].title.length);
  return out.slice(0, limit || 400).map(x => x[1]);
}
function mountSearch(el, big) {
  el.innerHTML = SEARCH_ICON + `<input type="search" placeholder="${big ? "Search fields, schemas, datasets and audiences – try “slot” or “coupon”" : "Search…  ( / )"}" autocomplete="off" aria-label="Search"><div class="drop"></div>`;
  const inp = el.querySelector("input"), drop = el.querySelector(".drop");
  let res = [], sel = -1;
  const show = () => {
    const q = inp.value.trim();
    res = q ? search(q, 12) : [];
    sel = -1;
    if (!q) { drop.style.display = "none"; return; }
    drop.innerHTML = (res.length ? res.map(e => `<a href="${href[e.t](e.i)}"><span class="badge ${TYPE_BADGE[e.t]}">${TYPE_LABEL[e.t]}</span><span>${h(e.title)}<div class="sub">${h(e.sub)}</div></span></a>`).join("")
      : `<a><span class="sub">Nothing matches “${h(q)}”.</span></a>`) + `<a class="all" href="#/search/${enc(q)}">See all results for “${h(q)}” →</a>`;
    drop.style.display = "block";
  };
  inp.addEventListener("input", show);
  inp.addEventListener("focus", show);
  inp.addEventListener("keydown", ev => {
    const links = [...drop.querySelectorAll("a[href]")];
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      sel = Math.max(0, Math.min(links.length - 1, sel + (ev.key === "ArrowDown" ? 1 : -1)));
      links.forEach((a, k) => a.classList.toggle("sel", k === sel));
    } else if (ev.key === "Enter") {
      const q = inp.value.trim();
      location.hash = sel >= 0 && links[sel] ? links[sel].getAttribute("href") : "#/search/" + enc(q);
      drop.style.display = "none"; inp.blur();
    } else if (ev.key === "Escape") { drop.style.display = "none"; inp.blur(); }
  });
  document.addEventListener("click", ev => { if (!el.contains(ev.target)) drop.style.display = "none"; });
  drop.addEventListener("click", () => { drop.style.display = "none"; inp.value = ""; });
  return inp;
}
const topInput = mountSearch(document.getElementById("topsearch"), false);
document.addEventListener("keydown", ev => {
  if (ev.key === "/" && !/input|textarea/i.test(document.activeElement.tagName)) { ev.preventDefault(); topInput.focus(); }
});

/* ---------- small pieces ---------- */
function copyBtn(text) {
  const id = "c" + Math.random().toString(36).slice(2);
  COPY[id] = text;
  return `<button data-copy="${id}">Copy</button>`;
}
const COPY = {};
document.addEventListener("click", ev => {
  const b = ev.target.closest("button[data-copy]");
  if (!b) return;
  const text = COPY[b.dataset.copy];
  const done = () => { b.textContent = "Copied ✓"; setTimeout(() => b.textContent = "Copy", 1500); };
  const fallback = () => { const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); done(); };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
});
function covBar(c) {
  const n = num(c);
  return n === null ? '<span class="sub">–</span>' : `<span class="covbar"><i style="width:${Math.max(0, Math.min(100, n))}%"></i></span>${fmt(n, 0)}%`;
}
function strip(hist) {
  if (!hist || !hist.length) return '<span class="sub">no history read</span>';
  const cells = hist.map((v, k) => {
    const m = M.months[k];
    if (v === null || v === undefined) return `<i title="${h(m)}: no data arrived this month"></i>`;
    if (v === "?") return `<i class="u" title="${h(m)}: data arrived but couldn't be read (see How to read this)">?</i>`;
    const n = num(v);
    if (n === 0) return `<i class="z" title="${h(m)}: data arrived, but this field was empty in what we read"></i>`;
    const a = 0.25 + 0.75 * Math.min(100, n) / 100;
    return `<i style="background:color-mix(in srgb,var(--m-hi) ${Math.round(a * 100)}%,transparent)" title="${h(m)}: filled in ${fmt(n, 0)}% of the records read"></i>`;
  }).join("");
  return `<div style="display:inline-block"><div class="strip">${cells}</div><div class="striplab"><span>${h(M.months[0] || "")}</span><span>${h(M.months[M.months.length - 1] || "")}</span></div></div>`;
}
const STRIP_LEGEND = `<div class="legend"><span><i style="background:var(--m-hi)"></i>field filled in (darker = more often)</span><span><i class="z" style="background:var(--m-zero)"></i>data arrived, field empty</span><span><i style="background:var(--m-unk)"></i>couldn't be read (“?”)</span><span><i style="background:var(--m-none)"></i>no data that month</span></div>`;
function ragBadge(a) { return a.rag ? `<span class="badge ${h(a.rag)}">${h(a.rag)} ${a.score !== null && a.score !== undefined ? fmt(a.score, 0) : ""}</span>` : ""; }

/* ---------- tables ---------- */
const TABLES = {};
function table(id, cols, rows, opts) {
  TABLES[id] = {cols, rows, sort: opts?.sort ?? null, dir: opts?.dir ?? 1, limit: opts?.limit ?? 500};
  return `<div class="tablewrap"><table class="t" id="${id}"><thead></thead><tbody></tbody></table></div><div class="hint" id="${id}-more"></div>`;
}
function drawTable(id) {
  const T = TABLES[id], el = document.getElementById(id);
  if (!T || !el) return;
  const rows = T.rows.slice();
  if (T.sort !== null) {
    const c = T.cols[T.sort];
    rows.sort((a, b) => {
      const x = c.v(a), y = c.v(b);
      const nx = x === null || x === undefined || x === "", ny = y === null || y === undefined || y === "";
      if (nx || ny) return nx - ny;
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * T.dir;
    });
  }
  el.querySelector("thead").innerHTML = "<tr>" + T.cols.map((c, k) => `<th class="${c.num ? "num" : ""}" data-k="${k}" title="${h(c.tip || "Click to sort")}">${h(c.label)} <span class="arr">${T.sort === k ? (T.dir > 0 ? "▲" : "▼") : ""}</span></th>`).join("") + "</tr>";
  el.querySelector("tbody").innerHTML = rows.slice(0, T.limit).map(r => "<tr>" + T.cols.map(c => `<td class="${c.num ? "num" : ""}">${c.html(r)}</td>`).join("") + "</tr>").join("")
    || `<tr><td colspan="${T.cols.length}" class="sub">Nothing to show.</td></tr>`;
  document.getElementById(id + "-more").textContent = rows.length > T.limit ? `Showing the first ${T.limit} of ${fmt(rows.length, 0)} – type in the filter box to narrow it down.` : "";
  el.querySelectorAll("th").forEach(th => th.onclick = () => {
    const k = +th.dataset.k;
    if (T.sort === k) T.dir = -T.dir; else { T.sort = k; T.dir = T.cols[k].num ? -1 : 1; }
    drawTable(id);
  });
}

/* ---------- lineage ---------- */
let LIN = [];
function linked(a, b) {
  if (a.t > b.t) [a, b] = [b, a];
  const k = a.t + b.t;
  if (k === "ds") return D.datasets[a.i].s === b.i;
  if (k === "fs") return D.fields[a.i].on.some(o => o.s === b.i);
  if (k === "af") return D.audiences[a.i].f.includes(b.i);
  if (k === "aa") return D.audiences[a.i].dep.includes(b.i) || D.audiences[b.i].dep.includes(a.i);
  if (k === "as") return D.audiences[a.i].f.some(fi => D.fields[fi].on.some(o => o.s === b.i));
  return false;
}
function nodeHtml(n) {
  let label, sub = "", kc = "ds", extra = "";
  if (n.t === "f") { const f = D.fields[n.i]; label = fieldLabel(f); sub = f.path; kc = fieldKind(f); }
  if (n.t === "s") { const s = D.schemas[n.i]; label = s.name; sub = `${kindWord(s.kind)}${s.profile ? " · in Profile" : ""}`; kc = kindOf(s.kind); }
  if (n.t === "d") { const d = D.datasets[n.i]; label = d.name || d.table; sub = dsInProfile(d) ? "in Profile" : "data lake only"; kc = "ds"; if (!dsInProfile(d)) extra = " dim"; }
  if (n.t === "a") { const a = D.audiences[n.i]; label = a.name; sub = [a.eval, a.life].filter(Boolean).join(" · "); kc = "aud"; if (a.rag) sub += `<span class="r ${h(a.rag)}"></span>`; }
  return `<a class="node k-${kc}${n.focus ? " focus" : ""}${extra}" data-n="${n.t}${n.i}" href="${href[n.t](n.i)}" title="${h(label)}"><span class="l">${h(label)}</span><span class="s">${n.t === "a" ? sub : h(sub)}</span></a>`;
}
const CAP = 10;
function lineage(cols, hint) {
  const id = "lin" + LIN.length;
  LIN.push({id, cols});
  const body = cols.map(c => {
    const items = c.items.slice(0, CAP);
    const more = c.items.length > CAP ? `<a class="more" href="${c.more || "#"}">+ ${fmt(c.items.length - CAP, 0)} more – see the list below</a>` : "";
    return `<div class="col${c.ph ? " ph" : ""}"><h4>${h(c.title)}</h4>${items.length ? items.map(nodeHtml).join("") : `<div class="empty">${c.empty || "None"}</div>`}${more}</div>`;
  }).join("");
  return `<div class="panel"><div class="linwrap"><div class="lin" id="${id}"><svg></svg>${body}</div></div><div class="hint">${hint || "Every box is a link – click one to make it the centre and keep following the data."}</div></div>`;
}
function drawLineage() {
  for (const L of LIN) {
    const el = document.getElementById(L.id);
    if (!el) continue;
    const svg = el.querySelector("svg");
    const box = el.getBoundingClientRect();
    svg.setAttribute("width", el.scrollWidth); svg.setAttribute("height", el.scrollHeight);
    const colEls = [...el.querySelectorAll(":scope > .col")];
    let paths = "";
    for (let c = 0; c + 1 < L.cols.length; c++) {
      const A = L.cols[c].items.slice(0, CAP), B = L.cols[c + 1].items.slice(0, CAP);
      for (const a of A) for (const b of B) {
        if (!linked(a, b)) continue;
        const ea = colEls[c].querySelector(`[data-n="${a.t}${a.i}"]`), eb = colEls[c + 1].querySelector(`[data-n="${b.t}${b.i}"]`);
        if (!ea || !eb) continue;
        const ra = ea.getBoundingClientRect(), rb = eb.getBoundingClientRect();
        const x1 = ra.right - box.left, y1 = ra.top + ra.height / 2 - box.top, x2 = rb.left - box.left, y2 = rb.top + rb.height / 2 - box.top;
        const mx = (x1 + x2) / 2, hot = a.focus || b.focus ? " hot" : "";
        paths += `<path class="e${hot}" data-a="${a.t}${a.i}" data-b="${b.t}${b.i}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
      }
    }
    svg.innerHTML = paths;
    el.querySelectorAll(".node").forEach(nd => {
      nd.onmouseenter = () => svg.querySelectorAll(`path[data-a="${nd.dataset.n}"],path[data-b="${nd.dataset.n}"]`).forEach(p => p.classList.add("hot"));
      nd.onmouseleave = () => svg.querySelectorAll("path").forEach(p => p.classList.toggle("hot", /focus/.test(
        (el.querySelector(`[data-n="${p.dataset.a}"]`) || {}).className + " " + (el.querySelector(`[data-n="${p.dataset.b}"]`) || {}).className)));
    });
  }
}
window.addEventListener("resize", () => requestAnimationFrame(drawLineage));
const uniq = xs => [...new Set(xs)];
const N = (t, i, focus) => ({t, i, focus: !!focus});
const SOURCES_PH = {title: "Where it comes from", items: [], ph: true, empty: "Sources and dataflows – <b>next step</b> (not collected yet)"};
const DEST_PH = {title: "Where it goes", items: [], ph: true, empty: "Destinations and journeys – <b>next step</b> (not collected yet)"};
const audSort = (x, y) => (D.audiences[y].score || 0) - (D.audiences[x].score || 0);

/* ---------- pages ---------- */
function pageHome() {
  const S = D.schemas, kinds = {profile: 0, event: 0, other: 0};
  S.forEach(s => kinds[kindOf(s.kind)]++);
  const inProf = D.datasets.filter(dsInProfile).length;
  const withHist = D.fields.filter(f => f.on.some(o => num(o.months) > 0)).length;
  const red = D.audiences.filter(a => a.rag === "RED");
  const linkedAud = D.audiences.filter(a => a.f.length).length;
  const card = (s) => `<a class="panel tile" href="${href.s(D.schemas.indexOf(s))}" style="padding:10px 14px"><b>${h(s.name)}</b><div class="l">${kindWord(s.kind)} · ${plural(s.f.length, "field")} · ${s.profile ? "in Profile" : "not in Profile"}</div></a>`;
  const group = (k, title) => { const xs = S.filter(s => kindOf(s.kind) === k); return xs.length ? `<h3><span class="badge b-${k}">${title}</span> ${xs.length}</h3><div class="grid g4">${xs.map(card).join("")}</div>` : ""; };
  return `<section class="hero"><div class="crumb">${h(M.label)}</div><h1>What data do we have, and what uses it?</h1>
    <p class="lede">Search for anything below, or start from a box. Every name is a link: click through to follow the data from the ${term("dataset", "dataset")} it lands in, to its ${term("schema", "schema")} and ${term("fields", "field")}, to the ${term("audiences", "audience")} that use it.</p>
    <div class="searchwrap searchbig" id="bigsearch"></div></section>
    <div class="grid g4" style="margin-top:22px">
      <a class="panel tile" href="#/fields"><div class="n">${fmt(D.fields.length, 0)}</div><div class="l">fields · ${fmt(withHist, 0)} with history</div></a>
      <a class="panel tile" href="#/schemas"><div class="n">${fmt(S.length, 0)}</div><div class="l">schemas · ${kinds.profile} profile, ${kinds.event} event, ${kinds.other} other</div></a>
      <a class="panel tile" href="#/datasets"><div class="n">${fmt(D.datasets.length, 0)}</div><div class="l">datasets · ${fmt(inProf, 0)} loaded into Profile</div></a>
      <a class="panel tile" href="#/audiences"><div class="n">${fmt(D.audiences.length, 0)}</div><div class="l">audiences · ${fmt(linkedAud, 0)} traced to their fields</div></a>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="panel card"><h3>How far back can an audience look?</h3><p>Audiences read ${term("Profile", "profile")}, and Profile keeps each event dataset for a set number of days. An audience can't look back further than that.</p>
        ${retentionList()}</div>
      <div class="panel card"><h3>Is a field good enough to build on?</h3><p>Open any field to see, in one place: how often it's filled in, how many months of it we hold, how many days of it Profile has now, and which audiences already use it.</p>
        <p><a href="#/fields">Browse all fields →</a> &nbsp; <a href="#/help">How to read a field page →</a></p>
        <h3 style="margin-top:16px">Heaviest audiences</h3><p>${red.length ? `${plural(red.length, "audience")} score RED: the heaviest for Adobe to work out.` : "None scores RED. The heaviest are:"}</p>
        <ul class="list-plain">${D.audiences.filter(a => num(a.score) !== null).sort((a, b) => b.score - a.score).slice(0, 6).map(a => `<li><a href="${href.a(D.audiences.indexOf(a))}">${h(a.name)}</a> ${ragBadge(a)}</li>`).join("")}</ul>
        <a href="#/audiences${red.length ? "/RED" : ""}">${red.length ? `All ${red.length} RED audiences →` : "All audiences →"}</a></div>
    </div>
    ${M.gaps.length ? `<div class="panel" style="margin-top:14px"><h3 style="margin-top:0">Where the picture is incomplete</h3><ul class="list-plain">${M.gaps.map(g => { const i = by.schema[g.tab]; return `<li>${i !== undefined ? `<a href="${href.s(i)}">${h(D.schemas[i].name)}</a>` : h(g.schema)}: ${h(g.status)}</li>`; }).join("")}</ul></div>` : ""}
    <h2>Every schema</h2>${group("profile", "Profile records")}${group("event", "Events")}${group("other", "Lookups and other")}`;
}

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
function ruleHtml(rule) {
  // Audience ids become their names, linked: "is in «Mens_Audience»".
  const link = id => by.audience[id] !== undefined ? `<a href="${href.a(by.audience[id])}">«${h(D.audiences[by.audience[id]].name)}»</a>` : null;
  // One pass, so the links it writes are not rewritten again.
  const re = new RegExp(String.raw`segmentMembership\.\w+\.(${UUID})\.status in \[&quot;realized&quot;, &quot;existing&quot;\]|inSegment\(&quot;(${UUID})&quot;\)|(${UUID})`, "g");
  return h(rule).replace(re, (m, a, b, c) => { const l = link(a || b || c); return l ? (c ? l : "is in " + l) : m; });
}
function retentionList() {
  const rows = M.retention.map(r => {
    const t = r.split(":")[0].trim(), m = /Profile keeps (\d+) days/.exec(r), lake = /data lake: ([^)]+)/.exec(r);
    return {t, i: by.dataset[t], days: m ? +m[1] : null, lake: lake ? lake[1] : ""};
  });
  if (!rows.length) return '<p class="sub">Not in this workbook.</p>';
  const li = r => `<li>${r.i !== undefined ? `<a href="${href.d(r.i)}">${h(r.t)}</a>` : h(r.t)}: <b>${fmt(r.days, 0)} days</b>${r.lake ? ` <span class="sub">(data lake: ${h(r.lake)})</span>` : ""}</li>`;
  const short = rows.filter(r => r.days !== null && r.days < 90), rest = rows.filter(r => !short.includes(r));
  const common = rest.length && rest.every(r => r.days === rest[0].days) ? `${fmt(rest[0].days, 0)} days` : "90 days or more";
  return `${short.length ? `<p><b>Shorter than 90 days:</b></p><ul class="list-plain">${short.map(li).join("")}</ul>` : ""}
    ${rest.length ? `<details><summary>${short.length ? "The other " : ""}${plural(rest.length, "dataset")} keep ${common}</summary><ul class="list-plain">${rest.map(li).join("")}</ul></details>` : ""}`;
}
function topValues(s) {
  if (!s) return "";
  const vals = String(s).split(" | ").map(v => { const m = /^(.*)\((\d+)\)$/.exec(v.trim()); return m ? [m[1], m[2]] : [v.trim(), ""]; });
  return `<div class="vals">${vals.map(([v, n]) => `<span>${h(v)}${n ? `<b>×${fmt(+n, 0)}</b>` : ""}</span>`).join("")}</div>`;
}

function pageField(i) {
  const f = D.fields[i];
  const schemaIdx = uniq(f.on.map(o => o.s));
  const ds = uniq(schemaIdx.flatMap(s => D.schemas[s].ds));
  // Audiences others build on go first, so the lines to the last column show.
  const aud = f.aud.slice().sort((x, y) => D.audiences[y].used.length - D.audiences[x].used.length || audSort(x, y));
  const built = uniq(aud.flatMap(a => D.audiences[a].used)).filter(x => !aud.includes(x)).sort(audSort);
  const best = f.on.reduce((m, o) => Math.max(m, num(o.months) || 0), 0);
  const pd = f.on.reduce((m, o) => Math.max(m, num(o.pdays) || 0), 0);
  const cov = f.on.reduce((m, o) => Math.max(m, num(o.cov) ?? -1), -1);
  const ident = f.on.map(o => o.ident).find(Boolean), rel = f.on.map(o => o.rel).find(Boolean);
  const say = [];
  say.push(`It holds ${h(typeWords(f.type))}${f.on.length > 1 ? ` and appears in ${plural(f.on.length, "schema")}` : ` in the <a href="${href.s(f.on[0].s)}">${h(D.schemas[f.on[0].s].name)}</a> schema`}.`);
  if (cov >= 0) say.push(`In a recent sample it was filled in for <b>${fmt(cov, 0)}%</b> of records${f.on.length > 1 ? " (best schema)" : ""}.`);
  if (best > 0) say.push(`We hold about <b>${fmt(best)} months</b> of it.`);
  if (pd > 0) say.push(`Profile has <b>${fmt(pd, 0)} days</b> of it right now, so audiences can look back that far.`);
  say.push(aud.length ? `<b>${plural(aud.length, "audience")}</b> use it.` : "No audience uses it yet.");
  const rows = f.on.map(o => ({o, s: D.schemas[o.s]}));
  return `<div class="crumb"><a href="#/fields">Fields</a> › ${h(f.fpath || f.path)}</div>
    <h1>${h(fieldLabel(f))}</h1>
    <div class="copyrow" style="max-width:760px"><pre class="code">${h(f.path)}</pre>${copyBtn(f.path)}</div>
    ${f.desc ? `<p class="say">${h(f.desc)}</p>` : ""}
    <p class="say">${say.join(" ")}</p>
    ${ident || rel ? `<p>${ident ? `<span class="badge b-profile">Identity: ${h(ident)}</span>` : ""}${rel ? `<span class="badge b-other">Links to ${h(rel)}</span>` : ""}</p>` : ""}
    <h2>Lineage</h2>
    ${lineage([
      {title: "Stored in (datasets)", items: ds.map(d => N("d", d)), more: "#f-where"},
      {title: "Schemas", items: schemaIdx.map(s => N("s", s)), more: "#f-where"},
      {title: "This field", items: [N("f", i, true)]},
      {title: "Audiences that read it", items: aud.map(a => N("a", a)), more: "#f-aud", empty: "No audience reads this field."},
      {title: "Audiences built on those", items: built.map(a => N("a", a)), more: "#f-aud", empty: "None."},
      DEST_PH], "Greyed datasets are data lake only: audiences can't read them. Every box is a link.")}
    <h2 id="f-where">Where it lives, and how good the data is</h2>
    ${rows.map(({o, s}) => `<div class="panel" style="margin-bottom:12px">
      <h3 style="margin-top:0"><a href="${href.s(o.s)}">${h(s.name)}</a> <span class="badge b-${kindOf(s.kind)}">${kindWord(s.kind)}</span>${profileWord(s.profile)}${o.req ? '<span class="badge b-out">Required</span>' : ""}</h3>
      <div class="grid g3">
        <div><div class="sub">Filled in (recent sample)</div><div class="big">${num(o.cov) === null ? "–" : fmt(o.cov, 0) + "%"}</div>${o.top ? `<div class="sub" style="margin-top:6px">Most common values (personal data masked):</div>${topValues(o.top)}` : ""}</div>
        <div><div class="sub">Have we got it? (months of data)</div><div class="big">${num(o.months) === null ? "?" : fmt(o.months)} <small>months</small></div><div class="sub">${h(o.hdet || "")}</div></div>
        <div><div class="sub">In Profile now</div><div class="big">${num(o.pdays) === null ? (o.pdays ? h(o.pdays) : "–") : fmt(o.pdays, 0)} <small>days</small></div><div class="sub">${h(o.pdet || "")}</div></div>
      </div>
      <h3>Month by month</h3>${strip(o.hist)}${STRIP_LEGEND}
      ${o.sql ? `<details><summary>SQL to check it yourself (paste into Query Service)</summary><div class="copyrow"><pre class="code">${h(o.sql)}</pre>${copyBtn(o.sql)}</div></details>` : ""}
    </div>`).join("")}
    <h2 id="f-aud">Audiences that read this field</h2>
    ${aud.length ? table("t-faud", audCols(), aud.map(a => D.audiences[a]), {sort: 3, dir: -1}) : '<p class="sub">None.</p>'}`;
}

function audCols() {
  return [
    {label: "Audience", v: a => a.name, html: a => `<a href="${href.a(by.audience[a.id])}">${h(a.name)}</a>${a.desc ? `<div class="sub">${h(a.desc)}</div>` : ""}`},
    {label: "Evaluation", v: a => a.eval, html: a => h(a.eval || "–")},
    {label: "Status", v: a => a.life, html: a => h(a.life || "–")},
    {label: "Complexity", num: true, v: a => num(a.score), html: a => ragBadge(a), tip: "0–100; RED is heaviest for Adobe to evaluate"},
    {label: "Fields read", num: true, v: a => a.f.length, html: a => fmt(a.f.length, 0)},
    {label: "Built on", num: true, v: a => a.dep.length, html: a => fmt(a.dep.length, 0), tip: "Other audiences this one includes"},
  ];
}
function fieldCols(s) {
  const pick = f => s === undefined ? f.on.reduce((m, o) => (num(o.months) || 0) >= (num(m.months) || 0) ? o : m, f.on[0]) : f.on.find(o => o.s === s);
  return [
    {label: "Field", v: f => fieldLabel(f).toLowerCase(), html: f => `<a href="${href.f(by.field[f.path])}">${h(fieldLabel(f))}</a><div class="sub">${h(f.path)}</div>`},
    {label: "Holds", v: f => typeWords(f.type), html: f => h(typeWords(f.type))},
    ...(s === undefined ? [{label: "Schemas", num: true, v: f => f.on.length, html: f => fmt(f.on.length, 0)}] : []),
    {label: "Filled in", num: true, v: f => num(pick(f).cov), html: f => covBar(pick(f).cov), tip: "Share of a recent sample of records with this field filled in"},
    {label: "Months held", num: true, v: f => num(pick(f).months), html: f => num(pick(f).months) === null ? '<span class="sub">?</span>' : fmt(pick(f).months), tip: "Have we got it? Months of unbroken data"},
    {label: "Profile days", num: true, v: f => num(pick(f).pdays), html: f => num(pick(f).pdays) === null ? '<span class="sub">–</span>' : fmt(pick(f).pdays, 0), tip: "Days of it in Profile now"},
    {label: "Audiences", num: true, v: f => f.aud.length, html: f => f.aud.length ? `<b>${fmt(f.aud.length, 0)}</b>` : '<span class="sub">0</span>'},
  ];
}
function dsCols() {
  return [
    {label: "Dataset", v: d => (d.name || d.table || "").toLowerCase(), html: d => `<a href="${href.d(by.dataset[d.table || d.id])}">${h(d.name || d.table)}</a><div class="sub">${h(d.table || "")}</div>`},
    {label: "Schema", v: d => d.schema, html: d => d.s >= 0 ? `<a href="${href.s(d.s)}">${h(D.schemas[d.s].name)}</a>` : `<span class="sub">${h(d.schema || "–")}</span>`},
    {label: "In Profile", v: d => dsInProfile(d) ? 1 : 0, html: d => dsInProfile(d) ? '<span class="badge b-in">Yes</span>' : '<span class="sub">No</span>'},
    {label: "Rows (lake)", num: true, v: d => num(d.rows), html: d => fmt(d.rows, 0)},
    {label: "GB (lake)", num: true, v: d => num(d.gb), html: d => fmt(d.gb, 2)},
    {label: "Lake keeps (days)", num: true, v: d => num(d.ttl), html: d => num(d.ttl) === null ? '<span class="sub">no expiry</span>' : fmt(d.ttl, 0)},
    {label: "Profile keeps (days)", num: true, v: d => num(d.pttl), html: d => num(d.pttl) === null ? '<span class="sub">–</span>' : fmt(d.pttl, 0)},
  ];
}

function pageSchema(i) {
  const s = D.schemas[i];
  const used = s.f.filter(f => D.fields[f].aud.length).sort((x, y) => D.fields[y].aud.length - D.fields[x].aud.length);
  const aud = uniq(used.flatMap(f => D.fields[f].aud)).sort(audSort);
  const kind = kindOf(s.kind);
  const what = {profile: "one record per customer (or account) – the facts about them", event: "things that happened, each with a date and time", other: "reference data, such as products or stores, that other data links to"}[kind];
  return `<div class="crumb"><a href="#/schemas">Schemas</a> › ${h(s.title)}</div>
    <h1>${h(s.name)}</h1>
    <p><span class="badge b-${kind}">${kindWord(s.kind)}</span>${profileWord(s.profile)}</p>
    ${s.desc && ![s.tab, s.name, s.title].includes(s.desc) ? `<p class="say">${h(s.desc)}</p>` : ""}
    <p class="say">This ${term("schema", "schema")} describes ${what}. It has <b>${plural(s.f.length, "field")}</b> and <b>${plural(s.ds.length, "dataset")}</b>${s.profile ? ", and its data is in Profile, so audiences can use it" : ". None of its data is in Profile, so audiences can't use it directly (it's for SQL and reporting)"}.</p>
    <div class="facts">
      <div>Data class<b>${h(s.cls || "–")}</b></div><div>Data lake<b>${fmt(s.rows, 0)} rows · ${fmt(s.gb, 2)} GB</b></div>
      <div>Lake keeps<b>${num(s.ttl) === null ? "no expiry set" : fmt(s.ttl, 0) + " days"}</b></div><div>Profile keeps<b>${num(s.pttl) === null ? (s.profile ? "no expiry set" : "–") : fmt(s.pttl, 0) + " days"}</b></div>
      <div>Last changed<b>${h(s.mod || "–")}</b></div><div>SQL table names<b class="mono">${h(s.tables || "–")}</b></div>
    </div>
    <h2>Lineage</h2>
    ${lineage([SOURCES_PH,
      {title: "Stored in (datasets)", items: s.ds.map(d => N("d", d)), more: "#s-ds"},
      {title: "This schema", items: [N("s", i, true)]},
      {title: "Fields audiences read", items: used.map(f => N("f", f)), more: "#s-fields", empty: "No audience reads any of its fields."},
      {title: "Audiences", items: aud.map(a => N("a", a)), more: "#s-aud", empty: "None."}])}
    <h2 id="s-fields">All ${plural(s.f.length, "field")}</h2>
    <div class="tools"><input type="search" placeholder="Filter these fields…" data-filter="t-sf"><span class="count" id="t-sf-count"></span></div>
    ${table("t-sf", fieldCols(i), s.f.map(f => D.fields[f]))}
    <h2 id="s-ds">Datasets</h2>${table("t-sds", dsCols(), s.ds.map(d => D.datasets[d]), {sort: 3, dir: -1})}
    <h2 id="s-aud">Audiences that read its fields</h2>${aud.length ? table("t-saud", audCols(), aud.map(a => D.audiences[a]), {sort: 3, dir: -1}) : '<p class="sub">None.</p>'}`;
}

function pageDataset(i) {
  const d = D.datasets[i], s = d.s >= 0 ? D.schemas[d.s] : null, inP = dsInProfile(d);
  const used = s && inP ? s.f.filter(f => D.fields[f].aud.length).sort((x, y) => D.fields[y].aud.length - D.fields[x].aud.length) : [];
  const aud = uniq(used.flatMap(f => D.fields[f].aud)).sort(audSort);
  return `<div class="crumb"><a href="#/datasets">Datasets</a> › ${h(d.table || d.name)}</div>
    <h1>${h(d.name || d.table)}</h1><p>${inP ? '<span class="badge b-in">In Profile</span>' : '<span class="badge b-out">Data lake only</span>'}</p>
    ${d.desc ? `<p class="say">${h(d.desc)}</p>` : ""}
    <p class="say">${inP ? `This ${term("dataset", "dataset")} is loaded into ${term("Profile", "profile")}, so audiences can use its data${num(d.pttl) !== null ? `; Profile keeps it for <b>${fmt(d.pttl, 0)} days</b>` : ""}.` : `This ${term("dataset", "dataset")} is not loaded into Profile: audiences can't use it, but SQL in Query Service can.`}
      The ${term("data lake", "lake")} holds <b>${fmt(d.rows, 0)} rows</b> (${fmt(d.gb, 2)} GB)${num(d.ttl) !== null ? ` and keeps them for ${fmt(d.ttl, 0)} days` : ", with no expiry set"}.</p>
    <div class="facts"><div>SQL table name<b class="mono">${h(d.table || "–")}</b></div><div>Schema<b>${s ? `<a href="${href.s(d.s)}">${h(s.name)}</a>` : h(d.schema || "–") + (d.s < 0 ? ' <span class="sub">(not in this dictionary)</span>' : "")}</b></div><div>Dataset ID<b class="mono">${h(d.id || "–")}</b></div><div>Type<b>${h(d.kind || "–")}</b></div></div>
    ${d.table ? `<div class="copyrow" style="max-width:760px;margin-top:8px"><pre class="code">SELECT * FROM ${h(d.table)} LIMIT 10</pre>${copyBtn(`/* Data Dictionary: look at ${d.table} */ SELECT * FROM ${d.table} LIMIT 10`)}</div>` : ""}
    <h2>Lineage</h2>
    ${lineage([SOURCES_PH,
      {title: "This dataset", items: [N("d", i, true)]},
      {title: "Schema", items: s ? [N("s", d.s)] : [], empty: "Its schema is not in this dictionary."},
      {title: "Fields audiences read", items: used.map(f => N("f", f)), more: s ? href.s(d.s) : "", empty: inP ? "No audience reads its fields." : "Not in Profile, so no audience can read it."},
      {title: "Audiences", items: aud.map(a => N("a", a)), empty: "None."}],
      "Audiences are traced through the fields they read. The same field can also come from another dataset of the schema.")}
    ${aud.length ? `<h2>Audiences that read its fields</h2>${table("t-daud", audCols(), aud.map(a => D.audiences[a]), {sort: 3, dir: -1})}` : ""}`;
}

function pageAudience(i) {
  const a = D.audiences[i];
  const schemas = uniq(a.f.flatMap(f => D.fields[f].on.map(o => o.s)));
  const reads = [...a.f.map(f => N("f", f)), ...a.dep.map(d => N("a", d))];
  const used = a.used.slice().sort(audSort);
  const noRule = !a.fmt && !a.rule;
  let pretty = a.raw;
  if (a.fmt === "json") { try { pretty = JSON.stringify(JSON.parse(a.raw), null, 1); } catch (e) { } }
  return `<div class="crumb"><a href="#/audiences">Audiences</a> › ${h(a.id)}</div>
    <h1>${h(a.name)}</h1>
    <p>${ragBadge(a)}${a.eval ? `<span class="badge b-out">${h(a.eval)}</span>` : ""}${a.life ? `<span class="badge b-out">${h(a.life)}</span>` : ""}${(a.tags || "").split(/\s*[,;|]\s*/).filter(Boolean).map(t => `<span class="badge b-aud">${h(t)}</span>`).join("")}</p>
    ${a.desc ? `<p class="say">${h(a.desc)}</p>` : ""}
    <p class="say">${noRule ? `This audience has no rule inside Adobe: it was ${a.origin ? `made by <b>${h(a.origin)}</b>` : "uploaded or made elsewhere"}, so there are no fields to trace.`
      : `It reads <b>${plural(a.f.length, "field")}</b>${a.dep.length ? ` and builds on <b>${plural(a.dep.length, "other audience")}</b>` : ""}. ${used.length ? `<b>${plural(used.length, "audience")}</b> build on it.` : "No other audience builds on it."}`}</p>
    <div class="facts"><div>Created by<b>${h(a.by || "–")}</b></div><div>Last changed by<b>${h(a.mod || "–")}</b></div><div>Origin<b>${h(a.origin || "–")}</b></div><div>Looks back<b>${num(a.lookback) ? fmt(a.lookback, 0) + " days" : "–"}</b></div><div>Largest audience it builds on<b>${h(a.base || "–")}${num(a.basen) ? ` (${fmt(a.basen, 0)})` : ""}</b></div><div>Audience ID<b class="mono">${h(a.id)}</b></div></div>
    <h2>Lineage</h2>
    ${lineage([
      {title: "Schemas", items: schemas.map(s => N("s", s)), more: "#a-reads"},
      {title: "What it reads", items: reads, more: "#a-reads", empty: noRule ? "No rule – nothing to trace." : "No field we could match."},
      {title: "This audience", items: [N("a", i, true)]},
      {title: "Audiences built on it", items: used.map(x => N("a", x)), more: "#a-used", empty: "None."},
      DEST_PH])}
    ${a.why ? `<h2>Complexity ${ragBadge(a)}</h2><p class="say">${h(a.why)}</p><p class="hint">How heavy the rule is for Adobe to work out, 0–100. The formula is on the workbook's How to Use tab.</p>` : ""}
    ${a.rule || a.raw ? `<h2>The rule</h2>${a.rule ? `<div class="copyrow"><pre class="code">${ruleHtml(a.rule)}</pre></div>${/\[…\]/.test(a.rule) ? '<p class="hint">[…] marks parts too complex to show as text; the full rule is below.</p>' : ""}` : ""}
      ${a.raw ? `<details><summary>Full rule, as Adobe stores it (technical)</summary><div class="copyrow"><pre class="code">${h(pretty)}</pre>${copyBtn(a.raw)}</div>${a.rawcut ? '<p class="hint">Cut short here; the whole rule is in the PQL file next to the workbook.</p>' : ""}</details>` : ""}` : ""}
    <h2 id="a-reads">Fields it reads</h2>
    ${a.f.length ? table("t-af", fieldCols(), a.f.map(f => D.fields[f])) : `<p class="sub">${a.dep.length ? "None directly – it is built from the audiences below." : noRule ? "None – it has no rule." : "None that is in this dictionary."}</p>`}
    ${a.miss.length ? `<p class="hint">Also reads ${a.miss.map(p => `<code>${h(p)}</code>${/^_experience\.customerJourneyManagement\./.test(p) ? " (Journey Optimizer's own email and push tracking)" : /^pushNotificationDetails/.test(p) ? " (Journey Optimizer's push-notification sign-ups)" : ""}`).join(", ")} – not in this dictionary: Adobe system data, or a schema that was left out.</p>` : ""}
    ${a.dep.length ? `<h2>Audiences it builds on</h2>${table("t-adep", audCols(), a.dep.map(d => D.audiences[d]), {sort: 3, dir: -1})}` : ""}
    <h2 id="a-used">Audiences built on it</h2>${used.length ? table("t-aused", audCols(), used.map(x => D.audiences[x]), {sort: 3, dir: -1}) : '<p class="sub">None.</p>'}`;
}

let FILTERS = [];
function listPage(title, lede, id, cols, rowsAll, chips, opts) {
  FILTERS.push({id, rowsAll, chips: chips || [], text: "", on: (opts && opts.on) || new Set()});
  return `<h1>${h(title)}</h1><p class="lede">${lede}</p>
    <div class="tools"><input type="search" placeholder="Filter…" data-filter="${id}" value="">${(chips || []).map((c, k) => `<span class="chip${opts && opts.on && opts.on.has(k) ? " on" : ""}" data-chip="${id}:${k}">${h(c.label)}</span>`).join("")}<span class="count" id="${id}-count"></span></div>
    ${table(id, cols, rowsAll, opts)}`;
}
function applyFilter(id) {
  const F = FILTERS.find(x => x.id === id), T = TABLES[id];
  if (!T) return;
  const words = (F ? F.text : "").toLowerCase().split(/\s+/).filter(Boolean);
  const rows = (F ? F.rowsAll : T.all || T.rows).filter(r => {
    if (F) for (const k of F.on) if (!F.chips[k].test(r)) return false;
    if (!words.length) return true;
    const hay = (r.__hay || (r.__hay = JSON.stringify([r.name, r.path, r.fpath, r.desc, r.tab, r.title, r.table, r.schema, r.tags, r.id]).toLowerCase()));
    return words.every(w => hay.includes(w));
  });
  T.rows = rows;
  const c = document.getElementById(id + "-count");
  if (c) c.textContent = `${fmt(rows.length, 0)} shown`;
  drawTable(id);
}
document.addEventListener("input", ev => {
  const id = ev.target.dataset && ev.target.dataset.filter;
  if (!id) return;
  let F = FILTERS.find(x => x.id === id);
  if (!F) { F = {id, rowsAll: TABLES[id].rows.slice(), chips: [], text: "", on: new Set()}; FILTERS.push(F); }
  F.text = ev.target.value;
  applyFilter(id);
});
document.addEventListener("click", ev => {
  const c = ev.target.closest("[data-chip]");
  if (!c) return;
  const [id, k] = c.dataset.chip.split(":"), F = FILTERS.find(x => x.id === id);
  if (F.on.has(+k)) F.on.delete(+k); else F.on.add(+k);
  c.classList.toggle("on");
  applyFilter(id);
});

function pageFields() {
  return listPage("Fields", `Every field across every schema, ${fmt(D.fields.length, 0)} in all. Click a column heading to sort; click a field for its full story.`,
    "t-fields", fieldCols(), D.fields,
    [{label: "Used by an audience", test: f => f.aud.length > 0}, {label: "In Profile", test: f => f.on.some(o => D.schemas[o.s].profile)},
     {label: "12+ months held", test: f => f.on.some(o => num(o.months) >= 12)}, {label: "Profile records", test: f => f.on.some(o => kindOf(D.schemas[o.s].kind) === "profile")},
     {label: "Events", test: f => f.on.some(o => kindOf(D.schemas[o.s].kind) === "event")}], {sort: 6, dir: -1});
}
function pageSchemas() {
  const cols = [
    {label: "Schema", v: s => s.name.toLowerCase(), html: s => `<a href="${href.s(by.schema[s.tab])}">${h(s.name)}</a>${s.desc && ![s.tab, s.name, s.title].includes(s.desc) ? `<div class="sub">${h(s.desc)}</div>` : ""}`},
    {label: "Kind", v: s => kindWord(s.kind), html: s => `<span class="badge b-${kindOf(s.kind)}">${kindWord(s.kind)}</span>`},
    {label: "In Profile", v: s => s.profile ? 1 : 0, html: s => s.profile ? '<span class="badge b-in">Yes</span>' : '<span class="sub">No</span>'},
    {label: "Fields", num: true, v: s => s.f.length, html: s => fmt(s.f.length, 0)},
    {label: "Datasets", num: true, v: s => s.ds.length, html: s => fmt(s.ds.length, 0)},
    {label: "Rows (lake)", num: true, v: s => num(s.rows), html: s => fmt(s.rows, 0)},
    {label: "Audiences", num: true, v: s => uniq(s.f.flatMap(f => D.fields[f].aud)).length, html: s => fmt(uniq(s.f.flatMap(f => D.fields[f].aud)).length, 0), tip: "Audiences that read at least one of its fields"},
  ];
  return listPage("Schemas", `A ${term("schema", "schema")} is the blueprint for one kind of data. Purple are customer records, blue are events, green are lookups.`,
    "t-schemas", cols, D.schemas,
    [{label: "In Profile", test: s => s.profile}, {label: "Profile records", test: s => kindOf(s.kind) === "profile"}, {label: "Events", test: s => kindOf(s.kind) === "event"}, {label: "Lookups / other", test: s => kindOf(s.kind) === "other"}],
    {sort: 6, dir: -1});
}
function pageDatasets() {
  return listPage("Datasets", `A ${term("dataset", "dataset")} is where data actually lands. All ${fmt(D.datasets.length, 0)} are listed, including those whose schema was left out of this dictionary.`,
    "t-datasets", dsCols(), D.datasets,
    [{label: "In Profile", test: dsInProfile}, {label: "Schema in this dictionary", test: d => d.s >= 0}, {label: "Has rows", test: d => num(d.rows) > 0}],
    {sort: 3, dir: -1, on: new Set([1])});
}
function pageAudiences(rag) {
  const chips = [{label: "RED", test: a => a.rag === "RED"}, {label: "AMBER", test: a => a.rag === "AMBER"}, {label: "GREEN", test: a => a.rag === "GREEN"},
    {label: "Published", test: a => /publish/i.test(a.life || "")}, {label: "Streaming", test: a => /stream/i.test(a.eval || "")}, {label: "Batch", test: a => /batch/i.test(a.eval || "")},
    {label: "Has a rule", test: a => a.f.length || a.dep.length}];
  const on = new Set(rag ? [chips.findIndex(c => c.label === rag)] : []);
  return listPage("Audiences", `Every ${term("audience", "audience")} in ${h(M.label)}. Complexity is how heavy the rule is for Adobe to work out (RED is heaviest).`,
    "t-audiences", audCols(), D.audiences, chips, {sort: 3, dir: -1, on});
}
function pageSearch(q) {
  const res = search(q, 400);
  const groups = ["f", "s", "d", "a"].map(t => [t, res.filter(e => e.t === t)]).filter(g => g[1].length);
  return `<h1>Results for “${h(q)}”</h1><p class="lede">${res.length ? plural(res.length, "match", "matches") : "Nothing matches. Try part of a word, or a friendly name such as “email” or “store”."}</p>
    ${groups.map(([t, xs]) => `<h2>${TYPE_LABEL[t]}s <span class="count">${xs.length}</span></h2><div class="panel" style="padding:6px 14px">${xs.slice(0, 100).map(e => `<div style="padding:5px 0;border-bottom:1px solid var(--line)"><a href="${href[e.t](e.i)}">${h(e.title)}</a> <span class="sub">${h(e.sub)}</span></div>`).join("")}${xs.length > 100 ? `<div class="hint">…and ${xs.length - 100} more; add another word to narrow it down.</div>` : ""}</div>`).join("")}`;
}
function pageHelp() {
  return `<h1>How to read this</h1><p class="lede">Everything here was read from Adobe Experience Platform (${h(M.label)}) by the Data Dictionary. It shows what data exists, how good it is, and what uses it. Nothing here changes anything in Adobe.</p>
  <div class="grid g2">
   <div class="panel"><h3 style="margin-top:0">The words</h3><dl class="kv">${Object.entries({"Schema": GLOSS.schema, "Dataset": GLOSS.dataset, "Field": GLOSS.field, "Profile": GLOSS.profile, "Data lake": GLOSS.lake, "Audience": GLOSS.audience, "Filled in": GLOSS.coverage}).map(([k, v]) => `<dt><b>${k}</b></dt><dd>${h(v)}</dd>`).join("")}</dl></div>
   <div class="panel"><h3 style="margin-top:0">Following the data (lineage)</h3><p>Each page has a lineage strip. Data flows left to right:</p>
    <p><b>dataset → schema → field → audiences that read it → audiences built on those</b></p>
    <p>Click any box to move there; the lines show which boxes connect. Hover a box to highlight its lines. A greyed dataset is data-lake only, so audiences can't read it.</p>
    <p class="warn">Not here yet: <b>where data comes from</b> (sources, dataflows) and <b>where audiences go</b> (destinations, journeys). They're the next step.</p></div>
   <div class="panel"><h3 style="margin-top:0">A field's three numbers</h3>
    <p><b>Filled in</b>: out of a recent sample of records, the share that had the field. 0% can mean the field is new or unused.</p>
    <p><b>Have we got it? (months)</b>: how many months of unbroken data we hold, read month by month from the data lake.</p>
    <p><b>In Profile now (days)</b>: how far back an audience can look. Audiences read Profile, not the data lake, and Profile keeps each event dataset for a set number of days.</p></div>
   <div class="panel"><h3 style="margin-top:0">The month-by-month strip</h3>${strip([null, 0, "?", 40, 75, 100])}${STRIP_LEGEND}
    <p style="margin-top:10px">A <b>“?”</b> month means data arrived but couldn't be read from the data lake – usually because it went straight into Profile, or the lake keeps only empty placeholder files for it. It is <b>not</b> the same as “no data”. To check by hand, open the field and use <b>SQL to check it yourself</b>.</p></div>
  </div>
  <p class="foot">Built from <b>${h(M.workbook)}</b> (${h(M.generated || "")}) by ${h(M.builder)} on ${h(M.built)}. Prototype – tell Barry what's missing or confusing.</p>`;
}

/* ---------- router ---------- */
function route() {
  LIN = []; FILTERS = [];
  for (const k in TABLES) delete TABLES[k];
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  const [kind, ...rest] = parts, key = decodeURIComponent(rest.join("/"));
  let html, nav = kind || "";
  const miss = what => `<h1>Not found</h1><p class="lede">There is no ${what} called “${h(key)}” in this dictionary. <a href="#/">Back to the start</a>.</p>`;
  if (!kind) html = pageHome();
  else if (kind === "field") { html = by.field[key] !== undefined ? pageField(by.field[key]) : miss("field"); nav = "fields"; }
  else if (kind === "schema") { html = by.schema[key] !== undefined ? pageSchema(by.schema[key]) : miss("schema"); nav = "schemas"; }
  else if (kind === "dataset") { html = by.dataset[key] !== undefined ? pageDataset(by.dataset[key]) : miss("dataset"); nav = "datasets"; }
  else if (kind === "audience") { html = by.audience[key] !== undefined ? pageAudience(by.audience[key]) : miss("audience"); nav = "audiences"; }
  else if (kind === "fields") html = pageFields();
  else if (kind === "schemas") html = pageSchemas();
  else if (kind === "datasets") html = pageDatasets();
  else if (kind === "audiences") html = pageAudiences(rest[0]);
  else if (kind === "search") html = pageSearch(key);
  else if (kind === "help") html = pageHelp();
  else html = miss("page");
  app.innerHTML = html + `<div class="foot">Data Dictionary Explorer (prototype) · ${h(M.label)} · from ${h(M.workbook)} · ${h(M.generated || "")} · Barry Mann (barrymann.com)</div>`;
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("on", a.getAttribute("href") === "#/" + nav));
  const big = document.getElementById("bigsearch");
  if (big) mountSearch(big, true);
  Object.keys(TABLES).forEach(id => FILTERS.find(f => f.id === id) ? applyFilter(id) : drawTable(id));
  window.scrollTo(0, 0);
  requestAnimationFrame(drawLineage);
  document.title = (app.querySelector("h1") ? app.querySelector("h1").textContent + " – " : "") + "Data Dictionary Explorer – " + M.label;
}
/* light / dark / follow the computer; ?theme=light also works */
const THEMES = ["auto", "light", "dark"];
function setTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
  document.getElementById("theme").textContent = "Theme: " + t;
  try { localStorage.setItem("dd-theme", t); } catch (e) { }
  requestAnimationFrame(drawLineage);
}
let theme = new URLSearchParams(location.search).get("theme");
if (!THEMES.includes(theme)) { try { theme = localStorage.getItem("dd-theme"); } catch (e) { theme = null; } }
setTheme(THEMES.includes(theme) ? theme : "auto");
document.getElementById("theme").onclick = () => setTheme(THEMES[(THEMES.indexOf(document.getElementById("theme").textContent.split(": ")[1]) + 1) % 3]);
document.getElementById("brand-sub").textContent = `${M.label} · ${M.generated ? M.generated.split("|")[0].replace("Generated", "").trim() : ""}`;
window.addEventListener("hashchange", route);
document.addEventListener("click", ev => {
  const a = ev.target.closest('a[href^="#f-"],a[href^="#s-"],a[href^="#a-"]');
  if (!a) return;
  ev.preventDefault();
  const t = document.getElementById(a.getAttribute("href").slice(1));
  if (t) t.scrollIntoView({behavior: "smooth", block: "start"});
});
route();
/* ?selftest renders every page once and lists any that fail (used by the build check) */
if (new URLSearchParams(location.search).has("selftest")) {
  const pages = ["#/", "#/fields", "#/schemas", "#/datasets", "#/audiences", "#/audiences/RED", "#/help", "#/search/coupon",
    ...D.fields.map((_, i) => href.f(i)), ...D.schemas.map((_, i) => href.s(i)),
    ...D.datasets.map((_, i) => href.d(i)), ...D.audiences.map((_, i) => href.a(i))];
  const bad = [];
  for (const p of pages) {
    try {
      history.replaceState(null, "", p);
      route(); drawLineage();
      if (/^Not found/.test((app.querySelector("h1") || {}).textContent || "")) bad.push(p + "  not found");
    } catch (e) { bad.push(p + "  " + e.message); }
  }
  document.body.insertAdjacentHTML("beforeend", `<pre id="selftest">SELFTEST ${pages.length} pages, ${bad.length} problems\n${h(bad.slice(0, 40).join("\n"))}</pre>`);
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
