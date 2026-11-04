"""Every clock that matters in a sandbox, for the System Times tab.

When the sandbox evaluates its batch audiences, when it writes its snapshots,
how old the estimator's sample is, when the member counts were last refreshed,
and which SQL jobs and dataflows run on a timer. These are the times that
explain "why is this number not there yet".

Adobe stores its schedules as Quartz crons, always UTC, in
/config/schedules. describe_cron() turns those into words. Read-only and never
fatal: what can't be read becomes a row saying so, because a blank here would
read as "nothing is scheduled".

    rows = read(get_json, snapshot_rows, flows)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

UPS = "https://platform.adobe.io/data/core/ups"
SCHEDULES = f"{UPS}/config/schedules?limit=100"
JOBS = f"{UPS}/segment/jobs?limit=50&sort=creationTime:desc"
SAMPLE = f"{UPS}/previewsamplestatus"
AUDIENCES = f"{UPS}/audiences?limit=100"
QUERY_SCHEDULES = "https://platform.adobe.io/data/foundation/query/schedules?limit=100"

COLUMNS = ["Sandbox", "Area", "What", "When (UTC)", "How often", "State", "Last ran (UTC)",
           "What it means", "Adobe schedule"]
DAYS = {"MON": "Mondays", "TUE": "Tuesdays", "WED": "Wednesdays", "THU": "Thursdays", "FRI": "Fridays",
        "SAT": "Saturdays", "SUN": "Sundays", "1": "Sundays", "2": "Mondays", "3": "Tuesdays",
        "4": "Wednesdays", "5": "Thursdays", "6": "Fridays", "7": "Saturdays"}

# Adobe's schedule types, in the order that matters to a reader.
KINDS = {
    "batch_segmentation": ("Audiences", "System evaluation (batch audiences)",
                           "Batch audiences are worked out at this time. Membership changes then."),
    "profile_export": ("Exports", "Profile snapshot export",
                       "Writes the Profile snapshot table: what SQL on Profile and this dictionary's "
                       "Profile coverage read."),
    "segment_export": ("Exports", "Audience definitions snapshot export",
                       "Writes the audience definitions snapshot table."),
    "export": ("Exports", "Snapshot export", "Writes a snapshot table."),
    "batch_cs_projection_job": ("Adobe internal", "Profile projection job",
                                "Adobe's own housekeeping on the Profile store. Nothing to act on."),
    "org_schedule_task_runner_job": ("Adobe internal", "Org task runner",
                                     "Adobe's own scheduler heartbeat. Nothing to act on."),
}
ORDER = list(KINDS)


def _pad(hour: str, minute: str) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def describe_cron(cron: str) -> tuple[str, str]:
    """Quartz (sec min hour dom mon dow [year]) -> (when, how often), UTC.

    Anything it can't put plainly comes back as the raw cron, never a guess.
    """
    parts = (cron or "").split()
    if len(parts) == 5:           # a plain cron has no seconds field
        parts = ["0"] + parts
    if len(parts) < 6:
        return (cron or ""), ""
    _, minute, hour, dom, month, dow = parts[:6]
    if month != "*" or not minute.replace("/", "").isdigit() or "/" in minute and "/" in hour:
        step = re.fullmatch(r"(\d+)/(\d+)", minute)
        if step and hour == "*" and dom in "*?" and month == "*":
            return f"every {step.group(2)} min", "Every few minutes"
        return cron, ""
    step = re.fullmatch(r"(\d+)/(\d+)", minute)
    if step:
        if hour == "*" and dom in "*?":
            return f"every {step.group(2)} min", "Every few minutes"
        return cron, ""
    step = re.fullmatch(r"(\d+)/(\d+)", hour)
    if hour == "*" or (step and step.group(2) == "1"):
        return f"every hour at :{int(minute):02d}", "Hourly"
    if step:
        return f"every {step.group(2)} hours at :{int(minute):02d}", f"Every {step.group(2)} hours"
    if not re.fullmatch(r"\d+(,\d+)*", hour):
        return cron, ""
    when = ", ".join(_pad(h, minute) for h in hour.split(","))
    if dom in "*?" and dow in "*?":
        return when, "Daily" if "," not in hour else f"{hour.count(',') + 1} times a day"
    if dom == "?" and dow in DAYS:
        return when, DAYS[dow]
    if dom.isdigit() and dow in "*?":
        return when, f"Monthly on day {dom}"
    return cron, ""


def utc(value: Any) -> str:
    """Seconds or milliseconds since 1970, or a datetime / date string -> 'YYYY-MM-DD HH:MM'."""
    if value in (None, "", 0):
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, (int, float)) or str(value).isdigit():
        n = float(value)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, timezone.utc).strftime("%Y-%m-%d %H:%M")
    return str(value).replace("T", " ")[:16]


def _kind(schedule: dict[str, Any]) -> str:
    kind = schedule.get("type") or ""
    if kind != "export":
        return kind
    payload = (schedule.get("properties") or {}).get("payload") or {}
    name = str((payload.get("schema") or {}).get("name") or "")
    return {"_xdm.context.profile": "profile_export",
            "_xdm.context.segmentdefinition": "segment_export"}.get(name, "export")


def _row(area: str, what: str, when: str, often: str, state: str, last: str, means: str, raw: str = "") -> dict:
    return {"Area": area, "What": what, "When (UTC)": when, "How often": often, "State": state,
            "Last ran (UTC)": last, "What it means": means, "Adobe schedule": raw}


def _last_export(snapshot_rows: list[dict], kind: str) -> str:
    """Newest write of that snapshot, from the Snapshot Tables read."""
    want = {"profile_export": "profile_snapshot", "segment_export": "segmentdefinition_snapshot"}.get(kind)
    times = [r.get("snapshot_completed") for r in snapshot_rows or []
             if want and r.get("snapshot_completed")
             and want in f"{r.get('snapshot_name')} {r.get('table_name')}".lower().replace("-", "_")]
    return utc(max(times)) if times else ""


def _last_evaluation(get_json: Callable[[str], Any]) -> tuple[str, str]:
    """(last run, note) from the scheduler's segment jobs; orgs on B2B simplification list none."""
    body = get_json(JOBS) or {}
    jobs = [j for j in body.get("children") or [] if j.get("source") == "scheduler"]
    if not jobs:
        return "", " Adobe lists no evaluation jobs for this sandbox, so the last run can't be shown."
    newest = max(jobs, key=lambda j: j.get("creationTime") or 0)
    status = newest.get("status") or ""
    return utc(newest.get("creationTime")), f" Last run: {status}." if status else ""


def schedule_rows(get_json: Callable[[str], Any], snapshot_rows: list[dict]) -> list[dict]:
    body = get_json(SCHEDULES) or {}
    scheds = body if isinstance(body, list) else body.get("children") or []
    scheds = sorted(scheds, key=lambda s: ORDER.index(_kind(s)) if _kind(s) in ORDER else len(ORDER))
    rows = []
    for s in scheds:
        kind, cron = _kind(s), (s.get("schedule") or "").strip()
        area, what, means = KINDS.get(kind, ("Other", str(s.get("type") or "Schedule"), ""))
        when, often = describe_cron(cron)
        last = _last_export(snapshot_rows, kind)
        if kind == "batch_segmentation":
            try:
                last, note = _last_evaluation(get_json)
                means += note
            except Exception as e:
                means += f" Last run UNREADABLE ({type(e).__name__})."
        rows.append(_row(area, what, when, often, s.get("state") or "", last, means, cron))
    return rows


def count_row(get_json: Callable[[str], Any], rows: list[dict]) -> dict | None:
    """When the member counts were last refreshed, and off which job."""
    children = (get_json(AUDIENCES) or {}).get("children") or []
    metrics = [a["metrics"] for a in children if isinstance(a.get("metrics"), dict) and a["metrics"].get("updateEpoch")]
    if not metrics:
        return None
    newest = max(metrics, key=lambda m: m["updateEpoch"])
    means = "The member counts the UI shows are this old."
    when = ""
    if newest.get("type") == "export":
        export = next((r for r in rows if r["What"] == "Profile snapshot export"), None)
        when = f"after the {export['When (UTC)']} export" if export else "after the snapshot export"
        means += (" They come from the snapshot export, not the evaluation, so an audience can have "
                  "members before the UI count moves.")
    return _row("Audiences", "Member counts last refreshed", when, "", "", utc(newest["updateEpoch"]), means)


def sample_row(get_json: Callable[[str], Any]) -> dict:
    body = get_json(SAMPLE) or {}
    rows_in = str(body.get("totalRows") or body.get("numRowsToRead") or "").strip('"')
    return _row("Audiences", "Estimator sample", "", "No published schedule", str(body.get("status") or ""),
                utc(body.get("lastSampledTimestamp")),
                f"The estimates in the audience builder use this sample ({rows_in or 'unknown'} profiles). "
                "An old sample gives estimates of 0 or errors whatever the rule.")


def query_rows(get_json: Callable[[str], Any]) -> list[dict]:
    rows = []
    for q in (get_json(QUERY_SCHEDULES) or {}).get("schedules") or []:
        sched = q.get("schedule") or {}
        cron = str(sched.get("schedule") or "")
        if str(q.get("state") or sched.get("state") or "").upper() in ("DISABLED", "DELETED"):
            continue
        when, often = describe_cron(cron)
        rows.append(_row("Scheduled SQL", str(q.get("name") or q.get("id") or ""), when, often,
                         str(q.get("state") or sched.get("state") or ""), utc(q.get("updated")),
                         "A Data Distiller job on a timer. 'Last ran' is when it was last changed.", cron))
    return rows


def _flow_time(params: dict[str, Any]) -> tuple[str, str]:
    frequency = str(params.get("frequency") or "").lower()
    interval = params.get("interval")
    start = utc(params.get("startTime"))
    at = start[11:] if start else ""
    often = {"once": "Once", "minute": f"Every {interval} min", "hour": f"Every {interval} hours",
             "day": "Daily" if interval in (1, "1", None) else f"Every {interval} days",
             "week": "Weekly" if interval in (1, "1", None) else f"Every {interval} weeks"}.get(
                 frequency, frequency.title())
    return ("" if frequency in ("minute", "hour") else at), often


def scheduled_flows(sweep: dict[str, Any]) -> list[dict]:
    """Switched-on dataflows that run on a timer, from lineage's Flow Service sweep."""
    rows = []
    for f in sweep.get("flows") or []:
        params = f.get("scheduleParams")
        if not params or str(f.get("state") or "").lower() != "enabled":
            continue
        if str(params.get("frequency") or "").lower() == "once":
            continue
        when, often = _flow_time(params)
        spec = (sweep.get("flow_specs") or {}).get((f.get("flowSpec") or {}).get("id"), "")
        last = ((f.get("lastOperation") or {}).get("started") or
                ((f.get("inheritedAttributes") or {}).get("lastRun") or {}).get("started"))
        rows.append(_row("Dataflows", str(f.get("name") or f.get("id") or ""), when, often, "enabled", utc(last),
                         f"Runs on a timer ({spec})." if spec else "Runs on a timer.",
                         str(params.get("frequency") or "")))
    return rows


def read(get_json: Callable[[str], Any], snapshot_rows: list[dict] | None = None,
         flows: list[dict] | None = None) -> list[dict]:
    """All the rows for one sandbox. A read that fails becomes a row saying so."""
    def unreadable(what: str, e: Exception) -> dict:
        return _row("NOT READ", what, "", "", "", "", f"Could not be read ({type(e).__name__}), so this "
                    "says nothing about whether one exists.")

    try:
        rows = schedule_rows(get_json, snapshot_rows or [])
    except Exception as e:
        rows = [unreadable("Schedules (evaluation and exports)", e)]
    for what, fn in (("Member counts", lambda: count_row(get_json, rows)), ("Estimator sample", lambda: sample_row(get_json))):
        try:
            row = fn()
            if row:
                rows.append(row)
        except Exception as e:
            rows.append(unreadable(what, e))
    try:
        rows += query_rows(get_json)
    except Exception as e:
        rows.append(unreadable("Scheduled SQL", e))
    return rows + (flows or [])
