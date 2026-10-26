"""Offline tests for data_dictionary/audience_rule_reader.py: which fields and
audiences an audience rule reads (the Audiences tab's 'Fields it reads' and
'Builds on (audience ids)')."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
from data_dictionary import audience_rule_reader as rr  # noqa: E402

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
got = rr.field_refs(sel)
check("profile array field via select", got == {("profile", "_tescostores.lcmDataGroup.lcmData[].campaignCode")}, got)

# Event sequence over xEvent: fields relative to the event; its timestamp is left out.
chain = {"nodeType": "chain", "array": look("xEvent"),
         "timestampField": {"nodeType": "lambda", "varNames": ["v0"], "body": look("timestamp", root=var("v0"))},
         "elements": [{"nodeType": "element", "name": "C0", "what": {"nodeType": "lambda", "varNames": ["v0"],
                       "body": {"nodeType": "fnApply", "fnName": "stringCompare", "params": [
                           lit("equals"), look("_experience", "customerJourneyManagement", "messageExecution", "journeyVersionID", root=var("v0")),
                           lit("x")]}}}]}
got = rr.field_refs(chain)
check("event field in a sequence, no timestamp", got == {("event", "_experience.customerJourneyManagement.messageExecution.journeyVersionID")}, got)

# An array inside an event: exists(productListItems, v1 -> v1.SKU = ...)
inner = {"nodeType": "chain", "array": look("xEvent"), "elements": [{"nodeType": "element", "what": {
    "nodeType": "lambda", "varNames": ["v0"], "body": {"nodeType": "fnApply", "fnName": "exists", "params": [
        look("productListItems", root=var("v0")),
        {"nodeType": "lambda", "varNames": ["v1"], "body": {"nodeType": "fnApply", "fnName": "equals", "params": [
            look("SKU", root=var("v1")), lit("123")]}}]}}}]}
got = rr.field_refs(inner)
check("array inside an event", ("event", "productListItems[].SKU") in got, got)

got = rr.field_refs({"nodeType": "fnApply", "fnName": "equals", "params": [look("person", "name", "firstName"), lit("A")]})
check("plain profile field", got == {("profile", "person.name.firstName")}, got)

got = rr.field_refs(look("productListItems", "SKU@{_xdm.context.productLookup}", "name"))
check("lookup join keeps only the joining field", got == {("profile", "productListItems.SKU")}, got)

# segmentMembership[ns][id].status -- building on another audience without inSegment()
mem = look("status", root={"nodeType": "fnApply", "fnName": "get", "params": [
    {"nodeType": "fnApply", "fnName": "get", "params": [look("segmentMembership"), lit("CustomerAudienceUpload")]},
    lit("45531a21-e436-4a1d-b84e-d44fefe5a99a")]})
check("segmentMembership id is an audience it builds on", rr.membership_ids(mem) == ["45531a21-e436-4a1d-b84e-d44fefe5a99a"])
check("...and not a field it reads", rr.fields_read(mem) == [], rr.fields_read(mem))

refs, ids = rr.text_refs('inSegment("aaa") and xEvent.commerce.order.priceTotal > 5 and person.gender = "f"')
check("PQL text: inSegment ids and dotted names",
      ids == ["aaa"] and ("event", "commerce.order.priceTotal") in refs and ("profile", "person.gender") in refs, (refs, ids))
both = {"nodeType": "fnApply", "fnName": "and", "params": [
    sel, chain, {"nodeType": "fnApply", "fnName": "equals", "params": [look("_tescostores", "tescoIdentities", "userId"), lit("1")]}]}
prof, evt = rr.fields_read_by_root(both)
check("profile and event fields kept apart",
      prof == ["_tescostores.lcmDataGroup.lcmData[].campaignCode", "_tescostores.tescoIdentities.userId"]
      and evt == ["_experience.customerJourneyManagement.messageExecution.journeyVersionID"], (prof, evt))
check("fields_read: sorted dot paths, from a tree or from text",
      rr.fields_read(sel) == ["_tescostores.lcmDataGroup.lcmData[].campaignCode"]
      and rr.fields_read(text="person.gender = 'f'") == ["person.gender"])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
