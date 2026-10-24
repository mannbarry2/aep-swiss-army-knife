"""Offline tests for v3.4.15: Profile-side batch filter, batch order, month states, cache."""
import io
import sys
from pathlib import Path
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
import data_dictionary_v3 as dd  # noqa: E402

# Shapes as Catalog returned them on roi-prod, 2026-10-24.
PROFILE = {"created": 0, "metrics": {"startTime": 1, "endTime": 2, "recordsRead": 512,
           "recordsWritten": 512}, "tags": {"adobe/batchIngestion/status/targetSummaries": []}}
PROFILE_NO_TAG = {"created": 0, "metrics": {"recordsRead": 5, "recordsWritten": 5,
                  "profileFragments": {}}}
LAKE_JSON = {"created": 0, "metrics": {"inputByteSize": 373714, "inputFileCount": 1},
             "tags": {"acp_workflow": ["x"]}}
LAKE_7 = {"created": 0, "metrics": {"outputRecordCount": 7, "inputFileCount": 1,
          "outputFileCount": 1}}
LAKE_400 = {"created": 0, "metrics": {"outputRecordCount": 400, "inputFileCount": 1}}
EMPTY = {"created": 0, "metrics": {"outputRecordCount": 0, "inputFileCount": 4}}
results = []


def check(name, ok):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name)


check("profile-side by tag", dd._is_profile_side_batch(PROFILE))
check("profile-side by metrics alone", dd._is_profile_side_batch(PROFILE_NO_TAG))
check("lake JSON batch is not", not dd._is_profile_side_batch(LAKE_JSON))
check("lake parquet batch is not", not dd._is_profile_side_batch(LAKE_7))
order = [bid for bid, _ in dd._typical_batches(
    [("p", PROFILE), ("j", LAKE_JSON), ("s", LAKE_7), ("b", LAKE_400), ("e", EMPTY)], set())]
check(f"order: known big, known small, unknown; no profile/empty -> {order}",
      order == ["b", "s", "j"])

a, b = datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 3, 1, tzinfo=timezone.utc)
mid = dd._ms(datetime(2026, 2, 10, tzinfo=timezone.utc))


def batches(*items):
    return [(bid, dict(x, created=mid)) for bid, x in items]


calls = []


def fake_read(headers, bid, paths):
    calls.append(bid)
    if bid == "gone":
        raise urllib.error.HTTPError("https://platform.adobe.io/data/foundation/export/files/x",
                                     404, "nf", {}, io.BytesIO(b""))
    if bid == "slow":
        raise urllib.error.HTTPError("https://platform.adobe.io/data/foundation/export/files/x",
                                     504, "gw", {}, io.BytesIO(b""))
    if bid == "empty":
        return 0, {}, None
    return 100, {"a.b": 60}, "2026-02-10"


dd._history_read = fake_read
for name, cands, want_state, want_why, want_retry, want_calls in [
    ("profile-only month: marked, nothing read", [("p1", PROFILE), ("p2", PROFILE_NO_TAG)],
     "unsure", "Profile only: no data-lake copy to read", None, []),
    ("mixed month reads the lake batch", [("p1", PROFILE), ("ok", LAKE_JSON)],
     "sampled", None, None, ["ok"]),
    ("files gone (404): unsure, not retried", [("gone", LAKE_7)],
     "unsure", "Data Access: file list: HTTP 404", False, ["gone"]),
    ("timeout (504): unsure, retried", [("slow", LAKE_7)],
     "unsure", None, True, ["slow"]),
    ("empty files: unsure 'no rows', not retried", [("empty", LAKE_JSON)],
     "unsure", None, None, ["empty"]),
]:
    calls.clear()
    dd._history_batches = lambda h, d, extra, limits=(10, 3, 1), c=cands: batches(*c)
    rec = dd._history_sample({}, "ds", a, b, ["a.b"])
    ok = (rec["state"] == want_state and calls == want_calls
          and (want_why is None or rec.get("why") == want_why)
          and (want_retry is None or bool(rec.get("retry")) == want_retry))
    check(f"{name} -> state={rec['state']} why={rec.get('why')} retry={rec.get('retry')} "
          f"read={calls}", ok)

months = {"2026-01": {"state": "unsure", "why": "Profile only: no data-lake copy to read",
                      "batches": []},
          "2026-02": {"state": "unsure", "why": "x: HTTP 504", "batches": ["b"], "retry": True},
          "2026-03": {"state": "sampled", "rows": 5, "batches": ["c"]},
          "2026-10": {"state": "sampled", "rows": 5, "batches": ["d"]}}
saved = {}
dd._history_cache_file = lambda sb, ds: type("P", (), {
    "parent": type("Q", (), {"mkdir": lambda self, **k: None})(),
    "with_suffix": lambda self, s: type("T", (), {
        "write_text": lambda self, t, encoding=None: saved.update(t=t),
        "replace": lambda self, p: None})()})()
dd._history_cache_save("roi-prod", "ds", months, "2026-10")
import json  # noqa: E402
kept = sorted(json.loads(saved["t"])["months"])
check(f"cache keeps Profile-only + sampled, not the 504 or current month -> {kept}",
      kept == ["2026-01", "2026-03"])
print(f"\n{sum(results)}/{len(results)} passed")
