#!/usr/bin/env python3
"""
batch_eval_timing.py
====================
Measure how long BATCH audience evaluation actually takes in an AEP sandbox
(default: dev). Lists audiences with their evaluation method, focuses on the
BATCH-evaluated ones, and reports the timing of recent batch segment jobs --
the direct answer to "why is batch slow?".

At startup it prompts you to pick a credential set from ./creds/ (the same
credential bank used by credential_validator.py), authenticates against Adobe
IMS, then:

  1. Lists every audience in the sandbox (paged), tagging each with its
     evaluation method (batch / streaming / edge).
  2. Filters to BATCH-evaluated audiences and prints a table sorted by
     creation time (title, created, tags, id).
  3. Summarises the rate of audience creation (how many per month).
  4. Pages through ALL batch segment jobs (/segment/jobs) and reports how long
     each evaluation took -- the direct answer to "why is batch slow?". Each job
     row/CSV also carries the NAME(S) of the segment(s) it evaluated (resolved
     via /segment/definitions, so SYSTEM segments get a name too), the schedule
     time it fired at, and the scheduleId -- so the export can be filtered by
     audience name or grouped by schedule.
  5. Exports every job to ./output/batch_eval_timing_<sandbox>_<stamp>.csv
     (job_id, status, audience_names, segment_ids, schedule_id, source,
     scheduled_utc, ended_utc, duration, num_segments).

Single-audience probe (--audience): instead of the estate-wide report, point it
at ONE audience -- by id, by name substring, or picked from a filtered menu --
and it prints a "timing card": its createEpoch, last-modified, current count and
count-snapshot time, the last batch job to run in the sandbox, its FEEDERS (the
dependency segments it's built on, each with method/count/last-evaluated and an
EMPTY/STALE flag), and a plain stuck / not-stuck VERDICT. Answers "is this
specific audience constipated, or just new?" -- and, via the feeders, "is it
empty because a feeder isn't populated?". A dependent can only be as fresh and
full as its feeders, so a feeder sitting at 0 is the first place to look.

Read-only: it never creates, edits or deletes anything in AEP.

VDI-friendly: stdlib only, no pip install required.

Usage:
    python batch_eval_timing.py                 # interactive cred menu, dev sandbox
    python batch_eval_timing.py prod            # pick creds/prod.json by stem
    python batch_eval_timing.py --sandbox=stage # override sandbox
    python batch_eval_timing.py --jobs=50       # cap to 50 jobs (default: all)
    python batch_eval_timing.py --all-methods   # don't filter to batch-only
    python batch_eval_timing.py prod --audience  # probe ONE audience: is it stuck?
    python batch_eval_timing.py prod --audience=00000000-0000-0000-0000-000000000000
    python batch_eval_timing.py prod --audience="MY_AUDIENCE_NAME"
    python batch_eval_timing.py prod --schedules  # dump the scheduled-segmentation config
    python batch_eval_timing.py prod --verify-run --date=2026-07-01 --ids=id1,id2
    python batch_eval_timing.py prod --verify-run --job=<jobId> --ids-file=ids.txt

Verify-run mode (--verify-run): prove whether a specific job evaluated a set of
audiences. Give it --job=<jobId> or --date=YYYY-MM-DD (finds that day's scheduler
run) plus --ids=/--ids-file=. It reports PRESENT/ABSENT per audience AND the count
that job computed for it. The manifest is metrics.segmentedProfileCounter (the
scheduler job's segments[] holds only a trigger entry -- the counter holds the
1600+ segments it actually evaluated). This settles "did the 04:00 run evaluate it,
or only a later api job?": if PRESENT in the 04:00 scheduled run, evaluation
happened then and a later count change is a metric/display lag, not an eval lag.
Writes output/verify_run_<sandbox>_<stamp>.csv.

Schedules mode (--schedules): GET /config/schedules and print every sandbox
schedule -- id, state, cron/trigger time, and (for the batch_segmentation entry)
whether it targets ALL segments ['*'] or a specific list (ids resolved to names).
This is the direct answer to "is the estate really on the 4am schedule, or is it
materialised later by an api-triggered job?" -- if the schedule is active, at
04:00, and targets ALL, yet audiences only refresh hours later, the scheduled run
isn't what evaluates the estate. Writes output/schedules_<sandbox>_<stamp>.csv.
"""

from __future__ import annotations

import csv
import json
import logging
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_NAME = "batch_eval_timing"
SCRIPT_VERSION = "1.2.0"
SCRIPT_DATE = "2026-07-01"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent
CREDS_DIR = SCRIPT_DIR / "creds"
OUTPUT_DIR = SCRIPT_DIR / "output"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
AUDIENCES_URL = "https://platform.adobe.io/data/core/ups/audiences"
SEGMENT_JOBS_URL = "https://platform.adobe.io/data/core/ups/segment/jobs"
# Segment-definition detail. A batch job's segments[].segmentId is a segment-
# definition id (== audienceId), but that id space is a SUPERSET of the friendly
# /audiences list: it also carries SYSTEM segments (e.g. 'email-unsubscribers')
# that never appear in /audiences. We resolve job segment ids to names here so a
# system segment doesn't show up as a bare id.
SEGMENT_DEFS_URL = "https://platform.adobe.io/data/core/ups/segment/definitions"
# Scheduled-segmentation config. The sandbox-level schedules that decide WHEN
# (and WHICH) batch audiences are evaluated. The batch_segmentation entry here is
# the "does the 4am run cover the whole estate?" answer -- properties.segments is
# ['*'] for the whole estate, or a specific id list for a subset.
CONFIG_SCHEDULES_URL = "https://platform.adobe.io/data/core/ups/config/schedules"

DEFAULT_SANDBOX = "dev"
# Statuses that represent a batch evaluation that actually ran to completion.
# Timing stats / the distribution chart use ONLY these -- a KILLED or FAILED
# job's "end" timestamp is when it was abandoned (sometimes months later), not
# how long an evaluation takes, and those outliers wreck the time buckets.
COMPLETED_STATUSES = {"SUCCEEDED", "PROCESSED"}
# Job statuses used by the single-audience "is it stuck?" probe.
RUNNING_STATUSES = {"PROCESSING", "QUEUED", "QUEUEING", "NEW",
                    "RUNNING", "SCHEDULED", "STARTED"}
FAILED_STATUSES = {"FAILED", "ERROR", "KILLED", "CANCELLED", "CANCELED"}
# Profile-count fields AEP has exposed on the audience object / its detail.
COUNT_KEYS = ("profileCount", "totalProfiles", "profiles", "count", "totalRows")
PAGE_LIMIT = 100
JOBS_PAGE_LIMIT = 100
# Safety backstop so a runaway cursor can't loop forever. ~50k jobs.
MAX_JOB_PAGES = 500

DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

# ----------------------------------------------------------------------------
# ANSI / logging - matches credential_validator.py / batch_fetcher.py style
# ----------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h, mode.value | 0x0004)  # VT processing
    except Exception:
        pass

ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
}
LEVEL_COLOR = {
    "DEBUG": ANSI["dim"], "INFO": ANSI["green"],
    "WARNING": ANSI["yellow"],
    "ERROR": ANSI["red"] + ANSI["bold"],
    "CRITICAL": ANSI["red"] + ANSI["bold"],
}


class ColoredFormatter(logging.Formatter):
    def format(self, record):
        color = LEVEL_COLOR.get(record.levelname, "")
        ts = self.formatTime(record, "%H:%M:%S")
        return (
            f"{ANSI['dim']}{ts}{ANSI['reset']} "
            f"{color}[{record.levelname:<7}]{ANSI['reset']} "
            f"{record.getMessage()}"
        )


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(ColoredFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger("batch_eval_timing")
SSL_CTX = ssl._create_unverified_context()


# ----------------------------------------------------------------------------
# HTTP / IMS / credential helpers (shared shape with credential_validator.py)
# ----------------------------------------------------------------------------
def http(url, method="GET", headers=None, data=None, timeout=60):
    """Stdlib-only HTTP. Returns response bytes; raises HTTPError on 4xx/5xx."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
        return r.read()


def load_creds(path: Path):
    """Read a creds/<name>.json file. Underscored keys are treated as inline
    documentation and ignored. Requires client_id / client_secret / org_id."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    conf = {
        k: v.strip() if isinstance(v, str) else v
        for k, v in raw.items()
        if not k.startswith("_")
    }
    for key in ("client_id", "client_secret", "org_id"):
        if not conf.get(key):
            raise ValueError(f"Missing required key {key!r} in {path.name}")
    return conf


def authenticate(conf):
    """client_credentials grant against Adobe IMS -> access token string."""
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": conf["client_id"],
        "client_secret": conf["client_secret"],
        "scope": conf.get("scopes") or DEFAULT_SCOPES,
    }).encode("utf-8")
    body = http(
        conf.get("oauth_url") or IMS_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    return json.loads(body)["access_token"]


def aep_headers(token, conf, sandbox):
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "x-sandbox-name": sandbox,
        "Accept": "application/json",
    }


# ----------------------------------------------------------------------------
# Credential bank menu (same UX as credential_validator.py)
# ----------------------------------------------------------------------------
def discover_creds():
    paths = []
    if CREDS_DIR.exists():
        for p in sorted(CREDS_DIR.glob("*.json")):
            if p.stem == "example":
                continue
            paths.append(p)
    return paths


def menu(creds):
    """Prompt for ONE credential set (this tool targets a single sandbox)."""
    print()
    bar = ANSI["cyan"] + "=" * 70 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}Credential bank{ANSI['reset']}  "
          f"{ANSI['dim']}({CREDS_DIR}){ANSI['reset']}")
    print(ANSI["cyan"] + "-" * 70 + ANSI["reset"])
    for i, p in enumerate(creds, 1):
        print(f"  {ANSI['bold']}{i:>2}{ANSI['reset']}  "
              f"{ANSI['yellow']}{p.stem:<20}{ANSI['reset']} "
              f"{ANSI['dim']}{p.name}{ANSI['reset']}")
    print(bar)
    raw = input(f"\nPick a credential set by number "
                f"({ANSI['cyan']}1{ANSI['reset']}), blank to quit: ").strip()
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(creds):
        return creds[int(raw) - 1]
    logger.warning(f"Invalid choice: {raw}")
    return None


# ----------------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------------
def to_dt(value) -> datetime | None:
    """Best-effort parse of the many timestamp shapes AEP uses into an aware
    UTC datetime: epoch ms (int/str), epoch seconds, or ISO-8601 strings.
    Returns None when it can't be parsed."""
    if value in (None, "", 0):
        return None
    # Numeric epoch (audiences use ms; jobs sometimes use seconds).
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        if n > 1e12:        # milliseconds
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    # ISO-8601 string, possibly with a trailing Z.
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "?"


def fmt_dur(seconds: float | None) -> str:
    """Human-friendly duration, e.g. '4m 12s' or '1h 03m'."""
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


# ----------------------------------------------------------------------------
# Audiences
# ----------------------------------------------------------------------------
# An audience's evaluation method lives under evaluationInfo. We normalise the
# three AEP modes to friendly names: batch (scheduled), streaming (continuous),
# edge (synchronous / on-device).
def evaluation_method(aud: dict) -> str:
    ev = aud.get("evaluationInfo") or {}
    if (ev.get("batch") or {}).get("enabled"):
        return "batch"
    if (ev.get("continuous") or {}).get("enabled"):
        return "streaming"
    if (ev.get("synchronous") or {}).get("enabled"):
        return "edge"
    return "?"


def audience_created(aud: dict) -> datetime | None:
    """Resolve an audience's creation time across the field names AEP has used
    on the audiences / segment-definitions endpoints."""
    for key in ("createEpoch", "creationTime", "createdAt", "created"):
        dt = to_dt(aud.get(key))
        if dt:
            return dt
    return None


def audience_tags(aud: dict) -> list[str]:
    """Collect human-meaningful tags/labels from whichever fields are present.
    AEP exposes folder tags under `tags` (sometimes a dict of name->[values])
    and governance labels under `labels`."""
    out: list[str] = []
    tags = aud.get("tags")
    if isinstance(tags, dict):
        for k, v in tags.items():
            if isinstance(v, list):
                out.extend(str(x) for x in v)
            elif v:
                out.append(str(v))
            else:
                out.append(str(k))
    elif isinstance(tags, list):
        out.extend(str(x) for x in tags)
    labels = aud.get("labels")
    if isinstance(labels, list):
        out.extend(str(x) for x in labels)
    return out


def fetch_audiences(headers) -> list[dict]:
    """Page through every audience in the sandbox. The audiences API returns
    items under `children` and a cursor at _page.next; older deployments use
    `segments`. We handle both."""
    out: list[dict] = []
    start = None
    page = 0
    while True:
        page += 1
        params = {"limit": PAGE_LIMIT}
        if start:
            params["start"] = start
        url = f"{AUDIENCES_URL}?{urllib.parse.urlencode(params)}"
        try:
            body = http(url, headers=headers)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            logger.error(f"Audience list failed: HTTP {e.code} {detail}")
            break
        data = json.loads(body) or {}
        batch = data.get("children") or data.get("segments") or []
        out.extend(batch)
        logger.info(f"  page {page}: {len(batch)} audience(s) (running total {len(out)})")
        nxt = (data.get("_page") or {}).get("next")
        if not batch or len(batch) < PAGE_LIMIT or not nxt:
            break
        start = nxt
    return out


def print_audience_table(auds: list[dict]) -> None:
    """Table sorted oldest->newest by creation time."""
    rows = sorted(auds, key=lambda a: audience_created(a) or datetime.min.replace(tzinfo=timezone.utc))
    bar = ANSI["cyan"] + "=" * 132 + ANSI["reset"]
    print()
    print(bar)
    print(ANSI["bold"] +
          f"  {'CREATED (UTC)':<21}{'METHOD':<10}{'NAME':<46}{'TAGS':<26}ID" +
          ANSI["reset"])
    print(ANSI["cyan"] + "-" * 132 + ANSI["reset"])
    for a in rows:
        created = fmt_dt(audience_created(a))
        method = evaluation_method(a)
        name = (a.get("name") or "(unnamed)")[:44]
        tags = ", ".join(audience_tags(a))[:24]
        aid = a.get("id") or a.get("audienceId") or "?"
        mcolor = ANSI["yellow"] if method == "batch" else ANSI["dim"]
        print(f"  {ANSI['dim']}{created:<21}{ANSI['reset']}"
              f"{mcolor}{method:<10}{ANSI['reset']}"
              f"{name:<46}"
              f"{ANSI['blue']}{tags:<26}{ANSI['reset']}"
              f"{ANSI['dim']}{aid}{ANSI['reset']}")
    print(bar)
    print(f"  {ANSI['bold']}{len(rows)} audience(s){ANSI['reset']}")


def print_creation_rate(auds: list[dict]) -> None:
    """How fast are audiences being created? Count per calendar month."""
    months = Counter()
    undated = 0
    for a in auds:
        dt = audience_created(a)
        if dt:
            months[dt.strftime("%Y-%m")] += 1
        else:
            undated += 1
    if not months:
        return
    print()
    print(f"  {ANSI['bold']}Audience creation rate (per month){ANSI['reset']}")
    peak = max(months.values())
    for ym in sorted(months):
        n = months[ym]
        bar = "#" * max(1, round(n / peak * 40))
        print(f"     {ANSI['dim']}{ym}{ANSI['reset']}  "
              f"{ANSI['green']}{bar}{ANSI['reset']} {n}")
    if undated:
        print(f"     {ANSI['dim']}(undated: {undated}){ANSI['reset']}")


# ----------------------------------------------------------------------------
# Batch segment jobs -- "how long does each batch evaluation take?"
# ----------------------------------------------------------------------------
def fetch_segment_jobs(headers, max_jobs=None) -> list[dict]:
    """Page through segment (batch) jobs. These are the scheduled evaluations
    that materialise batch audiences -- their timing is what people mean by
    'batch is slow'. By default pulls EVERY job in the sandbox; pass max_jobs
    to cap the total. Mirrors the cursor pagination used for audiences
    (`_page.next` -> `start`)."""
    out: list[dict] = []
    start = None
    page = 0
    while page < MAX_JOB_PAGES:
        page += 1
        params = {"limit": JOBS_PAGE_LIMIT}
        if start:
            params["start"] = start
        url = f"{SEGMENT_JOBS_URL}?{urllib.parse.urlencode(params)}"
        try:
            body = http(url, headers=headers)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            logger.error(f"Segment-job list failed: HTTP {e.code} {detail}")
            break
        data = json.loads(body) or {}
        batch = data.get("children") or data.get("segmentJobs") or []
        out.extend(batch)
        logger.info(f"  page {page}: {len(batch)} job(s) (running total {len(out)})")
        if max_jobs and len(out) >= max_jobs:
            out = out[:max_jobs]
            break
        nxt = (data.get("_page") or {}).get("next")
        if not batch or len(batch) < JOBS_PAGE_LIMIT or not nxt:
            break
        start = nxt
    return out


def job_schedule_id(job: dict) -> str:
    """The scheduleId that triggered this job (properties.scheduleId). Jobs kicked
    off by the batch scheduler carry one; ad-hoc/manual runs usually don't. Lets
    you group every job that came from the same scheduled evaluation."""
    props = job.get("properties")
    if isinstance(props, dict):
        return str(props.get("scheduleId") or "")
    return ""


def make_name_resolver(headers, audiences: list[dict]):
    """Return resolve(segment_id) -> friendly name for the ids a batch job
    references. Seeds a cache from the audiences we already fetched (id -> name)
    then lazily GETs /segment/definitions/<id> for anything missing -- that's how
    SYSTEM segments (absent from /audiences) get a name instead of a bare id.
    Both hits and misses are cached, so each id is fetched at most once."""
    cache: dict[str, str] = {}
    for a in audiences:
        aid = a.get("id") or a.get("audienceId")
        if aid and a.get("name"):
            cache[str(aid)] = a["name"]

    def resolve(sid) -> str:
        key = str(sid)
        if key in cache:
            return cache[key]
        name = ""
        try:
            body = http(f"{SEGMENT_DEFS_URL}/{urllib.parse.quote(key, safe='')}",
                        headers=headers, timeout=30)
            name = (json.loads(body) or {}).get("name") or ""
        except Exception:
            name = ""            # deleted/inaccessible -> fall back to the id
        cache[key] = name
        return name

    return resolve


def job_audience_names(job: dict, resolve) -> list[str]:
    """Friendly names of the segment(s) a job evaluated, resolved from its
    segment ids. An unresolved id (deleted segment / no access) shows as its
    short id in parentheses so the row is never blank."""
    names = []
    for sid in sorted(job_segment_ids(job)):
        nm = resolve(sid)
        names.append(nm or f"({sid[:8]}..)")
    return names


def job_times(job: dict) -> tuple[datetime | None, datetime | None]:
    """Resolve (started, ended) for a segment job across the field names AEP
    has used. Falls back to created/updated when explicit start/complete
    timestamps aren't present."""
    started = None
    for key in ("startTime", "startedAt", "createEpoch", "creationTime", "created"):
        started = to_dt(job.get(key))
        if started:
            break
    ended = None
    for key in ("completedTime", "completedAt", "endTime", "updateEpoch", "updated"):
        ended = to_dt(job.get(key))
        if ended:
            break
    return started, ended


def print_segment_jobs(jobs: list[dict], resolve=None, max_rows: int = 40) -> None:
    """Per-job table: status + how long the batch evaluation took. The full set
    can be large, so only the most recent `max_rows` are printed -- but the
    timing summary underneath is computed over EVERY job (the CSV has them all).

    The SCHEDULED column is the scheduler fire time (creationTime) -- for a
    scheduler-triggered job that IS its schedule time. NAME(S) resolves the
    segment ids the job evaluated to friendly names via `resolve` (falls back to
    the id when no resolver is given)."""
    if not jobs:
        logger.info("No batch segment jobs returned (none recently, or no access).")
        return
    resolve = resolve or (lambda s: "")
    bar = ANSI["cyan"] + "=" * 140 + ANSI["reset"]
    print()
    print(bar)
    print(ANSI["bold"] +
          f"  {'SCHEDULED (UTC)':<21}{'STATUS':<12}{'DURATION':<12}"
          f"{'NAME(S)':<48}JOB ID" +
          ANSI["reset"])
    print(ANSI["cyan"] + "-" * 140 + ANSI["reset"])
    # Timing is measured over completed evaluations only; rows shown are most
    # recent first. Non-completed jobs (KILLED/FAILED) still appear in the
    # table but never feed the stats/chart -- their "end" is an abandonment
    # time, not a run-time.
    durations = []
    skipped = 0
    rows = sorted(jobs, key=lambda j: job_times(j)[0] or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True)
    for idx, j in enumerate(rows):
        started, ended = job_times(j)
        dur = (ended - started).total_seconds() if started and ended else None
        status = j.get("status") or "?"
        if dur is not None and dur >= 0:
            if status in COMPLETED_STATUSES:
                durations.append(dur)
            else:
                skipped += 1
        if idx >= max_rows:
            continue
        names = job_audience_names(j, resolve)
        # Show the first name (+N more) so a multi-segment job stays one line;
        # the CSV lists every name in full.
        label = names[0] if names else "?"
        if len(names) > 1:
            label = f"{label} (+{len(names) - 1})"
        label = label[:46]
        jid = j.get("id") or "?"
        scolor = (ANSI["green"] if status in ("SUCCEEDED", "PROCESSED")
                  else ANSI["red"] if status in ("FAILED", "ERROR")
                  else ANSI["yellow"])
        print(f"  {ANSI['dim']}{fmt_dt(started):<21}{ANSI['reset']}"
              f"{scolor}{status:<12}{ANSI['reset']}"
              f"{ANSI['bold']}{fmt_dur(dur):<12}{ANSI['reset']}"
              f"{ANSI['yellow']}{label:<48}{ANSI['reset']}"
              f"{ANSI['dim']}{jid}{ANSI['reset']}")
    if len(rows) > max_rows:
        print(f"  {ANSI['dim']}... {len(rows) - max_rows} more job(s) "
              f"(see CSV for the full list){ANSI['reset']}")
    print(bar)
    if durations:
        durations.sort()
        avg = sum(durations) / len(durations)
        mid = durations[len(durations) // 2]
        print(f"  {ANSI['bold']}Batch evaluation timing{ANSI['reset']} over "
              f"{len(durations)} completed job(s):  "
              f"min {fmt_dur(durations[0])}  |  median {fmt_dur(mid)}  |  "
              f"avg {fmt_dur(avg)}  |  max {fmt_dur(durations[-1])}")
        if skipped:
            print(f"  {ANSI['dim']}(excluded {skipped} non-completed job(s) "
                  f"-- KILLED/FAILED -- from the timing){ANSI['reset']}")
        print_duration_histogram(durations)
    else:
        logger.info("No completed jobs with a measurable duration to chart.")


def print_duration_histogram(durations: list[float], bins: int = 10) -> None:
    """Vertical time axis of how long batch evaluations take. The run-time
    range is carved into equal-width time bands and drawn SLOWEST-AT-TOP,
    FASTEST-AT-BOTTOM -- so the dense bunching of normal jobs sits low, an empty
    middle shows the gap, and a slow outlier floats alone at the top. The bar
    and number on each band are HOW MANY jobs ran that long."""
    if not durations:
        return
    lo, hi = min(durations), max(durations)
    print()
    print(f"  {ANSI['bold']}How long do batch evaluations take?{ANSI['reset']} "
          f"{ANSI['dim']}({len(durations)} jobs){ANSI['reset']}")
    print(f"  {ANSI['dim']}time runs up the page (slow at top, fast at bottom); "
          f"bar/number = how many jobs landed in that band{ANSI['reset']}")
    if hi <= lo:
        # All identical -- a single band is the whole story.
        print(f"     {ANSI['cyan']}{fmt_dur(lo):>20}{ANSI['reset']}  "
              f"{ANSI['green']}{'#' * 40}{ANSI['reset']} {len(durations)} jobs")
        return
    width = (hi - lo) / bins
    counts = [0] * bins
    for d in durations:
        idx = min(bins - 1, int((d - lo) / width))
        counts[idx] += 1
    peak = max(counts)
    # Draw slowest band first (top) down to fastest (bottom).
    for i in range(bins - 1, -1, -1):
        n = counts[i]
        edge_lo = lo + i * width
        edge_hi = lo + (i + 1) * width
        label = f"{fmt_dur(edge_lo)}-{fmt_dur(edge_hi)}"
        bar = "#" * round(n / peak * 40) if n else ""
        marker = "  <- most jobs here" if n == peak else ""
        # Empty bands print a faint baseline so the gap is visible, not blank.
        if not n:
            print(f"     {ANSI['dim']}{label:>22}  |{ANSI['reset']}")
        else:
            print(f"     {ANSI['cyan']}{label:>22}{ANSI['reset']}  "
                  f"{ANSI['green']}{bar:<40}{ANSI['reset']} "
                  f"{n:>4} jobs{marker}")


def write_segment_jobs_csv(jobs: list[dict], sandbox: str, stamp: str,
                           resolve=None) -> Path:
    """Write every job to output/batch_eval_timing_<sandbox>_<stamp>.csv with
    one row per job: its evaluation duration PLUS the segment name(s) it ran and
    the schedule that triggered it, so the sheet can be filtered by audience name
    or grouped by schedule. Returns the path written."""
    resolve = resolve or (lambda s: "")
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"batch_eval_timing_{sandbox}_{stamp}.csv"
    cols = [
        "job_id", "status", "audience_names", "segment_ids",
        "schedule_id", "source", "scheduled_utc", "ended_utc",
        "duration_seconds", "duration_human", "num_segments",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for j in jobs:
            started, ended = job_times(j)
            dur = (ended - started).total_seconds() if started and ended else None
            ids = sorted(job_segment_ids(j))
            names = job_audience_names(j, resolve)
            w.writerow([
                j.get("id") or "",
                j.get("status") or "",
                " | ".join(names),
                " | ".join(ids),
                job_schedule_id(j),
                j.get("source") or "",
                started.isoformat() if started else "",
                ended.isoformat() if ended else "",
                f"{dur:.0f}" if dur is not None and dur >= 0 else "",
                fmt_dur(dur) if dur is not None and dur >= 0 else "",
                len(ids),
            ])
    return path


# ----------------------------------------------------------------------------
# Scheduled-segmentation config -- "is the estate really on the 4am schedule?"
# ----------------------------------------------------------------------------
# This is the direct test behind "audiences built overnight aren't there in the
# morning": the sandbox has ONE batch_segmentation schedule that's meant to
# evaluate the estate. If it's active, at 04:00, and targets ['*'] but audiences
# still only refresh hours later in an api-triggered job, then the scheduled run
# isn't what actually materialises the estate.
def fetch_schedules(headers) -> list[dict]:
    """GET the sandbox scheduled-segmentation config. Returns the list of
    schedule objects (batch_segmentation, export, delta, system jobs, ...)."""
    try:
        body = http(CONFIG_SCHEDULES_URL, headers=headers)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        logger.error(f"Schedule config fetch failed: HTTP {e.code} {detail}")
        return []
    except Exception as e:
        logger.error(f"Schedule config fetch failed: {type(e).__name__}: {e}")
        return []
    data = json.loads(body) or {}
    if isinstance(data, list):
        return data
    return data.get("children") or data.get("schedules") or []


def cron_time_utc(cron: str) -> str:
    """Pull an HH:MM (UTC) out of a cron that pins a single daily time. Handles
    Quartz 6-7 field ('sec min hour dom mon dow [year]') and 5-field standard
    ('min hour dom mon dow'). Returns the raw cron when it's recurring/sub-daily
    (minute or hour not a plain number)."""
    if not isinstance(cron, str) or not cron.strip():
        return "-"
    parts = cron.split()
    if len(parts) >= 6:
        minute, hour = parts[1], parts[2]
    elif len(parts) == 5:
        minute, hour = parts[0], parts[1]
    else:
        return cron
    if minute.isdigit() and hour.isdigit():
        return f"{int(hour):02d}:{int(minute):02d}"
    return cron


def schedule_targets(sched: dict, resolve):
    """Describe what a schedule evaluates. For a batch_segmentation schedule,
    properties.segments is ['*'] (the whole estate) or a specific id list which
    we resolve to names. Non-segmentation schedules (export/delta/system) don't
    target segments. Returns (summary, detail_names)."""
    props = sched.get("properties") or {}
    segs = props.get("segments")
    if segs in ("*", ["*"]):
        return "ALL segments (*)", ""
    if isinstance(segs, list) and segs:
        names = [resolve(s) or f"({str(s)[:8]}..)" for s in segs]
        return f"{len(segs)} specific segment(s)", " | ".join(names)
    if segs is not None:
        return str(segs), ""
    return "(n/a - not a segmentation schedule)", ""


def schedule_updated(sched: dict) -> datetime | None:
    for key in ("updateEpoch", "updateTime", "updated"):
        dt = to_dt(sched.get(key))
        if dt:
            return dt
    return None


def print_schedules(scheds: list[dict], resolve) -> None:
    """Table of the sandbox schedules, batch_segmentation highlighted -- it's the
    one that answers 'does the 4am run cover the whole estate?'."""
    if not scheds:
        logger.info("No schedules returned (none configured, or no access).")
        return
    bar = ANSI["cyan"] + "=" * 132 + ANSI["reset"]
    print()
    print(bar)
    print(ANSI["bold"] +
          f"  {'STATE':<10}{'TYPE':<22}{'TIME(UTC)':<12}{'CRON':<18}"
          f"{'TARGETS':<28}SCHEDULE ID" + ANSI["reset"])
    print(ANSI["cyan"] + "-" * 132 + ANSI["reset"])
    for s in scheds:
        state = s.get("state") or "?"
        stype = (s.get("type") or "?")[:20]
        cron = (s.get("schedule") or "").strip()
        thms = cron_time_utc(cron)
        summary, detail = schedule_targets(s, resolve)
        sid = s.get("id") or "?"
        # The batch_segmentation schedule is the one people mean by "the 4am run".
        is_seg = s.get("type") == "batch_segmentation"
        scolor = ANSI["green"] if state == "active" else ANSI["dim"]
        tcolor = ANSI["yellow"] + ANSI["bold"] if is_seg else ANSI["dim"]
        print(f"  {scolor}{state:<10}{ANSI['reset']}"
              f"{tcolor}{stype:<22}{ANSI['reset']}"
              f"{thms:<12}{cron[:16]:<18}"
              f"{summary:<28}{ANSI['dim']}{sid}{ANSI['reset']}")
        if detail:
            print(f"  {'':<10}{ANSI['dim']}-> {detail[:118]}{ANSI['reset']}")
    print(bar)
    # Spell out the estate schedule explicitly.
    seg = [s for s in scheds if s.get("type") == "batch_segmentation"]
    if seg:
        for s in seg:
            summary, _ = schedule_targets(s, resolve)
            print(f"  {ANSI['bold']}Estate batch_segmentation schedule:{ANSI['reset']} "
                  f"state={s.get('state')}, time={cron_time_utc((s.get('schedule') or '').strip())} UTC, "
                  f"targets={summary}")
        print(f"  {ANSI['dim']}(If this is active/at 04:00/targets ALL yet audiences "
              f"only refresh hours later in an api job, the scheduled run isn't what "
              f"materialises the estate.){ANSI['reset']}")
    else:
        print(f"  {ANSI['yellow']}No batch_segmentation schedule found -- the estate "
              f"has no scheduled evaluation configured.{ANSI['reset']}")


def write_schedules_csv(scheds: list[dict], sandbox: str, stamp: str,
                        resolve) -> Path:
    """Write output/schedules_<sandbox>_<stamp>.csv -- one row per schedule with
    state, cron/time, what it targets (all vs named segments), and audit fields."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"schedules_{sandbox}_{stamp}.csv"
    cols = ["schedule_id", "name", "type", "state", "cron", "trigger_time_utc",
            "targets", "target_names", "created_by", "update_time_utc"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for s in scheds:
            cron = (s.get("schedule") or "").strip()
            summary, detail = schedule_targets(s, resolve)
            updated = schedule_updated(s)
            w.writerow([
                s.get("id") or "",
                s.get("name") or "",
                s.get("type") or "",
                s.get("state") or "",
                cron,
                cron_time_utc(cron),
                summary,
                detail,
                s.get("createdBy") or s.get("owner") or "",
                updated.isoformat() if updated else "",
            ])
    return path


def run_schedules(headers, sandbox) -> None:
    """--schedules mode: dump the sandbox scheduled-segmentation config and write
    a CSV. Answers whether the estate's daily evaluation is really on a schedule."""
    logger.info("Fetching scheduled-segmentation config (/config/schedules)...")
    scheds = fetch_schedules(headers)
    if not scheds:
        return
    # Resolver with no seed list -> lazily names any specific target ids.
    resolve = make_name_resolver(headers, [])
    print_schedules(scheds, resolve)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = write_schedules_csv(scheds, sandbox, stamp, resolve)
    logger.info(f"Wrote {len(scheds)} schedule(s) to {csv_path}")


# ----------------------------------------------------------------------------
# Verify-run -- "did THIS job actually evaluate these audiences?"
# ----------------------------------------------------------------------------
# The subtlety that makes this necessary: a job's segments[] is NOT the list of
# what it evaluated -- the daily scheduler job carries just a single trigger entry
# there while actually evaluating the whole estate. The AUTHORITATIVE manifest is
# metrics.segmentedProfileCounter (segmentId -> profile count computed this run),
# which for the 04:00 run holds 1600+ segments. So "was audience X evaluated by
# the 04:00 job?" is answered by that counter, not by segments[].
def fetch_job(headers, job_id) -> dict:
    """GET one segment job's full payload by id (richer than the list entry)."""
    if not job_id:
        return {}
    try:
        body = http(f"{SEGMENT_JOBS_URL}/{urllib.parse.quote(str(job_id), safe='')}",
                    headers=headers, timeout=60)
        return json.loads(body) or {}
    except urllib.error.HTTPError as e:
        logger.error(f"Job fetch failed: HTTP {e.code} "
                     f"{e.read().decode(errors='replace')[:200]}")
        return {}
    except Exception as e:
        logger.error(f"Job fetch failed: {type(e).__name__}: {e}")
        return {}


def job_evaluated_manifest(job: dict) -> dict:
    """Every segment the job ACTUALLY evaluated, mapped to the profile count it
    produced. Primary source is metrics.segmentedProfileCounter; segments[] ids
    are folded in (as count None) so a trigger-only entry is still reflected."""
    out: dict[str, int | None] = {}
    mc = (job.get("metrics") or {}).get("segmentedProfileCounter")
    if isinstance(mc, dict):
        for k, v in mc.items():
            out[str(k)] = v if isinstance(v, int) else None
    for sid in job_segment_ids(job):
        out.setdefault(str(sid), None)
    return out


def find_scheduler_job(jobs: list[dict], date_str: str) -> dict | None:
    """Find the scheduled batch_segmentation job that ran on date_str (UTC).
    Prefers source='scheduler'; returns the earliest if several ran that day."""
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.error(f"Bad --date {date_str!r}; use YYYY-MM-DD.")
        return None
    lo, hi = day, day + timedelta(days=1)
    cands = []
    for j in jobs:
        ct = to_dt(j.get("creationTime"))
        if ct and lo <= ct < hi and j.get("source") == "scheduler":
            cands.append((ct, j))
    if not cands:
        logger.error(f"No scheduler job found on {date_str} "
                     f"(source='scheduler'). Try --job=<id>.")
        return None
    cands.sort(key=lambda x: x[0])
    if len(cands) > 1:
        logger.info(f"{len(cands)} scheduler job(s) on {date_str}; using the "
                    f"earliest ({fmt_dt(cands[0][0])}).")
    return cands[0][1]


def print_verify(job: dict, ids: list[str], resolve) -> None:
    manifest = job_evaluated_manifest(job)
    started, ended = job_times(job)
    dur = (ended - started).total_seconds() if started and ended else None
    seg_only = len(job_segment_ids(job))
    C = ANSI
    bar = C["cyan"] + "=" * 104 + C["reset"]
    print()
    print(bar)
    print(f"  {C['bold']}Verify run{C['reset']}  "
          f"{C['dim']}did this job actually evaluate these audiences?{C['reset']}")
    print(bar)
    print(f"  {C['bold']}Job{C['reset']}        {job.get('id')}")
    print(f"  {C['bold']}Source{C['reset']}     {job.get('source')}    "
          f"schedule {job_schedule_id(job) or '(none)'}")
    print(f"  {C['bold']}Fired{C['reset']}      {fmt_dt(started)} UTC")
    print(f"  {C['bold']}Ran{C['reset']}        {fmt_dt(started)} -> {fmt_dt(ended)}"
          f"   ({fmt_dur(dur)})")
    print(f"  {C['bold']}Status{C['reset']}     {job.get('status')}")
    print(f"  {C['bold']}Evaluated{C['reset']}  {C['bold']}{len(manifest)}{C['reset']} "
          f"segment(s) in this run   {C['dim']}(from metrics.segmentedProfileCounter; "
          f"segments[] alone lists only {seg_only}){C['reset']}")
    print(C["cyan"] + "-" * 104 + C["reset"])
    present = 0
    for i in ids:
        key = str(i)
        in_it = key in manifest
        if in_it:
            present += 1
        cnt = manifest.get(key)
        name = (resolve(i) or "(unknown)")[:44]
        mark = (f"{C['green']}{C['bold']}PRESENT{C['reset']}" if in_it
                else f"{C['red']}{C['bold']}ABSENT {C['reset']}")
        cnt_s = f"{cnt:,}" if isinstance(cnt, int) else ("-" if in_it else "")
        print(f"  {mark}  {C['yellow']}{name:<44}{C['reset']} "
              f"{C['dim']}{i}{C['reset']}  {C['bold']}{cnt_s:>12}{C['reset']}")
    print(bar)
    verdict = (f"{C['green']}{C['bold']}{present}/{len(ids)} were evaluated by this job"
               if present == len(ids) else
               f"{C['yellow']}{C['bold']}{present}/{len(ids)} present, "
               f"{len(ids) - present} absent")
    print(f"  {verdict}{C['reset']}")
    if present == len(ids) and job.get("source") == "scheduler":
        print(f"  {C['dim']}=> evaluation happened in this scheduled run; if the UI "
              f"count updated later, that's a metric/display lag, not an eval lag."
              f"{C['reset']}")


def write_verify_csv(job: dict, ids: list[str], resolve,
                     sandbox: str, stamp: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    manifest = job_evaluated_manifest(job)
    started, _ = job_times(job)
    path = OUTPUT_DIR / f"verify_run_{sandbox}_{stamp}.csv"
    cols = ["audience_id", "audience_name", "present", "count_in_job",
            "job_id", "job_source", "schedule_id", "job_fired_utc", "job_status"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for i in ids:
            key = str(i)
            in_it = key in manifest
            cnt = manifest.get(key)
            w.writerow([
                i, resolve(i), "present" if in_it else "absent",
                cnt if isinstance(cnt, int) else "",
                job.get("id") or "", job.get("source") or "",
                job_schedule_id(job),
                started.isoformat() if started else "",
                job.get("status") or "",
            ])
    return path


def run_verify(headers, sandbox, job_sel, date_sel, ids) -> None:
    if not ids:
        logger.error("--verify-run needs audience ids: --ids=id1,id2,... "
                     "(or --ids-file=path).")
        return
    if job_sel:
        logger.info(f"Fetching job {job_sel} ...")
        job = fetch_job(headers, job_sel)
    elif date_sel:
        logger.info(f"Fetching all jobs to find the scheduler run on {date_sel} ...")
        jobs = fetch_segment_jobs(headers, None)
        found = find_scheduler_job(jobs, date_sel)
        job = fetch_job(headers, found.get("id")) if found else {}
    else:
        logger.error("--verify-run needs --job=<id> or --date=YYYY-MM-DD.")
        return
    if not job:
        return
    resolve = make_name_resolver(headers, [])
    print_verify(job, ids, resolve)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = write_verify_csv(job, ids, resolve, sandbox, stamp)
    logger.info(f"Wrote verification of {len(ids)} audience(s) to {csv_path}")


# ----------------------------------------------------------------------------
# Single-audience probe -- "is THIS audience stuck, or just new?"
# ----------------------------------------------------------------------------
def human_ago(dt: datetime | None, now: datetime) -> str:
    """Coarse '18h ago' / 'in 5m' relative to now. Deliberately low-precision --
    it's a gut-feel signal, not a stopwatch."""
    if not dt:
        return "?"
    secs = (now - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    if secs < 90:
        out = f"{int(secs)}s"
    elif secs < 5400:
        out = f"{int(secs // 60)}m"
    elif secs < 172800:
        out = f"{int(secs // 3600)}h"
    else:
        out = f"{int(secs // 86400)}d"
    return f"in {out}" if future else f"{out} ago"


def audience_modified(aud: dict) -> datetime | None:
    for key in ("updateEpoch", "updateTime", "modifiedAt", "lastModified", "updated"):
        dt = to_dt(aud.get(key))
        if dt:
            return dt
    return None


def raw_create_epoch(aud: dict):
    """The literal createEpoch value (ms) for display alongside the parsed date --
    it's the number the user actually asked to see."""
    for key in ("createEpoch", "creationTime"):
        v = aud.get(key)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def dig_count(obj) -> int | None:
    """Find a plausible profile-count integer, probing the nests AEP has used
    (the confirmed current shape is metrics.data.totalProfiles)."""
    if not isinstance(obj, dict):
        return None
    for k in COUNT_KEYS:
        v = obj.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return int(v)
        if isinstance(v, str) and v.isdigit():
            return int(v)
    for nest in ("metrics", "_metrics", "lifecycleMetrics", "audienceMetrics", "data"):
        sub = obj.get(nest)
        if isinstance(sub, dict):
            c = dig_count(sub)
            if c is not None:
                return c
    return None


def count_snapshot(aud: dict) -> datetime | None:
    """When the profile count last refreshed (metrics.updateEpoch). This is the
    authoritative 'when was THIS audience last evaluated' signal -- 'never'
    plus a 0 count on an old audience is the constipated case."""
    metrics = aud.get("metrics")
    if isinstance(metrics, dict):
        dt = to_dt(metrics.get("updateEpoch"))
        if dt:
            return dt
        data = metrics.get("data")
        if isinstance(data, dict):
            return to_dt(data.get("updateEpoch"))
    return None


def fetch_audience_detail(headers, aid) -> dict:
    """GET one audience's full object (the list payload can be thinner than the
    per-id detail -- metrics/createEpoch live more reliably here)."""
    if not aid:
        return {}
    try:
        body = http(f"{AUDIENCES_URL}/{urllib.parse.quote(str(aid), safe='')}",
                    headers=headers, timeout=30)
    except urllib.error.HTTPError as e:
        logger.warning(f"Audience detail fetch failed: HTTP {e.code} "
                       f"{e.read().decode(errors='replace')[:200]}")
        return {}
    except Exception as e:
        logger.warning(f"Audience detail fetch failed: {type(e).__name__}: {e}")
        return {}
    try:
        return json.loads(body) or {}
    except (ValueError, TypeError):
        return {}


def fetch_definition(headers, sid) -> dict:
    """GET one segment-definition's full object. Unlike /audiences this also
    returns SYSTEM segments and feeders that never surface in the friendly list,
    so it's what we use to resolve a dependency (feeder) id to its details."""
    if not sid:
        return {}
    try:
        body = http(f"{SEGMENT_DEFS_URL}/{urllib.parse.quote(str(sid), safe='')}",
                    headers=headers, timeout=30)
        return json.loads(body) or {}
    except Exception:
        return {}


def resolve_feeders(headers, aud: dict) -> list[dict]:
    """An audience's `dependencies` are the feeder segments it's built on
    (audience-of-audiences). Resolve each to the facts that reveal a lagging /
    broken feeder: name, evaluation method, current count, and when that count
    last refreshed. A feeder sitting at 0 (or never evaluated) is the classic
    reason a dependent audience comes back empty."""
    deps = aud.get("dependencies") or []
    out: list[dict] = []
    for did in deps:
        d = fetch_definition(headers, did)
        out.append({
            "id": str(did),
            "name": d.get("name") if d else None,
            "method": evaluation_method(d) if d else "?",
            "count": dig_count(d),
            "snapshot": count_snapshot(d),
        })
    return out


def job_segment_ids(job: dict) -> set[str]:
    """Audience/segment ids a segment job evaluated, across the shapes AEP uses.
    The scheduled batch job often lists none in the list payload (it evaluates
    every batch segment at once); when that's so we fall back to the sandbox-wide
    last job as the 'last evaluation opportunity'."""
    ids: set[str] = set()
    for key in ("segments", "segmentDefinitions", "batchSegments", "segmentIds"):
        v = job.get(key)
        if isinstance(v, list):
            for s in v:
                if isinstance(s, dict):
                    sid = s.get("segmentId") or s.get("id") or s.get("segmentDefinitionId")
                    if sid:
                        ids.add(str(sid))
                elif s:
                    ids.add(str(s))
    sid = job.get("segmentId")
    if sid:
        ids.add(str(sid))
    return ids


def analyse_jobs_for(jobs: list[dict], aid: str):
    """Return (sandbox_last_success, this_audience_last_success, running, failures)
    from the fetched job set. 'running' and 'failures' only count jobs that
    explicitly reference this audience id."""
    sandbox_last = None
    per_last = None
    running = False
    failures = []
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for j in jobs:
        status = (j.get("status") or "").upper()
        started, ended = job_times(j)
        when = ended or started
        mine = str(aid) in job_segment_ids(j)
        if status in COMPLETED_STATUSES and when:
            if not sandbox_last or when > sandbox_last:
                sandbox_last = when
            if mine and (not per_last or when > per_last):
                per_last = when
        if mine and status in RUNNING_STATUSES:
            running = True
        if mine and status in FAILED_STATUSES:
            failures.append((when, status, j.get("id")))
    failures.sort(key=lambda x: x[0] or floor, reverse=True)
    return sandbox_last, per_last, running, failures


def _failure_line(failures, now) -> str:
    when, status, _jid = failures[0]
    extra = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
    return (f"WARNING: {len(failures)} failed job(s) reference this audience -- "
            f"latest {status} at {fmt_dt(when)} ({human_ago(when, now)}){extra}.")


def build_verdict(method, created, snapshot, count, sandbox_last, running,
                  failures, now):
    """The whole point of the probe: turn the timestamps into a plain verdict.
    Returns (label, ansi_colour_key, [detail lines])."""
    if method == "streaming":
        if count and count > 0:
            return ("HEALTHY", "green",
                    [f"{count:,} profiles qualify (streaming / continuous eval)."])
        return ("STREAMING -- N/A", "yellow",
                ["Streaming audience: evaluated continuously, NOT by the batch job.",
                 "0 means nobody currently qualifies (or none have streamed in since "
                 "creation). Not a batch-timing / stuck issue."])
    if method == "edge":
        return ("EDGE -- N/A", "yellow",
                ["Edge/synchronous audience: evaluated on-device at request time.",
                 "A batch count of 0 is expected and not meaningful here."])
    # ---- batch ----
    if count and count > 0:
        return ("HEALTHY", "green",
                [f"{count:,} profiles as of the last evaluation "
                 f"({fmt_dt(snapshot)}, {human_ago(snapshot, now)})."
                 if snapshot else f"{count:,} profiles."])
    if running:
        return ("RUNNING NOW", "yellow",
                ["A batch job referencing this audience is in progress right now -- "
                 "the count may be about to change. Re-check when it finishes."])
    if snapshot is not None:
        lines = [f"It HAS been evaluated (count refreshed {fmt_dt(snapshot)}, "
                 f"{human_ago(snapshot, now)}) and came back 0.",
                 "So it is NOT stuck in the pipeline -- the definition genuinely "
                 "matches nobody. Check the PQL, the merge policy, or whether the "
                 "source data has landed."]
        if failures:
            lines.append(_failure_line(failures, now))
        return ("EVALUATED-EMPTY", "yellow", lines)
    if created and sandbox_last and created > sandbox_last:
        return ("NEW -- NOT STUCK", "green",
                [f"Created {human_ago(created, now)}; no batch job has completed since "
                 f"(last sandbox job: {fmt_dt(sandbox_last)}).",
                 "A 0 count is expected for a brand-new batch audience -- it will "
                 "populate on the next scheduled batch evaluation."])
    if created and sandbox_last and created <= sandbox_last:
        lines = [f"Created {human_ago(created, now)}, and batch job(s) HAVE completed "
                 f"since (last: {fmt_dt(sandbox_last)}) -- yet this audience has never "
                 f"produced a count snapshot.",
                 "That's the constipated case: it should have been evaluated by now but "
                 "wasn't. Likely a definition error, exclusion from the scheduled job, "
                 "or failing jobs."]
        if failures:
            lines.append(_failure_line(failures, now))
        return ("STUCK?", "red", lines)
    return ("UNKNOWN", "yellow",
            ["Not enough job history to judge -- no completed batch job found in the "
             "fetched set. Re-run with --jobs=all, or check /segment/jobs access."])


def print_feeders(feeders: list[dict], now: datetime) -> None:
    """List the target audience's feeder (dependency) segments and flag the ones
    that would starve it: empty or never-evaluated. This is the direct test of
    the 'a feeder isn't working / ran late' hypothesis."""
    if not feeders:
        return
    C = ANSI
    print(C["cyan"] + "-" * 78 + C["reset"])
    print(f"  {C['bold']}Feeders ({len(feeders)} dependenc"
          f"{'y' if len(feeders) == 1 else 'ies'}){C['reset']}  "
          f"{C['dim']}the segments this audience is built on{C['reset']}")
    suspects = 0
    for f in feeders:
        name = f["name"] or f"({f['id'][:8]}..)"
        count = f["count"]
        snap = f["snapshot"]
        empty = not count  # 0 or None
        never = snap is None
        if empty or never:
            suspects += 1
        mark = (f"{C['red']}<- EMPTY/STALE{C['reset']}"
                if (empty or never) else f"{C['green']}ok{C['reset']}")
        cnt = f"{count:,}" if isinstance(count, int) else "?"
        when = (f"{fmt_dt(snap)} ({human_ago(snap, now)})" if snap
                else "never evaluated")
        print(f"     {C['yellow']}{name[:40]:<40}{C['reset']} "
              f"{C['dim']}{f['method']:<9}{C['reset']} "
              f"{C['bold']}{cnt:>12}{C['reset']}  "
              f"{C['dim']}{when:<28}{C['reset']} {mark}")
    if suspects:
        print(f"  {C['red']}{C['bold']}{suspects} feeder(s) look empty or "
              f"un-evaluated{C['reset']} -- if this audience is empty, start here: "
              f"a dependent can only be as fresh/full as its feeders.")


def print_timing_card(aud: dict, detail: dict, jobs: list[dict],
                      feeders: list[dict] | None = None) -> None:
    now = datetime.now(timezone.utc)
    # The per-id detail is richer than the list object; overlay it where present.
    merged = dict(aud)
    merged.update({k: v for k, v in (detail or {}).items() if v is not None})

    name = merged.get("name") or "(unnamed)"
    aid = merged.get("id") or merged.get("audienceId") or "?"
    method = evaluation_method(merged)
    created = audience_created(merged)
    modified = audience_modified(merged)
    snapshot = count_snapshot(merged)
    count = dig_count(merged)
    raw_ep = raw_create_epoch(merged)
    lifecycle = merged.get("lifecycleState") or merged.get("lifecycle") or "?"
    tags = ", ".join(audience_tags(merged)) or "None"

    sandbox_last, per_last, running, failures = analyse_jobs_for(jobs, aid)
    label, color, lines = build_verdict(method, created, snapshot, count,
                                        sandbox_last, running, failures, now)

    C = ANSI
    bar = C["cyan"] + "=" * 78 + C["reset"]

    def row(k, v):
        print(f"  {C['bold']}{k:<16}{C['reset']}{v}")

    print()
    print(bar)
    print(f"  {C['bold']}Audience timing card{C['reset']}  "
          f"{C['dim']}(is this one stuck, or just new?){C['reset']}")
    print(bar)
    row("Name", f"{C['yellow']}{name}{C['reset']}")
    row("ID", f"{C['dim']}{aid}{C['reset']}")
    row("Evaluation", method)
    row("Lifecycle", lifecycle)
    ep_str = f"   {C['dim']}(createEpoch {raw_ep}){C['reset']}" if raw_ep else ""
    c_ago = f"   {C['cyan']}<- {human_ago(created, now)}{C['reset']}" if created else ""
    row("Created", f"{fmt_dt(created)}{ep_str}{c_ago}")
    m_ago = f"   {C['cyan']}<- {human_ago(modified, now)}{C['reset']}" if modified else ""
    row("Last modified", f"{fmt_dt(modified)}{m_ago}")
    cnt = f"{count:,}" if isinstance(count, int) else "?"
    snap = (f"   {C['dim']}(refreshed {fmt_dt(snapshot)}, {human_ago(snapshot, now)})"
            f"{C['reset']}" if snapshot else f"   {C['dim']}(never refreshed){C['reset']}")
    row("Profile count", f"{C['bold']}{cnt}{C['reset']}{snap}")
    row("Tags", tags)
    print(C["cyan"] + "-" * 78 + C["reset"])
    sb = (f"{fmt_dt(sandbox_last)}   {C['dim']}(sandbox-wide, "
          f"{human_ago(sandbox_last, now)}){C['reset']}" if sandbox_last
          else "(none found in fetched jobs)")
    row("Last batch job", sb)
    if per_last:
        row("This aud. last", f"{fmt_dt(per_last)}   {C['dim']}"
            f"({human_ago(per_last, now)}){C['reset']}")
    print_feeders(feeders or [], now)
    print(C["cyan"] + "-" * 78 + C["reset"])
    vcolor = C.get(color, "")
    print(f"  {C['bold']}{'Verdict':<16}{C['reset']}{vcolor}{C['bold']}{label}{C['reset']}")
    for ln in lines:
        print(f"  {'':<16}{ln}")
    print(bar)


def _looks_like_id(s: str) -> bool:
    s = (s or "").strip()
    return len(s) >= 16 and all(c in "0123456789abcdefABCDEF-" for c in s)


def pick_from_list(items: list[dict], limit: int = 50) -> dict | None:
    show = items[:limit]
    for i, a in enumerate(show, 1):
        name = (a.get("name") or "(unnamed)")[:50]
        method = evaluation_method(a)
        aid = a.get("id") or a.get("audienceId") or "?"
        print(f"  {ANSI['bold']}{i:>3}{ANSI['reset']}  "
              f"{name:<52}{ANSI['dim']}{method:<10}{aid}{ANSI['reset']}")
    if len(items) > limit:
        print(f"  {ANSI['dim']}... {len(items) - limit} more; narrow your "
              f"filter.{ANSI['reset']}")
    raw = input("Pick a number (blank to cancel): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(show):
        return show[int(raw) - 1]
    return None


def pick_audience(audiences: list[dict]) -> dict | None:
    """Interactive filter-then-pick over the audience list."""
    while True:
        term = input("\nFilter audiences by name/id substring "
                     "(blank = show all, q = quit): ").strip()
        if term.lower() == "q":
            return None
        low = term.lower()
        matches = ([a for a in audiences
                    if low in (a.get("name") or "").lower()
                    or low in str(a.get("id") or a.get("audienceId") or "").lower()]
                   if term else list(audiences))
        if not matches:
            print("  no matches; try again.")
            continue
        matches.sort(key=lambda a: (a.get("name") or "").lower())
        chosen = pick_from_list(matches)
        if chosen:
            return chosen


def resolve_target_audience(audiences, selector, headers) -> dict | None:
    """Find the one audience to probe: exact id (in the list or by direct GET),
    then unique name/id substring, else an interactive picker."""
    if selector:
        sel = selector.strip()
        for a in audiences:
            if str(a.get("id") or a.get("audienceId") or "") == sel:
                return a
        if _looks_like_id(sel):
            d = fetch_audience_detail(headers, sel)
            if d and (d.get("id") or d.get("audienceId")):
                return d
        low = sel.lower()
        matches = [a for a in audiences
                   if low in (a.get("name") or "").lower()
                   or low in str(a.get("id") or a.get("audienceId") or "").lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            logger.error(f"No audience matches {selector!r} (by id or name).")
            return None
        logger.info(f"{len(matches)} audiences match {selector!r}; pick one:")
        return pick_from_list(sorted(matches, key=lambda a: (a.get("name") or "").lower()))
    return pick_audience(audiences)


def run_probe(headers, audiences, selector, jobs_cap) -> None:
    target = resolve_target_audience(audiences, selector, headers)
    if not target:
        logger.info("No audience selected.")
        return
    aid = target.get("id") or target.get("audienceId") or ""
    detail = {}
    if aid:
        logger.info(f"Fetching detail for audience {aid} ...")
        detail = fetch_audience_detail(headers, aid)
    scope = f"up to {jobs_cap}" if jobs_cap else "ALL"
    logger.info(f"Fetching {scope} batch segment job(s) to locate the last "
                f"evaluation...")
    jobs = fetch_segment_jobs(headers, jobs_cap)
    # Feeders come from the richer per-id detail (the list object omits them).
    feeders = resolve_feeders(headers, detail or target)
    if feeders:
        logger.info(f"Resolving {len(feeders)} feeder (dependency) segment(s)...")
    print_timing_card(target, detail, jobs, feeders)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv):
    # jobs=None means "all jobs" (paginate everything); --jobs=N caps the total.
    opts = {"sandbox": None, "jobs": None, "all_methods": False, "name": None,
            "audience_mode": False, "audience_sel": None, "schedules_mode": False,
            "verify_mode": False, "job_sel": None, "date_sel": None, "ids": []}
    for a in argv:
        if a.startswith("--sandbox="):
            opts["sandbox"] = a.split("=", 1)[1].strip() or None
        elif a.startswith("--jobs="):
            val = a.split("=", 1)[1].strip()
            if val.lower() in ("all", "0", ""):
                opts["jobs"] = None
            else:
                try:
                    opts["jobs"] = int(val)
                except ValueError:
                    pass
        elif a in ("--all-methods", "--all"):
            opts["all_methods"] = True
        elif a in ("--schedules", "--schedule"):
            opts["schedules_mode"] = True
        elif a in ("--verify-run", "--verify"):
            opts["verify_mode"] = True
        elif a.startswith("--job="):
            opts["job_sel"] = a.split("=", 1)[1].strip() or None
        elif a.startswith("--date="):
            opts["date_sel"] = a.split("=", 1)[1].strip() or None
        elif a.startswith("--ids="):
            opts["ids"] = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
        elif a.startswith("--ids-file="):
            fp = a.split("=", 1)[1].strip()
            try:
                text = Path(fp).read_text(encoding="utf-8")
                opts["ids"] = [ln.strip() for ln in text.replace(",", "\n").splitlines()
                               if ln.strip()]
            except OSError as e:
                logger.error(f"Could not read --ids-file {fp!r}: {e}")
        elif a == "--audience":
            opts["audience_mode"] = True
        elif a.startswith("--audience=") or a.startswith("--id="):
            opts["audience_mode"] = True
            opts["audience_sel"] = a.split("=", 1)[1].strip() or None
        elif a.startswith("-"):
            continue
        else:
            opts["name"] = a  # creds stem
    return opts


def banner(conf, sandbox):
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {ANSI['dim']}Measure how long batch audience evaluation takes in an AEP "
          f"sandbox (read-only){ANSI['reset']}")
    print(f"  {ANSI['bold']}AEP Batch Evaluation Timing{ANSI['reset']}  "
          f"{ANSI['dim']}(read-only){ANSI['reset']}")
    print(f"  {ANSI['bold']}Org:{ANSI['reset']}      {ANSI['magenta']}{conf['org_id']}{ANSI['reset']}")
    print(f"  {ANSI['bold']}Sandbox:{ANSI['reset']}  {ANSI['yellow']}{sandbox}{ANSI['reset']}")
    print(bar)


def main():
    opts = parse_args(sys.argv[1:])

    creds = discover_creds()
    if not creds:
        logger.error(f"No credential JSONs found in {CREDS_DIR}. "
                     f"Drop your <tenant>.json files there.")
        return

    # Resolve which credential set to use: by stem on the CLI, else the menu.
    if opts["name"]:
        by_stem = {p.stem: p for p in creds}
        path = by_stem.get(opts["name"])
        if not path:
            logger.error(f"No credential set named {opts['name']!r} in {CREDS_DIR}")
            return
    else:
        path = menu(creds)
    if not path:
        logger.info("Nothing chosen. Exiting.")
        return

    try:
        conf = load_creds(path)
    except Exception as e:
        logger.error(f"Failed to load {path.name}: {e}")
        return

    sandbox = opts["sandbox"] or conf.get("sandbox") or DEFAULT_SANDBOX
    if sandbox == "all":
        sandbox = DEFAULT_SANDBOX
    banner(conf, sandbox)

    # Authenticate.
    try:
        token = authenticate(conf)
    except urllib.error.HTTPError as e:
        logger.error(f"IMS auth FAILED: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
        return
    except Exception as e:
        logger.error(f"IMS auth FAILED: {type(e).__name__}: {e}")
        return
    logger.info("IMS authenticated.")
    headers = aep_headers(token, conf, sandbox)

    # Schedules mode short-circuits the estate report: dump the scheduled-
    # segmentation config (does the 4am run really cover the whole estate?),
    # write a CSV, then stop. No need to list audiences first.
    if opts["schedules_mode"]:
        run_schedules(headers, sandbox)
        print()
        logger.info("Done.")
        return

    # Verify-run mode: prove whether a given job (or the scheduler job on a date)
    # evaluated a set of audiences, using the job's real manifest.
    if opts["verify_mode"]:
        run_verify(headers, sandbox, opts["job_sel"], opts["date_sel"], opts["ids"])
        print()
        logger.info("Done.")
        return

    # 1) List audiences.
    logger.info(f"Listing audiences in sandbox '{sandbox}'...")
    audiences = fetch_audiences(headers)
    if not audiences:
        logger.warning("No audiences returned (empty sandbox, or no access).")
        return

    # Single-audience probe short-circuits the estate report: pick one audience
    # and print its stuck / not-stuck timing card, then stop.
    if opts["audience_mode"]:
        run_probe(headers, audiences, opts["audience_sel"], opts["jobs"])
        print()
        logger.info("Done.")
        return

    # Method breakdown across everything we found.
    methods = Counter(evaluation_method(a) for a in audiences)
    logger.info("Evaluation methods: " +
                ", ".join(f"{m}={n}" for m, n in methods.most_common()))

    # 2) Batch-only table (unless --all-methods).
    if opts["all_methods"]:
        shown = audiences
        logger.info(f"Showing all {len(shown)} audience(s) (--all-methods).")
    else:
        shown = [a for a in audiences if evaluation_method(a) == "batch"]
        logger.info(f"Filtering to {len(shown)} BATCH audience(s) "
                    f"(pass --all-methods to see every method).")
    print_audience_table(shown)

    # 3) Creation rate (over the filtered set).
    print_creation_rate(shown)

    # 4) Batch segment jobs -- the timing concern. Pull EVERY job (paged)
    #    unless the user capped it with --jobs=N.
    print()
    scope = f"up to {opts['jobs']}" if opts["jobs"] else "ALL"
    logger.info(f"Fetching {scope} batch segment job(s) (paged)...")
    jobs = fetch_segment_jobs(headers, opts["jobs"])
    # Resolver turns each job's segment ids into names (seeded from the audiences
    # already fetched, with a lazy /segment/definitions lookup for system ones).
    resolve = make_name_resolver(headers, audiences)
    print_segment_jobs(jobs, resolve)

    # 5) Export the full set to CSV for offline analysis.
    if jobs:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        csv_path = write_segment_jobs_csv(jobs, sandbox, stamp, resolve)
        logger.info(f"Wrote {len(jobs)} job(s) to {csv_path}")

    print()
    logger.info("Done.")


if __name__ == "__main__":
    main()
