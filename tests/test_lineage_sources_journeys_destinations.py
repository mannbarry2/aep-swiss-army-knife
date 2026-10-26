"""Offline tests for DD v3.4.23 lineage: the library that reads sources,
journeys and destinations, and the Data Dictionary step that puts 'Where it
goes' on every audience."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
import data_dictionary_v3 as dd  # noqa: E402
from data_dictionary import lineage_sources_journeys_and_destinations as L  # noqa: E402

results = []


def check(name, ok, got=""):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL"), name, got)


# ---- journeys -----------------------------------------------------------------
journey = {"nodes": [{"type": "readSegment", "audiences": [{"id": "A", "name": "Opt-in"}]},
                     {"type": "audience_qualification", "segmentId": "B"}]}
got = {a["audience_id"]: a["via"] for a in L.extract_audiences(journey)}
check("journey: Read Audience and Audience Qualification", got == {"A": "read_audience", "B": "qualification"}, got)

pages = {0: {"results": [{"id": "j1", "name": "Live one", "version": "1.0", "status": "Deployed",
                          "metadata": {"lastModifiedAt": "2026-10-20T10:00:00Z", "lastModifiedBy": "Ann"}},
                         {"id": "j2", "name": "Old one", "version": "2.0", "status": "Stopped", "metadata": {}},
                         {"id": "j3", "name": "Broken", "version": "1.0", "status": "Draft", "metadata": {}}],
             "pagination": {"totalCount": 3}}}


def get_json(url):
    if "/ajo/journey?" in url:
        return pages[int(url.split("page=")[1].split("&")[0])]
    jid = url.rsplit("/", 1)[1]
    if jid == "j3":
        raise RuntimeError("HTTP 500")
    return journey if jid == "j1" else {"nodes": [{"type": "readSegment", "audiences": [{"id": "A"}]}]}


js, unreadable = L.read_journeys(get_json, workers=2)
check("journeys: every version listed, live first", [j["id"] for j in js] == ["j1", "j3", "j2"], [j["id"] for j in js])
check("journeys: Deployed is live, Stopped is not", js[0]["live"] and not js[-1]["live"])
check("journeys: an unreadable definition keeps its row, audiences unknown",
      unreadable == 1 and next(j for j in js if j["id"] == "j3")["audiences"] is None)
check("journeys: via in words", js[0]["audiences"][0]["via"] == "Read Audience", js[0]["audiences"])

# ---- sources ------------------------------------------------------------------
fs = {"flows": [
        {"id": "f1", "name": "Order  Dataflow", "state": "enabled", "flowSpec": {"id": "s-stream"},
         "sourceConnectionIds": ["sc1"], "targetConnectionIds": ["tc1"],
         "lastRunDetails": {"startedAtUTC": 1793012400000}, "createdBy": "IMS1"},
        {"id": "f2", "name": "Order Daily", "state": "disabled", "flowSpec": {"id": "s-cloud"},
         "sourceConnectionIds": ["sc2"], "targetConnectionIds": ["tc2"], "lastRunDetails": {}},
        {"id": "f3", "name": "INSERT INTO x", "state": "enabled", "flowSpec": {"id": "s-qs"},
         "sourceConnectionIds": ["sc3"], "targetConnectionIds": ["tc3"]},
        {"id": "f4", "name": "TTL", "state": "enabled", "flowSpec": {"id": "s-ttl"},
         "sourceConnectionIds": [], "targetConnectionIds": ["tc1"]},
        {"id": "f5", "name": "LiveRamp", "state": "enabled", "flowSpec": {"id": "s-dest"},
         "sourceConnectionIds": [], "targetConnectionIds": ["tc9"]}],
      "flow_specs": {"s-stream": "Stream data without transformation", "s-cloud": "CloudStorageToAEP",
                     "s-qs": "Query Service", "s-ttl": "Lakehouse TTL", "s-dest": "LiveRamp - Onboarding"},
      "conn_specs": {"cs-lake": "datalake", "cs-http": "Streaming Connection", "cs-az": "azure-blob",
                     "cs-lr": "LiveRamp - Onboarding"},
      "sources": {"sc1": {"connectionSpec": {"id": "cs-http"}, "params": {"name": "Order", "inletId": "x"}},
                  "sc2": {"connectionSpec": {"id": "cs-az"}, "params": {"path": "adobe-batch-data/orders/"}},
                  "sc3": {"connectionSpec": {"id": "cs-lake"}, "params": {"datasets": [{"datasetId": "dsA"}]}}},
      "targets": {"tc1": {"connectionSpec": {"id": "cs-lake"}, "params": {"dataSetId": "dsO"}},
                  "tc2": {"connectionSpec": {"id": "cs-lake"}, "params": {"dataSetId": "dsO"}},
                  "tc3": {"connectionSpec": {"id": "cs-lake"}, "params": {"datasets": [{"datasetId": "dsX"}]}},
                  "tc9": {"connectionSpec": {"id": "cs-lr"}, "params": {}}}}
src = {s["id"]: s for s in L.read_sources(fs)}
check("sources: housekeeping and destinations left out", set(src) == {"f1", "f2", "f3"}, set(src))
check("sources: streaming names its inlet", src["f1"]["kind"] == "Streaming (HTTP API)" and src["f1"]["detail"] == "inlet: Order"
      and src["f1"]["datasets"] == ["dsO"] and src["f1"]["name"] == "Order Dataflow", src["f1"])
check("sources: cloud storage in words, with its folder", src["f2"]["kind"] == "Files from Azure Blob"
      and src["f2"]["detail"] == "folder: adobe-batch-data/orders/" and not src["f2"]["live"], src["f2"])
check("sources: a SQL job names what it reads and writes", src["f3"]["kind"] == "SQL job (Data Distiller)"
      and src["f3"]["reads"] == ["dsA"] and src["f3"]["datasets"] == ["dsX"], src["f3"])
check("sources: last run in UTC", src["f1"]["last_run"] == "2026-10-26 11:00", src["f1"]["last_run"])
check("fed by: names, and switched-off flows said",
      L.fed_by([src["f1"], src["f2"]]) == "Streaming (HTTP API): Order Dataflow; Files from Azure Blob: Order Daily (1 switched off)",
      L.fed_by([src["f1"], src["f2"]]))
many = [dict(src["f1"], name=f"Upload {i}", kind="File uploaded in the UI") for i in range(5)]
check("fed by: many uploads are a count", L.fed_by(many) == "File uploaded in the UI: 5 dataflows", L.fed_by(many))

# ---- destinations ---------------------------------------------------------------
fs_d = dict(fs, flows=[fs["flows"][4]])
detail = {"items": [{"name": "LiveRamp", "state": "enabled", "targetConnectionIds": ["tc9"], "transformations": [
    {"params": {"segmentSelectors": {"selectors": [{"value": {"id": "A"}}, {"value": {"id": "C"}}]}}}]}]}
dst = L.read_destinations(lambda url: detail, fs_d)
check("destinations: audiences from the flow detail", dst and dst[0]["audiences"] == ["A", "C"]
      and dst[0]["destination"] == "LiveRamp - Onboarding" and dst[0]["live"], dst)

# ---- where it goes ----------------------------------------------------------------
use = L.usage_by_audience(js, dst)
check("where: a live journey and a live destination", L.where_it_goes(use["A"]) == "In use: 1 live journey, 1 live destination",
      L.where_it_goes(use["A"]))
check("where: only a stopped journey", L.where_it_goes(use.get("Z", {"other": [js[-1]]})) ==
      "Not live: only in 1 journey that is not running")
check("where: another audience built on it counts as in use", L.where_it_goes({}, 2) == "In use: 2 audiences built on it")
check("where: nothing", L.where_it_goes({}) == "Nothing found using it")

# ---- the Data Dictionary step -------------------------------------------------------
def row(aid, name, built_on=""):
    r = [""] * dd.AUD_USED_BY + [built_on]
    r[1], r[3] = name, aid
    return r


res = {"name": "roi-prod", "audiences": [row("A", "Opt-in"), row("K", "Base", "X"), row("N", "Nobody")]}
saved = (L.read_flow_service, L.read_sources, L.read_destinations, L.read_journeys)
L.read_flow_service = lambda g: fs
L.read_sources = lambda f: [src["f1"]]
L.read_destinations = lambda g, f: dst
L.read_journeys = lambda g: (js, 0)
try:
    dd.attach_lineage("tok", {"client_id": "c", "org_id": "o"}, res, {"directory": {}})
finally:
    L.read_flow_service, L.read_sources, L.read_destinations, L.read_journeys = saved
by = {r[3]: dict(zip(dd.AUDIENCE_COLUMNS, r)) for r in res["audiences"]}
check("dictionary: every audience row gets the four lineage columns",
      all(len(r) == len(dd.AUDIENCE_COLUMNS) for r in res["audiences"]))
check("dictionary: in use, with its journey and destination named",
      by["A"]["Where it goes"].startswith("In use") and by["A"]["Live journeys"] == "Live one"
      and by["A"]["Journeys not running"] == "Old one (Stopped)" and by["A"]["Destinations"] == "LiveRamp - Onboarding – LiveRamp",
      by["A"])
check("dictionary: built on by another audience is in use", by["K"]["Where it goes"] == "In use: 1 audience built on it",
      by["K"]["Where it goes"])
check("dictionary: the unpublish candidate", by["N"]["Where it goes"] == "Nothing found using it", by["N"]["Where it goes"])
check("dictionary: sources kept for the Sources tab", res["sources"] == [src["f1"]] and res["lineage_errors"] == [])

res2 = {"name": "roi-prod", "audiences": [row("N", "Nobody")]}
L.read_flow_service = lambda g: (_ for _ in ()).throw(RuntimeError("HTTP 403"))
L.read_journeys = lambda g: ([], 0)
try:
    dd.attach_lineage("tok", {"client_id": "c", "org_id": "o"}, res2, {})
finally:
    L.read_flow_service, L.read_sources, L.read_destinations, L.read_journeys = saved
check("dictionary: never 'nothing found' on a partial read",
      res2["audiences"][0][-4] == "Nothing found using it (journeys / destinations not fully read)"
      and res2["lineage_errors"], res2["audiences"][0][-4])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
