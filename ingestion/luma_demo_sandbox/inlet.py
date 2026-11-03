"""Phase 5: an HTTP API streaming inlet for the Luma event datasets.

One unauthenticated streaming connection (one inlet URL), and per event dataset
a source connection, a Data Lake target connection and a "Stream data without
transformation" dataflow. Messages say which dataset they are for in their
XDM header; the x-adobe-flow-id header pins the dataflow.

Looked up by name first, so running it twice creates nothing the second time.
Without apply it only reads.
"""
from __future__ import annotations

from typing import Any, Callable

from .client import Platform

FLOWSERVICE = "/data/foundation/flowservice"
HTTP_API = "bc7b00d6-623a-4dfc-9fdb-f1240aeadaeb"       # connectionSpec "Streaming Connection"
DATA_LAKE = "c604ff05-7f1a-43c0-8e18-33bf874cb11c"      # target connectionSpec
STREAM_FLOW = "d8a6f005-7eaf-4153-983e-e8574508b877"    # flowSpec "Stream data without transformation"
XDM_FULL = "application/vnd.adobe.xed-full+json;version=1"
NAME = "Luma demo streaming"

Log = Callable[[str], None]


def spec(spec_id: str) -> dict[str, str]:
    return {"id": spec_id, "version": "1.0"}


def by_name(api: Platform, kind: str, prop: str) -> dict[str, dict[str, Any]]:
    """Flow Service objects of one spec, keyed by name (deleted ones left out)."""
    found: dict[str, dict[str, Any]] = {}
    path = f"{FLOWSERVICE}/{kind}"
    params: dict[str, Any] | None = {"property": prop}
    while path:
        page = api.get_json(path, params=params)
        found.update({i["name"]: i for i in page.get("items", []) if i.get("state") != "deleted"})
        nxt = ((page.get("_links") or {}).get("next") or {}).get("href", "")
        # The next href is relative to the flowservice base.
        path, params = (f"{FLOWSERVICE}/{nxt.lstrip('/')}", None) if nxt else ("", None)
    return found


def inlet_url(connection: dict[str, Any]) -> str:
    return ((connection.get("auth") or {}).get("params") or {}).get("inletUrl", "")


def run_inlet(api: Platform, targets: dict[str, tuple[str, str]], *, apply: bool,
              log: Log = print) -> dict[str, Any]:
    """targets: {dataset key: (dataset id, schema id)}.

    Returns {"connection_id", "inlet_url", "flows": {key: flow id}} for what exists
    (or was created), plus "missing": the names still to create.
    """
    connections = by_name(api, "connections", f"connectionSpec.id=={HTTP_API}")
    sources = by_name(api, "sourceConnections", f"connectionSpec.id=={HTTP_API}")
    tgts = by_name(api, "targetConnections", f"connectionSpec.id=={DATA_LAKE}")
    flows = by_name(api, "flows", f"flowSpec.id=={STREAM_FLOW}")

    out: dict[str, Any] = {"connection_id": "", "inlet_url": "", "flows": {}, "missing": []}
    conn = connections.get(NAME)
    log(f"  {'exists' if conn else ('will create' if apply else 'missing'):<12} streaming connection '{NAME}'")
    if not conn:
        out["missing"].append(NAME)
    for key in targets:
        flow_name = f"{NAME}: {key}"
        state = "exists" if flow_name in flows else ("will create" if apply else "missing")
        log(f"  {state:<12} dataflow '{flow_name}'")
        if flow_name in flows:
            out["flows"][key] = flows[flow_name]["id"]
        else:
            out["missing"].append(flow_name)
    if conn:
        conn = api.get_json(f"{FLOWSERVICE}/connections/{conn['id']}")["items"][0]
        out.update(connection_id=conn["id"], inlet_url=inlet_url(conn))
    if not apply or not out["missing"]:
        return out

    log("")
    if not conn:
        made = api.call("POST", f"{FLOWSERVICE}/connections", json={
            "name": NAME, "description": "Luma demo events streamed by the feed command.",
            "connectionSpec": spec(HTTP_API),
            "auth": {"specName": "Streaming Connection",
                     "params": {"dataType": "xdm", "authenticationRequired": False}},
        }).json()
        conn = api.get_json(f"{FLOWSERVICE}/connections/{made['id']}")["items"][0]
        out.update(connection_id=conn["id"], inlet_url=inlet_url(conn))
        log(f"  created      streaming connection '{NAME}'")
    for key, (dataset_id, schema_id) in targets.items():
        flow_name = f"{NAME}: {key}"
        if flow_name in flows:
            continue
        src = sources.get(flow_name) or api.call("POST", f"{FLOWSERVICE}/sourceConnections", json={
            "name": flow_name, "baseConnectionId": conn["id"], "connectionSpec": spec(HTTP_API),
        }).json()
        tgt = tgts.get(flow_name) or api.call("POST", f"{FLOWSERVICE}/targetConnections", json={
            "name": flow_name, "connectionSpec": spec(DATA_LAKE),
            "data": {"format": "parquet_xdm", "schema": {"id": schema_id, "version": XDM_FULL}},
            "params": {"dataSetId": dataset_id},
        }).json()
        flow = api.call("POST", f"{FLOWSERVICE}/flows", json={
            "name": flow_name, "flowSpec": spec(STREAM_FLOW),
            "sourceConnectionIds": [src["id"]], "targetConnectionIds": [tgt["id"]],
        }).json()
        out["flows"][key] = flow["id"]
        log(f"  created      dataflow '{flow_name}'")
    out["missing"] = []
    return out
