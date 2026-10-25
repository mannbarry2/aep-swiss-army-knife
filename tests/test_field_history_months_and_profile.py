"""Offline tests for DD v3.4.16: paging, -INGEST, empty files, Profile records,
and the 'In Profile now' column."""
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
import data_dictionary_v3 as dd  # noqa: E402

results = []


def check(name, ok, got=""):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL"), name, got)


a, b = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)
mid = dd._ms(datetime(2026, 1, 15, tzinfo=timezone.utc))
PROF = {"created": mid, "metrics": {"recordsRead": 10, "recordsWritten": 10}}
LAKE0 = {"created": mid, "metrics": {"outputRecordCount": 0, "inputFileCount": 1}}
LAKE5 = {"created": mid, "metrics": {"outputRecordCount": 5, "inputFileCount": 1}}
JSONQ = {"created": mid, "metrics": {"inputByteSize": 9, "inputFileCount": 1}}
INGEST = {"created": mid, "metrics": {"endTime": 1}, "createdClient": "acp_core_identity_data"}
ING_ID = "01KSX26FW4KF0XCK9QGWN33Z88-E4860C0F-01d31ca4-684c1db8-INGEST"

check("-INGEST batch is not lake-readable", not dd._lake_readable(ING_ID, INGEST))
check("lake batch is lake-readable", dd._lake_readable("01KJKAGMB4KW98SVF49AAJ3VAC", LAKE5))
check("Profile-side is not lake-readable", not dd._lake_readable("69a2ac2a0864fd2b5de88ade", PROF))

pages_seen, reads = [], []


def run(pages, reader):
    """pages: list of [(bid, meta)] per Catalog page; reader: bid -> rows."""
    pages_seen.clear()
    reads.clear()

    def fake_batches(h, d, extra, limits=None):
        if "orderBy=desc:metrics.outputRecordCount" in extra:   # biggest first; no count last
            every = [kv for p in pages for kv in p]
            pages_seen.append("biggest")
            return sorted(every, key=lambda kv: -dd._batch_record_count(kv[1]))[:dd.HISTORY_PAGE]
        start = int(re.search(r"&start=(\d+)", extra).group(1))
        pages_seen.append(start)
        return pages[start // dd.HISTORY_PAGE] if start // dd.HISTORY_PAGE < len(pages) else []

    def fake_read(h, bid, paths):
        reads.append(bid)
        got = reader(bid)
        n, fields = got if isinstance(got, tuple) else (got, {"a.b": got} if got else {})
        return n, fields, ("2026-01-15" if n else None)

    dd._history_batches, dd._history_read = fake_batches, fake_read
    return dd._history_sample({}, "ds", a, b, ["a.b"])


full = lambda kind, n, tag: [(f"{tag}{i:03d}", kind) for i in range(n)]  # noqa: E731
# A busy month: page 1 is all Profile-side / empty, page 2 holds a readable batch.
rec = run([full(PROF, 40, "p") + full(LAKE0, 60, "z"), [("good", LAKE5)] + full(LAKE0, 20, "y")],
          lambda bid: 5)
check("busy month: the biggest-first call finds the readable batch at once",
      rec["state"] == "sampled" and pages_seen == ["biggest", 0] and reads == ["good"],
      f"pages={pages_seen} reads={reads}")
# The semi-smart coupon case: 1,480 batches, 3 readable ones far past page 5.
deep = [full(PROF, 97, f"p{i}_") + full(LAKE0, 3, f"z{i}_") for i in range(14)]
deep.append(full(PROF, 77, "q") + [("r1", LAKE5), ("r2", LAKE5), ("r3", LAKE5)])
rec = run(deep, lambda bid: 5)
check("1,480 batches, 3 readable past page 5 -> found in one call, not 'Profile only'",
      rec["state"] == "sampled" and pages_seen == ["biggest", 0] and reads[0] in ("r1", "r2", "r3"),
      f"pages={pages_seen} reads={reads}")
# The profile dataset's October 2026 case: the biggest batches "have records" but
# empty files; a newer batch the count cannot rank has the rows.
LAKE9 = {"created": mid, "metrics": {"outputRecordCount": 9, "inputFileCount": 1}}
rec = run([full(LAKE9, 4, "big") + [("fresh", JSONQ)]], lambda bid: 5 if bid == "fresh" else 0)
check("biggest batches all empty -> newest-first walk finds the rows",
      rec["state"] == "sampled" and reads[-1] == "fresh" and len(reads) == 5
      and pages_seen == ["biggest", 0] and "why" not in rec, f"pages={pages_seen} reads={reads}")
rec = run([full(LAKE9, 4, "big") + full(LAKE0, 3, "z")], lambda bid: 0)
check("biggest all empty, nothing else -> 'data-lake files empty', not 'none'",
      rec["state"] == "unsure" and rec.get("why") == "data-lake files empty", rec.get("why"))
# Nothing with a record count: fall back to the newest-first walk, and count Profile.
rec = run([full(PROF, 40, "p") + full(LAKE0, 60, "z"), full(PROF, 20, "y")], lambda bid: 5)
check("nothing with records -> newest-first walk, Profile records counted across pages",
      pages_seen == ["biggest", 0, 100] and rec.get("profile_records") == 600
      and rec["why"].startswith("Profile only"), f"pages={pages_seen} {rec.get('profile_records')}")

rec = run([full(PROF, 3, "p") + full(LAKE0, 2, "z")], lambda bid: 5)
check("Profile-only month: labelled, nothing read, records counted",
      rec["state"] == "unsure" and rec["why"].startswith("Profile only") and not reads
      and rec["profile_records"] == 30 and not rec.get("retry"), rec)

rec = run([[(ING_ID, INGEST)] + full(LAKE0, 3, "z")], lambda bid: 5)
check("only identity markers and empty batches: 'none', nothing read",
      rec["state"] == "none" and not reads, rec)

rec = run([full(JSONQ, 6, "j")], lambda bid: 0)
check("empty files: up to 4 tried, labelled, not retried",
      rec["state"] == "unsure" and rec["why"] == "data-lake files empty"
      and len(reads) == dd.HISTORY_TRIES and not rec.get("retry"), f"reads={len(reads)} {rec.get('why')}")

rec = run([[("e1", JSONQ), ("e2", JSONQ), ("ok", LAKE5)]], lambda bid: 5 if bid == "ok" else 0)
check("empty then readable: sampled, label cleared",
      rec["state"] == "sampled" and "why" not in rec and reads[0] == "ok", f"reads={reads}")
rec = run([[("big", LAKE5), ("new", JSONQ)]],
          lambda bid: (5, {"a.b": 5}) if bid == "big" else (4, {"c.d": 4}))
check("biggest + one newer batch: fields counted across both",
      reads == ["big", "new"] and rec["rows"] == 9 and set(rec["fields"]) == {"a.b", "c.d"},
      f"reads={reads} rows={rec['rows']} fields={rec['fields']}")
# The prod profile dataset, June 2025 (v3.4.20): one daily load of 34.5M records
# among ~2,400 small loads whose lake files are empty. The median of the biggest
# 100 is a few hundred -- the daily load must be read first, not last.
DAILY = {"created": mid, "metrics": {"outputRecordCount": 34_508_208, "inputFileCount": 39}}
SMALL = {"created": mid, "metrics": {"outputRecordCount": 350, "inputFileCount": 1}}
rec = run([[("daily", DAILY)] + full(SMALL, 99, "s")], lambda bid: 5 if bid == "daily" else 0)
check("one huge daily load among 99 small empty ones -> the daily load is read first",
      rec["state"] == "sampled" and reads[0] == "daily", f"reads={reads[:5]} state={rec['state']}")

# 'In Profile now'
ev90 = {"table": "returns", "profile": "Profile-enabled", "profile_ttl_days": 90}
ev45 = {"table": "orders", "profile": "Profile-enabled", "profile_ttl_days": 45}
rec_ = {"table": "lookup", "profile": "Profile-enabled", "profile_ttl_days": None}
lake = {"table": "raw", "profile": "", "profile_ttl_days": None}
for name, args, want_days, want_text in [
    ("18 months on a 90-day dataset -> all 90", (18.0, "since Apr 2025", [ev90]), 90, "all 90 days"),
    ("18 months on a 45-day dataset -> only 45", (18.0, "since Apr 2025", [ev45]), 45,
     "Profile keeps only 45 days (orders)"),
    ("1 month on a 90-day dataset -> 30 of 90", (1.0, "since 24 Sep 2026", [ev90]), 30,
     "30 of the 90 days"),
    ("both datasets carry it -> the longer retention", (18.0, "x", [ev45, ev90]), 90, "all 90"),
    ("no Profile expiry -> its unbroken days", (3.0, "since Jul 2026", [rec_]), 91, "no Profile expiry"),
    ("not Profile-enabled", (18.0, "x", [lake]), None, "not in Profile"),
    ("unknown history", (None, "unknown -- the data could not be read", [ev90]), None, "unknown"),
    ("stopped", (0, "stopped -- last seen Aug 2026", [ev90]), 0, "stopped"),
]:
    days, text = dd._profile_now(*args)
    check(f"profile: {name} -> {days}, '{text}'", days == want_days and want_text in text)

# schema_history: Profile goes dataset by dataset (the 2026-10-24 Order Event case).
now = datetime(2026, 10, 24, tzinfo=timezone.utc)
months = dd._history_months(24, now)
labels = [m[0] for m in months]


def recs_for(have):
    return {"first": None, "first_month": None, "months": {
        lab: ({"state": "sampled", "rows": 100, "fields": {"a.b": 90}} if lab in have
              else {"state": "none"}) for lab in labels}}


k = {"fields": [("a.b",)]}
orders = dict(ev45, lake_ttl_days=730)
backup = {"table": "orders_backup", "profile": "Profile-enabled", "profile_ttl_days": 90,
          "lake_ttl_days": 730}
long_run, one_old = recs_for(labels[-18:]), recs_for([labels[-10]])
h = dd.schema_history(k, [long_run, one_old], [orders, backup], months, now)["a.b"]
check("45-day dataset's long run + a 90-day backup's one old month -> 45",
      h["profile_days"] == 45 and "only 45" in h["profile_detail"],
      f"-> {h['profile_days']}, '{h['profile_detail']}'")
recent = recs_for(labels[-4:])
h = dd.schema_history(k, [long_run, recent], [orders, backup], months, now)["a.b"]
check("…but a 90-day dataset with the last 4 months -> 90",
      h["profile_days"] == 90, f"-> {h['profile_days']}, '{h['profile_detail']}'")
check("history months still use every dataset", h["months"] and h["months"] > 16, h["months"])

# The marketplace case: the lake shows the field only in a dataset NOT enabled for
# Profile; the Profile-enabled one loads to Profile only.
lake_only = {"table": "mkt_history", "profile": "", "profile_ttl_days": None, "lake_ttl_days": None}
mkt_event = {"table": "mkt_event", "profile": "Profile-enabled", "profile_ttl_days": 90,
             "lake_ttl_days": 730}
po_rec = {"first": None, "first_month": None, "months": {
    lab: ({"state": "unsure", "why": "Profile only: no data-lake copy to read",
           "profile_records": 1200, "batches": []} if lab in labels[-4:] else {"state": "none"})
    for lab in labels}}
h = dd.schema_history(k, [recs_for(labels[-8:]), po_rec], [lake_only, mkt_event], months, now)["a.b"]
check("lake-only carrier + Profile-only Profile dataset -> unknown, not 'not in Profile'",
      h["profile_days"] is None and "loads to Profile only" in h["profile_detail"]
      and "4,800" in h["profile_detail"] and h["months"] > 6,
      f"-> {h['profile_days']}, '{h['profile_detail']}' (history {h['months']})")
h = dd.schema_history(k, [recs_for(labels[-8:])], [lake_only], months, now)["a.b"]
check("only lake datasets -> not in Profile", h["profile_days"] is None
      and h["profile_detail"].startswith("not in Profile"), h["profile_detail"])
h = dd.schema_history(k, [recs_for(labels[-8:]), recs_for([])], [lake_only, backup], months, now)["a.b"]
check("Profile dataset that never carries the field -> 0", h["profile_days"] == 0,
      f"-> {h['profile_days']}, '{h['profile_detail']}'")
print(f"\n{sum(results)}/{len(results)} passed")
