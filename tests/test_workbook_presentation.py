"""Offline tests for DD v3.4.22: the presentation pass over the workbook."""
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
import data_dictionary_v3 as dd  # noqa: E402

results = []


def check(name, ok, got=""):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL"), name, got)


check("plural: 1 dataset / 3 datasets / 2 identities",
      (dd._plural(1, "dataset"), dd._plural(3, "dataset"), dd._plural(2, "identity", "identities"))
      == ("1 dataset", "3 datasets", "2 identities"))
check("wrapped lines: words, not characters", dd._wrapped_lines("aaa bbb ccc", 7) == 2)
check("wrapped lines: a line break counts", dd._wrapped_lines("a\nb", 50) == 2)
check("dashes and arrows in prose", dd._tidy_text("since May -- gap; a -> b") == "since May – gap; a → b")
sql = "/* Data Dictionary: x -- y */ SELECT 1"
check("SQL left exactly as written", dd._tidy_text(sql) == sql)
check("partial rule: markers gone",
      dd._pql_for_people("[PARTIAL: select] a = 1 and <select: see raw>") == "a = 1 and […]")
check("rule that is nothing but a hidden part says so",
      dd._pql_for_people("[PARTIAL: select] <select: see raw>").startswith("(too complex"))
check("placeholder description shows blank",
      dd._plain_text("Description for furnished object") == ""
      and dd._plain_text("Real words") == "Real words")
check("custom class by name, not hash",
      dd.class_name("https://ns.adobe.com/t/classes/28f877a4", {"https://ns.adobe.com/t/classes/28f877a4": "Store Lookup"})
      == "Store Lookup")

wb = Workbook()
ws = wb.active
ws.title = "Field Index"
wb.create_sheet("Orders")
ws["A2"] = "Field Index – roi-prod"
ws["A3"] = "A long note. " * 20
ws.append([])
heads = ["Field (dot notation)", "Tab", "Data lake rows", "Dataset ID", "Description",
         "Have we got it? (months)", "Spark"]
for c, h in enumerate(heads, 1):
    ws.cell(5, c, h)
ws._dd_table = (5, len(heads))
for i in range(30):
    ws.append([f"_t.group.field{i}", "Orders", 2674505, "6901f9f1b8d2467e8f722e66",
               "word " * 30, 3.0, "░░█"])
    ws.cell(6 + i, 7).font = Font(name="Consolas")
ws.column_dimensions["G"].width = 27
ws.auto_filter.ref = f"A5:G{ws.max_row}"
dd._polish_workbook(wb)
w = {c: ws.column_dimensions[c].width for c in "ABCDEFG"}
check("width fitted to the content", 17 <= w["A"] <= 22, w["A"])
check("a short column is not left wide", w["B"] <= 10, w["B"])
check("a fixed-width (Consolas) column is left alone", w["G"] == 27, w["G"])
check("header row tall enough for its words", ws.row_dimensions[5].height >= 36,
      ws.row_dimensions[5].height)
check("technical column folded away", ws.column_dimensions["D"].hidden
      and ws.column_dimensions["D"].outline_level == 1)
check("Tab name links to its sheet", ws["B6"].hyperlink is not None
      and ws["B6"].hyperlink.location == "'Orders'!A1", ws["B6"].hyperlink)
check("thousands separator", ws["C6"].number_format == "#,##0")
check("prose column wraps, width capped", ws["E6"].alignment.wrap_text and w["E"] <= dd.FIT_MAX_WRAP)
check("other cells in a tall row read from the top", ws["A6"].alignment.vertical == "top")
check("long note merged and wrapped",
      any(str(r) .startswith("A3:") for r in ws.merged_cells.ranges) and ws["A3"].alignment.wrap_text)
check("colour scale on the months", any("F6" in str(r.sqref) for r in ws.conditional_formatting))

# Adobe Analytics report-suite schemas are flagged (kept, for the Explorer to hide).
check("Adobe Analytics: an 'AA:' title", dd.is_adobe_analytics({"title": "AA: AcmeUKGroceries"}, ["a.b"]))
check("Adobe Analytics: mostly _experience.analytics fields",
      dd.is_adobe_analytics({"title": "Web"}, ["_experience.analytics.event1to100.event1.value"] * 3 + ["a"] * 7))
check("Adobe Analytics: an ordinary schema is not", not dd.is_adobe_analytics(
    {"title": "Acme Order Event"}, ["_acmestores.x"] * 50 + ["_experience.analytics.customDimensions.eVars.eVar1"]))

# Mostly short descriptions with a few longer ones: room for a sentence, not a sliver.
wb2 = Workbook()
ws2 = wb2.active
for c, h in enumerate(["Audience name", "Description"], 1):
    ws2.cell(5, c, h)
ws2._dd_table = (5, 2)
for i in range(20):
    ws2.append([f"aud{i}", "Week 20" if i < 18 else "Audience who have opened the email we sent in week 24"])
ws2.auto_filter.ref = f"A5:B{ws2.max_row}"
dd._polish_workbook(wb2)
check("prose column wide enough for a short sentence", ws2.column_dimensions["B"].width >= dd.WRAP_MIN,
      ws2.column_dimensions["B"].width)
print(f"\n{sum(results)}/{len(results)} passed")
