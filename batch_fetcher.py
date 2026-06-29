#!/usr/bin/env python3
"""
batch_fetcher.py
================
Lists recent AEP batches in the configured sandbox, prompts the user for a
batch ID, and downloads every file in that batch to the current working
directory. Replaces the earlier auth.py / authandret.py / fetchbatch.py trio.

VDI-friendly: stdlib only, no pip install required.

At startup it picks a credential set from ./creds/ (the same credential bank
used by credential_validator.py / batch_eval_timing.py), mints a fresh Adobe
IMS token, then lists and downloads failed batches.

Usage:
    python batch_fetcher.py                    # interactive cred menu
    python batch_fetcher.py prod               # pick creds/prod.json by stem
    python batch_fetcher.py prod --sandbox=dev # override sandbox
    python batch_fetcher.py prod <batch_id>    # process a known batch ID
"""

from __future__ import annotations

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

SCRIPT_NAME = "batch_fetcher"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-06-24"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
CATALOG_URL = "https://platform.adobe.io/data/foundation/catalog/batches"
EXPORT_BATCHES_URL = "https://platform.adobe.io/data/foundation/export/batches"
EXPORT_FILES_URL = "https://platform.adobe.io/data/foundation/export/files"
DEFAULT_REGION = "GBR9"
DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

# Enable ANSI escape processing on Windows cmd.exe (modern terminals already
# support it; this is a no-op elsewhere).
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
logger = logging.getLogger("aep_batch_fetcher")
SSL_CTX = ssl._create_unverified_context()


def banner(conf, sandbox):
    """Print script identity plus which org and sandbox we're about to hit."""
    org = conf.get("org_id", "?")
    region = (conf.get("region") or DEFAULT_REGION).strip()
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {ANSI['dim']}List recent failed AEP batches and download their failed-record files.{ANSI['reset']}")
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


def list_recent_batches(headers, hours=168, limit=20, status="failure"):
    start = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
    params = urllib.parse.urlencode({
        "limit": limit,
        "createdAfter": start,
        "orderBy": "desc:created",
        "status": status,
    })
    body = http(f"{CATALOG_URL}?{params}", headers=headers)
    return json.loads(body) or {}


def print_batches(batches):
    """Render a numbered table; returns the ordered list of batch IDs."""
    rows = list(batches.items())
    bar = ANSI["cyan"] + "-" * 110 + ANSI["reset"]
    print()
    print(ANSI["cyan"] + "=" * 110 + ANSI["reset"])
    print(
        ANSI["bold"]
        + f"  #   {'BATCH ID':<27}{'CREATED (UTC)':<22}{'DATASET':<28}{'IN/FAIL':<10}ERROR"
        + ANSI["reset"]
    )
    print(bar)
    for i, (batch_id, info) in enumerate(rows, 1):
        ts = info.get("created", 0) / 1000
        created = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        status = info.get("status", "?")
        related = info.get("relatedObjects") or []
        dataset = related[0].get("id", "-") if related else "-"
        metrics = info.get("metrics") or {}
        recs_in = metrics.get("inputRecordCount", "-")
        recs_fail = metrics.get("failedRecordCount", "-")
        errs = info.get("errors") or []
        err_code = errs[0].get("code", "") if errs else ""
        id_color = ANSI["red"] if status == "failure" else ANSI["green"]
        print(
            f"  {ANSI['bold']}{i:>2}{ANSI['reset']}  "
            f"{id_color}{batch_id:<27}{ANSI['reset']}"
            f"{ANSI['dim']}{created:<22}{ANSI['reset']}"
            f"{dataset:<28}"
            f"{recs_in}/{recs_fail:<8}"
            f"{ANSI['yellow']}{err_code}{ANSI['reset']}"
        )
    print(ANSI["cyan"] + "=" * 110 + ANSI["reset"])
    return [bid for bid, _ in rows]


def pick_batches(ids):
    """Prompt for batch selection; returns a list of batch IDs to process."""
    prompt = (
        f"\nPick batch(es) by number (e.g. {ANSI['cyan']}1{ANSI['reset']}, "
        f"{ANSI['cyan']}1,3{ANSI['reset']}, {ANSI['cyan']}all{ANSI['reset']}), "
        f"or paste a batch ID. Blank to quit: "
    )
    raw = input(prompt).strip()
    if not raw:
        return []
    if raw.lower() == "all":
        return list(ids)
    selected = []
    for token in raw.replace(",", " ").split():
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(ids):
                selected.append(ids[idx])
            else:
                logger.warning(f"Index {token} out of range; skipping.")
        else:
            selected.append(token)  # treat as a raw batch ID
    return selected


DEFAULT_DOWNLOAD_ROOT = Path(__file__).resolve().parent / "output" / "failed_batches"


def get_batch_detail(headers, batch_id):
    body = http(f"{CATALOG_URL}/{batch_id}", headers=headers)
    info = json.loads(body) or {}
    # Catalog returns either {batchId: {...}} or the bare batch object.
    return info.get(batch_id, info if isinstance(info, dict) else {})


def print_batch_errors(batch_id, detail):
    status = detail.get("status", "?")
    metrics = detail.get("metrics") or {}
    errors = detail.get("errors") or []
    color = ANSI["red"] if status == "failure" else ANSI["green"]
    print()
    print(f"  {ANSI['bold']}Batch:{ANSI['reset']}    {batch_id}")
    print(f"  {ANSI['bold']}Status:{ANSI['reset']}   {color}{status}{ANSI['reset']}")
    print(
        f"  {ANSI['bold']}Records:{ANSI['reset']}  "
        f"input={metrics.get('inputRecordCount','-')}, "
        f"failed={metrics.get('failedRecordCount','-')}, "
        f"output={metrics.get('outputRecordCount','-')}"
    )
    if errors:
        print(f"  {ANSI['bold']}{ANSI['red']}Catalog errors:{ANSI['reset']}")
        for err in errors:
            code = err.get("code", "?")
            desc = err.get("description", "")
            print(f"    {ANSI['red']}[{code}]{ANSI['reset']} {desc}")
            rows = err.get("rows") or []
            if rows:
                shown = rows[:10]
                more = "..." if len(rows) > 10 else ""
                print(f"      affected rows: {shown}{more}")
    else:
        print(f"  {ANSI['bold']}Catalog errors:{ANSI['reset']} (none)")
    print()


def walk_failed(headers, batch_id):
    """Recursively walk /export/batches/{batchId}/failed.

    The endpoint is hierarchical: folders have length="0" and JSON-listing
    children; leaves have length>0 and return raw bytes. Yields
    (relative_path, fetch_url, length_bytes) per leaf.
    """
    root_url = f"{EXPORT_BATCHES_URL}/{batch_id}/failed"
    queue = [("", root_url)]
    while queue:
        prefix, url = queue.pop()
        body = http(url, headers=headers)
        listing = json.loads(body) or {}
        for entry in listing.get("data", []):
            name = entry.get("name", "")
            length = int(entry.get("length", 0) or 0)
            child_url = ((entry.get("_links") or {}).get("self") or {}).get("href") or ""
            child_path = f"{prefix}/{name}" if prefix else name
            if length == 0:
                queue.append((child_path, child_url))
            else:
                yield (child_path, child_url, length)


def download_failed_tree(headers, batch_id, root: Path = DEFAULT_DOWNLOAD_ROOT):
    """Mirror the batch's /failed tree into root/<batchId>/<rel_path>."""
    out_dir = root / batch_id
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for rel_path, url, length in walk_failed(headers, batch_id):
        dest = out_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Downloading {rel_path} ({length:,} bytes)")
        content = http(url, headers=headers)
        dest.write_bytes(content)
        logger.info(f"Saved {len(content):,} bytes to {dest}")
        saved.append(dest)
    return saved


def peek_error_payload(path: Path, max_bytes=200_000):
    """If the file is small JSON/text with error-shaped fields, return them."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > max_bytes:
        return None
    head = raw.lstrip()[:1]
    if head not in (b"{", b"["):
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(data, dict):
        for key in ("error", "errors", "errorMessage", "error_message",
                    "_error", "_errors", "errorCode", "error_code", "message"):
            if key in data:
                return {key: data[key]}
    return None


def process_batch(headers, batch_id):
    """Pull catalog errors + /failed tree for one batch and surface findings."""
    try:
        detail = get_batch_detail(headers, batch_id)
        print_batch_errors(batch_id, detail)
    except Exception as e:
        logger.warning(f"Could not fetch catalog detail: {e}")

    try:
        saved = download_failed_tree(headers, batch_id)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.error(
                f"Batch {batch_id} has no /failed tree (404). "
                f"It may not be a failed batch, or has no recoverable rows."
            )
            return
        raise

    if not saved:
        logger.warning(f"No failed-record files retrieved for {batch_id}.")
        return

    logger.info(f"Downloaded {len(saved)} file(s) under {DEFAULT_DOWNLOAD_ROOT / batch_id}")

    # Peek inside each downloaded file for any embedded error info.
    found_embedded = False
    for path in saved:
        embedded = peek_error_payload(path)
        if embedded:
            found_embedded = True
            print(f"  {ANSI['bold']}{ANSI['red']}Embedded error in {path.relative_to(DEFAULT_DOWNLOAD_ROOT)}:{ANSI['reset']}")
            snippet = json.dumps(embedded, indent=2, default=str)
            for line in snippet.splitlines()[:20]:
                print(f"    {line}")
    if not found_embedded:
        logger.info(
            "No row-level error payload embedded in downloaded files "
            "(this is normal for whole-file failures - see Catalog errors above)."
        )


def parse_args(argv):
    """Tiny stdlib-only CLI parser. The FIRST positional is the credential set
    name (creds/<name>.json stem); any further positionals are batch IDs.
    --sandbox=NAME overrides the sandbox."""
    sandbox_override = None
    name = None
    ids = []
    for a in argv:
        if a.startswith("--sandbox="):
            sandbox_override = a.split("=", 1)[1].strip() or None
        elif a.startswith("-"):
            continue
        elif name is None:
            name = a  # creds stem
        else:
            ids.append(a)
    return sandbox_override, name, ids


def main():
    sandbox_override, name, cli_ids = parse_args(sys.argv[1:])

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
                     "Pass a credential name, e.g. `batch_fetcher prod`.")
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
    banner(conf, sandbox)

    token = authenticate(conf)
    headers = aep_headers(token, conf, sandbox)

    # Allow batch IDs from the command line; fall back to interactive prompt.
    if cli_ids:
        for bid in cli_ids:
            logger.info(f"=== Processing {bid} ===")
            process_batch(headers, bid)
        logger.info("Done.")
        return

    ids = []
    try:
        batches = list_recent_batches(headers)
        if batches:
            ids = print_batches(batches)
        else:
            logger.info("No FAILED batches found in the last 7 days.")
    except Exception as e:
        logger.warning(f"Could not list recent batches ({e}). You can still paste a known batch ID below.")

    selected = pick_batches(ids)
    if not selected:
        logger.info("Nothing selected. Exiting.")
        return
    for bid in selected:
        logger.info(f"=== Processing {bid} ===")
        process_batch(headers, bid)
    logger.info("Done.")


if __name__ == "__main__":
    main()
