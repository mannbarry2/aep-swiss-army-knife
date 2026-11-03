"""Phase 2 transforms: tenant swap, timestamp shift, fresh _ids. Pure functions."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .datasets import SOURCE_TENANT_KEY

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def swap_tenant(value: Any, tenant_id: str) -> Any:
    """Rename every `_yourTenantId` key to the sandbox's tenant id, at any depth."""
    if isinstance(value, dict):
        return {(tenant_id if k == SOURCE_TENANT_KEY else k): swap_tenant(v, tenant_id)
                for k, v in value.items()}
    if isinstance(value, list):
        return [swap_tenant(v, tenant_id) for v in value]
    return value


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


def shift_timestamps(rows: list[dict[str, Any]], now: datetime,
                     window_days: float | None = 30) -> list[dict[str, Any]]:
    """Move all `timestamp`s so the latest one is `now`.

    Spacing is kept exactly when the file already fits inside `window_days`.
    A longer span (the inventory file covers a year) is squeezed linearly into
    the window so every event is recent. `window_days=None` or 0 never squeezes.
    """
    times = [parse_ts(r["timestamp"]) for r in rows]
    latest, earliest = max(times), min(times)
    span = latest - earliest
    window = timedelta(days=window_days) if window_days else None
    scale = window / span if window and span > window else 1.0
    return [{**row, "timestamp": format_ts(now - (latest - t) * scale)}
            for row, t in zip(rows, times)]


def regenerate_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New uuid4 `_id` per event, so a reload is never deduplicated against an old one."""
    return [{**row, "_id": str(uuid.uuid4())} for row in rows]


def prepare(rows: list[dict[str, Any]], *, tenant_id: str, is_event: bool, now: datetime,
            window_days: float | None = 30) -> list[dict[str, Any]]:
    rows = swap_tenant(rows, tenant_id)
    if is_event:
        rows = regenerate_ids(shift_timestamps(rows, now, window_days))
    return rows


def to_json_file(rows: list[dict[str, Any]]) -> bytes:
    """Same layout as Adobe's files: a JSON array, one record per line."""
    return ("[" + ",\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "]").encode()
