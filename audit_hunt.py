#!/usr/bin/env python3
"""
audit_hunt.py  (AEP Swiss Army Knife)
=====================================
WHO loaded data into a set of datasets in a time window? Read-only: every call
is a GET. Built for the 5-6 Oct 2026 question -- ~3.7M new UK Prod profiles
appeared from 2026-10-05 07:48Z and were rewritten 2026-10-06 06:00-06:02Z --
but the targets and window are arguments, so it serves the next one too.

Four sources, each matched to the target datasets locally (the APIs' filter
syntax is thin and undocumented):

  1. AUDIT EVENTS  /data/foundation/audit/events, every event in the window,
     kept where the asset is a dataset (Add Data / Create / Update / Enable
     for profile ...). The API caps a window at 1,000 events, so each day is
     walked in shrinking windows (reusing aep_usage_log's walker); the one
     tech account that floods the log with AJO Suppression List deletes is
     fetched separately for its OTHER asset types, so nothing is dropped.
  2. CATALOG DATASETS  /catalog/dataSets, to map id <-> display name <-> Query
     Service table name, plus the dataset's own created/updated user.
  3. CATALOG BATCHES  /catalog/batches per target dataset in the window: id,
     created, status, record count, createdUser / createdClient as returned.
  4. QUERY SERVICE  /query/queries (every query run in the window) and
     /query/schedules (+ their templates): kept where the SQL mentions a
     target table (INSERT INTO / INSERT OVERWRITE / CREATE AUDIENCE / CREATE
     TABLE / any mention), with user / client ids as returned.

IMS user ids are resolved to emails through the org's user directory where the
credential can read it (same lookup as the Data Dictionary); tech accounts
(@techacct.adobe.com, @AdobeID, @AdobeService, client ids) are flagged and
listed separately, never dropped.

Output: one console table per target dataset, and everything to
output/audit_hunt_<yyyy-mm-dd>.csv. API errors are printed with their status;
a request is retried at most twice.

Usage:
    python audit_hunt.py                               # defaults below
    python audit_hunt.py --from=2026-10-04T00:00:00Z --to=2026-10-07T12:00:00Z
    python audit_hunt.py --target=tesco_profile_dataset --target=whoosh
    python audit_hunt.py --sandbox=prod --credential=aep-prod
"""

from __future__ import annotations

import csv
import json
import re
import sys
import urllib.error
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aep_creds
import aep_usage_log as ul          # audit client + window walker (read-only)
import data_dictionary_v3 as dd     # http(), authenticate(), user directory
import snapshot_tables as st        # to_dt / fmt helpers

SCRIPT_NAME = "audit_hunt"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-10-07"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
PLATFORM = "https://platform.adobe.io"
DATASETS_URL = f"{PLATFORM}/data/foundation/catalog/dataSets"
BATCHES_URL = f"{PLATFORM}/data/foundation/catalog/batches"
QUERIES_URL = f"{PLATFORM}/data/foundation/query/queries"
SCHEDULES_URL = f"{PLATFORM}/data/foundation/query/schedules"
TEMPLATES_URL = f"{PLATFORM}/data/foundation/query/query-templates"

DEFAULT_FROM = "2026-10-04T00:00:00Z"
DEFAULT_TO = "2026-10-07T12:00:00Z"
DEFAULT_TARGETS = [
    "tesco_profile_dataset",
    "tesco_dd_profile_calculated_fields_dataset",
    "tesco_nearest_whoosh_profile_dataset",
    "perftest_tesco_profile_dataset",
    "ts51_new_week_offers_live_suppresing_ts04_profile",
]
# The account that floods the prod audit log with AJO Suppression List deletes
# (measured 2026-10-02). Its other events are fetched in full.
NOISE_USER = "8531ca25-5321-4df2-a937-9450bd4af0ce@techacct.adobe.com"
NOISE_ASSET_TYPE = "AJO Suppression List"
DATASET_ACTIONS = {"add", "add data", "create", "update", "enable for profile",
                   "enable", "delete", "disable for profile"}
TECH_MARKERS = ("@techacct.adobe.com", "@adobeid", "@adobeservice", "@adobe.com")
MAX_TRIES = 3            # one try + two retries, per the brief
PAGE_CAP = 400

logger = ul.logger


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def get_json(url: str, headers: dict, timeout: int = 90):
    """GET with at most two retries; errors are printed with their status."""
    last = None
    for attempt in range(1, MAX_TRIES + 1):
        try:
            body, _ = dd.http(url, headers=headers, timeout=timeout)
            return json.loads(body)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {dd.flatten_err(e.read().decode(errors='replace'))}"
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        logger.warning(f"  {url[len(PLATFORM):][:70]} -> {last} (attempt {attempt}/{MAX_TRIES})")
    logger.error(f"  GIVING UP on {url[len(PLATFORM):][:90]}: {last}")
    return None


def looks_technical(who: str) -> bool:
    w = (who or "").lower()
    return (not w or any(m in w for m in TECH_MARKERS)
            or re.fullmatch(r"[0-9a-f]{32}", w) is not None       # bare client id
            or "@" not in w)                                      # no identity at all


def resolve(who, directory: dict) -> str:
    """IMS id -> email where the directory knows it; else the id as given."""
    if not who:
        return ""
    return dd.resolve_actor(who, directory) if directory else str(who)


# ----------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------
def fetch_datasets(headers) -> dict:
    """{id: {name, table, created, updated, createdUser, updatedUser}}."""
    out, start = {}, 0
    props = "name,created,updated,createdUser,updatedUser,createdClient,updatedClient,tags"
    while start < PAGE_CAP * 100:
        data = get_json(f"{DATASETS_URL}?limit=100&start={start}&properties={props}", headers)
        if not data:
            break
        for dsid, ds in data.items():
            if isinstance(ds, dict):
                out[dsid] = {
                    "name": ds.get("name") or "", "table": st._pqs_table(ds),
                    "created": st.to_dt(ds.get("created")), "updated": st.to_dt(ds.get("updated")),
                    "createdUser": ds.get("createdUser") or "", "updatedUser": ds.get("updatedUser") or "",
                    "createdClient": ds.get("createdClient") or "", "updatedClient": ds.get("updatedClient") or "",
                }
        if len(data) < 100:
            break
        start += 100
    return out


def match_targets(datasets: dict, targets: list[str]) -> dict:
    """{target: [dataset ids]} matched on table name OR display name,
    case-insensitive, partial."""
    hits = {}
    for t in targets:
        tl = t.lower()
        hits[t] = [dsid for dsid, d in datasets.items()
                   if tl in d["table"].lower() or tl in d["name"].lower()
                   or tl.replace("_", " ") in d["name"].lower()]
    return hits


def fetch_batches(headers, dsid: str, a: datetime, b: datetime) -> list[dict]:
    out, start = [], 0
    base = (f"{BATCHES_URL}?dataSet={dsid}&createdAfter={int(a.timestamp() * 1000)}"
            f"&createdBefore={int(b.timestamp() * 1000)}&orderBy=desc:created&limit=100")
    while start < PAGE_CAP * 100:
        data = get_json(f"{base}&start={start}", headers)
        if not data:
            break
        out += [{**v, "id": k} for k, v in data.items() if isinstance(v, dict)]
        if len(data) < 100:
            break
        start += 100
    return out


def fetch_audit(client: ul.AuditClient, a: datetime, b: datetime) -> list[dict]:
    """Every audit event in [a, b): everyone but the noise account, plus the
    noise account's non-bulk events. Day by day, windows split under the cap."""
    events, cur = [], a
    while cur < b:
        nxt = min(cur.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1), b)
        stats = {"splits": 0, "incomplete": [], "counted": 0}
        got = ul.walk_windows(client, cur, nxt, [f"user!={NOISE_USER}"], True, stats)
        got += ul.walk_windows(client, cur, nxt, [f"user=={NOISE_USER}",
                                                  f"assetType!={NOISE_ASSET_TYPE}"], True, stats)
        logger.info(f"  audit {cur:%Y-%m-%d}: {len(got)} event(s)"
                    + (f", {stats['splits']} window split(s)" if stats["splits"] else "")
                    + (f", INCOMPLETE: {len(stats['incomplete'])} window(s) still at the cap"
                       if stats["incomplete"] else ""))
        events += got
        cur = nxt
    return events


def fetch_queries(headers, a: datetime, b: datetime) -> list[dict]:
    """Every query run in the window (newest first; stops once older than a)."""
    out, start, pages = [], None, 0
    while pages < PAGE_CAP:
        pages += 1
        url = f"{QUERIES_URL}?limit=100&orderby=-created" + (f"&start={urllib.parse.quote(start)}" if start else "")
        data = get_json(url, headers)
        if not data:
            break
        page = data.get("queries") or []
        fresh = [q for q in page if (st.to_dt(q.get("created")) or a) >= a]
        out += [q for q in fresh if (st.to_dt(q.get("created")) or b) < b]
        nxt = (data.get("_page") or {}).get("next")
        if not page or len(fresh) < len(page) or not nxt:
            break
        start = nxt
    return out


def fetch_schedules(headers) -> list[dict]:
    out, start, pages = [], None, 0
    while pages < PAGE_CAP:
        pages += 1
        url = f"{SCHEDULES_URL}?limit=100" + (f"&start={urllib.parse.quote(start)}" if start else "")
        data = get_json(url, headers)
        if not data:
            break
        page = data.get("schedules") or []
        out += page
        nxt = (data.get("_page") or {}).get("next")
        if not page or not nxt:
            break
        start = nxt
    return out


def fetch_templates(headers) -> dict:
    """{templateId: template} for every saved query (schedules point at these)."""
    out, url, pages = {}, f"{TEMPLATES_URL}?limit=100&orderby=-created", 0
    while url and pages < PAGE_CAP:
        pages += 1
        data = get_json(url, headers)
        if not data:
            break
        for t in data.get("templates") or []:
            if t.get("id"):
                out[t["id"]] = t
        nxt = ((data.get("_links") or {}).get("next") or {}).get("href")
        url = nxt if nxt and nxt != url else None
    return out


def sql_mentions(sql: str, tables: set[str], names: set[str]) -> str:
    """The target table (or display name) this SQL mentions, else ''."""
    s = (sql or "").lower()
    for t in sorted(tables, key=len, reverse=True):
        if t and t in s:
            return t
    for n in sorted(names, key=len, reverse=True):
        if n and n in s:
            return n
    return ""


def sql_verb(sql: str) -> str:
    s = (sql or "").lower()
    for verb in ("insert overwrite", "insert into", "create audience", "create table",
                 "drop table", "delete from", "alter table", "merge into", "select"):
        if verb in s:
            return verb.upper()
    return "?"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def parse_args(argv):
    opts = {"credential": "aep-prod", "sandbox": "prod", "from": DEFAULT_FROM,
            "to": DEFAULT_TO, "targets": []}
    for a in argv:
        k, _, v = a.partition("=")
        if k == "--from":
            opts["from"] = v
        elif k == "--to":
            opts["to"] = v
        elif k == "--sandbox":
            opts["sandbox"] = v
        elif k == "--credential":
            opts["credential"] = v
        elif k == "--target":
            opts["targets"].append(v)
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            logger.warning(f"Ignoring unknown argument {a}")
    opts["targets"] = opts["targets"] or list(DEFAULT_TARGETS)
    return opts


def main():
    opts = parse_args(sys.argv[1:])
    a, b = parse_iso(opts["from"]), parse_iso(opts["to"])
    sandbox = opts["sandbox"]
    try:
        conf = aep_creds.load_creds(aep_creds.pick_service(opts["credential"]))
        token = dd.authenticate(conf)["access_token"]
    except Exception as e:
        logger.error(f"credentials / IMS: {type(e).__name__}: {e}")
        return
    headers = dd.aep_headers(token, conf, sandbox)
    A = st.ANSI
    print(A["cyan"] + "=" * 72 + A["reset"])
    print(f"  {A['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{A['reset']}   ({SCRIPT_DATE})  by {SCRIPT_AUTHOR}")
    print(f"  Who loaded data into the target datasets?  read-only, GETs only")
    print(f"  {A['bold']}Org:{A['reset']} {conf['org_id']}   {A['bold']}Sandbox:{A['reset']} {sandbox}")
    print(f"  {A['bold']}Window:{A['reset']} {fmt(a)} -> {fmt(b)} UTC")
    print(A["cyan"] + "=" * 72 + A["reset"])

    # Users: IMS id -> email, where readable.
    try:
        directory = dd.fetch_user_directory(token, conf)
        logger.info(f"user directory: {len(directory)} user(s).")
    except Exception as e:
        directory = {}
        logger.warning(f"user directory unavailable ({type(e).__name__}); ids shown raw.")

    # 2. Catalog datasets -> targets
    logger.info("Catalog: listing datasets...")
    datasets = fetch_datasets(headers)
    hits = match_targets(datasets, opts["targets"])
    for t, ids in hits.items():
        logger.info(f"  target '{t}': {len(ids)} dataset(s) "
                    + ", ".join(f"{i} [{datasets[i]['table'] or datasets[i]['name']}]" for i in ids))
    target_ids = {i: t for t, ids in hits.items() for i in ids}
    tables = {datasets[i]["table"].lower() for i in target_ids if datasets[i]["table"]}
    names = {datasets[i]["name"].lower() for i in target_ids if datasets[i]["name"]}

    rows = []   # every finding, one dict each

    def add(target, when, source, action, who, client="", records="", obj="", detail="", ident=""):
        rows.append({"target": target, "timestamp_utc": fmt(when), "source": source,
                     "action": action, "user": resolve(who, directory), "user_raw": who or "",
                     "client": client or "", "records": records, "object": obj,
                     "id": ident, "detail": detail,
                     "technical": "yes" if looks_technical(resolve(who, directory)) else ""})

    # Dataset records themselves: created / updated inside the window?
    for dsid, t in target_ids.items():
        d = datasets[dsid]
        for key, who, cl in (("created", d["createdUser"], d["createdClient"]),
                             ("updated", d["updatedUser"], d["updatedClient"])):
            when = d[key]
            if when and a <= when < b:
                add(t, when, "catalog dataset", f"dataset {key}", who, cl, "",
                    d["table"] or d["name"], "", dsid)

    # 3. Batches per target dataset
    logger.info("Catalog: batches per target dataset in the window...")
    for dsid, t in target_ids.items():
        for bt in fetch_batches(headers, dsid, a, b):
            created = st.to_dt(bt.get("created"))
            done = st.to_dt(bt.get("completed"))
            tags = bt.get("tags") or {}
            tag_text = "; ".join(f"{k}={','.join(map(str, v)) if isinstance(v, list) else v}"
                                 for k, v in tags.items())[:200]
            add(t, created, "catalog batch", f"batch {bt.get('status') or '?'}",
                bt.get("createdUser"), bt.get("createdClient"), st._batch_record_count(bt),
                datasets[dsid]["table"] or datasets[dsid]["name"],
                f"completed {fmt(done)}; {tag_text}", bt["id"])

    # 1. Audit events
    logger.info("Audit log: every event in the window (day by day)...")
    client = ul.AuditClient(token, conf, sandbox)
    events = fetch_audit(client, a, b)
    n_ds = 0
    for e in events:
        if (e.get("assetType") or "").lower() != "dataset":
            continue
        action = (e.get("action") or "").lower()
        if action not in DATASET_ACTIONS:
            continue
        aid = e.get("assetId") or ""
        aname = (e.get("assetName") or "")
        t = target_ids.get(aid)
        if not t:
            # match by name as well: audit assetName is the display name
            t = next((tt for tt, ids in hits.items()
                      if any(datasets[i]["name"].lower() == aname.lower() for i in ids)), None)
        if not t:
            continue
        n_ds += 1
        add(t, st.to_dt(e.get("timestamp")), "audit", f"{e.get('action')} ({e.get('status')})",
            e.get("userEmail"), ", ".join(e.get("userIpAddresses") or []), "",
            aname, f"actor {e.get('actorType')}; request {e.get('requestId')}", e.get("id"))
    logger.info(f"  {len(events)} event(s) read, {n_ds} on the target datasets.")

    # 4. Query Service
    logger.info("Query Service: queries run in the window...")
    queries = fetch_queries(headers, a, b)
    n_q = 0
    for q in queries:
        sql = (q.get("request") or {}).get("sql") or q.get("effectiveSQL") or ""
        hit = sql_mentions(sql, tables, names)
        if not hit:
            continue
        n_q += 1
        t = next((tt for tt, ids in hits.items()
                  if any(hit in (datasets[i]["table"].lower(), datasets[i]["name"].lower()) for i in ids)), hit)
        add(t, st.to_dt(q.get("created")), "query", f"{sql_verb(sql)} ({q.get('state')})",
            q.get("userId"), q.get("clientId") or q.get("client"), q.get("rowCount") or "",
            hit, sql.replace("\n", " ")[:300], q.get("id"))
    logger.info(f"  {len(queries)} quer(ies) in the window, {n_q} mention a target.")

    logger.info("Query Service: schedules and their templates...")
    templates = fetch_templates(headers)
    schedules = fetch_schedules(headers)
    n_s = 0
    for s in schedules:
        tid = ((s.get("query") or {}).get("templateId")) or ""
        tpl = templates.get(tid) or {}
        sql = tpl.get("sql") or ""
        hit = sql_mentions(sql, tables, names)
        if not hit:
            continue
        n_s += 1
        t = next((tt for tt, ids in hits.items()
                  if any(hit in (datasets[i]["table"].lower(), datasets[i]["name"].lower()) for i in ids)), hit)
        cron = (s.get("schedule") or {}).get("schedule") or ""
        add(t, st.to_dt(s.get("updated")) or st.to_dt(s.get("created")), "schedule",
            f"{sql_verb(sql)} schedule {s.get('state')} cron '{cron}'",
            s.get("updatedUserId") or s.get("userId"), "", "",
            tpl.get("name") or tid, sql.replace("\n", " ")[:300], s.get("id"))
    logger.info(f"  {len(schedules)} schedule(s), {len(templates)} template(s); {n_s} mention a target.")

    # ---- console: one table per target ---------------------------------
    rows.sort(key=lambda r: (r["target"], r["timestamp_utc"]))
    by_target = defaultdict(list)
    for r in rows:
        by_target[r["target"]].append(r)
    for t in opts["targets"]:
        print()
        print(A["cyan"] + "=" * 150 + A["reset"])
        print(f"  {A['bold']}{t}{A['reset']}   datasets: "
              + (", ".join(f"{i} [{datasets[i]['table'] or datasets[i]['name']}]" for i in hits[t]) or A["red"] + "NO MATCH" + A["reset"]))
        print(A["cyan"] + "-" * 150 + A["reset"])
        print(A["bold"] + f"  {'TIMESTAMP (UTC)':<21}{'SOURCE':<16}{'ACTION':<40}{'USER / CLIENT':<48}{'RECORDS':>10}  OBJECT" + A["reset"])
        for r in by_target.get(t, []):
            who = r["user"] + (f"  [{r['client']}]" if r["client"] else "")
            colour = A["magenta"] if r["technical"] else ""
            recs = f"{r['records']:,}" if isinstance(r["records"], int) else str(r["records"])
            print(f"  {r['timestamp_utc']:<21}{r['source']:<16}{r['action'][:38]:<40}"
                  f"{colour}{who[:46]:<48}{A['reset']}{recs:>10}  {r['object'][:40]}")
        if not by_target.get(t):
            print(f"  {A['dim']}(nothing found in the window){A['reset']}")
    tech = sorted({(r["user"], r["client"]) for r in rows if r["technical"]})
    print()
    print(A["cyan"] + "=" * 150 + A["reset"])
    print(f"  {A['bold']}Technical / service accounts seen{A['reset']} (kept in the tables above, flagged here):")
    for u, c in tech:
        print(f"    {A['magenta']}{u or '(blank user)'}{A['reset']}" + (f"  client {c}" if c else ""))
    if not tech:
        print("    (none)")
    people = sorted({r["user"] for r in rows if not r["technical"] and r["user"]})
    print(f"  {A['bold']}People seen:{A['reset']} " + (", ".join(people) or "(none)"))

    # ---- CSV ------------------------------------------------------------
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"audit_hunt_{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    cols = ["target", "timestamp_utc", "source", "action", "user", "user_raw", "client",
            "technical", "records", "object", "id", "detail"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print()
    logger.info(f"Wrote {len(rows)} finding(s) to {path}")


if __name__ == "__main__":
    main()
