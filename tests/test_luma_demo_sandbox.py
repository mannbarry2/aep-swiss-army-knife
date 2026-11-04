"""Offline tests for ingestion/luma_demo_sandbox_builder_and_event_feeder.py and its
ingestion/luma_demo_sandbox/ modules: the tenant swap and time shift, the synthetic
events, setup's lookup-before-create, batch and streaming calls, and the demo-only
guard. No Adobe calls: HTTP goes to a fake session."""
import contextlib
import io
import json
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
from ingestion import luma_demo_sandbox_builder_and_event_feeder as tool  # noqa: E402
from ingestion.luma_demo_sandbox import batch, client, datasets, registry, stream, synth, transform  # noqa: E402

results = []
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
CONF = {"client_id": "cid", "client_secret": "not-a-real-secret", "org_id": "ORG@AdobeOrg"}


def check(name, ok, got=""):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL"), name, got if not ok else "")


def response(method, url, status=200, body=None, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = b"" if body is None else json.dumps(body).encode()
    resp.headers.update(headers or {})
    resp.url = url
    resp.request = requests.Request(method, url).prepare()
    return resp


class FakeSession:
    """Answers Adobe calls from `handler(method, url, kwargs) -> (status, body[, headers])`."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.token_calls = 0

    def post(self, url, data=None, timeout=None):        # the IMS token request
        self.token_calls += 1
        return response("POST", url, 200, {"access_token": f"token-{self.token_calls}", "expires_in": 86400})

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": headers, **kwargs})
        status, body, *extra = self.handler(method, url, kwargs)
        return response(method, url, status, body, extra[0] if extra else None)


def platform(handler):
    session = FakeSession(handler)
    return client.Platform(CONF, "dev", session=session, log=lambda line: None), session


def quiet(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        value = fn(*args, **kwargs)
    return value, out.getvalue()


# --- transform -----------------------------------------------------------------
row = {"_yourTenantId": {"systemIdentifier": {"crmId": "_yourTenantId"}}, "items": [{"_yourTenantId": {"sku": "A"}}]}
check("tenant swap renames keys at any depth, never values",
      transform.swap_tenant(row, "_acme") == {"_acme": {"systemIdentifier": {"crmId": "_yourTenantId"}},
                                              "items": [{"_acme": {"sku": "A"}}]})

events = [{"_id": "old", "timestamp": t} for t in ("2022-11-01T00:00:00Z", "2022-11-10T06:30:00Z", "2022-11-29T00:00:00Z")]
times = [transform.parse_ts(r["timestamp"]) for r in transform.shift_timestamps(events, NOW, 30)]
check("time shift: latest event is now, spacing kept when it fits the window",
      times[-1] == NOW and times[2] - times[0] == timedelta(days=28) and times[1] - times[0] == timedelta(days=9, hours=6, minutes=30))

long_span = [{"timestamp": t} for t in ("2021-11-15T00:00:00Z", "2022-05-15T00:00:00Z", "2022-11-15T00:00:00Z")]
times = [transform.parse_ts(r["timestamp"]) for r in transform.shift_timestamps(long_span, NOW, 30)]
check("time shift: a year-long file is squeezed into the 30 days, in order",
      times == sorted(times) and times[-1] == NOW and NOW - times[0] == timedelta(days=30))
times = [transform.parse_ts(r["timestamp"]) for r in transform.shift_timestamps(long_span, NOW, None)]
check("time shift: no window means no squeeze", times[2] - times[0] == timedelta(days=365))

ids = [r["_id"] for r in transform.regenerate_ids(events)]
check("fresh, unique uuid4 _ids", len(set(ids)) == 3 and "old" not in ids and all(len(i) == 36 for i in ids))

counts, recent = [], True
for ds in datasets.DATASETS:
    rows = transform.prepare(datasets.read_rows(ds), tenant_id="_acme", is_event=ds.is_event, now=NOW)
    counts.append(len(rows))
    if "_yourTenantId" in transform.to_json_file(rows).decode():
        recent = False
    if ds.is_event:
        stamps = [transform.parse_ts(r["timestamp"]) for r in rows]
        recent &= max(stamps) == NOW and NOW - min(stamps) <= timedelta(days=30)
check("Adobe's files: 1000/1000/101/3/1000/1000/1000 rows", counts == [1000, 1000, 101, 3, 1000, 1000, 1000], counts)
check("Adobe's files: no _yourTenantId left, every event in the last 30 days", recent)

loyalty = transform.prepare(datasets.read_rows(datasets.BY_KEY["loyalty"]), tenant_id="_t", is_event=False, now=NOW)
purchases = transform.prepare(datasets.read_rows(datasets.BY_KEY["offline-purchases"]), tenant_id="_t", is_event=True, now=NOW)
members = {r["_t"]["systemIdentifier"]["loyaltyId"] for r in loyalty}
check("identity overlap survives: every purchase's loyaltyId is a member",
      all(p["_t"]["systemIdentifier"]["loyaltyId"] in members for p in purchases))
check("batch file round-trips", json.loads(transform.to_json_file([{"a": 1}, {"b": [2]}])) == [{"a": 1}, {"b": [2]}])

# --- synth ---------------------------------------------------------------------
pools = synth.load_pools()
check("mix split is exact: 20 -> 10/8/2",
      synth.split_counts(20, synth.DEFAULT_MIX) == {"web-events": 10, "offline-purchases": 8, "inventory-events": 2})
check("mix split always sums to n", all(sum(synth.split_counts(n, synth.DEFAULT_MIX).values()) == n for n in (0, 1, 7, 333)))
try:
    synth.split_counts(5, {"web-events": 0})
    check("an all-zero mix is refused", False)
except ValueError:
    check("an all-zero mix is refused", True)

out = synth.generate(300, pools=pools, tenant_id="_acme", now=NOW, rng=random.Random(7))
loyalty_ids, crm_ids, skus = set(pools.loyalty_ids), set(pools.crm_ids), {s for s, _ in pools.products}
prices = dict(pools.products)
check("offline purchases: real loyaltyIds, 1-4 real SKUs, priceTotal from catalogue prices",
      all(e["_acme"]["systemIdentifier"]["loyaltyId"] in loyalty_ids
          and 1 <= len(e["productListItems"]) <= 4
          and all(i["SKU"] in skus for i in e["productListItems"])
          and e["commerce"]["order"]["priceTotal"] == round(sum(prices[i["SKU"]] * i["quantity"] for i in e["productListItems"]), 2)
          for e in out["offline-purchases"]))
check("web events: real lumaCrmId with the ECID it already has, real SKUs",
      all(e["identityMap"]["lumaCrmId"][0]["id"] in crm_ids
          and e["identityMap"]["ECID"][0]["id"] == pools.ecid_by_crm[e["identityMap"]["lumaCrmId"][0]["id"]]
          and e["eventType"] in synth.WEB_EVENT_TYPES
          and all(i["SKU"] in skus for i in e["productListItems"]) for e in out["web-events"]))
adobe_types = {r["eventType"] for r in datasets.read_rows(datasets.BY_KEY["web-events"])}
check("web event types are exactly the ones in Adobe's web file",
      set(synth.WEB_EVENT_TYPES) == adobe_types, sorted(adobe_types))
check("inventory events: real SKU, the field group's enum",
      all(e["_acme"]["inventoryEvent"]["sku"] in skus
          and e["_acme"]["inventoryEvent"]["stockEventType"] in ("restock", "outOfStock") for e in out["inventory-events"]))
every = [r for rows in out.values() for r in rows]
check("timestamps in the last 60 minutes, _ids unique",
      all(NOW - timedelta(minutes=60) <= transform.parse_ts(r["timestamp"]) <= NOW for r in every)
      and len({r["_id"] for r in every}) == len(every))
check("an unknown CRM id gets a stable 38-digit ECID",
      synth.ecid_for("nope", pools) == synth.ecid_for("nope", pools) and len(synth.ecid_for("nope", pools)) == 38)

# --- registry (setup) ------------------------------------------------------------
steps = registry.load_steps()
kinds = [s["kind"] for s in steps]
check("port covers every structure request in collections 1-7",
      {k: kinds.count(k) for k in set(kinds)} == {"datatype": 1, "class": 2, "fieldgroup": 4, "namespace": 3,
                                                  "schema": 7, "descriptor": 13, "dataset": 7})


def everything_exists():
    """Every lookup finds its object: the second run of setup."""
    variables, descriptors = {"TENANT_ID": "_acme"}, []
    for step in steps:
        body = registry.render(step, variables, allow_unknown=True)
        if step["kind"] in registry.SCHEMA_REGISTRY_KINDS:
            for var, src in step["save"].items():
                variables[var] = f"https://ns.adobe.com/acme/{body['title']}" if src == "$id" else "_alt"
        if step["kind"] == "descriptor":
            descriptors.append(body)
    codes = [registry.render(s, {}, allow_unknown=True)["code"] for s in steps if s["kind"] == "namespace"]

    def handler(method, url, kwargs):
        if "title==" in url:
            title = unquote(re.search(r"title==([^&]+)", url).group(1))
            return 200, {"results": [{"title": title, "$id": f"https://ns.adobe.com/acme/{title}", "meta:altId": "_alt"}]}
        if url.endswith("/idnamespace/identities"):
            return 200, [{"code": c, "id": i} for i, c in enumerate(codes)]
        if "/catalog/dataSets" in url:
            return 200, {f"ds-{len(kwargs['params']['name'])}": {"name": kwargs["params"]["name"]}}
        if url.endswith("/descriptors") and method == "GET":
            return 200, {"xdm:descriptorIdentity": descriptors}
        return 500, {"unexpected": url}
    return handler


api, session = platform(everything_exists())
(variables, counts), _ = quiet(registry.run_setup, api, {"TENANT_ID": "_acme"}, apply=True, log=lambda line: None)
check("second setup run: all 37 exist, nothing POSTed",
      counts == {"exists": 37, "created": 0, "missing": 0} and not [c for c in session.calls if c["method"] == "POST"], counts)
check("existing ids are picked up for load/feed",
      variables["luma_web_events_schema_id"] == "https://ns.adobe.com/acme/Luma Web Events Schema"
      and variables["loyalty_dataset_id"].startswith("ds-"))


def nothing_exists(method, url, kwargs):
    if method == "GET":
        if "title==" in url:
            return 200, {"results": []}
        if url.endswith("/idnamespace/identities"):
            return 200, []
        return 200, {}
    body = kwargs["json"]
    if "/catalog/dataSet" in url:
        return 201, [f"@/dataSets/id-{body['name'].replace(' ', '')}"]
    if "/idnamespace/" in url:
        return 200, {"id": 99, "code": body["code"]}
    if url.endswith("/descriptors"):
        return 201, {"@id": "d1", **body}
    return 201, {"$id": f"https://ns.adobe.com/acme/{body['title']}", "meta:altId": "_alt"}


api, session = platform(nothing_exists)
(_, counts), _ = quiet(registry.run_setup, api, {"TENANT_ID": "_acme"}, apply=False, log=lambda line: None)
check("read-only setup: 37 missing, nothing POSTed",
      counts["missing"] == 37 and not [c for c in session.calls if c["method"] == "POST"], counts)

api, session = platform(nothing_exists)
(variables, counts), _ = quiet(registry.run_setup, api, {"TENANT_ID": "_acme"}, apply=True, log=lambda line: None)
posts = [c["json"] for c in session.calls if c["method"] == "POST"]
primary = next(p for p in posts if p.get("xdm:sourceProperty") == "/_acme/systemIdentifier/loyaltyId")
field_group = next(p for p in posts if p.get("title") == "Luma Identity Profile Field Group")
check("first setup run with --apply: 37 created", counts["created"] == 37 and len(posts) == 37, counts)
check("ids chain: the loyalty descriptor points at the schema just created",
      primary["xdm:sourceSchema"] == "https://ns.adobe.com/acme/Luma Loyalty Schema")
check("ids chain: the field group embeds the data type, under the tenant key",
      field_group["definitions"]["property"]["properties"]["_acme"]["properties"]["systemIdentifier"]["$ref"]
      == "https://ns.adobe.com/acme/System Identifier")
check("dataset id taken from Catalog's ['@/dataSets/<id>']", variables["luma_web_events_dataset_id"] == "id-LumaWebEventsDataset")
try:
    registry.render(next(s for s in steps if s["kind"] == "descriptor"), {"TENANT_ID": "_acme"})
    check("a missing id is an error when writing", False)
except KeyError:
    check("a missing id is an error when writing", True)

# --- client ------------------------------------------------------------------------
replies = iter([(429, None, {"Retry-After": "1"}), (200, {"tenantId": "acme"})])
logged = []
api, session = platform(lambda m, u, k: next(replies))
api.log = logged.append
client_sleep = client.time.sleep
client.time.sleep = lambda s: None
try:
    got = api.get_json("/data/foundation/schemaregistry/stats")
finally:
    client.time.sleep = client_sleep
headers = session.calls[-1]["headers"]
check("429 is retried, and the retry names the endpoint and reason",
      got == {"tenantId": "acme"} and any("/data/foundation/schemaregistry: HTTP 429" in line for line in logged), logged)
check("every call carries token, api key, org and sandbox",
      headers["Authorization"].startswith("Bearer token-") and headers["x-api-key"] == "cid"
      and headers["x-gw-ims-org-id"] == "ORG@AdobeOrg" and headers["x-sandbox-name"] == "dev")
check("endpoint health report counts the retry",
      any("retried 1x (HTTP 429)" in line for line in api.health.report()), api.health.report())

replies = iter([(401, None), (200, {})])
api, session = platform(lambda m, u, k: next(replies))
api.get_json("/data/foundation/catalog/dataSets")
check("401 fetches a fresh token once", session.token_calls == 2)

api, _ = platform(lambda m, u, k: (403, {"title": "no READ permission"}))
try:
    api.get_json("/x")
    check("an Adobe error carries its body", False)
except client.ApiError as err:
    check("an Adobe error carries its body", err.status == 403 and "READ permission" in str(err))

# --- batch and stream ----------------------------------------------------------------
def batch_handler(method, url, kwargs):
    if method == "POST" and url.endswith("/import/batches"):
        return 201, {"id": "B1"}
    return 200, None


api, session = platform(batch_handler)
check("batch: create -> upload -> COMPLETE", batch.ingest(api, "DS", "f.json", b"[]") == "B1")
check("batch: format json, octet-stream upload, COMPLETE last",
      session.calls[0]["json"] == {"datasetId": "DS", "inputFormat": {"format": "json"}}
      and session.calls[1]["headers"]["Content-Type"] == "application/octet-stream"
      and session.calls[2]["url"].endswith("?action=COMPLETE"))

replies = iter([(404, None), (200, {"B1": {"status": "processing"}}), (200, {"B1": {"status": "success"}})])
api, _ = platform(lambda m, u, k: next(replies))
check("Catalog polled through 404 and processing to success",
      batch.wait_for(api, "B1", interval=0, sleep=lambda s: None)["status"] == "success")
check("failed batch: Adobe's reasons are printed",
      batch.failure_reasons({"errors": [{"code": "101", "description": "bad field"}], "metrics": {"failedRecordCount": 3}})
      == ["101: bad field", "3 record(s) failed validation"])

msg = stream.envelope({"_id": "e1"}, schema_id="S", dataset_id="D", org_id="O@AdobeOrg")
check("streaming envelope: header schemaRef/imsOrgId/datasetId, body xdmEntity",
      msg["header"]["schemaRef"] == {"id": "S", "contentType": stream.CONTENT_TYPE}
      and msg["header"]["datasetId"] == "D" and msg["header"]["imsOrgId"] == "O@AdobeOrg"
      and msg["body"]["xdmEntity"] == {"_id": "e1"})
inlet = FakeSession(lambda m, u, k: (200, None))
stream.post(inlet, "https://dcs.adobedc.net/collection/abc", msg)
check("streaming POST goes to the inlet with syncValidation=false",
      inlet.calls[0]["params"] == {"syncValidation": "false"} and inlet.calls[0]["json"] == msg)

# --- the tool --------------------------------------------------------------------------
opts = tool.parse_args(["demo-cred", "load", "--sandbox=dev", "--only=products"])
check("command line: credential first, then the command and --sandbox=",
      opts.credential == "demo-cred" and opts.command == "load" and opts.sandbox == "dev" and not opts.apply)
for settings, label in (({}, "nothing listed"), ({"allowed_credentials": ["other"]}, "a different credential"),
                        ({"allowed_credentials": ["demo-cred"], "allowed_org_ids": ["DEMO@AdobeOrg"]}, "another org")):
    try:
        tool.check_allowed("demo-cred", CONF, settings)
        check(f"demo-only guard refuses: {label}", False)
    except tool.Refused:
        check(f"demo-only guard refuses: {label}", True)
tool.check_allowed("demo-cred", CONF, {"allowed_credentials": ["demo-cred"], "allowed_org_ids": ["ORG@AdobeOrg"]})
check("demo-only guard lets a listed credential in its listed org through", True)
try:
    tool.check_sandbox("other", {"allowed_sandboxes": ["dev"]})
    check("sandbox lock refuses a sandbox not in allowed_sandboxes", False)
except tool.Refused:
    check("sandbox lock refuses a sandbox not in allowed_sandboxes", True)
tool.check_sandbox("dev", {"allowed_sandboxes": ["dev"]})
check("sandbox lock lets the listed sandbox through", True)

api, session = platform(lambda m, u, k: (500, {"unexpected": u}))
state = {"tenant_id": "acme", "vars": {}}
code, printed = quiet(tool.cmd_load, api, tool.parse_args(["demo-cred", "load", "--sandbox=dev"]), state)
rows = [int(n) for n in re.findall(r"batch \d/7\s+\S+\s+(\d+) rows", printed)]
check("load without --apply: 7 batches planned, no Adobe call",
      code == 0 and rows == [1000, 1000, 101, 3, 1000, 1000, 1000] and not session.calls and "nothing sent" in printed, rows)
try:
    quiet(tool.cmd_load, api, tool.parse_args(["demo-cred", "load", "--sandbox=dev", "--apply"]), state)
    check("load --apply refuses before setup has created the datasets", False)
except tool.Refused:
    check("load --apply refuses before setup has created the datasets", True)

code, printed = quiet(tool.cmd_feed, api, tool.parse_args(["demo-cred", "feed", "--sandbox=dev", "--n=20", "--seed=3"]), state, {})
check("feed without --apply: envelopes printed, nothing sent",
      code == 0 and "10 x web-events" in printed and '"xdmEntity"' in printed and not session.calls)

ids = {}
for ds in datasets.DATASETS:
    ids[ds.dataset_var], ids[ds.schema_var] = f"ds-{ds.key}", f"schema-{ds.key}"
api, _ = platform(lambda m, u, k: (500, None))
inlet = FakeSession(lambda m, u, k: (200, None))
api.session = inlet
code, printed = quiet(tool.cmd_feed, api, tool.parse_args(
    ["demo-cred", "feed", "--sandbox=dev", "--n=20", "--apply", "--inlet=https://dcs.adobedc.net/collection/abc"]),
    {"tenant_id": "acme", "vars": ids}, {})
sent = [c["json"] for c in inlet.calls if "dcs.adobedc.net" in c["url"]]
check("feed --apply: 20 events streamed into the three event datasets",
      code == 0 and "feed: 20/20 accepted" in printed
      and {m["header"]["datasetId"] for m in sent} == {"ds-web-events", "ds-offline-purchases", "ds-inventory-events"}, printed)

# --- audiences ---------------------------------------------------------------------------
from ingestion.luma_demo_sandbox import audiences, cards  # noqa: E402

defs = audiences.definitions("_acme")
check("audiences: every one named Luma: ..., tenant swapped in, no {T} left",
      len(defs) == len(audiences.AUDIENCES) + len(audiences.CHILDREN) + len(audiences.STREAMING) and all(d["name"].startswith("Luma: ") for d in defs)
      and all("{T}" not in d["pql"] for d in defs)
      and all(d["parent"] in {x["name"] for x in defs} for d in defs if d.get("parent")) and any("_acme.systemIdentifier.loyaltyId" in d["pql"] for d in defs))
check("audiences: names are unique", len({d["name"] for d in defs}) == len(defs))


def tree(pql):
    return json.dumps({"tree": pql})


def segment_api(existing, old_names=()):
    """existing audiences carry today's rule, except old_names (an older rule form)."""
    created, patched = [], []
    seg = {n: f"seg-{i}" for i, n in enumerate(existing)}
    by_name = {d["name"]: d for d in defs}

    def stored(n):
        if n in old_names:
            return {"format": "pql/text", "value": "select X from xEvent where X.old"}
        return {"format": "pql/json", "value": tree(audiences.rule(by_name[n], seg))}

    def handler(method, url, kw):
        if method == "GET" and "/segment/definitions/" in url:
            sid = url.rsplit("/", 1)[1]
            name = next((n for n, i in seg.items() if i == sid), None)
            if name:
                return 200, {"id": sid, "expression": stored(name)}
            body = created[int(sid.split("-")[1]) - 1]
            return 200, {"id": sid, "expression": body["expression"]}
        if method == "GET" and url.endswith("/segment/definitions"):
            return 200, {"segments": [{"name": n, "id": seg[n], "expression": stored(n)} for n in existing]}
        if method == "POST" and url.endswith("/segment/conversion"):
            return 200, {"expression": {"format": "pql/json", "value": tree(kw["json"]["expression"]["value"])}}
        if method == "PATCH" and "/segment/definitions/" in url:
            patched.append((url.rsplit("/", 1)[1], kw["json"]))
            return 204, None
        if method == "GET" and "mergePolicies" in url:
            return 200, {"children": [{"id": "mp-other", "schema": {"name": "_xdm.context.experienceevent"}, "default": True},
                                      {"id": "mp-profile", "schema": {"name": "_xdm.context.profile"}, "default": True}]}
        if method == "POST" and url.endswith("/segment/definitions"):
            created.append(kw["json"])
            return 200, {"id": f"new-{len(created)}"}
        if method == "POST" and url.endswith("/segment/jobs"):
            return 200, {"id": "job-1"}
        if method == "GET" and url.endswith("/segment/jobs/job-1"):
            ids = [f"new-{i}" for i in range(1, len(created) + 1)]
            return 200, {"status": "SUCCEEDED", "metrics": {"segmentedProfileByStatusCounter":
                                                            {sid: {"realized": 7} for sid in ids}}}
        return 500, {"unexpected": url}
    api, session = platform(handler)
    return api, session, created, patched


api, session, created, patched = segment_api([defs[0]["name"]])
(ids, counts), printed = quiet(audiences.run_audiences, api, "_acme", apply=False)
check("audiences without apply: one lookup, nothing created",
      not created and counts == {"exists": 1, "created": 0, "missing": len(defs) - 1, "repaired": 0, "outdated": 0}
      and all(c["method"] == "GET" or c["url"].endswith("/segment/conversion") for c in session.calls), counts)
check("cards: the layout hash is String.hashCode of the stored rule (matches a UI-built audience)",
      cards.rule_hash("hello") == 99162322 and cards.rule_hash("polygenelubricants") == -2147483648)
(ids, counts), printed = quiet(audiences.run_audiences, api, "_acme", apply=True)
check("audiences with apply: only the missing created, on the Profile merge policy, batch-evaluated",
      len(created) == len(defs) - 1 and counts["created"] == len(defs) - 1 and len(ids) == len(defs)
      and all(b["mergePolicyId"] == "mp-profile" for b in created)
      and all(b["evaluationInfo"]["batch"]["enabled"] for b in created if not b["name"].startswith("Luma: Streaming")))
streamed = [b for b in created if b["name"].startswith("Luma: Streaming")]
kids = [b for b in created if " > " in b["name"]]
check("child audiences: inSegment(parent id) and their condition, after the parent",
      len(kids) == len(audiences.CHILDREN)
      and any(b["name"] == "Luma: All Buyers > California" and 'inSegment(\\"seg-0\\") and homeAddress.state' in
              b["expression"]["value"] for b in kids)
      and all("{PARENT}" not in b["expression"]["value"] for b in kids)
      and all('inSegment(\\"new-' in b["expression"]["value"] for b in kids if "All Buyers >" not in b["name"]))
check("streaming audiences: continuous evaluation on, batch off, 24-hour window",
      len(streamed) == len(audiences.STREAMING)
      and all(b["evaluationInfo"]["continuous"]["enabled"] and not b["evaluationInfo"]["batch"]["enabled"]
              for b in streamed) and all("24 hours before now" in b["expression"]["value"] for b in streamed))
check("audiences are stored as pql/json (converted from the PQL text)",
      all(b["expression"]["format"] == "pql/json" and "tree" in json.loads(b["expression"]["value"]) for b in created))
check("event rules are in the rule builder's CHAIN form (UI-editable), none in select form",
      all("select X from xEvent" not in d["pql"] for d in defs)
      and all("CHAIN(xEvent, timestamp, [C0: WHAT(" in d["pql"] for d in defs if "Buyers" in d["name"] and " > " not in d["name"]))
members = audiences.evaluate(api, [f"new-{i}" for i in range(1, len(created) + 1)], log=lambda line: None,
                             sleep=lambda s: None)
check("segment job: members read per audience", set(members.values()) == {7} and len(members) == len(created))
api, session, created, patched = segment_api([d["name"] for d in defs], old_names={defs[2]["name"]})
(ids, counts), printed = quiet(audiences.run_audiences, api, "_acme", apply=True)
check("an audience with an old rule is repaired in place (same id), the rest left alone, nothing created",
      not created and counts["repaired"] == 1
      and [p[0] for p in patched if p[1][0]["path"] == "/expression"] == ["seg-2"]
      and all(p[1][0]["value"]["format"] == "pql/json" for p in patched if p[1][0]["path"] == "/expression"), counts)
check("apply also gives Rule view cards to the audiences it can lay out, the rest stay code-view only",
      counts["cards"] == sum(p[1][0]["path"] == "/ansibleDataModel" for p in patched) > 0
      and counts["cards"] + counts["code_only"] == len(defs), counts)

# --- Rule view cards ---------------------------------------------------------------------
from ingestion.luma_demo_sandbox import cards  # noqa: E402

m = cards.card_model(audiences.tier("gold", "platinum"), {})
prof = m["dataModel"]["expression"]["profileAttributesContainer"]["items"]
check("cards: tier gold-or-platinum is one equals card with both values (as the UI lays it out)",
      len(prof) == 1 and prof[0]["component"]["id"] == "profile.loyalty.tier"
      and prof[0]["comparisonType"] == "equals" and prof[0]["value"] == ["gold", "platinum"], prof)
m = cards.card_model('inSegment("p-1") and homeAddress.state.equals("CA", false)', {"p-1": "Luma: All Buyers"})
prof = m["dataModel"]["expression"]["profileAttributesContainer"]["items"]
check("cards: a child is its parent's membership card and its condition",
      [r["component"]["id"] for r in prof] == ["profile.segmentMembership.segmentID._id", "profile.homeAddress.state"]
      and prof[0]["value"]["name"] == "Luma: All Buyers" and prof[1]["value"] == ["CA"], prof)
m = cards.card_model(audiences.has(audiences.WEB_BUY, audiences.within(30)), {})
ev = m["dataModel"]["expression"]["xEventAttributesContainer"]
check("cards: one event with an in-last-30-days window",
      ev["temporalConstraint"] == {"type": "in-last", "unit": "days", "subType": "in-last-constraint", "quantity": 30}
      and ev["items"][0]["items"][0]["container"]["items"][0]["component"]["id"] == "xEvent.eventType", ev)
m = cards.card_model(next(d["pql"] for d in defs if d["name"] == "Luma: Interested in Gear, Never Bought It"), {})
ev = m["dataModel"]["expression"]["xEventAttributesContainer"]
check("cards: an excluded event is its own card with exclude on, beside the included one",
      [c["exclude"] for c in ev["items"]] == [False, True] and ev["logicalOperator"] == "and", ev)
check("cards: product SKU starts-with is a rule on the list item",
      '"xEvent.productListItems.SKU"' in json.dumps(m) and '"startsWith"' in json.dumps(m))
check("cards: exists is the UI's isNotNull",
      cards.card_model("personalEmail.address.isNotNull()", {})["dataModel"]["expression"]
      ["profileAttributesContainer"]["items"][0]["comparisonType"] == "isNotNull")
check("cards: a window the UI can't show (more than N days ago) stays code-view only",
      cards.card_model(next(d["pql"] for d in defs if d["name"] == "Luma: New Customers - Last 14 Days"), {}) is None)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
