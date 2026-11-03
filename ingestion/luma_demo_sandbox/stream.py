"""Streaming ingestion: one XDM envelope per event, POSTed to the HTTP API inlet."""
from __future__ import annotations

from typing import Any

import requests

from .client import send

CONTENT_TYPE = "application/vnd.adobe.xed-full+json;version=1"


def envelope(event: dict[str, Any], *, schema_id: str, dataset_id: str, org_id: str,
             source: str = "luma-demo-sandbox") -> dict[str, Any]:
    schema_ref = {"id": schema_id, "contentType": CONTENT_TYPE}
    return {
        "header": {"schemaRef": schema_ref, "imsOrgId": org_id, "datasetId": dataset_id,
                   "source": {"name": source}},
        "body": {"xdmMeta": {"schemaRef": schema_ref}, "xdmEntity": event},
    }


def post(session: requests.Session, inlet_url: str, message: dict[str, Any],
         auth_headers: dict[str, str] | None = None) -> requests.Response:
    """Auth headers are only needed when the inlet was created as authenticated."""
    headers = {"Content-Type": "application/json", **(auth_headers or {})}
    return send(session, "POST", inlet_url, headers=headers, json=message,
                params={"syncValidation": "false"})
