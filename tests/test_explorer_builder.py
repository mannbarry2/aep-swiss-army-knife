"""Offline tests for the Data Dictionary Explorer builder: it draws what the
workbook holds -- lineage from the Audiences columns and the Sources /
Journeys / Destinations tabs -- and works out nothing itself."""
import json
import sys
from pathlib import Path

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
from data_dictionary import data_dictionary_explorer_html_builder as ex  # noqa: E402

results = []


def check(name, ok, got=""):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL"), name, got)


check("no rule-reading of its own", not hasattr(ex, "pql_field_refs") and "data_dictionary_v3" not in sys.modules)

FIELD = "_acmestores.lcmDataGroup.lcmData[].campaignCode"
wbk = {"meta": {"title": "Data Dictionary v3.4.23 – ROI Prod", "generated": "Generated x", "retention": [], "gaps": []},
       "schemas": [{"Tab": "Acme LCM", "Schema": "Acme LCM Schema ", "Kind": "Profile", "In Profile": "Yes"}],
       "datasets": [{"Schema": "Acme LCM Schema", "Friendly Name (dataset)": "LCM", "Table Name (SQL / system)": "acme_lcm_dataset",
                     "Profile": "Profile-enabled", "Fed by": "Files from Azure Blob: LCM Daily"},
                    {"Schema": "profile__union", "Table Name (SQL / system)": "flexible_audience"}],
       "fields": [{"Field (dot notation)": FIELD, "Schema": "Acme LCM Schema",
                   "Friendly Name": "Campaign Code", "Coverage %": "44", "Month by month, Nov 2024 → Oct 2026": "░█"}],
       "hist": [{"Field (dot notation)": FIELD, "Schema": "Acme LCM Schema", "Sep 2026": None, "Oct 2026": 44}],
       "months": ["Sep 2026", "Oct 2026"], "tabs": {"Acme LCM": {}},
       "audiences": [{"Audience id": "A", "Audience name": "Base", "Fields it reads": f"{FIELD}; _experience.x.y",
                      "Used by (audience ids)": "B", "Where it goes": "In use: 1 live journey"},
                     {"Audience id": "B", "Audience name": "Built on A", "Builds on (audience ids)": "A",
                      "Where it goes": "Nothing found using it"}],
       "Journeys": [{"Journey": "Weekly", "Version": "1.0", "Status": "Deployed", "Live now": "Yes",
                     "Audience ids": "A", "Journey id": "j1"}],
       "Destinations": [{"Destination": "LiveRamp", "Dataflow": "LR", "Live now": "No", "Audience ids": "A, B", "Flow id": "x1"}],
       "Sources": [{"How it comes in": "Files from Azure Blob", "Dataflow": "LCM Daily", "Live now": "Yes",
                    "Into dataset": "acme_lcm_dataset", "Into Profile": "Yes", "Flow id": "r1"}]}
m = ex.build_model(wbk, {}, "wb.xlsx")
a, b = m["audiences"]
check("schema name drops ' Schema'", m["schemas"][0]["name"] == "Acme LCM", m["schemas"][0]["name"])
check("dataset of a kept schema is linked; others are not", [d["s"] for d in m["datasets"]] == [0, -1])
check("fields it reads: from the workbook column", a["f"] == [0] and m["fields"][0]["aud"] == [0] and a["miss"] == ["_experience.x.y"],
      (a["f"], a["miss"]))
check("builds on / used by: from the workbook's id columns", b["dep"] == [0] and a["used"] == [1])
check("where it goes: the workbook's verdict, as written", a["where"] == "In use: 1 live journey" and b["where"] == "Nothing found using it")
check("journeys and destinations joined to audiences by id", a["j"] == [0] and a["x"] == [0] and b["x"] == [0] and not m["destinations"][0]["live"])
check("sources joined to datasets by table name", m["sources"][0]["into"] == [0] and m["datasets"][0]["src"] == [0]
      and m["datasets"][0]["fed"] == "Files from Azure Blob: LCM Daily")
check("month cells follow the Field History header", m["fields"][0]["on"][0]["hist"] == [None, 44])
check("label from the workbook title", m["meta"]["label"] == "ROI Prod", m["meta"]["label"])

old = dict(wbk, Journeys=[], Destinations=[], Sources=[],
           audiences=[{"Audience id": "A", "Audience name": "Base"}])
m_old = ex.build_model(old, {}, "old.xlsx")
check("a workbook from before v3.4.23 still builds, with no lineage", m_old["journeys"] == [] and m_old["audiences"][0]["f"] == [])

# The page: data cannot close its own <script>, and the title is escaped.
m["schemas"][0]["desc"] = "</script><b>x</b>"
page = ex.render_html(m)
block = page.split('id="dd-data">', 1)[1].split("</script>", 1)[0]
check("data block survives a '</script>' in a description", "<\\/script>" in block and json.loads(block.replace("<\\/", "</")))
check("page title names the sandbox", "<title>Data Dictionary Explorer – ROI Prod</title>" in page)
check("author is Barry, not a library", 'content="Barry Mann (barrymann.com)"' in page)
wbk_aa = dict(wbk, schemas=[dict(wbk["schemas"][0], **{"Adobe Analytics": "Yes"})])
check("Adobe Analytics flag read from the Schemas tab", ex.build_model(wbk_aa, {}, "x")["schemas"][0]["aa"]
      and not m["schemas"][0]["aa"])
check("Adobe Analytics hidden by default, with a switch", 'let SHOW_AA = false;' in page and 'id="aa"' in page)
check("field lists show the top values, from the workbook column",
      'label: "Most common"' in page and "topValues(pick(f).top, 3)" in page)

# Reading the Summary sheet: retention lines and the completeness table.
wb = Workbook()
ws = wb.active
for row in [["STRICTLY CONFIDENTIAL"], ["Data Dictionary v3.4.22 – ROI Prod"], ["Generated 2026-10-25 17:00 UTC | by x"],
            ["PROFILE RETENTION – an audience can look back ..."], ["acme_order_event_dataset:  Profile keeps 45 days  (data lake: 2 years)"],
            [], ["DATA COMPLETENESS – coverage is NOT complete"], ["Schema", "Tab", "Sandbox", "Coverage status"],
            ["Acme YCP Event Schema", "Acme YCP Event", "ROI Prod", "empty"], [], ["Produced by Barry Mann (barrymann.com)"]]:
    ws.append(row)
s = ex.read_summary(ws)
check("summary: retention line", s["retention"] == ["acme_order_event_dataset:  Profile keeps 45 days  (data lake: 2 years)"], s["retention"])
check("summary: completeness rows only", s["gaps"] == [{"schema": "Acme YCP Event Schema", "tab": "Acme YCP Event", "status": "empty"}], s["gaps"])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
