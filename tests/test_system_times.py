"""Offline tests for data_dictionary/system_times.py: Quartz crons in words, the
rows for a sandbox, and a failed read never reading as 'nothing scheduled'."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_dictionary import system_times as st  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name, "" if ok else detail)


for cron, want in {"0 1 12 * * ?": ("12:01", "Daily"),
                   "0 1 0,12 * * ?": ("00:01, 12:01", "2 times a day"),
                   "0 34 3 * * ?": ("03:34", "Daily"),
                   "0 38 0/1 * * ?": ("every hour at :38", "Hourly"),
                   "58 2/5 * ? * *": ("every 5 min", "Every few minutes"),
                   "0 0 0/6 * * ?": ("every 6 hours at :00", "Every 6 hours"),
                   "0 30 6 ? * MON": ("06:30", "Mondays"),
                   "0 30 6 1 * ?": ("06:30", "Monthly on day 1"),
                   "30 6 * * *": ("06:30", "Daily"),
                   "not a cron": ("not a cron", ""),
                   "": ("", "")}.items():
    check(f"cron {cron!r}", st.describe_cron(cron) == want, st.describe_cron(cron))

SCHEDS = {"children": [
    {"type": "org_schedule_task_runner_job", "schedule": "58 2/5 * ? * *", "state": "active"},
    {"type": "export", "schedule": "0 34 3 * * ?", "state": "active",
     "properties": {"payload": {"schema": {"name": "_xdm.context.profile"}}}},
    {"type": "batch_segmentation", "schedule": "0 1 12 * * ?", "state": "active"},
    {"type": "brand_new_job", "schedule": "0 0 1 * * ?", "state": "active"}]}
AUDS = {"children": [{"metrics": None}, {"metrics": {"type": "export", "updateEpoch": 1793764266}}]}
SAMPLE = {"lastSampledTimestamp": "2026-11-02 19:00:12.0", "totalRows": "2", "status": "TASK_FINISHED"}


def reader(jobs=None, fail=()):
    def get_json(url):
        for bit in fail:
            if bit in url:
                raise RuntimeError("no")
        if "/config/schedules" in url:
            return SCHEDS
        if "/segment/jobs" in url:
            return {"children": jobs or []}
        if "/audiences" in url:
            return AUDS
        if "previewsamplestatus" in url:
            return SAMPLE
        if "/query/schedules" in url:
            return {"schedules": [
                {"name": "nightly", "state": "ENABLED", "schedule": {"schedule": "0 0 2 * * ?"}},
                {"name": "off", "state": "DISABLED", "schedule": {"schedule": "0 0 3 * * ?"}}]}
        raise AssertionError(url)
    return get_json


snap = [{"snapshot_name": "Profile-Snapshot-Export-abc", "table_name": "profile_snapshot_export_abc",
         "snapshot_completed": "2026-11-04T03:49:14Z"}]
rows = st.read(reader(), snap, [])
what = [r["What"] for r in rows]
check("evaluation first, exports next, Adobe internal after, an unknown type still listed",
      what[:4] == ["System evaluation (batch audiences)", "Profile snapshot export", "Org task runner", "brand_new_job"], what)
ev = rows[0]
check("no jobs listed: says so instead of a blank last run",
      ev["Last ran (UTC)"] == "" and "lists no evaluation jobs" in ev["What it means"], ev)
check("export's last run comes from the snapshot table", rows[1]["Last ran (UTC)"] == "2026-11-04 03:49", rows[1])
counts = next(r for r in rows if r["What"] == "Member counts last refreshed")
check("counts: the time, and that they follow the export",
      counts["When (UTC)"] == "after the 03:34 export" and counts["Last ran (UTC)"] == "2026-11-04 03:51", counts)
sample = next(r for r in rows if r["What"] == "Estimator sample")
check("sample: age and size", sample["Last ran (UTC)"] == "2026-11-02 19:00" and "(2 profiles)" in sample["What it means"], sample)
check("scheduled SQL: on ones only", [r["What"] for r in rows if r["Area"] == "Scheduled SQL"] == ["nightly"])
rows = st.read(reader(jobs=[{"source": "manual", "creationTime": 9}, {"source": "scheduler", "creationTime": 1793764266000, "status": "SUCCEEDED"}]))
check("the scheduler's job gives the last run", rows[0]["Last ran (UTC)"] == "2026-11-04 03:51" and "SUCCEEDED" in rows[0]["What it means"], rows[0])

rows = st.read(reader(fail=["/config/schedules", "/query/schedules"]))
check("a failed read is a NOT READ row, not silence",
      [r["What"] for r in rows if r["Area"] == "NOT READ"] == ["Schedules (evaluation and exports)", "Scheduled SQL"], rows)
rows = st.read(reader(fail=["/segment/jobs"]))
check("evaluation still listed when its jobs can't be read", "UNREADABLE" in rows[0]["What it means"] and rows[0]["When (UTC)"] == "12:01")

flows = st.scheduled_flows({"flow_specs": {"s1": "Sftp"}, "flows": [
    {"name": "daily", "state": "enabled", "flowSpec": {"id": "s1"}, "scheduleParams": {"frequency": "day", "interval": 1, "startTime": "1793764266"}},
    {"name": "quarter", "state": "enabled", "scheduleParams": {"frequency": "minute", "interval": 15, "startTime": "1793764266"}},
    {"name": "off", "state": "disabled", "scheduleParams": {"frequency": "day"}},
    {"name": "backfill", "state": "enabled", "scheduleParams": {"frequency": "once"}},
    {"name": "streaming", "state": "enabled"}]})
check("dataflows: on and on a timer only", [(r["What"], r["When (UTC)"], r["How often"]) for r in flows]
      == [("daily", "03:51", "Daily"), ("quarter", "", "Every 15 min")], flows)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
