"""Phase 1: create the Luma structure, request-for-request from Adobe's Postman collections.

The payloads live in specs/ (ported by port_postman.py) with Postman's
{{variable}} placeholders. Each step is looked up by title/code/name first, so
running setup twice creates nothing the second time. Without apply it only reads.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .client import Platform

SPECS_DIR = Path(__file__).parent / "specs"
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
SCHEMA_REGISTRY_KINDS = {"datatype", "class", "fieldgroup", "schema"}
XED_ID = "application/vnd.adobe.xed-id+json"
XDM_FULL = "application/vnd.adobe.xdm+json"

Log = Callable[[str], None]


def load_steps() -> list[dict[str, Any]]:
    return json.loads((SPECS_DIR / "index.json").read_text(encoding="utf-8"))


def spec_text(step: dict[str, Any]) -> str:
    return (SPECS_DIR / step["spec"]).read_text(encoding="utf-8")


def unknown_variables(step: dict[str, Any], variables: dict[str, str]) -> list[str]:
    """Placeholders this step needs that no earlier step has produced yet."""
    return [name for name in PLACEHOLDER.findall(spec_text(step)) if name not in variables]


def render(step: dict[str, Any], variables: dict[str, str], allow_unknown: bool = False) -> Any:
    """Fill {{placeholders}}. With allow_unknown, values not known yet show as <name>."""
    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return variables[name]
        if allow_unknown:
            return f"<{name}>"
        raise KeyError(f"step '{step['name']}' needs {{{{{name}}}}}, which no earlier step produced")

    return json.loads(PLACEHOLDER.sub(fill, spec_text(step)))


def label(step: dict[str, Any], body: dict[str, Any]) -> str:
    if step["kind"] == "descriptor":
        return f"{step['name']} (collection {step['collection']})"
    return body.get("title") or body.get("name") or step["name"]


# --- lookups: return the existing resource, normalised like a create response ---

def find_registry(api: Platform, path: str, title: str) -> dict[str, Any] | None:
    query = quote(f"title=={title}", safe="=")
    found = api.get_json(f"{path}?property={query}", accept=XED_ID).get("results", [])
    return next((r for r in found if r.get("title") == title), None)


def find_namespace(api: Platform, code: str) -> dict[str, Any] | None:
    namespaces = api.get_json("/data/core/idnamespace/identities")
    return next((ns for ns in namespaces if ns.get("code", "").lower() == code.lower()), None)


def find_dataset(api: Platform, name: str) -> dict[str, Any] | None:
    found = api.get_json("/data/foundation/catalog/dataSets",
                         params={"name": name, "properties": "name", "limit": 100})
    match = next((ds_id for ds_id, ds in found.items() if ds.get("name") == name), None)
    return {"dataset_id": match} if match else None


def flatten_descriptors(payload: Any) -> list[dict[str, Any]]:
    """The descriptor list comes back as a list, or grouped by @type in a dict."""
    if isinstance(payload, list):
        return payload
    return [d for value in payload.values() if isinstance(value, list) for d in value]


def same_descriptor(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys = ("@type", "xdm:sourceSchema", "xdm:sourceProperty")
    return all(a.get(k) == b.get(k) for k in keys)


class Descriptors:
    """All tenant descriptors, fetched once per run and extended as we create."""

    def __init__(self, api: Platform):
        self.api = api
        self._items: list[dict[str, Any]] | None = None

    def find(self, body: dict[str, Any]) -> dict[str, Any] | None:
        if self._items is None:
            payload = self.api.get_json("/data/foundation/schemaregistry/tenant/descriptors",
                                        accept=XDM_FULL)
            self._items = flatten_descriptors(payload)
        return next((d for d in self._items if same_descriptor(d, body)), None)

    def add(self, body: dict[str, Any]) -> None:
        if self._items is not None:
            self._items.append(body)


def find_existing(api: Platform, step: dict[str, Any], body: dict[str, Any],
                  descriptors: Descriptors) -> dict[str, Any] | None:
    kind = step["kind"]
    if kind in SCHEMA_REGISTRY_KINDS:
        return find_registry(api, step["path"], body["title"])
    if kind == "namespace":
        return find_namespace(api, body["code"])
    if kind == "dataset":
        return find_dataset(api, body["name"])
    return descriptors.find(body)


def create(api: Platform, step: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    resp = api.call(step["method"], step["path"], json=body)
    data = resp.json() if resp.content else {}
    if step["kind"] == "dataset":
        # Catalog answers ["@/dataSets/<id>"]
        return {"dataset_id": data[0].split("/")[2]}
    return data


def run_setup(api: Platform, variables: dict[str, str], *, apply: bool,
              verbose: bool = False, log: Log = print) -> tuple[dict[str, str], dict[str, int]]:
    """Look every step up (read-only), print the plan, and with apply=True create what is missing.

    Returns the variables (ids) found or created, and the counts per status.
    """
    descriptors = Descriptors(api)
    steps = load_steps()
    to_create = []
    for step in steps:
        body = render(step, variables, allow_unknown=True)
        # A descriptor on a schema that doesn't exist yet can't exist either.
        if step["kind"] == "descriptor" and unknown_variables(step, variables):
            existing = None
        else:
            existing = find_existing(api, step, body, descriptors)
        if existing:
            for var, source in step["save"].items():
                variables[var] = existing[source]
            log(f"  exists        {step['kind']:<10} {label(step, body)}")
            continue
        to_create.append(step)
        log(f"  {'will create' if apply else 'missing':<13} {step['kind']:<10} {label(step, body)}")
        if verbose:
            log(f"      {step['method']} {step['path']}  {json.dumps(body, separators=(',', ':'))}")
    counts = {"exists": len(steps) - len(to_create), "created": 0, "missing": len(to_create)}
    if not apply or not to_create:
        return variables, counts

    log("")
    for step in to_create:
        body = render(step, variables)
        data = create(api, step, body)
        if step["kind"] == "descriptor":
            descriptors.add(body)
        for var, source in step["save"].items():
            variables[var] = data[source]
        counts["created"] += 1
        counts["missing"] -= 1
        log(f"  created       {step['kind']:<10} {label(step, body)}")
    return variables, counts
