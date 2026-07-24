#!/usr/bin/env python3
"""
dataset_census.py  (AEP Swiss Army Knife)
=========================================
Dataset Census -- counts the profile-enabled datasets in a sandbox and groups
them by the XDM base class of the schema behind each one.

Real Time Customer Profile has a guardrail on how many datasets you can enable
per class (commonly 20). Nobody notices they're near it until an enablement
silently fails, so this tool answers one question fast:

    "How many profile-enabled datasets do I have, and of which class?"

  1. Authenticate against IMS (pick a credential set from the keyring vault,
     or a plaintext creds/ folder where keyring is unavailable).
  2. Pick a sandbox (Enter = prod). --sandbox=all sweeps every sandbox.
  3. Pull ALL Catalog dataSets (paged -- not just the first 100), then keep the
     ones whose tags.unifiedProfile contains "enabled:true".

     Do NOT be tempted back to Catalog's ?unifiedProfile=enabled:true query
     filter. Catalog takes it, returns 200, and ignores it -- you get every
     dataset in the sandbox and no way to tell. That bug reported every
     dataset in the sandbox as "profile-enabled" -- an order of magnitude
     more than the UI showed.

     Enablement is a TOGGLE, and it is the only thing the guardrail counts. It
     is NOT the same question as "is this schema built on the Profile class" --
     plenty of Profile-class datasets are never enabled.
  4. Resolve each surviving dataset's schemaRef.id against the Schema Registry,
     read meta:class, and bucket it: Profile / ExperienceEvent / Other.
     Schemas are de-duped and fetched concurrently -- many datasets share one.
  5. Print the dataset table, the per-class totals, and shout if any class is
     at or over the warn threshold (default 18 of 20).

Read-only. No creds, org IDs or dataset names are written anywhere unless you
ask for --csv, which drops a file in output/.

Usage:
    python dataset_census.py                          # interactive menus
    python dataset_census.py "acme-beta"              # pick creds by service name
    python dataset_census.py "acme-k" --sandbox=prod
    python dataset_census.py "acme-k" --sandbox=all --csv
    python dataset_census.py "acme-k" --all-datasets  # census EVERY dataset
    python dataset_census.py "acme-k" --limit=25 --warn=22
    python dataset_census.py "acme-k" --debug         # per-dataset include/exclude
    python dataset_census.py "acme-k" --expect=23     # tripwire vs the UI count
    python dataset_census.py "acme-k" --expect=off    # ...or silence it

--debug (or DATASET_CENSUS_DEBUG=1) prints, for every dataset counted: its name,
its raw unifiedProfile tag, the schemaRef.id, and the class we resolved. That is
the table to read when the total disagrees with the UI.

Credentials come from the OS keyring (Windows Credential Manager) via
aep_creds, falling back to a plaintext creds/ folder only where keyring is
unavailable; manage them with credential_validator_v2.py. Service names are the
keys you stored -- run `credential_validator_v2.py list` to see them.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import aep_creds  # keyring-backed credential store (replaces creds/*.json)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_NAME    = "dataset_census"
SCRIPT_VERSION = "1.1.0"
SCRIPT_DATE    = "2026-07-24"
SCRIPT_AUTHOR  = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
PLATFORM = "https://platform.adobe.io"
SANDBOX_LIST_URL = f"{PLATFORM}/data/foundation/sandbox-management/sandboxes"
SCHEMAS_URL = f"{PLATFORM}/data/foundation/schemaregistry/tenant/schemas"
DATASETS_URL = f"{PLATFORM}/data/foundation/catalog/dataSets"

DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

XED_LIST = "application/vnd.adobe.xed+json"

# The Profile enablement guardrail, and the point at which we start shouting.
# Adobe's published default is 20 datasets per class; override with --limit.
# Warn only AT the guardrail, not before it -- an early warning on a number we
# already know we can't fully trust is just noise.
DEFAULT_CLASS_LIMIT = 20
DEFAULT_WARN_AT = 20

# What the AEP UI's "Show Profiles: Enabled" filter reports for this sandbox.
# A tripwire, not a target: if our count drifts from what a human can see in the
# UI, the census is measuring the wrong population and every number below it is
# suspect. Override per-sandbox with --expect=N, or --expect=off to silence.
EXPECTED_ENABLED = 23

# meta:class -> bucket. Anything unrecognised falls into "Other".
CLASS_BUCKETS = {
    "https://ns.adobe.com/xdm/context/profile": "Profile",
    "https://ns.adobe.com/xdm/context/experienceevent": "ExperienceEvent",
}
# "Unclassified" is deliberately NOT "Other": it means we could not determine a
# class, which is a different fact from "the class is something else". Two ways
# to land there, both seen in the wild:
#   * the dataset carries no schemaRef at all (ad-hoc / system datasets), and
#   * the schema resolves but has no meta:class even in xed-full -- Adobe system
#     schemas like Destinations and the Offer Decisioning repositories do this.
BUCKET_ORDER = ["Profile", "ExperienceEvent", "Other", "Unclassified"]
GUARDED_BUCKETS = ("Profile", "ExperienceEvent")

# Ad-hoc schemas mint one throwaway class per dataset, named as a hex hash under
# the tenant namespace. They dominate the Other bucket and are noise -- worth
# collapsing into a single line rather than listing a hundred one-offs.
_ADHOC_CLASS_RE = re.compile(r"/classes/[0-9a-f]{20,}$", re.I)

# Retry policy for the two APIs (Catalog throttles hard on big sandboxes).
MAX_RETRIES = 4
BACKOFF_BASE = 1.5

# Big sandboxes carry hundreds of distinct schemas. Resolving them one at a time
# is minutes of silence, so fetch them concurrently. Keep the pool modest --
# Schema Registry starts 429ing above roughly this, and the retry/backoff in
# http() then eats any speed we gained.
SCHEMA_WORKERS = 8


# ----------------------------------------------------------------------------
# ANSI / logging - matches the house style (data_dictionary_v3.py et al.)
# ----------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h, mode.value | 0x0004)
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
logger = logging.getLogger(SCRIPT_NAME)
SSL_CTX = ssl._create_unverified_context()


# ----------------------------------------------------------------------------
# HTTP / IMS / credential helpers  (shared house style, + retry/backoff)
# ----------------------------------------------------------------------------
def _raw_http(url, method="GET", headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def http(url, method="GET", headers=None, data=None, timeout=60):
    """As the house `http()`, but retries 429 / 5xx / transient network errors
    with exponential backoff. Honours Retry-After when the server sends it."""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return _raw_http(url, method, headers, data, timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                raise                       # 4xx that won't fix itself -- bail
            wait = _retry_after(e) or BACKOFF_BASE ** attempt
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            wait = BACKOFF_BASE ** attempt
        if attempt == MAX_RETRIES - 1:
            break
        logger.warning(f"{_err_label(last)} -- retrying in {wait:.0f}s "
                       f"({attempt + 1}/{MAX_RETRIES - 1})")
        time.sleep(wait)
    raise last


def _retry_after(e: urllib.error.HTTPError):
    try:
        return float(e.headers.get("Retry-After") or 0) or None
    except (TypeError, ValueError):
        return None


def _err_label(e) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    return f"{type(e).__name__}: {e}"


def flatten_err(text: str, limit: int = 200) -> str:
    return " ".join((text or "").split())[:limit]


def authenticate(conf):
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": conf["client_id"],
        "client_secret": conf["client_secret"],
        "scope": conf.get("scopes") or DEFAULT_SCOPES,
    }).encode("utf-8")
    body, _ = http(
        conf.get("oauth_url") or IMS_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    return json.loads(body)


def aep_headers(token, conf, sandbox=None, accept="application/json"):
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "Accept": accept,
    }
    if sandbox:
        headers["x-sandbox-name"] = sandbox
    return headers


def list_sandboxes(token, conf):
    try:
        body, _ = http(SANDBOX_LIST_URL, headers=aep_headers(token, conf))
        data = json.loads(body)
        return True, data.get("sandboxes") or []
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {flatten_err(e.read().decode(errors='replace'))}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------------------
# AEP reads -- datasets + schema classes
# ----------------------------------------------------------------------------
def get_profile_datasets(token, conf, sandbox, profile_only=True,
                         exclude_system=False):
    """Page through Catalog /dataSets. Catalog returns a DICT keyed by dataset
    id, not a list, and caps each page at `limit` -- so walk start/limit until a
    short page comes back. Returns [{id, name, schema_id, profile_enabled}].

    Fetches EVERY dataset and decides profile-enablement here, from the
    dataset's own `tags.unifiedProfile`, rather than trusting Catalog's
    `?unifiedProfile=enabled:true` query filter.

    That filter is a trap. Catalog accepts it, returns 200, and -- at least for
    this org -- ignores it, handing back every dataset in the sandbox. A filter
    that is silently ignored is indistinguishable from a filter that matched
    everything, so the first version of this script reported every dataset in the
    sandbox as profile-enabled. Reading the tag is the only answer we can
    actually verify."""
    out = _page_datasets(token, conf, sandbox, server_filter=False)

    total = len(out)
    if not profile_only:
        logger.info(f"{sandbox}: {total} dataset(s) in the sandbox.")
        return out

    enabled = [d for d in out if d["profile_enabled"]]
    logger.info(f"{sandbox}: {total} dataset(s) in the sandbox, "
                f"{len(enabled)} enabled for Profile (by tags.unifiedProfile).")

    _compare_server_filter(token, conf, sandbox, enabled)

    # Adobe-managed datasets (AJO, Journey, CJA, Audience Portal, the UPS
    # segment-ingestion dataset) are enabled for Profile but Adobe owns them --
    # they carry classification.managedBy=SYSTEM and the systemLabel
    # "user_no_write", and the Datasets UI hides them. You did not create them
    # and you cannot turn them off, so for "what have WE enabled" they are noise.
    # They still occupy the Profile store, so they are NOT excluded by default --
    # the guardrail counts them even though the UI does not show them.
    system = [d for d in enabled if d["managed_by"] == "SYSTEM"]
    if system:
        logger.info(f"{sandbox}: {len(system)} of those are Adobe-managed "
                    f"(managedBy=SYSTEM) and are hidden in the AEP UI. "
                    f"{len(enabled) - len(system)} are yours. "
                    f"Use --exclude-system to count only yours.")
    if exclude_system:
        enabled = [d for d in enabled if d["managed_by"] != "SYSTEM"]
        logger.info(f"{sandbox}: excluding Adobe-managed datasets -- "
                    f"{len(enabled)} remain.")
    return enabled


def _page_datasets(token, conf, sandbox, server_filter: bool):
    """One pass over Catalog's paged dataSets. `server_filter` appends the
    unifiedProfile query param -- used ONLY to audit that param, never to
    produce the census."""
    which = "profile-filtered" if server_filter else "all"
    logger.info(f"{sandbox}: listing {which} datasets from Catalog...")
    out = []
    headers = aep_headers(token, conf, sandbox)
    start, limit = 0, 100
    bar = Progress(f"{sandbox}: paging Catalog ({which}) --")   # total unknown
    while True:
        # `classification` is what carries managedBy, and `properties=` is a
        # PROJECTION -- anything omitted here comes back absent, not merely
        # unfiltered. Leaving it out silently emptied managed_by on every row.
        url = (f"{DATASETS_URL}?limit={limit}&start={start}"
               f"&properties=name,schemaRef,tags,classification")
        if server_filter:
            url += "&unifiedProfile=enabled:true"
        body, _ = http(url, headers=headers)
        data = json.loads(body)
        if not isinstance(data, dict) or not data:
            break
        for dsid, ds in data.items():
            if not isinstance(ds, dict):
                continue
            out.append({
                "id": dsid,
                "name": ds.get("name") or dsid,
                "schema_id": (ds.get("schemaRef") or {}).get("id") or "",
                "profile_tag": _profile_tag_raw(ds),
                "profile_enabled": is_profile_enabled(ds),
                "managed_by": (ds.get("classification") or {}).get(
                    "managedBy") or "(absent)",
            })
        bar.advance(len(data))
        if len(data) < limit:
            break
        start += limit
    bar.finish()
    return out


def _compare_server_filter(token, conf, sandbox, enabled):
    """Audit Catalog's ?unifiedProfile=enabled:true against our own tag test.

    We do NOT use the server filter for the census -- it has been observed to be
    silently ignored, returning every dataset in the sandbox with a cheerful 200.
    But whether it works is a fact about the platform worth knowing, and if Adobe
    ever fixes it this is what will tell us. Cheap: one extra paged read."""
    try:
        served = _page_datasets(token, conf, sandbox, server_filter=True)
    except Exception as e:
        logger.debug(f"{sandbox}: server-filter audit skipped -- {e}")
        return
    ours, theirs = {d["id"] for d in enabled}, {d["id"] for d in served}
    if ours == theirs:
        logger.info(f"{sandbox}: Catalog's unifiedProfile filter agrees with the "
                    f"tag test ({len(theirs)}). Both are trustworthy here.")
        return
    logger.warning(
        f"{sandbox}: Catalog's unifiedProfile=enabled:true filter returned "
        f"{len(theirs)} dataset(s); the tags.unifiedProfile test says {len(ours)}. "
        f"The filter is NOT reliable -- using the tag test. "
        f"(+{len(theirs - ours)} it wrongly included, "
        f"-{len(ours - theirs)} it wrongly dropped.)")


def is_profile_enabled(ds: dict) -> bool:
    """True when the dataset's Profile toggle is on.

    Catalog carries this as a TAG, not a field: tags.unifiedProfile is a list
    of strings, and enablement is the literal "enabled:true" among them. The
    tag is absent entirely on datasets that were never enabled.

    This is the ONLY thing the Profile guardrail counts. A dataset can be built
    on the XDM Individual Profile class and still not be enabled -- class and
    enablement are different questions, and conflating them is what produced
    the original over-count."""
    tags = ds.get("tags") or {}
    values = tags.get("unifiedProfile") or []
    if isinstance(values, str):          # Catalog is inconsistent about this
        values = [values]
    return any(str(v).strip().lower() == "enabled:true" for v in values)


def _profile_tag_raw(ds: dict) -> str:
    """The unifiedProfile tag exactly as Catalog sent it, for --debug. Shows the
    difference between enabled:false and the tag being absent -- both are "not
    enabled", but only one means somebody turned it off."""
    tags = ds.get("tags") or {}
    if "unifiedProfile" not in tags:
        return "(absent)"
    return json.dumps(tags.get("unifiedProfile"))


def fetch_schema_class(token, conf, sandbox, schema_id):
    """meta:class + title for one schemaRef.id.

    `reason` explains an EMPTY class_id, so the caller can tell "lookup broke"
    apart from "this schema legitimately has no class". Adobe's own system
    schemas (Destinations, Offer Decisioning) resolve fine and still carry no
    meta:class -- that's not an error, it's just unclassifiable."""
    result = {"class_id": "", "title": "", "error": "", "reason": ""}
    if not schema_id:
        result["reason"] = "no schemaRef on the dataset"
        return result

    url = f"{SCHEMAS_URL}/{urllib.parse.quote(schema_id, safe='')}"
    try:
        body, _ = http(url, headers=aep_headers(token, conf, sandbox,
                                                accept=XED_LIST))
        raw = json.loads(body)
        result["class_id"] = raw.get("meta:class") or ""
        result["title"] = raw.get("title") or ""
        if not result["class_id"]:
            result["reason"] = "schema declares no meta:class (system schema)"
    except urllib.error.HTTPError as e:
        detail = flatten_err(e.read().decode(errors="replace"), 120)
        result["error"] = f"HTTP {e.code}: {detail}"
        result["reason"] = "schema lookup failed"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["reason"] = "schema lookup failed"
    return result


def resolve_schema_classes(token, conf, sandbox, schema_ids):
    """Resolve a set of schemaRef ids to {schema_id: info}, concurrently.

    Datasets share schemas heavily, so we de-dupe first -- hundreds of datasets
    are usually a much smaller set of schemas. What's left still has to go over
    the wire one GET at a time, so run a small pool and report progress: a
    silent multi-minute stall is indistinguishable from a hang."""
    ids = sorted(set(schema_ids))
    if not ids:
        return {}
    logger.info(f"{sandbox}: {len(schema_ids)} dataset(s) share {len(ids)} "
                f"distinct schema(s) -- resolving the class of each, "
                f"{SCHEMA_WORKERS} at a time. This is the slow part.")
    resolved = {}
    bar = Progress(f"{sandbox}: schemas", total=len(ids))
    with ThreadPoolExecutor(max_workers=SCHEMA_WORKERS) as pool:
        futures = {
            pool.submit(fetch_schema_class, token, conf, sandbox, sid): sid
            for sid in ids
        }
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                resolved[sid] = fut.result()
            except Exception as e:      # http() re-raises after its retries
                resolved[sid] = {"class_id": "", "title": "",
                                 "error": f"{type(e).__name__}: {e}",
                                 "reason": "schema lookup failed"}
            bar.advance()
    bar.finish(f"{sandbox}: resolved {len(resolved)} schema(s).")
    return resolved


class Progress:
    """Live progress for the long network loops.

    This exists because the first version of this script sat silent for minutes
    on a big sandbox and looked hung. A slow tool that says what it is doing is
    a slow tool; a slow tool that says nothing is a broken one. So: never let a
    loop over the network run without a heartbeat.

    On a TTY it redraws one line with a bar, count, elapsed and an ETA. Piped to
    a file it degrades to a periodic log line -- carriage returns would be noise
    there, but silence would be worse.

    `total` may be None when the length isn't known ahead of time (Catalog paging
    doesn't tell us how many datasets there are until we hit the last page); we
    just show the running count and drop the bar and ETA."""

    BAR_WIDTH = 24
    LOG_EVERY = 10.0        # seconds, non-TTY only

    def __init__(self, label: str, total: int | None = None):
        self.label = label
        self.total = total
        self.done = 0
        self.start = time.monotonic()
        self.tty = sys.stdout.isatty()
        self._last_log = 0.0

    def advance(self, n: int = 1) -> None:
        self.done += n
        self._render()

    def _render(self) -> None:
        elapsed = time.monotonic() - self.start
        if self.tty:
            print(f"\r{ANSI['dim']}  {self.label} {self._body(elapsed)}"
                  f"{ANSI['reset']}\033[K", end="", flush=True)
        elif elapsed - self._last_log >= self.LOG_EVERY:
            self._last_log = elapsed
            logger.info(f"{self.label} {self._body(elapsed)}")

    def _body(self, elapsed: float) -> str:
        if not self.total:
            return f"{self.done} so far... [{_mmss(elapsed)}]"
        frac = min(1.0, self.done / self.total)
        filled = int(frac * self.BAR_WIDTH)
        bar = "#" * filled + "." * (self.BAR_WIDTH - filled)
        # Rate is measured, not assumed -- the pool warms up and Schema Registry
        # throttles, so an ETA extrapolated from the first call would be a lie.
        eta = ""
        if self.done and frac < 1.0:
            remaining = (elapsed / self.done) * (self.total - self.done)
            eta = f", ~{_mmss(remaining)} left"
        return (f"[{bar}] {self.done}/{self.total} "
                f"({frac * 100:.0f}%, {_mmss(elapsed)} elapsed{eta})")

    def finish(self, note: str = "") -> None:
        elapsed = time.monotonic() - self.start
        if self.tty:
            print("\r\033[K", end="", flush=True)
        if note:
            logger.info(f"{note} [{_mmss(elapsed)}]")


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def bucket_of(class_id: str) -> str:
    cid = (class_id or "").strip()
    if not cid:
        return "Unclassified"
    return CLASS_BUCKETS.get(cid, "Other")


def class_label(class_id: str) -> str:
    """Short, readable name for a class id in the Other breakdown."""
    if _ADHOC_CLASS_RE.search(class_id or ""):
        return "(ad-hoc class, one per dataset)"
    return (class_id or "").rsplit("/", 1)[-1] or "(unknown)"


def census(token, conf, sandbox, profile_only=True, exclude_system=False):
    """Returns the per-dataset rows for one sandbox, class resolved.

    get_profile_datasets() has already applied the enablement filter, so the
    rows here are the population the guardrail actually counts."""
    datasets = get_profile_datasets(token, conf, sandbox, profile_only,
                                    exclude_system)
    if not datasets:
        return []

    resolved = resolve_schema_classes(
        token, conf, sandbox, [d["schema_id"] for d in datasets])

    rows = []
    failed = 0
    for d in sorted(datasets, key=lambda x: x["name"].lower()):
        info = resolved.get(d["schema_id"]) or fetch_schema_class(
            token, conf, sandbox, d["schema_id"])
        if info["error"]:
            failed += 1
            logger.debug(f"{d['name']}: schema lookup failed -- {info['error']}")
        rows.append({
            "sandbox": sandbox,
            "dataset": d["name"],
            "dataset_id": d["id"],
            "profile_tag": d.get("profile_tag", ""),
            "managed_by": d.get("managed_by", ""),
            "schema": info["title"] or "(unresolved)",
            "schema_id": d["schema_id"],
            "class_id": info["class_id"],
            "bucket": bucket_of(info["class_id"]),
            "note": info["reason"],
        })
    if failed:
        # One line, not one per dataset: a broken schema shared by 200 datasets
        # would otherwise bury the report under 200 identical warnings.
        logger.warning(f"{sandbox}: {failed} dataset(s) whose schema would not "
                       f"resolve -- they land in Unclassified. Re-run with "
                       f"--debug to see each one.")
    return rows


def print_debug_table(rows, sandbox_label):
    """Everything the filter and the classifier decided, per dataset, so a human
    can eyeball what got included and why. This is the view that would have
    caught the original over-count in about ten seconds."""
    print()
    print(ANSI["magenta"] + "=" * 130 + ANSI["reset"])
    print(f"  {ANSI['bold']}DEBUG -- every dataset counted as profile-enabled"
          f"{ANSI['reset']}  {ANSI['dim']}({sandbox_label}){ANSI['reset']}")
    print(ANSI["magenta"] + "-" * 130 + ANSI["reset"])
    print(f"  {ANSI['bold']}{'Dataset':<40} {'unifiedProfile tag':<34} "
          f"{'managedBy':<10} {'Class':<16} {'schemaRef.id':<24}{ANSI['reset']}")
    for r in sorted(rows, key=lambda x: (x["bucket"], x["dataset"].lower())):
        # SYSTEM rows dimmed: they're the ones the UI hides, so they're the usual
        # reason a count here disagrees with what you can see in AEP.
        mb = r.get("managed_by", "")
        mb_col = ANSI["yellow"] if mb == "SYSTEM" else ANSI["dim"]
        print(f"  {_clip(r['dataset'], 40):<40} "
              f"{ANSI['cyan']}{_clip(r['profile_tag'], 34):<34}{ANSI['reset']} "
              f"{mb_col}{_clip(mb, 10):<10}{ANSI['reset']} "
              f"{_clip(r['bucket'], 16):<16} "
              f"{ANSI['dim']}{_clip(r['schema_id'] or '(none)', 24):<24}"
              f"{ANSI['reset']}")
    print(ANSI["magenta"] + "=" * 130 + ANSI["reset"])


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def _tally(rows):
    counts = {b: 0 for b in BUCKET_ORDER}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    return counts


def _clip(s: str, width: int) -> str:
    s = s or ""
    return s if len(s) <= width else s[: width - 1] + "…"


def print_report(rows, sandbox_label, limit, warn_at, profile_only, expect=None):
    bar = ANSI["cyan"] + "=" * 96 + ANSI["reset"]
    scope = "Profile-enabled datasets" if profile_only else "All datasets"
    print()
    print(bar)
    print(f"  {ANSI['bold']}{scope} by XDM class{ANSI['reset']}  "
          f"{ANSI['dim']}({sandbox_label}){ANSI['reset']}")
    print(ANSI["cyan"] + "-" * 96 + ANSI["reset"])

    if not rows:
        print(f"  {ANSI['dim']}No datasets found.{ANSI['reset']}")
        print(bar)
        return

    print(f"  {ANSI['bold']}{'Dataset':<44} {'Class':<16} {'Schema':<32}{ANSI['reset']}")
    for b in BUCKET_ORDER:
        for r in [x for x in rows if x["bucket"] == b]:
            colour = {"Profile": ANSI["green"],
                      "ExperienceEvent": ANSI["blue"]}.get(b, ANSI["yellow"])
            print(f"  {_clip(r['dataset'], 44):<44} "
                  f"{colour}{b:<16}{ANSI['reset']} "
                  f"{ANSI['dim']}{_clip(r['schema'], 32):<32}{ANSI['reset']}")

    counts = _tally(rows)
    print(ANSI["cyan"] + "-" * 96 + ANSI["reset"])
    for b in BUCKET_ORDER:
        n = counts.get(b, 0)
        if b not in GUARDED_BUCKETS:
            flag, colour = "", ANSI["dim"]
        elif n > limit:
            flag, colour = f"  <-- OVER the {limit} guardrail", ANSI["red"] + ANSI["bold"]
        elif n >= warn_at:
            flag, colour = f"  <-- approaching the {limit} guardrail", ANSI["yellow"] + ANSI["bold"]
        else:
            flag, colour = "", ANSI["green"]
        print(f"  {colour}{b:<16} {n:>4}{ANSI['reset']}{ANSI['bold']}{flag}{ANSI['reset']}")
    print(f"  {ANSI['dim']}{'Total':<16} {len(rows):>4}{ANSI['reset']}")

    # Who owns them. This is the line that reconciles our count with the UI's:
    # the AEP Datasets UI hides SYSTEM-managed datasets, so it will always show
    # fewer than the guardrail actually counts.
    owners = {}
    for r in rows:
        mb = r.get("managed_by") or "(absent)"
        owners[mb] = owners.get(mb, 0) + 1
    if len(owners) > 1:
        print(ANSI["cyan"] + "-" * 96 + ANSI["reset"])
        print(f"  {ANSI['bold']}Who enabled them{ANSI['reset']}")
        for mb, n in sorted(owners.items(), key=lambda kv: -kv[1]):
            note = ("  <-- Adobe's own; hidden in the AEP UI, but still counts "
                    "toward the guardrail" if mb == "SYSTEM" else "")
            print(f"    {ANSI['dim']}{mb:<12}{ANSI['reset']} {n:>4}"
                  f"{ANSI['dim']}{note}{ANSI['reset']}")

    _print_breakdown("Other -- by class", ANSI["yellow"],
                     [class_label(r["class_id"]) for r in rows
                      if r["bucket"] == "Other"])
    _print_breakdown("Unclassified -- why", ANSI["dim"],
                     [r["note"] or "(unknown)" for r in rows
                      if r["bucket"] == "Unclassified"])
    print(bar)

    if not profile_only:
        return

    # The tripwire runs BEFORE the guardrail warnings, because if the population
    # is wrong the guardrail numbers are meaningless and shouldn't be read at all.
    _check_expected(rows, sandbox_label, expect)

    for b in GUARDED_BUCKETS:
        n = counts.get(b, 0)
        if n > limit:
            logger.error(f"{sandbox_label}: {n} {b} datasets enabled for Profile "
                         f"-- past the {limit} guardrail. New enablements may fail.")
        elif n >= warn_at:
            logger.warning(f"{sandbox_label}: {n} {b} datasets enabled for Profile "
                           f"-- at the {limit} guardrail. New enablements may fail.")


def _check_expected(rows, sandbox_label, expect):
    """Compare our profile-enabled count against what the UI shows.

    A warning, not an assert. An assert would kill the run and throw away a
    report that is still useful for diagnosing the very mismatch it tripped on --
    and the expected number is itself just a human's reading of a UI, so it is
    not automatically the more correct of the two."""
    if not expect:
        return
    n = len(rows)
    if n == expect:
        logger.info(f"{sandbox_label}: {n} profile-enabled datasets, matching the "
                    f"expected UI count. The filter is measuring the right set.")
        return
    system = sum(1 for r in rows if r.get("managed_by") == "SYSTEM")
    logger.warning(
        f"{sandbox_label}: counted {n} profile-enabled dataset(s) but expected "
        f"{expect} (the AEP UI's 'Show Profiles: Enabled' count) -- "
        f"{'over' if n > expect else 'under'} by {abs(n - expect)}.")
    if system:
        logger.warning(
            f"{sandbox_label}: {system} of the {n} are Adobe-managed "
            f"(managedBy=SYSTEM) and do not appear in the UI list at all, which "
            f"accounts for {system} of the difference. That still leaves "
            f"{abs(n - system - expect)} unexplained -- run --debug and compare "
            f"the table against the UI to find them.")


def _print_breakdown(title, colour, labels, top=8):
    """Roll up a bucket's contents so a big count isn't an opaque number. Counts
    that fall past `top` are reported as a remainder -- never silently dropped."""
    if not labels:
        return
    tally = {}
    for lab in labels:
        tally[lab] = tally.get(lab, 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    print(ANSI["cyan"] + "-" * 96 + ANSI["reset"])
    print(f"  {colour}{ANSI['bold']}{title}{ANSI['reset']}")
    for lab, n in ranked[:top]:
        print(f"    {ANSI['dim']}{_clip(lab, 60):<60}{ANSI['reset']} {n:>4}")
    if len(ranked) > top:
        rest = sum(n for _, n in ranked[top:])
        print(f"    {ANSI['dim']}{f'... and {len(ranked) - top} more class(es)':<60}"
              f"{ANSI['reset']} {rest:>4}")


def write_csv(rows, client: str):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    path = OUTPUT_DIR / f"Dataset Census - {client} - {stamp}.csv"
    cols = ["sandbox", "dataset", "dataset_id", "profile_tag", "managed_by",
            "schema", "schema_id", "class_id", "bucket", "note"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"Wrote {path}")


# ----------------------------------------------------------------------------
# Credential menu + sandbox picker  (shared house style)
# ----------------------------------------------------------------------------
def pretty_name(stem: str) -> str:
    return stem.replace("-", " ").replace("_", " ").title()


def client_label(conf: dict, stem: str) -> str:
    c = (conf.get("client") or "").strip()
    if not c:
        parts = stem.split()
        c = " ".join(parts[:-1]) if len(parts) > 1 else stem
    return c.replace("-", " ").replace("_", " ").strip().title() or "Client"


def short_type(raw: str) -> str:
    t = (raw or "").lower()
    if "prod" in t:
        return "prod"
    if "dev" in t:
        return "dev"
    return raw or "?"


def menu(services):
    """House-style numbered picker over keyring service names."""
    print()
    bar = ANSI["cyan"] + "=" * 60 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}Credential bank{ANSI['reset']}  "
          f"{ANSI['dim']}(OS keyring vault){ANSI['reset']}")
    print(ANSI["cyan"] + "-" * 60 + ANSI["reset"])
    for i, name in enumerate(services, 1):
        print(f"  {ANSI['bold']}[{i}]{ANSI['reset']} "
              f"{ANSI['yellow']}{pretty_name(name):<20}{ANSI['reset']} "
              f"{ANSI['dim']}{name}{ANSI['reset']}")
    print(bar)
    raw = input(
        f"\nChoose a credential set by number "
        f"({ANSI['cyan']}1{ANSI['reset']}-{ANSI['cyan']}{len(services)}{ANSI['reset']}), "
        "blank to quit: "
    ).strip()
    if not raw or not raw.isdigit():
        return None
    idx = int(raw)
    if 1 <= idx <= len(services):
        return services[idx - 1]
    logger.warning(f"Choice {idx} out of range.")
    return None


def _default_prod(sandboxes):
    for sb in sandboxes:
        if sb.get("name") == "prod":
            return sb
    for sb in sandboxes:
        if short_type(sb.get("type", "")) == "prod":
            return sb
    return sandboxes[0] if sandboxes else None


def pick_sandbox(sandboxes):
    """Prompt for ONE sandbox to read. Enter = prod (default)."""
    default = _default_prod(sandboxes)
    print()
    bar = ANSI["cyan"] + "=" * 60 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}Sandboxes visible to this credential{ANSI['reset']}")
    print(ANSI["cyan"] + "-" * 60 + ANSI["reset"])
    for i, sb in enumerate(sandboxes, 1):
        name = sb.get("name", "?")
        title = sb.get("title") or name
        env = short_type(sb.get("type", ""))
        star = " (default)" if sb is default else ""
        print(f"  {ANSI['bold']}[{i:>2}]{ANSI['reset']} "
              f"{ANSI['yellow']}{title:<22}{ANSI['reset']} "
              f"{ANSI['dim']}{name:<16}{ANSI['reset']} {env}{star}")
    print(bar)
    dlabel = default.get("name", "?") if default else "?"
    raw = input(
        f"\nPick a sandbox ({ANSI['cyan']}1{ANSI['reset']}, or Enter for "
        f"{ANSI['green']}{dlabel}{ANSI['reset']}): "
    ).strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(sandboxes):
        return sandboxes[int(raw) - 1]
    logger.warning(f"Invalid choice {raw!r}; using {dlabel}.")
    return default


def resolve_sandboxes_arg(arg: str, sandboxes):
    """Map a --sandbox=<names|all> value to a LIST of sandbox dicts. Each token
    matches by exact name first, then by substring of name/title."""
    tokens = [t for t in arg.replace(",", " ").split() if t]
    if any(t.lower() == "all" for t in tokens):
        return list(sandboxes)
    by_name = {sb.get("name"): sb for sb in sandboxes}
    out = []
    for t in tokens:
        if by_name.get(t):
            out.append(by_name[t])
            continue
        hits = [sb for sb in sandboxes
                if t.lower() in (sb.get("name") or "").lower()
                or t.lower() in (sb.get("title") or "").lower()]
        if hits:
            out.append(hits[0])
        else:
            logger.warning(f"--sandbox: no sandbox matching {t!r}; ignoring.")
    # de-dupe, preserve order
    seen, uniq = set(), []
    for sb in out:
        if sb.get("name") not in seen:
            seen.add(sb.get("name"))
            uniq.append(sb)
    return uniq


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
def run(service: str, sandbox_arg, *, profile_only=True, limit=DEFAULT_CLASS_LIMIT,
        warn_at=DEFAULT_WARN_AT, to_csv=False, expect=EXPECTED_ENABLED,
        debug=False, exclude_system=False):
    try:
        conf = aep_creds.load_creds(service)
    except aep_creds.CredsError as e:
        logger.error(f"Failed to load credentials for {service!r}: {e}")
        return
    client = client_label(conf, service)
    logger.info(f"Authenticating as {pretty_name(service)}...")
    try:
        tok = authenticate(conf)
    except urllib.error.HTTPError as e:
        detail = flatten_err(e.read().decode(errors="replace"))
        logger.error(f"IMS auth failed: HTTP {e.code}: {detail}")
        logger.error("Check client_id / client_secret / scopes for "
                     f"{service!r}, and that the credential is not expired.")
        return
    except Exception as e:
        logger.error(f"IMS auth failed: {type(e).__name__}: {e}")
        return
    token = tok.get("access_token")
    if not token:
        logger.error("IMS returned no access_token.")
        return

    ok, sandboxes = list_sandboxes(token, conf)
    if not ok:
        logger.error(f"Could not list sandboxes: {sandboxes}")
        return
    if not sandboxes:
        logger.error("This credential can see no sandboxes.")
        return

    if sandbox_arg:
        chosen = resolve_sandboxes_arg(sandbox_arg, sandboxes)
        if not chosen:
            logger.error(f"--sandbox={sandbox_arg!r} matched nothing.")
            return
    else:
        one = pick_sandbox(sandboxes)
        if not one:
            logger.info("No sandbox chosen. Exiting.")
            return
        chosen = [one]

    all_rows = []
    for sb in chosen:
        name = sb.get("name")
        title = sb.get("title") or name
        try:
            rows = census(token, conf, name, profile_only, exclude_system)
        except urllib.error.HTTPError as e:
            detail = flatten_err(e.read().decode(errors="replace"))
            logger.error(f"{name}: Catalog read failed -- HTTP {e.code}: {detail}")
            continue
        if debug and rows:
            print_debug_table(rows, f"{client} / {title}")
        print_report(rows, f"{client} / {title}", limit, warn_at, profile_only,
                     expect=expect if profile_only else None)
        all_rows.extend(rows)

    if to_csv and all_rows:
        write_csv(all_rows, client)


def print_banner() -> None:
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}Dataset Census v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {ANSI['dim']}Counts profile-enabled datasets and groups them by "
          f"XDM base class.{ANSI['reset']}")
    print(bar)
    print(f"{ANSI['dim']}{aep_creds.source_banner()}{ANSI['reset']}")


def main():
    print_banner()
    sandbox_arg = None
    profile_only = True
    to_csv = False
    limit = DEFAULT_CLASS_LIMIT
    warn_at = DEFAULT_WARN_AT
    expect = EXPECTED_ENABLED
    exclude_system = False
    debug = os.environ.get("DATASET_CENSUS_DEBUG", "").strip().lower() in (
        "1", "true", "yes", "on")
    positional = []
    for a in sys.argv[1:]:
        if a.startswith("--sandbox="):
            sandbox_arg = a.split("=", 1)[1]
        elif a in ("--all-datasets", "--all"):
            profile_only = False
        elif a in ("--exclude-system", "--no-system"):
            exclude_system = True
        elif a == "--csv":
            to_csv = True
        elif a.startswith("--limit="):
            try:
                limit = max(1, int(a.split("=", 1)[1]))
            except ValueError:
                logger.warning(f"Ignoring bad --limit value in {a!r}.")
        elif a.startswith("--warn="):
            try:
                warn_at = max(1, int(a.split("=", 1)[1]))
            except ValueError:
                logger.warning(f"Ignoring bad --warn value in {a!r}.")
        elif a.startswith("--expect="):
            val = a.split("=", 1)[1].strip().lower()
            if val in ("off", "none", "0"):
                expect = None       # different sandbox, no UI number to check
            else:
                try:
                    expect = max(1, int(val))
                except ValueError:
                    logger.warning(f"Ignoring bad --expect value in {a!r}.")
        elif a in ("--debug", "-v"):
            debug = True
            logger.setLevel(logging.DEBUG)
        elif a.startswith("-"):
            continue
        else:
            positional.append(a)

    if warn_at > limit:
        logger.warning(f"--warn={warn_at} is above --limit={limit}; "
                       f"clamping warn to {limit}.")
        warn_at = limit

    services = aep_creds.list_services()
    if not services:
        logger.error("No credentials found in the keyring vault or the creds/ "
                     "folder. Add one with credential_validator_v2.py store "
                     "(or migrate), or drop a <service>.json in creds/.")
        return

    if positional:
        try:
            chosen = aep_creds.pick_service(positional[0])
        except aep_creds.CredsError as e:
            logger.error(str(e))
            return
    else:
        chosen = menu(services)

    if not chosen:
        logger.info("Nothing chosen. Exiting.")
        return

    run(chosen, sandbox_arg, profile_only=profile_only, limit=limit,
        warn_at=warn_at, to_csv=to_csv, expect=expect, debug=debug,
        exclude_system=exclude_system)


if __name__ == "__main__":
    main()
