"""Batch Ingestion: create batch -> upload file -> COMPLETE -> poll Catalog."""
from __future__ import annotations

import time
from typing import Any, Callable

from .client import ApiError, Platform

IMPORT = "/data/foundation/import/batches"
DONE_OK = {"success", "active"}
DONE_BAD = {"failed", "failure", "aborted", "abandoned", "inactive"}


def create_batch_body(dataset_id: str) -> dict[str, Any]:
    return {"datasetId": dataset_id, "inputFormat": {"format": "json"}}


def ingest(api: Platform, dataset_id: str, filename: str, payload: bytes) -> str:
    """Start one batch holding one file; returns the batch id. Does not wait."""
    batch_id = api.call("POST", IMPORT, json=create_batch_body(dataset_id)).json()["id"]
    api.call("PUT", f"{IMPORT}/{batch_id}/datasets/{dataset_id}/files/{filename}",
             content_type="application/octet-stream", data=payload, ok=(200, 201))
    api.call("POST", f"{IMPORT}/{batch_id}?action=COMPLETE", ok=(200, 201, 202, 204))
    return batch_id


def batch_info(api: Platform, batch_id: str) -> dict[str, Any]:
    """Catalog answers {<batchId>: {...}}; a fresh batch can 404 for a moment."""
    try:
        payload = api.get_json(f"/data/foundation/catalog/batches/{batch_id}")
    except ApiError as err:
        if err.status == 404:
            return {"status": "pending"}
        raise
    return payload.get(batch_id) or next(iter(payload.values()), {})


def wait_for(api: Platform, batch_id: str, *, interval: float = 15, timeout: float = 1800,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Poll until success / failed. Returns the last Catalog record (status 'timeout' if slow)."""
    deadline = clock() + timeout
    while True:
        info = batch_info(api, batch_id)
        status = str(info.get("status", "")).lower()
        if status in DONE_OK | DONE_BAD:
            return {**info, "status": status}
        if clock() > deadline:
            return {**info, "status": "timeout"}
        sleep(interval)


def failure_reasons(info: dict[str, Any]) -> list[str]:
    reasons = [f"{e.get('code', '?')}: {e.get('description') or e.get('message', '')}".strip()
               for e in info.get("errors", [])]
    failed = (info.get("metrics") or {}).get("failedRecordCount")
    if failed:
        reasons.append(f"{failed} record(s) failed validation")
    return reasons or ["no reason given - check Monitoring > Batches in the UI"]
