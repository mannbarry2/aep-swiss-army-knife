"""Offline tests for DD v3.4.21: the 'Check by hand (SQL)' column -- one
read-only Query Service statement per field, ready to paste."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
import data_dictionary_v3 as dd  # noqa: E402

results = []


def check(name, ok, got=""):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL"), name, got)


for path, dtype, want in [
    ("a.b", "string", "a.b IS NOT NULL"),
    ("identityMap", "map", "size(identityMap) > 0"),
    ("a.l[]", "array<string>", "size(a.l) > 0"),
    ("a.tags", "array<string>", "size(a.tags) > 0"),
    ("a.p[].g", "string", "exists(a.p, x1 -> x1.g IS NOT NULL)"),
    ("a.s[].c[].code", "string", "exists(a.s, x1 -> exists(x1.c, x2 -> x2.code IS NOT NULL))"),
]:
    got = dd._sql_has_value(path, dtype)
    check(f"has a value: {path} ({dtype})", got == want, got)


def k(kind, profile=True, tables=("t_one",)):
    return {"kind": kind, "profile_enabled": profile,
            "tables": [(f"name {t}", t, f"id-{t}") for t in tables]}


ev = dd._sql_check(k("Event", tables=("t_one", "t_two")), "a.p[].g", "string",
                   "profile_snapshot_export_x", "2024-11-01")
check("event: by month, every table, from the window start",
      "date_format(timestamp, 'yyyy-MM')" in ev and ev.count(" UNION ALL ") == 1
      and "FROM t_one WHERE timestamp >= '2024-11-01'" in ev and "FROM t_two" in ev
      and "snapshot" not in ev.split("*/")[1], ev[:160])
pr = dd._sql_check(k("Profile"), "a.b", "string", "profile_snapshot_export_x", "2024-11-01")
check("Profile record: the default snapshot table, not the lake",
      "FROM profile_snapshot_export_x" in pr and "t_one" not in pr, pr[:160])
check("Profile record without a snapshot table: the lake totals",
      "FROM t_one" in dd._sql_check(k("Profile"), "a.b", "string", None, "2024-11-01"))
check("Profile schema not enabled for Profile: the lake totals",
      "FROM t_one" in dd._sql_check(k("Profile", profile=False), "a.b", "string",
                                    "profile_snapshot_export_x", "2024-11-01"))
lk = dd._sql_check(k("Custom"), "a.b", "string", "profile_snapshot_export_x", "2024-11-01")
check("lookup: records in the lake, no date", "FROM t_one" in lk and "timestamp" not in lk, lk[:160])
check("no table: no SQL", dd._sql_check(k("Event", tables=()), "a.b", "string", None,
                                        "2024-11-01") is None)
for name, sql in (("event", ev), ("profile", pr), ("lookup", lk)):
    check(f"{name}: one line, labelled 'Data Dictionary', SELECT only",
          "\n" not in sql and sql.startswith("/* Data Dictionary: ")
          and sql.split("*/", 1)[1].strip().startswith("SELECT ")
          and not re.search(r"\b(INSERT|CREATE|DROP|DELETE|UPDATE|ALTER)\b", sql, re.I))
print(f"\n{sum(results)}/{len(results)} passed")
