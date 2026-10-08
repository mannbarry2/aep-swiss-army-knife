#!/usr/bin/env python3
"""
permissions_audit.py  (AEP Swiss Army Knife)
============================================
Who can do what in AEP, and how they got it. Reads the Access Control API
(roles, their permission sets, sandboxes and labels, and the subjects -- users,
groups, API credentials -- in each role), flattens it to one row per
subject x role x permission set, and answers questions such as:

    who has manage-datasets?            --permission=manage-datasets
    who can see any dashboard?          --permission=dashboard
    everything in the Dashboards group  --category=dashboards
    what can this person do?            --user=jakub.lewicki@tesco.com
    who is in this role?                --role="AEP Platform Admin"

IMS user ids are resolved to emails through the org's user directory when the
credential can read it (the same lookup as the Data Dictionary); every answer
is cached in output/user_directory_cache.json so a day the directory refuses
us still shows names it has given before. Unresolved ids are shown raw.

Also lists the Role / Permission events from the audit log for the last
--days (default 30): who changed which role, when.

Output: ONE workbook, output/permissions_<org-short>.xlsx, overwritten each run:
  Who has what     one row per subject x role x permission set (filterable)
  Permission sets  the catalogue: id, name, category, what it grants, roles using it
  Roles            every role with sandboxes, labels, member counts
  Role members     every subject in every role
  Role changes     audit events on roles/permissions in the window
The console prints the answer to the filter you gave.

Read-only: GETs only. Needs the Access Control "view permissions" rights on the
credential (the aep-prod one has them).

Usage:
    python permissions_audit.py                              # everything
    python permissions_audit.py --permission=manage-datasets
    python permissions_audit.py --permission=dashboard --sandbox=prod
    python permissions_audit.py --category="Data Management"
    python permissions_audit.py --user=someone@tesco.com
    python permissions_audit.py --role="AEP Platform Admin" --days=90
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aep_creds
import aep_usage_log as ul
import data_dictionary_v3 as dd
import snapshot_tables as st
from house_xlsx import Book

SCRIPT_NAME = "permissions_audit"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-10-08"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
ACL = "https://platform.adobe.io/data/foundation/access-control"
CACHE = OUTPUT_DIR / "user_directory_cache.json"
TECH = ("@techacct.adobe.com", "@adobeid", "@adobeservice")
AUDIT_TYPES = {"Role", "Permission", "Permission Set", "Policy", "Access Control Policy"}
logger = ul.logger


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
def get(path, headers):
    try:
        body, _ = dd.http(ACL + path, headers=headers, timeout=90)
        return json.loads(body), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {dd.flatten_err(e.read().decode(errors='replace'))}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def page(path, headers, key="items", limit=100):
    out, start, n = [], None, 0
    while n < 100:
        n += 1
        url = f"{path}{'&' if '?' in path else '?'}limit={limit}" + (f"&start={urllib.parse.quote(str(start))}" if start else "")
        d, err = get(url, headers)
        if err:
            logger.error(f"  {path[:60]}: {err[:160]}")
            break
        items = d.get(key) or []
        out += items
        nxt = (d.get("_page") or {}).get("next")
        if not nxt or len(items) < limit:
            break
        start = nxt
    return out


def fetch_roles(h):
    return page("/administration/roles", h)


def fetch_permission_sets(h):
    d, err = get("/administration/products/acp/permission-sets", h)
    if err:
        logger.error(f"  permission-sets: {err[:160]}")
        return {}
    return {p["id"]: p for p in d.get("permission-sets", []) if p.get("id")}


def fetch_subjects(h, role_id):
    """Every subject in the role; groups are exploded to their members as
    well as listed themselves, so a person inherits through a group visibly."""
    direct = page(f"/administration/roles/{role_id}/subjects", h)
    exploded = page(f"/administration/roles/{role_id}/subjects?explode-group=true", h)
    seen, out = set(), []
    for s in direct + exploded:
        key = (s.get("subjectType"), s.get("subjectId"), s.get("associatedGroupId"))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ----------------------------------------------------------------------------
# Names
# ----------------------------------------------------------------------------
def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=1), encoding="utf-8")


def resolve_all(token, conf, ids: set) -> dict:
    """{ims id: email or display name}. Directory first (cached), else the id."""
    cache = load_cache()
    try:
        directory = dd.fetch_user_directory(token, conf)
    except Exception as e:
        directory = {}
        logger.warning(f"user directory unavailable ({type(e).__name__}); using the cache.")
    fresh = 0
    for i in ids:
        if i in directory:
            name = dd.resolve_actor(i, directory)
            if name and name != i:
                if cache.get(i) != name:
                    fresh += 1
                cache[i] = name
    if fresh:
        save_cache(cache)
    logger.info(f"names: {sum(1 for i in ids if i in cache)} of {len(ids)} subject ids resolved "
                f"({'directory live' if directory else 'directory unavailable, cache only'}).")
    return cache


def label(subject_id: str, names: dict) -> str:
    return names.get(subject_id) or subject_id


def is_technical(name: str) -> bool:
    n = (name or "").lower()
    return not n or any(t in n for t in TECH) or "@" not in n


# ----------------------------------------------------------------------------
# Audit: role changes
# ----------------------------------------------------------------------------
def role_changes(token, conf, days: int) -> list[dict]:
    client = ul.AuditClient(token, conf, "prod")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    a = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0)
    out = []
    cur = a
    while cur < now:
        nxt = min(cur + timedelta(days=1), now)
        stats = {"splits": 0, "incomplete": [], "counted": 0}
        try:
            events = ul.walk_windows(client, cur, nxt, ["assetType==Role"], True, stats)
        except Exception as e:
            logger.warning(f"  audit {cur:%Y-%m-%d}: {type(e).__name__}: {e}")
            events = []
        out += [e for e in events if (e.get("assetType") or "") in AUDIT_TYPES]
        cur = nxt
    return sorted(out, key=lambda e: e.get("timestamp") or "")


# ----------------------------------------------------------------------------
def parse_args(argv):
    o = {"permission": "", "category": "", "user": "", "role": "", "sandbox": "",
         "days": 30, "credential": "aep-prod"}
    for a in argv:
        k, _, v = a.partition("=")
        if k in ("--permission", "--category", "--user", "--role", "--sandbox", "--credential"):
            o[k[2:]] = v.strip()
        elif k == "--days":
            o["days"] = int(v or 30)
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            logger.warning(f"ignoring {a}")
    return o


def main():
    o = parse_args(sys.argv[1:])
    conf = aep_creds.load_creds(aep_creds.pick_service(o["credential"]))
    token = dd.authenticate(conf)["access_token"]
    h = dd.aep_headers(token, conf)          # org-level: no sandbox header
    A = st.ANSI
    print(A["cyan"] + "=" * 72 + A["reset"])
    print(f"  {A['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{A['reset']}   ({SCRIPT_DATE})  by {SCRIPT_AUTHOR}")
    print(f"  Who can do what in AEP, and how.  Org {conf['org_id']}  (read-only)")
    print(A["cyan"] + "=" * 72 + A["reset"])

    logger.info("roles...")
    roles = fetch_roles(h)
    logger.info(f"  {len(roles)} role(s)")
    psets = fetch_permission_sets(h)
    logger.info(f"  {len(psets)} permission set(s) in the catalogue")
    logger.info("role members (every role, groups exploded)...")
    members = {}
    for r in roles:
        members[r["id"]] = fetch_subjects(h, r["id"])
    ids = {s.get("subjectId") for subs in members.values() for s in subs if s.get("subjectId")}
    ids |= {s.get("associatedGroupId") for subs in members.values() for s in subs if s.get("associatedGroupId")}
    ids |= {r.get("createdBy") for r in roles} | {r.get("modifiedBy") for r in roles}
    names = resolve_all(token, conf, {i for i in ids if i})

    # ---- flatten: subject x role x permission set ---------------------------
    grants = []
    for r in roles:
        sandboxes = ", ".join(r.get("sandboxes") or []) or "(all)"
        labels = ", ".join(((r.get("subjectAttributes") or {}).get("labels") or []))
        for s in members.get(r["id"], []):
            who = label(s.get("subjectId"), names)
            via = label(s["associatedGroupId"], names) if s.get("associatedGroupId") else ""
            for ps in r.get("permissionSets") or []:
                p = psets.get(ps, {})
                grants.append({
                    "who": who, "subject_id": s.get("subjectId"), "subject_type": s.get("subjectType"),
                    "via_group": via, "technical": "yes" if is_technical(who) else "",
                    "role": r.get("name"), "role_id": r["id"], "sandboxes": sandboxes,
                    "labels": labels, "permission_set": ps, "permission": p.get("name") or ps,
                    "category": p.get("category") or "",
                    "grants": "; ".join(f"{x.get('resource')}: {','.join(x.get('actions') or [])}"
                                        for x in (p.get("permissions") or [])),
                })

    # ---- the question asked -------------------------------------------------
    sel = grants
    q = []
    if o["permission"]:
        t = o["permission"].lower()
        sel = [g for g in sel if t in g["permission_set"].lower() or t in g["permission"].lower()]
        q.append(f"permission ~ '{o['permission']}'")
    if o["category"]:
        t = o["category"].lower()
        sel = [g for g in sel if t in g["category"].lower()]
        q.append(f"category ~ '{o['category']}'")
    if o["user"]:
        t = o["user"].lower()
        sel = [g for g in sel if t in g["who"].lower() or t in (g["subject_id"] or "").lower()]
        q.append(f"user ~ '{o['user']}'")
    if o["role"]:
        t = o["role"].lower()
        sel = [g for g in sel if t in g["role"].lower()]
        q.append(f"role ~ '{o['role']}'")
    if o["sandbox"]:
        t = o["sandbox"].lower()
        sel = [g for g in sel if g["sandboxes"] == "(all)" or t in [x.strip().lower() for x in g["sandboxes"].split(",")]]
        q.append(f"sandbox = {o['sandbox']}")
    print()
    print(f"  {A['bold']}Question:{A['reset']} " + (" and ".join(q) or "everything"))
    print(A["cyan"] + "-" * 72 + A["reset"])
    by_who = defaultdict(list)
    for g in sel:
        by_who[g["who"]].append(g)
    people = sorted(k for k in by_who if not is_technical(k))
    tech = sorted(k for k in by_who if is_technical(k))
    print(f"  {len(people)} person(s), {len(tech)} technical / unresolved subject(s), {len(sel)} grant row(s)")
    for who in people + tech:
        gs = by_who[who]
        colour = A["magenta"] if is_technical(who) else A["yellow"]
        hows = sorted({(g["role"], g["sandboxes"], g["via_group"]) for g in gs})
        perms = sorted({g["permission_set"] for g in gs})
        print(f"  {colour}{who}{A['reset']}")
        for role, sbx, via in hows:
            print(f"      via role {A['bold']}{role}{A['reset']}  [{sbx}]" + (f"  (through group {via})" if via else ""))
        if o["permission"] or o["category"] or o["user"] or o["role"]:
            print(f"      {A['dim']}{', '.join(perms)[:160]}{A['reset']}")
    if o["permission"]:
        hits = sorted({g["permission_set"] for g in sel})
        print(f"\n  permission sets matched: {', '.join(hits) or 'none'}")
        near = sorted(k for k in psets if o["permission"].lower() in k.lower() and k not in hits)
        if near:
            print(f"  also in the catalogue but granted to nobody: {', '.join(near)}")

    # ---- role changes (audit) -------------------------------------------------
    logger.info(f"audit log: role changes, last {o['days']} day(s)...")
    changes = role_changes(token, conf, o["days"])
    logger.info(f"  {len(changes)} event(s)")

    # ---- workbook ---------------------------------------------------------------
    org_short = conf["org_id"].split("@")[0][:8]
    book = Book(f"AEP permissions -- who can do what  ({datetime.now(timezone.utc):%Y-%m-%d})",
                "Every subject (user, group, API credential) in every role, crossed with the "
                "role's permission sets: one row per grant, so 'who has X' is a filter on the "
                "Permission set column and 'what can Y do' a filter on Who. 'Via group' is set "
                "when the person is in the role through a user group. Names come from the org "
                "user directory where readable; otherwise the IMS id is shown. Read-only.")
    book.sheet("Who has what",
               ["Who", "Subject type", "Via group", "Technical", "Role", "Sandboxes", "Labels",
                "Permission set", "Permission", "Category", "Grants (resource: actions)",
                "Subject id", "Role id"],
               [[g["who"], g["subject_type"], g["via_group"], g["technical"], g["role"], g["sandboxes"],
                 g["labels"], g["permission_set"], g["permission"], g["category"], g["grants"],
                 g["subject_id"], g["role_id"]] for g in sorted(grants, key=lambda g: (g["who"].lower(), g["role"], g["permission_set"]))],
               widths=[40, 12, 30, 9, 44, 24, 30, 36, 36, 22, 70, 44, 38],
               red_when={4: "yes"}, wrap_cols=(11,), tab_colour="C00000",
               facts=[("Org", conf["org_id"]), ("Roles", len(roles)),
                      ("Grant rows", len(grants)),
                      ("Distinct people", len({g["who"] for g in grants if not is_technical(g["who"])}))])
    use = Counter(ps for r in roles for ps in (r.get("permissionSets") or []))
    book.sheet("Permission sets",
               ["Permission set", "Name", "Category", "Roles using it", "People with it",
                "What it grants (resource: actions)", "Description"],
               [[pid, p.get("name"), p.get("category"), use.get(pid, 0),
                 len({g["who"] for g in grants if g["permission_set"] == pid and not is_technical(g["who"])}),
                 "; ".join(f"{x.get('resource')}: {','.join(x.get('actions') or [])}" for x in (p.get("permissions") or [])),
                 p.get("description") or ""]
                for pid, p in sorted(psets.items(), key=lambda x: ((x[1].get("category") or ""), x[0]))],
               widths=[36, 36, 24, 10, 10, 70, 90], number_formats={4: "#,##0", 5: "#,##0"},
               wrap_cols=(6, 7), row_height=30, tab_colour="7030A0")
    book.sheet("Roles",
               ["Role", "Type", "Sandboxes", "Labels", "Members", "People", "Permission sets",
                "Created (UTC)", "Created by", "Modified (UTC)", "Modified by", "Description", "Role id"],
               [[r.get("name"), r.get("roleType"), ", ".join(r.get("sandboxes") or []) or "(all)",
                 ", ".join(((r.get("subjectAttributes") or {}).get("labels") or [])),
                 len(members.get(r["id"], [])),
                 len({label(s.get("subjectId"), names) for s in members.get(r["id"], []) if s.get("subjectType") == "user"}),
                 ", ".join(r.get("permissionSets") or []),
                 st.fmt_dt(st.to_dt(r.get("createdAt"))), label(r.get("createdBy"), names),
                 st.fmt_dt(st.to_dt(r.get("modifiedAt"))), label(r.get("modifiedBy"), names),
                 r.get("description") or "", r["id"]]
                for r in sorted(roles, key=lambda r: (r.get("name") or "").lower())],
               widths=[44, 12, 24, 30, 9, 9, 90, 20, 36, 20, 36, 80, 38],
               wrap_cols=(7, 12), row_height=30, tab_colour="1F4E78")
    book.sheet("Role members",
               ["Role", "Who", "Subject type", "Via group", "Technical", "Subject id", "Sandboxes"],
               [[r.get("name"), label(s.get("subjectId"), names), s.get("subjectType"),
                 label(s["associatedGroupId"], names) if s.get("associatedGroupId") else "",
                 "yes" if is_technical(label(s.get("subjectId"), names)) else "", s.get("subjectId"),
                 ", ".join(r.get("sandboxes") or []) or "(all)"]
                for r in sorted(roles, key=lambda r: (r.get("name") or "").lower())
                for s in members.get(r["id"], [])],
               widths=[44, 40, 12, 30, 9, 44, 24], red_when={5: "yes"})
    book.sheet("Role changes",
               ["Timestamp (UTC)", "Asset type", "Action", "Status", "By", "Role / asset", "Asset id"],
               [[(e.get("timestamp") or "")[:19].replace("T", " "), e.get("assetType"), e.get("action"),
                 e.get("status"), e.get("userEmail"), e.get("assetName"), e.get("assetId")] for e in changes],
               widths=[20, 16, 12, 8, 40, 50, 40], tab_colour="C55A11",
               note=f"Audit-log events on roles and permissions, last {o['days']} days: who changed what, when.")
    path = book.save(OUTPUT_DIR / f"permissions_{org_short}.xlsx")
    print()
    logger.info(f"Wrote {path}")


if __name__ == "__main__":
    main()
