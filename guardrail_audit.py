"""
guardrail_audit.py  --  AEP Guardrail Estimator (BETA)
======================================================

Re-measures the guardrail scoreboard for one sandbox, straight from the APIs,
and writes a before/after one-pager. Built for the monthly "is the platform
still inside Adobe's guardrails?" check that used to be done by hand.

Every check is classified by HOW it can be measured:

    api     read directly from Catalog / Segmentation / Schema Registry /
            Observability -- measured on every run.
    query   needs a Query Service job over the data (events per profile,
            identities per graph). The tool prints the SQL it would run and
            leaves the row "pending" -- run it deliberately, it costs compute.
    manual  no API surface (Edge segmentation throughput, Adobe's new capacity
            metric) -- carried forward from the previous measurement.

Checks (guardrail in brackets; Adobe published defaults, override in GUARDRAILS):
    profile_datasets_record   profile-enabled datasets, Profile class   [20]
    profile_datasets_event    profile-enabled datasets, Event class     [20]
    relationships             multi-entity (one-to-one) relationships  [5]
    events_per_profile        max events per profile                   [5,000]  query
    largest_audience          largest audience as % of profile base    [30%]
    total_audiences           total audience definitions               [4,000]
    streaming_audiences       continuous-evaluation audiences          [500]
    edge_audiences            edge (synchronous) audiences             [150]
    batch_audiences           batch audiences                          [4,000]
    identities_per_graph      identities in one graph                  [50]     query
    profile_storage           Profile store size (TB); attrs max 50 MB [--]
    batches_per_day_profile   success batches/day into Profile-enabled [90]
    batch_throughput          records ingested / 24h                   [--]
    hard_size_drops_30d       failed batches (30d) citing size limits  [0]
    edge_throughput           Edge segmentation throughput             [--]     manual
    streaming_rps             streaming ingestion to Profile, peak RPS [ceiling] (valve micro-batches by hour)
    new_capacity_metric       Adobe capacity metric                    [--]     manual

Output (per run):
    output/guardrail_audit_<sandbox>_<YYYY-MM-DD>.md     scoreboard one-pager
    output/guardrail_audit_<sandbox>_<YYYY-MM-DD>.json   raw measurements + detail
    output/guardrail_audit_history_<sandbox>.json        appended; earlier runs
                                                         become the "before" columns
A baseline file (baselines/guardrail_baseline_<sandbox>.json) holding earlier
hand-measured figures is shown as extra columns when present.

Usage:
    python guardrail_audit.py aep-prod --sandbox=prod
    python guardrail_audit.py aep-prod --sandbox=prod --days=7
    python guardrail_audit.py aep-prod --sandbox=prod --include-system  # count Adobe-managed datasets too
    python guardrail_audit.py aep-prod --sandbox=prod --no-batches      # skip the batch scans

Scope: dataset counts, batch counts, throughput and storage default to CUSTOMER
datasets (Catalog classification.managedBy != SYSTEM) -- the AJO / Journey /
CJA datasets Adobe creates and hides in the UI are excluded, matching how the
audit one-pager was scoped. --include-system counts everything.

Read-only: every call is a GET (plus one POST to the Observability *query*
endpoint, which reads metrics). Nothing in AEP is changed.
"""

from __future__ import annotations

import json
import logging
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aep_creds

SCRIPT_VERSION = "0.1-beta"
SCRIPT_DATE = "2026-09-21"
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
BASELINE_DIR = SCRIPT_DIR / "baselines"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
PLATFORM = "https://platform.adobe.io"
DATASETS_URL = f"{PLATFORM}/data/foundation/catalog/dataSets"
BATCHES_URL = f"{PLATFORM}/data/foundation/catalog/batches"
SCHEMAS_URL = f"{PLATFORM}/data/foundation/schemaregistry/tenant/schemas"
DESCRIPTORS_URL = f"{PLATFORM}/data/foundation/schemaregistry/tenant/descriptors"
AUDIENCES_URL = f"{PLATFORM}/data/core/ups/audiences"
SEGMENT_JOBS_URL = f"{PLATFORM}/data/core/ups/segment/jobs"
PREVIEW_STATUS_URL = f"{PLATFORM}/data/core/ups/previewsamplestatus"
OBSERVABILITY_URL = f"{PLATFORM}/data/infrastructure/observability/insights/metrics"
SANDBOX_LIST_URL = f"{PLATFORM}/data/foundation/sandbox-management/sandboxes"

XED_LIST = "application/vnd.adobe.xed+json"
XDM_JSON = "application/vnd.adobe.xdm+json"
PROFILE_CLASS = "https://ns.adobe.com/xdm/context/profile"
EVENT_CLASS = "https://ns.adobe.com/xdm/context/experienceevent"
DEFAULT_SCOPES = ("openid,AdobeID,read_organizations,"
                  "additional_info.projectedProductContext,session")

# Adobe's published guardrails (defaults; override here if your contract differs).
GUARDRAILS = {
    "profile_datasets_record": 20,
    "profile_datasets_event": 20,
    "relationships": 5,
    "events_per_profile": 5000,
    "largest_audience_pct": 30.0,
    "total_audiences": 4000,
    "streaming_audiences": 500,
    "edge_audiences": 150,
    "batch_audiences": 4000,
    "identities_per_graph": 50,
    "batches_per_day_profile": 90,
    "hard_size_drops_30d": 0,
    "streaming_rps": 5800,          # ceiling quoted in the audit; HTS -> 11,500
}
AMBER_AT = 0.8      # >= 80% of a guardrail is amber

# Failed-batch error text that means a hard size limit was hit.
SIZE_LIMIT_MARKERS = ("size", "too large", "exceed", "limit", "50mb", "50 mb", "quota")

# Observability Insights metrics that work for per-dataset ingestion series
# (records into Profile; records into the data lake). Not used by the checks --
# the streaming figures come from Catalog's valve batches, which are exact --
# but read_observability() is kept for cross-checks.
STREAMING_METRICS = [
    "timeseries.profiles.dataset.recordsuccess.count",
    "timeseries.ingestion.dataset.recordsuccess.count",
]

SSL_CTX = ssl.create_default_context()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-7s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("guardrail")


# ----------------------------------------------------------------------------
# HTTP + auth
# ----------------------------------------------------------------------------
def http(url, method="GET", headers=None, data=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
        return r.read()


def authenticate(conf) -> str:
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": conf["client_id"],
        "client_secret": conf["client_secret"],
        "scope": conf.get("scopes") or DEFAULT_SCOPES,
    }).encode()
    body = http(conf.get("oauth_url") or IMS_URL, "POST",
                {"Content-Type": "application/x-www-form-urlencoded"}, payload)
    return json.loads(body)["access_token"]


def aep_headers(token, conf, sandbox, accept="application/json"):
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "x-sandbox-name": sandbox,
        "Accept": accept,
    }


def get_json(url, headers, timeout=90, default=None):
    try:
        return json.loads(http(url, headers=headers, timeout=timeout))
    except urllib.error.HTTPError as e:
        logger.warning(f"GET {url.split('?')[0].replace(PLATFORM, '')} -> HTTP {e.code} "
                       f"{e.read()[:160].decode('utf-8', 'replace')}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"GET {url.split('?')[0].replace(PLATFORM, '')} -> {type(e).__name__}: {e}")
    return default


# ----------------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------------
def read_datasets(headers) -> dict:
    """id -> {name, schema_id, profile, managed_by, lake_gb, profile_gb, table}."""
    out, start, limit = {}, 0, 100
    while True:
        page = get_json(f"{DATASETS_URL}?limit={limit}&start={start}"
                        f"&properties=name,schemaRef,tags,classification,extensions",
                        headers, default={}) or {}
        page = {k: v for k, v in page.items() if isinstance(v, dict)}
        for dsid, ds in page.items():
            tags = ds.get("tags") or {}
            up = tags.get("unifiedProfile") or []
            flat = [str(x) for x in (up if isinstance(up, list) else [up])]
            ext = ds.get("extensions") or {}
            lake = ((ext.get("adobe_lakeHouse") or {}).get("metrics") or {}).get("storageSize")
            prof = ((ext.get("adobe_unifiedProfile") or {}).get("metrics") or {}).get("storageSize")
            pqs = tags.get("adobe/pqs/table")
            out[dsid] = {
                "name": ds.get("name") or dsid,
                "schema_id": (ds.get("schemaRef") or {}).get("id") or "",
                "profile": any(t.startswith("enabled:true") for t in flat),
                "snapshot": any(t.startswith("ups_snapshot_type") for t in flat),
                "managed_by": (ds.get("classification") or {}).get("managedBy") or "",
                "lake_bytes": lake if isinstance(lake, (int, float)) else None,
                "profile_bytes": prof if isinstance(prof, (int, float)) else None,
                "table": pqs[0] if isinstance(pqs, list) and pqs else (pqs or ""),
            }
        if len(page) < limit:
            break
        start += limit
    return out


def read_schema_classes(headers) -> dict:
    """schema $id -> meta:class (tenant schemas, all pages)."""
    out, url = {}, f"{SCHEMAS_URL}?limit=300"
    h = dict(headers, Accept=XED_LIST)
    while url:
        data = get_json(url, h, default={}) or {}
        for r in data.get("results") or []:
            out[r.get("$id")] = r.get("meta:class") or ""
        nxt = (data.get("_page") or {}).get("next")
        url = f"{SCHEMAS_URL}?limit=300&start={urllib.parse.quote(str(nxt), safe='')}" if nxt else None
    return out


def read_relationships(headers) -> list:
    data = get_json(f"{DESCRIPTORS_URL}?property=@type==xdm:descriptorOneToOne&limit=300",
                    dict(headers, Accept=XDM_JSON), default={}) or {}
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return [x for v in (data or {}).values() if isinstance(v, list) for x in v]


def read_audiences(headers) -> list:
    """All audience definitions. NB: this endpoint's `start` is a PAGE INDEX
    (start=1 is the second page of `limit`), not a row offset."""
    out, page_no, limit = [], 0, 100
    while True:
        data = get_json(f"{AUDIENCES_URL}?limit={limit}&start={page_no}", headers, default={}) or {}
        items = data.get("children") or []
        out.extend(items)
        page = data.get("_page") or {}
        total_pages = int(page.get("totalPages") or 0)
        page_no += 1
        if not items or page_no >= total_pages:
            break
    return out


def read_latest_job_counts(headers) -> tuple[dict, dict]:
    """Per-audience counts from the most recent completed scheduler segment job
    (metrics.segmentedProfileCounter) -- the authoritative overnight numbers."""
    data = get_json(f"{SEGMENT_JOBS_URL}?limit=50&status=SUCCEEDED", headers, default={}) or {}
    jobs = data.get("children") or data.get("jobs") or []
    sched = [j for j in jobs if j.get("source") == "scheduler"] or jobs
    if not sched:
        return {}, {}
    sched.sort(key=lambda j: j.get("updateEpoch") or j.get("creationTime") or 0, reverse=True)
    job = sched[0]
    full = get_json(f"{SEGMENT_JOBS_URL}/{urllib.parse.quote(str(job.get('id')), safe='')}",
                    headers, default={}) or {}
    raw = (full.get("metrics") or {}).get("segmentedProfileCounter") or {}
    return {str(k): v for k, v in raw.items() if isinstance(v, int)}, job


def read_profile_base(headers) -> tuple[int | None, dict]:
    """Total profiles = sampled rows / sampling ratio (the same estimate the
    Profiles dashboard shows)."""
    s = get_json(PREVIEW_STATUS_URL, headers, default={}) or {}
    try:
        rows, ratio = int(str(s.get("numRowsToRead")).strip('"')), float(s.get("samplingRatio"))
        return int(rows / ratio) if ratio else None, s
    except (TypeError, ValueError):
        return None, s


def read_batches(headers, days, status="success", dataset_id=None, max_pages=30,
                 props="status,metrics,relatedObjects,created"):
    """Batches of one status created in the last `days` (newest first), optionally
    for ONE dataset (Catalog's dataSet= filter). Capped at max_pages*100 so a
    busy sandbox can't run away; the cap is reported."""
    end = int(time.time() * 1000)
    start_ms = end - days * 86400000
    out, offset, pages = {}, 0, 0
    while pages < max_pages:
        params = {"status": status, "createdAfter": start_ms, "createdBefore": end,
                  "limit": 100, "offset": offset, "orderBy": "desc:created", "properties": props}
        if dataset_id:
            params["dataSet"] = dataset_id
        q = urllib.parse.urlencode(params)
        page = get_json(f"{BATCHES_URL}?{q}", headers, default={}) or {}
        page = {k: v for k, v in page.items() if isinstance(v, dict)}
        out.update(page)
        pages += 1
        if len(page) < 100:
            return out, False
        offset += 100
    return out, True


def read_observability(headers, metric, start, end, granularity="hour", dataset_id=None):
    """One Observability Insights metric as {iso_hour: value}. A dataset filter
    needs an explicit value (groupBy without one is rejected)."""
    m = {"name": metric, "aggregator": "sum"}
    if dataset_id:
        m["filters"] = [{"name": "dataSetId", "value": dataset_id}]
    body = json.dumps({"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "granularity": granularity, "metrics": [m]}).encode()
    try:
        data = json.loads(http(OBSERVABILITY_URL, "POST",
                               dict(headers, **{"Content-Type": "application/json"}), body))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} {e.read()[:120].decode('utf-8', 'replace')}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    pts = {}
    for mr in data.get("metricResponses") or []:
        for dp in mr.get("datapoints") or []:
            pts.update(dp.get("dps") or {})
    return pts, None


# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------
def rag(value, guard, higher_is_bad=True, amber_at=AMBER_AT):
    if value is None or guard is None:
        return "grey"
    if higher_is_bad:
        if value > guard:
            return "red"
        if guard == 0:               # a zero guardrail: at 0 you are clear, not "near"
            return "green"
        return "amber" if value >= guard * amber_at else "green"
    return "green" if value <= guard else "red"


def check(key, title, value, guard, display, method="api", detail=None, status=None, note=""):
    return {"key": key, "title": title, "value": value, "guardrail": guard,
            "display": display, "method": method, "status": status or rag(value, guard),
            "note": note, "detail": detail or {}}


def gb(b):
    return round(b / 1024 ** 3, 1) if isinstance(b, (int, float)) else None


def run_checks(headers, sandbox, days, exclude_system, do_batches) -> list:
    checks = []
    t0 = time.perf_counter()

    logger.info("Catalog: datasets (with hygiene extensions)...")
    datasets = read_datasets(headers)
    logger.info("Schema Registry: classes for every tenant schema...")
    classes = read_schema_classes(headers)
    for d in datasets.values():
        d["class"] = classes.get(d["schema_id"], "")
    enabled = {k: v for k, v in datasets.items() if v["profile"]}
    customer = {k: v for k, v in enabled.items() if v["managed_by"] != "SYSTEM"}
    scope = customer if exclude_system else enabled
    scope_label = "customer (Adobe-managed excluded)" if exclude_system else "all incl. Adobe-managed"
    # The feeds the batch / throughput / streaming checks are about: profile-
    # enabled datasets in scope, minus snapshot exports (outputs, not feeds).
    feeds = {k: v for k, v in scope.items() if not v["snapshot"]}

    # 1-2. profile-enabled datasets per class
    for key, cls, title in (("profile_datasets_record", PROFILE_CLASS, "Profile-enabled datasets -- record class"),
                            ("profile_datasets_event", EVENT_CLASS, "Profile-enabled datasets -- event class")):
        rows = sorted((v for v in scope.values() if v["class"] == cls), key=lambda v: v["name"].lower())
        n_all = sum(1 for v in enabled.values() if v["class"] == cls)
        n_cust = sum(1 for v in customer.values() if v["class"] == cls)
        checks.append(check(key, title, len(rows), GUARDRAILS[key],
                            f"{len(rows)} of {GUARDRAILS[key]}",
                            note=f"{n_cust} customer + {n_all - n_cust} Adobe-managed = {n_all} enabled; counted: {scope_label}",
                            detail={"datasets": [{"name": v["name"], "managed_by": v["managed_by"],
                                                  "profile_gb": gb(v["profile_bytes"])} for v in rows]}))

    # 3. relationships
    logger.info("Schema Registry: relationship descriptors...")
    rels = read_relationships(headers)
    schemas_with_data = {v["schema_id"] for v in datasets.values()}
    # Adobe ships relationships of its own (Segment definition -> Destinations
    # Segment Mapping, Journey Step Event -> journey). At least one end of each
    # sits in an Adobe namespace (xdm/, experience/) -- the Journey Step Event
    # SOURCE is an Adobe-created schema in the tenant namespace, but its
    # target is Adobe's. They are not the customer's model and the audit never
    # counted them; a customer relationship has tenant schemas at both ends.
    ADOBE_NS = ("https://ns.adobe.com/xdm/", "https://ns.adobe.com/experience/")
    def adobe_owned(r):
        return (str(r.get("xdm:sourceSchema", "")).startswith(ADOBE_NS)
                or str(r.get("xdm:destinationSchema", "")).startswith(ADOBE_NS))
    customer_rels = [r for r in rels if not adobe_owned(r)]
    live = [r for r in customer_rels if r.get("xdm:sourceSchema") in schemas_with_data]
    checks.append(check("relationships", "Multi-entity relationships", len(live), GUARDRAILS["relationships"],
                        f"{len(live)} of {GUARDRAILS['relationships']}",
                        note=f"{len(rels)} descriptors in total: {len(rels) - len(customer_rels)} Adobe-owned excluded, "
                             f"{len(live)} customer relationships on schemas that have a dataset",
                        detail={"relationships": [{"source": r.get("xdm:sourceSchema", "").rsplit("/", 1)[-1][:16],
                                                   "property": r.get("xdm:sourceProperty"),
                                                   "target": r.get("xdm:destinationSchema", "").rsplit("/", 1)[-1][:16]}
                                                  for r in rels]}))

    # 4. events per profile -- query-based; proxy = profile-store size of event feeds
    ev_feeds = sorted((v for v in enabled.values() if v["class"] == EVENT_CLASS and v["profile_bytes"]),
                      key=lambda v: v["profile_bytes"], reverse=True)
    top = ", ".join(f"{v['name'][:34]} {gb(v['profile_bytes'])} GB" for v in ev_feeds[:3])
    sql = ("-- per event dataset (replace <table>, <idns>): max events per identity, last 90 days\n"
           "SELECT MAX(c) AS max_events_per_profile, PERCENTILE(c, 0.99) AS p99 FROM (\n"
           "  SELECT identityMap['<idns>'][0].id AS pid, COUNT(*) AS c\n"
           "  FROM <table> WHERE timestamp > CURRENT_DATE - INTERVAL '90' DAY GROUP BY 1)")
    checks.append(check("events_per_profile", "Events per profile", None, GUARDRAILS["events_per_profile"],
                        "query pending", method="query", status="grey",
                        note=f"proxy -- event feeds in the Profile store: {top}",
                        detail={"suggested_sql": sql,
                                "event_feeds": [{"name": v["name"], "table": v["table"], "profile_gb": gb(v["profile_bytes"])}
                                                for v in ev_feeds[:10]]}))

    # 5-9. audiences
    logger.info("Segmentation: audiences + latest overnight counts...")
    audiences = read_audiences(headers)
    counts, job = read_latest_job_counts(headers)
    base, sample = read_profile_base(headers)
    by_id = {str(a.get("id")): a for a in audiences}
    largest_id, largest = (max(counts.items(), key=lambda kv: kv[1]) if counts else (None, None))
    pct = round(largest / base * 100, 1) if (largest and base) else None
    checks.append(check("largest_audience", "Largest audience size", pct, GUARDRAILS["largest_audience_pct"],
                        (f"{largest/1e6:.1f}M = {pct}% of {base/1e6:.1f}M base (guide {GUARDRAILS['largest_audience_pct']:.0f}%)"
                         if pct is not None else "no overnight count available"),
                        note=f"largest: '{(by_id.get(largest_id) or {}).get('name', largest_id)}'; "
                             f"counts from segment job {str(job.get('id', ''))[:8]}; base = sampled rows / sampling ratio",
                        detail={"largest_audience": (by_id.get(largest_id) or {}).get("name"), "largest_count": largest,
                                "profile_base": base,
                                "top10": [{"name": (by_id.get(k) or {}).get("name", k), "count": v}
                                          for k, v in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]]}))
    now_ms = int(time.time() * 1000)
    created_30 = sum(1 for a in audiences if (a.get("creationTime") or 0) > now_ms - 30 * 86400000)
    created_90 = sum(1 for a in audiences if (a.get("creationTime") or 0) > now_ms - 90 * 86400000)
    per_month = round(created_90 / 3)
    headroom = GUARDRAILS["total_audiences"] - len(audiences)
    months_left = (headroom / per_month) if per_month else None
    forecast = ((datetime.now(timezone.utc) + timedelta(days=30 * months_left)).strftime("%b %Y")
                if months_left is not None else "n/a")
    checks.append(check("total_audiences", "Total audiences", len(audiences), GUARDRAILS["total_audiences"],
                        f"{len(audiences):,} of {GUARDRAILS['total_audiences']:,}, rising {per_month}/month "
                        f"(90-day avg; {created_30} in last 30d) -- forecast to exceed {forecast}",
                        detail={"created_last_30d": created_30, "created_last_90d": created_90}))
    def method(a):
        ev = a.get("evaluationInfo") or {}
        return ("batch" if (ev.get("batch") or {}).get("enabled") else
                "streaming" if (ev.get("continuous") or {}).get("enabled") else
                "edge" if (ev.get("synchronous") or {}).get("enabled") else "unknown")
    meth = Counter(method(a) for a in audiences)
    for key, mk, title in (("streaming_audiences", "streaming", "Streaming audiences"),
                           ("edge_audiences", "edge", "Edge audiences"),
                           ("batch_audiences", "batch", "Batch audiences")):
        checks.append(check(key, title, meth[mk], GUARDRAILS[key], f"{meth[mk]:,} of {GUARDRAILS[key]:,}",
                            note=f"evaluation methods: {dict(meth)}"))

    # 10. identities per graph -- query-based
    checks.append(check("identities_per_graph", "Identities per graph", None, GUARDRAILS["identities_per_graph"],
                        "query pending", method="query", status="grey",
                        note="needs a Query Service pass over the Profile snapshot (identityMap sizes)",
                        detail={"suggested_sql": "SELECT COUNT(*) FROM <profile_snapshot_table> "
                                                 "WHERE size(map_values(identityMap)) >= 50  -- profiles at cap"}))

    # 11. profile storage
    prof_all = sum(v["profile_bytes"] or 0 for v in datasets.values())
    prof_cust = sum(v["profile_bytes"] or 0 for v in datasets.values() if v["managed_by"] != "SYSTEM")
    lake_total = sum(v["lake_bytes"] or 0 for v in datasets.values())
    prof_scope = prof_cust if exclude_system else prof_all
    top_prof = sorted((v for v in datasets.values() if v["profile_bytes"]), key=lambda v: v["profile_bytes"], reverse=True)[:8]
    checks.append(check("profile_storage", "Profile storage size", round(prof_scope / 1024 ** 4, 2), None,
                        f"profile store ~{prof_scope/1024**4:.2f} TB ({scope_label}); "
                        f"all incl. Adobe-managed {prof_all/1024**4:.2f} TB; data lake {lake_total/1024**4:.1f} TB", status="green",
                        note="attributes-per-profile 50 MB cap not measurable via API; store sizes from Catalog "
                             "extensions.adobe_unifiedProfile.metrics",
                        detail={"profile_store_customer_tb": round(prof_cust / 1024 ** 4, 2),
                                "profile_store_all_tb": round(prof_all / 1024 ** 4, 2),
                                "top_profile_store": [{"name": v["name"], "managed_by": v["managed_by"],
                                                       "profile_gb": gb(v["profile_bytes"])} for v in top_prof]}))

    # 12-14. batches
    if do_batches:
        # Per profile-enabled feed (Catalog dataSet= filter), so Adobe's own
        # segment-membership / AJO batches -- thousands a week -- don't drown the
        # customer feeds the guardrail is about.
        # Streaming ingestion ALSO lands as Catalog batches -- one micro-batch
        # per feed every ~15 minutes, tagged acp_workflow=ValveWorkflow (Adobe's
        # "valve" streaming pipeline). Those are split out: the batches-per-day
        # guardrail is about real batch loads, and the valve batches, bucketed
        # by hour, give streaming records/sec straight from Catalog.
        logger.info(f"Catalog: success batches into {len(feeds)} profile-enabled feed(s), last {days} day(s)...")
        now_ms = int(time.time() * 1000)
        day_ms = now_ms - 86400000
        per_ds, capped_any = {}, False
        batch_rec24, stream_rec24 = 0, 0
        stream_hourly = Counter()           # hour-bucket (ms) -> streaming records
        stream_by_feed = Counter()          # feed name -> streaming records, 24h
        # Every success batch on a feed is one of four kinds:
        #   valve     acp_workflow=ValveWorkflow      streaming micro-batch (source side)
        #   ups       adobe/batchIngestion/status/targetSummaries, created by the UPS
        #             ingest controller -- a batch INGESTED INTO PROFILE, with
        #             metrics.recordsWritten. This is what the guardrail counts.
        #   identity  created by the identity service -- ignored
        #   other     conventional source loads (inputRecordCount) and reverts
        kinds_total = Counter()
        for dsid, v in feeds.items():
            succ, capped = read_batches(headers, days, dataset_id=dsid,
                                        props="status,metrics,relatedObjects,created,tags,createdClient")
            capped_any = capped_any or capped
            kinds = Counter()
            for b in succ.values():
                tags = b.get("tags") or {}
                met = b.get("metrics") or {}
                created = b.get("created") or 0
                client = str(b.get("createdClient") or "")
                if "ValveWorkflow" in json.dumps(tags.get("acp_workflow") or []):
                    kind, recs = "valve", met.get("inputRecordCount") or 0
                elif "adobe/batchIngestion/status/targetSummaries" in tags or "ups_ingest" in client:
                    kind, recs = "ups", met.get("recordsWritten") or met.get("recordsRead") or 0
                elif "identity" in client:
                    kind, recs = "identity", 0
                else:
                    kind, recs = "other", met.get("inputRecordCount") or 0
                kinds[kind] += 1
                if created >= day_ms:
                    if kind == "valve":
                        stream_rec24 += recs
                        stream_hourly[created // 3600000] += recs
                        stream_by_feed[v["name"]] += recs
                    elif kind in ("ups", "other"):
                        batch_rec24 += recs
            kinds_total.update(kinds)
            per_ds[dsid] = {"name": v["name"], "to_profile": kinds["ups"], "streaming": kinds["valve"],
                            "identity": kinds["identity"], "other": kinds["other"]}
        total_ups = kinds_total["ups"]
        per_day = round(total_ups / days, 1)
        top = sorted(per_ds.values(), key=lambda d: d["to_profile"], reverse=True)
        checks.append(check("batches_per_day_profile", "Batches per day to Profile", per_day, GUARDRAILS["batches_per_day_profile"],
                            f"{per_day:g} of {GUARDRAILS['batches_per_day_profile']} (avg over {days}d; {total_ups:,} batches ingested "
                            f"into Profile across {sum(1 for d in per_ds.values() if d['to_profile'])} feeds)",
                            note=(f"counts UPS ingest-controller batches (targetSummaries); excluded: {kinds_total['valve']:,} streaming "
                                  f"micro-batches, {kinds_total['identity']:,} identity-service batches, {kinds_total['other']:,} other "
                                  f"(source loads / reverts); scope: {scope_label}; busiest: " +
                                  ", ".join(f"{d['name'][:28]} {d['to_profile']}" for d in top[:4] if d["to_profile"]) +
                                  (" -- a feed hit the page cap, understated" if capped_any else "")),
                            detail={"per_dataset": [d for d in top if any(d[k] for k in ("to_profile", "streaming", "identity", "other"))],
                                    "kinds_total": dict(kinds_total), "capped": capped_any}))
        checks.append(check("batch_throughput", "Batch ingest throughput", batch_rec24, None,
                            f"{batch_rec24/1e6:.2f}M records/24h written to Profile by batch (streaming brought {stream_rec24/1e6:.1f}M)",
                            status="green",
                            note=f"recordsWritten over UPS ingest batches (+ inputRecordCount of conventional loads) created in the "
                                 f"last 24h; scope: {scope_label}"))
        # 16. streaming RPS from the valve micro-batches, hourly.
        if stream_hourly:
            peak_h, peak = max(stream_hourly.items(), key=lambda kv: kv[1])
            hours = len(stream_hourly)
            peak_rps, avg_rps = round(peak / 3600), round(stream_rec24 / 86400)
            n_feeds = sum(1 for d in per_ds.values() if d["streaming"])
            peak_at = datetime.fromtimestamp(peak_h * 3600, tz=timezone.utc).strftime("%H:%M UTC")
            busiest = ", ".join(f"{n[:26]} {r/1e6:.1f}M" for n, r in stream_by_feed.most_common(4))
            checks.append(check("streaming_rps", "Streaming ingestion to Profile (RPS)", peak_rps, GUARDRAILS["streaming_rps"],
                                f"~{peak_rps:,} rps peak hour ({peak_at}), {avg_rps:,} rps 24h avg, {n_feeds} streaming feeds "
                                f"(ceiling {GUARDRAILS['streaming_rps']:,})",
                                note=f"records in ValveWorkflow (streaming) batches bucketed by hour of creation; hourly average, "
                                     f"so sub-hour bursts are understated; busiest 24h: {busiest}",
                                detail={"streaming_records_24h": stream_rec24, "peak_hour_records": peak, "hours_with_data": hours,
                                        "by_feed_24h": dict(stream_by_feed.most_common(15))}))
        else:
            checks.append(check("streaming_rps", "Streaming ingestion to Profile (RPS)", 0, GUARDRAILS["streaming_rps"],
                                "no streaming micro-batches in the last 24h", status="green"))
        logger.info(f"Catalog: failed batches on those feeds, last 30 day(s)...")
        f_prof = {}
        for dsid in feeds:
            failed, _ = read_batches(headers, 30, status="failure", dataset_id=dsid,
                                     props="status,metrics,relatedObjects,created,errors")
            f_prof.update(failed)
        def size_hit(b):
            txt = json.dumps(b.get("errors") or []).lower()
            return any(mk in txt for mk in SIZE_LIMIT_MARKERS)
        size_drops = [b for b in f_prof.values() if size_hit(b)]
        checks.append(check("hard_size_drops_30d", "Hard size-limit drops (30 days)", len(size_drops), GUARDRAILS["hard_size_drops_30d"],
                            f"{len(size_drops)} on profile feeds ({len(f_prof)} failed batches on those feeds in 30d)",
                            note="failed-batch error text matched on size/limit markers; scope: " + scope_label,
                            detail={"failed_on_profile_feeds": len(f_prof), "examples": [
                                {"dataset": next((o.get("id") for o in b.get("relatedObjects") or [] if o.get("type") == "dataSet"), ""),
                                 "error": (json.dumps(b.get("errors"))[:160])} for b in list(f_prof.values())[:5]]}))
    else:
        for key, title in (("batches_per_day_profile", "Batches per day to Profile"),
                           ("batch_throughput", "Batch ingest throughput"),
                           ("hard_size_drops_30d", "Hard size-limit drops (30 days)"),
                           ("streaming_rps", "Streaming ingestion to Profile (RPS)")):
            checks.append(check(key, title, None, GUARDRAILS.get(key), "skipped (--no-batches)", status="grey"))

    # 15. edge throughput -- manual
    checks.append(check("edge_throughput", "Edge segmentation throughput", None, None, "manual",
                        method="manual", status="grey", note="no API surface; carry forward"))

    # 17. Adobe's new capacity metric -- manual
    checks.append(check("new_capacity_metric", "New capacity metric (Adobe rollout)", None, None, "manual",
                        method="manual", status="grey", note="scope with Adobe; carry forward"))

    logger.info(f"checks done in {time.perf_counter() - t0:.0f}s")
    return checks


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
DOT = {"red": "🔴", "amber": "🟠", "green": "🟢", "grey": "⚪"}


def load_history(sandbox) -> list:
    p = OUTPUT_DIR / f"guardrail_audit_history_{sandbox}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def load_baseline(sandbox) -> dict:
    p = BASELINE_DIR / f"guardrail_baseline_{sandbox}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_report(sandbox, checks, history, baseline, datestr) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    # Columns: baseline columns (oldest first) + previous run (if any) + now.
    cols = [(c["label"], c["values"]) for c in baseline.get("columns", [])]
    prev = history[-1] if history else None
    if prev:
        cols.append((f"Previous -- {prev['date']}", {c["key"]: c for c in prev["checks"]}))
    lines = [f"# AEP Guardrail Audit -- {sandbox}  ({datestr})", "",
             f"Measured by guardrail_audit.py v{SCRIPT_VERSION}. Rows marked *query* / *manual* are not measured by "
             f"the API pass; *query* rows carry the SQL to run in the JSON. RAG: red = over the guardrail, "
             f"amber = within {int((1-AMBER_AT)*100)}%, green = clear, grey = not measured this run.", ""]
    hdr = ["", "Check"] + [lab for lab, _ in cols] + [f"**Now -- {datestr}**"]
    lines.append("| " + " | ".join(hdr) + " |")
    lines.append("|" + "---|" * len(hdr))
    for c in checks:
        cells = [DOT[c["status"]], f"**{c['title']}**" + (f" *({c['method']})*" if c["method"] != "api" else "")]
        for _, vals in cols:
            v = vals.get(c["key"])
            if isinstance(v, dict):
                cells.append(f"{DOT.get(v.get('status', 'grey'), '')} {v.get('display', '')}".strip())
            else:
                cells.append(str(v) if v is not None else "--")
        cells.append(f"**{c['display']}**")
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "## Notes per check", ""]
    for c in checks:
        if c.get("note"):
            lines.append(f"- **{c['title']}** -- {c['note']}")
    reds = [c["title"] for c in checks if c["status"] == "red"]
    ambers = [c["title"] for c in checks if c["status"] == "amber"]
    lines += ["", f"**Over guardrail:** {', '.join(reds) or 'none'}.  **Approaching:** {', '.join(ambers) or 'none'}.", "",
              f"_guardrail_audit.py v{SCRIPT_VERSION} ({SCRIPT_DATE}) -- read-only API pass; "
              f"detail in guardrail_audit_{sandbox}_{datestr}.json_"]
    md = OUTPUT_DIR / f"guardrail_audit_{sandbox}_{datestr}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    (OUTPUT_DIR / f"guardrail_audit_{sandbox}_{datestr}.json").write_text(
        json.dumps({"sandbox": sandbox, "date": datestr, "version": SCRIPT_VERSION, "checks": checks}, indent=1),
        encoding="utf-8")
    history = [h for h in history if h.get("date") != datestr] + [
        {"date": datestr, "version": SCRIPT_VERSION,
         "checks": [{k: c[k] for k in ("key", "title", "value", "display", "status", "method")} for c in checks]}]
    (OUTPUT_DIR / f"guardrail_audit_history_{sandbox}.json").write_text(json.dumps(history, indent=1), encoding="utf-8")
    return md


def print_console(checks):
    print()
    for c in checks:
        tag = {"red": "\033[31mRED  \033[0m", "amber": "\033[33mAMBER\033[0m",
               "green": "\033[32mGREEN\033[0m", "grey": "\033[2mgrey \033[0m"}[c["status"]]
        print(f"  {tag}  {c['title']:<42} {c['display']}")
    print()


# ----------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    sandbox, days, exclude_system, do_batches, positional = "prod", 7, True, True, []
    for a in args:
        if a.startswith("--sandbox="):
            sandbox = a.split("=", 1)[1]
        elif a.startswith("--days="):
            days = max(1, int(a.split("=", 1)[1]))
        elif a == "--include-system":
            exclude_system = False
        elif a == "--no-batches":
            do_batches = False
        elif a.startswith("-"):
            continue
        else:
            positional.append(a)
    service = positional[0] if positional else aep_creds.pick_service(None) if hasattr(aep_creds, "pick_service") else None
    if not service:
        print(__doc__); return
    conf = aep_creds.load_creds(service)
    token = authenticate(conf)
    headers = aep_headers(token, conf, sandbox)
    print(f"\n  AEP Guardrail Estimator v{SCRIPT_VERSION}  --  {sandbox}  (credential {service})\n")
    checks = run_checks(headers, sandbox, days, exclude_system, do_batches)
    datestr = datetime.now().strftime("%Y-%m-%d")
    md = write_report(sandbox, checks, load_history(sandbox), load_baseline(sandbox), datestr)
    print_console(checks)
    print(f"  Report: {md}\n")


if __name__ == "__main__":
    main()
