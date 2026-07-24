#!/usr/bin/env python3
"""
credential_validator_v2.py  (AEP Swiss Army Knife)
==================================================
Credential Validator v2 -- manage and validate Adobe AEP OAuth
Server-to-Server credentials that live in the **Windows Credential Manager**
(via the `keyring` library) instead of plaintext JSON on disk.

This is the keyring-backed successor to credential_validator.py. Same rich
AEP probing, but no secrets ever touch the filesystem. It is the migration
tool: import your legacy creds/*.json into Credential Manager, `validate`
that they still work, then scrub the plaintext folder.

Storage model
-------------
Each credential *set* is a keyring "service" (default: "aep-prod"). Use
--service to name others, e.g. "aep-dev" for a second sandbox/org. Within a
service, each field (client_id, client_secret, org_id, ...) is one keyring
entry. A tiny registry entry ("aep-cred-index") tracks which services exist,
because keyring itself can't enumerate them.

Run with NO command for the original interactive experience: pick a stored
credential, pick a sandbox (Enter = prod), then sweep every AEP API surface and
report what's reachable. (If keyring is empty it offers to migrate creds/ first.)

Commands
--------
    migrate   Bulk-import every legacy creds/*.json into Credential Manager
              (service name = filename stem). Non-destructive: the plaintext
              files stay put until you delete them yourself.

    store     Prompt (no echo) for client_id / client_secret / org_id /
              tech_account_id / scopes (+ optional api_key, sandbox) and save
              them under --service. Or one-off import a legacy JSON:
                  credential_validator_v2.py store --service aep-prod \
                      --import-json "C:\\creds\\auth.json"
              Never writes any value back to disk; reminds you to delete the
              source folder afterward.

    validate  Pull creds from keyring, mint a token against IMS token/v3
              (client_credentials), print expiry in minutes, then make one
              lightweight authenticated call (GET .../sandboxes) and print
              HTTP status + sandbox count as proof of life. --deep adds the
              full probe (token decode, Query Service, product-surface sweep,
              AJO). On failure: a human-readable diagnosis.
                  credential_validator_v2.py validate --service aep-prod
                  credential_validator_v2.py validate --all --deep

    list      Show which keys exist under a service, values masked
              (first 4 chars + ****).

    delete    Remove all entries for a service after a y/n confirm.

    sweep     Discover EVERY credential service in the vault (the cred-index
              registry PLUS a live Windows Credential Manager scan for keyring's
              `key@service` entries), check which of the 5 core keys each holds
              and -- for the ones with the keys IMS needs -- attempt a token
              mint (throttled 1/sec). Prints a service | keys | OK/FAIL/
              INCOMPLETE table. Read-only; values masked; makes no changes.
                  credential_validator_v2.py sweep   # exit 0 if all pass, 1 if any fail

    purge     Run a sweep, then for each FAILED or INCOMPLETE service offer to
              delete all its keys (y/N, then type the service name to confirm).
              --dry-run previews the targets without changing anything.

Exit codes (so other scripts can use this as a pre-flight check)
    0  ok / token minted     1  auth failure     2  config missing

Dependencies: requests, keyring.
    pip install requests keyring
Author: Barry Mann (barrymann.com)
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    import keyring
    import keyring.errors
    import aep_creds  # shared keyring layer (single source of truth)
    _DEPS_OK = True
    _DEPS_ERR = ""
except ImportError as _e:  # pragma: no cover - reported cleanly in main()
    _DEPS_OK = False
    _DEPS_ERR = str(_e)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_NAME    = "credential_validator_v2"
SCRIPT_VERSION = "2.2.0"
SCRIPT_DATE    = "2026-07-24"
SCRIPT_AUTHOR  = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent

# IMS OAuth Server-to-Server (client_credentials) token endpoint.
IMS_TOKEN_V3_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
# IMS profile carries projectedProductContext for the technical account.
IMS_PROFILE_URL  = "https://ims-na1.adobelogin.com/ims/profile/v1"

PLATFORM         = "https://platform.adobe.io"
SANDBOX_LIST_URL = f"{PLATFORM}/data/foundation/sandbox-management/sandboxes"
QUERIES_URL      = f"{PLATFORM}/data/foundation/query/queries"

# Field model, registry, scopes, and the legacy creds/ dir all come from the
# shared aep_creds layer so there's ONE source of truth. Names kept local so the
# existing call sites below are unchanged. (aep_creds is imported above; on a
# deps-missing box _DEPS_OK is False and main() reports before any use.)
if _DEPS_OK:
    DEFAULT_SCOPES   = aep_creds.DEFAULT_SCOPES
    REGISTRY_SERVICE = aep_creds.REGISTRY_SERVICE
    REGISTRY_KEY     = aep_creds.REGISTRY_KEY
    DEFAULT_SERVICE  = aep_creds.DEFAULT_SERVICE
    REQUIRED_KEYS    = aep_creds.REQUIRED_KEYS
    OPTIONAL_KEYS    = aep_creds.OPTIONAL_KEYS
    ALL_KEYS         = aep_creds.ALL_KEYS
    CREDS_DIR        = aep_creds.CREDS_DIR   # legacy plaintext dir, for migrate

# Interactive `store` prompts. (label, required). oauth_url is intentionally
# not prompted -- it defaults to IMS token/v3; import-json still preserves it.
STORE_FIELDS = [
    ("client_id",       True,  "Client ID"),
    ("client_secret",   True,  "Client secret"),
    ("org_id",          True,  "IMS Org ID (…@AdobeOrg)"),
    ("tech_account_id", False, "Technical account ID (optional, Enter to skip)"),
    ("scopes",          False, "Scopes (optional, Enter for default)"),
    ("api_key",         False, "x-api-key (optional, Enter = same as client_id)"),
    ("sandbox",         False, "Default sandbox (optional, Enter = prod)"),
]

# Product API surfaces swept by --deep. One lightweight GET each -- we only
# care whether the service "stood up" for this credential.
#   (label, url, sandbox_scoped, extra_headers, best_effort)
PRODUCT_SURFACES = [
    ("Query Service",
     f"{PLATFORM}/data/foundation/query/queries?limit=1", True, {}, False),
    ("Catalog Service",
     f"{PLATFORM}/data/foundation/catalog/dataSets?limit=1", True, {}, False),
    ("Schema Registry",
     f"{PLATFORM}/data/foundation/schemaregistry/tenant/schemas?limit=1",
     True, {"Accept": "application/vnd.adobe.xed-id+json"}, False),
    ("Flow Service (Sources)",
     f"{PLATFORM}/data/foundation/flowservice/connectionSpecs?limit=1",
     False, {}, False),
    ("Data Ingestion (batches)",
     f"{PLATFORM}/data/foundation/catalog/batches?limit=1", True, {}, False),
    ("Real-Time Profile",
     f"{PLATFORM}/data/core/ups/config/mergePolicies?limit=1", True, {}, False),
    ("Identity Service",
     f"{PLATFORM}/data/core/idnamespace/identities", False, {}, False),
    ("Privacy Service",
     f"{PLATFORM}/data/core/privacy/jobs", False, {}, False),
    ("Customer Journey Analytics",
     "https://cja.adobe.io/data/dataviews?limit=1", False, {}, False),
    ("Adobe Journey Optimizer (Journeys)",
     "https://journey.adobe.io/authoring/journeys", True, {}, False),
    ("Offer Decisioning",
     f"{PLATFORM}/data/core/xcore/", True, {}, True),
]

# TLS verification. Flipped off by --insecure (corporate proxy MITM).
VERIFY_TLS = True

# ----------------------------------------------------------------------------
# ANSI / logging - matches credential_validator.py style
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
    def format(self, record: logging.LogRecord) -> str:
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


def print_banner() -> None:
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {ANSI['dim']}Keyring-backed AEP credential store + validator. "
          f"No secrets on disk.{ANSI['reset']}")
    print(bar)


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def mask(value: str | None, keep: int = 4) -> str:
    """Mask a secret for display: first `keep` chars + '****'. Short values are
    fully starred so we never reveal a tiny secret in whole."""
    if not value:
        return f"{ANSI['dim']}(absent){ANSI['reset']}"
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return f"{s[:keep]}****"


def shorten(s: str | None, n: int = 12) -> str:
    if not s:
        return "?"
    return s if len(s) <= n else f"{s[:n]}..."


def clean_detail(raw: str | bytes, limit: int = 140) -> str:
    """Collapse a (possibly HTML) error body into one safe inline string.
    str.split() with no args splits on ANY whitespace -- including the CR in
    gateway error pages that would otherwise reset the terminal cursor."""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    return " ".join(text.split())[:limit]


def is_placeholder(value: str) -> bool:
    """True for template values like '<adobe-ims-client-id>' in example.json."""
    return bool(re.match(r"^\s*<.*>\s*$", str(value)))


# ----------------------------------------------------------------------------
# Keyring layer -- the primitives live in aep_creds now (single source of
# truth); aliased here so this admin tool's call sites stay unchanged. NOTE:
# these are the raw keyring/registry primitives, NOT aep_creds.load_creds --
# this tool deliberately reads the VAULT ONLY (never the folder fallback), so
# `store`/`sweep`/`purge` operate on exactly what's in Credential Manager.
if _DEPS_OK:
    kr_set          = aep_creds.kr_set
    kr_get          = aep_creds.kr_get
    kr_del          = aep_creds.kr_del
    registry_list   = aep_creds.registry_list
    registry_add    = aep_creds.registry_add
    registry_remove = aep_creds.registry_remove


def load_creds(service: str) -> dict[str, str]:
    """Read all present fields for a service from the VAULT into a config dict
    (stripped). Keyring-only by design -- sweep/purge report on what's actually
    in Credential Manager, so this must not fall back to the plaintext folder
    the way aep_creds.load_creds does."""
    conf: dict[str, str] = {}
    for key in ALL_KEYS:
        val = kr_get(service, key)
        if val:
            conf[key] = val.strip()
    return conf


# ----------------------------------------------------------------------------
# IMS auth + HTTP
# ----------------------------------------------------------------------------
class IMSAuthError(Exception):
    """IMS token endpoint returned a non-200. Carries the parsed error."""

    def __init__(self, status: int, error: str, description: str, raw: str):
        self.status = status
        self.error = error
        self.description = description
        self.raw = raw
        super().__init__(f"HTTP {status}: {error} - {description}")


def _parse_ims_error(resp: "requests.Response") -> tuple[str, str]:
    """Pull (error, error_description) out of an IMS error response."""
    try:
        body = resp.json()
        return (str(body.get("error", "")),
                str(body.get("error_description", "") or body.get("message", "")))
    except ValueError:
        return "", clean_detail(resp.text, 300)


def authenticate(conf: dict) -> dict:
    """Mint an access token via IMS client_credentials. Raises IMSAuthError on
    a non-200, or requests exceptions on a transport failure."""
    url = conf.get("oauth_url") or IMS_TOKEN_V3_URL
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": conf["client_id"],
            "client_secret": conf["client_secret"],
            "scope": conf.get("scopes") or DEFAULT_SCOPES,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        verify=VERIFY_TLS,
    )
    if resp.status_code != 200:
        error, description = _parse_ims_error(resp)
        raise IMSAuthError(resp.status_code, error, description, resp.text)
    return resp.json()


def diagnose_ims(e: IMSAuthError) -> list[str]:
    """Translate an IMS auth failure into human-readable guidance lines."""
    err = (e.error or "").lower()
    desc = (e.description or "").lower()
    lines = [f"IMS rejected the credential (HTTP {e.status}: "
             f"{e.error or 'error'})."]

    if "scope" in err or "scope" in desc:
        lines.append("=> MISSING / INVALID SCOPE. The requested scopes aren't "
                     "granted to this integration. Fix the stored `scopes` "
                     "(store --service ...) or add the scope to the project in "
                     "the Adobe Developer Console.")
    elif "invalid_client" in err or e.status in (400, 401):
        if "expired" in desc:
            lines.append("=> EXPIRED CLIENT SECRET. Generate a fresh secret in "
                         "the Developer Console (Credentials → OAuth S2S) and "
                         "re-store it. Adobe S2S secrets expire.")
        elif "secret" in desc:
            lines.append("=> BAD CLIENT SECRET. The stored client_secret is "
                         "wrong (or was rotated). Re-store the current secret.")
        elif "client_id" in desc or "client id" in desc or "api_key" in desc:
            lines.append("=> BAD CLIENT ID. The stored client_id doesn't match "
                         "any integration in this org.")
        else:
            lines.append("=> INVALID CLIENT. IMS can't distinguish a wrong from "
                         "an expired secret here -- both present as "
                         "'invalid_client'. Check the client_secret is current "
                         "(re-copy from the Console; note S2S secrets expire) "
                         "and that the client_id is right.")
    else:
        lines.append("=> Unrecognised auth error. Raw detail below.")

    if e.description:
        lines.append(f"   detail: {clean_detail(e.description, 200)}")
    return lines


def api_headers(token: str, conf: dict, sandbox: str | None = None,
                extra: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "Accept": "application/json",
    }
    if sandbox:
        headers["x-sandbox-name"] = sandbox
    if extra:
        headers.update(extra)
    return headers


def get_sandboxes(token: str, conf: dict) -> tuple[int, list | None, str | None]:
    """Proof-of-life call. Returns (http_status, sandboxes, error).
    On success `sandboxes` is the list and error is None; on failure
    `sandboxes` is None. status is 0 for a transport failure."""
    try:
        resp = requests.get(SANDBOX_LIST_URL, headers=api_headers(token, conf),
                            timeout=30, verify=VERIFY_TLS)
    except requests.RequestException as ex:
        return 0, None, f"{type(ex).__name__}: {ex}"
    if resp.status_code == 200:
        try:
            return 200, resp.json().get("sandboxes") or [], None
        except ValueError:
            return 200, [], None
    return resp.status_code, None, clean_detail(resp.text, 300)


# ----------------------------------------------------------------------------
# Access-token decode + deep probe (mirrors credential_validator.py)
# ----------------------------------------------------------------------------
def _b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg.encode("ascii"))


def decode_access_token(token: str) -> tuple[dict, dict]:
    """Decode (header, payload) from the JWT access token. Signature is NOT
    verified -- this only surfaces scopes/org/expiry already granted."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"Not a decodable access token ({len(parts)} segments)")
    header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
    payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    return header, payload


def _service_codes_from_ppc(ppc) -> list[str]:
    if not isinstance(ppc, list):
        return []
    codes = set()
    for item in ppc:
        if not isinstance(item, dict):
            continue
        ctx = item.get("prodCtx") if isinstance(item.get("prodCtx"), dict) else item
        code = ctx.get("serviceCode")
        if code:
            codes.add(code)
    return sorted(codes)


def fetch_product_contexts(token: str, payload: dict, resp: dict,
                           conf: dict) -> tuple[list[str] | None, str]:
    """Return (serviceCodes, source) of the products this credential reaches,
    checking the token, then the IMS response, then a best-effort profile call."""
    codes = _service_codes_from_ppc(payload.get("projectedProductContext"))
    if codes:
        return codes, "access token"
    codes = _service_codes_from_ppc(resp.get("projectedProductContext"))
    if codes:
        return codes, "IMS token response"
    try:
        r = requests.get(IMS_PROFILE_URL, params={"client_id": conf["client_id"]},
                        headers={"Authorization": f"Bearer {token}",
                                 "Accept": "application/json"},
                        timeout=20, verify=VERIFY_TLS)
        if r.status_code != 200:
            return None, f"IMS profile HTTP {r.status_code}"
        codes = _service_codes_from_ppc(r.json().get("projectedProductContext"))
        return (codes, "IMS profile") if codes else \
               (None, "not present in token or IMS profile")
    except requests.RequestException as ex:
        return None, f"IMS profile {type(ex).__name__}: {ex}"


def list_queries(token: str, conf: dict, sandbox: str) -> tuple[bool, object]:
    """Probe Query Service in one sandbox. (ok, queries|error_string)."""
    try:
        r = requests.get(QUERIES_URL, params={"limit": 10},
                        headers=api_headers(token, conf, sandbox),
                        timeout=30, verify=VERIFY_TLS)
    except requests.RequestException as ex:
        return False, f"{type(ex).__name__}: {ex}"
    if r.status_code == 200:
        try:
            return True, r.json().get("queries") or []
        except ValueError:
            return True, []
    return False, f"HTTP {r.status_code}: {clean_detail(r.text, 300)}"


def _classify_status(code: int, best_effort: bool = False) -> tuple[str, str, str]:
    """Map an HTTP status (0 = transport failure) to a sweep verdict."""
    if 200 <= code < 300:
        return "UP", "green", "reachable + authorized"
    if code in (401, 403):
        return "NO-PERM", "yellow", f"reachable, no access (HTTP {code})"
    if best_effort and code in (0, 400, 404, 405, 406, 415, 422):
        why = f"HTTP {code}" if code > 0 else "no response"
        return "INCONC", "dim", f"inconclusive - endpoint unconfirmed ({why})"
    if code in (400, 404, 405, 406, 415, 422):
        return "UP", "green", f"reachable (HTTP {code})"
    if code == 429:
        return "UP", "green", "reachable (HTTP 429 rate-limited)"
    if code >= 500:
        return "ERR", "red", f"service error (HTTP {code})"
    return "DOWN", "red", "unreachable"


def probe_surface(token: str, conf: dict, url: str, sandbox_scoped: bool,
                  extra: dict, sandbox: str) -> tuple[int, str]:
    """Fire one GET at a product surface. Returns (status, detail)."""
    headers = api_headers(token, conf,
                          sandbox if sandbox_scoped and sandbox else None, extra)
    try:
        r = requests.get(url, headers=headers, timeout=20, verify=VERIFY_TLS)
    except requests.RequestException as ex:
        return 0, f"{type(ex).__name__}: {ex}"
    return r.status_code, "" if r.ok else clean_detail(r.text)


def probe_product_surfaces(token: str, conf: dict, sandbox: str) -> None:
    print()
    sb_note = f" (sandbox-scoped tests use '{sandbox}')" if sandbox else ""
    print(f"  {ANSI['bold']}AEP product API surfaces{ANSI['reset']}"
          f"{ANSI['dim']}{sb_note}{ANSI['reset']}")
    up = inconclusive = 0
    for label, url, sb_scoped, extra, best_effort in PRODUCT_SURFACES:
        code, detail = probe_surface(token, conf, url, sb_scoped, extra, sandbox)
        tag, color, human = _classify_status(code, best_effort)
        if tag == "UP":
            up += 1
        elif tag == "INCONC":
            inconclusive += 1
        line = (f"     {ANSI['yellow']}{label:<28}{ANSI['reset']} "
                f"{ANSI[color]}[{tag}]{ANSI['reset']} {human}")
        if tag in ("ERR", "DOWN", "NO-PERM", "INCONC") and detail:
            line += f" {ANSI['dim']}- {detail}{ANSI['reset']}"
        print(line)
    total = len(PRODUCT_SURFACES)
    summary = f"=> {up}/{total} product surface(s) stood up for this credential."
    if inconclusive:
        summary += (f" ({inconclusive} inconclusive - best-effort endpoint "
                    f"not confirmed.)")
    print(f"  {ANSI['green' if up else 'red']}{summary}{ANSI['reset']}")


def probe_services(token: str, resp: dict, conf: dict,
                   query_sandboxes: list[str], surface_sandbox: str) -> None:
    """The full v1-style probe: token decode, product contexts, Query Service
    (one line per sandbox in `query_sandboxes`), and the product-surface sweep
    (scoped to `surface_sandbox`)."""
    try:
        _hdr, payload = decode_access_token(token)
    except Exception as ex:  # noqa: BLE001 - decode is best-effort
        logger.warning(f"Could not decode access token: {ex}")
        payload = {}

    if payload:
        granted = payload.get("scope", "(no scope claim)")
        print(f"  {ANSI['bold']}Granted scopes:{ANSI['reset']}")
        for s in granted.split(","):
            print(f"     {ANSI['cyan']}- {s.strip()}{ANSI['reset']}")

        codes, src = fetch_product_contexts(token, payload, resp, conf)
        if codes:
            print(f"  {ANSI['bold']}Provisioned products{ANSI['reset']} "
                  f"{ANSI['dim']}(projectedProductContext via {src}):{ANSI['reset']}")
            for c in codes:
                print(f"     {ANSI['cyan']}- {c}{ANSI['reset']}")
        else:
            print(f"  {ANSI['bold']}Provisioned products:{ANSI['reset']} "
                  f"{ANSI['dim']}undetermined ({src}){ANSI['reset']}")

        org_in_token = payload.get("org") or "?"
        mismatch = ("" if org_in_token == conf["org_id"]
                    else f"  {ANSI['yellow']}(mismatch vs config!){ANSI['reset']}")
        print(f"  {ANSI['bold']}Token org:{ANSI['reset']}   {org_in_token}{mismatch}")
        print(f"  {ANSI['bold']}Tech acct:{ANSI['reset']}   "
              f"{payload.get('user_id') or payload.get('aa_id') or '?'}")

    print()
    print(f"  {ANSI['bold']}AEP /query/queries  (Query Service access){ANSI['reset']}")
    any_access = False
    for sb in query_sandboxes:
        ok_q, res_q = list_queries(token, conf, sb)
        if not ok_q:
            print(f"     {ANSI['yellow']}{sb:<20}{ANSI['reset']} "
                  f"{ANSI['red']}[FAIL] {res_q}{ANSI['reset']}")
        else:
            any_access = True
            n = len(res_q)
            print(f"     {ANSI['yellow']}{sb:<20}{ANSI['reset']} "
                  f"{ANSI['green']}[OK] Query Service reachable - "
                  f"{n} recent quer{'y' if n == 1 else 'ies'} listed{ANSI['reset']}")
    print(f"  {ANSI['green']}=> Credential HAS Query Service access.{ANSI['reset']}"
          if any_access else
          f"  {ANSI['red']}=> No Query Service access in any tested sandbox."
          f"{ANSI['reset']}")

    probe_product_surfaces(token, conf, surface_sandbox)


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
def cmd_store(args: argparse.Namespace) -> int:
    """store: interactive getpass entry, or one-off --import-json migration."""
    if args.import_json:
        return _store_from_json(Path(args.import_json),
                                args.service or Path(args.import_json).stem)
    return _store_interactive(args.service or DEFAULT_SERVICE)


def _store_interactive(service: str) -> int:
    print()
    print(f"  {ANSI['bold']}Store credentials for service "
          f"{ANSI['yellow']}{service}{ANSI['reset']}"
          f"{ANSI['dim']} (Windows Credential Manager){ANSI['reset']}")
    print(f"  {ANSI['dim']}Input is hidden. Nothing is written to disk. "
          f"Ctrl-C to abort.{ANSI['reset']}")
    print()

    collected: dict[str, str] = {}
    try:
        for key, required, label in STORE_FIELDS:
            while True:
                val = getpass.getpass(f"  {label}: ").strip()
                if val or not required:
                    break
                print(f"  {ANSI['yellow']}{key} is required.{ANSI['reset']}")
            if val:
                collected[key] = val
    except (KeyboardInterrupt, EOFError):
        print()
        logger.warning("Aborted. Nothing was stored.")
        return 2

    print()
    print(f"  {ANSI['bold']}About to store (masked):{ANSI['reset']}")
    for key in ALL_KEYS:
        if key in collected:
            print(f"     {key:<16} {mask(collected[key])}")
    confirm = input(f"\n  Save to Credential Manager under '{service}'? [y/N] ")
    if confirm.strip().lower() not in ("y", "yes"):
        logger.warning("Not saved.")
        return 0

    for key, value in collected.items():
        kr_set(service, key, value)
    registry_add(service)
    logger.info(f"Stored {len(collected)} field(s) under service '{service}'. "
                f"Validate with: {SCRIPT_NAME}.py validate --service {service}")
    return 0


def import_json_core(path: Path, service: str) -> tuple[list[str], list[str], str | None]:
    """Import one legacy creds JSON into keyring under `service`. Returns
    (stored_keys, skipped_placeholder_keys, error). Never writes back to disk."""
    if not path.exists():
        return [], [], "no such file"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as ex:
        return [], [], f"{type(ex).__name__}: {ex}"

    stored, skipped = [], []
    for key in ALL_KEYS:
        val = data.get(key)
        if isinstance(val, str):
            val = val.strip()
        if not val:
            continue
        if is_placeholder(val):
            skipped.append(key)
            continue
        kr_set(service, key, val)
        stored.append(key)
    if stored:
        registry_add(service)
    return stored, skipped, None


def _store_from_json(path: Path, service: str) -> int:
    """`store --import-json`: import one file, verbosely, with a scrub reminder."""
    stored, skipped, err = import_json_core(path, service)
    if err:
        logger.error(f"Could not import {path.name}: {err}")
        return 2
    if not stored:
        logger.error(f"Nothing importable in {path.name} "
                     f"(only placeholders/empties?). Skipped: {skipped}")
        return 2

    print()
    print(f"  {ANSI['bold']}Imported into service "
          f"{ANSI['yellow']}{service}{ANSI['reset']} (masked):")
    for key in stored:
        print(f"     {key:<16} {mask(kr_get(service, key))}")
    if skipped:
        print(f"  {ANSI['dim']}Skipped placeholder value(s): "
              f"{', '.join(skipped)}{ANSI['reset']}")

    print()
    print(f"  {ANSI['green']}Now validate, THEN scrub the plaintext:{ANSI['reset']}")
    print(f"     1. {SCRIPT_NAME}.py validate --service {service}")
    print(f"     2. Once it passes, delete the source file AND any backups:")
    print(f"        {ANSI['yellow']}{path}{ANSI['reset']}")
    print(f"        {ANSI['dim']}(and the whole creds folder once every set is "
          f"migrated: {path.parent}){ANSI['reset']}")
    print(f"  {ANSI['dim']}This tool never writes secret values to disk.{ANSI['reset']}")
    return 0


def discover_creds_files() -> list[Path]:
    """Legacy plaintext creds/*.json, excluding the tracked example template."""
    if not CREDS_DIR.exists():
        return []
    return [p for p in sorted(CREDS_DIR.glob("*.json")) if p.stem != "example"]


def migrate_all(files: list[Path]) -> int:
    """Bulk-import every legacy creds file into keyring (service = filename
    stem). Non-destructive: the plaintext files remain until you scrub them."""
    print()
    print(f"  {ANSI['bold']}Migrating {len(files)} legacy file(s) from "
          f"{ANSI['dim']}{CREDS_DIR}{ANSI['reset']}{ANSI['bold']} "
          f"into Credential Manager{ANSI['reset']}")
    ok = 0
    for path in files:
        service = path.stem
        stored, skipped, err = import_json_core(path, service)
        if err:
            print(f"     {ANSI['red']}[SKIP]{ANSI['reset']} {service:<24} {err}")
        elif not stored:
            print(f"     {ANSI['yellow']}[SKIP]{ANSI['reset']} {service:<24} "
                  f"nothing importable (placeholders only)")
        else:
            ok += 1
            extra = f" (+{len(skipped)} placeholder skipped)" if skipped else ""
            print(f"     {ANSI['green']}[OK]{ANSI['reset']}   {service:<24} "
                  f"{len(stored)} field(s){ANSI['dim']}{extra}{ANSI['reset']}")
    print()
    print(f"  {ANSI['green']}Migrated {ok}/{len(files)} set(s).{ANSI['reset']} "
          f"Registered services: {', '.join(registry_list()) or '(none)'}")
    if ok:
        print(f"  {ANSI['dim']}Validate them, then you can safely delete "
              f"{CREDS_DIR}. This tool never writes secrets back to disk."
              f"{ANSI['reset']}")
    return 0 if ok else 2


def cmd_migrate(args: argparse.Namespace) -> int:
    """migrate: bulk-import every creds/*.json into keyring."""
    files = discover_creds_files()
    if not files:
        logger.error(f"No legacy creds found in {CREDS_DIR}.")
        return 2
    return migrate_all(files)


def cmd_validate(args: argparse.Namespace) -> int:
    """validate: mint a token + proof-of-life sandbox call. Exit 0/1/2."""
    if args.all:
        services = registry_list()
        if not services:
            logger.error("No services registered. Store one first "
                         "(or pass --service).")
            return 2
    else:
        services = [args.service or DEFAULT_SERVICE]

    worst = 0
    for service in services:
        worst = max(worst, _validate_one(service, deep=args.deep))
    return worst


def _validate_one(service: str, deep: bool) -> int:
    bar = ANSI["cyan"] + "=" * 70 + ANSI["reset"]
    print()
    print(bar)
    print(f"  {ANSI['bold']}Validating service "
          f"{ANSI['yellow']}{service}{ANSI['reset']}")
    print(bar)

    conf = load_creds(service)
    if not conf:
        logger.error(f"No credentials under service '{service}'. "
                     f"Run: {SCRIPT_NAME}.py store --service {service}")
        return 2
    missing = [k for k in REQUIRED_KEYS if not conf.get(k)]
    if missing:
        logger.error(f"Service '{service}' is missing required key(s): "
                     f"{', '.join(missing)}. Re-run store.")
        return 2

    api_key = conf.get("api_key") or conf["client_id"]
    print(f"  {ANSI['bold']}client_id:{ANSI['reset']} {mask(conf['client_id'])}   "
          f"{ANSI['bold']}api_key:{ANSI['reset']} {mask(api_key)}   "
          f"{ANSI['bold']}org_id:{ANSI['reset']} {ANSI['magenta']}{conf['org_id']}"
          f"{ANSI['reset']}")

    # 1) Mint a token.
    resp, rc = authenticate_and_report(conf)
    if resp is None:
        return rc
    token = resp["access_token"]

    # 2) Proof of life: one authenticated AEP call.
    status, sandboxes, err = get_sandboxes(token, conf)
    if sandboxes is not None:
        print(f"  {ANSI['green']}[OK] AEP reachable.{ANSI['reset']} "
              f"GET /sandboxes -> HTTP {status}, "
              f"{ANSI['bold']}{len(sandboxes)} sandbox(es){ANSI['reset']} visible.")
        for sb in sandboxes:
            print(f"     {ANSI['yellow']}{sb.get('name', '?'):<18}{ANSI['reset']} "
                  f"{ANSI['dim']}{sb.get('type', '?'):<10}{ANSI['reset']} "
                  f"{sb.get('state', '?')}")
    else:
        colour = "yellow" if status else "red"
        print(f"  {ANSI[colour]}[WARN] Token is valid, but the sandbox call "
              f"failed: HTTP {status or 'no response'} - {err}{ANSI['reset']}")
        if status in (401, 403):
            print(f"  {ANSI['yellow']}=> The token minted, so the credential is "
                  f"good, but its technical account has no AEP product-profile "
                  f"access. An admin must add it in the Adobe Admin Console."
                  f"{ANSI['reset']}")

    if deep:
        if sandboxes:
            names = [sb.get("name") for sb in sandboxes if sb.get("name")]
        else:
            fb = conf.get("sandbox") or "prod"
            names = [fb if fb != "all" else "prod"]
        probe_services(token, resp, conf, names, names[0] if names else "")

    # Token minted => auth succeeded => exit 0 (pre-flight contract).
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """list: show which keys exist under a service, values masked."""
    service = args.service or DEFAULT_SERVICE
    registered = registry_list()
    print()
    print(f"  {ANSI['bold']}Registered services:{ANSI['reset']} "
          + (", ".join(registered) if registered
             else f"{ANSI['dim']}(none recorded){ANSI['reset']}"))
    print(f"  {ANSI['bold']}Keys under {ANSI['yellow']}{service}{ANSI['reset']}"
          f"{ANSI['bold']}:{ANSI['reset']}")
    found = 0
    for key in ALL_KEYS:
        val = kr_get(service, key)
        req = " (required)" if key in REQUIRED_KEYS else ""
        if val:
            found += 1
            print(f"     {key:<16} {mask(val)}{ANSI['dim']}{req}{ANSI['reset']}")
        else:
            print(f"     {ANSI['dim']}{key:<16} (absent){req}{ANSI['reset']}")
    if not found:
        print(f"  {ANSI['yellow']}No entries for '{service}'.{ANSI['reset']}")
        return 2
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """delete: remove all entries for a service after a y/n confirm."""
    service = args.service or DEFAULT_SERVICE
    present = [k for k in ALL_KEYS if kr_get(service, k)]
    if not present:
        logger.info(f"Nothing to delete for service '{service}'.")
        return 0

    print()
    print(f"  {ANSI['bold']}Will delete {len(present)} entr(y/ies) for "
          f"{ANSI['yellow']}{service}{ANSI['reset']}:")
    for key in present:
        print(f"     {key:<16} {mask(kr_get(service, key))}")
    confirm = input(f"\n  {ANSI['red']}Delete ALL of these?{ANSI['reset']} "
                    f"[y/N] ")
    if confirm.strip().lower() not in ("y", "yes"):
        logger.info("Aborted. Nothing deleted.")
        return 0

    deleted = sum(1 for key in present if kr_del(service, key))
    registry_remove(service)
    logger.info(f"Deleted {deleted} entr(y/ies) for service '{service}'.")
    return 0


# ----------------------------------------------------------------------------
# sweep / purge  (vault-wide discovery, validation, cleanup)
# ----------------------------------------------------------------------------
# The 5 core keys the sweep reports on. Presence of all 5 is shown, but only the
# REQUIRED_KEYS actually gate a token mint -- tech_account_id isn't used by
# client_credentials and scopes has a default, so a set lacking those can still
# authenticate and should not be branded INCOMPLETE.
SWEEP_KEYS = ["client_id", "client_secret", "org_id", "tech_account_id", "scopes"]
KEY_ABBR = {"client_id": "id", "client_secret": "sec", "org_id": "org",
            "tech_account_id": "ta", "scopes": "sco"}


# Vault enumeration (Credential Manager `key@service` scan) + registry∪vault
# discovery both live in aep_creds now; aliased so sweep/purge are unchanged.
# These stay keyring-only (no folder), which is exactly what a vault sweep wants.
if _DEPS_OK:
    vault_services    = aep_creds.vault_services
    discover_services = aep_creds.discover_services


def short_auth_reason(ex: Exception) -> str:
    """Compact one-phrase reason for a failed token mint, for the sweep table."""
    if isinstance(ex, IMSAuthError):
        err = (ex.error or "").lower()
        desc = (ex.description or "").lower()
        if "scope" in err or "scope" in desc:
            return "missing/invalid scope"
        if "invalid_client" in err or ex.status in (400, 401):
            if "expired" in desc:
                return "expired secret"
            if "secret" in desc:
                return "bad secret"
            if "client_id" in desc or "client id" in desc:
                return "bad client_id"
            return "invalid client (bad/expired secret)"
        return f"HTTP {ex.status} {ex.error}".strip()
    if isinstance(ex, requests.exceptions.SSLError):
        return "TLS error (try --insecure)"
    return f"network: {type(ex).__name__}"


def sweep_services(rate_limit: float = 1.0) -> tuple[list[dict], int, bool]:
    """Read-only vault-wide validation. Discovers every credential service,
    records which of the 5 core keys it holds, and -- for services that have the
    keys IMS needs -- attempts a token mint, throttled to `rate_limit` seconds
    between live calls to be polite to IMS.

    Returns (rows, exit_code, scanned_ok). Each row is a dict: service, present
    (set of the 5 keys held), missing_required, orphan (bool), status
    ('OK'|'FAIL'|'INCOMPLETE'), detail. exit_code is 0 only if every service
    minted a token, else 1. Makes NO changes to the vault."""
    services, orphans, scanned_ok = discover_services()
    rows: list[dict] = []
    minted_any = False
    for service in services:
        present = {k for k in SWEEP_KEYS if kr_get(service, k)}
        missing_req = [k for k in REQUIRED_KEYS if k not in present]
        row = {"service": service, "present": present,
               "missing_required": missing_req, "orphan": service in orphans,
               "status": "INCOMPLETE", "detail": ""}
        if missing_req:
            row["detail"] = "missing " + ",".join(KEY_ABBR[k]
                                                   for k in missing_req)
            rows.append(row)
            continue
        # Complete enough to mint. Throttle between live IMS calls (not before
        # the first one).
        if minted_any and rate_limit > 0:
            time.sleep(rate_limit)
        minted_any = True
        try:
            authenticate(load_creds(service))
            row["status"] = "OK"
        except (IMSAuthError, requests.RequestException) as ex:
            row["status"] = "FAIL"
            row["detail"] = short_auth_reason(ex)
        rows.append(row)

    rc = 0 if rows and all(r["status"] == "OK" for r in rows) else \
         (0 if not rows else 1)
    return rows, rc, scanned_ok


def _key_flags(present: set[str]) -> str:
    """Fixed-width, colorised presence indicator for the 5 core keys."""
    parts = []
    for key in SWEEP_KEYS:
        abbr = KEY_ABBR[key]
        if key in present:
            parts.append(f"{ANSI['green']}{abbr}{ANSI['reset']}")
        else:
            parts.append(f"{ANSI['dim']}{'-' * len(abbr)}{ANSI['reset']}")
    return " ".join(parts)


def print_sweep_table(rows: list[dict], scanned_ok: bool) -> None:
    """Render the sweep result as a service | keys | status table."""
    print()
    if not scanned_ok:
        print(f"  {ANSI['dim']}(vault scan unavailable; discovered from the "
              f"cred-index registry only){ANSI['reset']}")
    if not rows:
        print(f"  {ANSI['yellow']}No credential services found.{ANSI['reset']}")
        return
    svc_w = min(max([len("service")] + [len(r["service"]) for r in rows]), 28)
    flags_hdr = " ".join(KEY_ABBR[k] for k in SWEEP_KEYS)  # "id sec org ta sco"
    print(f"  {ANSI['bold']}{'service':<{svc_w}}  {flags_hdr}  status"
          f"{ANSI['reset']}")
    print(f"  {ANSI['dim']}{'-' * svc_w}  {'-' * len(flags_hdr)}  ------"
          f"{ANSI['reset']}")

    n_ok = n_fail = n_inc = 0
    for row in rows:
        status = row["status"]
        if status == "OK":
            n_ok += 1
            status_cell = f"{ANSI['green']}OK{ANSI['reset']}"
        elif status == "FAIL":
            n_fail += 1
            status_cell = (f"{ANSI['red']}FAIL{ANSI['reset']} "
                           f"{ANSI['dim']}{row['detail']}{ANSI['reset']}")
        else:
            n_inc += 1
            status_cell = (f"{ANSI['yellow']}INCOMPLETE{ANSI['reset']} "
                           f"{ANSI['dim']}{row['detail']}{ANSI['reset']}")
        name = row["service"]
        shown = name if len(name) <= svc_w else name[:svc_w - 1] + "…"
        orphan = f" {ANSI['magenta']}*{ANSI['reset']}" if row["orphan"] else ""
        print(f"  {ANSI['yellow']}{shown:<{svc_w}}{ANSI['reset']}  "
              f"{_key_flags(row['present'])}  {status_cell}{orphan}")

    print()
    summary = (f"  {ANSI['green']}{n_ok} OK{ANSI['reset']}   "
               f"{ANSI['red']}{n_fail} FAIL{ANSI['reset']}   "
               f"{ANSI['yellow']}{n_inc} INCOMPLETE{ANSI['reset']}")
    if any(r["orphan"] for r in rows):
        summary += (f"   {ANSI['magenta']}* = in vault but not in the "
                    f"cred-index{ANSI['reset']}")
    print(summary)


def cmd_sweep(args: argparse.Namespace) -> int:
    """sweep: discover + validate every service, print a table. Read-only."""
    rows, rc, scanned_ok = sweep_services(rate_limit=args.rate)
    print_sweep_table(rows, scanned_ok)
    return rc


def cmd_purge(args: argparse.Namespace) -> int:
    """purge: sweep, then offer to delete each FAILED/INCOMPLETE service."""
    rows, _rc, scanned_ok = sweep_services(rate_limit=args.rate)
    print_sweep_table(rows, scanned_ok)

    targets = [r for r in rows if r["status"] in ("FAIL", "INCOMPLETE")]
    if not targets:
        print()
        logger.info("Nothing to purge -- every service minted a token.")
        return 0

    print()
    print(f"  {ANSI['bold']}{len(targets)} service(s) failed or incomplete:"
          f"{ANSI['reset']} "
          f"{', '.join(r['service'] for r in targets)}")

    if args.dry_run:
        print(f"  {ANSI['magenta']}[DRY-RUN]{ANSI['reset']} would prompt to "
              f"delete each of these; no changes made.")
        for row in targets:
            n = sum(1 for k in ALL_KEYS if kr_get(row["service"], k))
            print(f"     {ANSI['yellow']}{row['service']:<26}{ANSI['reset']} "
                  f"{ANSI['dim']}{row['status']}: {row['detail']} -> would "
                  f"delete {n} key(s){ANSI['reset']}")
        return 0

    removed = 0
    for row in targets:
        service = row["service"]
        keys = [k for k in ALL_KEYS if kr_get(service, k)]
        print()
        print(f"  {ANSI['bold']}{service}{ANSI['reset']} "
              f"{ANSI['dim']}({row['status']}: {row['detail'] or 'n/a'}) - "
              f"{len(keys)} key(s){ANSI['reset']}")
        ans = input(f"  {ANSI['red']}Delete all its keys?{ANSI['reset']} "
                    f"[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print(f"  {ANSI['dim']}Skipped.{ANSI['reset']}")
            continue
        typed = input(f"  Type the service name "
                      f"{ANSI['yellow']}{service}{ANSI['reset']} to confirm: ")
        if typed.strip() != service:
            print(f"  {ANSI['yellow']}Name mismatch -- skipped '{service}'."
                  f"{ANSI['reset']}")
            continue
        deleted = sum(1 for k in keys if kr_del(service, k))
        registry_remove(service)
        removed += 1
        print(f"  {ANSI['green']}Deleted {deleted} key(s) for '{service}'."
              f"{ANSI['reset']}")

    print()
    logger.info(f"Purge complete. Removed {removed}/{len(targets)} flagged "
                f"service(s).")
    return 0


# ----------------------------------------------------------------------------
# Auth reporting + interactive session
# ----------------------------------------------------------------------------
def authenticate_and_report(conf: dict) -> tuple[dict | None, int]:
    """Mint a token and print an OK line or a human diagnosis. Returns
    (resp, rc): resp is None on failure and rc is the exit code to use."""
    try:
        resp = authenticate(conf)
    except IMSAuthError as ex:
        print(f"  {ANSI['red']}[FAIL] Could not mint a token.{ANSI['reset']}")
        for line in diagnose_ims(ex):
            print(f"  {ANSI['yellow']}{line}{ANSI['reset']}")
        return None, 1
    except requests.exceptions.SSLError as ex:
        logger.error(f"TLS verification failed talking to IMS: {ex}")
        print(f"  {ANSI['yellow']}=> If you're behind a corporate proxy that "
              f"MITMs TLS, re-run with --insecure.{ANSI['reset']}")
        return None, 1
    except requests.RequestException as ex:
        logger.error(f"NETWORK error reaching IMS: {type(ex).__name__}: {ex}")
        print(f"  {ANSI['yellow']}=> Check connectivity / proxy / DNS. IMS host: "
              f"ims-na1.adobelogin.com{ANSI['reset']}")
        return None, 1

    if not resp.get("access_token"):
        logger.error(f"IMS returned 200 but no access_token: "
                     f"{clean_detail(str(resp))}")
        return None, 1
    expires_in = resp.get("expires_in")
    mins = f"{int(expires_in) / 60:.0f}" if expires_in else "?"
    print(f"  {ANSI['green']}[OK] Token minted.{ANSI['reset']} "
          f"type={resp.get('token_type', '?')}  "
          f"expires in {ANSI['bold']}{mins} min{ANSI['reset']} "
          f"{ANSI['dim']}({expires_in}s){ANSI['reset']}")
    return resp, 0


def pick_service(services: list[str]) -> str | None:
    """Original-style credential bank menu, sourced from keyring services."""
    bar = ANSI["cyan"] + "=" * 70 + ANSI["reset"]
    print()
    print(bar)
    print(f"  {ANSI['bold']}Credential bank{ANSI['reset']}  "
          f"{ANSI['dim']}(Windows Credential Manager){ANSI['reset']}")
    print(ANSI["cyan"] + "-" * 70 + ANSI["reset"])
    for i, s in enumerate(services, 1):
        print(f"  {ANSI['bold']}{i:>2}{ANSI['reset']}  "
              f"{ANSI['yellow']}{s}{ANSI['reset']}")
    print(bar)
    raw = input("\nPick a credential set by number or name (Enter to quit): ").strip()
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(services):
        return services[int(raw) - 1]
    if raw in services:
        return raw
    logger.warning(f"Invalid choice: {raw!r}")
    return None


def pick_sandbox(sandboxes: list | None, conf: dict) -> str:
    """Let the user pick a sandbox (Enter = prod, else first / configured)."""
    names = [sb.get("name") for sb in (sandboxes or []) if sb.get("name")]
    default = ("prod" if "prod" in names
               else names[0] if names
               else conf.get("sandbox") or "prod")
    print()
    if names:
        print(f"  {ANSI['bold']}Sandboxes visible to this credential:{ANSI['reset']}")
        for i, sb in enumerate(sandboxes, 1):
            nm = sb.get("name", "?")
            marker = (f" {ANSI['green']}(default){ANSI['reset']}"
                      if nm == default else "")
            print(f"    {ANSI['bold']}{i:>2}{ANSI['reset']}  "
                  f"{ANSI['yellow']}{nm:<18}{ANSI['reset']} "
                  f"{ANSI['dim']}{sb.get('type', '?'):<10}{ANSI['reset']}{marker}")
        raw = input(f"\nPick a sandbox by number or name "
                    f"[Enter = {default}]: ").strip()
    else:
        raw = input(f"\nNo sandbox list available. Sandbox name "
                    f"[Enter = {default}]: ").strip()
    if not raw:
        return default
    if raw.isdigit() and names and 1 <= int(raw) <= len(names):
        return names[int(raw) - 1]
    return raw


def probe_credential(service: str) -> int:
    """Full original-style probe for one keyring service: authenticate, pick a
    sandbox, then sweep every API surface and report."""
    bar = ANSI["cyan"] + "=" * 70 + ANSI["reset"]
    print()
    print(bar)
    print(f"  {ANSI['bold']}Probing {ANSI['yellow']}{service}{ANSI['reset']}")
    print(bar)

    conf = load_creds(service)
    if not conf:
        logger.error(f"No credentials under service '{service}'.")
        return 2
    missing = [k for k in REQUIRED_KEYS if not conf.get(k)]
    if missing:
        logger.error(f"Service '{service}' is missing required key(s): "
                     f"{', '.join(missing)}.")
        return 2

    api_key = conf.get("api_key") or conf["client_id"]
    print(f"  {ANSI['bold']}client_id:{ANSI['reset']} {mask(conf['client_id'])}   "
          f"{ANSI['bold']}api_key:{ANSI['reset']} {mask(api_key)}   "
          f"{ANSI['bold']}org_id:{ANSI['reset']} {ANSI['magenta']}{conf['org_id']}"
          f"{ANSI['reset']}")

    resp, rc = authenticate_and_report(conf)
    if resp is None:
        return rc
    token = resp["access_token"]

    status, sandboxes, err = get_sandboxes(token, conf)
    if sandboxes is not None:
        print(f"  {ANSI['green']}[OK] AEP reachable.{ANSI['reset']} "
              f"GET /sandboxes -> HTTP {status}, "
              f"{ANSI['bold']}{len(sandboxes)} sandbox(es){ANSI['reset']} visible.")
    else:
        colour = "yellow" if status else "red"
        print(f"  {ANSI[colour]}[WARN] Token valid, but the sandbox call failed: "
              f"HTTP {status or 'no response'} - {err}{ANSI['reset']}")

    sandbox = pick_sandbox(sandboxes, conf)
    print(f"  {ANSI['bold']}Probing against sandbox "
          f"{ANSI['yellow']}{sandbox}{ANSI['reset']}")
    probe_services(token, resp, conf, [sandbox], sandbox)
    print()
    logger.info(f"Done probing '{service}'.")
    return 0


def run_interactive() -> int:
    """Naked run: pick a stored credential, pick a sandbox, probe everything.
    If keyring is empty, offer to migrate the legacy creds/ folder first."""
    services = registry_list()
    if not services:
        files = discover_creds_files()
        if not files:
            logger.error(f"No credentials in Credential Manager, and no legacy "
                         f"JSONs in {CREDS_DIR}. Add one with: "
                         f"{SCRIPT_NAME}.py store --service <name>")
            return 2
        print()
        print(f"  {ANSI['yellow']}No credentials in Credential Manager yet, but "
              f"found {len(files)} legacy file(s) in {CREDS_DIR}.{ANSI['reset']}")
        ans = input("  Import them into Credential Manager now? [Y/n] ").strip().lower()
        if ans not in ("", "y", "yes"):
            logger.info("Nothing imported. Exiting.")
            return 0
        migrate_all(files)
        services = registry_list()
        if not services:
            return 2

    service = pick_service(sorted(services))
    if not service:
        logger.info("Nothing chosen. Exiting.")
        return 0
    return probe_credential(service)


# ----------------------------------------------------------------------------
# Main / argparse
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--service", default=None,
                        help=f"Credential Manager service name "
                             f"(default: {DEFAULT_SERVICE}). e.g. aep-dev.")
    common.add_argument("--insecure", action="store_true",
                        help="Disable TLS verification (corporate proxy MITM).")

    parser = argparse.ArgumentParser(
        prog=f"{SCRIPT_NAME}.py",
        description="Keyring-backed AEP credential store + validator. "
                    "Run with no command for the interactive probe (pick a "
                    "credential, pick a sandbox, sweep every API service).")
    parser.add_argument("--insecure", action="store_true",
                        help="Disable TLS verification (corporate proxy MITM).")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("migrate", parents=[common],
                   help="Bulk-import every legacy creds/*.json into keyring.")

    p_store = sub.add_parser("store", parents=[common],
                             help="Store creds (getpass) or import a legacy JSON.")
    p_store.add_argument("--import-json", metavar="PATH",
                         help="One-off import of a legacy creds JSON into keyring.")

    p_val = sub.add_parser("validate", parents=[common],
                           help="Mint a token + proof-of-life sandbox call.")
    p_val.add_argument("--deep", action="store_true",
                       help="Also run the full probe (token decode, Query "
                            "Service, product-surface sweep).")
    p_val.add_argument("--all", action="store_true",
                       help="Validate every registered service.")

    sub.add_parser("list", parents=[common],
                   help="Show which keys exist under a service (masked).")
    sub.add_parser("delete", parents=[common],
                   help="Delete all entries for a service (after confirm).")

    p_sweep = sub.add_parser("sweep", parents=[common],
                             help="Discover + validate every vault service "
                                  "(read-only table).")
    p_sweep.add_argument("--rate", type=float, default=1.0,
                         help="Seconds between token attempts (default 1.0).")

    p_purge = sub.add_parser("purge", parents=[common],
                             help="Sweep, then delete failed/incomplete "
                                  "services (confirmed).")
    p_purge.add_argument("--rate", type=float, default=1.0,
                         help="Seconds between token attempts (default 1.0).")
    p_purge.add_argument("--dry-run", action="store_true",
                         help="Preview what purge would delete; make no changes.")
    return parser


def main() -> int:
    print_banner()
    if not _DEPS_OK:
        logger.error(f"Missing dependency: {_DEPS_ERR}. "
                     f"Install with:  pip install requests keyring")
        return 2

    parser = build_parser()
    args = parser.parse_args()

    global VERIFY_TLS
    if getattr(args, "insecure", False):
        VERIFY_TLS = False
        try:
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
        except Exception:
            pass

    if not args.command:
        # Naked run == the original validator UX: menu -> sandbox -> probe.
        return run_interactive()

    dispatch = {
        "store": cmd_store,
        "migrate": cmd_migrate,
        "validate": cmd_validate,
        "list": cmd_list,
        "delete": cmd_delete,
        "sweep": cmd_sweep,
        "purge": cmd_purge,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
