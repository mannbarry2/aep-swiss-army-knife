"""Offline checks of the history verdict logic (no API)."""
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root
import data_dictionary_v3 as dd

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
MONTHS = dd._history_months(24, NOW)
LABELS = [m[0] for m in MONTHS]
assert LABELS[0] == "2024-11" and LABELS[-1] == "2026-10", LABELS


def rec(first, spec, ttl=None, first_month=None):
    """spec: {label: None (no batches) | 'U' (unsure) | {path: pct}} ; others none."""
    months = {}
    cutoff = NOW - dd.timedelta(days=ttl) if ttl else None
    for lab, a, b in MONTHS:
        v = spec.get(lab, "N")
        if cutoff and b <= cutoff:          # as dataset_history does
            v = "N"
        if v == "N":
            months[lab] = {"state": "none"}
        elif v == "U":
            months[lab] = {"state": "unsure", "rows": 0, "fields": {}, "batches": ["x"]}
        else:
            months[lab] = {"state": "sampled", "rows": 100, "batches": ["b"],
                           "fields": {p: pct for p, pct in v.items() if pct}}
    return ({"first": first, "first_month": first_month, "months": months},
            {"lake_ttl_days": ttl})


def run(name, first, spec, expect_months, expect_detail, path="f", ttl=None,
        first_month=None):
    r, d = rec(first, spec, ttl, first_month)
    k = {"fields": [(path,)]}
    h = dd.schema_history(k, [r], [d], MONTHS, NOW)[path]
    m = h["months"]
    ok_m = (m is None and expect_months is None) or (
        m is not None and expect_months is not None and abs(m - expect_months) <= 0.6)
    ok_d = expect_detail in h["detail"]
    print(f"{'PASS' if ok_m and ok_d else 'FAIL'}  {name:<34} months={m!s:<5} "
          f"spark={h['spark']}  detail={h['detail']!r}")
    return ok_m and ok_d


every = {lab: {"f": 80} for lab in LABELS}
results = [
    run("slots: since 28 Sep (<1)", "2026-09-28",
        {"2026-09": {"f": 100}, "2026-10": {"f": 100}}, 0.8, "since 28 Sep 2026"),
    run("orders: fake gap in Apr breaks", "2025-04-23",
        {**{lab: {"f": 70} for lab in LABELS if lab >= "2025-04"}, "2026-04": {"f": 0}},
        5.7, "missing in Apr 2026"),
    run("sparse field: one hole = unsure", "2025-04-23",
        {**{lab: {"f": 3} for lab in LABELS if lab >= "2025-04"}, "2026-04": {"f": 0}},
        18.0, "1 month unsure"),
    run("stopped after Aug", "2025-01-10",
        {**{lab: {"f": 50} for lab in LABELS if "2025-01" <= lab <= "2026-08"},
         "2026-09": {"f": 0}, "2026-10": {"f": 0}}, 0, "last seen Aug 2026"),
    run("never seen", "2025-01-10",
        {lab: {"f": 0} for lab in LABELS if lab >= "2025-01"}, 0, "never seen"),
    run("whole window", "2023-01-01", every, 23.7, "whole 24-month window"),
    run("current month no batch yet", "2023-01-01",
        {**every, "2026-10": "N"}, 23.7, "whole 24-month window"),
    run("feed gap (no batches) breaks", "2023-01-01",
        {**every, "2026-03": "N"}, 6.7, "no data at all in Mar 2026"),
    run("TTL 13 months", "2023-01-01", every, 13.0, "past the TTL", ttl=395),
    run("start month known, day not", None,
        {lab: {"f": 60} for lab in LABELS if lab >= "2025-06"}, 16.7, "since Jun 2025",
        first_month="2025-06"),
    run("all unreadable", "2025-01-10",
        {lab: "U" for lab in LABELS if lab >= "2025-01"}, None, "unknown"),
]
print(f"\n{sum(results)}/{len(results)} passed")
