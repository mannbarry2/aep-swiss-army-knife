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
SCRIPT_NAME    = "credential_validator"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE    = "2026-06-24"
SCRIPT_AUTHOR  = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent
CREDS_DIR = SCRIPT_DIR / "creds"
LEGACY_CONFIG = SCRIPT_DIR / "config.json"

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
# IMS profile carries projectedProductContext for the technical account.
# (userinfo/v2 returns only {sub} for S2S tokens -- it does NOT work here.)
IMS_PROFILE_URL = "https://ims-na1.adobelogin.com/ims/profile/v1"
SANDBOX_LIST_URL = (
    "https://platform.adobe.io/data/foundation/sandbox-management/sandboxes"
)
QUERIES_URL = "https://platform.adobe.io/data/foundation/query/queries"

# AEP product API surfaces swept by the connectivity check. One lightweight
# GET each -- we only care whether the service "stood up" for this
# credential, not the payload. Tuple:
#   (label, url, sandbox_scoped, extra_headers, best_effort)
# sandbox_scoped surfaces get an x-sandbox-name header. best_effort marks
# endpoints whose exact path can't be guaranteed (no dedicated host, so a
# wrong sub-path 404s on the shared gateway) -- for those, a 404 is reported
# as INCONCLUSIVE rather than a misleading "reachable". A clean 200/401/403
# is still treated as a real verdict.
PRODUCT_SURFACES = [
    ("Query Service",
     "https://platform.adobe.io/data/foundation/query/queries?limit=1",
     True, {}, False),
    ("Catalog Service",
     "https://platform.adobe.io/data/foundation/catalog/dataSets?limit=1",
     True, {}, False),
    ("Schema Registry",
     "https://platform.adobe.io/data/foundation/schemaregistry/tenant/schemas?limit=1",
     True, {"Accept": "application/vnd.adobe.xed-id+json"}, False),
    ("Flow Service (Sources)",
     "https://platform.adobe.io/data/foundation/flowservice/connectionSpecs?limit=1",
     False, {}, False),
    ("Data Ingestion (batches)",
     "https://platform.adobe.io/data/foundation/catalog/batches?limit=1",
     True, {}, False),
    ("Real-Time Profile",
     "https://platform.adobe.io/data/core/ups/config/mergePolicies?limit=1",
     True, {}, False),
    ("Identity Service",
     "https://platform.adobe.io/data/core/idnamespace/identities",
     False, {}, False),
    ("Privacy Service",
     "https://platform.adobe.io/data/core/privacy/jobs",
     False, {}, False),
    ("Customer Journey Analytics",
     "https://cja.adobe.io/data/dataviews?limit=1",
     False, {}, False),
    # Adobe Journey Optimizer journeys live on their OWN host
    # (journey.adobe.io/authoring), NOT the platform.adobe.io gateway -- which
    # is why the old /data/core/ajo path always 404'd. This is a CONFIRMED
    # endpoint, so it is no longer best-effort: a 403 here is a real verdict
    # ("reachable, not entitled" -- the Journey API product isn't on this
    # integration), which is exactly the "do these creds reach AJO?" answer.
    ("Adobe Journey Optimizer (Journeys)",
     "https://journey.adobe.io/authoring/journeys",
     True, {}, False),
    # NB: the newer AJO REST APIs ride the platform.adobe.io/ajo gateway -- a
    # DIFFERENT surface from journey.adobe.io/authoring above. We do NOT probe it
    # in this generic sweep: a journey GET (/ajo/journey/{id}) only 200s in the
    # one sandbox that actually holds that id, and AJO answers a missing/cross-
    # sandbox id with 500 (not 404/403), so a sweep-sandbox guess is misleading.
    # The pinned probe_ajo_journey() check (see AJO_JOURNEY_PROBE) handles it.
    # Best-effort: Offer Decisioning rides the shared platform.adobe.io gateway
    # with no dedicated host, so an unconfirmed sub-path 404s -- a 404 reads
    # INCONCLUSIVE, not a false "reachable".
    ("Offer Decisioning",
     "https://platform.adobe.io/data/core/xcore/",
     True, {}, True),
]

DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

# Pinned Adobe Journey Optimizer reachability check. A journey GET only returns
# 200 in the sandbox that actually holds the journey, and AJO answers a missing
# or cross-sandbox id with 500 (not 404/403) -- so the only way to definitively
# prove "do these creds reach AJO journeys?" is to GET a KNOWN journey id in its
# OWN sandbox. Hardcoded for the test org for now (this id 200'd in prod with
# the same x-api-key/org); other orgs would each need their own (sandbox, id).
AJO_JOURNEY_PROBE = {
    "sandbox": "prod",
    "journey_id": "3b3a2615-4100-48fe-9e4b-bca60ade2fb1",
}

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


def print_banner() -> None:
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {ANSI['dim']}Check whether an Adobe IMS / AEP credential set is alive "
          f"and what it can reach.{ANSI['reset']}")
    print(bar)


# ----------------------------------------------------------------------------
# HTTP / IMS / access-token helpers
# ----------------------------------------------------------------------------
def http(url, method="GET", headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def clean_detail(raw: bytes | str, limit: int = 140) -> str:
    """Collapse a (possibly HTML) error body into one safe inline string.
    str.split() with no args splits on ANY whitespace -- including the CR
    (\\r) in openresty/gateway error pages that would otherwise reset the
    terminal cursor to column 0 and overwrite the line's label. Joining on a
    single space also de-noises multi-line HTML before truncation."""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    return " ".join(text.split())[:limit]


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


def _service_codes_from_ppc(ppc):
    """Pull a sorted, de-duped list of serviceCode strings out of a
    projectedProductContext array. IMS returns each entry either flat
    ({'serviceCode': ...}) or nested under 'prodCtx' depending on the
    surface, so handle both."""
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


def fetch_product_contexts(token, payload, resp, conf):
    """Return (codes, source) where codes is the list of provisioned product
    serviceCodes for this credential, and source notes where we found them.

    projectedProductContext lists the products the org has granted this
    integration (serviceCode + label). Useful, but NOT a full entitlement
    verdict: apps built on AEP have no serviceCode of their own (AJO journey
    access rides on the 'acp' context + journey scopes), so an absence here is
    a hint, not proof of no access. It is also NOT reliably embedded in S2S
    access-token JWTs even when the scope is requested, so we check the token /
    IMS response first, then fall back to a best-effort IMS profile call.
    Returns (None, reason) if it can't be determined."""
    codes = _service_codes_from_ppc(payload.get("projectedProductContext"))
    if codes:
        return codes, "access token"
    codes = _service_codes_from_ppc(resp.get("projectedProductContext"))
    if codes:
        return codes, "IMS token response"
    # Best-effort live lookup -- the IMS profile endpoint carries
    # projectedProductContext when the additional_info.projectedProductContext
    # scope was granted, even when it is absent from the access-token JWT.
    url = f"{IMS_PROFILE_URL}?{urllib.parse.urlencode({'client_id': conf['client_id']})}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        body, _ = http(url, headers=headers, timeout=20)
        data = json.loads(body)
        codes = _service_codes_from_ppc(data.get("projectedProductContext"))
        if codes:
            return codes, "IMS profile"
        return None, "not present in token or IMS profile"
    except urllib.error.HTTPError as e:
        return None, f"IMS profile HTTP {e.code}"
    except Exception as e:
        return None, f"IMS profile {type(e).__name__}: {e}"


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
        err = clean_detail(e.read(), 400)
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
        err = clean_detail(e.read(), 400)
        return False, f"HTTP {e.code}: {err}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _classify_status(code: int, best_effort: bool = False) -> tuple[str, str, str]:
    """Map an HTTP status (or 0/-1 for transport failure) to a verdict for
    the connectivity sweep. We only care if the service stood up, so 4xx
    other than auth/permission still counts as 'reachable'.

    For best_effort surfaces (path not guaranteed), a 404/400-style miss is
    INCONCLUSIVE rather than 'reachable' -- we can't tell a wrong path from a
    real service. A clean 200/401/403 is still a real verdict either way.

    Returns (tag, ansi_color_key, human_label)."""
    if 200 <= code < 300:
        return "UP", "green", "reachable + authorized"
    if code in (401, 403):
        return "NO-PERM", "yellow", f"reachable, no access (HTTP {code})"
    if best_effort and code in (0, -1, 400, 404, 405, 406, 415, 422):
        why = f"HTTP {code}" if code > 0 else "no response"
        return "INCONC", "dim", f"inconclusive - endpoint unconfirmed ({why})"
    if code in (400, 404, 405, 406, 415, 422):
        return "UP", "green", f"reachable (HTTP {code})"
    if code == 429:
        return "UP", "green", "reachable (HTTP 429 rate-limited)"
    if code >= 500:
        return "ERR", "red", f"service error (HTTP {code})"
    return "DOWN", "red", "unreachable"


def probe_surface(token, conf, url, sandbox_scoped, extra_headers, sandbox):
    """Fire one GET at a product API surface. Returns (code, detail)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "Accept": "application/json",
    }
    if sandbox_scoped and sandbox:
        headers["x-sandbox-name"] = sandbox
    headers.update(extra_headers)
    try:
        http(url, headers=headers, timeout=20)
        return 200, ""
    except urllib.error.HTTPError as e:
        return e.code, clean_detail(e.read())
    except urllib.error.URLError as e:
        return 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}"
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def probe_product_surfaces(token, conf, sandbox):
    """Sweep every entry in PRODUCT_SURFACES and print a one-line verdict
    per product: did the service stand up for this credential?"""
    print()
    sb_note = f" (sandbox-scoped tests use '{sandbox}')" if sandbox else ""
    print(f"  {ANSI['bold']}AEP product API surfaces{ANSI['reset']}{ANSI['dim']}{sb_note}{ANSI['reset']}")
    up = 0
    inconclusive = 0
    for label, url, sb_scoped, extra, best_effort in PRODUCT_SURFACES:
        code, detail = probe_surface(token, conf, url, sb_scoped, extra, sandbox)
        tag, color, human = _classify_status(code, best_effort)
        if tag == "UP":
            up += 1
        elif tag == "INCONC":
            inconclusive += 1
        line = (f"     {ANSI['yellow']}{label:<28}{ANSI['reset']} "
                f"{ANSI[color]}[{tag}]{ANSI['reset']} {human}")
        # Surface the response body for denials/errors too -- it distinguishes
        # "Api Key is invalid" (integration not entitled to the product) from a
        # plain role-based permission denial, and a wrong path from a real one.
        if tag in ("ERR", "DOWN", "NO-PERM", "INCONC") and detail:
            line += f" {ANSI['dim']}- {detail}{ANSI['reset']}"
        print(line)
    total = len(PRODUCT_SURFACES)
    summary = (f"=> {up}/{total} product surface(s) stood up for this "
               f"credential.")
    if inconclusive:
        summary += (f" ({inconclusive} inconclusive - best-effort endpoint "
                    f"not confirmed.)")
    print(f"  {ANSI['green' if up else 'red']}{summary}{ANSI['reset']}")


def probe_ajo_journey(token, conf):
    """Pinned 'do these creds reach AJO journeys?' check (test org only).

    Unlike the generic sweep, this GETs a KNOWN journey id in its OWN sandbox
    (AJO_JOURNEY_PROBE), because that is the only call that gives a definitive
    answer: the authoring host returns 'Api Key is invalid', a guessed list
    path 404s, and a missing/cross-sandbox id 500s -- only a 200 on a real id
    in the right sandbox proves AJO is reachable. Hardcoded for the test org."""
    sb = AJO_JOURNEY_PROBE["sandbox"]
    jid = AJO_JOURNEY_PROBE["journey_id"]
    url = f"https://platform.adobe.io/ajo/journey/{jid}"
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "x-sandbox-name": sb,
        "Accept": "application/json",
    }
    print()
    print(f"  {ANSI['bold']}Adobe Journey Optimizer journey GET{ANSI['reset']} "
          f"{ANSI['dim']}(pinned: sandbox '{sb}', journey {shorten(jid, 12)}){ANSI['reset']}")
    print(f"     {ANSI['yellow']}NOTE: hard-coded to ONE org's journey id + "
          f"sandbox (the test org). If you picked this up for a different org, "
          f"edit AJO_JOURNEY_PROBE at the top of this script to your own journey "
          f"id/sandbox - otherwise the result below is meaningless for you."
          f"{ANSI['reset']}")
    try:
        body, _ = http(url, headers=headers, timeout=20)
        name = "?"
        try:
            name = json.loads(body).get("name", "?")
        except Exception:
            pass
        print(f"     {ANSI['green']}[UP] reachable + authorized - journey "
              f"'{name}' returned. Credential REACHES AJO journeys.{ANSI['reset']}")
    except urllib.error.HTTPError as e:
        detail = clean_detail(e.read())
        if e.code in (401, 403):
            low = detail.lower()
            # AJO returns 403 for two very different reasons; name which one.
            if "403003" in detail or "api key is invalid" in low:
                why = ("this integration is NOT subscribed to the AJO product "
                       "- add its api-key to the AJO product profile in the "
                       "Admin Console")
            elif "not authorized" in low or "forbidden" in low:
                why = ("subscribed to AJO but this identity lacks the journey "
                       "role/permission")
            else:
                why = "reachable but not entitled"
            print(f"     {ANSI['yellow']}[NO-PERM] HTTP {e.code} - {why} - "
                  f"{detail}{ANSI['reset']}")
        elif e.code >= 500:
            print(f"     {ANSI['red']}[ERR] HTTP {e.code} - ambiguous: AJO 500s "
                  f"for BOTH a missing/cross-sandbox journey id AND a credential "
                  f"that lacks permission on the journey. If this id is known to "
                  f"exist in '{sb}' (it does for the test org), a 500 points to "
                  f"THIS credential's AJO/journey permissions, not the id - "
                  f"{detail}{ANSI['reset']}")
        else:
            print(f"     {ANSI['yellow']}[?] HTTP {e.code} - {detail}{ANSI['reset']}")
    except urllib.error.URLError as e:
        print(f"     {ANSI['red']}[DOWN] {type(e).__name__}: "
              f"{getattr(e, 'reason', e)}{ANSI['reset']}")
    except Exception as e:
        print(f"     {ANSI['red']}[DOWN] {type(e).__name__}: {e}{ANSI['reset']}")


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
        body = clean_detail(e.read(), 500)
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

        # Authoritative "which products does this credential reach?" answer --
        # see fetch_product_contexts. Not embedded in S2S JWTs, so this may do
        # a best-effort IMS profile call.
        codes, src = fetch_product_contexts(token, payload, resp, conf)
        if codes:
            print(f"  {ANSI['bold']}Provisioned products{ANSI['reset']} "
                  f"{ANSI['dim']}(projectedProductContext via {src}):{ANSI['reset']}")
            for c in codes:
                print(f"     {ANSI['cyan']}- {c}{ANSI['reset']}")
        else:
            print(f"  {ANSI['bold']}Provisioned products:{ANSI['reset']} "
                  f"{ANSI['dim']}undetermined ({src}){ANSI['reset']}")

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
        low = result.lower()
        if "403" in result or "permission" in low or "does not have" in low:
            print(f"  {ANSI['yellow']}    => Token MINTED fine but has NO sandbox "
                  f"access. Classic 'technical account not added to a product "
                  f"profile': the integration exists, but its tech account isn't "
                  f"granted any AEP/AJO permissions.{ANSI['reset']}")
            print(f"  {ANSI['yellow']}    => Fix: an admin adds THIS credential's "
                  f"technical account to the relevant product profiles in the "
                  f"Adobe Admin Console - then MINT A FRESH TOKEN (this tool does "
                  f"that on every run; permissions are snapshotted at mint time)."
                  f"{ANSI['reset']}")
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

    # 5) Product API surface sweep - one simple GET per AEP product to see
    #    which services stand up for this credential. sandbox_names[0] is the
    #    first sandbox resolved above (real list or config fallback).
    probe_product_surfaces(token, conf, sandbox_names[0] if sandbox_names else "")

    # 6) Pinned AJO journey GET - definitive AJO reachability for the test org
    #    (can't ride the sweep: a journey id only 200s in its own sandbox).
    probe_ajo_journey(token, conf)


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
    print_banner()
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
