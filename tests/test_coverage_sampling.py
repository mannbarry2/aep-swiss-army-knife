"""Offline tests for DD v3.4.19 coverage sampling: newest 20, then the biggest of
the last 90 days; Profile-only and empty-file verdicts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
import data_dictionary_v3 as dd  # noqa: E402

PROF = {"status": "success", "metrics": {"recordsRead": 9, "recordsWritten": 9}}
LAKE0 = {"status": "success", "metrics": {"outputRecordCount": 0, "inputFileCount": 1}}
LAKE7 = {"status": "success", "metrics": {"outputRecordCount": 7, "inputFileCount": 1}}
results, calls = [], []


def check(name, ok, got=""):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL"), name, got)


def run(newest, biggest, rows_for):
    calls.clear()

    def fake_list(url, headers, timeout, attempts=3, pause=8, label=""):
        calls.append("biggest" if "outputRecordCount" in url else "newest")
        return dict(biggest if "outputRecordCount" in url else newest)

    def fake_dl(headers, bid, need, smallest_first=False, file_timeout=75, **k):
        return [{"a": 1}] * min(need, rows_for(bid)), 100

    dd._get_json_retry, dd._download_batch_rows = fake_list, fake_dl
    dd.aep_headers = lambda *a, **k: {}
    return dd.sample_schema_rows("tok", {}, "roi-prod", ["ds1"], 100)


newest = {f"p{i}": PROF for i in range(15)} | {f"z{i}": LAKE0 for i in range(5)}
rows, st = run(newest, {"big1": LAKE7, "p0": PROF}, lambda b: 7 if b == "big1" else 0)
check("newest 20 all Profile-side/empty -> the biggest of 90 days are sampled",
      len(rows) == 7 and calls == ["newest", "biggest"] and st["available"] == 1, f"{len(rows)} {calls} {st}")

rows, st = run(newest, {"p0": PROF, "p1": PROF}, lambda b: 0)
check("nothing readable anywhere, Profile-side seen -> profile_side counted, available 0",
      not rows and st["available"] == 0 and st["profile_side"] == 17, st)

rows, st = run({f"e{i}": LAKE7 for i in range(3)}, {}, lambda b: 0)
check("batches report records but files empty -> empty_reads, no failures",
      not rows and st["available"] == 3 and st["empty_reads"] == 3 and not st["failed"], st)

rows, st = run({f"g{i}": LAKE7 for i in range(20)}, {"x": LAKE7}, lambda b: 7)
check("rows from the newest -> no extra Catalog call", len(rows) == 84
      and calls == ["newest"], f"{len(rows)} {calls}")

for reason, code in (("profile_only", "profile_only"), ("lake_empty", "lake_empty"), ("empty", "empty")):
    c, label = dd._coverage_status({"dd_empty_reason": reason})
    check(f"coverage status {reason} -> {c}: {label[:60]}", c == code and "Genuinely" not in label)
print(f"\n{sum(results)}/{len(results)} passed")
