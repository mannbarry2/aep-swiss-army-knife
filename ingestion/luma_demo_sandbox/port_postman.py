"""Port Adobe's Luma Postman collections (1-7) into ingestion/luma_demo_sandbox/specs/.

Every structure request (datatype, class, field group, namespace, schema,
descriptor, dataset) becomes one JSON payload file, verbatim, with Postman's
{{variable}} placeholders left in place. specs/index.json records the order,
the endpoint, and which response field each step saves into which variable.
Batch-ingestion requests are skipped: `load` owns that flow.

Run from the repo root when Adobe's collections change; the output is committed:
    python ingestion/luma_demo_sandbox/port_postman.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
POSTMAN = ROOT / "demo" / "luma" / "_raw" / "platform-utils-main"   # Adobe's download, pristine
SPECS = HERE / "specs"

# Postman test-script captures -> the response field they read.
CAPTURE = re.compile(r'setEnvironmentVariable\("(\w+)",\s*jsonData(\[[^)]*?\])(\.split\("/"\)\[2\])?\)')
SOURCES = {'["$id"]': "$id", '["meta:altId"]': "meta:altId", '["id"]': "id"}

KINDS = {
    "/schemaregistry/tenant/datatypes": "datatype",
    "/schemaregistry/tenant/classes": "class",
    "/schemaregistry/tenant/fieldgroups": "fieldgroup",
    "/schemaregistry/tenant/mixins": "fieldgroup",
    "/schemaregistry/tenant/schemas": "schema",
    "/schemaregistry/tenant/descriptors": "descriptor",
    "/idnamespace/identities": "namespace",
    "/catalog/dataSet": "dataset",
}


def url_path(url: str) -> str:
    return re.sub(r"^(\{\{platform_end_point\}\}|https://platform\.adobe\.io)", "", url)


def captures(item: dict) -> dict[str, str]:
    script = "\n".join(
        line for ev in item.get("event", []) if ev["listen"] == "test"
        for line in ev["script"].get("exec", []) if not line.strip().startswith("//"))
    out = {}
    for var, src, split in CAPTURE.findall(script):
        out[var] = "dataset_id" if split else SOURCES[src]
    return out


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main() -> None:
    SPECS.mkdir(exist_ok=True)
    for old in SPECS.glob("*.json"):
        old.unlink()
    index = []
    for coll in sorted(POSTMAN.glob("[1-7]-*.postman_collection.json")):
        number = coll.name.split("-")[0]
        for item in json.loads(coll.read_text(encoding="utf-8"))["item"]:
            req = item["request"]
            raw = req["url"]["raw"] if isinstance(req["url"], dict) else req["url"]
            path = url_path(raw)
            if "/import/batches" in path:
                continue
            kind = next(k for suffix, k in KINDS.items() if path.endswith(suffix))
            # /mixins is the deprecated alias of /fieldgroups; same payload.
            path = path.replace("/tenant/mixins", "/tenant/fieldgroups")
            # Two bodies carry a trailing // comment, which is not JSON.
            body = json.loads(re.sub(r"\n\s*//.*$", "", req["body"]["raw"].strip()))
            name = f"{len(index) + 1:02d}-{slug(item['name'])}.json"
            (SPECS / name).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
            index.append({"collection": number, "name": item["name"], "kind": kind,
                          "method": req["method"], "path": path, "spec": name,
                          "save": captures(item)})
    (SPECS / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"ported {len(index)} requests into {SPECS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
