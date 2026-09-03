#!/usr/bin/env python3
"""
overlap_report.py  --  AEP Addressable Audience cut-list
========================================================
Pull the Profile store's overlap/distribution reports and rank what to cut to
get back under the Addressable Audience licence.

The problem this solves
-----------------------
Addressable Audience is licensed on merged profile count. When you go over, the
question is never "how many profiles do we have" -- it's "which dataset do I
expire to claw back the overage, without touching profiles some other dataset
also needs?". The dataset overlap report answers exactly that, but it answers it
as a wall of comma-separated dataset IDs and raw integers. This tool turns it
into a ranked cut-list with a running total, so you can see where the overage
clears.

How the dataset overlap report actually works
---------------------------------------------
`GET /previewsamplestatus/report/dataset/overlap` returns `reportTimestamp` plus
a `data` object whose KEYS are comma-separated dataset ID combinations and whose
VALUES are profile counts. Each row is the set of profiles contributed by
EXACTLY that combination of datasets -- the rows are mutually exclusive, so they
sum to the total profile count. The identity graph is already applied; merge
policy is NOT.

  * A SINGLE-ID row ("5d92...": 4_000_000) is profiles that ONLY that dataset
    contributes. Expire it and you lose those profiles -- nothing else is
    holding them up. These are the primary cut candidates, and the only rows
    where the count is also the saving.
  * A MULTI-ID row ("5d92...,5da7...": 900_000) is profiles both datasets
    contribute. Expiring either one alone saves NOTHING here -- the profile
    survives on the other dataset. Shown in --csv, never in the cut-list.

That distinction is the whole point of the tool: summing every row a dataset
appears in overstates its saving, sometimes wildly.

A caveat on `identity`
----------------------
Adobe documents no identity/namespace OVERLAP endpoint -- only
`/previewsamplestatus/report/namespace`, which is a namespace DISTRIBUTION. Its
rows are NOT mutually exclusive: one profile carries multiple namespaces, so the
values legitimately sum to MORE than the total profile count, and no row is a
"saving". The `identity` subcommand reports that distribution honestly and
refuses to present it as a cut-list. See the header it prints.

Verified against Experience League (Sept 2026):
  https://experienceleague.adobe.com/en/docs/experience-platform/profile/tutorials/dataset-overlap-report
  https://experienceleague.adobe.com/en/docs/experience-platform/profile/api/preview-sample-status

Dataset name resolution
-----------------------
IDs resolve to names from the Catalog API by default (one paged sweep, no
truncation). `--dataset-list FILE.xlsx` reads names from a spreadsheet instead
-- any sheet with an ID-ish and a name-ish column. Unresolved IDs always pass
through raw and are never dropped.

Read-only: it never creates, edits or deletes anything in AEP.

Credentials come from the OS keyring (Windows Credential Manager) via aep_creds,
with a plaintext creds/*.json fallback; manage them with credential_validator_v2.py.
HTTPS_PROXY and REQUESTS_CA_BUNDLE are honoured for Zscaler-style interception.

Usage:
    python overlap_report.py dataset aep-prod --sandbox=prod
    python overlap_report.py dataset aep-prod --sandbox=prod --date=2026-09-01
    python overlap_report.py dataset aep-prod --licensed=62500000
    python overlap_report.py dataset aep-prod --csv                # full report
    python overlap_report.py dataset aep-prod --json=output/raw.json
    python overlap_report.py identity aep-prod --sandbox=prod
"""
import argparse
import csv
import json
import logging
import os
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
SCRIPT_NAME = "overlap_report"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-09-03"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
PLATFORM = "https://platform.adobe.io"
UPS = f"{PLATFORM}/data/core/ups"
PREVIEW_URL = f"{UPS}/previewsamplestatus"
DATASET_OVERLAP_URL = f"{PREVIEW_URL}/report/dataset/overlap"
# The namespace DISTRIBUTION report, whose rows are not mutually exclusive.
NAMESPACE_URL = f"{PREVIEW_URL}/report/namespace"
# Undocumented as of Sept 2026 -- absent from the preview-sample-status guide,
# the OpenAPI reference and the pseudonymous-profiles page, though the latter
# cross-links an "identity overlap report" anchor that does not resolve. The
# `probe` subcommand exists to settle empirically whether they are live.
NAMESPACE_OVERLAP_URL = f"{PREVIEW_URL}/report/namespace/overlap"
UNSTITCHED_URL = f"{PREVIEW_URL}/report/unstitchedProfiles"
DATASET_DIST_URL = f"{PREVIEW_URL}/report/dataset"
DATASETS_URL = f"{PLATFORM}/data/foundation/catalog/dataSets"

DEFAULT_SANDBOX = "prod"
DEFAULT_SERVICE = "aep-prod"
DEFAULT_LICENSED = 62_500_000    # Addressable Audience entitlement
HIGH_THRESHOLD = 1_000_000       # single-dataset rows at/above this are HIGH
STALE_REPORT_DAYS = 7            # warn if the sample is older than this
CATALOG_PAGE = 100               # paging only -- every page is read, nothing truncated

DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

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


# ----------------------------------------------------------------------------
# HTTP / IMS / credential helpers
# ----------------------------------------------------------------------------
def build_ssl_context(insecure: bool) -> ssl.SSLContext:
    """Verify against REQUESTS_CA_BUNDLE / SSL_CERT_FILE when either is set --
    that is how a Zscaler-style intercepting proxy is trusted properly. Only
    fall back to an unverified context when asked, or when no bundle is
    available (the estate's proxy would otherwise fail every call)."""
    if insecure:
        logger.warning("TLS verification DISABLED (--insecure).")
        return ssl._create_unverified_context()

    bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if bundle:
        if Path(bundle).is_file():
            logger.info(f"TLS verified against {Path(bundle).name}")
            return ssl.create_default_context(cafile=bundle)
        logger.warning(f"CA bundle not found, ignoring: {bundle}")

    try:
        ctx = ssl.create_default_context()
        ctx.load_default_certs()
        return ctx
    except Exception as exc:                                # pragma: no cover
        logger.warning(f"Falling back to unverified TLS: {exc}")
        return ssl._create_unverified_context()


def build_opener(ctx: ssl.SSLContext) -> urllib.request.OpenerDirector:
    """One opener carrying BOTH the proxy config and the TLS context -- they have
    to travel together, or honouring REQUESTS_CA_BUNDLE would quietly drop the
    proxy (or vice versa). urllib reads HTTPS_PROXY/HTTP_PROXY/NO_PROXY from the
    environment via getproxies(); building it explicitly lets us log which proxy
    is actually in play."""
    proxies = urllib.request.getproxies()
    shown = ", ".join(f"{k}={v}" for k, v in sorted(proxies.items())
                      if k in ("http", "https"))
    if shown:
        logger.info(f"Proxy in use: {shown}")
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        urllib.request.HTTPSHandler(context=ctx),
    )


OPENER: urllib.request.OpenerDirector | None = None


def http(url, method="GET", headers=None, data=None, timeout=120):
    """Stdlib HTTP. Returns response bytes; raises HTTPError on 4xx/5xx."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    opener = OPENER or urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as r:
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
# Formatting helpers
# ----------------------------------------------------------------------------
def commas(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def to_dt(value) -> datetime | None:
    """Parse the timestamp shapes AEP uses into an aware UTC datetime: epoch ms,
    epoch seconds, or ISO-8601. None if unparsable."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        raw = float(value)
        if raw > 1e11:          # milliseconds
            raw /= 1000.0
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "(unknown)"


# ----------------------------------------------------------------------------
# Dataset name resolution
# ----------------------------------------------------------------------------
def names_from_catalog(headers) -> dict[str, str]:
    """Every dataset id -> name, via a full paged Catalog sweep. Paging only;
    no row is truncated."""
    out: dict[str, str] = {}
    start = 0
    while True:
        url = f"{DATASETS_URL}?limit={CATALOG_PAGE}&start={start}&properties=name"
        try:
            data = json.loads(http(url, headers=headers))
        except urllib.error.HTTPError as e:
            logger.warning(f"Catalog lookup stopped at start={start}: HTTP {e.code}. "
                           "Unresolved IDs will show raw.")
            break
        except Exception as exc:
            logger.warning(f"Catalog lookup failed: {exc}. Unresolved IDs show raw.")
            break
        if not isinstance(data, dict) or not data:
            break
        for dsid, ds in data.items():
            if isinstance(ds, dict):
                out[dsid] = ds.get("name") or dsid
        if len(data) < CATALOG_PAGE:
            break
        start += CATALOG_PAGE
    logger.info(f"Catalog resolved {len(out):,} dataset name(s).")
    return out


def names_from_xlsx(path: Path) -> dict[str, str]:
    """Read id -> name from a spreadsheet. Finds the header row and the two
    columns that look like an ID and a name; tolerant of layout changes."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        logger.warning("openpyxl not installed -- ignoring --dataset-list. "
                       "pip install openpyxl")
        return {}
    if not path.is_file():
        logger.warning(f"--dataset-list not found, ignoring: {path}")
        return {}

    out: dict[str, str] = {}
    wb = load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        id_col = name_col = None
        for row in rows:
            if row is None:
                continue
            cells = [str(c).strip().lower() if c is not None else "" for c in row]
            for i, c in enumerate(cells):
                if id_col is None and c in ("id", "dataset id", "datasetid",
                                            "dataset_id"):
                    id_col = i
                if name_col is None and c in ("name", "dataset name",
                                              "datasetname", "dataset_name"):
                    name_col = i
            if id_col is not None and name_col is not None:
                break                       # header found; the rest are data
        if id_col is None or name_col is None:
            continue
        for row in rows:
            if row is None or len(row) <= max(id_col, name_col):
                continue
            dsid, name = row[id_col], row[name_col]
            if dsid and name:
                out[str(dsid).strip()] = str(name).strip()
    wb.close()
    logger.info(f"Spreadsheet resolved {len(out):,} dataset name(s) "
                f"from {path.name}.")
    return out


# ----------------------------------------------------------------------------
# Report fetch
# ----------------------------------------------------------------------------
class NoReport(Exception):
    """No report exists to return. Not an error state.

    404 "Report not found" is the documented answer for a date with no report.
    Observed against prod, an UNDATED request for a report that has never been
    generated answers 500 "Could not retrieve report" instead -- same meaning,
    different status, so both land here rather than being reported as failures."""


def fetch_report(headers, url: str, date: str | None) -> dict:
    full = f"{url}?date={urllib.parse.quote(date)}" if date else url
    logger.info(f"GET {full}")
    try:
        return json.loads(http(full, headers=headers))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code == 404:
            raise NoReport(date or "most recent") from None
        if e.code == 500 and "could not retrieve report" in detail.lower():
            raise NoReport(date or "most recent") from None
        raise RuntimeError(f"HTTP {e.code} from {full}: {detail}") from None


def _as_int(val) -> int | None:
    """The sample-status endpoint returns numbers as strings, and some of them
    arrive double-quoted ('"4515164787"'). Coerce anything integer-ish."""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        cleaned = val.strip().strip('"').strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def fetch_total_profiles(headers) -> int | None:
    """Total merged profiles from the last sample status, for the sum check.
    `totalRows` is the profile count; `totalFragmentCount` counts fragments and
    `cosmosDocCount` counts store documents, so neither is the right number."""
    try:
        data = json.loads(http(PREVIEW_URL, headers=headers))
    except Exception as exc:
        logger.debug(f"Could not read {PREVIEW_URL}: {exc}")
        return None
    for key in ("totalRows", "totalProfileCount", "profileCount", "totalProfiles"):
        n = _as_int(data.get(key))
        if n and n > 0:
            return n
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------
def build_rows_distribution(data: list, kind: str) -> list[dict]:
    """The DISTRIBUTION reports (/report/dataset, /report/namespace) return a
    LIST of row objects, not the overlap report's {comma-separated-ids: count}
    map. Each row already carries its own label, so no ID resolution is needed.

    Rows here are per-entity, NOT mutually exclusive: a profile present in two
    datasets is counted in both, so these counts are never savings."""
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        n = _as_int(item.get("fullIDsCount"))
        if n is None:
            continue
        # dataset rows carry name/value(id); namespace rows carry code/value.
        label = (item.get("name") or item.get("code") or item.get("value")
                 or "(unnamed)")
        rows.append({
            "ids": [str(item.get("value") or label)],
            "names": [label],
            "labels": [label],
            "count": n,
            "exclusive": False,      # distribution rows are never exclusive
            "unresolved": 0,
            "pct": item.get("fullIDsPercentage"),
            "sample_count": _as_int(item.get("sampleCount")),
            "description": item.get("description") or "",
            "kind": kind,
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def build_rows(data: dict, names: dict[str, str]) -> list[dict]:
    """One row per key in `data`. Keys are comma-separated dataset IDs."""
    rows = []
    for key, count in data.items():
        ids = [p.strip() for p in str(key).split(",") if p.strip()]
        try:
            n = int(count)
        except (TypeError, ValueError):
            logger.warning(f"Non-numeric count for {key!r}: {count!r} -- skipped.")
            continue
        resolved = [names.get(i, "") for i in ids]
        rows.append({
            "ids": ids,
            "names": resolved,
            # Unresolved IDs pass through raw rather than being dropped.
            "labels": [nm or i for i, nm in zip(ids, resolved)],
            "count": n,
            "exclusive": len(ids) == 1,
            "unresolved": sum(1 for nm in resolved if not nm),
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


# ----------------------------------------------------------------------------
# Console output
# ----------------------------------------------------------------------------
def print_header(title, sandbox, report_dt, requested_date, stale_days):
    C = ANSI
    bar = C["cyan"] + "=" * 78 + C["reset"]
    print(bar)
    print(f"  {C['bold']}{title}{C['reset']}   {C['dim']}sandbox={sandbox}{C['reset']}")
    print(bar)
    print(f"  {C['bold']}reportTimestamp:{C['reset']} "
          f"{C['magenta']}{fmt_dt(report_dt)}{C['reset']}"
          + (f"   {C['dim']}(requested {requested_date}){C['reset']}"
             if requested_date else ""))
    if stale_days is not None and stale_days > STALE_REPORT_DAYS:
        print(f"  {C['yellow']}{C['bold']}[WARN]{C['reset']} "
              f"{C['yellow']}This sample is {stale_days} days old "
              f"(> {STALE_REPORT_DAYS}). The cut-list may not reflect the "
              f"current store.{C['reset']}")
    print()


def print_cut_list(rows, licensed, total, high_threshold, smallest_first=False):
    """Exclusive rows only, desc by count, with a cumulative running total and a
    marker at the point the overage clears.

    Descending is the default because it ranks by contribution. Note it makes
    the marker land on row 1 whenever the biggest dataset alone exceeds the
    overage -- true, but the most destructive option. --smallest-first inverts
    the order so the marker instead shows the least-collateral set that clears
    it."""
    C = ANSI
    exclusive = [r for r in rows if r["exclusive"]]
    shared = [r for r in rows if not r["exclusive"]]
    if smallest_first:
        exclusive = sorted(exclusive, key=lambda r: r["count"])

    overage = (total - licensed) if total is not None else None

    print(f"  {C['bold']}Licence position{C['reset']}")
    print(f"    Licensed          {commas(licensed):>16}")
    if total is not None:
        pct = (total / licensed * 100) if licensed else 0
        colour = C["red"] if overage and overage > 0 else C["green"]
        print(f"    Addressable       {colour}{commas(total):>16}{C['reset']}"
              f"   {colour}({pct:.1f}%){C['reset']}")
        if overage and overage > 0:
            print(f"    {C['bold']}Overage to clear  "
                  f"{C['red']}{commas(overage):>16}{C['reset']}")
        else:
            print(f"    {C['green']}Within licence -- nothing to cut.{C['reset']}")
    else:
        print(f"    Addressable       {C['dim']}(unknown -- profile total "
              f"unavailable){C['reset']}")
    print()

    if not exclusive:
        print(f"  {C['yellow']}No single-dataset rows in this report -- every "
              f"profile is contributed by more than one dataset, so no single "
              f"expiry saves anything.{C['reset']}")
        return exclusive, shared

    order = ("smallest first -- least collateral" if smallest_first
             else "largest first")
    print(f"  {C['bold']}Cut-list -- exclusive contribution per dataset{C['reset']}"
          f"   {C['dim']}({order}){C['reset']}")
    print(f"  {C['dim']}Profiles ONLY this dataset contributes. Expiring it saves "
          f"exactly this many.{C['reset']}")
    print(f"  {C['dim']}Rows where two or more datasets share the profiles are "
          f"excluded ({len(shared):,} of them) -- see --csv.{C['reset']}")
    print()
    print(f"  {C['bold']}{'#':>3}  {'PROFILES':>14}  {'CUMULATIVE':>14}  "
          f"{'FLAG':<6} DATASET{C['reset']}")
    print(f"  {C['dim']}{'-' * 74}{C['reset']}")

    cumulative = 0
    cleared_at = None
    for i, r in enumerate(exclusive, 1):
        cumulative += r["count"]
        high = r["count"] >= high_threshold
        flag = f"{C['red']}{C['bold']}HIGH{C['reset']}  " if high else "      "
        label = r["labels"][0]
        raw_note = (f"  {C['dim']}(unresolved id){C['reset']}"
                    if r["unresolved"] else "")
        print(f"  {i:>3}  {C['bold']}{commas(r['count']):>14}{C['reset']}  "
              f"{C['cyan']}{commas(cumulative):>14}{C['reset']}  "
              f"{flag}{label}{raw_note}")
        if (cleared_at is None and overage is not None and overage > 0
                and cumulative >= overage):
            cleared_at = i
            print(f"  {C['green']}{C['bold']}{'':>3}  "
                  f"{'-' * 14}  {'-' * 14}{C['reset']}  "
                  f"{C['green']}{C['bold']}^^ OVERAGE CLEARED HERE "
                  f"({commas(cumulative)} >= {commas(overage)}) -- "
                  f"{i} dataset(s){C['reset']}")

    print(f"  {C['dim']}{'-' * 74}{C['reset']}")
    print(f"  {C['bold']}{len(exclusive):,} exclusive row(s), "
          f"{commas(cumulative)} profiles total.{C['reset']}")
    if overage is not None and overage > 0 and cleared_at is None:
        print(f"  {C['red']}{C['bold']}[WARN]{C['reset']} {C['red']}Expiring EVERY "
              f"dataset above still only recovers {commas(cumulative)} of the "
              f"{commas(overage)} overage.{C['reset']}")
    return exclusive, shared


def print_distribution(rows, total, entity="namespace", note=None):
    """A DISTRIBUTION report -- explicitly NOT a cut-list. See module docstring."""
    C = ANSI
    if note:
        for line in note:
            print(line)
        print()
    print(f"  {C['dim']}Rows here are NOT mutually exclusive -- one profile can "
          f"appear under several {entity}s -- so the values legitimately sum to "
          f"MORE than the{C['reset']}")
    print(f"  {C['dim']}profile total, and no single row is a saving. Ranked by "
          f"size for visibility; do not read it as a cut-list.{C['reset']}")
    print()
    print(f"  {C['bold']}{'#':>3}  {'PROFILES':>14}  {'SHARE':>7}  "
          f"{entity.upper()}{C['reset']}")
    print(f"  {C['dim']}{'-' * 74}{C['reset']}")
    running = 0
    for i, r in enumerate(rows, 1):
        running += r["count"]
        pct = r.get("pct")
        share = f"{pct * 100:.1f}%" if isinstance(pct, (int, float)) else ""
        print(f"  {i:>3}  {C['bold']}{commas(r['count']):>14}{C['reset']}  "
              f"{C['cyan']}{share:>7}{C['reset']}  {', '.join(r['labels'])}")
    print(f"  {C['dim']}{'-' * 74}{C['reset']}")
    print(f"  {len(rows):,} {entity}(s); values sum to {commas(running)}.")
    if total is not None:
        rel = "above" if running > total else "below"
        print(f"  {C['dim']}Profile total is {commas(total)} -- a sum {rel} this "
              f"is expected here, not an error.{C['reset']}")


def check_sum(rows, total) -> bool:
    """The dataset overlap rows are mutually exclusive, so they MUST sum to the
    total profile count. Warn loudly when they do not."""
    C = ANSI
    got = sum(r["count"] for r in rows)
    if total is None:
        logger.warning("Could not read the profile total, so the "
                       "rows-sum-to-total check was SKIPPED.")
        print(f"  {C['yellow']}[WARN] Sum check skipped -- profile total "
              f"unavailable. Rows sum to {commas(got)}.{C['reset']}\n")
        return False
    if got == total:
        print(f"  {C['green']}[OK] Rows sum to {commas(got)}, matching the "
              f"profile total.{C['reset']}\n")
        return True
    delta = got - total
    pct = abs(delta) / total * 100 if total else 0
    print(f"  {C['red']}{C['bold']}[WARN] ROWS DO NOT SUM TO THE PROFILE TOTAL."
          f"{C['reset']}")
    print(f"  {C['red']}  rows sum   {commas(got):>16}{C['reset']}")
    print(f"  {C['red']}  profiles   {commas(total):>16}{C['reset']}")
    print(f"  {C['red']}  difference {commas(delta):>16}  ({pct:.2f}%){C['reset']}")
    print(f"  {C['red']}  The report may be partial or mid-refresh -- treat the "
          f"cut-list as indicative until this reconciles.{C['reset']}\n")
    logger.warning(f"Overlap rows sum to {got:,} but the profile total is "
                   f"{total:,} (difference {delta:,}).")
    return False


# ----------------------------------------------------------------------------
# Writers
# ----------------------------------------------------------------------------
def write_csv(rows, target: Path, report_dt, sandbox, kind) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([f"# {kind} report", f"sandbox={sandbox}",
                    f"reportTimestamp={fmt_dt(report_dt)}",
                    f"generated={datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"])
        w.writerow(["rank", "row_type", "entity_count", "profiles",
                    "cumulative_exclusive", "share_pct", "flag",
                    "ids", "names", "description"])
        cumulative = 0
        rank = 0
        for r in rows:                       # already sorted desc
            rank += 1
            if r["exclusive"]:
                cumulative += r["count"]
                cum = cumulative
                flag = "HIGH" if r["count"] >= HIGH_THRESHOLD else ""
            else:
                cum = ""
                flag = ""
            pct = r.get("pct")
            # A distribution row is neither exclusive nor shared-overlap; label
            # it for what it is so the CSV can't be misread as savings.
            row_type = ("distribution" if r.get("kind")
                        else "exclusive" if r["exclusive"] else "shared")
            w.writerow([
                rank,
                row_type,
                len(r["ids"]),
                r["count"],
                cum,
                f"{pct * 100:.4f}" if isinstance(pct, (int, float)) else "",
                flag,
                ",".join(r["ids"]),
                " | ".join(r["labels"]),
                r.get("description", ""),
            ])
    return target


def write_json(payload, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


# ----------------------------------------------------------------------------
# probe -- is the whole overlap family gone, or only the dataset report?
# ----------------------------------------------------------------------------
def probe_endpoint(headers, label: str, url: str, date: str | None = None) -> dict:
    """Call one endpoint and RECORD what happened. Never raises for a non-200 --
    a status is the finding here, not a failure."""
    full = f"{url}?date={urllib.parse.quote(date)}" if date else url
    rec = {"label": label, "url": full, "date": date, "status": None,
           "reportTimestamp": None, "count": None, "error": None, "body": None}
    try:
        raw = http(full, headers=headers)
        rec["status"] = 200
        try:
            body = json.loads(raw)
        except ValueError:
            rec["error"] = "200 but body was not JSON"
            return rec
        rec["body"] = body
        if isinstance(body, dict):
            rec["reportTimestamp"] = body.get("reportTimestamp")
            data = body.get("data")
            if isinstance(data, dict):
                rec["count"] = len(data)
            elif isinstance(data, list):
                rec["count"] = len(data)
            else:
                rec["count"] = len(body)
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        rec["body"] = text
        # First line of the error body -- these come back as pretty-printed
        # JSON, so pull the message out when we can.
        msg = ""
        try:
            msg = str(json.loads(text).get("message", "")).strip()
        except Exception:
            msg = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        rec["error"] = msg[:160]
    except Exception as exc:
        rec["status"] = 0
        rec["error"] = f"{type(exc).__name__}: {exc}"[:160]
    return rec


def _probe_line(rec: dict) -> None:
    C = ANSI
    st = rec["status"]
    if st == 200:
        colour, tag = C["green"], "200"
    elif st in (404, 500):
        colour, tag = C["yellow"], str(st)
    elif st == 0:
        colour, tag = C["red"], "ERR"
    else:
        colour, tag = C["red"], str(st)
    when = "dated  " if rec["date"] else "undated"
    ts = rec["reportTimestamp"] or ""
    cnt = "" if rec["count"] is None else f"{rec['count']:,} row/key(s)"
    print(f"    {colour}{C['bold']}{tag:>4}{C['reset']}  {C['dim']}{when}{C['reset']}  "
          f"{rec['label']:<34} {cnt:<16} {C['dim']}{ts}{C['reset']}")
    if rec["error"]:
        print(f"          {C['dim']}-> {rec['error']}{C['reset']}")


def print_reconciliation(unstitched_body, total_rows, licensed) -> None:
    """Show the three competing profile numbers side by side WITHOUT picking a
    winner -- which one is authoritative is a question for Adobe, not for this
    tool to assume."""
    C = ANSI
    data = (unstitched_body or {}).get("data") or {}
    tnp = _as_int(data.get("totalNumberOfProfiles"))
    tne = _as_int(data.get("totalNumberOfEvents"))

    print()
    print(f"  {C['bold']}Reconciliation -- three profile numbers, side by side"
          f"{C['reset']}")
    print(f"  {C['dim']}Shown, not adjudicated. Which is authoritative for "
          f"Addressable Audience is an Adobe question.{C['reset']}")
    print()
    rows = [
        ("unstitchedProfiles.totalNumberOfProfiles", tnp,
         "documented as equivalent to the addressable audience count"),
        ("previewsamplestatus.totalRows", total_rows,
         "the sampled Profile-store total"),
        ("licensed entitlement", licensed, "as supplied on the command line"),
    ]
    width = max(len(r[0]) for r in rows)
    for name, val, note in rows:
        shown = commas(val) if val is not None else "(unavailable)"
        print(f"    {name:<{width}}  {C['bold']}{shown:>16}{C['reset']}   "
              f"{C['dim']}{note}{C['reset']}")

    print()
    pairs = [
        ("totalNumberOfProfiles", tnp, "totalRows", total_rows),
        ("totalNumberOfProfiles", tnp, "licensed", licensed),
        ("totalRows", total_rows, "licensed", licensed),
    ]
    for an, a, bn, b in pairs:
        if a is None or b is None:
            continue
        delta = a - b
        colour = C["red"] if delta > 0 else C["green"]
        sign = "+" if delta > 0 else ""
        pct = (delta / b * 100) if b else 0
        print(f"    {an} - {bn}:  {colour}{sign}{commas(delta)}{C['reset']} "
              f"{C['dim']}({sign}{pct:.1f}%){C['reset']}")
    if tne is not None:
        print()
        print(f"    {C['dim']}totalNumberOfEvents: {commas(tne)}{C['reset']}")

    buckets = data.get("unstitchedProfiles")
    if isinstance(buckets, dict) and buckets:
        print()
        print(f"  {C['bold']}Unstitched profiles by age{C['reset']}")
        print(f"    {C['bold']}{'BUCKET':<10} {'PROFILES':>16} {'EVENTS':>18}"
              f"{C['reset']}")
        for key in ("7days", "30days", "60days", "90days", "120days"):
            b = buckets.get(key)
            if not isinstance(b, dict):
                continue
            cp = _as_int(b.get("countOfProfiles"))
            ev = _as_int(b.get("eventsAssociated"))
            print(f"    {key:<10} {commas(cp):>16} {commas(ev):>18}")
            ns = b.get("nsDistribution")
            if isinstance(ns, dict):
                for code, nsv in sorted(
                        ns.items(),
                        key=lambda kv: -(_as_int((kv[1] or {}).get(
                            "countOfProfiles")) or 0)):
                    if not isinstance(nsv, dict):
                        continue
                    ncp = _as_int(nsv.get("countOfProfiles"))
                    nev = _as_int(nsv.get("eventsAssociated"))
                    print(f"      {C['dim']}{code:<8}{C['reset']} "
                          f"{commas(ncp):>16} {commas(nev):>18}")


def run_probe(headers, sandbox, date, licensed, json_target) -> int:
    """Hit every endpoint in the family and report what each one does."""
    C = ANSI
    # An explicit recent date, because undated and dated behave differently:
    # undated currently 500s while dated 404s, and both are worth recording.
    probe_date = date or (datetime.now(timezone.utc).date()
                          - timedelta(days=1)).isoformat()

    print()
    bar = C["cyan"] + "=" * 78 + C["reset"]
    print(bar)
    print(f"  {C['bold']}Preview-sample-status family probe{C['reset']}   "
          f"{C['dim']}sandbox={sandbox}  dated-attempts={probe_date}{C['reset']}")
    print(bar)
    print()

    print(f"  {C['bold']}Baseline -- endpoints known to work here{C['reset']}")
    base = [
        probe_endpoint(headers, "previewsamplestatus", PREVIEW_URL),
        probe_endpoint(headers, "report/dataset", DATASET_DIST_URL),
        probe_endpoint(headers, "report/namespace", NAMESPACE_URL),
    ]
    for rec in base:
        _probe_line(rec)

    print()
    print(f"  {C['bold']}Overlap family{C['reset']}")
    fam = [
        probe_endpoint(headers, "report/dataset/overlap", DATASET_OVERLAP_URL),
        probe_endpoint(headers, "report/dataset/overlap", DATASET_OVERLAP_URL,
                       probe_date),
        probe_endpoint(headers, "report/namespace/overlap", NAMESPACE_OVERLAP_URL),
        probe_endpoint(headers, "report/namespace/overlap", NAMESPACE_OVERLAP_URL,
                       probe_date),
        # No date parameter is documented for this one.
        probe_endpoint(headers, "report/unstitchedProfiles", UNSTITCHED_URL),
    ]
    for rec in fam:
        _probe_line(rec)

    def ok(label) -> bool:
        return any(r["status"] == 200 for r in fam if r["label"].endswith(label))

    ds_ok = ok("dataset/overlap")
    ns_ok = ok("namespace/overlap")
    un_ok = ok("unstitchedProfiles")

    print()
    print(f"  {C['bold']}Verdict{C['reset']}")

    # If the BASELINE endpoints are locked out too, we cannot see this sandbox
    # at all and know nothing about the overlap family here. Saying anything
    # about the family would be a false finding -- and this output is meant to
    # go into a support ticket.
    denied = {401, 403}
    if all(r["status"] in denied for r in base):
        print(f"    {C['red']}{C['bold']}NO ACCESS TO THIS SANDBOX{C['reset']} "
              f"{C['red']}-- every endpoint including the baseline returns "
              f"403/401, so this run says nothing about the overlap family."
              f"{C['reset']}")
        print(f"    {C['dim']}Add the technical account to a product profile "
              f"covering '{sandbox}', then re-run. Compare with a sandbox where "
              f"the baseline answers.{C['reset']}")
    elif not (ds_ok or ns_ok or un_ok):
        print(f"    {C['red']}{C['bold']}FAMILY RETIRED{C['reset']} {C['red']}-- "
              f"all three overlap-family endpoints fail in this sandbox. The "
              f"problem is the family, not the dataset report.{C['reset']}")
    elif un_ok and not (ds_ok or ns_ok):
        print(f"    {C['yellow']}{C['bold']}DATASET/NAMESPACE OVERLAP BROKEN IN "
              f"THIS ORG{C['reset']} {C['yellow']}-- unstitchedProfiles answers, "
              f"so the family is live and only the two overlap reports are "
              f"missing.{C['reset']}")
    elif ns_ok and not ds_ok:
        print(f"    {C['yellow']}{C['bold']}DATASET OVERLAP ONLY{C['reset']} "
              f"{C['yellow']}-- namespace/overlap answers, so the overlap "
              f"machinery works and only the dataset report is missing."
              f"{C['reset']}")
    else:
        got = ", ".join(n for n, v in (("dataset/overlap", ds_ok),
                                       ("namespace/overlap", ns_ok),
                                       ("unstitchedProfiles", un_ok)) if v)
        print(f"    {C['green']}{C['bold']}PARTIAL/WORKING{C['reset']} "
              f"{C['green']}-- answering: {got}.{C['reset']}")

    if un_ok:
        body = next(r["body"] for r in fam
                    if r["label"].endswith("unstitchedProfiles")
                    and r["status"] == 200)
        status_body = base[0]["body"] if base[0]["status"] == 200 else {}
        total_rows = _as_int((status_body or {}).get("totalRows"))
        print_reconciliation(body, total_rows, licensed)

    if json_target is not None:
        target = resolve_target(json_target, f"probe_{sandbox}")
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        dump = {
            "probedAt": datetime.now(timezone.utc).isoformat(),
            "sandbox": sandbox,
            "datedAttempts": probe_date,
            "endpoints": [
                {k: v for k, v in r.items() if k != "body"} | {"body": r["body"]}
                for r in base + fam
            ],
        }
        written = write_json(dump, target)
        print()
        logger.info(f"Raw probe JSON written: {written}")

    print()
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog=f"{SCRIPT_NAME}.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Rank AEP dataset overlap into an Addressable Audience "
                    "cut-list.")
    ap.add_argument("--version", action="version",
                    version=f"{SCRIPT_NAME} {SCRIPT_VERSION} ({SCRIPT_DATE})")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("service", nargs="?", default=None,
                        help=f"Keyring service name (default: {DEFAULT_SERVICE}). "
                             "Omit for an interactive menu.")
    common.add_argument("--sandbox", default=DEFAULT_SANDBOX,
                        help=f"Sandbox name (default: {DEFAULT_SANDBOX}).")
    common.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                        help="Report date. Omit for the most recent.")
    common.add_argument("--csv", nargs="?", const="", default=None, metavar="PATH",
                        help="Write the FULL report (exclusive + shared) to CSV.")
    common.add_argument("--json", nargs="?", const="", default=None, metavar="PATH",
                        help="Write the raw API response to JSON.")
    common.add_argument("--dataset-list", default=None, metavar="XLSX",
                        help="Resolve dataset names from a spreadsheet instead "
                             "of the Catalog API.")
    common.add_argument("--licensed", type=int, default=DEFAULT_LICENSED,
                        help=f"Addressable Audience entitlement "
                             f"(default: {DEFAULT_LICENSED:,}).")
    common.add_argument("--high", type=int, default=HIGH_THRESHOLD,
                        help=f"Flag exclusive rows at/above this as HIGH "
                             f"(default: {HIGH_THRESHOLD:,}).")
    common.add_argument("--smallest-first", action="store_true",
                        help="Rank the cut-list ascending, so the marker shows "
                             "the SMALLEST set of datasets that clears the "
                             "overage (least collateral) rather than the "
                             "largest single cut.")
    common.add_argument("--distribution", action="store_true",
                        help="Use the per-dataset DISTRIBUTION report instead of "
                             "the overlap report. Its rows are not mutually "
                             "exclusive, so it cannot produce savings -- but it "
                             "is available when no overlap report exists.")
    common.add_argument("--insecure", action="store_true",
                        help="Disable TLS verification (last resort; prefer "
                             "REQUESTS_CA_BUNDLE).")

    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("dataset", parents=[common],
                   help="Dataset overlap report -> ranked cut-list.")
    sub.add_parser("identity", parents=[common],
                   help="Identity namespace distribution (NOT an overlap "
                        "report -- see --help notes).")
    sub.add_parser("probe", parents=[common],
                   help="Call every preview-sample-status endpoint and report "
                        "what each does: is the whole overlap family gone, or "
                        "only the dataset report?")
    return ap.parse_args(argv)


def resolve_target(opt, default_name: str) -> Path:
    """--csv / --json with no value land in output/ with a stamped name."""
    if opt:
        return Path(opt)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"{default_name}_{stamp}"


def main() -> int:
    global OPENER
    args = parse_args(sys.argv[1:])

    OPENER = build_opener(build_ssl_context(args.insecure))

    print(aep_creds.source_banner(), file=sys.stderr)
    services = aep_creds.list_services()
    if not services:
        logger.error("No credentials found in the keyring vault or the creds/ "
                     "folder. Add one with credential_validator_v2.py store.")
        return 1

    wanted = args.service or (DEFAULT_SERVICE if DEFAULT_SERVICE in services
                              else None)
    if wanted:
        try:
            chosen = aep_creds.pick_service(wanted)
        except aep_creds.CredsError as e:
            logger.error(str(e))
            return 1
    elif not sys.stdin.isatty():
        logger.error("No credential service given and no TTY to prompt. Pass the "
                     f"service name. Options: {', '.join(services)}")
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

    try:
        token = authenticate(conf)
    except Exception as exc:
        logger.error(f"Authentication failed: {exc}")
        return 1
    headers = aep_headers(token, conf, args.sandbox)

    # `probe` is a diagnostic and shares nothing with the report paths below.
    if args.command == "probe":
        return run_probe(headers, args.sandbox, args.date, args.licensed,
                         args.json)

    is_dataset = args.command == "dataset"
    want_overlap = is_dataset and not args.distribution
    if want_overlap:
        url, title = (DATASET_OVERLAP_URL,
                      "Dataset overlap -- Addressable Audience cut-list")
    elif is_dataset:
        url, title = (f"{PREVIEW_URL}/report/dataset",
                      "Dataset distribution (not an overlap report)")
    else:
        url, title = NAMESPACE_URL, "Identity namespace distribution"

    try:
        payload = fetch_report(headers, url, args.date)
    except NoReport as which:
        # No report is information, not failure -- 404 for a dated request,
        # 500 "Could not retrieve report" for one that was never generated.
        C = ANSI
        print()
        print(f"  {C['yellow']}{C['bold']}No {'overlap ' if want_overlap else ''}"
              f"report exists for {which} in sandbox '{args.sandbox}'."
              f"{C['reset']}")
        if want_overlap:
            print()
            print(f"  {C['dim']}The dataset overlap report is not generated "
                  f"automatically -- it has to be enabled for the org, and this "
                  f"one has none.{C['reset']}")
            print(f"  {C['dim']}The per-dataset distribution report IS available "
                  f"here. It cannot show savings (its rows are not mutually "
                  f"exclusive), but it does{C['reset']}")
            print(f"  {C['dim']}rank what each dataset contributes:{C['reset']}")
            print()
            print(f"      {C['cyan']}python {SCRIPT_NAME}.py dataset "
                  f"{chosen} --sandbox={args.sandbox} --distribution{C['reset']}")
        else:
            print(f"  {C['dim']}Try another --date, or omit --date for the most "
                  f"recent.{C['reset']}")
        print()
        return 0
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    data = payload.get("data")
    if not isinstance(data, (dict, list)):
        logger.error(f"Unexpected response shape: 'data' is "
                     f"{type(data).__name__}, expected object or list "
                     f"(keys: {', '.join(map(str, payload.keys())) or 'none'}).")
        return 1

    report_dt = to_dt(payload.get("reportTimestamp"))
    stale_days = ((datetime.now(timezone.utc) - report_dt).days
                  if report_dt else None)

    # The overlap report is a {ids: count} map needing name resolution; the
    # distribution reports are a list of rows that already carry their labels.
    if isinstance(data, dict):
        names = (names_from_xlsx(Path(args.dataset_list)) if args.dataset_list
                 else names_from_catalog(headers))
        rows = build_rows(data, names)
    else:
        rows = build_rows_distribution(data, args.command)

    total = fetch_total_profiles(headers)

    print()
    print_header(title, args.sandbox, report_dt, args.date, stale_days)

    if want_overlap:
        check_sum(rows, total)
        print_cut_list(rows, args.licensed, total, args.high,
                       smallest_first=args.smallest_first)
    elif is_dataset:
        note = [
            f"  {ANSI['yellow']}{ANSI['bold']}[NOTE]{ANSI['reset']} "
            f"{ANSI['yellow']}This is the dataset DISTRIBUTION report, not the "
            f"overlap report -- it cannot tell you what expiring a dataset "
            f"saves.{ANSI['reset']}",
        ]
        print_distribution(rows, total, entity="dataset", note=note)
    else:
        note = [
            f"  {ANSI['yellow']}{ANSI['bold']}[NOTE]{ANSI['reset']} "
            f"{ANSI['yellow']}Adobe documents no identity/namespace OVERLAP "
            f"endpoint. This is the namespace DISTRIBUTION report.{ANSI['reset']}",
        ]
        print_distribution(rows, total, entity="namespace", note=note)

    unresolved = sum(1 for r in rows if r["unresolved"])
    if unresolved:
        print()
        logger.info(f"{unresolved:,} row(s) contain an ID with no name -- shown "
                    "raw, never dropped.")

    if args.csv is not None:
        target = resolve_target(args.csv, f"overlap_{args.command}_{args.sandbox}")
        if target.suffix.lower() != ".csv":
            target = target.with_suffix(".csv")
        written = write_csv(rows, target, report_dt, args.sandbox, args.command)
        print()
        logger.info(f"CSV written: {written}")

    if args.json is not None:
        target = resolve_target(args.json, f"overlap_{args.command}_{args.sandbox}")
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        written = write_json(payload, target)
        logger.info(f"Raw JSON written: {written}")

    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        sys.exit(130)
