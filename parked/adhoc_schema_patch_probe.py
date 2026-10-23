"""
Probe: add a field to an AD HOC schema (the ones Query Service CTAS creates).

Verified against the dev sandbox on 2026-09-21 (schema for dataset
at_store_test3, field 'banana'):

  * The ad hoc SCHEMA has no properties of its own -- it is just
    allOf: [{"$ref": <generated class>}]. PATCHing
    /properties/<ns>/properties/<field> on the schema returns
    400 XDM-1600 'Missing field "properties"'.
  * The fields live on the generated CLASS (meta:class), under
    /allOf/1/properties/<ns>/properties/, where <ns> is the class's own hash
    (e.g. _6b631d56...), NOT the org tenant id. The class is meta:extensible,
    so PATCH it there -> 200, and the resolved schema picks the field up.
  * Content-Type must be application/json (application/json-patch+json -> 415).

So the script resolves dataset -> schema -> class -> namespace, then PATCHes
the class. Interpret the result:

    200      -> works; swap the field name / type for the real one.
    400/403  -> not supported on this object; fall back to route 1.
    404      -> object not under /tenant; the script retries under /global.

Credentials: AEP_TOKEN / AEP_API_KEY / AEP_ORG_ID environment variables if
set, else the 'aep-prod' entry in the OS keyring via aep_creds (a token is
minted from the stored client credentials). Sandbox defaults to DEV; pass
--sandbox=prod deliberately if you really mean prod.

Usage:
    python adhoc_schema_patch_probe.py <datasetId> [fieldName] [--sandbox=dev]
"""

import json
import os
import sys
import urllib.parse

import requests

BASE = "https://platform.adobe.io/data/foundation"
DEFAULT_SANDBOX = "dev"
DEFAULT_FIELD = "banana"                              # harmless probe field
XED = "application/vnd.adobe.xed+json"                # raw stored object


def headers(sandbox: str) -> dict:
    tok, key, org = (os.environ.get("AEP_TOKEN"), os.environ.get("AEP_API_KEY"),
                     os.environ.get("AEP_ORG_ID"))
    if not (tok and key and org):
        # Fall back to the keyring credential this repo's other tools use.
        import aep_creds
        conf = aep_creds.load_creds("aep-prod")
        r = requests.post(
            conf.get("oauth_url") or "https://ims-na1.adobelogin.com/ims/token/v3",
            data={"grant_type": "client_credentials",
                  "client_id": conf["client_id"],
                  "client_secret": conf["client_secret"],
                  "scope": conf.get("scopes")
                  or "openid,AdobeID,read_organizations,"
                     "additional_info.projectedProductContext,session"},
            timeout=60)
        r.raise_for_status()
        tok = r.json()["access_token"]
        key = conf.get("api_key") or conf["client_id"]
        org = conf["org_id"]
    return {
        "Authorization": f"Bearer {tok}",
        "x-api-key": key,
        "x-gw-ims-org-id": org,
        "x-sandbox-name": sandbox,
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sandbox = next((a.split("=", 1)[1] for a in sys.argv[1:]
                    if a.startswith("--sandbox=")), DEFAULT_SANDBOX)
    if not args:
        print(__doc__)
        sys.exit(2)
    dataset_id, field = args[0], (args[1] if len(args) > 1 else DEFAULT_FIELD)
    H = headers(sandbox)
    print(f"sandbox : {sandbox}")

    # 1. dataset -> schema $id
    r = requests.get(f"{BASE}/catalog/dataSets/{dataset_id}", headers=H, timeout=60)
    if r.status_code != 200:
        print("catalog:", r.status_code, r.text[:300]); sys.exit(1)
    ds = next(iter(r.json().values()))
    schema_id = ds["schemaRef"]["id"]
    print(f"dataset : {ds.get('name')}")
    print(f"schema  : {schema_id}")

    # 2. schema -> its class (ad hoc fields live on the generated class)
    r = requests.get(f"{BASE}/schemaregistry/tenant/schemas/"
                     f"{urllib.parse.quote(schema_id, safe='')}",
                     headers={**H, "Accept": XED}, timeout=60)
    if r.status_code != 200:
        print("schema GET:", r.status_code, r.text[:300]); sys.exit(1)
    raw = r.json()
    class_id = raw.get("meta:class")
    print(f"class   : {class_id}   (schemaType={raw.get('schemaType')})")

    # 3. class -> namespace key + which allOf entry carries the properties
    r = requests.get(f"{BASE}/schemaregistry/tenant/classes/"
                     f"{urllib.parse.quote(class_id, safe='')}",
                     headers={**H, "Accept": XED}, timeout=60)
    if r.status_code != 200:
        print("class GET:", r.status_code, r.text[:300]); sys.exit(1)
    cls = r.json()
    idx, ns = None, None
    for i, entry in enumerate(cls.get("allOf") or []):
        props = entry.get("properties") or {}
        ns = next((k for k in props if k.startswith("_")), None)
        if ns:
            idx = i
            break
    if idx is None:
        print("no tenant namespace found on the class; allOf =",
              json.dumps(cls.get("allOf"))[:400]); sys.exit(1)
    existing = list(cls["allOf"][idx]["properties"][ns].get("properties") or {})
    print(f"ns      : {ns}  (allOf[{idx}])  extensible={cls.get('meta:extensible')}")
    print(f"fields  : {existing}")
    if field in existing:
        print(f"'{field}' already present -- nothing to do"); return

    # 4. PATCH the class: add a string field
    patch = [{"op": "add",
              "path": f"/allOf/{idx}/properties/{ns}/properties/{field}",
              "value": {"type": "string", "title": field}}]
    ph = {**H, "Content-Type": "application/json"}   # json-patch+json -> 415
    for container in ("tenant", "global"):
        url = (f"{BASE}/schemaregistry/{container}/classes/"
               f"{urllib.parse.quote(class_id, safe='')}")
        r = requests.patch(url, headers=ph, data=json.dumps(patch), timeout=60)
        print(f"PATCH /{container}/classes -> {r.status_code} {r.text[:500]}")
        if r.status_code != 404:
            break
        print("  404 under /tenant -- retrying under /global ...")

    # 5. verify through the resolved schema
    r = requests.get(f"{BASE}/schemaregistry/tenant/schemas/"
                     f"{urllib.parse.quote(schema_id, safe='')}",
                     headers={**H, "Accept": "application/vnd.adobe.xed-full+json; version=1"},
                     timeout=60)
    if r.status_code == 200:
        fields = (r.json().get("properties") or {}).get(ns, {}).get("properties") or {}
        print(f"verify  : {field} -> {fields.get(field)}")


if __name__ == "__main__":
    main()
