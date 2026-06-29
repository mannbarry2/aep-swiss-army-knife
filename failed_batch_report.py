#!/usr/bin/env python3
"""
failed_batch_report.py
======================
Exports a CSV summary of every AEP batch that FAILED in the last N hours
(default 24) in the configured sandbox. Use this for a quick estate-wide
health snapshot; use batch_fetcher.py when you need to drill into one
batch and download its failed-record files.

VDI-friendly: stdlib only, no pip install required. Picks a credential set
from ./creds/ (the same credential bank used by batch_fetcher.py /
credential_validator.py / batch_eval_timing.py).

Usage:
    python failed_batch_report.py                       # interactive cred menu, last 24h
    python failed_batch_report.py prod                  # pick creds/prod.json by stem
    python failed_batch_report.py prod --hours=72 --sandbox=prod

Generated reports are written under ./output/ (gitignored).
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

# ============================================================================
# CONFIG
# ----------------------------------------------------------------------------
# Credentials come from a JSON file in ./creds/ (the shared credential bank).
# Required keys:
#   client_id     -- Adobe IMS client ID
#   client_secret -- IMS client_credentials secret
#   org_id        -- Adobe org ID (e.g. "ABC@AdobeOrg")
# Optional keys (sensible defaults applied):
#   api_key       -- AEP x-api-key (defaults to client_id)
#   oauth_url     -- IMS token endpoint
#   scopes        -- IMS scopes (comma-separated)
#   sandbox       -- "all" or a specific sandbox name
#   sandbox_names -- list used when `sandbox == "all"`; "prod" wins if present
#   region        -- AEP region header value (defaults to "GBR9")
# Underscored keys (e.g. "_comment_1") are treated as inline documentation
# and ignored by the loader.
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
CREDS_DIR = SCRIPT_DIR / "creds"

SCRIPT_NAME = "failed_batch_report"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-06-24"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
CATALOG_URL = "https://platform.adobe.io/data/foundation/catalog/batches"
DEFAULT_REGION = "GBR9"
DEFAULT_HOURS = 24
PAGE_LIMIT = 100  # AEP catalog page size cap
DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

# Enable ANSI escape processing on Windows cmd.exe (no-op on modern terminals).
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11; ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        h = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
}
LEVEL_COLOR = {
    "DEBUG": ANSI["dim"],
    "INFO": ANSI["green"],
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
logger = logging.getLogger("failed_batch_report")
SSL_CTX = ssl._create_unverified_context()

DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "output"


def banner(conf, sandbox, hours):
    """Print script identity plus the org/sandbox/region context."""
    org = conf.get("org_id", "?")
    region = (conf.get("region") or DEFAULT_REGION).strip()
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {ANSI['dim']}CSV report of every AEP batch that failed in the last {hours}h.{ANSI['reset']}")
    print(f"  {ANSI['bold']}Org:{ANSI['reset']}      {ANSI['magenta']}{org}{ANSI['reset']}")
    print(f"  {ANSI['bold']}Sandbox:{ANSI['reset']}  {ANSI['yellow']}{sandbox}{ANSI['reset']}")
    print(f"  {ANSI['bold']}Region:{ANSI['reset']}   {ANSI['blue']}{region}{ANSI['reset']}")
    print(bar)


# ----------------------------------------------------------------------------
# Credential bank (shared shape with credential_validator.py / batch_eval_timing.py)
# ----------------------------------------------------------------------------
def discover_creds():
    """Return ordered list of credential JSON paths in ./creds/ (skip example)."""
    paths = []
    if CREDS_DIR.exists():
        for p in sorted(CREDS_DIR.glob("*.json")):
            if p.stem == "example":
                continue
            paths.append(p)
    return paths


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


def pick_sandbox(conf):
    """AEP needs a single sandbox per request; resolve from config."""
    sandbox = conf.get("sandbox")
    names = conf.get("sandbox_names") or []
    if sandbox and sandbox != "all":
        return sandbox
    if len(names) == 1:
        return names[0]
    if "prod" in names:
        return "prod"
    if names:
        return names[0]
    return "prod"


def http(url, method="GET", headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, context=SSL_CTX) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"HTTP {e.code} {method} {url}: {body}")
        raise


def authenticate(conf):
    """Mint a fresh client_credentials access token against Adobe IMS."""
    logger.info("Authenticating with Adobe IMS...")
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
    token = json.loads(body)["access_token"]
    logger.info("Authentication successful.")
    return token


def aep_headers(token, conf, sandbox):
    region = (conf.get("region") or DEFAULT_REGION).strip()
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "x-sandbox-name": sandbox,
        "x-adp-region": region,
        "x-device-region": region,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }


def fetch_failed_batches(headers, hours):
    """Page through /catalog/batches pulling every failed batch in the window."""
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ms = int(
        (datetime.now(tz=timezone.utc) - timedelta(hours=hours)).timestamp() * 1000
    )
    all_batches: dict = {}
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "status": "failure",
            "createdAfter": start_ms,
            "createdBefore": end_ms,
            "limit": PAGE_LIMIT,
            "offset": offset,
            "orderBy": "desc:created",
        })
        logger.info(f"Fetching failed batches (offset {offset})...")
        body = http(f"{CATALOG_URL}?{params}", headers=headers)
        page = json.loads(body) or {}
        if not page:
            break
        all_batches.update(page)
        logger.info(f"Retrieved {len(page)} batch(es) this page.")
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    logger.info(f"Total failed batches in last {hours}h: {len(all_batches)}")
    return all_batches


def _fmt_ts(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (TypeError, ValueError):
        return ""


def write_report(batches, sandbox, root: Path = DEFAULT_OUTPUT_ROOT):
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = root / f"failed_batches_{sandbox}_{stamp}.csv"

    # Determine the max number of related objects so columns are stable.
    max_related = 0
    for info in batches.values():
        max_related = max(max_related, len(info.get("relatedObjects") or []))

    fieldnames = ["Batch ID", "Status", "Created", "Updated",
                  "Input Records", "Failed Records"]
    for i in range(1, max_related + 1):
        fieldnames += [f"Related Object {i} Type", f"Related Object {i} ID"]

    with open(out_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for batch_id, info in batches.items():
            metrics = info.get("metrics") or {}
            row = {
                "Batch ID": batch_id,
                "Status": info.get("status", ""),
                "Created": _fmt_ts(info.get("created")),
                "Updated": _fmt_ts(info.get("updated")),
                "Input Records": metrics.get("inputRecordCount", ""),
                "Failed Records": metrics.get("failedRecordCount", ""),
            }
            for idx, obj in enumerate(info.get("relatedObjects") or [], 1):
                row[f"Related Object {idx} Type"] = obj.get("type", "")
                row[f"Related Object {idx} ID"] = obj.get("id", "")
            writer.writerow(row)

    logger.info(f"Report written: {out_file}  ({len(batches)} row(s))")
    return out_file


def parse_args(argv):
    """Stdlib-only CLI: a positional credential name (creds/<name>.json stem)
    plus --sandbox=NAME --hours=N."""
    sandbox_override = None
    hours = DEFAULT_HOURS
    name = None
    for a in argv:
        if a.startswith("--sandbox="):
            sandbox_override = a.split("=", 1)[1].strip() or None
        elif a.startswith("--hours="):
            try:
                hours = int(a.split("=", 1)[1])
            except ValueError:
                logger.warning(f"Ignoring invalid --hours value: {a}")
        elif a.startswith("-"):
            continue
        elif name is None:
            name = a  # creds stem
    return sandbox_override, hours, name


def main():
    sandbox_override, hours, name = parse_args(sys.argv[1:])

    creds = discover_creds()
    if not creds:
        logger.error(f"No credential JSONs found in {CREDS_DIR}. "
                     f"Drop your <tenant>.json files there.")
        return

    # Resolve which credential set to use: by stem on the CLI, else the menu
    # (only when interactive). Non-interactive with no name is an error.
    if name:
        by_stem = {p.stem: p for p in creds}
        path = by_stem.get(name)
        if not path:
            logger.error(f"No credential set named {name!r} in {CREDS_DIR}")
            return
    elif sys.stdin.isatty():
        path = menu(creds)
    else:
        logger.error("No credential set given and not interactive. "
                     "Pass a credential name, e.g. `failed_batch_report prod`.")
        return
    if not path:
        logger.info("Nothing chosen. Exiting.")
        return

    try:
        conf = load_creds(path)
    except Exception as e:
        logger.error(f"Failed to load {path.name}: {e}")
        return

    sandbox = sandbox_override or pick_sandbox(conf)
    banner(conf, sandbox, hours)

    token = authenticate(conf)
    headers = aep_headers(token, conf, sandbox)

    batches = fetch_failed_batches(headers, hours)
    if not batches:
        logger.info(f"No failed batches in the last {hours}h. Nothing to report.")
        return
    write_report(batches, sandbox)
    logger.info("Done.")


if __name__ == "__main__":
    main()
