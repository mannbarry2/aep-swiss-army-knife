"""
ajo_journey_export.py  --  AJO journey definitions, every node, for a sandbox
==========================================================================

Python port of Craig's ajo-journey-export/ajo-journey-export.mjs (kept in the
repo for provenance; the two produce interchangeable output). Pulls Adobe
Journey Optimizer journey definitions -- every node/step, i.e. the same content
as the UI's "Copy technical details" -- for a whole sandbox, straight from the
AJO authoring API. For each journey it saves the raw definition JSON, plus an
index.csv summarising node counts and the audiences referenced in conditions
(inSegment / inAudience).

!!  UNSUPPORTED ENDPOINT. journey-private.adobe.io/authoring is the private API
    behind the AJO UI. It is not an official/public Adobe API and can change
    without notice. Use it for inspection/reporting only; do not build critical
    automation on it. Strictly read-only (GET only) -- it never modifies a
    journey. There is a supported Journey Public API in development at Adobe
    (GET /ajo/journey/{id}?include=...) which is the long-term route.

!!  This is a UI hack and porting it to Python does not change that: the
    endpoint only accepts a USER (shell) bearer token, paired with the UI's own
    client id 'voyager_ui' as x-api-key. The service-account credentials in the
    keyring are NOT entitled, so the token has to be lifted from the browser.

HOW TO GET THE TOKEN (every run -- it expires within ~24h)
    1. Sign in to Adobe Experience Cloud in Chrome/Edge and open Journey
       Optimizer -> Journeys, in the sandbox you want to export.
    2. Press F12 (DevTools) -> Network tab -> tick "Fetch/XHR". If the list is
       empty, reload the Journeys page so requests appear.
    3. In the filter box type   journey-private   and click any request whose
       URL starts with journey-private.adobe.io/authoring/ (e.g. journeys/).
    4. In the right-hand pane open "Headers" -> "Request Headers".
         authorization:      Bearer eyJhbGci....   <- copy everything AFTER "Bearer "
         x-gw-ims-org-id:    XXXXXXXX@AdobeOrg     <- only needed without the keyring
       Tip: right-click the request -> Copy -> Copy as cURL also gives you
       both headers in one go.
    5. Give the token to this script one of three ways:
         set AJO_BEARER_TOKEN=eyJ...          (PowerShell: $env:AJO_BEARER_TOKEN="eyJ...")
         --token=eyJ...
         or just run it and paste at the masked prompt (pasting "Bearer eyJ..." is fine).
    A 401 / 403 means the token has expired or belongs to a different org --
    grab a fresh one. Never commit the token anywhere; it is your user session.

What IS reused from the rest of the knife: the org id from the keyring
credential (it is the same org the UI token belongs to, and it isn't secret),
the sandbox picker, the http helper, colours and logging.

Usage:
    set AJO_BEARER_TOKEN=eyJ...            (or --token=..., or paste at the prompt)
    python ajo_journey_export.py                         # creds menu -> org id; sandbox menu; ALL journeys
    python ajo_journey_export.py aep-prod --sandbox=prod
    python ajo_journey_export.py aep-prod --sandbox=prod --journey=<journeyUid>
    python ajo_journey_export.py aep-prod --sandbox=dev --out=./export-2026-09
    python ajo_journey_export.py --org-id=XXXX@AdobeOrg --sandbox=prod          # no keyring at all

Output (default output/ajo-journeys_<sandbox>_<YYYY-MM-DD>/):
    index.csv                       one row per journey (Craig's columns, unchanged)
    <JourneyName>__<uid>.json       full definition per journey (all nodes)

index.csv columns: journeyUid, displayName (current version name, what the UI
shows), listName (name from the list -- can be a stale duplicated name),
authoringFormatVersion (1.0 / 2.0), nodeCount, segmentRefCount, segmentIds
(space-separated), file.

Gotchas (Craig's, all still true):
  * Sandbox name is case-sensitive and must be lowercase -- "Prod" silently
    returns an empty result set. This script lowercases it and warns.
  * The list endpoint can return a stale journey-level name (..._Copy_Copy90);
    the real name is result.name from /latest. Both are reported.
  * Authoring format 1.0 and 2.0 both carry inSegment/inAudience in steps.
"""

from __future__ import annotations

import base64
import csv
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import aep_creds
import journey_audience_census as census   # http(), pick_sandbox(), ANSI, logger

SCRIPT_VERSION = "1.0"
SCRIPT_DATE = "2026-09-25"
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"

HOST = os.environ.get("AJO_HOST") or "https://journey-private.adobe.io/authoring"
DEFAULT_API_KEY = "voyager_ui"          # the AJO UI's own client id
CONCURRENCY = 5
PAGE_SIZE = 100
MAX_PAGES = 500
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")

ANSI = census.ANSI
logger = census.logger

# Printed at the paste prompt and again on a 401/403, so nobody has to open
# the file to remember the dance. Same steps as the header docstring.
TOKEN_HELP = f"""
  {ANSI['bold']}How to get the token{ANSI['reset']}  {ANSI['dim']}(browser session token; expires within ~24h){ANSI['reset']}
    1. Sign in to Experience Cloud, open Journey Optimizer -> Journeys (in the sandbox you want).
    2. F12 -> Network -> tick Fetch/XHR (reload the Journeys page if the list is empty).
    3. Filter on  journey-private  and click any journey-private.adobe.io/authoring/ request.
    4. Headers -> Request Headers -> authorization: copy everything AFTER "Bearer ".
       (x-gw-ims-org-id is on the same list if you are not using the keyring for the org id.)
    5. Paste it below, or set AJO_BEARER_TOKEN, or pass --token=...  (never commit it).
"""


# ----------------------------------------------------------------------------
# Helpers (1:1 with the .mjs)
# ----------------------------------------------------------------------------
def b64(obj) -> str:
    """Byte-identical to Node's Buffer.from(JSON.stringify(obj)).toString('base64')."""
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def safe_name(s) -> str:
    return re.sub(r"[^\w.-]+", "_", str(s or "journey"))[:120]


def extract_segment_refs(node, out: set) -> None:
    """Recursively pull inSegment / inAudience references out of a definition."""
    if isinstance(node, dict):
        if node.get("function") in ("inSegment", "inAudience"):
            for a in node.get("args") or []:
                v = a.get("value") if isinstance(a, dict) else None
                if isinstance(v, str) and UUID_RE.match(v):
                    out.add(v)
        for v in node.values():
            extract_segment_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            extract_segment_refs(v, out)


def get_json(url, headers, timeout=60):
    try:
        body, _ = census.http(url, headers=headers, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403):
            print(f"\n  {ANSI['red']}HTTP {e.code} -- the token was rejected: expired (~24h), "
                  f"wrong org, or not a browser session token.{ANSI['reset']}", file=sys.stderr)
            print(TOKEN_HELP, file=sys.stderr)
        raise RuntimeError(f"HTTP {e.code} for {url}\n{detail}") from None
    return json.loads(body)


# ----------------------------------------------------------------------------
# 1) enumerate journeys
# ----------------------------------------------------------------------------
def list_journeys(headers, one_journey=None) -> list[dict]:
    if one_journey:
        return [{"uid": one_journey, "name": one_journey}]
    fields = b64([["uid"], ["name"]])
    sorts = b64([{"direction": "ascending", "fields": ["name"]}])
    page, out = 0, []
    for _ in range(MAX_PAGES):
        data = get_json(f"{HOST}/journeys/", {**headers,
                                              "x-vyg-query-fields": fields,
                                              "x-vyg-query-page": str(page),
                                              "x-vyg-query-pagesize": str(PAGE_SIZE),
                                              "x-vyg-query-sorts": sorts})
        results = data.get("results") if isinstance(data.get("results"), list) else []
        out.extend(results)
        pg = data.get("pagination") or {}
        size = pg.get("pageSize") or PAGE_SIZE
        total = pg.get("totalCount") or 0
        sys.stdout.write(f"\r  listed {len(out)}/{pg.get('totalCount', '?')} journeys")
        sys.stdout.flush()
        if (page + 1) * size >= total or not results:
            break
        page += 1
    sys.stdout.write("\n")
    return out


# ----------------------------------------------------------------------------
# 2) fetch each definition + summarise
# ----------------------------------------------------------------------------
def export(headers, sandbox, out_dir: Path, one_journey=None) -> tuple[int, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  AJO Journey Exporter -> {out_dir}  (sandbox: {sandbox})")
    journeys = list_journeys(headers, one_journey)
    rows, ok, failed = [], 0, 0

    def one(j):
        nonlocal ok, failed
        uid = j.get("uid")
        try:
            definition = get_json(f"{HOST}/journeys/{uid}/latest", headers)
            r = definition.get("result") or definition
            steps = r.get("steps") if isinstance(r.get("steps"), list) else []
            segs: set = set()
            extract_segment_refs(definition, segs)
            display_name = r.get("name") or j.get("name")
            file = f"{safe_name(display_name)}__{uid}.json"
            (out_dir / file).write_text(json.dumps(definition, indent=2), encoding="utf-8")
            rows.append([uid, display_name, j.get("name"), r.get("authoringFormatVersion") or "",
                         len(steps), len(segs), " ".join(sorted(segs)), file])
            ok += 1
            sys.stdout.write(f"\r  exported {ok}/{len(journeys)}")
            sys.stdout.flush()
        except Exception as e:  # noqa: BLE001 -- one bad journey must not sink the run
            failed += 1
            print(f"\n  ! {uid} ({j.get('name')}): {e}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=min(CONCURRENCY, max(1, len(journeys)))) as ex:
        list(ex.map(one, journeys))
    sys.stdout.write("\n")

    # index.csv -- Craig's columns, CRLF, same quoting rules as the .mjs
    rows.sort(key=lambda r: (str(r[1]).lower(), r[0]))
    with open(out_dir / "index.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(["journeyUid", "displayName", "listName", "authoringFormatVersion",
                    "nodeCount", "segmentRefCount", "segmentIds", "file"])
        w.writerows(rows)
    print(f"\n  Done. {ok} exported, {failed} failed.")
    print(f"    Per-journey JSON: {out_dir}{os.sep}*.json")
    print(f"    Summary:          {out_dir}{os.sep}index.csv")
    return ok, failed


# ----------------------------------------------------------------------------
# Auth: user token from the browser; org id from env or the keyring credential
# ----------------------------------------------------------------------------
def resolve_token(arg_token) -> str:
    tok = arg_token or os.environ.get("AJO_BEARER_TOKEN") or ""
    if not tok and sys.stdin.isatty():
        print(TOKEN_HELP)
        tok = getpass.getpass("  Bearer token > ").strip()
    tok = tok.strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    if not tok:
        logger.error("No token. Set AJO_BEARER_TOKEN, pass --token=, or paste it at the prompt.")
        sys.exit(1)
    return tok


def resolve_org(arg_org, service) -> tuple[str, str]:
    """(org_id, source). Env / flag first; else the keyring credential's org_id."""
    org = arg_org or os.environ.get("AJO_ORG_ID")
    if org:
        return org, "flag/env"
    services = aep_creds.list_services()
    if service:
        name = census.resolve_service(services, service) or service
    elif services and sys.stdin.isatty():
        name = census.menu(services)
    else:
        name = services[0] if services else None
    if not name:
        logger.error("No org id: pass --org-id=, set AJO_ORG_ID, or store a credential "
                     "(credential_validator_v2.py store).")
        sys.exit(1)
    conf = aep_creds.load_creds(name)
    return conf["org_id"], f"keyring '{name}'"


def print_banner():
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}ajo_journey_export v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})")
    print(f"  {ANSI['dim']}Every AJO journey definition, every node -- via the UI's private "
          f"authoring API (unsupported; read-only).{ANSI['reset']}")
    print(bar)


def main():
    args = sys.argv[1:]
    opt = {}
    positional = []
    for a in args:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opt[k] = v
        elif a.startswith("-"):
            continue
        else:
            positional.append(a)
    print_banner()
    org, org_src = resolve_org(opt.get("org-id"), positional[0] if positional else None)
    token = resolve_token(opt.get("token"))
    api_key = opt.get("api-key") or os.environ.get("AJO_API_KEY") or DEFAULT_API_KEY

    sandbox = opt.get("sandbox") or os.environ.get("AJO_SANDBOX")
    if not sandbox:
        # The sandbox-management API may refuse a UI token; pick_sandbox falls
        # back to 'prod' on any error, so this never blocks.
        sandbox = census.pick_sandbox(token, api_key, {"org_id": org}, default="prod")[0]
    if sandbox != sandbox.lower():
        logger.warning(f"Sandbox names are case-sensitive on this endpoint; "
                       f"using {sandbox.lower()!r} not {sandbox!r}.")
        sandbox = sandbox.lower()

    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": api_key,
        "x-gw-ims-org-id": org,
        "x-sandbox-name": sandbox,
        "Content-Type": "application/json",
    }
    logger.info(f"org {org} ({org_src}); sandbox {sandbox}; api key {api_key}")
    out_dir = Path(opt["out"]) if opt.get("out") else (
        OUTPUT_DIR / f"ajo-journeys_{sandbox}_{datetime.now():%Y-%m-%d}")
    export(headers, sandbox, out_dir, opt.get("journey"))


if __name__ == "__main__":
    main()
