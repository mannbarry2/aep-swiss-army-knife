"""Offline tests for the access audit's group descriptions: paging by repeat,
blank descriptions, and a failed read staying visible. No Adobe calls."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from platform_access_and_audit import external_access_entitlement_and_usage_audit as audit  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name, "" if ok else detail)


CONF = {"org_id": "ORG", "client_id": "CLIENT"}
ROWS = [{"id": "1", "name": "with text", "description": "  Data engineers "},
        {"id": "2", "name": "no key"},
        {"id": "3", "name": "empty", "description": ""}]
calls = []


def repeats(url, headers=None, timeout=None):
    calls.append(url)
    return json.dumps(ROWS)


audit.ul.http = repeats
got = audit.fetch_group_descriptions("TOKEN", CONF)
check("trims a description", got["with text"] == "Data engineers", got)
check("missing key is blank", got["no key"] == "", got)
check("empty string is blank", got["empty"] == "", got)
check("stops when a page repeats", len(calls) == 2, calls)
check("reads the route that has descriptions", "/ORG/user-groups?page=0" in calls[0], calls)

pages = [ROWS[:2], ROWS[2:], []]
audit.ul.http = lambda url, headers=None, timeout=None: json.dumps(pages.pop(0))
check("reads every page", len(audit.fetch_group_descriptions("TOKEN", CONF)) == 3)


def refused(url, headers=None, timeout=None):
    raise RuntimeError("refused")


audit.ul.http = refused
check("a failed read is None, not all blank", audit.fetch_group_descriptions("TOKEN", CONF) is None)

DESCS = {"Data": "Data engineers", "Blank": ""}
check("a group's own text wins", audit.group_description({"groupName": "Data"}, DESCS) == "Data engineers")
check("an admin group borrows its parent's", audit.group_description(
    {"groupName": "_admin_Data", "userGroupName": "Data"}, DESCS) == "Admins of: Data engineers")
check("no text anywhere stays blank", audit.group_description(
    {"groupName": "_admin_Blank", "userGroupName": "Blank"}, DESCS) == "")
check("a product profile stays blank", audit.group_description({"groupName": "Profile"}, DESCS) == "")

print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
