#!/usr/bin/env python3
"""
credential_validator.py
========================
Quickly check whether an Adobe IMS / AEP credential set is alive.

Pick a credential JSON from ./creds/ (e.g. prod.json, dev.json) and the
validator will:
  1. Authenticate against the IMS token endpoint (client_credentials).
  2. Decode the returned access token (no signature check) to show scopes,
     org, client_id, expiry, and the technical account it belongs to.
  3. Hit AEP /sandbox-management/sandboxes to list which sandboxes the
     credential can actually see - a useful proxy for tenancy/admin breadth.

Stdlib only, VDI-friendly. No pip install required.

Usage:
    python credential_validator.py            # interactive menu
    python credential_validator.py prod dev   # one or more by name (filename stem)
    python credential_validator.py --all      # validate every set in ./creds/
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
CREDS_DIR = SCRIPT_DIR / "creds"
LEGACY_CONFIG = SCRIPT_DIR / "config.json"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
SANDBOX_LIST_URL = (
    "https://platform.adobe.io/data/foundation/sandbox-management/sandboxes"
)
QUERIES_URL = "https://platform.adobe.io/data/foundation/query/queries"
DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

# ----------------------------------------------------------------------------
# ANSI / logging - matches batch_fetcher.py style
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
logger = logging.getLogger("credential_validator")
SSL_CTX = ssl._create_unverified_context()


# ----------------------------------------------------------------------------
# HTTP / IMS / access-token helpers
# ----------------------------------------------------------------------------
def http(url, method="GET", headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def load_creds(path: Path):
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


def b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg.encode("ascii"))


def decode_access_token(token: str):
    """Decode the OAuth server-to-server access token into (header, payload)
    dicts. Signature is NOT verified -- this is only to surface the scopes,
    org, expiry and technical account already granted to the credential."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"Not a decodable access token "
                         f"(got {len(parts)} segments)")
    header = json.loads(b64url_decode(parts[0]).decode("utf-8"))
    payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
    return header, payload


def list_sandboxes(token, conf):
    """Returns (ok, sandboxes_or_error_string)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "Accept": "application/json",
    }
    try:
        body, _ = http(SANDBOX_LIST_URL, headers=headers)
        data = json.loads(body)
        return True, data.get("sandboxes") or []
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")[:400]
        return False, f"HTTP {e.code}: {err}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def list_queries(token, conf, sandbox):
    """Probe AEP Query Service in one sandbox by listing queries.

    Returns (ok, result) where, on success, result is the list of query
    objects (the /queries execution history). A 403 here means the token
    authenticated but lacks Query Service permission in this sandbox - a
    direct answer to 'do these creds have query access?'.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "x-sandbox-name": sandbox,
        "Accept": "application/json",
    }
    url = f"{QUERIES_URL}?{urllib.parse.urlencode({'limit': 10})}"
    try:
        body, _ = http(url, headers=headers)
        data = json.loads(body)
        return True, data.get("queries") or []
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")[:400]
        return False, f"HTTP {e.code}: {err}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------------------
# Discovery / menu
# ----------------------------------------------------------------------------
def discover_creds():
    """Return ordered list of credential JSON paths."""
    paths = []
    if CREDS_DIR.exists():
        for p in sorted(CREDS_DIR.glob("*.json")):
            if p.stem == "example":
                continue
            paths.append(p)
    return paths


def menu(creds):
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
    raw = input(
        f"\nPick set(s) by number ({ANSI['cyan']}1{ANSI['reset']}, "
        f"{ANSI['cyan']}1,3{ANSI['reset']}, or {ANSI['cyan']}all{ANSI['reset']}), "
        "blank to quit: "
    ).strip()
    if not raw:
        return []
    if raw.lower() == "all":
        return list(creds)
    chosen = []
    for tok in raw.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(creds):
            chosen.append(creds[int(tok) - 1])
        else:
            logger.warning(f"Ignoring invalid choice: {tok}")
    return chosen


# ----------------------------------------------------------------------------
# Probe
# ----------------------------------------------------------------------------
def shorten(s, n=12):
    if not s:
        return "?"
    return s if len(s) <= n else f"{s[:n]}..."


def probe(path: Path):
    bar = ANSI["cyan"] + "=" * 70 + ANSI["reset"]
    print()
    print(bar)
    print(f"  {ANSI['bold']}Probing {ANSI['yellow']}{path.stem}{ANSI['reset']}  "
          f"{ANSI['dim']}({path.name}){ANSI['reset']}")
    print(bar)

    try:
        conf = load_creds(path)
    except Exception as e:
        logger.error(f"Failed to load {path.name}: {e}")
        return

    api_key = conf.get("api_key") or conf["client_id"]
    api_key_note = "" if api_key == conf["client_id"] else " (separate from client_id)"
    print(f"  {ANSI['bold']}client_id:{ANSI['reset']}  {shorten(conf['client_id'])}")
    print(f"  {ANSI['bold']}api_key:{ANSI['reset']}    {shorten(api_key)}{ANSI['dim']}{api_key_note}{ANSI['reset']}")
    print(f"  {ANSI['bold']}org_id:{ANSI['reset']}     {ANSI['magenta']}{conf['org_id']}{ANSI['reset']}")
    print(f"  {ANSI['bold']}requested:{ANSI['reset']}  {ANSI['dim']}{conf.get('scopes') or DEFAULT_SCOPES}{ANSI['reset']}")
    print()

    # 1) IMS authenticate
    try:
        resp = authenticate(conf)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        logger.error(f"IMS auth FAILED: HTTP {e.code} {body}")
        return
    except Exception as e:
        logger.error(f"IMS auth FAILED: {type(e).__name__}: {e}")
        return

    print(f"  {ANSI['green']}[OK] IMS authenticated{ANSI['reset']}  "
          f"token_type={resp.get('token_type')} "
          f"expires_in={resp.get('expires_in')}s")

    token = resp["access_token"]

    # 2) Decode the access token
    try:
        _hdr, payload = decode_access_token(token)
        granted = payload.get("scope", "(no scope claim)")
        user = payload.get("user_id") or payload.get("aa_id") or "?"
        client_in_token = payload.get("client_id") or "?"
        token_type = payload.get("type") or "?"
        org_in_token = payload.get("org") or "?"
        created_at = payload.get("created_at")
        expires_in_ms = payload.get("expires_in")
        try:
            created_dt = datetime.fromtimestamp(int(created_at) / 1000, tz=timezone.utc)
            created_str = created_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            created_str = str(created_at)

        print(f"  {ANSI['bold']}Granted scopes:{ANSI['reset']}")
        for s in granted.split(","):
            print(f"     {ANSI['cyan']}- {s.strip()}{ANSI['reset']}")
        print(f"  {ANSI['bold']}Token type:{ANSI['reset']} {token_type}")
        print(f"  {ANSI['bold']}Token org:{ANSI['reset']}    {org_in_token}"
              + (f"  {ANSI['yellow']}(mismatch vs config!){ANSI['reset']}"
                 if org_in_token != conf["org_id"] else ""))
        print(f"  {ANSI['bold']}Token client:{ANSI['reset']} {shorten(client_in_token)}")
        print(f"  {ANSI['bold']}Tech acct:{ANSI['reset']}  {user}")
        print(f"  {ANSI['bold']}Created:{ANSI['reset']}    {created_str}  "
              f"{ANSI['dim']}(expires_in {expires_in_ms} ms){ANSI['reset']}")
    except Exception as e:
        logger.warning(f"Could not decode access token: {e}")

    # 3) AEP probe - sandbox listing
    print()
    print(f"  {ANSI['bold']}AEP /sandbox-management/sandboxes{ANSI['reset']}")
    ok, result = list_sandboxes(token, conf)
    if not ok:
        print(f"  {ANSI['red']}[FAIL] {result}{ANSI['reset']}")
    elif not result:
        print(f"  {ANSI['yellow']}[OK] Authenticated, but 0 sandboxes visible - credential likely has no AEP product profile.{ANSI['reset']}")
    else:
        print(f"  {ANSI['green']}[OK] {len(result)} sandbox(es) visible:{ANSI['reset']}")
        for sb in result:
            name = sb.get("name", "?")
            title = sb.get("title", "")
            sb_type = sb.get("type", "?")
            state = sb.get("state", "?")
            print(f"     {ANSI['yellow']}{name:<20}{ANSI['reset']} "
                  f"{ANSI['dim']}{sb_type:<12}{ANSI['reset']} "
                  f"{state:<10} {title}")

    # 4) Query Service probe - try to list queries in each sandbox.
    #    Listing sandboxes proves tenancy; this proves Query Service access.
    if ok and result:
        sandbox_names = [sb.get("name") for sb in result if sb.get("name")]
    else:
        # Sandbox-management denied/empty - fall back to the configured
        # default sandbox so we can still test query access. "all" is a
        # sentinel meaning "every sandbox", not a real name, so map it to
        # 'prod' (the one sandbox that virtually always exists).
        fallback = conf.get("sandbox") or "prod"
        if fallback == "all":
            fallback = "prod"
        sandbox_names = [fallback]
        print(f"  {ANSI['dim']}(no sandbox list; testing query access against "
              f"'{fallback}' from config){ANSI['reset']}")

    print()
    print(f"  {ANSI['bold']}AEP /query/queries  (Query Service access){ANSI['reset']}")
    any_access = False
    for sb in sandbox_names:
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
            for q in res_q[:3]:
                qid = shorten(q.get("id", "?"), 18)
                state = (q.get("state") or "?")
                sql = (q.get("sql", "") or "").strip().replace("\n", " ")[:60]
                print(f"        {ANSI['dim']}{qid:<22} {state:<10} {sql}{ANSI['reset']}")
    if any_access:
        print(f"  {ANSI['green']}=> Credential HAS Query Service access.{ANSI['reset']}")
    else:
        print(f"  {ANSI['red']}=> Credential has NO Query Service access in any "
              f"tested sandbox.{ANSI['reset']}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def parse_args(argv):
    flags = {"all": False}
    names = []
    for a in argv:
        if a in ("--all", "-a"):
            flags["all"] = True
        elif a.startswith("-"):
            continue
        else:
            names.append(a)
    return flags, names


def main():
    flags, names = parse_args(sys.argv[1:])
    creds = discover_creds()
    if not creds:
        logger.error(f"No credential JSONs found in {CREDS_DIR}. "
                     f"Drop your <tenant>.json files there.")
        return

    if flags["all"]:
        chosen = list(creds)
    elif names:
        by_stem = {p.stem: p for p in creds}
        chosen = [by_stem[n] for n in names if n in by_stem]
        missing = [n for n in names if n not in by_stem]
        for n in missing:
            logger.warning(f"No credential set named {n!r} (looked in {CREDS_DIR})")
        if not chosen:
            return
    else:
        chosen = menu(creds)

    if not chosen:
        logger.info("Nothing chosen. Exiting.")
        return

    for path in chosen:
        probe(path)
    print()
    logger.info("Done.")


if __name__ == "__main__":
    main()
