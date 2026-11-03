"""Phase 4: demo audiences on the loaded Luma data.

Every rule below was checked against Adobe's Luma files (and validated by the
preview endpoint): each one has members once the data is in Profile. The loaded
data spans about 30 days and most people have one event, so there are no
lapsed / repeat-buyer audiences yet.

{T} is the tenant field prefix (_<tenant id>). Looked up by name first, so
running it twice creates nothing the second time. Without apply it only reads.

Rules are written here as PQL text but stored as pql/json (Adobe's conversion
endpoint), the way the UI stores them: the Data Dictionary reads the fields an
audience uses from that tree. An existing audience still stored as text is
converted in place (same rule).

STREAMING audiences are evaluated as each event arrives (continuous
evaluation), so they fill from feed's streamed events within minutes. Adobe
only streams rules of one event inside a window under 24 hours, alone or with
profile attributes; anything wider stays batch.

CHILDREN are audiences of audiences: inSegment("<parent id>") and one more
condition, e.g. "All Buyers > California". They are made after their parent,
whose id goes into the rule.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .client import ApiError, Platform

PREFIX = "Luma: "
DEFINITIONS = "/data/core/ups/segment/definitions"
JOBS = "/data/core/ups/segment/jobs"
CONVERSION = "/data/core/ups/segment/conversion"
PROFILE = "_xdm.context.profile"
# Adobe's 400 when the org refuses on-demand segment jobs.
JOBS_BLOCKED = "Non-scheduled segment jobs are not allowed"
# Adobe's 400 for a streaming audience when no merge policy is Active-On-Edge.
NEEDS_EDGE = "NEBULA-100042"

Log = Callable[[str], None]

# Event building blocks (X is one event).
WEB_BUY = 'X.eventType.equals("commerce.purchases", false)'
POS_BUY = "X.{T}.systemIdentifier.loyaltyId.isNotNull()"   # only offline purchases carry it
ANY_BUY = f"({WEB_BUY} or {POS_BUY})"
VIEW = 'X.eventType.equals("commerce.productViews", false)'
SAVE = 'X.eventType.equals("commerce.saveForLaters", false)'
BROWSE = f"({VIEW} or {SAVE})"
# SKU prefixes in the Luma catalogue: LLM men's, LLW women's, 24- gear.
CATEGORY = {"Gear": "24-", "Men's Apparel": "LLM", "Women's Apparel": "LLW"}


def has(cond: str) -> str:
    return f"(select X from xEvent where {cond})"


def within(days: int) -> str:
    return f"X.timestamp occurs <= {days} days before now"


def within_hours(hours: int) -> str:
    return f"X.timestamp occurs <= {hours} hours before now"


def sku(prefix: str) -> str:
    return f'X.productListItems.exists(SKU.startsWith("{prefix}", false))'


AUDIENCES: list[tuple[str, str, str]] = [
    ("All Buyers", "Any purchase, web or in store.", has(ANY_BUY)),
    ("Web Buyers - Last 30 Days", "A web purchase in the last 30 days.",
     has(f"{WEB_BUY} and {within(30)}")),
    ("In-store Buyers - Last 90 Days", "An in-store (POS) purchase in the last 90 days.",
     has(f"{POS_BUY} and {within(90)}")),
    ("Omnichannel Buyers", "Bought on the web and in store.", f"{has(WEB_BUY)} and {has(POS_BUY)}"),
    ("High-Value In-store Buyers", "An in-store order of $200 or more.",
     has(f"{POS_BUY} and X.commerce.order.priceTotal >= 200")),
    ("Big Basket", "An in-store order with 3 or more of one item.",
     has(f"{POS_BUY} and X.productListItems.exists(quantity >= 3)")),
    ("Browsers, Never Bought", "Viewed a product, never bought anything.",
     f"{has(VIEW)} and not {has(ANY_BUY)}"),
    ("Browsed Last 7 Days, No Purchase", "Viewed a product in the last 7 days, no purchase since.",
     f"{has(f'{VIEW} and {within(7)}')} and not {has(f'{ANY_BUY} and {within(7)}')}"),
    ("Save-for-Later, No Purchase 14 Days", "Saved a product for later, no purchase in 14 days.",
     f"{has(SAVE)} and not {has(f'{ANY_BUY} and {within(14)}')}"),
    *[(f"Interested in {name}, Never Bought It", f"Viewed or saved {name.lower()}, never bought any.",
       f"{has(f'{BROWSE} and {sku(prefix)}')} and not {has(f'{ANY_BUY} and {sku(prefix)}')}")
      for name, prefix in CATEGORY.items()],
    ("New Customers - Last 14 Days", "First purchase in the last 14 days.",
     f"{has(f'{ANY_BUY} and {within(14)}')} and not {has(f'{ANY_BUY} and X.timestamp occurs > 14 days before now')}"),
    ("California Residents", "Home address in California.", 'homeAddress.state.equals("CA", false)'),
    ("California Buyers", "Lives in California and has bought something.",
     f'homeAddress.state.equals("CA", false) and {has(ANY_BUY)}'),
    ("Loyalty Gold or Platinum", "Loyalty tier gold or platinum.", 'loyalty.tier in ["gold", "platinum"]'),
    ("Points-Rich, No Purchase 60 Days", "500,000+ loyalty points, no purchase in 60 days.",
     f"loyalty.points >= 500000 and not {has(f'{ANY_BUY} and {within(60)}')}"),
    ("Gen Z", "Born 1997 or later.", "person.birthYear >= 1997"),
    ("Email-Contactable, Never Purchased", "Has a personal email, never bought anything.",
     f"personalEmail.address.isNotNull() and not {has(ANY_BUY)}"),
]

STREAMING: list[tuple[str, str, str]] = [
    ("Streaming - In-store Order, Last 24 Hours", "An in-store (POS) order in the last 24 hours.",
     has(f"{POS_BUY} and {within_hours(24)}")),
    ("Streaming - Web Purchase, Last 24 Hours", "A web purchase in the last 24 hours.",
     has(f"{WEB_BUY} and {within_hours(24)}")),
    ("Streaming - In-store Order $200+, Last 24 Hours", "An in-store order of $200 or more in the last 24 hours.",
     has(f"{POS_BUY} and X.commerce.order.priceTotal >= 200 and {within_hours(24)}")),
    ("Streaming - Bought Gear, Last 24 Hours", "Bought gear, web or in store, in the last 24 hours.",
     has(f"{ANY_BUY} and {sku(CATEGORY['Gear'])} and {within_hours(24)}")),
    ("Streaming - Viewed a Product, Last 24 Hours", "Viewed a product in the last 24 hours.",
     has(f"{VIEW} and {within_hours(24)}")),
    ("Streaming - Gold or Platinum In-store Order, Last 24 Hours",
     "Loyalty tier gold or platinum, with an in-store order in the last 24 hours.",
     f'loyalty.tier in ["gold", "platinum"] and {has(f"{POS_BUY} and {within_hours(24)}")}'),
]

# Profile conditions a child adds to its parent.
CA = 'homeAddress.state.equals("CA", false)'
GOLD_PLUS = 'loyalty.tier in ["gold", "platinum"]'
CHILD_OF = {
    "All Buyers": [("California", "who live in California", CA),
                   ("Gold or Platinum", "on the gold or platinum tier", GOLD_PLUS),
                   ("Platinum", "on the platinum tier", 'loyalty.tier.equals("platinum", false)'),
                   ("Gen Z", "born 1997 or later", "person.birthYear >= 1997"),
                   ("Email-Contactable", "with a personal email", "personalEmail.address.isNotNull()"),
                   ("Points-Rich", "with 500,000+ loyalty points", "loyalty.points >= 500000")],
    "In-store Buyers - Last 90 Days": [("California", "who live in California", CA),
                                       ("Gold or Platinum", "on the gold or platinum tier", GOLD_PLUS)],
    "Web Buyers - Last 30 Days": [("California", "who live in California", CA),
                                  ("Gold or Platinum", "on the gold or platinum tier", GOLD_PLUS)],
    "Omnichannel Buyers": [("Gold or Platinum", "on the gold or platinum tier", GOLD_PLUS)],
    "High-Value In-store Buyers": [("Gold or Platinum", "on the gold or platinum tier", GOLD_PLUS)],
}
CHILDREN: list[tuple[str, str, str, str]] = [   # (parent, name, description, pql with {PARENT})
    (parent, f"{parent} > {label}", f"{PREFIX}{parent}, {who}.", f'inSegment("{{PARENT}}") and {cond}')
    for parent, kids in CHILD_OF.items() for label, who, cond in kids]


def definitions(tenant_prefix: str) -> list[dict[str, Any]]:
    """Parents first, then children, then streaming (refused without an edge merge policy)."""
    out = [{"name": PREFIX + name, "description": desc, "pql": pql.replace("{T}", tenant_prefix),
            "streaming": False} for name, desc, pql in AUDIENCES]
    out += [{"name": PREFIX + name, "description": desc, "pql": pql, "streaming": False,
             "parent": PREFIX + parent} for parent, name, desc, pql in CHILDREN]
    out += [{"name": PREFIX + name, "description": desc, "pql": pql.replace("{T}", tenant_prefix),
             "streaming": True} for name, desc, pql in STREAMING]
    return out


def rule(audience: dict[str, Any], ids: dict[str, str]) -> str:
    """The PQL, with a child's parent id filled in."""
    if not audience.get("parent"):
        return audience["pql"]
    return audience["pql"].replace("{PARENT}", ids[audience["parent"]])


def profile_merge_policy(api: Platform) -> str:
    policies = api.get_json("/data/core/ups/config/mergePolicies", params={"limit": 100}).get("children", [])
    profile = [p for p in policies if (p.get("schema") or {}).get("name") == PROFILE]
    chosen = next((p for p in profile if p.get("default")), profile[0] if profile else None)
    if not chosen:
        raise RuntimeError("no Profile merge policy in this sandbox")
    return chosen["id"]


def existing_by_name(api: Platform) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    start = 0
    while True:
        page = api.get_json(DEFINITIONS, params={"limit": 100, "start": start})
        segments = page.get("segments", [])
        found.update({s["name"]: s for s in segments})
        if len(segments) < 100:
            return found
        start += 100


def to_json_expression(api: Platform, pql: str) -> dict[str, str]:
    """PQL text -> Adobe's pql/json expression."""
    converted = api.call("POST", CONVERSION, json={
        "type": "PQL", "targetFormat": "pql/json", "schema": {"name": PROFILE},
        "expression": {"type": "PQL", "format": "pql/text", "value": pql}}).json()["expression"]
    return {"type": "PQL", "format": "pql/json", "value": converted["value"]}


def is_text(segment: dict[str, Any]) -> bool:
    return (segment.get("expression") or {}).get("format") == "pql/text"


def create_body(audience: dict[str, Any], merge_policy_id: str, expression: dict[str, str]) -> dict[str, Any]:
    streaming = bool(audience.get("streaming"))
    return {
        "name": audience["name"],
        "description": audience["description"],
        "schema": {"name": PROFILE},
        "expression": expression,
        "mergePolicyId": merge_policy_id,
        "evaluationInfo": {"batch": {"enabled": not streaming}, "continuous": {"enabled": streaming},
                           "synchronous": {"enabled": False}},
    }


def run_audiences(api: Platform, tenant_prefix: str, *, apply: bool,
                  log: Log = print) -> tuple[dict[str, str], dict[str, int]]:
    """Look every audience up by name; with apply=True create the missing ones and
    convert any still stored as PQL text.

    Returns {name: segment id} for those that exist (or were created), and counts.
    """
    wanted = definitions(tenant_prefix)
    have = existing_by_name(api)
    ids = {a["name"]: have[a["name"]]["id"] for a in wanted if a["name"] in have}
    missing = [a for a in wanted if a["name"] not in have]
    as_text = [a for a in wanted if a["name"] in have and is_text(have[a["name"]])]
    for a in wanted:
        if a["name"] not in have:
            status = "will create" if apply else "missing"
        elif is_text(have[a["name"]]):
            status = "will convert" if apply else "stored as text"
        else:
            status = "exists"
        log(f"  {status:<14} {a['name']}")
    counts = {"exists": len(ids), "created": 0, "missing": len(missing), "converted": 0, "as_text": len(as_text)}
    if not apply or not (missing or as_text):
        return ids, counts
    log("")
    for a in as_text:
        patch = [{"op": "add", "path": "/expression", "value": to_json_expression(api, rule(a, ids))}]
        api.call("PATCH", f"{DEFINITIONS}/{ids[a['name']]}", json=patch, ok=(200, 204))
        counts["converted"] += 1
        counts["as_text"] -= 1
        log(f"  converted      {a['name']}")
    if not missing:
        return ids, counts
    merge_policy_id = profile_merge_policy(api)
    needs_edge = 0
    for a in missing:
        if a.get("parent") and a["parent"] not in ids:
            log(f"  skipped        {a['name']} (parent not there)")
            continue
        body = create_body(a, merge_policy_id, to_json_expression(api, rule(a, ids)))
        try:
            created = api.call("POST", DEFINITIONS, json=body).json()
        except ApiError as err:
            if not (a["streaming"] and NEEDS_EDGE in str(err)):
                raise
            needs_edge += 1
            log(f"  refused        {a['name']} (needs an Active-On-Edge merge policy)")
            continue
        ids[a["name"]] = created["id"]
        counts["created"] += 1
        counts["missing"] -= 1
        log(f"  created        {a['name']}")
    counts["needs_edge"] = needs_edge
    return ids, counts


def evaluate(api: Platform, segment_ids: list[str], *, interval: float = 30, timeout: float = 3600,
             log: Log = print, sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """One batch segment job for these audiences; waits for it and returns {segment id: members}."""
    job = api.call("POST", JOBS, json=[{"segmentId": sid} for sid in segment_ids]).json()
    log(f"  segment job {job['id']} started; polls every {interval:.0f}s ...")
    waited = 0.0
    while True:
        info = api.get_json(f"{JOBS}/{job['id']}")
        status = info.get("status")
        if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if waited >= timeout:
            raise RuntimeError(f"segment job {job['id']} still {status} after {timeout / 60:.0f} min")
        sleep(interval)
        waited += interval
    if status != "SUCCEEDED":
        raise RuntimeError(f"segment job {job['id']} {status}: {info.get('errors') or ''}")
    metrics = info.get("metrics") or {}
    by_status = metrics.get("segmentedProfileByStatusCounter") or {}
    if by_status:
        return {sid: int((by_status.get(sid) or {}).get("realized", 0)) for sid in segment_ids}
    counter = metrics.get("segmentedProfileCounter") or {}
    return {sid: int(counter.get(sid, 0)) for sid in segment_ids}
