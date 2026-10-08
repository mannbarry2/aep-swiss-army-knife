#!/usr/bin/env python3
"""
profile_jump.py  (AEP Swiss Army Knife)
=======================================
What wrote N million records into the Profile store for ONE dataset, and who
or what triggered it? Built for the 5-6 Oct 2026 UK Prod question: 3.7M new
"shell" profiles (Tesco ID only), 21.8M records ingested into Profile from the
dataset's data-lake flow, while the dataset itself received ~300 records a day.

Six read-only checks (GET only, plus one POST to the Observability Insights
metrics endpoint, which is a read query):

  1. FLOW SERVICE   the flow(s) that feed Profile from the dataset, with their
                    runs in the window and record metrics.
  2. CATALOG        the dataset record (tags.unifiedProfile enabled / upsert,
                    schemaRef, modified) and every batch in the window.
  3. SCHEMA         the dataset's schema and the field group holding the
                    field of interest: version, created / modified, by whom.
  4. OBSERVABILITY  per-dataset ingestion metrics by day, to show the spike
                    against a baseline.
  5. PROFILE ACCESS the sample profiles by id, every attribute returned.
  6. AUDIT          every event over the window touching the dataset, its
                    schema, its flow, merge policies or identity settings.

Output: a UTC timeline on the console (facts and inferences kept apart) and
output/profile_jump_prod.xlsx (one file, overwritten) with every finding.

Usage:
    python profile_jump.py                       # defaults: the 5-6 Oct case
    python profile_jump.py --dataset=<id> --field=sensitivityMoments \\
        --from=2026-09-21T00:00:00Z --to=2026-10-07T12:00:00Z \\
        --profile=<id1> --profile=<id2> --namespace=tescoid
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aep_creds
import aep_usage_log as ul
import data_dictionary_v3 as dd
import snapshot_tables as st
import audit_hunt as ah

SCRIPT_NAME = "profile_jump"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-10-07"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
P = "https://platform.adobe.io"
FLOWS = f"{P}/data/foundation/flowservice"
SCHEMAS = f"{P}/data/foundation/schemaregistry/tenant"
OBS = f"{P}/data/infrastructure/observability/insights/metrics"
ACCESS = f"{P}/data/core/ups/access/entities"
MERGE = f"{P}/data/core/ups/config/mergePolicies"
IDENTITY_NS = f"{P}/data/core/idnamespace/identities"

DEFAULTS = {
    "dataset": "67dd472ce5012f2aeeda12bc",
    "field": "sensitivityMoments",
    "from": "2026-09-21T00:00:00Z",
    "to": "2026-10-07T12:00:00Z",
    "profiles": ["f878bedf-9fee-4e13-b3ca-1a6ef42d0dce",
                 "a1158e96-4cc0-4ffb-a043-ca1a459036d3"],
    "namespace": "tescoid",
}
OBS_METRICS = [
    "timeseries.ingestion.dataset.recordsuccess.count",
    "timeseries.ingestion.dataset.recordfailed.count",
    "timeseries.ingestion.dataset.batchsuccess.count",
    "timeseries.ingestion.dataset.batchfailed.count",
    "timeseries.ingestion.dataset.size",
    "timeseries.profiles.dataset.recordsuccess.count",
    "timeseries.profiles.dataset.recordfailed.count",
    "timeseries.profiles.dataset.recordskipped.count",
    "timeseries.profiles.streaming.recordsuccess.count",
    "timeseries.profiles.batch.recordsuccess.count",
    "timeseries.identity.dataset.recordsuccess.count",
]
logger = ul.logger
XED = "application/vnd.adobe.xed-full+json; version=1"


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def get(url, headers, timeout=90, accept=None):
    h = dict(headers)
    if accept:
        h["Accept"] = accept
    try:
        body, _ = dd.http(url, headers=h, timeout=timeout)
        return json.loads(body), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {dd.flatten_err(e.read().decode(errors='replace'))}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def post_json(url, headers, payload, timeout=90):
    h = dict(headers)
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, context=dd.SSL_CTX, timeout=timeout) as r:
            return json.loads(r.read()), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {dd.flatten_err(e.read().decode(errors='replace'))}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def who(v):
    return str(v or "")


class Timeline:
    def __init__(self):
        self.rows = []

    def add(self, when, source, event, actor="", records="", detail="", kind="fact"):
        self.rows.append({"timestamp_utc": fmt(when) if isinstance(when, datetime) else (when or ""),
                          "source": source, "event": event, "actor": actor,
                          "records": records, "detail": (detail or "")[:400], "kind": kind})


# ----------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------
def _runs_in_window(h, flow_id, a, b):
    runs, err = get(f"{FLOWS}/runs?property=flowId=={flow_id}&limit=300", h)
    if err:
        return [], err
    out = []
    for r in runs.get("items", []):
        m = r.get("metrics") or {}
        s0 = st.to_dt((m.get("durationSummary") or {}).get("startedAtUTC"))
        if s0 and a <= s0 < b:
            out.append((s0, st.to_dt((m.get("durationSummary") or {}).get("completedAtUTC")), r))
    out.sort(key=lambda x: x[0])
    return out, ""


def _skip_reasons(run) -> str:
    """The skippedInfo codes a run reports (e.g. SSING-1500-405 = XDM Entity
    Update messages are not stored in the Data Lake)."""
    seen = {}
    for act in run.get("activities") or []:
        for si in ((act.get("recordSummary") or {}).get("skippedInfo") or []):
            seen[si.get("code")] = (si.get("message"), si.get("count"))
    return "; ".join(f"{c}: {m} ({n:,})" for c, (m, n) in seen.items())


def check_flows(h, dsid, a, b, tl):
    print("\n=== 1. FLOW SERVICE ===")
    found = []
    # (a) every flow whose TARGET is the dataset -- the loads INTO it
    tcs, err = get(f"{FLOWS}/targetConnections?property=params.dataSetId=={dsid}&limit=100", h)
    if err:
        print("  targetConnections:", err)
    tc_ids = {t["id"] for t in (tcs or {}).get("items", []) if t.get("id")}
    flows, url, pages = [], f"{FLOWS}/flows?limit=100", 0
    while url and pages < 60:
        pages += 1
        d, err = get(url, h)
        if err:
            print("  flows page:", err[:120])
            break
        flows += d.get("items", [])
        nxt = ((d.get("_links") or {}).get("next") or {}).get("href")
        url = (nxt if nxt.startswith("http") else P + nxt) if nxt else None
    feeders = [f for f in flows if set(f.get("targetConnectionIds") or []) & tc_ids]
    print(f"  {len(tc_ids)} target connection(s) point at the dataset; {len(flows)} flow(s) in the sandbox; "
          f"{len(feeders)} feed the dataset")
    for f in sorted(feeders, key=lambda x: x.get("createdAt") or 0):
        found.append(f)
        src_id = (f.get("sourceConnectionIds") or [""])[0]
        sc, _ = get(f"{FLOWS}/sourceConnections/{src_id}", h) if src_id else ({}, "")
        sc = (sc.get("items") or [sc])[0] if sc else {}
        sp = sc.get("params") or {}
        print(f"\n  FEEDER {f.get('name')!r} id={f['id']} state={f.get('state')}")
        print(f"       created {fmt(st.to_dt(f.get('createdAt')))} by {who(f.get('createdBy'))} | updated {fmt(st.to_dt(f.get('updatedAt')))} by {who(f.get('updatedBy'))}")
        print(f"       source: {sc.get('name')!r} spec={(sc.get('connectionSpec') or {}).get('id')} inlet={sp.get('inletId') or ''} sourceId={sp.get('sourceId') or ''} dataType={sp.get('dataType') or ''}")
        tl.add(st.to_dt(f.get("createdAt")), "flow service", f"feeder flow created: {f.get('name')} (source {sc.get('name')!r})", who(f.get("createdBy")), "", f["id"])
        runs, err = _runs_in_window(h, f["id"], a, b)
        if err:
            print("       runs:", err[:100])
            continue
        by_day = {}
        reasons = ""
        for s0, s1, r in runs:
            rec = (r.get("metrics") or {}).get("recordSummary") or {}
            d = fmt(s0)[:10]
            by_day.setdefault(d, [0, 0, 0, 0, 0])
            by_day[d][0] += 1
            by_day[d][1] += rec.get("inputRecordCount") or 0
            by_day[d][2] += rec.get("createdRecordCount") or 0
            by_day[d][3] += rec.get("skippedRecordCount") or 0
            by_day[d][4] += rec.get("failedRecordCount") or 0
            reasons = reasons or _skip_reasons(r)
            if (rec.get("inputRecordCount") or 0) >= 500000:
                tl.add(s0, "flow run", f"{f.get('name')}: {rec.get('inputRecordCount', 0):,} records in this hour",
                       "upstream system via the inlet", rec.get("inputRecordCount", 0), _skip_reasons(r)[:200])
        print(f"       {len(runs)} run(s) in window; skip reason: {reasons or '-'}")
        for d in sorted(by_day):
            n, i, c, sk, fl = by_day[d]
            print(f"         {d}: {n:>2} runs  in={i:>11,}  created={c:>11,}  skipped={sk:>11,}  failed={fl:,}")
            tl.add(d + " (day)", "flow run", f"{f.get('name')}: daily total", "upstream system via the inlet", i, f"created={c:,} skipped={sk:,} failed={fl:,}")
    # (b) the Profile-side flow NAMED after the dataset (streaming segmentation reads it)
    fl, err = get(f"{FLOWS}/flows?limit=100&property=name=={urllib.parse.quote('Flow for datasetId = ' + dsid)}", h)
    for f in (fl or {}).get("items", []):
        found.append(f)
        print(f"\n  PROFILE-SIDE {f.get('name')!r} id={f['id']} state={f.get('state')} created {fmt(st.to_dt(f.get('createdAt')))} by {who(f.get('createdBy'))}")
        src = ((f.get("inheritedAttributes") or {}).get("sourceConnections") or [{}])[0]
        tgt = ((f.get("inheritedAttributes") or {}).get("targetConnections") or [{}])[0]
        print(f"       source type={json.dumps(src.get('typeInfo'))} target type={json.dumps(tgt.get('typeInfo'))} flowSpec={(f.get('flowSpec') or {}).get('id')}")
        tl.add(st.to_dt(f.get("createdAt")), "flow service", f"profile-side flow created: {f.get('name')}", who(f.get("createdBy")), "", f["id"])
        runs, err = _runs_in_window(h, f["id"], a, b)
        by_day = {}
        for s0, s1, r in runs:
            rec = (r.get("metrics") or {}).get("recordSummary") or {}
            d = fmt(s0)[:10]
            by_day.setdefault(d, [0, 0, 0, 0])
            by_day[d][0] += 1
            by_day[d][1] += rec.get("inputRecordCount") or 0
            by_day[d][2] += rec.get("createdRecordCount") or 0
            by_day[d][3] += rec.get("deletedRecordCount") or 0
        for d in sorted(by_day):
            n, i, c, dl = by_day[d]
            print(f"         {d}: {n:>2} runs  in={i:>11,}  created={c:>11,}  deleted={dl:,}")
            tl.add(d + " (day)", "flow run", f"{f.get('name')}: daily total (profile fragments in -> segment records created)", "system", i, f"created={c:,} deleted={dl:,}")
    if not found:
        print("  no flow found for the dataset")
    return found


def check_catalog(h, dsid, a, b, tl):
    print("\n=== 2. CATALOG ===")
    d, err = get(f"{dd.DATASETS_URL}/{dsid}", h)
    if err:
        print("  dataset:", err)
        return None
    ds = d[dsid]
    tags = ds.get("tags") or {}
    print(f"  name={ds.get('name')!r} table={st._pqs_table(ds)} state={ds.get('state')} version={ds.get('version')}")
    print(f"  created {fmt(st.to_dt(ds.get('created')))} by {ds.get('createdUser')} ({ds.get('createdClient')})")
    print(f"  updated {fmt(st.to_dt(ds.get('updated')))} by {ds.get('updatedUser')} ({ds.get('updatedClient')})")
    print(f"  schemaRef={json.dumps(ds.get('schemaRef'))}")
    print(f"  tags.unifiedProfile={tags.get('unifiedProfile')}  tags.unifiedIdentity={tags.get('unifiedIdentity')}")
    for k, v in tags.items():
        if k not in ("unifiedProfile", "unifiedIdentity", "adobe/pqs/table"):
            print(f"  tag {k} = {json.dumps(v)[:160]}")
    tl.add(st.to_dt(ds.get("updated")), "catalog", f"dataset record last updated (v{ds.get('version')})", who(ds.get("updatedUser")), "", f"unifiedProfile={tags.get('unifiedProfile')}")
    bts = ah.fetch_batches(h, dsid, a, b)
    total = 0
    by_day = {}
    for bt in bts:
        n = st._batch_record_count(bt)
        n = n if isinstance(n, int) else 0
        total += n
        day = fmt(st.to_dt(bt.get("created")))[:10]
        by_day.setdefault(day, [0, 0, set()])
        by_day[day][0] += 1
        by_day[day][1] += n
        by_day[day][2].add(f"{bt.get('createdUser')}")
        if bt.get("status") != "success" or n >= 10000:
            tl.add(st.to_dt(bt.get("created")), "catalog batch", f"batch {bt.get('status')}", who(bt.get("createdUser")), n, json.dumps(bt.get("errors"))[:200] if bt.get("errors") else bt["id"])
    print(f"  batches in window: {len(bts)}, {total:,} records in total")
    for day in sorted(by_day):
        n, recs, users = by_day[day]
        print(f"    {day}: {n:>3} batch(es) {recs:>8,} records  {', '.join(sorted(users))}")
    tl.add("", "catalog batch", f"{len(bts)} batches, {total:,} records into the dataset over the whole window", "", total)
    return ds


def check_schema(h, ds, field, a, tl):
    print("\n=== 3. SCHEMA REGISTRY ===")
    sid = ((ds or {}).get("schemaRef") or {}).get("id")
    if not sid:
        print("  dataset has no schemaRef")
        return
    sch, err = get(f"{SCHEMAS}/schemas/{urllib.parse.quote(sid, safe='')}", h, accept=XED)
    if err:
        print("  schema:", err)
        return
    meta = sch.get("meta:registryMetadata") or {}
    print(f"  schema {sch.get('title')!r} version={sch.get('version')} created {fmt(st.to_dt(meta.get('repo:createdDate')))} by {meta.get('repo:createdBy')} | modified {fmt(st.to_dt(meta.get('repo:lastModifiedDate')))} by {meta.get('repo:lastModifiedBy')}")
    tl.add(st.to_dt(meta.get("repo:lastModifiedDate")), "schema registry", f"schema last modified: {sch.get('title')} v{sch.get('version')}", who(meta.get("repo:lastModifiedBy")))
    refs = [x.get("$ref") for x in (sch.get("allOf") or []) if isinstance(x, dict) and x.get("$ref")]
    print(f"  {len(refs)} field group(s) / class")
    hit = None
    for ref in refs:
        if "/classes/" in ref:
            continue
        fg, err = get(f"{SCHEMAS}/fieldgroups/{urllib.parse.quote(ref, safe='')}", h, accept=XED)
        if err:
            fg, err = get(f"{P}/data/foundation/schemaregistry/global/fieldgroups/{urllib.parse.quote(ref, safe='')}", h, accept=XED)
        if err:
            print(f"    {ref[-50:]}: {err[:80]}")
            continue
        fm = fg.get("meta:registryMetadata") or {}
        has = field.lower() in json.dumps(fg).lower()
        print(f"    {fg.get('title')!r:<50} v{fg.get('version')} modified {fmt(st.to_dt(fm.get('repo:lastModifiedDate')))} by {fm.get('repo:lastModifiedBy')}{'   <-- holds ' + field if has else ''}")
        if has:
            hit = fg
            tl.add(st.to_dt(fm.get("repo:lastModifiedDate")), "schema registry", f"field group holding {field} last modified: {fg.get('title')} v{fg.get('version')}", who(fm.get("repo:lastModifiedBy")))
        lm = st.to_dt(fm.get("repo:lastModifiedDate"))
        if lm and lm >= a:
            tl.add(lm, "schema registry", f"field group modified IN WINDOW: {fg.get('title')} v{fg.get('version')}", who(fm.get("repo:lastModifiedBy")))
    if not hit:
        print(f"  no field group contains '{field}' (it may sit on the class or the schema itself)")


def check_observability(h, dsid, a, b, tl):
    print("\n=== 4. OBSERVABILITY INSIGHTS (POST, read query) ===")
    start = (a - timedelta(days=0)).strftime("%Y-%m-%dT00:00:00Z")
    end = b.strftime("%Y-%m-%dT%H:%M:%SZ")
    for metric in OBS_METRICS:
        payload = {"start": start, "end": end, "granularity": "day",
                   "metrics": [{"name": metric, "filters": {"dataSetId": [dsid]}, "aggregator": "sum"}]}
        d, err = post_json(OBS, h, payload)
        if err:
            print(f"  {metric}: {err[:140]}")
            continue
        series = d.get("metricResponses") or d.get("data") or d
        print(f"  {metric}: OK")
        try:
            for mr in (d.get("metricResponses") or []):
                for dp in mr.get("datapoints") or []:
                    ts = dp.get("timestamp")
                    val = dp.get("value")
                    print(f"      {str(ts)[:10]}  {val:,}" if isinstance(val, (int, float)) else f"      {ts} {val}")
                    tl.add(st.to_dt(ts), "observability", metric.split(".", 1)[1], "system", val if isinstance(val, (int, float)) else "")
        except Exception as e:
            print("    (unparsed)", json.dumps(series)[:500])


def check_profiles(h, ids, ns, tl):
    print("\n=== 5. PROFILE ACCESS ===")
    for pid in ids:
        for n in (ns, ns.capitalize(), "tescoId", "tescoID"):
            q = {"schema.name": "_xdm.context.profile", "entityId": pid, "entityIdNS": n}
            d, err = get(f"{ACCESS}?{urllib.parse.urlencode(q)}", h)
            if err and "404" not in err:
                print(f"  {pid} ns={n}: {err[:120]}")
                continue
            if not d:
                continue
            for key, ent in d.items():
                e = ent.get("entity") or {}
                print(f"  {pid} (ns {n}) -> profile {key[:40]} lastModifiedAt={ent.get('lastModifiedAt')} mergePolicy={json.dumps(ent.get('mergePolicy'))[:80]}")
                print("     attributes:", json.dumps(e, ensure_ascii=False)[:1500])
                tl.add(st.to_dt(ent.get("lastModifiedAt")), "profile access", f"sample profile {pid[:8]}… last modified", "", "", json.dumps(e)[:300])
            break
        else:
            print(f"  {pid}: not found under any tescoid namespace spelling")


def check_audit(token, conf, dsid, ds, flows, a, b, tl):
    print("\n=== 6. AUDIT EVENTS ===")
    client = ul.AuditClient(token, conf, "prod")
    events = ah.fetch_audit(client, a, b)
    sid = ((ds or {}).get("schemaRef") or {}).get("id") or "~"
    names = {(ds or {}).get("name", "~").lower(), st._pqs_table(ds or {}).lower()}
    flow_ids = {f.get("id") for f in flows}
    hits = []
    for e in sorted(events, key=lambda x: x.get("timestamp") or ""):
        at = (e.get("assetType") or "")
        aid = str(e.get("assetId") or "")
        an = (e.get("assetName") or "").lower()
        if (aid == dsid or an in names or aid == sid or aid in flow_ids
                or at in ("Merge Policy", "Identity Namespace", "Identity Graph", "Identity Settings", "Schema", "Field Group", "Class")
                and (aid == sid or at.startswith("Identity") or at == "Merge Policy")):
            hits.append(e)
            print(f"  {e['timestamp'][:19]} {at:<18} {e.get('action'):<10} {e.get('status'):<5} {e.get('userEmail')!s:<42} {(e.get('assetName') or '')[:50]}")
            tl.add(st.to_dt(e.get("timestamp")), "audit", f"{at} {e.get('action')} ({e.get('status')}) {e.get('assetName') or ''}", who(e.get("userEmail")))
    print(f"  {len(events)} events read, {len(hits)} touch the dataset / schema / flow / merge policy / identity")
    return events


# ----------------------------------------------------------------------------
def parse_args(argv):
    o = {"dataset": DEFAULTS["dataset"], "field": DEFAULTS["field"], "from": DEFAULTS["from"],
         "to": DEFAULTS["to"], "profiles": [], "namespace": DEFAULTS["namespace"]}
    for arg in argv:
        k, _, v = arg.partition("=")
        if k == "--dataset": o["dataset"] = v
        elif k == "--field": o["field"] = v
        elif k == "--from": o["from"] = v
        elif k == "--to": o["to"] = v
        elif k == "--profile": o["profiles"].append(v)
        elif k == "--namespace": o["namespace"] = v
        elif arg in ("-h", "--help"):
            print(__doc__); sys.exit(0)
    o["profiles"] = o["profiles"] or list(DEFAULTS["profiles"])
    return o


def main():
    o = parse_args(sys.argv[1:])
    a, b = ah.parse_iso(o["from"]), ah.parse_iso(o["to"])
    conf = aep_creds.load_creds(aep_creds.pick_service("aep-prod"))
    token = dd.authenticate(conf)["access_token"]
    h = dd.aep_headers(token, conf, "prod")
    print(f"{SCRIPT_NAME} v{SCRIPT_VERSION}  dataset {o['dataset']}  window {fmt(a)} -> {fmt(b)} UTC  (read-only)")
    tl = Timeline()
    flows = check_flows(h, o["dataset"], a, b, tl)
    ds = check_catalog(h, o["dataset"], a, b, tl)
    check_schema(h, ds, o["field"], a, tl)
    check_observability(h, o["dataset"], a, b, tl)
    check_profiles(h, o["profiles"], o["namespace"], tl)
    check_audit(token, conf, o["dataset"], ds, flows, a, b, tl)

    # merge policies + identity namespaces: last changed
    print("\n=== merge policies / identity namespaces: last changed ===")
    pols = st.fetch_merge_policies(h)
    for pid, p in pols.items():
        if (p.get("schema") or {}).get("name") == "_xdm.context.profile":
            upd = st.to_dt(p.get("updateEpoch"))
            print(f"  merge policy {p.get('name')!r} default={p.get('default')} updated {fmt(upd)} graph={json.dumps(p.get('identityGraph'))}")
            tl.add(upd, "merge policy", f"merge policy last updated: {p.get('name')} (default={p.get('default')})")
    ns, err = get(IDENTITY_NS, h)
    if err:
        print("  identity namespaces:", err[:120])
    else:
        for n in ns if isinstance(ns, list) else []:
            if (n.get("code") or "").lower().startswith("tesco"):
                print(f"  namespace {n.get('code')} id={n.get('id')} updated {n.get('updateTime')} status={n.get('status')}")
                tl.add(st.to_dt(n.get("updateTime")), "identity namespace", f"namespace {n.get('code')} last updated")

    # ---- timeline + CSV ---------------------------------------------------
    rows = sorted(tl.rows, key=lambda r: (r["timestamp_utc"] == "", r["timestamp_utc"]))
    print("\n=== TIMELINE (UTC) ===")
    print(f"  {'TIME':<20}{'SOURCE':<18}{'EVENT':<70}{'ACTOR':<36}RECORDS")
    for r in rows:
        print(f"  {r['timestamp_utc']:<20}{r['source']:<18}{r['event'][:68]:<70}{r['actor'][:34]:<36}{r['records']}")
    from house_xlsx import Book
    keys = ["timestamp_utc", "source", "event", "actor", "records", "detail", "kind"]
    book = Book(f"Profile jump -- dataset {o['dataset']}",
                "What wrote records into the Profile store for this dataset, and who "
                "or what triggered it: feeder dataflows and their runs, Catalog "
                "batches, schema changes, sample profiles and audit events, as one "
                "UTC timeline. 'kind' separates fact from inference.")
    book.sheet("Timeline", ["Timestamp (UTC)", "Source", "Event", "Actor", "Records", "Detail", "Kind"],
               [[r.get(k, "") for k in keys] for r in rows],
               widths=[20, 18, 80, 40, 14, 90, 10], number_formats={5: "#,##0"},
               wrap_cols=(3, 6), tab_colour="C00000",
               facts=[("Dataset", o["dataset"]), ("Window (UTC)", f"{fmt(a)} -> {fmt(b)}")])
    path = book.save(OUTPUT_DIR / "profile_jump_prod.xlsx")
    print(f"\nwrote {len(rows)} row(s) to {path}")


if __name__ == "__main__":
    main()
