#!/usr/bin/env python3
"""
estimate_prober.py  --  AEP audience-estimate health probe
==========================================================
Watch the two things that decide whether an audience *estimate* in Adobe
Experience Platform can be trusted, and keep a durable history of both, so a
degradation is something you detect rather than something the audience team
reports to you days later.

What it watches
---------------
1. THE SAMPLE JOB.  Estimates do not run against the full Profile store -- they
   run against a store-wide *sample* that AEP refreshes on its own schedule. If
   that sample goes stale the estimates it feeds go misleading, or zero. Adobe
   has confirmed to us that this job held stale data and needed a manual re-run;
   we had no visibility of it ourselves. "sample-status" reads the sample's own
   status endpoint, ages it, and fails a threshold.

2. THE ESTIMATE JOB.  We have watched estimate jobs sit at state PROCESSING with
   profilesReadSoFar = 0 and an empty error object, indefinitely, for a segment
   definition of one event with a short lookback. Adobe documents estimates as
   completing in 10-15 seconds. "probe" fires exactly that canary, polls it, and
   classifies the outcome -- so "never started" is told apart from "ran fine and
   legitimately matched nobody".

Outcomes (probe)
----------------
  COMPLETED_WITH_RESULT  RESULT_READY with a non-zero estimate.          exit 0
  COMPLETED_EMPTY        RESULT_READY, rows read, none matched. Legitimate
                         sample starvation, not a fault.                 exit 0
  NEVER_STARTED          Timed out at profilesReadSoFar = 0 with no error
                         -- THE fault signature this tool exists for.    exit 2
  STALLED                Timed out having read rows but not finished.    exit 3
  ERRORED                Error object populated, or the service answered
                         4xx / a body we could not parse.                exit 4

"sample-status" exits 0 when the sample is fresher than --max-age-hours
(default 96) and 2 when it is stale (or when its age cannot be established --
failing safe). Both subcommands exit 1 on a credential or transport failure and
4 on a response that could not be parsed.

Read-only
---------
Creating a preview job is inherent to probing an estimate: the estimate job is
triggered by the preview job and shares its id. Beyond that this tool writes
nothing to the platform -- no audience is created, published or modified, and no
segment definition is saved.

Credentials come from the OS keyring (Windows Credential Manager) via the shared
aep_creds layer; the service name defaults to "aep-prod". Secrets are never
accepted as CLI arguments, never written to disk, and never logged. Manage them
with credential_validator_v2.py.

How long has it been stuck?
---------------------------
A single run can only report its own wait, and that is capped at --timeout: a
service stuck for six hours still reads "180s", because every probe creates a
fresh preview job and none of them can see further back than itself. So each run
reads the history file back first, counts the unbroken run of failures for this
sandbox and subcommand, and reports the answer you actually want:

    STUCK FOR       : 6.2h across 41 consecutive runs, since 2026-08-21T06:00:00Z

That is service-level, not job-level -- how long estimates have been failing
here, not how long any one job has hung. It is only as granular as your schedule
(hourly runs give hourly resolution), and --no-history turns it off along with
the file. The first healthy run after a bad streak reports RECOVERED.

Output
------
--history <path>  CSV, appended, one row per run. Default:
                  output/estimate_probe_history.csv NEXT TO THE SCRIPT -- not
                  the working directory, because a scheduled task rarely runs
                  where you think it does. Each row holds an ISO 8601 UTC
                  timestamp, sandbox, subcommand, outcome, how long it has been
                  failing, and every numeric field observed.
--json            the full run record to stdout as JSON, for piping. Logging and
                  the human summary go to stderr, so stdout stays clean.
--verbose         print every poll as it happens.

No credential, org id or profile data reaches the history file or the log. The
previewId is masked in the log and kept out of the history entirely -- it is a
base64 wrapper around an org-scoped application id.

Naked run
---------
With no arguments it goes interactive, the way the rest of the toolkit does:
pick a credential set, pick a sandbox from the live list, and it checks the
sample immediately (free and instant), then offers the estimate probe (which can
take up to --timeout). Both results land in the default history, so the naked
runs and the scheduled ones build one continuous picture. No TTY and no
subcommand is a fast, loud failure -- it never sits there waiting on a prompt
nobody can answer.

Usage:
    python estimate_prober.py                    # naked: pick, then check both
    python estimate_prober.py sample-status
    python estimate_prober.py sample-status --sandbox=prod --max-age-hours=48
    python estimate_prober.py sample-status --sandbox=prod --report=dataset
    python estimate_prober.py probe --sandbox=prod --verbose
    python estimate_prober.py probe --sandbox=prod --timeout=300 --interval=10
    python estimate_prober.py probe --pql="select var1 from xEvent where var1.timestamp occurs <= 1 days before now"
    python estimate_prober.py probe --sandbox=prod --json | jq .outcome

Scheduler sketch (Windows Task Scheduler / cron, hourly, run as the logged-on
user so keyring can read the vault). Both runs share one history file, so the
"stuck for" line spans the whole schedule; a non-zero exit is the alert:
    python estimate_prober.py sample-status --service=aep-prod --sandbox=prod
    python estimate_prober.py probe --service=aep-prod --sandbox=prod
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Third-party and shared-repo imports are guarded so the pure-logic half of this
# module -- the response parsers and the outcome classifier -- imports cleanly
# under the unit tests on a box without requests/keyring installed.
try:
    import requests
    _DEPS_OK = True
    _DEPS_ERR = ""
except ImportError as _e:              # pragma: no cover - environment-dependent
    requests = None                    # type: ignore[assignment]
    _DEPS_OK = False
    _DEPS_ERR = str(_e)

try:
    import aep_creds                   # keyring-backed credential store
    _CREDS_OK = True
    _CREDS_ERR = ""
except ImportError as _e:              # pragma: no cover - environment-dependent
    aep_creds = None                   # type: ignore[assignment]
    _CREDS_OK = False
    _CREDS_ERR = str(_e)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_NAME    = "estimate_prober"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE    = "2026-08-20"
SCRIPT_AUTHOR  = "Barry Mann (barrymann.com)"

IMS_TOKEN_V3_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
PLATFORM         = "https://platform.adobe.io"
UPS_BASE         = f"{PLATFORM}/data/core/ups"

# Real-Time Customer Profile API -- status of the store-wide preview sample.
SAMPLE_STATUS_URL = f"{UPS_BASE}/previewsamplestatus"
SAMPLE_REPORT_URL = f"{SAMPLE_STATUS_URL}/report"        # /dataset | /namespace

# Segmentation Service API. A preview job triggers the estimate job, and the two
# share the previewId for lookup.
PREVIEW_URL  = f"{UPS_BASE}/preview"
ESTIMATE_URL = f"{UPS_BASE}/estimate"                    # /{previewId}

# Only used to populate the interactive sandbox menu on a naked run.
SANDBOX_LIST_URL = f"{PLATFORM}/data/foundation/sandbox-management/sandboxes"

DEFAULT_SERVICE = "aep-prod"
DEFAULT_SANDBOX = "prod"
DEFAULT_SCOPES  = ("openid,AdobeID,read_organizations,"
                   "additional_info.projectedProductContext,session")

# The canary: one experience event with a short lookback. Deliberately the
# simplest question the segmentation engine can be asked -- if THIS never
# starts, the service is degraded, not the segment.
#
# The event MUST be bound with `select var1 from xEvent where ...`. The bare
# form `xEvent.timestamp occurs <= 7 days before now` is rejected 400: xEvent
# .timestamp is array-typed (DATE-TIME[]) and `occurs` takes a scalar, so the
# service answers "The 'occurs' operator requires a scalar timestamp field".
# Verified against prod, 2026-08-21.
DEFAULT_PQL     = ("select var1 from xEvent "
                   "where var1.timestamp occurs <= 7 days before now")
PREDICATE_TYPE  = "pql/text"
PREDICATE_MODEL = "_xdm.context.profile"
GRAPH_TYPE      = "none"

DEFAULT_TIMEOUT_S     = 180.0   # Adobe documents 10-15s; 180 is generous
DEFAULT_INTERVAL_S    = 5.0
DEFAULT_MAX_AGE_HOURS = 96.0

# The history lives next to the script, in the repo's usual output/ folder --
# NOT relative to the working directory. A scheduled task often runs with a CWD
# you did not choose (C:\Windows\System32 for Task Scheduler), and a history
# that lands somewhere different on every invocation cannot answer "how long has
# this been stuck?". --history overrides it; a relative path there is resolved
# against the working directory as you would expect.
SCRIPT_DIR      = Path(__file__).resolve().parent
OUTPUT_DIR      = SCRIPT_DIR / "output"
DEFAULT_HISTORY = OUTPUT_DIR / "estimate_probe_history.csv"

HTTP_TIMEOUT_S = 30
HTTP_ATTEMPTS  = 3      # 1 try + 2 retries, on 5xx / transport failures only
HTTP_BACKOFF_S = 2.0    # 2s, 4s, 8s ...

# Documented preview/estimate states are NEW, RUNNING, RESULT_READY and FAILED;
# PROCESSING and ACCEPTED turn up in the wild. Only RESULT_READY is terminal
# success -- the substring hints are a defensive net for anything else the
# service decides to call a failure.
STATE_READY         = "RESULT_READY"
FAILURE_STATE_HINTS = ("FAIL", "ERROR", "CANCEL", "ABORT", "TIMEOUT", "TIMED_OUT")

# Probe outcomes. Exactly one is assigned per run.
OUTCOME_WITH_RESULT   = "COMPLETED_WITH_RESULT"
OUTCOME_EMPTY         = "COMPLETED_EMPTY"
OUTCOME_NEVER_STARTED = "NEVER_STARTED"
OUTCOME_STALLED       = "STALLED"
OUTCOME_ERRORED       = "ERRORED"

OUTCOME_EXIT = {
    OUTCOME_WITH_RESULT:   0,
    OUTCOME_EMPTY:         0,
    OUTCOME_NEVER_STARTED: 2,
    OUTCOME_STALLED:       3,
    OUTCOME_ERRORED:       4,
}

# sample-status outcomes.
SAMPLE_FRESH   = "FRESH"
SAMPLE_STALE   = "STALE"
SAMPLE_ERRORED = "ERRORED"

EXIT_OK      = 0
EXIT_FAILURE = 1    # credential / transport / usage
EXIT_STALE   = 2    # sample-status: sample older than the threshold
EXIT_ERRORED = 4    # response rejected or unparseable

MAX_ERROR_CHARS = 300   # keep a runaway traceback out of the CSV

# History CSV. One wide schema shared by both subcommands; columns the other
# subcommand does not fill stay empty, so a single file holds the whole history.
HISTORY_FIELDS = [
    "timestamp_utc", "sandbox", "command", "outcome", "exit_code", "note",
    # how long this has been going on, read back out of the history itself
    "consecutive_failures", "degraded_since_utc", "degraded_hours",
    # probe
    "elapsed_seconds", "polls", "final_state",
    "profiles_read", "profiles_matched", "num_rows_to_read", "total_rows",
    "estimated_size", "standard_error", "confidence_interval",
    "error_description", "error_traceback",
    # sample-status
    "sample_status", "sample_job_running", "sample_size", "total_profiles",
    "doc_count", "total_fragment_count", "sampling_ratio", "merge_strategy",
    "last_sampled_utc", "sample_age_hours", "last_successful_batch_utc",
]

LOG = logging.getLogger(SCRIPT_NAME)


class ProbeError(Exception):
    """A run cannot continue: no credential, no token, or no usable response."""


# ----------------------------------------------------------------------------
# Logging  (stderr only -- stdout is reserved for --json)
# ----------------------------------------------------------------------------
def setup_logging(verbose: bool) -> None:
    """Send log output to stderr so --json keeps stdout machine-clean."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOG.handlers.clear()
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.propagate = False


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def utc_now() -> datetime:
    """Timezone-aware 'now' in UTC."""
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None) -> str:
    """ISO 8601 UTC to the second, or '' for None."""
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unquote_value(raw: Any) -> str:
    """Normalise a previewsamplestatus value to a plain string.

    That endpoint double-encodes several fields -- docCount comes back as
    '"300803"' and an absent timestamp as '"null"' -- so surrounding quotes are
    stripped and the null spellings collapse to ''.
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if text.lower() in ("null", "none", "nan"):
        return ""
    return text


def as_int(raw: Any) -> int | None:
    """Best-effort int, tolerating the quoted-string encoding. None if absent."""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    text = unquote_value(raw)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def as_float(raw: Any) -> float | None:
    """Best-effort float, tolerating the quoted-string encoding."""
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, (int, float)):
        return float(raw)
    text = unquote_value(raw)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_adobe_timestamp(raw: Any) -> datetime | None:
    """Parse the timestamp spellings AEP uses here into an aware UTC datetime.

    Handles '2020-08-01 17:57:57.0' (previewsamplestatus), ISO 8601 with or
    without a Z/offset, and epoch seconds or milliseconds. Returns None for
    anything unrecognised -- an unparseable timestamp is reported, not raised.
    """
    text = unquote_value(raw)
    if not text:
        return None
    if text.isdigit():
        value = float(text)
        if value > 1e11:          # milliseconds
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    cleaned = text.replace("T", " ")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def mask_id(value: str, keep: int = 12) -> str:
    """Show only the head of an identifier.

    The previewId is base64 over an org-scoped application id, so the full value
    never reaches the log or the history file -- but enough of it survives to
    correlate two lines in one run.
    """
    if not value:
        return "-"
    if len(value) <= keep:
        return value
    return f"{value[:keep]}...({len(value)} chars)"


def fmt_int(value: int | None) -> str:
    """Thousands-separated int for the human summary, or '-' when absent."""
    return "-" if value is None else f"{value:,}"


def clip(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """Collapse whitespace and truncate -- CSV-safe error text."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


# ----------------------------------------------------------------------------
# HTTP -- every call in this tool goes through http_request()
# ----------------------------------------------------------------------------
@dataclass
class HttpResult:
    """One HTTP exchange: status, parsed body, and why it is unusable if it is."""

    status: int = 0
    body: dict | None = None
    text: str = ""
    parse_error: str = ""
    transport_error: str = ""
    elapsed: float = 0.0
    attempts: int = 0

    @property
    def ok(self) -> bool:
        """True when the call returned 2xx with a JSON object we could parse."""
        return (200 <= self.status < 300 and self.body is not None
                and not self.parse_error and not self.transport_error)

    @property
    def problem(self) -> str:
        """One-line reason the exchange is unusable ('' when it is fine)."""
        if self.transport_error:
            return self.transport_error
        if self.parse_error:
            return f"HTTP {self.status}: {self.parse_error}"
        if not 200 <= self.status < 300:
            detail = clip(self.text, 160)
            return f"HTTP {self.status}" + (f": {detail}" if detail else "")
        return ""


def http_request(method: str, url: str, headers: dict[str, str], *,
                 json_body: dict | None = None,
                 params: dict[str, str] | None = None,
                 timeout: int = HTTP_TIMEOUT_S,
                 attempts: int = HTTP_ATTEMPTS,
                 backoff: float = HTTP_BACKOFF_S,
                 label: str = "") -> HttpResult:
    """Make one API call, with retries on 5xx and transport failures only.

    4xx is never retried -- it is an answer, not a wobble. A missing or
    malformed body is captured in `parse_error` rather than raised, because an
    unparseable response is itself a finding worth logging. Headers are never
    logged: they carry the token.

    A retried POST /preview creates a second short-lived preview job; preview
    jobs are ephemeral and create no audience, so that is wasteful, not unsafe.
    """
    if not _DEPS_OK:                   # pragma: no cover - environment-dependent
        raise ProbeError(f"the 'requests' package is required: {_DEPS_ERR}")

    result = HttpResult()
    started = time.monotonic()
    tag = label or f"{method} {url}"

    for attempt in range(1, max(1, attempts) + 1):
        result.attempts = attempt
        try:
            response = requests.request(method, url, headers=headers,
                                        json=json_body, params=params,
                                        timeout=timeout)
        except Exception as ex:        # requests.RequestException + anything odd
            result.transport_error = f"{type(ex).__name__}: {ex}"
            result.status = 0
            if attempt < attempts:
                pause = backoff * (2 ** (attempt - 1))
                LOG.debug("  %s transport failure (%s) - retry %d/%d in %.0fs",
                          tag, result.transport_error, attempt, attempts - 1, pause)
                time.sleep(pause)
                continue
            break

        result.transport_error = ""
        result.status = response.status_code
        result.text = response.text or ""

        if 500 <= response.status_code < 600 and attempt < attempts:
            pause = backoff * (2 ** (attempt - 1))
            LOG.debug("  %s HTTP %d - retry %d/%d in %.0fs", tag,
                      response.status_code, attempt, attempts - 1, pause)
            time.sleep(pause)
            continue
        break

    result.elapsed = time.monotonic() - started

    if result.transport_error:
        return result

    stripped = result.text.strip()
    if not stripped:
        if result.status != 204:
            result.parse_error = "empty response body"
        return result
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError) as ex:
        result.parse_error = f"unparseable response body ({ex})"
        return result
    if isinstance(parsed, dict):
        result.body = parsed
    else:
        result.parse_error = (f"expected a JSON object, got "
                              f"{type(parsed).__name__}")
    return result


# ----------------------------------------------------------------------------
# Credentials + auth
# ----------------------------------------------------------------------------
def load_credentials(service: str) -> dict[str, str]:
    """Read a credential set out of the keyring vault via the shared layer.

    Same pattern as credential_validator_v2.py: keyring first, service name
    "aep-prod" by default. Raises ProbeError with actionable text when the set
    is missing or incomplete -- secrets are never accepted on the command line.
    """
    if not _CREDS_OK:                  # pragma: no cover - environment-dependent
        raise ProbeError(
            f"the shared aep_creds module could not be imported ({_CREDS_ERR}). "
            f"Run this from the repo root, with 'keyring' installed.")
    try:
        return aep_creds.load_creds(service)
    except Exception as ex:            # aep_creds.CredsError and friends
        raise ProbeError(
            f"no usable credential for service '{service}': {ex} "
            f"Store one with: credential_validator_v2.py store "
            f"--service {service}") from ex


def authenticate(conf: dict[str, str]) -> str:
    """Mint an IMS access token by client_credentials. Returns the token only.

    The token, the client secret and every other credential field stay out of
    the log; only the lifetime is reported.
    """
    if not _DEPS_OK:                   # pragma: no cover - environment-dependent
        raise ProbeError(f"the 'requests' package is required: {_DEPS_ERR}")

    url = conf.get("oauth_url") or IMS_TOKEN_V3_URL
    try:
        response = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": conf["client_id"],
                "client_secret": conf["client_secret"],
                "scope": conf.get("scopes") or DEFAULT_SCOPES,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=HTTP_TIMEOUT_S,
        )
    except Exception as ex:
        raise ProbeError(f"could not reach IMS to mint a token: "
                         f"{type(ex).__name__}: {ex}") from ex

    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = (f"{body.get('error', '')} "
                      f"{body.get('error_description', '') or body.get('message', '')}"
                      ).strip()
        except ValueError:
            detail = clip(response.text, 200)
        raise ProbeError(f"IMS rejected the credential (HTTP "
                         f"{response.status_code}): {detail or 'no detail'}. "
                         f"Check the client secret has not expired.")
    try:
        payload = response.json()
    except ValueError as ex:
        raise ProbeError(f"IMS returned an unparseable token response: {ex}") from ex

    token = payload.get("access_token")
    if not token:
        raise ProbeError("IMS returned no access_token.")
    LOG.debug("token minted, valid for %ss", payload.get("expires_in", "?"))
    return str(token)


def api_headers(token: str, conf: dict[str, str],
                sandbox: str = "") -> dict[str, str]:
    """The headers every Platform call in this tool must carry.

    x-sandbox-name is omitted only for the sandbox list itself, which is not
    scoped to one sandbox; every other call in this tool passes one.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if sandbox:
        headers["x-sandbox-name"] = sandbox
    return headers


# ----------------------------------------------------------------------------
# sample-status
# ----------------------------------------------------------------------------
@dataclass
class SampleStatus:
    """The parsed /previewsamplestatus response, with the sample's age worked out.

    Field meanings are Adobe's: numRowsToRead is the number of merged profiles
    *in the sample*, totalRows is the number of merged profiles in Platform
    (the profile count), and lastSampledTimestamp is when the sample job last
    succeeded.
    """

    status: str = ""
    job_running: bool | None = None
    job_submitted_utc: datetime | None = None
    sample_size: int | None = None            # numRowsToRead
    total_profiles: int | None = None         # totalRows
    doc_count: int | None = None
    total_fragment_count: int | None = None
    sampling_ratio: float | None = None
    merge_strategy: str = ""
    last_sampled_utc: datetime | None = None
    last_successful_batch_utc: datetime | None = None
    age_hours: float | None = None


def parse_sample_status(body: dict | None,
                        now: datetime | None = None) -> SampleStatus:
    """Turn a /previewsamplestatus body into a SampleStatus, ages included.

    Tolerates the endpoint's quirks: numbers arrive as strings, some values are
    double-quoted ('"300803"'), absent timestamps arrive as the string '"null"',
    and sampleJobRunning is documented as a boolean but observed as an object
    carrying {status, submissionTimestamp}.
    """
    status = SampleStatus()
    if not body:
        return status

    status.status = unquote_value(body.get("status"))
    status.sample_size = as_int(body.get("numRowsToRead"))
    status.total_profiles = as_int(body.get("totalRows"))
    # Documented as docCount; prod actually returns cosmosDocCount (and only
    # that one). Accept either rather than reporting a document count of "-".
    status.doc_count = as_int(body.get("docCount"))
    if status.doc_count is None:
        status.doc_count = as_int(body.get("cosmosDocCount"))
    status.total_fragment_count = as_int(body.get("totalFragmentCount"))
    status.sampling_ratio = as_float(body.get("samplingRatio"))
    status.merge_strategy = unquote_value(body.get("mergeStrategy"))
    status.last_sampled_utc = parse_adobe_timestamp(body.get("lastSampledTimestamp"))
    status.last_successful_batch_utc = parse_adobe_timestamp(
        body.get("lastSuccessfulBatchTimestamp"))

    running = body.get("sampleJobRunning")
    if isinstance(running, dict):
        flag = running.get("status")
        status.job_running = bool(flag) if flag is not None else None
        status.job_submitted_utc = parse_adobe_timestamp(
            running.get("submissionTimestamp"))
    elif running is not None:
        text = unquote_value(running).lower()
        status.job_running = text in ("true", "1", "yes") if text else None

    if status.last_sampled_utc is not None:
        reference = now or utc_now()
        delta = reference - status.last_sampled_utc
        status.age_hours = round(delta.total_seconds() / 3600.0, 2)
    return status


def classify_sample(status: SampleStatus, max_age_hours: float) -> tuple[str, str]:
    """Decide FRESH / STALE for a sample, with the reason.

    An unknown age fails safe as STALE: if the endpoint will not say when the
    sample last ran, we cannot assert that the estimates riding on it are sound.
    """
    if status.age_hours is None:
        return (SAMPLE_STALE,
                "no lastSampledTimestamp in the response - the sample's age "
                "cannot be established, failing safe")
    if status.age_hours > max_age_hours:
        return (SAMPLE_STALE,
                f"sample last ran {status.age_hours:.1f}h ago, over the "
                f"{max_age_hours:g}h threshold")
    return (SAMPLE_FRESH,
            f"sample last ran {status.age_hours:.1f}h ago, within the "
            f"{max_age_hours:g}h threshold")


def print_sample_summary(status: SampleStatus, sandbox: str,
                         outcome: str, note: str) -> None:
    """Print the human-readable sample summary to stderr."""
    ratio = ("-" if status.sampling_ratio is None
             else f"{status.sampling_ratio * 100:.2f}%")
    LOG.info("Sample status   sandbox=%s", sandbox)
    LOG.info("  last sample run : %s   (%s)",
             iso_utc(status.last_sampled_utc) or "unknown",
             "-" if status.age_hours is None else f"{status.age_hours:.1f}h ago")
    LOG.info("  sample size     : %s merged profiles (numRowsToRead)",
             fmt_int(status.sample_size))
    LOG.info("  total profiles  : %s  (sampling ratio %s)",
             fmt_int(status.total_profiles), ratio)
    LOG.info("  fragments       : %s   documents: %s",
             fmt_int(status.total_fragment_count), fmt_int(status.doc_count))
    LOG.info("  last batch in   : %s",
             iso_utc(status.last_successful_batch_utc) or "unknown")
    LOG.info("  sample status   : %s   job running: %s   merge: %s",
             status.status or "-",
             {True: "yes", False: "no", None: "-"}[status.job_running],
             status.merge_strategy or "-")
    line = f"  OUTCOME         : {outcome} - {note}"
    (LOG.info if outcome == SAMPLE_FRESH else LOG.warning)(line)


def print_sample_report(body: dict | None, kind: str) -> None:
    """Print a sample distribution report (by dataset or namespace) to stderr.

    Field names differ per report type and are not pinned by this tool, so the
    first list of records in the body is tabulated generically rather than
    against invented field names. Reports are informational: they are never
    written to the history file.
    """
    if not body:
        LOG.warning("  %s report: no body returned", kind)
        return
    rows: list[dict] = []
    for value in body.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            rows = value
            break
    LOG.info("  %s report (%d rows):", kind, len(rows) if rows else 0)
    if not rows:
        LOG.info("    %s", clip(json.dumps(body), 400))
        return
    for row in rows[:25]:
        LOG.info("    %s", clip(", ".join(f"{k}={v}" for k, v in row.items()), 200))
    if len(rows) > 25:
        LOG.info("    ... %d more rows", len(rows) - 25)


def run_sample_status(headers: dict[str, str], sandbox: str,
                      max_age_hours: float,
                      report: str = "", report_date: str = "") -> dict[str, Any]:
    """Fetch, classify and summarise the sample status. Returns the run record."""
    LOG.debug("GET %s", SAMPLE_STATUS_URL)
    result = http_request("GET", SAMPLE_STATUS_URL, headers, label="sample-status")

    record: dict[str, Any] = {
        "timestamp_utc": iso_utc(utc_now()),
        "sandbox": sandbox,
        "command": "sample-status",
        "http_status": result.status,
        "max_age_hours": max_age_hours,
    }

    if not result.ok:
        note = result.problem or "unusable response"
        record.update({"outcome": SAMPLE_ERRORED, "note": clip(note),
                       "exit_code": EXIT_ERRORED})
        LOG.error("Sample status   sandbox=%s", sandbox)
        LOG.error("  OUTCOME         : %s - %s", SAMPLE_ERRORED, note)
        return record

    status = parse_sample_status(result.body)
    outcome, note = classify_sample(status, max_age_hours)
    record.update({
        "outcome": outcome,
        "note": note,
        "exit_code": EXIT_OK if outcome == SAMPLE_FRESH else EXIT_STALE,
        "sample_status": status.status,
        "sample_job_running": status.job_running,
        "sample_size": status.sample_size,
        "total_profiles": status.total_profiles,
        "doc_count": status.doc_count,
        "total_fragment_count": status.total_fragment_count,
        "sampling_ratio": status.sampling_ratio,
        "merge_strategy": status.merge_strategy,
        "last_sampled_utc": iso_utc(status.last_sampled_utc),
        "sample_age_hours": status.age_hours,
        "last_successful_batch_utc": iso_utc(status.last_successful_batch_utc),
        "sample_job_submitted_utc": iso_utc(status.job_submitted_utc),
    })
    print_sample_summary(status, sandbox, outcome, note)

    if report:
        params = {"date": report_date} if report_date else None
        report_result = http_request("GET", f"{SAMPLE_REPORT_URL}/{report}",
                                     headers, params=params,
                                     label=f"sample-report/{report}")
        if report_result.ok:
            print_sample_report(report_result.body, report)
        else:
            LOG.warning("  %s report unavailable: %s", report,
                        report_result.problem)
    return record


# ----------------------------------------------------------------------------
# probe -- preview job, then poll the estimate
# ----------------------------------------------------------------------------
@dataclass
class Poll:
    """One observation of an estimate job."""

    elapsed: float = 0.0
    http_status: int = 0
    state: str = ""
    profiles_read: int | None = None          # profilesReadSoFar
    profiles_matched: int | None = None       # profilesMatchedSoFar
    num_rows_to_read: int | None = None
    total_rows: int | None = None
    estimated_size: int | None = None
    standard_error: float | None = None
    confidence_interval: str = ""
    error_description: str = ""
    error_traceback: str = ""
    parse_error: str = ""

    @property
    def has_error(self) -> bool:
        """True when the response's error object carries anything at all."""
        return bool(self.error_description or self.error_traceback)

    @property
    def state_failed(self) -> bool:
        """True when the reported state reads as a failure."""
        upper = (self.state or "").upper()
        return any(hint in upper for hint in FAILURE_STATE_HINTS)

    @property
    def is_ready(self) -> bool:
        """True at the one terminal success state, RESULT_READY."""
        return (self.state or "").upper() == STATE_READY

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view of this poll, for --json and --verbose."""
        return {
            "elapsed_seconds": round(self.elapsed, 2),
            "http_status": self.http_status,
            "state": self.state,
            "profilesReadSoFar": self.profiles_read,
            "profilesMatchedSoFar": self.profiles_matched,
            "numRowsToRead": self.num_rows_to_read,
            "totalRows": self.total_rows,
            "estimatedSize": self.estimated_size,
            "standardError": self.standard_error,
            "confidenceInterval": self.confidence_interval,
            "error": {"description": self.error_description,
                      "traceback": self.error_traceback},
            "parse_error": self.parse_error,
        }

    def summary_line(self) -> str:
        """The one-line form used by --verbose and the final summary."""
        return (f"t+{self.elapsed:6.1f}s  state={self.state or '-':<14} "
                f"read={fmt_int(self.profiles_read):>12} "
                f"matched={fmt_int(self.profiles_matched):>12} "
                f"toRead={fmt_int(self.num_rows_to_read):>12} "
                f"total={fmt_int(self.total_rows):>12} "
                f"est={fmt_int(self.estimated_size):>12} "
                f"stderr={'-' if self.standard_error is None else self.standard_error}"
                + (f"  error={clip(self.error_description, 120)}"
                   if self.has_error else "")
                + (f"  [{self.parse_error}]" if self.parse_error else ""))


def parse_estimate(body: dict | None, http_status: int = 200,
                   elapsed: float = 0.0, parse_error: str = "") -> Poll:
    """Turn a GET /estimate/{previewId} body into a Poll.

    Every field the classifier needs is read here, defensively: a missing key
    becomes None rather than an exception, and the error object is accepted as
    either the documented {description, traceback} object or a bare string.
    """
    poll = Poll(elapsed=elapsed, http_status=http_status, parse_error=parse_error)
    if not body:
        return poll

    poll.state = unquote_value(body.get("state"))
    poll.profiles_read = as_int(body.get("profilesReadSoFar"))
    poll.profiles_matched = as_int(body.get("profilesMatchedSoFar"))
    poll.num_rows_to_read = as_int(body.get("numRowsToRead"))
    poll.total_rows = as_int(body.get("totalRows"))
    poll.estimated_size = as_int(body.get("estimatedSize"))
    poll.standard_error = as_float(body.get("standardError"))
    poll.confidence_interval = unquote_value(body.get("confidenceInterval"))

    error = body.get("error")
    if isinstance(error, dict):
        poll.error_description = clip(unquote_value(error.get("description")))
        poll.error_traceback = clip(unquote_value(error.get("traceback")))
    elif isinstance(error, list):
        poll.error_description = clip("; ".join(str(item) for item in error))
    elif error is not None:
        poll.error_description = clip(unquote_value(error))
    return poll


def classify_outcome(last: Poll | None, timed_out: bool) -> tuple[str, str]:
    """Classify a probe into exactly one outcome, with the reason.

    Order matters. Anything the service reported as an error is ERRORED, however
    far it got. Otherwise RESULT_READY splits on whether anything matched, and a
    timeout splits on whether the job ever read a single profile -- zero reads
    with an empty error object being the signature of an estimate that never
    started at all.
    """
    if last is None:
        return (OUTCOME_ERRORED,
                "no estimate response was received at all")
    if last.parse_error:
        return (OUTCOME_ERRORED,
                f"the estimate response could not be read: {last.parse_error}")
    if last.http_status and not 200 <= last.http_status < 300:
        return (OUTCOME_ERRORED,
                f"the estimate endpoint answered HTTP {last.http_status}")
    if last.has_error:
        return (OUTCOME_ERRORED,
                f"the estimate reported an error: "
                f"{last.error_description or last.error_traceback}")
    if last.state_failed:
        return (OUTCOME_ERRORED,
                f"the estimate ended in state {last.state}")

    if last.is_ready:
        estimate = (last.estimated_size if last.estimated_size is not None
                    else last.profiles_matched)
        if (estimate or 0) > 0:
            return (OUTCOME_WITH_RESULT,
                    f"RESULT_READY with an estimate of {fmt_int(estimate)} "
                    f"profiles after {last.elapsed:.1f}s")
        read = last.profiles_read or 0
        if read > 0:
            return (OUTCOME_EMPTY,
                    f"RESULT_READY after reading {fmt_int(read)} profiles and "
                    f"matching none - sample starvation, not a fault")
        return (OUTCOME_EMPTY,
                "RESULT_READY having read 0 profiles - the store-wide sample "
                "looks empty; check sample-status")

    if timed_out:
        read = last.profiles_read or 0
        if read == 0:
            return (OUTCOME_NEVER_STARTED,
                    f"timed out after {last.elapsed:.0f}s in state "
                    f"{last.state or 'unknown'} having read 0 profiles, with no "
                    f"error reported - the estimate never started")
        return (OUTCOME_STALLED,
                f"timed out after {last.elapsed:.0f}s in state "
                f"{last.state or 'unknown'} having read {fmt_int(read)} "
                f"profiles without finishing")

    return (OUTCOME_STALLED,
            f"polling stopped in state {last.state or 'unknown'} before the "
            f"timeout without reaching {STATE_READY}")


def create_preview_job(headers: dict[str, str], pql: str) -> tuple[str, HttpResult]:
    """Create the preview job that triggers the estimate. Returns (previewId, result).

    The preview job is ephemeral: it evaluates the PQL against the sample and is
    never saved as a segment definition or audience.
    """
    payload = {
        "predicateExpression": pql,
        "predicateType": PREDICATE_TYPE,
        "predicateModel": PREDICATE_MODEL,
        "graphType": GRAPH_TYPE,
    }
    result = http_request("POST", PREVIEW_URL, headers, json_body=payload,
                          label="preview")
    if not result.ok:
        return "", result
    preview_id = str((result.body or {}).get("previewId") or "")
    return preview_id, result


def poll_estimate(headers: dict[str, str], preview_id: str, *,
                  timeout: float = DEFAULT_TIMEOUT_S,
                  interval: float = DEFAULT_INTERVAL_S) -> tuple[list[Poll], bool]:
    """Poll GET /estimate/{previewId} until it settles, fails, or the timeout.

    Returns (polls, timed_out). Polling stops early on RESULT_READY, on a
    failure state, on a populated error object, and on any response we could not
    read -- an unreadable response is a finding, not something to retry past.
    """
    url = f"{ESTIMATE_URL}/{urllib.parse.quote(preview_id, safe='')}"
    polls: list[Poll] = []
    started = time.monotonic()
    timed_out = False

    while True:
        result = http_request("GET", url, headers, label="estimate")
        elapsed = time.monotonic() - started
        poll = parse_estimate(result.body, result.status, elapsed,
                              result.parse_error or result.transport_error)
        polls.append(poll)
        LOG.debug("  %s", poll.summary_line())

        if poll.parse_error or not 200 <= poll.http_status < 300:
            break
        if poll.is_ready or poll.state_failed or poll.has_error:
            break

        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            timed_out = True
            break
        time.sleep(min(interval, remaining))

    return polls, timed_out


def run_probe(headers: dict[str, str], sandbox: str, pql: str, *,
              timeout: float = DEFAULT_TIMEOUT_S,
              interval: float = DEFAULT_INTERVAL_S) -> dict[str, Any]:
    """Fire the canary estimate, classify what happened, return the run record."""
    record: dict[str, Any] = {
        "timestamp_utc": iso_utc(utc_now()),
        "sandbox": sandbox,
        "command": "probe",
        "pql": pql,
        "timeout_seconds": timeout,
        "interval_seconds": interval,
    }

    LOG.info("Estimate probe  sandbox=%s", sandbox)
    LOG.info("  pql             : %s", pql)

    preview_id, create_result = create_preview_job(headers, pql)
    if not create_result.ok or not preview_id:
        note = (create_result.problem
                or "the preview job response carried no previewId")
        record.update({
            "outcome": OUTCOME_ERRORED,
            "note": clip(f"preview job not created: {note}"),
            "exit_code": OUTCOME_EXIT[OUTCOME_ERRORED],
            "http_status": create_result.status,
            "polls": 0,
            "elapsed_seconds": round(create_result.elapsed, 2),
            "poll_records": [],
        })
        LOG.error("  OUTCOME         : %s - preview job not created: %s",
                  OUTCOME_ERRORED, note)
        return record

    LOG.info("  preview id      : %s", mask_id(preview_id))

    polls, timed_out = poll_estimate(headers, preview_id, timeout=timeout,
                                     interval=interval)
    last = polls[-1] if polls else None
    outcome, note = classify_outcome(last, timed_out)
    elapsed = last.elapsed if last else 0.0

    record.update({
        "outcome": outcome,
        "note": note,
        "exit_code": OUTCOME_EXIT[outcome],
        "polls": len(polls),
        "elapsed_seconds": round(elapsed, 2),
        "timed_out": timed_out,
        "final_state": last.state if last else "",
        "http_status": last.http_status if last else 0,
        "profiles_read": last.profiles_read if last else None,
        "profiles_matched": last.profiles_matched if last else None,
        "num_rows_to_read": last.num_rows_to_read if last else None,
        "total_rows": last.total_rows if last else None,
        "estimated_size": last.estimated_size if last else None,
        "standard_error": last.standard_error if last else None,
        "confidence_interval": last.confidence_interval if last else "",
        "error_description": last.error_description if last else "",
        "error_traceback": last.error_traceback if last else "",
        "poll_records": [poll.as_dict() for poll in polls],
    })

    LOG.info("  polls           : %d over %.1fs", len(polls), elapsed)
    if last:
        LOG.info("  final           : %s", last.summary_line())
    emit = LOG.info if OUTCOME_EXIT[outcome] == 0 else LOG.warning
    emit("  OUTCOME         : %s - %s", outcome, note)
    return record


# ----------------------------------------------------------------------------
# History
# ----------------------------------------------------------------------------
def history_row(record: dict[str, Any]) -> dict[str, str]:
    """Project a run record onto the flat history schema.

    Only the columns in HISTORY_FIELDS are written -- which is what keeps the
    previewId, the PQL's sandbox context and every credential field out of the
    file. Booleans become yes/no, None becomes ''.
    """
    row: dict[str, str] = {}
    for name in HISTORY_FIELDS:
        value = record.get(name)
        if value is None:
            row[name] = ""
        elif isinstance(value, bool):
            row[name] = "yes" if value else "no"
        else:
            row[name] = str(value)
    return row


def append_history(path: Path, record: dict[str, Any]) -> None:
    """Append one row to the history CSV, writing the header on a new file.

    If the file already exists with a different header (an older version of this
    tool), its own column order is honoured so the CSV stays valid, and any
    column it lacks is reported rather than silently dropped.
    """
    row = history_row(record)
    fields = HISTORY_FIELDS
    exists = path.exists() and path.stat().st_size > 0

    if exists:
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                header = next(csv.reader(handle), [])
            if header and header != HISTORY_FIELDS:
                missing = [f for f in HISTORY_FIELDS if f not in header]
                if missing:
                    LOG.warning("  history: %s has an older header - not "
                                "writing %s. Point --history at a new file to "
                                "capture them.", path, ", ".join(missing))
                fields = header
        except OSError as ex:
            LOG.warning("  history: could not read %s (%s)", path, ex)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, restval="",
                                    extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(row)
        LOG.debug("  history: appended to %s", path)
    except OSError as ex:
        LOG.warning("  history: could not write %s (%s)", path, ex)


def previous_failure_streak(path: Path, sandbox: str,
                            command: str) -> tuple[int, str]:
    """Count the unbroken run of failures at the end of the history file.

    Walks backwards over the rows for this sandbox and subcommand, stopping at
    the first zero exit code, and returns (count, the earliest ISO timestamp in
    that run). A missing, empty or unreadable history returns (0, "") -- a file
    we cannot read is not evidence of a failure -- and a row whose exit code
    will not parse breaks the streak, so this under-reports rather than invents.
    """
    if not path.exists():
        return 0, ""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = [row for row in csv.DictReader(handle)
                    if (row.get("sandbox") or "") == sandbox
                    and (row.get("command") or "") == command]
    except (OSError, csv.Error) as ex:
        LOG.debug("  history: could not read %s (%s)", path, ex)
        return 0, ""

    streak: list[dict] = []
    for row in reversed(rows):
        code = (row.get("exit_code") or "").strip()
        if not code.isdigit() or int(code) == 0:
            break
        streak.append(row)
    if not streak:
        return 0, ""
    return len(streak), (streak[-1].get("timestamp_utc") or "").strip()


def report_degradation(path: Path, record: dict[str, Any], sandbox: str,
                       command: str,
                       now: datetime | None = None) -> dict[str, Any]:
    """Answer 'how long has this been stuck?' from the history, and say so.

    This is service-level, not job-level. Every probe creates a fresh preview
    job, so a single run can only ever report its own wait -- capped at
    --timeout. Reading the preceding runs back is what turns a column of
    180-second give-ups into "failing for 6.2 hours, since 06:00".

    Returns the fields to merge into the run record; {} when this run is
    healthy, in which case a recovery is noted if the previous run was not.
    """
    prior_count, prior_since = previous_failure_streak(path, sandbox, command)
    try:
        failing = int(record.get("exit_code", EXIT_FAILURE)) != 0
    except (TypeError, ValueError):
        failing = True

    if not failing:
        if prior_count:
            LOG.info("  RECOVERED       : first healthy run after %d "
                     "consecutive failure(s)", prior_count)
        return {}

    count = prior_count + 1
    since = prior_since or str(record.get("timestamp_utc") or "")
    started = parse_adobe_timestamp(since)
    hours: float | None = None
    if started is not None:
        hours = round(((now or utc_now()) - started).total_seconds() / 3600.0, 2)

    if count == 1:
        LOG.warning("  STUCK FOR       : this run only - first failure in "
                    "%s, nothing earlier to compare against", path.name)
    else:
        LOG.warning("  STUCK FOR       : %s across %d consecutive runs, since %s",
                    "unknown" if hours is None else f"{hours:.1f}h",
                    count, since or "unknown")
    return {"consecutive_failures": count, "degraded_since_utc": since,
            "degraded_hours": hours}


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the arguments both subcommands share."""
    parser.add_argument("--service", default=DEFAULT_SERVICE,
                        help=f"keyring service holding the credential set "
                             f"(default: {DEFAULT_SERVICE})")
    parser.add_argument("--sandbox", default="",
                        help="sandbox name (default: the credential's own "
                             f"sandbox, else {DEFAULT_SANDBOX})")
    parser.add_argument("--history", default=str(DEFAULT_HISTORY),
                        help=f"CSV appended to, one row per run (default: "
                             f"{DEFAULT_HISTORY.parent.name}/"
                             f"{DEFAULT_HISTORY.name} next to the script)")
    parser.add_argument("--no-history", action="store_true",
                        help="do not read or write the history file (also "
                             "disables the 'stuck for' report, which is read "
                             "back out of it)")
    parser.add_argument("--json", action="store_true",
                        help="emit the full run record to stdout as JSON")
    parser.add_argument("--verbose", action="store_true",
                        help="print each poll / request as it happens")


def record_run(record: dict[str, Any], history_path: Path, sandbox: str,
               command: str, *, write: bool = True) -> int:
    """Report how long this has been failing, file the run, return its exit code."""
    if write:
        # Must run before the append, or this run would count itself twice.
        record.update(report_degradation(history_path, record, sandbox, command))
        append_history(history_path, record)
    return int(record.get("exit_code", EXIT_FAILURE))


# ----------------------------------------------------------------------------
# Naked run -- no arguments, pick things at the prompt
# ----------------------------------------------------------------------------
def ask(question: str, default: bool = True) -> bool:
    """Yes/no at the TTY; Enter takes the default. Prompts on stderr."""
    print(f"  {question}{' [Y/n]: ' if default else ' [y/N]: '}",
          end="", file=sys.stderr, flush=True)
    try:
        raw = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return default if not raw else raw.startswith("y")


def list_sandboxes(token: str, conf: dict[str, str]) -> list[dict]:
    """Best-effort sandbox list for the interactive menu; [] if unavailable.

    Not every credential can read sandbox-management, and that is not a reason
    to fail -- the caller falls back to typing a name.
    """
    result = http_request("GET", SANDBOX_LIST_URL, api_headers(token, conf),
                          attempts=1, label="sandboxes")
    if not result.ok:
        return []
    found = (result.body or {}).get("sandboxes")
    return [s for s in found if isinstance(s, dict)] if isinstance(found, list) else []


def pick_sandbox(sandboxes: list[dict], default_name: str) -> str:
    """Numbered sandbox menu; Enter takes the default. Falls back to free text."""
    names = [str(s.get("name") or "") for s in sandboxes if s.get("name")]
    if not names:
        print(f"  Sandbox name (Enter for {default_name}): ",
              end="", file=sys.stderr, flush=True)
        try:
            return input().strip() or default_name
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return default_name

    if default_name not in names:
        default_name = names[0]
    print("\n  Select a sandbox:", file=sys.stderr)
    for i, name in enumerate(names, 1):
        mark = "  (default)" if name == default_name else ""
        print(f"    {i}. {name}{mark}", file=sys.stderr)
    while True:
        print(f"  Number (or Enter for {default_name}): ",
              end="", file=sys.stderr, flush=True)
        try:
            raw = input().strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return default_name
        if not raw:
            return default_name
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        print(f"  Enter 1-{len(names)}.", file=sys.stderr)


def run_interactive() -> int:
    """Run with no arguments: pick a credential and sandbox, check both surfaces.

    The sample status is free and instant, so it always runs. The probe can take
    up to --timeout, so it is offered rather than assumed. Both runs land in the
    default history, which is what makes the "stuck for" line meaningful over
    time. Returns the worst exit code of the checks actually run.
    """
    setup_logging(verbose=False)
    LOG.info("%s %s -- AEP audience-estimate health probe",
             SCRIPT_NAME, SCRIPT_VERSION)

    if not sys.stdin.isatty():
        LOG.error("No arguments and no TTY to prompt at. Name a subcommand "
                  "instead: 'sample-status' or 'probe' (see --help).")
        return EXIT_FAILURE
    if not _CREDS_OK:              # pragma: no cover - environment-dependent
        LOG.error("the shared aep_creds module is unavailable (%s)", _CREDS_ERR)
        return EXIT_FAILURE

    try:
        service = aep_creds.resolve_service(None)
        conf = load_credentials(service)
        token = authenticate(conf)
    except ProbeError as ex:
        LOG.error("%s", ex)
        return EXIT_FAILURE
    except Exception as ex:        # aep_creds.CredsError, or a cancelled menu
        LOG.error("%s", ex)
        return EXIT_FAILURE

    sandbox = pick_sandbox(list_sandboxes(token, conf),
                           conf.get("sandbox") or DEFAULT_SANDBOX)
    headers = api_headers(token, conf, sandbox)
    history = DEFAULT_HISTORY

    LOG.info("")
    record = run_sample_status(headers, sandbox, DEFAULT_MAX_AGE_HOURS)
    worst = record_run(record, history, sandbox, "sample-status")

    LOG.info("")
    if ask(f"Run the estimate probe too? Up to {DEFAULT_TIMEOUT_S:g}s."):
        LOG.info("")
        probe_record = run_probe(headers, sandbox, DEFAULT_PQL)
        worst = max(worst, record_run(probe_record, history, sandbox, "probe"))
    else:
        LOG.info("  probe skipped.")

    LOG.info("")
    LOG.info("History: %s", history)
    return worst


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for both subcommands."""
    parser = argparse.ArgumentParser(
        prog="estimate_prober.py",
        description="Observe the health of the AEP audience estimation service "
                    "in one sandbox: the store-wide sample it runs against, and "
                    "the estimate job itself.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Outcomes: COMPLETED_WITH_RESULT / COMPLETED_EMPTY exit 0; "
               "NEVER_STARTED exits 2, STALLED 3, ERRORED 4.")
    parser.add_argument("--version", action="version",
                        version=f"{SCRIPT_NAME} {SCRIPT_VERSION} ({SCRIPT_DATE})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser(
        "sample-status",
        help="report the age and size of the Profile sample estimates run on")
    add_common_arguments(sample)
    sample.add_argument("--max-age-hours", type=float,
                        default=DEFAULT_MAX_AGE_HOURS,
                        help=f"fail when the sample is older than this "
                             f"(default: {DEFAULT_MAX_AGE_HOURS:g})")
    sample.add_argument("--report", choices=["dataset", "namespace"], default="",
                        help="also print the sample's distribution by dataset "
                             "or by identity namespace (not written to history)")
    sample.add_argument("--report-date", default="",
                        help="report date, YYYY-MM-DD (default: latest)")

    probe = subparsers.add_parser(
        "probe", help="run the canary estimate and classify the outcome")
    add_common_arguments(probe)
    probe.add_argument("--pql", default=DEFAULT_PQL,
                       help=f"canary segment definition, PQL text "
                            f"(default: {DEFAULT_PQL!r})")
    probe.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                       help=f"give up after this many seconds "
                            f"(default: {DEFAULT_TIMEOUT_S:g})")
    probe.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                       help=f"seconds between polls "
                            f"(default: {DEFAULT_INTERVAL_S:g})")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code.

    With no arguments at all, drops into the interactive run; otherwise a
    subcommand is required, so a typo never silently starts prompting.
    """
    raw = sys.argv[1:] if argv is None else argv
    if not raw:
        return run_interactive()

    args = build_parser().parse_args(raw)
    setup_logging(args.verbose)

    if not _DEPS_OK:                   # pragma: no cover - environment-dependent
        LOG.error("%s needs the 'requests' package: %s", SCRIPT_NAME, _DEPS_ERR)
        return EXIT_FAILURE

    if args.command == "probe":
        if args.interval <= 0:
            LOG.error("--interval must be greater than 0")
            return EXIT_FAILURE
        if args.timeout <= 0:
            LOG.error("--timeout must be greater than 0")
            return EXIT_FAILURE

    try:
        conf = load_credentials(args.service)
        sandbox = args.sandbox or conf.get("sandbox") or DEFAULT_SANDBOX
        token = authenticate(conf)
        headers = api_headers(token, conf, sandbox)
    except ProbeError as ex:
        LOG.error("%s", ex)
        return EXIT_FAILURE

    if args.command == "sample-status":
        record = run_sample_status(headers, sandbox, args.max_age_hours,
                                   report=args.report,
                                   report_date=args.report_date)
    else:
        record = run_probe(headers, sandbox, args.pql,
                           timeout=args.timeout, interval=args.interval)

    exit_code = record_run(record, Path(args.history), sandbox, args.command,
                           write=not args.no_history)

    if args.json:
        print(json.dumps(record, indent=2, default=str))

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:          # pragma: no cover - interactive only
        LOG.warning("interrupted")
        sys.exit(EXIT_FAILURE)
