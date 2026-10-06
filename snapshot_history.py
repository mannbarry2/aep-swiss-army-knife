#!/usr/bin/env python3
"""
snapshot_history.py
===================
Timing history for the daily batch-segmentation -> profile snapshot pipeline,
per sandbox: when the system evaluation ran, when the snapshot landed, how long
each took, and whether the snapshot was ready by the morning. The trend behind
the single "latest run" that snapshot_tables.py reports.

For each sandbox it:

  1. Finds the profile snapshot export dataset on the DEFAULT merge policy (the
     same "Correct table = YES" logic as snapshot_tables.py, narrowed to the
     Profile union so the segment-definition snapshot is left out).
  2. Pages that dataset's Catalog batches inside the window. NOTE: Catalog marks
     only the LATEST snapshot batch 'success'; every earlier one is flipped to
     'inactive' once superseded. Filtering on status=success therefore returns
     one day, so completed runs are taken as success + inactive.
  3. Pages the SUCCEEDED segment jobs inside the window and keeps the scheduled
     ones (source 'scheduler' / carrying a scheduleId), not on-demand runs.
  4. Joins the two by UTC date.
  5. Lists every ON-DEMAND evaluation (source 'api': Flexible Audience
     Evaluation or an API/UI-triggered run) and pairs it with the export batch
     it spawned. On-demand runs export to the sandbox's flexible_audience
     dataset, NOT the profile snapshot; the batch starts within seconds of the
     job ending. Field names verified against live job JSON (--dump-job prints
     one): the trigger is `source`, there is no schedule.scheduleId.

Writes ONE file, ./output/snapshot_history_<credential>.xlsx (Summary, Charts,
Daily, On-demand evals, Blackout calendar, Raw, Chart Data), overwritten
each run. The Charts tab
plots snapshot time to run per day: prod on its own, all production sandboxes,
and all development / PPE sandboxes. Days with no batch or no job are
left blank, not treated as errors.

Read-only: GETs only. All times UTC.

Usage:
    python snapshot_history.py                          # aep-prod, 30 days, all sandboxes
    python snapshot_history.py --days=90
    python snapshot_history.py --sandbox=prod,roi-prod
    python snapshot_history.py --credential=aep-prod --days=14
    python snapshot_history.py --dump-job       # also print one raw job's JSON
"""

from __future__ import annotations

import json
import statistics
import sys
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aep_creds  # keyring-backed credential store (replaces creds/*.json)
# Auth, sandbox discovery, merge-policy / snapshot-dataset lookup and the time
# helpers are shared with the script this one grew out of.
import snapshot_tables as st

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_NAME = "snapshot_history"
SCRIPT_VERSION = "1.3.0"
SCRIPT_DATE = "2026-09-29"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"

DEFAULT_CREDENTIAL = "aep-prod"
DEFAULT_DAYS = 30
PAGE_LIMIT = 100
MAX_PAGES = 50          # safety backstop on both pagers

PROFILE_UNION = "https://ns.adobe.com/xdm/context/profile__union"
# A snapshot batch that ran to completion. Only the newest is 'success'; the
# rest are 'inactive' (superseded by the next day's snapshot).
DONE_BATCH_STATUSES = {"success", "inactive"}

# An on-demand job's export batch starts this soon after the job ends.
EXPORT_MATCH_WINDOW = timedelta(minutes=30)
# The dataset on-demand (flexible audience) evaluations export to.
ON_DEMAND_TABLE = "flexible_audience"
ON_DEMAND_SNAPSHOT_TYPE = "falcon"

# Summary RAG, on the p90 snapshot-completed time of day (minutes after 00:00 UTC).
RAG_GREEN_BEFORE = 7 * 60 + 30      # 07:30
RAG_AMBER_BEFORE = 8 * 60 + 30      # 08:30
# Daily 'ready by' flags.
READY_MARKS = (8 * 60, 9 * 60)      # 08:00, 09:00

CONFIDENTIAL = st.CONFIDENTIAL
_HEADER_BG = "1F4E78"               # the Data Dictionary's header blue

logger = st.logging.getLogger("snapshot_history")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def minutes_into(day: date, dt: datetime | None):
    """Minutes from 00:00 UTC on `day` to `dt`. Past 1440 when the run finished
    the day after it started. None when dt is missing."""
    if not dt:
        return None
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (dt - midnight).total_seconds() / 60


def fmt_hhmm(minutes) -> str:
    """'07:42', or '25:10 (+1d)' for a run that finished the following day."""
    if minutes is None:
        return ""
    m = int(round(minutes))
    out = f"{(m // 60) % 24:02d}:{m % 60:02d}"
    return out + f" (+{m // 1440}d)" if m >= 1440 else out


def fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def secs_between(a: datetime | None, b: datetime | None):
    if not a or not b or b < a:
        return None
    return (b - a).total_seconds()


def p90(values):
    """Nearest-rank 90th percentile."""
    vals = sorted(values)
    if not vals:
        return None
    return vals[max(0, -(-len(vals) * 9 // 10) - 1)]


def pct(values, limit) -> float | None:
    vals = list(values)
    if not vals:
        return None
    return 100.0 * sum(1 for v in vals if v < limit) / len(vals)


def summary_rag(p90_completed) -> str:
    if p90_completed is None:
        return ""
    if p90_completed < RAG_GREEN_BEFORE:
        return "GREEN"
    if p90_completed < RAG_AMBER_BEFORE:
        return "AMBER"
    return "RED"


# ----------------------------------------------------------------------------
# AEP reads
# ----------------------------------------------------------------------------
def find_datasets(headers) -> tuple[dict | None, dict | None]:
    """(profile snapshot on the DEFAULT merge policy, on-demand export dataset).
    Either is None when the sandbox has none."""
    policies = st.fetch_merge_policies(headers)
    every = st.fetch_snapshot_datasets(headers, policies)
    on_demand = next((s for s in every
                      if s["table"] == ON_DEMAND_TABLE
                      or s["snapshot_type"] == ON_DEMAND_SNAPSHOT_TYPE), None)
    hits = [s for s in every
            if s["schema"] == PROFILE_UNION
            and (policies.get(s["merge_policy_id"]) or {}).get("default") is True]
    if not hits:
        return None, on_demand
    if len(hits) > 1:
        logger.warning(f"    {len(hits)} profile snapshots on the default merge "
                       f"policy; using {hits[0]['table'] or hits[0]['name']!r}.")
    snap = hits[0]
    snap["merge_policy_name"] = (policies.get(snap["merge_policy_id"]) or {}).get("name") or ""
    return snap, on_demand


def fetch_batches(headers, dsid: str, cutoff: datetime) -> list[dict]:
    """Every Catalog batch of the dataset created since `cutoff`, newest first."""
    out, start = [], 0
    base = {"dataSet": dsid, "orderBy": "desc:created", "limit": PAGE_LIMIT,
            "createdAfter": int(cutoff.timestamp() * 1000)}
    for _ in range(MAX_PAGES):
        url = f"{st.CATALOG_BATCHES_URL}?{urllib.parse.urlencode({**base, 'start': start})}"
        data = json.loads(st.http(url, headers=headers))
        if not isinstance(data, dict) or not data:
            break
        page = [{**b, "id": bid} for bid, b in data.items() if isinstance(b, dict)]
        # Belt and braces: hold the window client-side too.
        out.extend(b for b in page
                   if (st.to_dt(b.get("created")) or cutoff) >= cutoff)
        if len(data) < PAGE_LIMIT:
            break
        start += PAGE_LIMIT
    return out


def job_is_scheduled(job: dict) -> bool:
    """A scheduler-triggered evaluation, not an on-demand (api / FAE) run."""
    if job.get("source") == "scheduler":
        return True
    for holder in (job.get("schedule"), job.get("properties")):
        if isinstance(holder, dict) and holder.get("scheduleId"):
            return job.get("source") != "api"
    return False


def job_schedule_id(job: dict) -> str:
    for holder in (job.get("schedule"), job.get("properties")):
        if isinstance(holder, dict) and holder.get("scheduleId"):
            return str(holder["scheduleId"])
    return ""


def job_class(job: dict) -> str:
    return "scheduled" if job_is_scheduled(job) else "on-demand"


def job_audiences(job: dict):
    """How many audiences the job evaluated. A scheduled run lists the single
    wildcard '*', so its real count is the size of the per-segment counter."""
    segs = job.get("segments") or []
    if [x.get("segmentId") for x in segs if isinstance(x, dict)] == ["*"]:
        counter = (job.get("metrics") or {}).get("segmentedProfileCounter")
        return len(counter) if isinstance(counter, dict) else "all (*)"
    return len(segs)


def job_times(job: dict) -> tuple[datetime | None, datetime | None]:
    """(started, ended). metrics.totalTime is the evaluation's own clock; the
    job's creation / update times are the fallback."""
    tt = (job.get("metrics") or {}).get("totalTime") or {}
    started = st.to_dt(tt.get("startTimeInMs")) or st.first_dt(
        job, ("startTime", "startedAt", "createEpoch", "creationTime", "created"))
    ended = st.to_dt(tt.get("endTimeInMs")) or st.first_dt(
        job, ("completedTime", "completedAt", "endTime", "updateEpoch",
              "updateTime", "updated"))
    return started, ended


def fetch_jobs(headers, cutoff: datetime) -> list[dict]:
    """EVERY segment job created since `cutoff` (any status, scheduled and
    on-demand), newest first. Stops paging at the first job older than the
    window."""
    out, start = [], None
    for _ in range(MAX_PAGES):
        params = {"limit": PAGE_LIMIT, "sort": "creationTime:desc"}
        if start:
            params["start"] = start
        url = f"{st.SEGMENT_JOBS_URL}?{urllib.parse.urlencode(params)}"
        data = json.loads(st.http(url, headers=headers)) or {}
        page = data.get("children") or data.get("segmentJobs") or []
        fresh = [j for j in page
                 if (st.to_dt(j.get("creationTime")) or cutoff) >= cutoff]
        out.extend(fresh)
        nxt = (data.get("_page") or {}).get("next")
        if not page or len(fresh) < len(page) or len(page) < PAGE_LIMIT or not nxt:
            break
        start = nxt
    return out


# ----------------------------------------------------------------------------
# Local history cache (survives a snapshot dataset changeover)
# ----------------------------------------------------------------------------
CACHE_DIR = OUTPUT_DIR / "snapshot_history_cache"


def load_cache(sandbox: str) -> dict:
    """{YYYY-MM-DD: entry} of every snapshot day seen so far for the sandbox."""
    path = CACHE_DIR / f"{sandbox}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        logger.warning(f"  {sandbox}: history cache unreadable ({e}); starting afresh.")
        return {}


def save_cache(sandbox: str, cached: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{sandbox}.json").write_text(
        json.dumps(dict(sorted(cached.items())), indent=1), encoding="utf-8")


def cache_entry(r: dict) -> dict:
    return {"table": r.get("table", ""), "batch_id": r["id"],
            "started": r["started"].isoformat(),
            "completed": r["completed"].isoformat(),
            "records": r["records"] if isinstance(r["records"], int) else None,
            "source": "api"}


def cache_row(c: dict) -> dict:
    """A cached entry in the shape of a fetched snapshot batch row."""
    a, e = st.to_dt(c["started"]), st.to_dt(c["completed"])
    return {"id": c.get("batch_id") or "", "started": a, "completed": e,
            "seconds": secs_between(a, e), "status": "cached",
            "records": c.get("records") if c.get("records") is not None else "",
            "table": c.get("table", "")}


# ----------------------------------------------------------------------------
# Collect
# ----------------------------------------------------------------------------
def collect_sandbox(token, conf, sb: dict, days: int, cutoff: datetime) -> dict:
    """Everything for one sandbox: the daily rows, the raw rows and the notes.
    Each read fails on its own -- a sandbox with no readable jobs still reports
    its snapshots, with the gap recorded in 'notes'."""
    name = sb["name"]
    headers = st.aep_headers(token, conf, name)
    out = {"sandbox": name, "type": sb.get("type") or "",
           "production": st.is_production(sb), "table": "", "dataset_id": "",
           "merge_policy": "", "daily": [], "raw": [], "notes": [],
           "on_demand": [], "on_demand_table": "", "sample_job": None}

    snap, fae, batches, fae_batches, jobs = None, None, [], [], []
    try:
        snap, fae = find_datasets(headers)
    except Exception as e:
        out["notes"].append(f"snapshot dataset UNREADABLE: {st.describe_error(e)}")
    if snap:
        out.update(table=snap["table"] or snap["name"], dataset_id=snap["id"],
                   merge_policy=snap["merge_policy_name"])
        try:
            batches = fetch_batches(headers, snap["id"], cutoff)
        except Exception as e:
            out["notes"].append(f"snapshot batches UNREADABLE: {st.describe_error(e)}")
    elif not out["notes"]:
        out["notes"].append("no profile snapshot on the default merge policy")
    try:
        jobs = fetch_jobs(headers, cutoff)
    except Exception as e:
        out["notes"].append(f"segment jobs UNREADABLE: {st.describe_error(e)}")
    if fae:
        out["on_demand_table"] = fae["table"] or fae["name"]
        try:
            fae_batches = fetch_batches(headers, fae["id"], cutoff)
        except Exception as e:
            out["notes"].append(f"on-demand export batches UNREADABLE: "
                                f"{st.describe_error(e)}")
    out["sample_job"] = jobs[0] if jobs else None

    # ---- raw: every row as fetched ------------------------------------------
    for b in batches:
        a = st.first_dt(b, ("started", "created"))
        e = st.first_dt(b, ("completed",))
        out["raw"].append({
            "sandbox": name, "kind": "snapshot batch", "id": b["id"],
            "status": b.get("status") or "", "source": "",
            "created": st.to_dt(b.get("created")), "started": a, "completed": e,
            "seconds": secs_between(a, e),
            "records": st._batch_record_count(b), "schedule_id": "",
            "classification": "", "audiences": "", "request_id": "",
            "object": out["table"], "used": b.get("status") in DONE_BATCH_STATUSES})
    exports = []
    for b in fae_batches:
        a = st.first_dt(b, ("started", "created"))
        e = st.first_dt(b, ("completed",))
        row = {
            "sandbox": name, "kind": "on-demand export batch", "id": b["id"],
            "status": b.get("status") or "", "source": "",
            "created": st.to_dt(b.get("created")), "started": a, "completed": e,
            "seconds": secs_between(a, e),
            "records": st._batch_record_count(b), "schedule_id": "",
            "classification": "", "audiences": "", "request_id": "",
            "object": out["on_demand_table"],
            "used": b.get("status") in DONE_BATCH_STATUSES}
        out["raw"].append(row)
        exports.append(row)
    job_rows = []
    for j in jobs:
        a, e = job_times(j)
        row = {
            "sandbox": name, "kind": "segment job", "id": j.get("id") or "",
            "status": j.get("status") or "", "source": j.get("source") or "",
            "created": st.to_dt(j.get("creationTime")), "started": a,
            "completed": e, "seconds": secs_between(a, e), "records": "",
            "schedule_id": job_schedule_id(j), "classification": job_class(j),
            "audiences": job_audiences(j),
            "request_id": str(j.get("requestId") or ""),
            "object": "batch segmentation",
            # Only a scheduled run that SUCCEEDED feeds the Daily join.
            "used": job_is_scheduled(j) and j.get("status") == "SUCCEEDED"}
        out["raw"].append(row)
        job_rows.append(row)

    # ---- on-demand runs, each paired with the export batch it spawned -------
    taken = set()
    for r in sorted((r for r in job_rows if r["classification"] == "on-demand"),
                    key=lambda r: r["started"] or cutoff):
        match = None
        if r["completed"]:
            cands = [x for x in exports
                     if x["id"] not in taken and x["started"]
                     and timedelta(0) <= x["started"] - r["completed"]
                     <= EXPORT_MATCH_WINDOW]
            match = min(cands, key=lambda x: x["started"], default=None)
        if match:
            taken.add(match["id"])
        x_end = match["completed"] if match else None
        when = r["started"] or r["created"]
        out["on_demand"].append({
            "sandbox": name, "production": out["production"],
            "date": when.date() if when else None,
            "job_id": r["id"], "source": r["source"], "status": r["status"],
            "started": r["started"], "ended": r["completed"],
            "seconds": r["seconds"], "audiences": r["audiences"],
            "request_id": r["request_id"],
            "export_id": match["id"] if match else "",
            "export_started": match["started"] if match else None,
            "export_completed": x_end,
            "export_seconds": match["seconds"] if match else None,
            "export_records": match["records"] if match else "",
            "blackout_seconds": secs_between(r["started"], x_end)})
    out["on_demand"].sort(key=lambda r: r["started"] or cutoff, reverse=True)

    # ---- join by UTC date ---------------------------------------------------
    # Several in one day: the earliest scheduled evaluation and the snapshot
    # that finished last are the pipeline's start and end for that date.
    snaps_by, evals_by = {}, {}
    for r in out["raw"]:
        if not r["used"] or not r["started"]:
            continue
        key = r["started"].date()
        if r["kind"] == "on-demand export batch":
            continue
        if r["kind"] == "snapshot batch":
            if not r["completed"]:
                continue
            cur = snaps_by.get(key)
            if cur is None or r["completed"] > cur["completed"]:
                snaps_by[key] = r
        else:
            cur = evals_by.get(key)
            if cur is None or r["started"] < cur["started"]:
                evals_by[key] = r

    # Local history: a day's snapshot, once seen, is remembered under
    # output/snapshot_history_cache/<sandbox>.json. When Adobe swaps the
    # snapshot dataset (prod, Fri 2 Oct 2026, 15:09 UTC) the old dataset and
    # every batch record on it vanish from the API; the cache carries the old
    # table's days across the changeover so the trend keeps its continuity.
    for r in snaps_by.values():
        r.setdefault("table", out["table"])
    cached = load_cache(name)
    for r in snaps_by.values():
        cached[r["started"].date().isoformat()] = cache_entry(r)
    save_cache(name, cached)
    for key, c in cached.items():
        day = date.fromisoformat(key)
        if day not in snaps_by and day >= cutoff.date():
            snaps_by[day] = cache_row(c)
            if c.get("table") and c["table"] != out["table"]:
                out.setdefault("previous_tables", set()).add(c["table"])

    today = datetime.now(timezone.utc).date()
    for n in range(days + 1):
        day = today - timedelta(days=n)
        if day < cutoff.date():
            break
        s, e = snaps_by.get(day), evals_by.get(day)
        done = minutes_into(day, s["completed"]) if s else None
        runs = sorted((r for r in out["on_demand"] if r["date"] == day),
                      key=lambda r: r["started"] or cutoff)
        out["daily"].append({
            "on_demand": runs,
            "sandbox": name, "production": out["production"], "date": day,
            "eval_started": e["started"] if e else None,
            "eval_ended": e["completed"] if e else None,
            "eval_seconds": e["seconds"] if e else None,
            "snap_started": s["started"] if s else None,
            "snap_completed": s["completed"] if s else None,
            "snap_seconds": s["seconds"] if s else None,
            "gap_seconds": (secs_between(e["completed"], s["completed"])
                            if s and e else None),
            "completed_minutes": done,
            "ready": ["" if done is None else ("Y" if done < mark else "N")
                      for mark in READY_MARKS],
            "records": s["records"] if s else "",
            "table": s.get("table", "") if s else "",
            "batch_id": s["id"] if s else "", "job_id": e["id"] if e else ""})

    if out.get("previous_tables"):
        out["notes"].append("history spans a dataset changeover: earlier days "
                            "are from " + ", ".join(sorted(out["previous_tables"]))
                            + " (local cache)")
    n_s, n_e = len(snaps_by), len(evals_by)
    n_od = len(out["on_demand"])
    n_paired = sum(1 for r in out["on_demand"] if r["export_id"])
    logger.info(f"  {name}: {len(batches)} batch(es), {len(jobs)} job(s) fetched "
                f"-> {n_s} snapshot day(s), {n_e} evaluation day(s), "
                f"{n_od} on-demand run(s) ({n_paired} paired with an export)"
                + (f"  [{'; '.join(out['notes'])}]" if out["notes"] else ""))
    return out


def summarise(res: dict) -> dict:
    days = [d for d in res["daily"] if d["completed_minutes"] is not None]
    done = [d["completed_minutes"] for d in days]
    durs = [d["snap_seconds"] for d in days if d["snap_seconds"] is not None]
    gaps = [d["gap_seconds"] for d in res["daily"] if d["gap_seconds"] is not None]
    med = lambda v: statistics.median(v) if v else None
    return {
        "n_days": len(days),
        "n_evals": sum(1 for d in res["daily"] if d["eval_started"]),
        "done_min": min(done) if done else None, "done_med": med(done),
        "done_p90": p90(done), "done_max": max(done) if done else None,
        "dur_med": med(durs), "dur_max": max(durs) if durs else None,
        "gap_med": med(gaps), "gap_max": max(gaps) if gaps else None,
        "pct_0730": pct(done, RAG_GREEN_BEFORE),
        "pct_0830": pct(done, RAG_AMBER_BEFORE),
        "rag": summary_rag(p90(done)),
        "od_runs": len(res["on_demand"]),
        "od_days": len({r["date"] for r in res["on_demand"] if r["date"]}),
        "od_longest": max((r["blackout_seconds"] for r in res["on_demand"]
                           if r["blackout_seconds"] is not None), default=None),
    }


def window(a: datetime | None, b: datetime | None, day: date) -> str:
    """'hh:mm-hh:mm' UTC; an open end shows as '?', an end on a later day as
    '(+1d)'."""
    if not a:
        return ""
    end = fmt_hhmm(minutes_into(day, b)) if b else "?"
    return f"{a:%H:%M}-{end}"


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
DAILY_COLS = ["Sandbox", "Date", "Eval started (UTC)", "Eval ended (UTC)",
              "Eval duration", "Snapshot started (UTC)",
              "Snapshot completed (UTC)", "Snapshot duration",
              "Gap: eval end -> snapshot completed", "ready_by_0800_utc",
              "ready_by_0900_utc", "Snapshot duration RAG", "Records",
              "Snapshot table", "Snapshot batch id", "Segment job id"]


def daily_values(d: dict) -> list:
    return [d["sandbox"], d["date"].isoformat(), fmt_time(d["eval_started"]),
            fmt_time(d["eval_ended"]), st.fmt_dur(d["eval_seconds"]),
            fmt_time(d["snap_started"]), fmt_time(d["snap_completed"]),
            st.fmt_dur(d["snap_seconds"]), st.fmt_dur(d["gap_seconds"]),
            d["ready"][0], d["ready"][1], st.run_rag(d["snap_seconds"]),
            d["records"], d["table"], d["batch_id"], d["job_id"]]


def write_xlsx(results: list[dict], conf: dict, label: str,
               days: int, cutoff: datetime):
    """One workbook per credential, OVERWRITTEN each run -- no timestamped
    copies and no CSV, so the output folder doesn't fill up."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        logger.error("openpyxl not installed -- nothing written "
                     "(pip install -r requirements.txt).")
        return None

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=_HEADER_BG)
    title_font = Font(bold=True, size=14)
    conf_font = Font(bold=True, size=11, color="C00000")
    note_font = Font(italic=True, color="666666")
    bad_font = Font(bold=True, color="C00000")
    section_fill = PatternFill("solid", fgColor="DCE6F1")
    wrap = Alignment(wrap_text=True, vertical="top")
    top = Alignment(vertical="top")
    ROW_H = 18
    tables = []

    def confidential(ws):
        ws["A1"] = CONFIDENTIAL
        ws["A1"].font = conf_font
        ws.oddHeader.center.text = f'&"-,Bold"&12&KC00000{CONFIDENTIAL}'
        ws.evenHeader.center.text = ws.oddHeader.center.text

    def note(ws, text, span, height):
        ws["A3"] = text
        ws["A3"].font = note_font
        ws["A3"].alignment = wrap
        ws.merge_cells(f"A3:{get_column_letter(span)}3")
        ws.row_dimensions[3].height = height

    def header(ws, cols, row):
        for c, nm in enumerate(cols, 1):
            cell = ws.cell(row, c, nm)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[row].height = 42
        tables.append((ws, row, len(cols)))

    def rag(cell, grade):
        fill, fg = st.RAG_COLOURS[grade]
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.font = Font(bold=True, color=fg)

    def band(ws, row, text, ncols):
        for c in range(1, ncols + 1):
            ws.cell(row, c).fill = section_fill
        ws.cell(row, 1, text).font = Font(bold=True, color=_HEADER_BG)
        ws.cell(row, 1).alignment = Alignment(vertical="center")
        ws.row_dimensions[row].height = 22

    def widths(ws, ws_widths):
        for i, w in enumerate(ws_widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    wb = Workbook()
    # House rule: the file's author is Barry, not the library ('openpyxl').
    wb.properties.creator = wb.properties.lastModifiedBy = SCRIPT_AUTHOR
    now = datetime.now(timezone.utc)

    # ---- Summary --------------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_properties.tabColor = _HEADER_BG
    confidential(ws)
    ws["A2"] = f"AEP Snapshot History -- last {days} days  ({now:%Y-%m-%d})"
    ws["A2"].font = title_font
    note(ws, "How the daily pipeline has behaved: the scheduled batch "
             "segmentation (system evaluation) runs, then the profile snapshot "
             "on the DEFAULT merge policy is written. One row per sandbox, "
             "production first. 'Snapshot completed' columns are times of day "
             "(UTC) across the window; '(+1d)' means it finished the day after "
             "it started. 'Gap' runs from the evaluation ending to the snapshot "
             "completing. RAG is on the p90 completed time: GREEN before "
             f"{fmt_hhmm(RAG_GREEN_BEFORE)}, AMBER before "
             f"{fmt_hhmm(RAG_AMBER_BEFORE)}, RED otherwise. It is a morning "
             "deadline, so a sandbox scheduled to evaluate in the afternoon "
             "reads RED by design. Days with no snapshot or no evaluation are "
             "blank on the Daily tab, not counted. 'On-demand' = evaluations "
             "triggered by API / UI / Flexible Audience Evaluation rather than "
             "the scheduler; a 'blackout' runs from the job starting to its "
             "export batch completing. Read-only.", 10, 126)
    facts = [
        ("Org", conf["org_id"]),
        ("Credential", label),
        ("Window", f"{cutoff:%Y-%m-%d %H:%M} to {now:%Y-%m-%d %H:%M} UTC "
                   f"({days} days)"),
        ("Sandboxes", len(results)),
        ("Generated (UTC)", now.strftime("%Y-%m-%d %H:%M:%S")),
        ("Generated by", f"{SCRIPT_NAME}.py v{SCRIPT_VERSION} ({SCRIPT_DATE})"),
    ]
    r0 = 5
    for i, (k, v) in enumerate(facts):
        ws.cell(r0 + i, 1, k).font = Font(bold=True)
        ws.cell(r0 + i, 2, v).alignment = Alignment(horizontal="left")
    r1 = r0 + len(facts) + 1
    cols = ["Sandbox", "RAG", "Days with a snapshot", "Snapshot completed: min",
            "Snapshot completed: median", "Snapshot completed: p90",
            "Snapshot completed: max", "Snapshot duration: median",
            "Snapshot duration: max", "Gap eval -> snapshot: median",
            "Gap eval -> snapshot: max",
            f"% days completed before {fmt_hhmm(RAG_GREEN_BEFORE)}",
            f"% days completed before {fmt_hhmm(RAG_AMBER_BEFORE)}",
            "Days with an evaluation", "On-demand runs",
            "Days with an on-demand run", "Longest on-demand blackout",
            "Snapshot table", "Merge policy", "Notes"]
    header(ws, cols, r1)
    ws.freeze_panes = ws.cell(r1 + 1, 2)
    rr, section = r1, None
    for res in results:
        if res["production"] != section:
            section = res["production"]
            rr += 1
            band(ws, rr, st.section_of(section), len(cols))
        rr += 1
        s = summarise(res)
        vals = [res["sandbox"], s["rag"], s["n_days"], fmt_hhmm(s["done_min"]),
                fmt_hhmm(s["done_med"]), fmt_hhmm(s["done_p90"]),
                fmt_hhmm(s["done_max"]), st.fmt_dur(s["dur_med"]),
                st.fmt_dur(s["dur_max"]), st.fmt_dur(s["gap_med"]),
                st.fmt_dur(s["gap_max"]), s["pct_0730"], s["pct_0830"],
                s["n_evals"], s["od_runs"], s["od_days"],
                st.fmt_dur(s["od_longest"]), res["table"], res["merge_policy"],
                "; ".join(res["notes"])]
        ws.row_dimensions[rr].height = ROW_H
        for c, v in enumerate(vals, 1):
            cell = ws.cell(rr, c, v)
            cell.alignment = top
            if c in (12, 13) and v is not None:
                cell.number_format = '0"%"'
            if c == 2 and v:
                rag(cell, v)
            if c == 20 and v:
                cell.font = bad_font
    widths(ws, [18, 10, 12, 14, 14, 14, 16, 14, 14, 14, 14, 14, 14, 12, 12, 14,
                16, 62, 24, 60])

    # ---- Daily ----------------------------------------------------------------
    dy = wb.create_sheet("Daily")
    dy.sheet_properties.tabColor = "7030A0"
    confidential(dy)
    dy["A2"] = "Daily -- one row per sandbox per day, newest first"
    dy["A2"].font = title_font
    note(dy, "Evaluation and snapshot joined by the UTC date each STARTED on. "
             "Blank cells mean no scheduled evaluation or no completed snapshot "
             "was found for that date. 'ready_by' compares the snapshot "
             "completed time with 08:00 / 09:00 UTC on that date. 'Snapshot "
             f"duration RAG': GREEN under {st.RAG_AMBER_MIN} min, AMBER "
             f"{st.RAG_AMBER_MIN}-{st.RAG_RED_MIN} min, RED {st.RAG_RED_MIN} min "
             "or more. Filter on Sandbox to read one trend top to bottom.", 9, 62)
    header(dy, DAILY_COLS, 5)
    dy.freeze_panes = "C6"
    i, section = 5, None
    for res in results:
        if res["production"] != section:
            section = res["production"]
            i += 1
            band(dy, i, st.section_of(section), len(DAILY_COLS))
        for d in res["daily"]:
            i += 1
            dy.row_dimensions[i].height = ROW_H
            for c, v in enumerate(daily_values(d), 1):
                cell = dy.cell(i, c, v)
                cell.alignment = top
                if c == 13 and isinstance(v, int):
                    cell.number_format = "#,##0"
                if c in (10, 11) and v:
                    rag(cell, "GREEN" if v == "Y" else "RED")
                if c in (8, 12) and d["snap_seconds"] is not None:
                    rag(cell, st.run_rag(d["snap_seconds"]))
    widths(dy, [16, 12, 20, 20, 12, 20, 20, 12, 16, 12, 12, 12, 14, 46, 28, 38])

    # ---- On-demand evals ------------------------------------------------------
    od = wb.create_sheet("On-demand evals")
    od.sheet_properties.tabColor = "C55A11"
    confidential(od)
    od["A2"] = "On-demand evaluations -- production first, newest first"
    od["A2"].font = title_font
    note(od, "Every evaluation NOT triggered by the scheduler (source 'api': "
             "Flexible Audience Evaluation, or an API / UI-triggered run), with "
             "the export batch it spawned. The export is the batch on the "
             "sandbox's flexible_audience dataset that started within "
             f"{int(EXPORT_MATCH_WINDOW.total_seconds() // 60)} minutes of the "
             "job ending -- on-demand runs do not write to the profile "
             "snapshot. 'Blackout window' runs from the job starting to the "
             "export completing. Blank export cells mean no batch was found.",
         9, 62)
    od_cols = ["Sandbox", "Date", "Started (UTC)", "Ended (UTC)", "Duration",
               "Audiences in run", "Export batch started (UTC)",
               "Export batch completed (UTC)", "Export duration",
               "Blackout window", "Blackout duration", "Status", "Source",
               "Export records", "Job id", "Export batch id", "Request id"]
    header(od, od_cols, 5)
    od.freeze_panes = "C6"
    i, section = 5, None
    for res in results:
        if not res["on_demand"]:
            continue
        if res["production"] != section:
            section = res["production"]
            i += 1
            band(od, i, st.section_of(section), len(od_cols))
        for r in res["on_demand"]:
            i += 1
            od.row_dimensions[i].height = ROW_H
            vals = [r["sandbox"], r["date"].isoformat() if r["date"] else "",
                    fmt_time(r["started"]), fmt_time(r["ended"]),
                    st.fmt_dur(r["seconds"]), r["audiences"],
                    fmt_time(r["export_started"]),
                    fmt_time(r["export_completed"]),
                    st.fmt_dur(r["export_seconds"]),
                    window(r["started"], r["export_completed"], r["date"])
                    if r["export_completed"] else "",
                    st.fmt_dur(r["blackout_seconds"]), r["status"], r["source"],
                    r["export_records"], r["job_id"], r["export_id"],
                    r["request_id"]]
            for c, v in enumerate(vals, 1):
                cell = od.cell(i, c, v)
                cell.alignment = top
                if c == 14 and isinstance(v, int):
                    cell.number_format = "#,##0"
                if c == 12 and v and v != "SUCCEEDED":
                    cell.font = bad_font
    widths(od, [16, 12, 20, 20, 12, 11, 20, 20, 12, 16, 12, 12, 9, 14, 38, 28, 38])

    # ---- Blackout calendar ----------------------------------------------------
    bc = wb.create_sheet("Blackout calendar")
    bc.sheet_properties.tabColor = "C55A11"
    confidential(bc)
    bc["A2"] = "Blackout calendar -- one row per sandbox per day, newest first"
    bc["A2"].font = title_font
    note(bc, "Every window in the day, as hh:mm-hh:mm UTC. 'Scheduled window' "
             "runs from the scheduled evaluation starting to the profile "
             "snapshot completing. 'On-demand windows' lists each on-demand run "
             "from the job starting to its export completing ('?' = no export "
             "found, '(+1d)' = finished the following day).", 7, 46)
    bc_cols = ["Sandbox", "Date", "Day", "Scheduled window (UTC)",
               "On-demand windows (UTC)", "On-demand runs", "Windows in the day"]
    header(bc, bc_cols, 5)
    bc.freeze_panes = "C6"
    i, section = 5, None
    for res in results:
        if res["production"] != section:
            section = res["production"]
            i += 1
            band(bc, i, st.section_of(section), len(bc_cols))
        for d in res["daily"]:
            i += 1
            runs = d["on_demand"]
            sched = window(d["eval_started"] or d["snap_started"],
                           d["snap_completed"], d["date"])
            vals = [d["sandbox"], d["date"].isoformat(), d["date"].strftime("%a"),
                    sched,
                    "\n".join(window(r["started"], r["export_completed"], d["date"])
                              for r in runs),
                    len(runs), len(runs) + (1 if sched else 0)]
            bc.row_dimensions[i].height = max(ROW_H, 15 * len(runs))
            for c, v in enumerate(vals, 1):
                cell = bc.cell(i, c, v)
                cell.alignment = wrap if c == 5 else top
                if c == 6 and v:
                    rag(cell, "AMBER" if v < 3 else "RED")
    widths(bc, [16, 12, 7, 24, 28, 12, 12])

    # ---- Raw ------------------------------------------------------------------
    rw = wb.create_sheet("Raw")
    confidential(rw)
    rw["A2"] = "Raw -- every batch and job row as fetched"
    rw["A2"].font = title_font
    note(rw, "'Used' = counted in the Daily tab: a snapshot batch that completed "
             "(status success or inactive -- Catalog flips every superseded "
             "snapshot to inactive) or a SUCCEEDED segment job triggered by the "
             "scheduler. On-demand (api) jobs, their export batches and "
             "unfinished batches are listed but not used there; on-demand runs "
             "have their own tab.", 9, 46)
    raw_cols = ["Sandbox", "Kind", "Used", "Status", "Source", "Created (UTC)",
                "Started (UTC)", "Completed (UTC)", "Duration",
                "Duration (seconds)", "Records", "Object", "Id", "Schedule id",
                "Classification", "Audiences in run", "Request id"]
    header(rw, raw_cols, 5)
    rw.freeze_panes = "C6"
    i = 5
    for res in results:
        for r in sorted(res["raw"], key=lambda x: x["created"] or cutoff,
                        reverse=True):
            i += 1
            rw.row_dimensions[i].height = ROW_H
            vals = [r["sandbox"], r["kind"], "Y" if r["used"] else "N",
                    r["status"], r["source"], fmt_time(r["created"]),
                    fmt_time(r["started"]), fmt_time(r["completed"]),
                    st.fmt_dur(r["seconds"]),
                    None if r["seconds"] is None else int(r["seconds"]),
                    r["records"], r["object"], r["id"], r["schedule_id"],
                    r["classification"], r["audiences"], r["request_id"]]
            for c, v in enumerate(vals, 1):
                cell = rw.cell(i, c, v)
                cell.alignment = top
                if c in (10, 11) and isinstance(v, int):
                    cell.number_format = "#,##0"
                if not r["used"]:
                    cell.font = Font(color="808080")
    widths(rw, [16, 22, 7, 12, 12, 20, 20, 20, 12, 12, 14, 50, 38, 38, 14, 12, 38])

    for w, row, ncols in tables:
        if w.max_row > row:
            w.auto_filter.ref = f"A{row}:{get_column_letter(ncols)}{w.max_row}"

    # ---- Charts ---------------------------------------------------------------
    # The numbers the charts plot live on their own tab (oldest day first, so
    # time reads left to right and ends today); the Charts tab sits straight
    # after the Summary.
    from openpyxl.chart import LineChart, Reference, Series

    cd = wb.create_sheet("Chart Data")
    confidential(cd)
    cd["A2"] = "Chart data -- snapshot time to run, per sandbox per day"
    cd["A2"].font = title_font
    note(cd, "What the Charts tab plots. Hours for the production block, minutes "
             "for the development / PPE block. Blank = no completed snapshot "
             "that day. The threshold columns draw the AMBER / RED lines.", 9, 34)
    days_asc = sorted({d["date"] for res in results for d in res["daily"]})
    n = len(days_asc)
    first, last = 6, 5 + n

    def block(col0, group, unit_secs, thresholds):
        """One table: Date, a column per sandbox, then the threshold columns.
        Returns {sandbox or threshold name: column index}."""
        names = [res["sandbox"] for res in group] + [t[0] for t in thresholds]
        header_cells = ["Date"] + names
        for c, nm in enumerate(header_cells, col0):
            cell = cd.cell(5, c, nm)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cd.column_dimensions[get_column_letter(c)].width = 14
        by = {res["sandbox"]: {d["date"]: d["snap_seconds"] for d in res["daily"]}
              for res in group}
        for i, day in enumerate(days_asc, first):
            cd.cell(i, col0, day.strftime("%d %b"))
            for c, res in enumerate(group, col0 + 1):
                secs = by[res["sandbox"]].get(day)
                if secs is not None:
                    cd.cell(i, c, round(secs / unit_secs, 2)).number_format = "0.00"
            for c, (_, value) in enumerate(thresholds, col0 + 1 + len(group)):
                cd.cell(i, c, value)
        return {nm: col0 + 1 + k for k, nm in enumerate(names)}

    prod_group = [r for r in results if r["production"]]
    dev_group = [r for r in results if not r["production"]]
    amber_h, red_h = st.RAG_AMBER_MIN / 60, st.RAG_RED_MIN / 60
    lines_h = [(f"AMBER ({amber_h:g}h)", amber_h), (f"RED ({red_h:g}h)", red_h)]
    lines_m = [(f"AMBER ({st.RAG_AMBER_MIN} min)", st.RAG_AMBER_MIN)]
    cd.row_dimensions[5].height = 42
    prod_cols = block(1, prod_group, 3600, lines_h) if prod_group else {}
    dev_col0 = 1 + (len(prod_cols) + 2 if prod_cols else 0)
    dev_cols = block(dev_col0, dev_group, 60, lines_m) if dev_group else {}
    cd.freeze_panes = "B6"

    PALETTE = ["1F4E78", "7030A0", "548235", "C55A11", "0070C0", "7F6000",
               "C00000", "008080"]
    THRESHOLD = {"AMBER": "FFC000", "RED": "C00000"}

    def chart(title, cats_col, cols, names, y_title, fmt):
        ch = LineChart()
        ch.title = title
        ch.height, ch.width = 11, 32
        ch.y_axis.title, ch.x_axis.title = y_title, "Day (oldest on the left, today on the right)"
        ch.y_axis.number_format = fmt
        ch.y_axis.scaling.min = 0
        # openpyxl hides the axes unless told otherwise.
        ch.x_axis.delete = ch.y_axis.delete = False
        ch.x_axis.tickLblSkip = max(1, n // 15)
        ch.legend.position = "b"
        k = 0
        for nm in names:
            col = cols[nm]
            s = Series(Reference(cd, min_col=col, min_row=first, max_row=last),
                       title=nm)
            grade = nm.split(" ")[0]
            if grade in THRESHOLD:
                s.graphicalProperties.line.solidFill = THRESHOLD[grade]
                s.graphicalProperties.line.dashStyle = "dash"
                s.graphicalProperties.line.width = 19050      # 1.5pt
            else:
                colour = PALETTE[k % len(PALETTE)]
                k += 1
                s.graphicalProperties.line.solidFill = colour
                s.graphicalProperties.line.width = 31750      # 2.5pt
                s.marker.symbol = "circle"
                s.marker.size = 6
                s.marker.graphicalProperties.solidFill = colour
                s.marker.graphicalProperties.line.solidFill = colour
            s.smooth = False
            ch.series.append(s)
        ch.set_categories(Reference(cd, min_col=cats_col, min_row=first,
                                    max_row=last))
        return ch

    cs = wb.create_sheet("Charts", 1)
    cs.sheet_properties.tabColor = "C00000"
    confidential(cs)
    cs["A2"] = f"Snapshot time to run -- last {days} days, ending today"
    cs["A2"].font = title_font
    note(cs, "How long the profile snapshot took to write each day. Time runs "
             "left to right and ends today. Dashed lines are the AMBER and RED "
             "thresholds. A gap in a line means no completed snapshot that day. "
             "The numbers are on the Chart Data tab.", 16, 34)
    row = 5
    if "prod" in prod_cols:
        cs.add_chart(chart("PROD -- snapshot time to run (hours)", 1, prod_cols,
                           ["prod"] + [t[0] for t in lines_h], "Hours", '0.0"h"'),
                     f"A{row}")
        row += 23
    if prod_cols:
        cs.add_chart(chart("ALL PRODUCTION sandboxes -- snapshot time to run (hours)",
                           1, prod_cols, list(prod_cols), "Hours", '0.0"h"'),
                     f"A{row}")
        row += 23
    if dev_cols:
        cs.add_chart(chart("ALL DEVELOPMENT / PPE sandboxes -- snapshot time to "
                           "run (minutes)", dev_col0, dev_cols, list(dev_cols),
                           "Minutes", "0"), f"A{row}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"snapshot_history_{label}.xlsx"
    try:
        wb.save(path)
    except PermissionError:
        alt = path.with_name(path.stem + f" ({datetime.now():%H%M%S})" + path.suffix)
        logger.warning(f"{path.name} is open; writing {alt.name} instead.")
        path = alt
        wb.save(path)
    return path


def print_summary(results: list[dict]) -> None:
    A = st.ANSI
    bar = A["cyan"] + "=" * 118 + A["reset"]
    print()
    print(bar)
    print(A["bold"] + f"  {'SANDBOX':<16}{'RAG':<8}{'DAYS':<6}"
          f"{'COMPLETED min / median / p90 / max (UTC)':<46}"
          f"{'DURATION median / max':<24}<07:30  <08:30" + A["reset"])
    print(A["cyan"] + "-" * 118 + A["reset"])
    section = None
    for res in results:
        if res["production"] != section:
            section = res["production"]
            print(f"  {A['bold']}{A['blue']}{st.section_of(section)}{A['reset']}")
        s = summarise(res)
        colour = {"GREEN": A["green"], "AMBER": A["yellow"],
                  "RED": A["red"] + A["bold"]}.get(s["rag"], A["dim"])
        done = " / ".join(fmt_hhmm(s[k]) or "-" for k in
                          ("done_min", "done_med", "done_p90", "done_max"))
        dur = f"{st.fmt_dur(s['dur_med']) or '-'} / {st.fmt_dur(s['dur_max']) or '-'}"
        p1 = "-" if s["pct_0730"] is None else f"{s['pct_0730']:.0f}%"
        p2 = "-" if s["pct_0830"] is None else f"{s['pct_0830']:.0f}%"
        print(f"  {A['yellow']}{res['sandbox'][:14]:<16}{A['reset']}"
              f"{colour}{s['rag'] or '-':<8}{A['reset']}{s['n_days']:<6}"
              f"{done:<46}{dur:<24}{p1:<8}{p2}")
    print(bar)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def parse_args(argv):
    opts = {"credential": DEFAULT_CREDENTIAL, "days": DEFAULT_DAYS,
            "sandboxes": None, "dump_job": False}
    it = iter(argv)
    for a in it:
        key, sep, val = a.partition("=")
        if key in ("--days", "--sandbox", "--sandboxes", "--credential") and not sep:
            val = next(it, "")          # '--days 30' as well as '--days=30'
        if key == "--days":
            try:
                opts["days"] = max(1, int(val))
            except ValueError:
                logger.warning(f"Ignoring bad --days value {val!r}.")
        elif key in ("--sandbox", "--sandboxes"):
            if val and val.lower() != "all":
                opts["sandboxes"] = [s.strip() for s in val.split(",") if s.strip()]
        elif key == "--credential":
            opts["credential"] = val.strip() or DEFAULT_CREDENTIAL
        elif a == "--dump-job":
            opts["dump_job"] = True
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            logger.warning(f"Ignoring unknown argument {a}")
    return opts


def banner(conf, opts):
    A = st.ANSI
    bar = A["cyan"] + "=" * 72 + A["reset"]
    print(bar)
    print(f"  {A['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{A['reset']}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {A['dim']}Timing history of the daily evaluation -> snapshot "
          f"pipeline, per sandbox (read-only){A['reset']}")
    print(f"  {A['bold']}Org:{A['reset']}      {A['magenta']}{conf['org_id']}{A['reset']}")
    print(f"  {A['bold']}Window:{A['reset']}   last {opts['days']} days")
    print(bar)


def main():
    opts = parse_args(sys.argv[1:])
    print(aep_creds.source_banner())
    try:
        chosen = aep_creds.pick_service(opts["credential"])
        conf = aep_creds.load_creds(chosen)
    except aep_creds.CredsError as e:
        logger.error(str(e))
        return
    banner(conf, opts)

    try:
        token = st.authenticate(conf)
    except Exception as e:
        logger.error(f"IMS auth FAILED: {st.describe_error(e)}")
        return
    logger.info("IMS authenticated.")

    ok, found = st.list_sandboxes(token, conf)
    sandboxes = []
    if ok:
        sandboxes = [s for s in found if isinstance(s, dict) and s.get("name")
                     and (s.get("state") or "active") == "active"]
    else:
        logger.warning(f"Sandbox list unavailable ({found}).")
    if opts["sandboxes"]:
        known = {s["name"]: s for s in sandboxes}
        sandboxes = [known.get(n) or {"name": n} for n in opts["sandboxes"]]
    elif not sandboxes:
        fallback = conf.get("sandbox") or st.DEFAULT_SANDBOX
        logger.warning(f"Falling back to the single sandbox '{fallback}' -- this "
                       f"is NOT the whole org. Pass --sandbox=a,b,c to name them.")
        sandboxes = [{"name": fallback}]
    sandboxes.sort(key=st.sandbox_order)
    logger.info(f"{len(sandboxes)} sandbox(es), production first.")

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=opts["days"])).replace(
        hour=0, minute=0, second=0, microsecond=0)
    results = []
    for sb in sandboxes:
        logger.info(f"Sandbox '{sb['name']}'...")
        results.append(collect_sandbox(token, conf, sb, opts["days"], cutoff))
        if opts["dump_job"] and results[-1]["sample_job"]:
            # One raw job, as returned, so the field names can be checked.
            print(json.dumps(results[-1]["sample_job"], indent=2))
            opts["dump_job"] = False

    print_summary(results)
    xlsx = write_xlsx(results, conf, chosen, opts["days"], cutoff)
    if xlsx:
        logger.info(f"Wrote workbook {xlsx}")
    print()
    logger.info("Done.")


if __name__ == "__main__":
    main()
