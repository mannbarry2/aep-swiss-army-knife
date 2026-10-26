"""
data_dictionary/lineage_sources_journeys_and_destinations.py  (AEP Swiss Army Knife)
=============================================================
Library: WHERE DATA COMES FROM AND WHERE AUDIENCES GO -- the two ends of the
lineage the schemas and fields sit between. Used by the Data Dictionary for its
Sources, Journeys and Destinations tabs, the Datasets tab's "Fed by" column and
the Audiences tab's "Where it goes" columns -- so an audience nothing uses can
be unpublished, and a dataset can be traced back to what fills it.

  * sources: every dataflow that brings data into a dataset -- streaming
    (HTTP API inlets), files from cloud storage (Azure Blob, S3, SFTP...),
    files uploaded in the UI, the Edge (web / app), and SQL jobs (Data
    Distiller), which also name the datasets they read. Adobe's own
    housekeeping flows (Profile and identity loading, TTL, lookups) are left out.
  * journeys: every Adobe Journey Optimizer journey version, and the audiences
    it reads (a Read Audience node) or starts from (Audience Qualification).
    extract_audiences() is the journey census's, moved here so there is one of
    it: journeys_and_audiences_census.py imports it from this module.
  * destinations: every destination dataflow and the audiences it activates
    (the flow detail's transformations[].params.segmentSelectors), as found by
    streaming_audiences_high_throughput_upgrade_triage.py.

AJO campaigns are not read separately: the API shows nearly all of them as the
messages inside journeys (metadata.sourceType "journey"), and a campaign
carries no audience id.

Read-only. The caller passes get_json(url) -> dict, so authentication, retries
and endpoint timing stay the caller's. Stdlib only.
"""

from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor

PLATFORM = "https://platform.adobe.io"
JOURNEYS_URL = f"{PLATFORM}/ajo/journey"
JOURNEY_FILTER = "metadata.lastModifiedAt>2000-01-01"   # the default lists recent ones only
MAX_PAGES = 500
FLOWSERVICE = f"{PLATFORM}/data/foundation/flowservice"

# Sending now. Closed takes no new entrants but finishes those already in it.
# Dry Run and Testing send nothing real.
LIVE_STATUSES = {"deployed", "live", "closed"}
VIA_WORDS = {"read_audience": "Read Audience", "qualification": "Audience Qualification",
             "audiences": "audience"}
# Connection specs that are AEP's own plumbing, never a marketing destination.
INTERNAL_SPECS = ("datalake", "dwh", "activation-ups", "activation-datalake", "entity lookup",
                  "ups-atlas", "uis identities", "uis graphs", "dataretention", "ups segments",
                  "ups-snapshot")


def extract_audiences(journey: dict) -> list[dict]:
    """Pull every audience/segment reference out of a journey definition,
    regardless of how it's used. Returns a de-duped list of
    {audience_id, audience_name, via} dicts.

    Two shapes are handled, plus a recursive catch-all so we don't miss a
    variant we haven't seen yet:
      * read-audience nodes: a node carrying an "audiences":[{id,name}] list.
      * audience qualification: an entry/event referencing a segment, seen as
        segmentId / audienceId / segment.{id,name} keys.
    (Moved here from journeys_and_audiences_census.py, which imports it.)
    """
    found: dict[str, dict] = {}

    def add(aid, name, via):
        if not aid:
            return
        cur = found.get(aid)
        if cur is None:
            found[aid] = {"audience_id": aid, "audience_name": name or "", "via": via}
        else:
            if name and not cur["audience_name"]:
                cur["audience_name"] = name
            if via not in cur["via"]:
                cur["via"] = f"{cur['via']}+{via}"

    def walk(obj, node_type=None):
        if isinstance(obj, dict):
            nt = obj.get("type") or node_type
            auds = obj.get("audiences")
            if isinstance(auds, list):
                for a in auds:
                    if isinstance(a, dict):
                        # Label by the actual node type: a read-audience node vs
                        # an 'audience_qualification' entry/trigger node (confirmed
                        # node type); fall back to a generic tag otherwise.
                        ntl = str(nt).lower() if nt else ""
                        via = ("read_audience" if "read" in ntl
                               else "qualification" if "qualif" in ntl else "audiences")
                        add(a.get("id") or a.get("audienceId"), a.get("name"), via)
            for key in ("segmentId", "audienceId"):
                if obj.get(key):
                    add(obj[key], obj.get("name"), "qualification")
            seg = obj.get("segment") or obj.get("audience")
            if isinstance(seg, dict) and (seg.get("id") or seg.get("segmentId")):
                add(seg.get("id") or seg.get("segmentId"), seg.get("name"), "qualification")
            for v in obj.values():
                walk(v, nt)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, node_type)

    walk(journey)
    return list(found.values())


def _via_words(via: str) -> str:
    return " + ".join(VIA_WORDS.get(v, v) for v in via.split("+"))


def read_journeys(get_json, workers: int = 6) -> tuple[list[dict], int]:
    """([journey], unreadable) -- every journey version with the audiences it
    reads. A definition that cannot be read keeps its row, audiences unknown."""
    listing, page = {}, 0
    while page < MAX_PAGES:
        q = urllib.parse.urlencode({"filter": JOURNEY_FILTER, "page": page, "pageSize": 100})
        d = get_json(f"{JOURNEYS_URL}?{q}") or {}
        res = d.get("results") or []
        for j in res:
            if j.get("id"):
                listing[j["id"]] = j
        total = (d.get("pagination") or {}).get("totalCount")
        page += 1
        if not res or (total is not None and len(listing) >= total):
            break

    def one(jid):
        try:
            return jid, get_json(f"{JOURNEYS_URL}/{urllib.parse.quote(jid, safe='')}")
        except Exception:  # noqa: BLE001 -- counted, and the row says so
            return jid, None

    with ThreadPoolExecutor(max(1, workers)) as ex:
        details = dict(ex.map(one, list(listing)))
    out, unreadable = [], 0
    for jid, item in listing.items():
        d = details.get(jid)
        unreadable += d is None
        meta = item.get("metadata") or (d or {}).get("metadata") or {}
        status = str(item.get("status") or (d or {}).get("status") or "")
        out.append({
            "id": jid, "name": item.get("name") or "", "version": str(item.get("version") or ""),
            "status": status, "live": status.lower() in LIVE_STATUSES,
            "audiences": None if d is None else [
                {"id": a["audience_id"], "name": a["audience_name"], "via": _via_words(a["via"])}
                for a in extract_audiences(d)],
            "changed": str(meta.get("lastModifiedAt") or "")[:16].replace("T", " "),
            "by": meta.get("lastModifiedBy") or meta.get("createdBy") or ""})
    out.sort(key=lambda j: (not j["live"], j["name"].lower(), j["version"]))
    return out, unreadable


def _flow_paged(get_json, url):
    """Flow Service pages by _links.next.href, relative to the flowservice base."""
    out, full, page = [], f"{url}?limit=100", 0
    while full and page < MAX_PAGES:
        page += 1
        d = get_json(full) or {}
        items = d.get("items") or []
        out.extend(items)
        nxt = ((d.get("_links") or {}).get("next") or {}).get("href")
        full = (nxt if nxt.startswith("http") else f"{FLOWSERVICE}{nxt}") if nxt and items else None
    return out


def read_flow_service(get_json) -> dict:
    """One sweep of Flow Service: flows, their specs, source and target
    connections. Shared by read_sources() and read_destinations()."""
    def by_id(path):
        return {x.get("id"): x for x in _flow_paged(get_json, f"{FLOWSERVICE}/{path}") if x.get("id")}
    return {"flows": _flow_paged(get_json, f"{FLOWSERVICE}/flows"),
            "flow_specs": {k: v.get("name") or "" for k, v in by_id("flowSpecs").items()},
            "conn_specs": {k: v.get("name") or "" for k, v in by_id("connectionSpecs").items()},
            "sources": by_id("sourceConnections"), "targets": by_id("targetConnections")}


# How data comes IN, in words a reader knows, by the dataflow's flow spec.
SOURCE_KINDS = {"stream data without transformation": "Streaming (HTTP API)",
                "http api": "Streaming (HTTP API)",
                "fileupload": "File uploaded in the UI",
                "datastream_to_dataset": "Web / app (Edge datastream)",
                "query service": "SQL job (Data Distiller)",
                "ups export": "Profile export to a dataset"}
FILE_STORES = {"azure-blob": "Azure Blob", "s3": "Amazon S3", "amazon-s3": "Amazon S3", "sftp": "SFTP",
               "google-cloud": "Google Cloud Storage", "gcs": "Google Cloud Storage",
               "adls-gen2": "Azure Data Lake", "data-landing-zone": "Data Landing Zone"}
# Flow specs that are Adobe's own housekeeping between its stores, not a way in.
HOUSEKEEPING_FLOWS = {"lakehouse ttl", "psi", "pss", "dataset_to_uis", "dataset_to_ups",
                      "lookup store flow", "campaign orchestration flow"}


def _ms_utc(v) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(v) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def read_sources(fs: dict) -> list[dict]:
    """Every dataflow that brings data INTO a dataset: [{kind, name, live,
    datasets: [id], reads: [dataset id], detail, last_run, last_state, by, id}].
    Streaming names its inlet, cloud storage its folder, a SQL job the datasets
    it reads; Adobe's housekeeping flows are left out."""
    out = []
    for f in fs["flows"]:
        spec = fs["flow_specs"].get((f.get("flowSpec") or {}).get("id"), "")
        if spec.lower() in HOUSEKEEPING_FLOWS:
            continue
        tgts = [fs["targets"].get(t) or {} for t in f.get("targetConnectionIds") or []]
        if not tgts or not all("datalake" in fs["conn_specs"].get((t.get("connectionSpec") or {}).get("id"), "")
                               for t in tgts):
            continue                     # not into a dataset: a destination, or Adobe's own stores
        srcs = [fs["sources"].get(s) or {} for s in f.get("sourceConnectionIds") or []]
        src_spec = next((fs["conn_specs"].get((s.get("connectionSpec") or {}).get("id"), "") for s in srcs), "")

        def ids(conns):
            got = []
            for c in conns:
                p = c.get("params") or {}
                got += [p["dataSetId"]] if p.get("dataSetId") else [d.get("datasetId") for d in p.get("datasets") or []]
            return [x for x in dict.fromkeys(got) if x]

        kind = SOURCE_KINDS.get(spec.lower())
        if not kind:
            store = FILE_STORES.get(src_spec.lower())
            kind = f"Files from {store}" if store else (src_spec or spec or "Other")
        sp = next((s.get("params") or {} for s in srcs), {})
        detail = sp.get("name") and f"inlet: {sp['name']}" or sp.get("path") and f"folder: {sp['path']}" or ""
        run = f.get("lastRunDetails") or {}
        out.append({"kind": kind, "name": " ".join(str(f.get("name") or "").split())[:200],
                    "live": str(f.get("state") or "").lower() == "enabled",
                    "datasets": ids(tgts), "reads": ids(srcs) if kind.startswith("SQL") else [],
                    "detail": detail, "last_run": _ms_utc(run.get("completedAtUTC") or run.get("startedAtUTC")),
                    "last_state": run.get("state") or "", "by": f.get("createdBy") or "", "id": f.get("id") or ""})
    out.sort(key=lambda s: (s["kind"], not s["live"], s["name"].lower()))
    return out


def fed_by(sources: list[dict]) -> str:
    """One line for a dataset's 'Fed by' cell: each way in, with the dataflow
    names when there are one or two, or a count when there are more (a file
    uploaded by hand makes a new dataflow every time). Switched-off ones say so."""
    groups: dict[str, list[dict]] = {}
    for s in sources:
        groups.setdefault(s["kind"], []).append(s)
    parts = []
    for kind, ss in groups.items():
        names = list(dict.fromkeys(s["name"][:60] for s in ss))
        off = sum(not s["live"] for s in ss)
        what = ", ".join(names) if len(names) <= 2 else f"{len(ss)} dataflows"
        parts.append(f"{kind}: {what}" + (f" ({off} switched off)" if off else ""))
    return "; ".join(parts)


def read_destinations(get_json, fs: dict | None = None) -> list[dict]:
    """Every destination dataflow that sends audiences out: [{name, destination,
    live, audiences: [id]}]. The /flows list leaves out the audiences, so each
    candidate flow's detail is read."""
    fs = fs or read_flow_service(get_json)
    flows = fs["flows"]
    targets = {k: fs["conn_specs"].get((t.get("connectionSpec") or {}).get("id"), "")
               for k, t in fs["targets"].items()}

    def external(ids):
        return [targets[t] for t in ids or [] if t in targets
                and not any(s in targets[t].lower() for s in INTERNAL_SPECS)]

    out = []
    for f in flows:
        if not external(f.get("targetConnectionIds")):
            continue
        d = get_json(f"{FLOWSERVICE}/flows/{urllib.parse.quote(str(f.get('id')), safe='')}") or {}
        d = (d.get("items") or [d])[0]
        ids = []
        for tr in d.get("transformations") or []:
            for s in (((tr.get("params") or {}).get("segmentSelectors") or {}).get("selectors") or []):
                v = s.get("value") if isinstance(s, dict) else None
                if isinstance(v, dict) and (v.get("id") or v.get("systemSegmentId")):
                    ids.append(str(v.get("id") or v.get("systemSegmentId")))
        if not ids:
            continue
        state = str(d.get("state") or f.get("state") or "").lower()
        out.append({"name": d.get("name") or f.get("name") or "",
                    "destination": ", ".join(dict.fromkeys(external(d.get("targetConnectionIds")
                                                                   or f.get("targetConnectionIds")))),
                    "live": state in ("enabled", "active", ""), "id": str(f.get("id") or ""),
                    "audiences": list(dict.fromkeys(ids))})
    out.sort(key=lambda x: (not x["live"], x["destination"].lower(), x["name"].lower()))
    return out


def usage_by_audience(journeys: list[dict], destinations: list[dict]) -> dict:
    """{audience id: {"live": [journey], "other": [journey], "dest_on": [flow], "dest_off": [flow]}}"""
    use: dict[str, dict] = {}
    for j in journeys:
        for a in j["audiences"] or []:
            use.setdefault(a["id"], {}).setdefault("live" if j["live"] else "other", []).append(j)
    for f in destinations:
        for aid in f["audiences"]:
            use.setdefault(aid, {}).setdefault("dest_on" if f["live"] else "dest_off", []).append(f)
    return use


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def where_it_goes(use: dict, built_on: int = 0) -> str:
    """One line a reader can filter on: 'In use: ...', 'Not live: ...' or
    'Nothing found using it'. Another audience built on this one counts as a
    use -- unpublishing it would break that one."""
    live = len({j["name"] for j in use.get("live", [])})
    other = len({j["name"] for j in use.get("other", [])})
    on, off = len(use.get("dest_on", [])), len(use.get("dest_off", []))
    bits = ([_n(live, "live journey")] if live else []) + ([_n(on, "live destination")] if on else []) \
        + ([f"{_n(built_on, 'audience')} built on it"] if built_on else [])
    if bits:
        return "In use: " + ", ".join(bits)
    if other or off:
        return "Not live: only in " + ", ".join(
            ([_n(other, "journey that is not running", "journeys that are not running")] if other else [])
            + ([_n(off, "switched-off destination")] if off else []))
    return "Nothing found using it"
