"""Phase 3: synthetic events that attach to the profiles loaded in phase 2.

Every identity is sampled from the Luma files, so new events stitch onto
existing profiles: offline purchases carry a real loyaltyId, web events a real
lumaCrmId (and the ECID that CRM id already has in luma-web-events.json), and
every SKU is a real product.
"""
from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .datasets import BY_KEY, SOURCE_TENANT_KEY, Dataset, read_rows
from .transform import format_ts

DEFAULT_MIX = {"web-events": 0.5, "offline-purchases": 0.4, "inventory-events": 0.1}
# A rough shopping funnel: lots of views, few purchases.
WEB_EVENT_TYPES = {"commerce.productViews": 0.5, "commerce.productListAdds": 0.25,
                   "commerce.checkouts": 0.15, "commerce.purchases": 0.1}
STOCK_EVENT_TYPES = ("restock", "outOfStock")  # the enum in the inventory field group


@dataclass
class Pools:
    loyalty_ids: list[str]
    crm_ids: list[str]
    products: list[tuple[str, float]]      # (sku, price)
    ecid_by_crm: dict[str, str]


def load_pools(read: Callable[[Dataset], list[dict[str, Any]]] = read_rows) -> Pools:
    def ids(rows: list[dict[str, Any]], field: str) -> list[str]:
        return [r[SOURCE_TENANT_KEY]["systemIdentifier"][field] for r in rows]

    products = [(p[SOURCE_TENANT_KEY]["product"]["sku"], float(p[SOURCE_TENANT_KEY]["product"]["price"]))
                for p in read(BY_KEY["products"])]
    ecids = {}
    for event in read(BY_KEY["web-events"]):
        idmap = event.get("identityMap", {})
        if idmap.get("lumaCrmId") and idmap.get("ECID"):
            ecids[idmap["lumaCrmId"][0]["id"]] = idmap["ECID"][0]["id"]
    return Pools(loyalty_ids=ids(read(BY_KEY["loyalty"]), "loyaltyId"),
                 crm_ids=ids(read(BY_KEY["crm"]), "crmId"),
                 products=products, ecid_by_crm=ecids)


def split_counts(n: int, mix: dict[str, float]) -> dict[str, int]:
    """Exact counts per dataset summing to n (largest remainder), e.g. 20 -> 10/8/2."""
    total = sum(mix.values())
    if n < 0 or total <= 0:
        raise ValueError("need n >= 0 and at least one positive mix weight")
    raw = {k: n * w / total for k, w in mix.items()}
    counts = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[: n - sum(counts.values())]:
        counts[k] += 1
    return counts


def ecid_for(crm_id: str, pools: Pools) -> str:
    """Reuse the CRM id's known ECID; otherwise derive a stable 38-digit one."""
    if crm_id in pools.ecid_by_crm:
        return pools.ecid_by_crm[crm_id]
    return str(int(hashlib.sha256(crm_id.encode()).hexdigest(), 16))[:38]


def purchase_id(rng: random.Random) -> str:
    d = lambda k: "".join(rng.choice("0123456789") for _ in range(k))  # noqa: E731
    return f"{d(3)}-{d(3)}-{d(3)}-{d(4)}"


def basket(rng: random.Random, pools: Pools, max_items: int) -> list[tuple[str, float, int]]:
    """1..max_items distinct products as (sku, unit price, quantity)."""
    picks = rng.sample(pools.products, rng.randint(1, max_items))
    return [(sku, price, rng.randint(1, 3)) for sku, price in picks]


def web_event(rng: random.Random, pools: Pools, ts: str) -> dict[str, Any]:
    event_type = rng.choices(list(WEB_EVENT_TYPES), weights=list(WEB_EVENT_TYPES.values()))[0]
    crm_id = rng.choice(pools.crm_ids)
    items = basket(rng, pools, 3)
    commerce: dict[str, Any] = {event_type.split(".")[1]: {"value": 1}}
    if event_type == "commerce.purchases":
        commerce["order"] = {"purchaseID": purchase_id(rng),
                             "priceTotal": round(sum(price for _, price, _ in items), 2)}
    return {
        "_id": str(uuid.uuid4()),
        "timestamp": ts,
        "eventType": event_type,
        "commerce": commerce,
        "identityMap": {
            "ECID": [{"id": ecid_for(crm_id, pools), "authenticatedState": "ambiguous", "primary": True}],
            "lumaCrmId": [{"id": crm_id, "authenticatedState": "authenticated", "primary": False}],
        },
        "productListItems": [{"SKU": sku} for sku, _, _ in items],
    }


def offline_purchase(rng: random.Random, pools: Pools, tenant_id: str, ts: str) -> dict[str, Any]:
    items = basket(rng, pools, 4)
    total = sum(price * qty for _, price, qty in items)
    return {
        "_id": str(uuid.uuid4()),
        "timestamp": ts,
        tenant_id: {"systemIdentifier": {"loyaltyId": rng.choice(pools.loyalty_ids)}},
        "commerce": {"order": {"purchaseID": purchase_id(rng), "priceTotal": round(total, 2)}},
        "productListItems": [{"SKU": sku, "quantity": qty} for sku, _, qty in items],
    }


def inventory_event(rng: random.Random, pools: Pools, tenant_id: str, ts: str) -> dict[str, Any]:
    return {
        "_id": str(uuid.uuid4()),
        "timestamp": ts,
        tenant_id: {"inventoryEvent": {"sku": rng.choice(pools.products)[0],
                                       "stockEventType": rng.choice(STOCK_EVENT_TYPES)}},
    }


def generate(n: int, *, pools: Pools, tenant_id: str, now: datetime, window_minutes: float = 60,
             mix: dict[str, float] | None = None,
             rng: random.Random | None = None) -> dict[str, list[dict[str, Any]]]:
    """n events split across the three event datasets, timestamps in the last window."""
    rng = rng or random.Random()
    makers = {
        "web-events": lambda ts: web_event(rng, pools, ts),
        "offline-purchases": lambda ts: offline_purchase(rng, pools, tenant_id, ts),
        "inventory-events": lambda ts: inventory_event(rng, pools, tenant_id, ts),
    }
    out = {}
    for key, count in split_counts(n, mix or DEFAULT_MIX).items():
        stamps = [format_ts(now - timedelta(seconds=rng.uniform(0, window_minutes * 60)))
                  for _ in range(count)]
        out[key] = [makers[key](ts) for ts in sorted(stamps)]
    return out
