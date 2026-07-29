#!/usr/bin/env python3
"""
sluggishness_detector.py  --  AEP 8am Audience Count Board
==========================================================
Show the REAL profile count of every audience built yesterday, at 08:00 --
hours before the platform UI gets around to displaying it.

The problem this solves
-----------------------
Someone builds a batch audience in the afternoon and needs to send it to Audience
API the next morning. They walk in at 8-9am, open it, and it shows 0 / blank -- so
they don't trust it, or they send it and it fails. The count they need doesn't
appear on screen until ~10:14.

The breakthrough (proven against prod)
--------------------------------------
The count is NOT actually late -- it's HIDDEN. When the overnight scheduled run
evaluates an audience it writes that audience's EXACT profile count into the job
itself (metrics.segmentedProfileCounter), and that job finishes ~07:00. A separate
downstream telemetry/export step is what copies those counts onto the audience
record for the UI, and THAT doesn't run until ~09:14 UTC / 10:14 BST -- stamping
every audience in the estate at the same instant (which is why audience
metrics.updateEpoch is identical for all 2400 audiences and useless as a per-
audience signal). So the number the UI shows at 10:14 was already computed and
sitting in the 07:00 job. This tool reads it from the job and hands it to you at
8am. It works for segment-of-segments (audience-of-audiences), which the on-demand
estimate API cannot price.

  * READY   = the audience is in the overnight run's count manifest -> here's its
              exact count, available now (~07:00), the same number the UI shows at
              10:14.
  * MISSING = built before the 04:00 run but NOT in its counts -> genuinely wasn't
              evaluated overnight, so no count exists yet anywhere. The real
              exception to chase (rare).
  * TOO-NEW = built AFTER the 04:00 run kicked off -> not due until the next cycle.
  * UNKNOWN = no creation time / no completed overnight run to compare against.

Only BATCH audiences are considered (streaming/edge don't wait on the nightly run).

Modes
-----
  (default)      Count board. For every batch audience built in the last N days
                 (--days, default 3), look its exact count up in the overnight
                 run's manifest and print it. This is the 8am cron target.
  --audience=X   One audience (by id or name substring): its count from the
                 overnight run, plus feeders if it wasn't counted.

Headless / cron (target: 08:00, AFTER the ~07:00 overnight run, BEFORE staff)
----------------------------------------------------------------------------
Pass the keyring service name as the first argument so there's no menu (no name +
no TTY = fail fast, never hangs). `--json[=PATH]` emits a machine-readable result
for whatever delivers it (email/Slack/an Adobe app/a dashboard). `--fail-on-missing`
exits 2 when any built-before-04:00 audience has no count, so a monitor can trigger
on the exit code. Logs go to stderr so `--json` stdout stays clean.

Read-only: it never creates, edits or deletes anything in AEP.

Credentials come from the OS keyring (Windows Credential Manager) via aep_creds,
with a plaintext creds/*.json fallback; manage them with credential_validator_v2.py.

Usage:
    python sluggishness_detector.py                        # cred menu, dev sandbox
    python sluggishness_detector.py "t mann" --sandbox=prod
    python sluggishness_detector.py "t mann" --sandbox=prod --days=4
    python sluggishness_detector.py "t mann" --sandbox=prod --json=output/counts.json
    python sluggishness_detector.py "t mann" --sandbox=prod --fail-on-missing
    python sluggishness_detector.py "t mann" --sandbox=prod --audience="AUD_...V2"

Cron sketch (Windows Task Scheduler, 08:00 daily, run AS you / logged-on so
keyring can read the vault):
    python sluggishness_detector.py "t mann" --sandbox=prod \
        --json=output/counts_latest.json --fail-on-missing
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aep_creds  # keyring-backed credential store (replaces creds/*.json)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_NAME = "sluggishness_detector"
SCRIPT_VERSION = "2.0.0"          # 2.x: pivoted to the overnight-manifest count board
SCRIPT_DATE = "2026-07-29"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
AUDIENCES_URL = "https://platform.adobe.io/data/core/ups/audiences"
SEGMENT_JOBS_URL = "https://platform.adobe.io/data/core/ups/segment/jobs"
SEGMENT_DEFS_URL = "https://platform.adobe.io/data/core/ups/segment/definitions"

DEFAULT_SANDBOX = "dev"
DEFAULT_WINDOW_DAYS = 3   # "built recently" -- covers a Fri build seen on Mon
# A scheduler job in one of these ran the estate evaluation to completion; its
# metrics.segmentedProfileCounter is the authoritative per-audience count source.
COMPLETED_STATUSES = {"SUCCEEDED", "PROCESSED"}
PAGE_LIMIT = 100
JOBS_PAGE_LIMIT = 100
MAX_JOB_PAGES = 500      # backstop so a runaway cursor can't loop forever

DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

# Classification of each recently-built batch audience against the overnight run.
STATUS_READY = "READY"        # counted in the overnight run -> here's the number
STATUS_MISSING = "MISSING"    # built before 04:00 but NOT counted -> real exception
STATUS_TOO_NEW = "TOO-NEW"    # built after the run kicked off -> next cycle (benign)
STATUS_UNKNOWN = "UNKNOWN"    # can't tell (no created time / no completed run)

# ----------------------------------------------------------------------------
# ANSI / logging
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


# Logging to STDERR so --json on stdout stays clean/parseable.
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(ColoredFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(SCRIPT_NAME)
SSL_CTX = ssl._create_unverified_context()


# ----------------------------------------------------------------------------
# HTTP / IMS / credential helpers
# ----------------------------------------------------------------------------
def http(url, method="GET", headers=None, data=None, timeout=60):
    """Stdlib HTTP. Returns response bytes; raises HTTPError on 4xx/5xx."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
        return r.read()


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


def menu(services):
    """Prompt for ONE credential set. Returns the chosen service name (or None).
    Prints to stderr so a piped stdout stays clean."""
    e = sys.stderr
    bar = ANSI["cyan"] + "=" * 70 + ANSI["reset"]
    print("", file=e)
    print(bar, file=e)
    print(f"  {ANSI['bold']}Credential bank{ANSI['reset']}  "
          f"{ANSI['dim']}(OS keyring vault){ANSI['reset']}", file=e)
    print(ANSI["cyan"] + "-" * 70 + ANSI["reset"], file=e)
    for i, name in enumerate(services, 1):
        print(f"  {ANSI['bold']}{i:>2}{ANSI['reset']}  "
              f"{ANSI['yellow']}{name:<24}{ANSI['reset']}", file=e)
    print(bar, file=e)
    try:
        raw = input(f"\nPick a credential set by number "
                    f"({ANSI['cyan']}1{ANSI['reset']}), blank to quit: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(services):
        return services[int(raw) - 1]
    logger.warning(f"Invalid choice: {raw}")
    return None


# ----------------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------------
# BST = UTC+1 (UK local; DST not modelled -- close enough for a morning check).
_BST = timezone(timedelta(hours=1))


def to_dt(value) -> datetime | None:
    """Parse the timestamp shapes AEP uses into an aware UTC datetime: epoch ms,
    epoch seconds, or ISO-8601. None if unparsable."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        if n > 1e12:        # milliseconds
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
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


def fmt_utc_bst(dt: datetime | None) -> str:
    """'YYYY-MM-DD HH:MM UTC / HH:MM BST' -- stakeholders think in local time."""
    if not dt:
        return "?"
    return (f"{dt.strftime('%Y-%m-%d %H:%M')} UTC / "
            f"{dt.astimezone(_BST).strftime('%H:%M')} BST")


def human_ago(dt: datetime | None, now: datetime) -> str:
    """Coarse '18h ago' / 'in 5m' -- a gut-feel signal, not a stopwatch."""
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


def iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


# ----------------------------------------------------------------------------
# Audience field extraction
# ----------------------------------------------------------------------------
def evaluation_method(aud: dict) -> str:
    """Normalise AEP's three eval modes. Only 'batch' waits on the nightly run."""
    ev = aud.get("evaluationInfo") or {}
    if (ev.get("batch") or {}).get("enabled"):
        return "batch"
    if (ev.get("continuous") or {}).get("enabled"):
        return "streaming"
    if (ev.get("synchronous") or {}).get("enabled"):
        return "edge"
    return "?"


def audience_created(aud: dict) -> datetime | None:
    for key in ("createEpoch", "creationTime", "createdAt", "created"):
        dt = to_dt(aud.get(key))
        if dt:
            return dt
    return None


def dig_count(obj) -> int | None:
    """Fallback count from the audience object's own metrics (the LATE telemetry
    value) -- used only for feeder display; the board's numbers come from the
    overnight manifest, not this."""
    if not isinstance(obj, dict):
        return None
    for k in ("profileCount", "totalProfiles", "profiles", "count", "totalRows"):
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


# ----------------------------------------------------------------------------
# Fetch: audiences, one audience's detail, jobs + the overnight manifest, feeders
# ----------------------------------------------------------------------------
def fetch_audiences(headers) -> list[dict]:
    """Page through every audience in the sandbox."""
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
        nxt = (data.get("_page") or {}).get("next")
        if not batch or len(batch) < PAGE_LIMIT or not nxt:
            break
        start = nxt
    logger.info(f"Listed {len(out)} audience(s) across {page} page(s).")
    return out


def fetch_audience_detail(headers, aid) -> dict:
    """GET one audience's full object -- for dependencies (SoS flag) and feeders."""
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


def fetch_segment_jobs(headers, max_jobs=None) -> list[dict]:
    """Page through segment (batch) jobs so we can find the last completed
    scheduler run. Pages everything by default (the newest scheduler job must be
    in the set)."""
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
        if max_jobs and len(out) >= max_jobs:
            out = out[:max_jobs]
            break
        nxt = (data.get("_page") or {}).get("next")
        if not batch or len(batch) < JOBS_PAGE_LIMIT or not nxt:
            break
        start = nxt
    return out


def fetch_full_job(headers, job_id) -> dict:
    """GET one segment job's FULL payload -- the list entry omits the
    metrics.segmentedProfileCounter manifest we need."""
    if not job_id:
        return {}
    try:
        body = http(f"{SEGMENT_JOBS_URL}/{urllib.parse.quote(str(job_id), safe='')}",
                    headers=headers, timeout=90)
        return json.loads(body) or {}
    except urllib.error.HTTPError as e:
        logger.error(f"Job fetch failed: HTTP {e.code} "
                     f"{e.read().decode(errors='replace')[:200]}")
        return {}
    except Exception as e:
        logger.error(f"Job fetch failed: {type(e).__name__}: {e}")
        return {}


def job_times(job: dict) -> tuple[datetime | None, datetime | None]:
    """(started, ended) across the field names AEP has used."""
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


def overnight_run(headers, jobs) -> tuple[datetime | None, datetime | None, dict, dict]:
    """Find the most recent COMPLETED scheduled estate run and pull its per-
    audience count manifest. Returns (started, ended, job, manifest) where
    manifest maps audience_id -> exact profile count computed by that run.

    This manifest (metrics.segmentedProfileCounter) is the whole point of the
    tool: it holds the real counts ~07:00, hours before the UI telemetry shows
    them at ~10:14. (started ~04:00 is also the 'built before 4am' due cutoff.)"""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    sched = [j for j in jobs if j.get("source") == "scheduler"
             and (j.get("status") or "").upper() in COMPLETED_STATUSES]
    if not sched:
        return None, None, {}, {}
    sched.sort(key=lambda j: job_times(j)[1] or floor, reverse=True)
    job = sched[0]
    started, ended = job_times(job)
    full = fetch_full_job(headers, job.get("id"))
    raw = (full.get("metrics") or {}).get("segmentedProfileCounter")
    manifest: dict[str, int | None] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            manifest[str(k)] = v if isinstance(v, int) else None
    return started, ended, job, manifest


def fetch_definition(headers, sid) -> dict:
    if not sid:
        return {}
    try:
        body = http(f"{SEGMENT_DEFS_URL}/{urllib.parse.quote(str(sid), safe='')}",
                    headers=headers, timeout=30)
        return json.loads(body) or {}
    except Exception:
        return {}


def resolve_feeders(headers, aud: dict, manifest: dict) -> list[dict]:
    """An audience's `dependencies` are the feeder segments it's built on. For a
    MISSING audience (no overnight count), a feeder that itself has no count in
    the overnight run is the likely reason -- a dependent can't be counted until
    its feeders are. Each: name, method, and its overnight count (from manifest)."""
    deps = aud.get("dependencies") or []
    out: list[dict] = []
    for did in deps:
        d = fetch_definition(headers, did)
        out.append({
            "id": str(did),
            "name": d.get("name") if d else None,
            "method": evaluation_method(d) if d else "?",
            "count": manifest.get(str(did)),
            "in_run": str(did) in manifest,
        })
    return out


# ----------------------------------------------------------------------------
# Classify one audience against the overnight run's count manifest
# ----------------------------------------------------------------------------
def classify(created: datetime | None, in_manifest: bool,
             overnight_start: datetime | None) -> tuple[str, str]:
    """READY if the audience has a count in the overnight run; else TOO-NEW if it
    was built after the run kicked off; else MISSING (was due, but the run didn't
    count it -> not evaluated overnight, no number exists yet); UNKNOWN if we
    can't judge."""
    if in_manifest:
        return STATUS_READY, "counted in the overnight run"
    if created is None or overnight_start is None:
        return STATUS_UNKNOWN, "no creation time or no completed overnight run to compare against"
    if created > overnight_start:
        return (STATUS_TOO_NEW,
                "built after the 04:00 run kicked off -- counts on the next cycle")
    return (STATUS_MISSING,
            "built before the 04:00 run but NOT in its counts -- wasn't evaluated "
            "overnight, so no count exists yet")


def assess_audience(headers, aud: dict, manifest: dict,
                    overnight_start: datetime | None, now: datetime) -> dict:
    """Look one audience's exact count up in the overnight manifest and classify
    it. Count comes from the manifest (the 07:00 truth), NOT the audience's own
    late telemetry. Detail is fetched for the SoS flag + feeders."""
    aid = str(aud.get("id") or aud.get("audienceId") or "")
    detail = fetch_audience_detail(headers, aid)
    merged = dict(aud)
    merged.update({k: v for k, v in (detail or {}).items() if v is not None})

    created = audience_created(merged)
    deps = merged.get("dependencies") or []
    is_sos = bool(deps)
    in_manifest = aid in manifest
    count = manifest.get(aid)
    status, reason = classify(created, in_manifest, overnight_start)

    # Feeders only matter for the MISSING case (why wasn't it counted?).
    feeders = resolve_feeders(headers, merged, manifest) if status == STATUS_MISSING else []

    return {
        "id": aid,
        "name": merged.get("name") or "(unnamed)",
        "method": evaluation_method(merged),
        "created": created,
        "count": count,
        "status": status,
        "reason": reason,
        "is_sos": is_sos,
        "feeder_count": len(deps),
        "feeders": feeders,
    }


# ----------------------------------------------------------------------------
# Count board -- the default, cron-facing mode
# ----------------------------------------------------------------------------
def run_board(headers, sandbox, days, json_target, fail_on_missing) -> int:
    """The 8am count board: for every batch audience built in the last `days`,
    show its exact count from the overnight run's manifest. Returns an exit code."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    audiences = fetch_audiences(headers)
    if not audiences:
        logger.warning("No audiences returned (empty sandbox, or no access).")
        return 1

    candidates = []
    for a in audiences:
        if evaluation_method(a) != "batch":
            continue
        created = audience_created(a)
        if created and created >= cutoff:
            candidates.append(a)
    candidates.sort(key=lambda a: audience_created(a) or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True)
    logger.info(f"{len(candidates)} batch audience(s) built in the last {days} day(s).")

    logger.info("Reading the overnight run's count manifest...")
    jobs = fetch_segment_jobs(headers, None)
    ov_start, ov_end, ov_job, manifest = overnight_run(headers, jobs)
    if manifest:
        logger.info(f"Overnight run {ov_job.get('id')} completed "
                    f"{fmt_utc_bst(ov_end)} -- {len(manifest)} audience count(s) available.")
    else:
        logger.warning("No overnight-run manifest found -- can't source counts; "
                       "every candidate will be UNKNOWN.")

    assessed = [assess_audience(headers, a, manifest, ov_start, now) for a in candidates]

    ready = [x for x in assessed if x["status"] == STATUS_READY]
    missing = [x for x in assessed if x["status"] == STATUS_MISSING]
    too_new = [x for x in assessed if x["status"] == STATUS_TOO_NEW]
    unknown = [x for x in assessed if x["status"] == STATUS_UNKNOWN]

    _print_board(sandbox, days, now, ov_start, ov_end, ov_job,
                 ready, missing, too_new, unknown)

    stamp = now.strftime("%Y%m%d_%H%M%S")
    csv_path = _write_csv(assessed, ov_start, ov_end, ov_job, sandbox, stamp)
    logger.info(f"Wrote {len(assessed)} audience(s) to {csv_path}")

    if json_target is not None:
        _emit_json(assessed, sandbox, days, now, ov_start, ov_end, ov_job,
                   json_target)

    if fail_on_missing and missing:
        return 2
    return 0


def _print_board(sandbox, days, now, ov_start, ov_end, ov_job,
                 ready, missing, too_new, unknown) -> None:
    C = ANSI
    bar = C["cyan"] + "=" * 96 + C["reset"]
    print()
    print(bar)
    print(f"  {C['bold']}Audience count board{C['reset']}  "
          f"{C['dim']}({sandbox}) -- yesterday's builds, their real counts, now"
          f"{C['reset']}")
    print(bar)
    print(f"  {C['bold']}Now{C['reset']}            {fmt_utc_bst(now)}")
    if ov_job:
        print(f"  {C['bold']}Source{C['reset']}         overnight run {ov_job.get('id')}")
        print(f"  {C['bold']}Completed{C['reset']}      {fmt_utc_bst(ov_end)}   "
              f"{C['dim']}({human_ago(ov_end, now)}){C['reset']}")
    else:
        print(f"  {C['bold']}Source{C['reset']}         {C['red']}no completed "
              f"overnight run found{C['reset']}")
    print(f"  {C['bold']}Window{C['reset']}         batch audiences built in the last "
          f"{days} day(s)")

    # THE BOARD: every recently-built audience with a count, newest build first.
    print(C["cyan"] + "-" * 96 + C["reset"])
    if ready:
        print(f"  {C['green']}{C['bold']}COUNTS READY -- exact, from the overnight run "
              f"({len(ready)}){C['reset']}")
        print(C["dim"] +
              f"    {'AUDIENCE':<50}{'BUILT':<12}{'COUNT':>16}" + C["reset"])
        for x in ready:
            _print_count_row(x, now, C)
    else:
        print(f"  {C['yellow']}No counted audiences in the window.{C['reset']}")

    # The genuine exceptions: built in time, but the overnight run didn't count it.
    if missing:
        print(C["cyan"] + "-" * 96 + C["reset"])
        print(f"  {C['red']}{C['bold']}NOT COUNTED -- built before 04:00 but missing "
              f"from the overnight run ({len(missing)}){C['reset']}")
        print(f"  {C['dim']}these genuinely weren't evaluated overnight -- no count "
              f"exists yet, anywhere{C['reset']}")
        for x in missing:
            _print_count_row(x, now, C, problem=True)
            print(f"      {C['dim']}{x['reason']}{C['reset']}")
            bad = [f for f in x["feeders"] if not f["in_run"]]
            if x["feeders"]:
                summ = ", ".join(f"{(f['name'] or f['id'][:8])}"
                                 f"{'!' if not f['in_run'] else ''}" for f in x["feeders"])
                print(f"      {C['dim']}feeders: {summ}{C['reset']}")
            if bad:
                print(f"      {C['red']}^ {len(bad)} feeder(s) also uncounted -- "
                      f"likely why this one couldn't be counted{C['reset']}")

    if too_new:
        print(C["cyan"] + "-" * 96 + C["reset"])
        print(f"  {C['dim']}TOO-NEW -- built after the 04:00 run; counts next cycle "
              f"({len(too_new)}){C['reset']}")
        for x in too_new:
            _print_count_row(x, now, C)

    if unknown:
        print(C["cyan"] + "-" * 96 + C["reset"])
        print(f"  {C['yellow']}UNKNOWN ({len(unknown)}){C['reset']}")
        for x in unknown:
            _print_count_row(x, now, C)
    print(bar)


def _print_count_row(x, now, C, problem=False) -> None:
    name = x["name"][:48]
    built = human_ago(x["created"], now) if x["created"] else "?"
    if isinstance(x["count"], int):
        cnt = f"{x['count']:,}"
        cntc = C["bold"] + C["green"]
    else:
        cnt = "-- none --" if problem else "?"
        cntc = C["red"] if problem else C["dim"]
    namec = C["red"] if problem else C["yellow"]
    sos = f" {C['magenta']}[SoS]{C['reset']}" if x["is_sos"] else ""
    print(f"    {namec}{name:<48}{C['reset']}{sos} {C['dim']}{built:<10}{C['reset']} "
          f"{cntc}{cnt:>16}{C['reset']}")


def _write_csv(assessed, ov_start, ov_end, ov_job, sandbox, stamp) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"count_board_{sandbox}_{stamp}.csv"
    cols = ["status", "name", "id", "method", "is_sos", "feeder_count",
            "built_utc", "built_bst", "count",
            "overnight_job_id", "overnight_completed_utc", "reason"]
    ov_id = (ov_job or {}).get("id") or ""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for x in assessed:
            created = x["created"]
            w.writerow([
                x["status"], x["name"], x["id"], x["method"],
                "yes" if x["is_sos"] else "", x["feeder_count"],
                iso(created),
                created.astimezone(_BST).isoformat() if created else "",
                x["count"] if isinstance(x["count"], int) else "",
                ov_id, iso(ov_end), x["reason"],
            ])
    return path


def _emit_json(assessed, sandbox, days, now, ov_start, ov_end, ov_job,
               json_target) -> None:
    """Machine-readable result for a downstream notifier (email/Slack/app/
    dashboard). `json_target` is a path, or '-' for stdout."""
    def one(x):
        return {
            "status": x["status"],
            "id": x["id"],
            "name": x["name"],
            "method": x["method"],
            "is_sos": x["is_sos"],
            "feeder_count": x["feeder_count"],
            "built_utc": iso(x["created"]),
            "count": x["count"],
            "reason": x["reason"],
        }

    by = lambda s: [one(x) for x in assessed if x["status"] == s]
    payload = {
        "generated_utc": iso(now),
        "sandbox": sandbox,
        "window_days": days,
        "overnight_run_completed_utc": iso(ov_end),
        "overnight_run_job_id": (ov_job or {}).get("id") if ov_job else None,
        "counts": {
            "assessed": len(assessed),
            "ready": sum(1 for x in assessed if x["status"] == STATUS_READY),
            "missing": sum(1 for x in assessed if x["status"] == STATUS_MISSING),
            "too_new": sum(1 for x in assessed if x["status"] == STATUS_TOO_NEW),
            "unknown": sum(1 for x in assessed if x["status"] == STATUS_UNKNOWN),
        },
        "ready": by(STATUS_READY),
        "missing": by(STATUS_MISSING),
        "assessed": [one(x) for x in assessed],
    }
    text = json.dumps(payload, indent=2)
    if json_target == "-":
        print(text)
        logger.info("Emitted JSON result to stdout.")
    else:
        p = Path(json_target)
        if not p.is_absolute():
            p = SCRIPT_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        logger.info(f"Wrote JSON result to {p}")


# ----------------------------------------------------------------------------
# Single-audience lookup -- one audience's count from the overnight run
# ----------------------------------------------------------------------------
def _looks_like_id(s: str) -> bool:
    s = (s or "").strip()
    return len(s) >= 16 and all(c in "0123456789abcdefABCDEF-" for c in s)


def resolve_target(audiences, selector, headers) -> dict | None:
    """Find the one audience to look up: exact id (list or direct GET), then
    unique name/id substring. No interactive picker -- keep it scriptable."""
    sel = (selector or "").strip()
    if not sel:
        return None
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
    logger.error(f"{len(matches)} audiences match {selector!r}; be more specific "
                 f"(names: {', '.join((m.get('name') or '?') for m in matches[:6])}"
                 f"{' ...' if len(matches) > 6 else ''}).")
    return None


def run_lookup(headers, sandbox, selector) -> int:
    audiences = fetch_audiences(headers)
    target = resolve_target(audiences, selector, headers)
    if not target:
        return 1
    now = datetime.now(timezone.utc)
    logger.info("Reading the overnight run's count manifest...")
    jobs = fetch_segment_jobs(headers, None)
    ov_start, ov_end, ov_job, manifest = overnight_run(headers, jobs)
    x = assess_audience(headers, target, manifest, ov_start, now)

    C = ANSI
    bar = C["cyan"] + "=" * 78 + C["reset"]
    scolor = {STATUS_READY: C["green"], STATUS_MISSING: C["red"],
              STATUS_TOO_NEW: C["dim"], STATUS_UNKNOWN: C["yellow"]}.get(x["status"], "")

    def row(k, v):
        print(f"  {C['bold']}{k:<16}{C['reset']}{v}")

    print()
    print(bar)
    print(f"  {C['bold']}Audience count{C['reset']}  "
          f"{C['dim']}(from the overnight run, before the UI shows it){C['reset']}")
    print(bar)
    row("Name", f"{C['yellow']}{x['name']}{C['reset']}")
    row("ID", f"{C['dim']}{x['id']}{C['reset']}")
    row("Evaluation", x["method"])
    if x["is_sos"]:
        row("Type", f"{C['magenta']}segment-of-segments{C['reset']} "
            f"{C['dim']}({x['feeder_count']} feeder(s)){C['reset']}")
    c_ago = f"   {C['cyan']}<- {human_ago(x['created'], now)}{C['reset']}" if x["created"] else ""
    row("Built", f"{fmt_utc_bst(x['created'])}{c_ago}")
    row("Overnight run", (f"{ov_job.get('id')}  completed {fmt_utc_bst(ov_end)}"
                          if ov_job else f"{C['red']}none found{C['reset']}"))
    if isinstance(x["count"], int):
        row("COUNT", f"{C['bold']}{C['green']}{x['count']:,}{C['reset']}   "
            f"{C['dim']}(exact -- the UI will show this at ~10:14){C['reset']}")
    else:
        row("COUNT", f"{C['red']}-- not available --{C['reset']}")
    if x["feeders"]:
        print(C["cyan"] + "-" * 78 + C["reset"])
        print(f"  {C['bold']}Feeders ({len(x['feeders'])}){C['reset']}")
        for f in x["feeders"]:
            name = (f["name"] or f"({f['id'][:8]}..)")[:40]
            cnt = f"{f['count']:,}" if isinstance(f["count"], int) else "-- none --"
            mark = (f"{C['green']}counted{C['reset']}" if f["in_run"]
                    else f"{C['red']}NOT counted{C['reset']}")
            print(f"     {C['yellow']}{name:<40}{C['reset']} {C['dim']}{f['method']:<9}"
                  f"{C['reset']}{cnt:>14}  {mark}")
    print(C["cyan"] + "-" * 78 + C["reset"])
    print(f"  {C['bold']}{'Verdict':<16}{C['reset']}{scolor}{C['bold']}{x['status']}{C['reset']}")
    print(f"  {'':<16}{x['reason']}")
    print(bar)
    return 2 if x["status"] == STATUS_MISSING else 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv):
    opts = {"sandbox": None, "name": None, "days": DEFAULT_WINDOW_DAYS,
            "audience_sel": None, "json_target": None, "fail_on_missing": False}
    for a in argv:
        if a.startswith("--sandbox="):
            opts["sandbox"] = a.split("=", 1)[1].strip() or None
        elif a.startswith("--days="):
            val = a.split("=", 1)[1].strip()
            try:
                opts["days"] = max(1, int(val))
            except ValueError:
                logger.warning(f"Bad --days {val!r}; keeping {opts['days']}.")
        elif a == "--json":
            opts["json_target"] = "-"          # stdout
        elif a.startswith("--json="):
            opts["json_target"] = a.split("=", 1)[1].strip() or "-"
        elif a in ("--fail-on-missing", "--fail-on-sluggish", "--fail"):
            opts["fail_on_missing"] = True
        elif a.startswith("--audience=") or a.startswith("--id="):
            opts["audience_sel"] = a.split("=", 1)[1].strip() or None
        elif a.startswith("-"):
            continue
        else:
            opts["name"] = a  # keyring service name
    return opts


def banner(conf, sandbox):
    e = sys.stderr
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar, file=e)
    print(f"  {ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})", file=e)
    print(f"  by {SCRIPT_AUTHOR}", file=e)
    print(f"  {ANSI['dim']}8am audience count board -- yesterday's builds and their "
          f"real counts, from the overnight run (read-only){ANSI['reset']}", file=e)
    print(f"  {ANSI['bold']}Org:{ANSI['reset']}      {ANSI['magenta']}{conf['org_id']}{ANSI['reset']}", file=e)
    print(f"  {ANSI['bold']}Sandbox:{ANSI['reset']}  {ANSI['yellow']}{sandbox}{ANSI['reset']}", file=e)
    print(bar, file=e)


def main() -> int:
    opts = parse_args(sys.argv[1:])

    print(aep_creds.source_banner(), file=sys.stderr)
    services = aep_creds.list_services()
    if not services:
        logger.error("No credentials found in the keyring vault or the creds/ "
                     "folder. Add one with credential_validator_v2.py store.")
        return 1

    if opts["name"]:
        try:
            chosen = aep_creds.pick_service(opts["name"])
        except aep_creds.CredsError as e:
            logger.error(str(e))
            return 1
    elif not sys.stdin.isatty():
        logger.error("No credential service given and no TTY to prompt. Pass the "
                     f"service name as the first argument. Options: {', '.join(services)}")
        return 1
    else:
        chosen = menu(services)
    if not chosen:
        logger.info("Nothing chosen. Exiting.")
        return 1

    try:
        conf = aep_creds.load_creds(chosen)
    except aep_creds.CredsError as e:
        logger.error(f"Failed to load credentials for {chosen!r}: {e}")
        return 1

    sandbox = opts["sandbox"] or conf.get("sandbox") or DEFAULT_SANDBOX
    if sandbox == "all":
        sandbox = DEFAULT_SANDBOX
    banner(conf, sandbox)

    try:
        token = authenticate(conf)
    except urllib.error.HTTPError as e:
        logger.error(f"IMS auth FAILED: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
        return 1
    except Exception as e:
        logger.error(f"IMS auth FAILED: {type(e).__name__}: {e}")
        return 1
    logger.info("IMS authenticated.")
    headers = aep_headers(token, conf, sandbox)

    if opts["audience_sel"]:
        rc = run_lookup(headers, sandbox, opts["audience_sel"])
    else:
        rc = run_board(headers, sandbox, opts["days"], opts["json_target"],
                       opts["fail_on_missing"])
    logger.info("Done.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
