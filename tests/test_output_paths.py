"""Offline tests for toolkit_internals/output_paths.py: one folder per client,
the old flat layout for unlisted orgs, and the Explorer builder finding a
workbook in a client folder and writing beside it. No Adobe calls."""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from toolkit_internals import output_paths  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name, "" if ok else detail)


tmp = Path(tempfile.mkdtemp())
os.environ["AEP_OUTPUT_DIR"] = str(tmp / "out")
cfg = tmp / "config.json"
cfg.write_text(json.dumps({"output_by_client": {"clients": {
    "client-a": {"org_ids": ["A@AdobeOrg"], "root": str(tmp / "synced")},
    "client-b": {"org_ids": ["B@AdobeOrg"]}}}}), encoding="utf-8")

check("org -> client", output_paths.client_for("B@AdobeOrg", cfg) == "client-b"
      and output_paths.client_for("Z@AdobeOrg", cfg) == "")
check("client with a root writes there",
      output_paths.folder("A@AdobeOrg", "pql", cfg) == tmp / "synced" / "pql")
check("client without a root: <output>/<client>/<kind>",
      output_paths.folder("B@AdobeOrg", "data_dictionary_explorer", cfg)
      == tmp / "out" / "client-b" / "data_dictionary_explorer")
check("unlisted org keeps the flat layout",
      output_paths.folder("Z@AdobeOrg", "data_dictionary", cfg) == tmp / "out" / "data_dictionary"
      and output_paths.folder("Z@AdobeOrg", "archive", cfg) == tmp / "out" / "data_dictionary" / "archive")
check("no config file: flat layout", output_paths.folder("A@AdobeOrg", "pql", tmp / "missing.json")
      == tmp / "out" / "data_dictionary" / "pql")
try:
    output_paths.folder("A@AdobeOrg", "nope", cfg)
    check("unknown kind refused", False)
except ValueError:
    check("unknown kind refused", True)
check("all_folders: every client, then the flat one",
      [n for n, _ in output_paths.all_folders("data_dictionary", cfg)] == ["client-a", "client-b", ""])

# The Explorer builder: a workbook in a client folder -> page in data_dictionary_explorer,
# the older page for the same sandbox -> archive.
from data_dictionary import data_dictionary_explorer_html_builder as ex  # noqa: E402
client = tmp / "synced"
for kind in ("data_dictionary", "pql", "data_dictionary_explorer"):
    (client / kind).mkdir(parents=True)
wb_path = client / "data_dictionary" / "Data Dictionary - dev - 2026-11-03.xlsx"
wb_path.write_bytes(b"")
old = client / "data_dictionary_explorer" / "Data Dictionary Explorer - dev - 2026-11-01.html"
old.write_text("old", encoding="utf-8")
seen = {}
ex.read_workbook = lambda path: {}
ex.read_pql = lambda path: seen.setdefault("pql", path) and {}
ex.build_model = lambda *a: {k: [] for k in ("schemas", "datasets", "fields", "audiences", "journeys",
                                               "destinations", "sources")}
ex.render_html = lambda model: "<html></html>"
sys.argv = ["x", f"--workbook={wb_path}"]
ex.main()
new = client / "data_dictionary_explorer" / "Data Dictionary Explorer - dev - 2026-11-03.html"
check("Explorer from a client folder goes to its data_dictionary_explorer", new.exists())
check("its PQL sidecar is read from the client's pql folder",
      seen.get("pql") == client / "pql" / "audiences_pql_dev_20261103.jsonl", seen)
check("the older Explorer page moves to the client's archive",
      not old.exists() and (client / "archive" / old.name).exists())

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
