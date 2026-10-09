#!/usr/bin/env python3
"""
qs_postgres_hunt.py  (AEP Swiss Army Knife)
===========================================
Who, or what, is running queries against AEP Query Service from an external
PostgreSQL client? The UI's query log shows client = "Generic PostgreSQL" and
a blank "Created by" for these. At Tesco that route is meant to be closed
(exports go via Azure Blob), so every such query is a finding -- and a user who
runs at the same hour every day is a job, not a person.

Two things, both read-only (GET only; a 429 backs off and retries, any other
error stops cleanly and prints the body):

  1. SINGLE LOOKUP   --query=<id>  GET /queries/{id}: client, userId, created,
                     state, elapsedTime, dbName and the first 300 chars of SQL.
  2. SWEEP           GET /queries?orderby=-created&excludeHidden=false
                     &excludeSoftDeleted=false&limit=100, paged via
                     _links.next.href, stopping once `created` is older than
                     --days. Client can't be filtered server-side, so rows
                     whose client contains "postgres" are kept in code and
                     grouped by userId: count, first / last seen, distinct
                     dbNames, hours of day seen (UTC), one sample SQL. A userId
                     ending @techacct.adobe.com is flagged TECHNICAL ACCOUNT.
                     Every distinct client value seen is counted too.

Names: userId is an IMS id. It is shown as an email where the org user
directory (or the local cache output/user_directory_cache.json) knows it.

Output: ONE workbook, output/qs_postgres_<sandbox>.xlsx (house rule: no CSV):
By user, Raw rows, Clients seen, Lookup.

Usage:
    python qs_postgres_hunt.py                                   # prod, 30 days
    python qs_postgres_hunt.py --sandbox=prod --days=60
    python qs_postgres_hunt.py --query=a9530b2e-35ef-4b82-99eb-2915abda60b8
    python qs_postgres_hunt.py --client=postgres                 # the match text
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aep_creds
import data_dictionary_v3 as dd
import permissions_audit as pa          # the user-name cache
from house_xlsx import Book

SCRIPT_NAME = "qs_postgres_hunt"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-10-09"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
QS = "https://platform.adobe.io/data/foundation/query"
DEFAULT_QUERY = "a9530b2e-35ef-4b82-99eb-2915abda60b8"
logger = dd.logger


class Stop(Exception):
    """A non-429 error: stop cleanly, body already printed."""


def get(url: str, headers: dict, tries: int = 6):
    for attempt in range(1, tries + 1):
        try:
            body, _ = dd.http(url, headers=headers, timeout=90)
            return json.loads(body)
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if e.code == 429 and attempt < tries:
                wait = min(2 ** attempt, 60)
                try:
                    wait = max(wait, int(e.headers.get("Retry-After", "0")))
                except ValueError:
                    pass
                logger.warning(f"  429 -- backing off {wait}s (attempt {attempt}/{tries})")
                time.sleep(wait)
                continue
            print(f"\nHTTP {e.code} on {url[len(QS):][:80]}\n{text[:1500]}")
            raise Stop()
        except Exception as e:
            if attempt < tries:
                time.sleep(min(2 ** attempt, 30))
                continue
            print(f"\n{type(e).__name__}: {e}")
            raise Stop()


def to_dt(s):
    return dd.to_dt(s) if hasattr(dd, "to_dt") else None


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def who(user_id: str, names: dict) -> str:
    return names.get(user_id or "", "") or (user_id or "")


def is_tech(user_id: str) -> bool:
    return (user_id or "").lower().endswith("@techacct.adobe.com")


# ----------------------------------------------------------------------------
def lookup(headers, qid: str, names: dict) -> dict:
    print(f"\n=== TASK 1: GET /queries/{qid} ===")
    q = get(f"{QS}/queries/{urllib.parse.quote(qid, safe='')}", headers)
    req = q.get("request") or {}
    sql = req.get("sql") or ""
    row = {"id": q.get("id"), "client": q.get("client"), "clientId": q.get("clientId"),
           "userId": q.get("userId"), "user": who(q.get("userId"), names),
           "created": q.get("created"), "updated": q.get("updated"), "state": q.get("state"),
           "elapsedTime": q.get("elapsedTime"), "dbName": req.get("dbName"),
           "sessionType": q.get("sessionType"), "isInsertInto": q.get("isInsertInto"),
           "rowCount": q.get("rowCount"), "sql300": sql[:300]}
    for k in ("client", "clientId", "userId", "user", "created", "updated", "state",
              "elapsedTime", "dbName", "sessionType", "rowCount"):
        print(f"  {k:<12} {row[k]}")
    print(f"  {'sql':<12} {sql[:300].replace(chr(10), ' ')}")
    tech = is_tech(q.get("userId"))
    print(f"  => {'TECHNICAL ACCOUNT' if tech else 'user id (not a tech account)'}"
          f"{'' if row['user'] != row['userId'] else ' -- not in the user directory / cache'}")
    return row


def sweep(headers, days: int, match: str) -> tuple[list, Counter, bool]:
    print(f"\n=== TASK 2: sweep, last {days} day(s) ===")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    limit = 100
    url = (f"{QS}/queries?orderby=-created&excludeHidden=false&excludeSoftDeleted=false"
           f"&limit={limit}")
    rows, clients, pages, done, limit_ok = [], Counter(), 0, False, True
    while url and not done:
        pages += 1
        try:
            d = get(url, headers)
        except Stop:
            if limit_ok and pages == 1:
                logger.warning("limit=100 rejected; falling back to the default page size.")
                limit_ok = False
                url = url.replace(f"&limit={limit}", "")
                continue
            raise
        page = d.get("queries") or []
        for q in page:
            created = parse_ts(q.get("created"))
            if created and created < cutoff:
                done = True
                break
            clients[q.get("client") or "(blank)"] += 1
            if match in (q.get("client") or "").lower():
                req = q.get("request") or {}
                rows.append({"created": created, "updated": parse_ts(q.get("updated")),
                             "id": q.get("id"), "client": q.get("client"), "clientId": q.get("clientId"),
                             "userId": q.get("userId") or "", "state": q.get("state"),
                             "elapsedTime": q.get("elapsedTime"), "dbName": req.get("dbName") or "",
                             "sessionType": q.get("sessionType"), "rowCount": q.get("rowCount"),
                             "sql": (req.get("sql") or "")})
        if pages % 25 == 0:
            logger.info(f"  page {pages}: {sum(clients.values()):,} queries read, {len(rows)} postgres rows so far")
        url = ((d.get("_links") or {}).get("next") or {}).get("href") if not done else None
        if not page:
            break
    logger.info(f"  {pages} page(s), {sum(clients.values()):,} queries in window, "
                f"{len(rows)} with client ~ '{match}'")
    return rows, clients, limit_ok


def summarise(rows: list, names: dict) -> list[dict]:
    by = defaultdict(list)
    for r in rows:
        by[r["userId"]].append(r)
    out = []
    for uid, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        hours = Counter(r["created"].hour for r in rs if r["created"])
        out.append({
            "userId": uid, "user": who(uid, names), "technical": "TECHNICAL ACCOUNT" if is_tech(uid) else "",
            "count": len(rs), "first_seen": min((r["created"] for r in rs if r["created"]), default=None),
            "last_seen": max((r["created"] for r in rs if r["created"]), default=None),
            "days_active": len({r["created"].date() for r in rs if r["created"]}),
            "dbNames": ", ".join(sorted({r["dbName"] for r in rs if r["dbName"]})),
            "hours_utc": ", ".join(f"{h:02d}:00 x{n}" for h, n in sorted(hours.items())),
            "peak_hour": f"{hours.most_common(1)[0][0]:02d}:00 UTC ({hours.most_common(1)[0][1]} of {len(rs)})" if hours else "",
            "states": ", ".join(f"{s}={n}" for s, n in Counter(r["state"] for r in rs).most_common()),
            "sample_sql": next((r["sql"][:150].replace("\n", " ") for r in rs if r["sql"]), ""),
        })
    return out


def main():
    args = sys.argv[1:]
    opt = lambda k, default: next((a.split("=", 1)[1] for a in args if a.startswith(k + "=")), default)
    sandbox = opt("--sandbox", "prod")
    days = int(opt("--days", "30"))
    qid = opt("--query", DEFAULT_QUERY)
    match = opt("--client", "postgres").lower()
    if "-h" in args or "--help" in args:
        print(__doc__)
        return
    conf = aep_creds.load_creds(aep_creds.pick_service(opt("--credential", "aep-prod")))
    token = dd.authenticate(conf)["access_token"]
    headers = dd.aep_headers(token, conf, sandbox)
    print(f"{SCRIPT_NAME} v{SCRIPT_VERSION}  sandbox={sandbox}  window={days} days  (read-only)")
    names = pa.load_cache()

    try:
        found = lookup(headers, qid, names)
        rows, clients, limit_ok = sweep(headers, days, match)
    except Stop:
        print("\nStopped on an API error (see above). Nothing written.")
        return

    # resolve any ids the directory can give us, then re-label
    ids = {r["userId"] for r in rows if r["userId"]} | {found.get("userId") or ""}
    names = pa.resolve_all(token, conf, {i for i in ids if i})
    found["user"] = who(found.get("userId"), names)
    summary = summarise(rows, names)

    print(f"\n=== clients seen in the window ===")
    for c, n in clients.most_common():
        print(f"  {n:>8,}  {c}")
    print(f"\n=== client ~ '{match}': by user ===")
    for s in summary:
        print(f"  {s['user']}  {s['technical']}")
        print(f"      {s['count']} quer(ies) over {s['days_active']} day(s), {fmt(s['first_seen'])} -> {fmt(s['last_seen'])}")
        print(f"      dbName: {s['dbNames'] or '-'} | peak {s['peak_hour']} | hours {s['hours_utc']}")
        print(f"      sample: {s['sample_sql'][:120]}")
    if not summary:
        print("  (none)")

    book = Book(f"Query Service -- external PostgreSQL clients -- {sandbox}",
                f"Queries whose client contains '{match}', last {days} days: who ran them, how "
                "often, at what hours (UTC). A user active at the same hour every day is a job. "
                "userId is an IMS id, shown as a name where the directory or cache knows it; "
                "@techacct.adobe.com ids are API integrations. Read-only.")
    book.sheet("By user",
               ["User", "User id", "Technical", "Queries", "Days active", "First seen (UTC)",
                "Last seen (UTC)", "Peak hour", "Hours seen (UTC)", "dbNames", "States", "Sample SQL"],
               [[s["user"], s["userId"], s["technical"], s["count"], s["days_active"],
                 fmt(s["first_seen"]), fmt(s["last_seen"]), s["peak_hour"], s["hours_utc"],
                 s["dbNames"], s["states"], s["sample_sql"]] for s in summary],
               widths=[40, 44, 20, 9, 10, 20, 20, 24, 50, 40, 30, 80],
               number_formats={4: "#,##0"}, red_when={3: "TECHNICAL ACCOUNT"},
               wrap_cols=(9, 10, 12), row_height=30, tab_colour="C00000",
               facts=[("Sandbox", sandbox), ("Window", f"last {days} days"),
                      ("Queries read", sum(clients.values())), ("Matching rows", len(rows)),
                      ("Page size", "100" if limit_ok else "default (100 was rejected)")])
    book.sheet("Raw rows",
               ["Created (UTC)", "Updated (UTC)", "Query id", "Client", "Client id", "User",
                "User id", "State", "Elapsed (ms)", "dbName", "Session type", "Row count", "SQL"],
               [[fmt(r["created"]), fmt(r["updated"]), r["id"], r["client"], r["clientId"],
                 who(r["userId"], names), r["userId"], r["state"], r["elapsedTime"], r["dbName"],
                 r["sessionType"], r["rowCount"], r["sql"][:32000]] for r in rows],
               widths=[20, 20, 38, 22, 20, 36, 44, 10, 12, 30, 16, 12, 100],
               number_formats={9: "#,##0", 12: "#,##0"}, wrap_cols=(13,), row_height=30)
    book.sheet("Clients seen", ["Client", "Queries in window"],
               [[c, n] for c, n in clients.most_common()], widths=[50, 18], number_formats={2: "#,##0"})
    book.sheet("Lookup", ["Field", "Value"], [[k, str(v)] for k, v in found.items()],
               widths=[16, 120], wrap_cols=(2,), row_height=30,
               note=f"GET /queries/{qid} in {sandbox}")
    path = book.save(OUTPUT_DIR / f"qs_postgres_{sandbox}.xlsx")

    print(f"\n=== summary ===")
    print(f"  {sum(clients.values()):,} queries in the last {days} days in '{sandbox}', "
          f"{len(clients)} distinct client value(s).")
    print(f"  {len(rows)} came from a client matching '{match}', from {len(summary)} user id(s)"
          f"{' -- ' + str(sum(1 for s in summary if s['technical'])) + ' technical account(s)' if summary else ''}.")
    print(f"  Workbook: {path}")


if __name__ == "__main__":
    main()
