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
     each evaluation took -- the direct answer to "why is batch slow?".
  5. Exports every job to ./output/batch_eval_timing_<sandbox>_<stamp>.csv.

Read-only: it never creates, edits or deletes anything in AEP.

VDI-friendly: stdlib only, no pip install required.

Usage:
    python batch_eval_timing.py                 # interactive cred menu, dev sandbox
    python batch_eval_timing.py prod            # pick creds/prod.json by stem
    python batch_eval_timing.py --sandbox=stage # override sandbox
    python batch_eval_timing.py --jobs=50       # cap to 50 jobs (default: all)
    python batch_eval_timing.py --all-methods   # don't filter to batch-only
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
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_NAME = "batch_eval_timing"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-06-24"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent
CREDS_DIR = SCRIPT_DIR / "creds"
OUTPUT_DIR = SCRIPT_DIR / "output"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
AUDIENCES_URL = "https://platform.adobe.io/data/core/ups/audiences"
SEGMENT_JOBS_URL = "https://platform.adobe.io/data/core/ups/segment/jobs"

DEFAULT_SANDBOX = "dev"
# Statuses that represent a batch evaluation that actually ran to completion.
# Timing stats / the distribution chart use ONLY these -- a KILLED or FAILED
# job's "end" timestamp is when it was abandoned (sometimes months later), not
# how long an evaluation takes, and those outliers wreck the time buckets.
COMPLETED_STATUSES = {"SUCCEEDED", "PROCESSED"}
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


def print_segment_jobs(jobs: list[dict], max_rows: int = 40) -> None:
    """Per-job table: status + how long the batch evaluation took. The full set
    can be large, so only the most recent `max_rows` are printed -- but the
    timing summary underneath is computed over EVERY job (the CSV has them all)."""
    if not jobs:
        logger.info("No batch segment jobs returned (none recently, or no access).")
        return
    bar = ANSI["cyan"] + "=" * 110 + ANSI["reset"]
    print()
    print(bar)
    print(ANSI["bold"] +
          f"  {'STARTED (UTC)':<21}{'STATUS':<12}{'DURATION':<12}{'#SEG':<6}JOB ID" +
          ANSI["reset"])
    print(ANSI["cyan"] + "-" * 110 + ANSI["reset"])
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
        segs = j.get("segments") or j.get("segmentDefinitions") or j.get("batchSegments") or []
        nseg = len(segs) if isinstance(segs, list) else "?"
        jid = j.get("id") or "?"
        scolor = (ANSI["green"] if status in ("SUCCEEDED", "PROCESSED")
                  else ANSI["red"] if status in ("FAILED", "ERROR")
                  else ANSI["yellow"])
        print(f"  {ANSI['dim']}{fmt_dt(started):<21}{ANSI['reset']}"
              f"{scolor}{status:<12}{ANSI['reset']}"
              f"{ANSI['bold']}{fmt_dur(dur):<12}{ANSI['reset']}"
              f"{str(nseg):<6}"
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


def write_segment_jobs_csv(jobs: list[dict], sandbox: str, stamp: str) -> Path:
    """Write every job to output/batch_eval_timing_<sandbox>_<stamp>.csv with
    one row per job and its evaluation duration. Returns the path written."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"batch_eval_timing_{sandbox}_{stamp}.csv"
    cols = [
        "job_id", "status", "started_utc", "ended_utc",
        "duration_seconds", "duration_human", "num_segments",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for j in jobs:
            started, ended = job_times(j)
            dur = (ended - started).total_seconds() if started and ended else None
            segs = j.get("segments") or j.get("segmentDefinitions") or j.get("batchSegments") or []
            nseg = len(segs) if isinstance(segs, list) else ""
            w.writerow([
                j.get("id") or "",
                j.get("status") or "",
                started.isoformat() if started else "",
                ended.isoformat() if ended else "",
                f"{dur:.0f}" if dur is not None and dur >= 0 else "",
                fmt_dur(dur) if dur is not None and dur >= 0 else "",
                nseg,
            ])
    return path


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv):
    # jobs=None means "all jobs" (paginate everything); --jobs=N caps the total.
    opts = {"sandbox": None, "jobs": None, "all_methods": False, "name": None}
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

    # 1) List audiences.
    logger.info(f"Listing audiences in sandbox '{sandbox}'...")
    audiences = fetch_audiences(headers)
    if not audiences:
        logger.warning("No audiences returned (empty sandbox, or no access).")
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
    print_segment_jobs(jobs)

    # 5) Export the full set to CSV for offline analysis.
    if jobs:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        csv_path = write_segment_jobs_csv(jobs, sandbox, stamp)
        logger.info(f"Wrote {len(jobs)} job(s) to {csv_path}")

    print()
    logger.info("Done.")


if __name__ == "__main__":
    main()
