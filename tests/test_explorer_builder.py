"""Offline tests for the Data Dictionary Explorer builder: which fields an
audience rule reads, the lineage links, and the page's data block."""
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


def look(*names, root=None):
    """fieldLookup chain a.b.c on the profile (or on `root`)."""
    node = root or {"nodeType": "parameterReference", "position": 1}
    for n in names:
        node = {"nodeType": "fieldLookup", "fieldName": n, "object": node}
    return node


def var(v):
    return {"nodeType": "varRef", "varName": v}


def lit(v):
    return {"nodeType": "literal", "literalType": "String", "value": v}


# Profile array through select: the Data Dictionary writes array items as 'list[].field'.
sel = {"nodeType": "select", "variables": [{"nodeType": "varDecl", "varName": "_Lcm1",
       "from": look("_tescostores", "lcmDataGroup", "lcmData"),
       "where": {"nodeType": "fnApply", "fnName": "equals", "params": [look("campaignCode", root=var("_Lcm1")), lit("X")]}}]}
got = ex.pql_field_refs(sel)
check("profile array field via select", got == {("profile", "_tescostores.lcmDataGroup.lcmData[].campaignCode")}, got)

# Event sequence over xEvent: fields relative to the event; its timestamp is left out.
chain = {"nodeType": "chain", "array": look("xEvent"),
         "timestampField": {"nodeType": "lambda", "varNames": ["v0"], "body": look("timestamp", root=var("v0"))},
         "elements": [{"nodeType": "element", "name": "C0", "what": {"nodeType": "lambda", "varNames": ["v0"],
                       "body": {"nodeType": "fnApply", "fnName": "stringCompare", "params": [
                           lit("equals"), look("_experience", "customerJourneyManagement", "messageExecution", "journeyVersionID", root=var("v0")),
                           lit("x")]}}}]}
got = ex.pql_field_refs(chain)
check("event field in a sequence, no timestamp", got == {("event", "_experience.customerJourneyManagement.messageExecution.journeyVersionID")}, got)

# An array inside an event: exists(productListItems, v1 -> v1.SKU = ...)
inner = {"nodeType": "chain", "array": look("xEvent"), "elements": [{"nodeType": "element", "what": {
    "nodeType": "lambda", "varNames": ["v0"], "body": {"nodeType": "fnApply", "fnName": "exists", "params": [
        look("productListItems", root=var("v0")),
        {"nodeType": "lambda", "varNames": ["v1"], "body": {"nodeType": "fnApply", "fnName": "equals", "params": [
            look("SKU", root=var("v1")), lit("123")]}}]}}}]}
got = ex.pql_field_refs(inner)
check("array inside an event", ("event", "productListItems[].SKU") in got, got)

got = ex.pql_field_refs({"nodeType": "fnApply", "fnName": "equals", "params": [look("person", "name", "firstName"), lit("A")]})
check("plain profile field", got == {("profile", "person.name.firstName")}, got)

got = ex.pql_field_refs(look("productListItems", "SKU@{_xdm.context.productLookup}", "name"))
check("lookup join keeps only the joining field", got == {("profile", "productListItems.SKU")}, got)

# segmentMembership[ns][id].status -- building on another audience without inSegment()
mem = look("status", root={"nodeType": "fnApply", "fnName": "get", "params": [
    {"nodeType": "fnApply", "fnName": "get", "params": [look("segmentMembership"), lit("CustomerAudienceUpload")]},
    lit("45531a21-e436-4a1d-b84e-d44fefe5a99a")]})
check("segmentMembership id is a dependency", ex.pql_membership_ids(mem) == ["45531a21-e436-4a1d-b84e-d44fefe5a99a"])
check("lookup on a function result is still walked", ("profile", "segmentMembership") in ex.pql_field_refs(mem))

refs, ids = ex.pql_text_refs('inSegment("aaa") and xEvent.commerce.order.priceTotal > 5 and person.gender = "f"')
check("PQL text: inSegment ids and dotted names",
      ids == ["aaa"] and ("event", "commerce.order.priceTotal") in refs and ("profile", "person.gender") in refs, (refs, ids))

# The model: links both ways, unmatched names, containers not reported as missing.
wbk = {"meta": {"title": "Data Dictionary v3.4.22 – ROI Prod", "generated": "Generated x", "retention": [], "gaps": []},
       "schemas": [{"Tab": "Tesco LCM", "Schema": "Tesco LCM Schema ", "Kind": "Profile", "In Profile": "Yes"}],
       "datasets": [{"Schema": "Tesco LCM Schema", "Friendly Name (dataset)": "LCM", "Table Name (SQL / system)": "tesco_lcm_dataset", "Profile": "Profile"},
                    {"Schema": "profile__union", "Table Name (SQL / system)": "flexible_audience"}],
       "fields": [{"Field (dot notation)": "_tescostores.lcmDataGroup.lcmData[].campaignCode", "Schema": "Tesco LCM Schema",
                   "Friendly Name": "Campaign Code", "Coverage %": "44", "Month by month, Nov 2024 → Oct 2026": "░█"}],
       "hist": [{"Field (dot notation)": "_tescostores.lcmDataGroup.lcmData[].campaignCode", "Schema": "Tesco LCM Schema", "Sep 2026": None, "Oct 2026": 44}],
       "months": ["Sep 2026", "Oct 2026"], "tabs": {"Tesco LCM": {}},
       "audiences": [{"Audience id": "A", "Audience name": "Base"}, {"Audience id": "B", "Audience name": "Built on A"}]}
pql = {"A": {"format": "json", "raw_pql": json.dumps({"nodeType": "fnApply", "fnName": "and", "params": [sel, look("_tescostores", "nope")]})},
       "B": {"format": "pql-text", "raw_pql": 'inSegment("A")'}}
m = ex.build_model(wbk, pql, "wb.xlsx")
a, b = m["audiences"]
check("schema name drops ' Schema'", m["schemas"][0]["name"] == "Tesco LCM", m["schemas"][0]["name"])
check("dataset of a kept schema is linked; others are not", [d["s"] for d in m["datasets"]] == [0, -1])
check("audience -> field and field -> audience", a["f"] == [0] and m["fields"][0]["aud"] == [0])
check("dependency both ways", b["dep"] == [0] and a["used"] == [1])
check("unmatched name reported, container is not", a["miss"] == ["_tescostores.nope"], a["miss"])
check("month cells follow the Field History header", m["fields"][0]["on"][0]["hist"] == [None, 44])
check("label from the workbook title", m["meta"]["label"] == "ROI Prod", m["meta"]["label"])

# The page: data cannot close its own <script>, and the title is escaped.
m["schemas"][0]["desc"] = "</script><b>x</b>"
page = ex.render_html(m)
block = page.split('id="dd-data">', 1)[1].split("</script>", 1)[0]
check("data block survives a '</script>' in a description", "<\\/script>" in block and json.loads(block.replace("<\\/", "</")))
check("page title names the sandbox", "<title>Data Dictionary Explorer – ROI Prod</title>" in page)
check("author is Barry, not a library", 'content="Barry Mann (barrymann.com)"' in page)

# Reading the Summary sheet: retention lines and the completeness table.
wb = Workbook()
ws = wb.active
for row in [["STRICTLY CONFIDENTIAL"], ["Data Dictionary v3.4.22 – ROI Prod"], ["Generated 2026-10-25 17:00 UTC | by x"],
            ["PROFILE RETENTION – an audience can look back ..."], ["tesco_order_event_dataset:  Profile keeps 45 days  (data lake: 2 years)"],
            [], ["DATA COMPLETENESS – coverage is NOT complete"], ["Schema", "Tab", "Sandbox", "Coverage status"],
            ["Tesco YCP Event Schema", "Tesco YCP Event", "ROI Prod", "empty"], [], ["Produced by Barry Mann (barrymann.com)"]]:
    ws.append(row)
s = ex.read_summary(ws)
check("summary: retention line", s["retention"] == ["tesco_order_event_dataset:  Profile keeps 45 days  (data lake: 2 years)"], s["retention"])
check("summary: completeness rows only", s["gaps"] == [{"schema": "Tesco YCP Event Schema", "tab": "Tesco YCP Event", "status": "empty"}], s["gaps"])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
