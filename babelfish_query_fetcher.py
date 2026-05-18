#!/usr/bin/env python3
"""
babelfish_query_fetcher.py
==========================
Read-only fork of babelfish_query_renamer. Lists and SAVES AEP Query
Service /query-templates -- it never writes back to AEP. No PUT, no
rename, no name suggestions, no prompts. Just pulls every accessible
query down into local files and one cross-tenant Markdown file.

These are the named/saved queries you see in the AEP Query Editor's
"Templates" panel -- NOT the execution history (which lives at /queries).

VDI-friendly: stdlib only, no pip install required. Fully non-interactive,
so it's safe to run on a schedule / in CI.

First-time setup:
    1. Copy `config.example.json` to `config.json` (next to this script).
    2. Fill in client_id / client_secret / org_id.
    3. python babelfish_query_fetcher.py

`config.json` is gitignored -- never commit it. It contains the bearer
token and/or client_secret, which are credentials. A `sql\\` folder (also
gitignored) is created next to the script for the SQL exports, with one
subfolder per tenant and per sandbox.

Output:
    sql/<tenant>/<sandbox>/<name>.sql   one file per template
    sql/<tenant>/_snapshot.json         this run's full list (per tenant)
    sql/all_queries_mega_file.md        every tenant, assembled from snapshots
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

# ============================================================================
# CONFIG
# ----------------------------------------------------------------------------
# All tunable values live in `config.json` next to this script. Required keys:
#   bearer_token       -- pasted access token (~24h); fallback for local
#                         testing only. Leave "" in normal operation.
#   client_id          -- Adobe IMS client ID
#   client_secret      -- IMS client_credentials secret. PREFERRED -- mints a
#                         fresh token every run.
#   org_id             -- Adobe org ID (e.g. "ABC@AdobeOrg")
#   oauth_url          -- IMS token endpoint
#   scopes             -- IMS scopes (comma-separated)
#   sandbox            -- "all" or a specific sandbox name
#   sandbox_names      -- fallback list when sandbox-management API is denied
# Optional keys (read for display only -- this script never renames):
#   org_labels         -- {org_id: friendly label} for sql/<tenant>/ folders
#   my_user_ids        -- labelled owner IDs; used only to annotate output
# Keys the renamer used (anthropic_*, naming_config) are ignored here.
# client_secret wins over bearer_token when both are set.
# ============================================================================

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _load_config() -> dict:
    """Read config.json next to this script. Hard-fail with a clear message
    if missing, malformed, or missing required keys."""
    if not CONFIG_PATH.exists():
        print(f"[ERROR] Config file not found: {CONFIG_PATH}", file=sys.stderr)
        print("[ERROR] Required JSON keys: client_id, client_secret, org_id, "
              "oauth_url, scopes, sandbox, sandbox_names", file=sys.stderr)
        sys.exit(1)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[ERROR] {CONFIG_PATH} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    # sandbox_names is only consulted as a fallback when sandbox == "all";
    # configs that pin a single sandbox (e.g. "prod") legitimately omit it.
    required = ["client_id", "org_id", "oauth_url", "scopes", "sandbox"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"[ERROR] {CONFIG_PATH} is missing required keys: {missing}",
              file=sys.stderr)
        sys.exit(1)
    return cfg


def _normalize_user_ids(raw: list) -> list[dict]:
    """Accept either ['abc', 'def'] (legacy) or [{'id': 'abc', 'label': '...'}]
    (preferred). Returns a uniform list of dicts. Used only to label owners in
    the output -- this script never filters or renames by owner."""
    out: list[dict] = []
    for entry in raw or []:
        if isinstance(entry, str):
            out.append({"id": entry, "label": ""})
        elif isinstance(entry, dict) and entry.get("id"):
            out.append({"id": entry["id"], "label": (entry.get("label") or "").strip()})
    return out


_CFG               = _load_config()
BEARER_TOKEN       = _CFG.get("bearer_token", "")
CLIENT_ID          = _CFG["client_id"]
CLIENT_SECRET      = _CFG.get("client_secret", "")
ORG_ID             = _CFG["org_id"]
OAUTH_URL          = _CFG["oauth_url"]
SCOPES             = _CFG["scopes"]
SANDBOX            = _CFG["sandbox"]
SANDBOX_NAMES      = list(_CFG.get("sandbox_names") or [])
MY_USER_IDS        = _normalize_user_ids(_CFG.get("my_user_ids") or [])
_LABELS_BY_ID      = {e["id"]: e["label"] for e in MY_USER_IDS}

# ============================================================================

# Script identity (shown in the startup banner).
SCRIPT_NAME    = "babelfish_query_fetcher"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE    = "2026-05-18"
SCRIPT_AUTHOR  = "Barry Mann (barrymann.com)"

TEMPLATES_URL = "https://platform.adobe.io/data/foundation/query/query-templates"
SANDBOX_URL   = "https://platform.adobe.io/data/foundation/sandbox-management/sandboxes"
PAGE_LIMIT    = 50

# Adobe doesn't expose org names via API, so we map org_id -> a friendly label
# to namespace sql/<tenant>/<sandbox>/... so two orgs with the same sandbox
# name (e.g. both have 'prod') don't collide. Real org_id<->label pairs live
# in the gitignored config.json under "org_labels" (keeps real org IDs and
# client descriptors out of version control). The dict below is example-only.
ORG_LABELS: dict[str, str] = {
    # "<your-org-id>@AdobeOrg": "car-insurance",  # real pairs go in config.json
}


def tenant_for_org(org_id: str) -> str:
    """Friendly label for an Adobe org_id. Consults config.json "org_labels"
    first (local, gitignored), then the example map, then falls back to a
    short prefix so different unknown orgs still get distinct folders."""
    local_labels = _CFG.get("org_labels") or {}
    if org_id in local_labels:
        return local_labels[org_id]
    if org_id in ORG_LABELS:
        return ORG_LABELS[org_id]
    return f"org-{org_id.split('@')[0][:8]}"


TENANT  = tenant_for_org(ORG_ID)
SQL_DIR = Path(__file__).resolve().parent / "sql" / TENANT


# ---- Coloured logging --------------------------------------------------------
# ANSI colour codes; we enable VT processing on Windows so PowerShell/cmd
# render them. Auto-disabled when stdout isn't a terminal (e.g. piped to a file).
_USE_COLOR = sys.stdout.isatty()
if _USE_COLOR and sys.platform == "win32":
    try:
        import ctypes
        _k32 = ctypes.windll.kernel32
        _h = _k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        _mode = ctypes.c_ulong()
        if _k32.GetConsoleMode(_h, ctypes.byref(_mode)):
            _k32.SetConsoleMode(_h, _mode.value | 0x4)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        else:
            _USE_COLOR = False
    except Exception:
        _USE_COLOR = False

_RESET   = "\033[0m"
_TAG_COLORS = {
    "START":  "\033[1;36m",  # bold cyan
    "AUTH":   "\033[34m",    # blue
    "FETCH":  "\033[36m",    # cyan
    "PREP":   "\033[32m",    # green
    "SAVE":   "\033[92m",    # bright green
    "SKIP":   "\033[90m",    # grey -- non-fatal "you don't have access here" notes
    "ERROR":  "\033[1;31m",  # bold red
    "HINT":   "\033[33m",    # yellow
}


def step(tag: str, msg: str) -> None:
    """Log one line, prefixed with a [TAG] indicating the current step."""
    if _USE_COLOR:
        color = _TAG_COLORS.get(tag, "")
        print(f"{color}[{tag}]{_RESET} {msg}", flush=True)
    else:
        print(f"[{tag}] {msg}", flush=True)


def print_banner() -> None:
    """Print a short header so each run is self-identifying in the log."""
    bar    = "=" * 72
    head   = f"\033[1;36m{bar}\033[0m" if _USE_COLOR else bar
    title  = f"\033[1m{SCRIPT_NAME} v{SCRIPT_VERSION}\033[0m" if _USE_COLOR \
             else f"{SCRIPT_NAME} v{SCRIPT_VERSION}"
    print(head)
    print(f"  {title}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print( "  READ-ONLY: lists and saves AEP Query Service templates.")
    print( "  Never writes back to AEP. Output goes to sql/<tenant>/<sandbox>/.")
    print(f"  Tenant for this run: {TENANT}    (org_id: {ORG_ID})")
    print(head)


_CACHED_TOKEN: str | None = None


def _mask_secret(s: str, keep: int = 4) -> str:
    """Return a short masked version of a secret -- e.g.
    'p8e-Yc3mHk' -> '********mHk'. Always renders as 8 stars + last `keep`
    chars + length, regardless of original length, so a 1500-char bearer
    token doesn't produce a screen full of asterisks."""
    if not s:
        return "(empty)"
    if len(s) <= keep:
        return "*" * len(s)
    return f"********{s[-keep:]} ({len(s)} chars)"


def _display_owner(uid: str) -> str:
    """Pretty display for a userId. Returns the friendly label when the ID is
    listed in my_user_ids, otherwise the raw userId. Display only."""
    if not uid:
        return "(no userId)"
    return _LABELS_BY_ID.get(uid, "") or uid


def print_user_id_map() -> None:
    """One-time printout near startup: which labels in config.json map to
    which userIds, so owner columns in the output stay readable."""
    if not MY_USER_IDS:
        return
    step("AUTH", "userId labels (from config.json my_user_ids):")
    for entry in MY_USER_IDS:
        label = entry.get("label") or "(no label)"
        step("AUTH", f"  {label}  =  {entry['id']}")


def fetch_oauth_token() -> str:
    """POST to Adobe IMS to mint a fresh access token via client_credentials."""
    body = urlencode({
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
        "scope":         SCOPES,
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    step("AUTH", f"POST {OAUTH_URL} (client_credentials, "
                  f"client_id={CLIENT_ID}, client_secret={_mask_secret(CLIENT_SECRET)})...")
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")[:300]
        step("ERROR", f"IMS auth failed: HTTP {e.code} {text}")
        sys.exit(1)
    except urllib.error.URLError as e:
        step("ERROR", f"IMS auth network error: {e.reason}")
        sys.exit(1)
    token = payload.get("access_token")
    if not token:
        step("ERROR", f"IMS response had no access_token: {payload}")
        sys.exit(1)
    expires_in = payload.get("expires_in", "?")
    step("AUTH", f"OK - got access token (expires in {expires_in}s).")
    return token


def get_token() -> str:
    """Return a valid bearer token, fetching from IMS if needed (cached per run).

    Prefers minting via OAuth client_credentials so every session picks up
    current permissions/credentials. Only falls back to the pasted
    BEARER_TOKEN when no client_secret is configured."""
    global _CACHED_TOKEN
    if _CACHED_TOKEN is None:
        if CLIENT_SECRET:
            _CACHED_TOKEN = fetch_oauth_token()
        elif BEARER_TOKEN:
            step("AUTH", f"No client_secret configured; using fallback "
                          f"bearer_token={_mask_secret(BEARER_TOKEN)} "
                          f"(may be expired).")
            _CACHED_TOKEN = BEARER_TOKEN
        else:
            step("ERROR", "Neither client_secret nor bearer_token is set in "
                          "config.json - cannot authenticate.")
            sys.exit(1)
    return _CACHED_TOKEN


def auth_headers(sandbox: str | None = None) -> dict:
    """Build the standard request headers. `sandbox` overrides SANDBOX for
    requests that need to target a specific sandbox (e.g. fetching templates
    from sandbox 'dev')."""
    headers = {
        "Authorization":   f"Bearer {get_token()}",
        "x-api-key":       CLIENT_ID,
        "x-gw-ims-org-id": ORG_ID,
        "Accept":          "application/json",
        "Content-Type":    "application/json",
    }
    sb = sandbox if sandbox is not None else SANDBOX
    if sb and sb != "all":
        headers["x-sandbox-name"] = sb
    return headers


def list_sandboxes() -> list[str]:
    """Return the names of every sandbox the token can see."""
    step("FETCH", f"GET {SANDBOX_URL} (listing all sandboxes)...")
    # Sandbox-management endpoint does NOT take x-sandbox-name itself.
    headers = auth_headers(sandbox="")
    headers.pop("x-sandbox-name", None)
    status, text = http_request("GET", SANDBOX_URL, headers)
    if status == 403:
        step("SKIP", "Sandbox-management API denied (403) -- token lacks the "
                      "management read scope. Falling back to sandbox_names "
                      "from config.json. This is expected on minimally-scoped "
                      "Query Service tokens.")
        return []
    if status < 200 or status >= 300:
        step("ERROR", f"Sandbox list failed: HTTP {status} {text[:200]}")
        return []
    body = json.loads(text)
    names = [s.get("name") for s in body.get("sandboxes", []) if s.get("name")]
    step("FETCH", f"  -> sandboxes available: {names}")
    return names


def http_request(method, url, headers, params=None, body=None):
    """Stdlib-only HTTP. Returns (status_code, response_text).

    Never raises on HTTP error codes -- 4xx/5xx are returned as a normal
    (status, text) pair so callers can branch on them.
    """
    if params:
        url = f"{url}?{urlencode(params)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        # Network/DNS/proxy failures land here. Surface them as a 0 status.
        return 0, f"URLError: {e.reason}"


def sanitize_filename(name: str) -> str:
    """Make a string safe to use as a filename on Windows + POSIX."""
    s = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name)  # forbidden chars
    s = re.sub(r"\s+", "_", s).strip("._ ")
    return s or "untitled"


def save_template_sql(template: dict, dest_dir: Path) -> Path:
    """Write the template's SQL to dest_dir/<sandbox>/<name>.sql with a header."""
    sandbox = template.get("_sandbox", "unknown")
    sandbox_dir = dest_dir / sanitize_filename(sandbox)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    name    = template.get("name", "") or "untitled"
    tid     = template.get("id", "")
    sql     = template.get("sql", "") or ""
    userid  = template.get("userId", "")
    created = template.get("created", "")
    updated = template.get("updated", "")

    filename = f"{sanitize_filename(name)}.sql"
    path = sandbox_dir / filename
    header = (
        f"-- Template ID : {tid}\n"
        f"-- Sandbox     : {sandbox}\n"
        f"-- Name        : {name}\n"
        f"-- userId      : {userid}\n"
        f"-- created     : {created}\n"
        f"-- updated     : {updated}\n"
        f"-- (saved by babelfish_query_fetcher -- read-only)\n\n"
    )
    path.write_text(header + sql, encoding="utf-8")
    return path


_SQL_FIRST_WORDS = {
    "select", "with", "insert", "update", "delete", "create", "drop", "alter",
    "truncate", "merge", "show", "describe", "desc", "explain", "use", "set",
    "reset", "analyze", "optimize", "vacuum", "begin", "commit", "rollback",
    "start", "grant", "revoke", "copy",
}


def looks_like_valid_sql(sql: str) -> bool:
    """Cheap heuristic: non-empty and starts with a recognised SQL keyword.
    Used to filter the mega-file so an LLM downstream sees only real queries."""
    if not sql or not sql.strip():
        return False
    first = sql.strip().split()[0].lower().rstrip(";,()")
    return first in _SQL_FIRST_WORDS


def _now_iso() -> str:
    """Local time with timezone offset, second precision -- e.g.
    '2026-05-18T14:00:00+01:00'. Goes into snapshot + mega-file headers."""
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_tenant_snapshot(templates: list[dict], dest_dir: Path) -> Path:
    """Persist this run's full template list (every sandbox, every owner) to a
    JSON snapshot at dest_dir/_snapshot.json. The cross-tenant mega writer
    reads these from each tenant's folder so a single file can span every
    Adobe org you've ever run against."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = dest_dir / "_snapshot.json"
    snapshot = {
        "tenant":        TENANT,
        "org_id":        ORG_ID,
        "generated_at":  _now_iso(),
        "script":        f"{SCRIPT_NAME} v{SCRIPT_VERSION}",
        "sandboxes":     sorted({t.get("_sandbox", "?") for t in templates}),
        "templates": [
            {
                "id":       t.get("id", ""),
                "name":     t.get("name", "") or "(unnamed)",
                "sandbox":  t.get("_sandbox", "?"),
                "userId":   t.get("userId", ""),
                "created":  t.get("created", ""),
                "updated":  t.get("updated", ""),
                "sql":      t.get("sql", "") or "",
            }
            for t in templates
        ],
    }
    snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return snapshot_path


def write_cross_tenant_mega_markdown(sql_root: Path) -> Path:
    """Read every tenant's _snapshot.json under sql_root and assemble ONE
    cross-tenant Markdown file at sql_root/all_queries_mega_file.md. Each run
    refreshes its own tenant snapshot; other tenants' snapshots are preserved,
    so the mega file accumulates a complete cross-org archive."""
    snapshots: list[dict] = []
    for snap_path in sorted(sql_root.glob("*/_snapshot.json")):
        try:
            snapshots.append(json.loads(snap_path.read_text(encoding="utf-8")))
        except Exception as e:
            step("ERROR", f"Skipping malformed snapshot {snap_path}: {e}")

    md_path = sql_root / "all_queries_mega_file.md"

    lines: list[str] = []
    lines.append("# AEP Query Templates - All Tenants")
    lines.append("")
    lines.append("## Manifest")
    lines.append("")
    lines.append(f"- Generated: {_now_iso()}")
    lines.append(f"- Source script: {SCRIPT_NAME} v{SCRIPT_VERSION}")
    lines.append(f"- Tenants: {len(snapshots)}")
    for s in snapshots:
        valid = sum(1 for t in s.get("templates", [])
                    if looks_like_valid_sql(t.get("sql", "")))
        lines.append(f"  - `{s['tenant']}` (org `{s['org_id']}`): "
                     f"{valid} valid templates from "
                     f"{', '.join(f'`{sb}`' for sb in s.get('sandboxes', []))} "
                     f"- snapshot {s.get('generated_at', '?')}")
    lines.append("")

    for s in snapshots:
        lines.append(f"# Tenant: {s['tenant']}")
        lines.append("")
        lines.append(f"- Org ID: `{s['org_id']}`")
        lines.append(f"- Snapshot taken: {s.get('generated_at', '?')}")
        lines.append("")

        by_sandbox: dict[str, list[dict]] = {}
        skipped: list[dict] = []
        for t in s.get("templates", []):
            if not looks_like_valid_sql(t.get("sql", "") or ""):
                skipped.append(t)
                continue
            by_sandbox.setdefault(t.get("sandbox", "?"), []).append(t)

        for sb in sorted(by_sandbox):
            lines.append(f"## {s['tenant']} / `{sb}`")
            lines.append("")
            for t in by_sandbox[sb]:
                name = t.get("name", "") or "(unnamed)"
                tid = t.get("id", "")
                created = t.get("created", "")
                updated = t.get("updated", "")
                sql = (t.get("sql", "") or "").strip()
                lines.append(f"### {name}")
                lines.append("")
                lines.append(f"- ID: `{tid}`")
                lines.append(f"- Created: {created}")
                lines.append(f"- Updated: {updated}")
                lines.append("")
                lines.append("```sql")
                lines.append(sql)
                lines.append("```")
                lines.append("")

        if skipped:
            lines.append(f"## {s['tenant']} - Skipped (does not look like SQL)")
            lines.append("")
            for t in skipped:
                name = t.get("name", "") or "(unnamed)"
                tid = t.get("id", "")
                sb = t.get("sandbox", "?")
                preview = ((t.get("sql", "") or "").strip().replace("\n", " "))[:100]
                lines.append(f"- `{sb}` - {name} (`{tid}`): {preview!r}")
            lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def _rtf_escape(text: str) -> str:
    """Escape a string for inclusion in RTF body text.

    RTF reserves \\, { and }; everything outside 7-bit ASCII must be emitted
    as a \\uN? unicode escape (N is a signed 16-bit code unit). Newlines and
    tabs become RTF line/tab control words so SQL keeps its shape."""
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "{":
            out.append("\\{")
        elif ch == "}":
            out.append("\\}")
        elif ch == "\n":
            out.append("\\line\n")
        elif ch == "\r":
            continue
        elif ch == "\t":
            out.append("\\tab ")
        elif ord(ch) < 128:
            out.append(ch)
        else:
            code = ord(ch)
            if code > 0xFFFF:  # outside the BMP -> surrogate pair
                code -= 0x10000
                hi = 0xD800 + (code >> 10)
                lo = 0xDC00 + (code & 0x3FF)
                out.append(f"\\u{hi}?\\u{lo - 65536}?")
            else:
                signed = code if code < 32768 else code - 65536
                out.append(f"\\u{signed}?")
    return "".join(out)


def write_cross_tenant_rtf(sql_root: Path) -> Path:
    """Assemble the same content as the mega Markdown file into a neat,
    formatted RTF document at sql_root/all_queries.rtf -- a title header,
    per-tenant / per-sandbox sections, bold query names, greyed metadata,
    and SQL in a monospace font. Reads the same _snapshot.json files, so it
    is purely a local render -- no network, no writes to AEP."""
    snapshots: list[dict] = []
    for snap_path in sorted(sql_root.glob("*/_snapshot.json")):
        try:
            snapshots.append(json.loads(snap_path.read_text(encoding="utf-8")))
        except Exception as e:
            step("ERROR", f"Skipping malformed snapshot {snap_path}: {e}")

    rtf_path = sql_root / "all_queries.rtf"
    total_valid = sum(
        sum(1 for t in s.get("templates", []) if looks_like_valid_sql(t.get("sql", "")))
        for s in snapshots
    )

    # Half-point font sizes (\fsN). f0 = Calibri (sans), f1 = Consolas (mono).
    # Colours: 1=black, 2=accent blue (headings), 3=grey (metadata).
    p: list[str] = []
    p.append(r"{\rtf1\ansi\ansicpg1252\deff0")
    p.append(r"{\fonttbl{\f0\fswiss Calibri;}{\f1\fmodern Consolas;}}")
    p.append(r"{\colortbl;\red20\green20\blue20;"
             r"\red0\green51\blue102;\red110\green110\blue110;}")
    p.append(r"\margl1440\margr1440\margt1440\margb1440")
    p.append(r"\sa120\f0\fs20\cf1")

    # ---- Title header ------------------------------------------------------
    p.append(r"{\qc\cf2\b\fs48 AEP Query Templates\b0\par}")
    p.append(r"{\qc\cf3\fs20 Read-only export \endash  human-authored "
             r"queries only\par}")
    p.append(r"\sa80\fs18\cf3")
    p.append(f"Generated: {_rtf_escape(_now_iso())}\\line")
    p.append(f"Source: {_rtf_escape(SCRIPT_NAME)} v{_rtf_escape(SCRIPT_VERSION)}\\line")
    p.append(f"Tenants: {len(snapshots)}  \\endash   "
             f"{total_valid} queries total\\par")
    for s in snapshots:
        valid = sum(1 for t in s.get("templates", [])
                    if looks_like_valid_sql(t.get("sql", "")))
        sbs = ", ".join(s.get("sandboxes", []))
        p.append(_rtf_escape(
            f"• {s['tenant']} (org {s['org_id']}): {valid} queries "
            f"from {sbs} — snapshot {s.get('generated_at', '?')}") + r"\par")
    p.append(r"\sa120\cf1\fs20")
    p.append(r"{\pard\brdrb\brdrs\brdrw10\brsp20\par}")

    # ---- Body --------------------------------------------------------------
    for s in snapshots:
        p.append(r"\sb240\cf2\b\fs36 "
                 + _rtf_escape(f"Tenant: {s['tenant']}") + r"\b0\cf1\par")
        p.append(r"\sb0\cf3\fs18 "
                 + _rtf_escape(f"Org {s['org_id']}  —  snapshot "
                               f"{s.get('generated_at', '?')}") + r"\cf1\fs20\par")

        by_sandbox: dict[str, list[dict]] = {}
        for t in s.get("templates", []):
            if not looks_like_valid_sql(t.get("sql", "") or ""):
                continue
            by_sandbox.setdefault(t.get("sandbox", "?"), []).append(t)

        for sb in sorted(by_sandbox):
            p.append(r"\sb200\cf2\b\fs28 "
                     + _rtf_escape(f"{sb}  ({len(by_sandbox[sb])})")
                     + r"\b0\cf1\par")
            for t in by_sandbox[sb]:
                name = t.get("name", "") or "(unnamed)"
                tid = t.get("id", "")
                created = t.get("created", "")
                updated = t.get("updated", "")
                sql = (t.get("sql", "") or "").strip()
                p.append(r"\sb160\b\fs24 " + _rtf_escape(name) + r"\b0\par")
                p.append(r"\sb0\cf3\fs16 "
                         + _rtf_escape(f"ID {tid}   •   created {created}"
                                       f"   •   updated {updated}")
                         + r"\cf1\par")
                p.append(r"\sb40\f1\fs18 " + _rtf_escape(sql) + r"\f0\fs20\par")

    p.append("}")
    rtf_path.write_text("".join(c if c.endswith("\n") else c + "\n" for c in p),
                        encoding="utf-8")
    return rtf_path


# Adobe provisions service/integration accounts (the AEP Insights Enablement
# data-warehouse loaders, Bizible, sample_data, etc.) under the "@AdobeID"
# IMS namespace. Real human users authenticate with hex userIds suffixed by
# their org domain (e.g. "@dow.com"). "@AdobeID" is therefore a clean,
# org-independent marker for "this is a system query, not human-authored".
_SYSTEM_OWNER_SUFFIX = "@AdobeID"


def _is_system_query(t: dict) -> bool:
    """True if a template is owned by an Adobe system/service account rather
    than a human. Used to keep auto-provisioned queries out of the export."""
    return (t.get("userId") or "").endswith(_SYSTEM_OWNER_SUFFIX)


_NO_ACCESS_SANDBOXES: list[str] = []  # populated by fetch_templates_in_sandbox


def fetch_templates_in_sandbox(sandbox: str) -> list[dict]:
    """Fetch all templates from a single sandbox, tagging each with `_sandbox`.

    A 403 from this endpoint means the token authenticated fine but the user
    lacks Query Service permission in *this specific sandbox*. That's not an
    error condition for the run -- it's expected on tokens scoped to one or
    two sandboxes per org. Log it as [SKIP] and move on; we'll show a summary
    at the end."""
    step("FETCH", f"Sandbox '{sandbox}': listing templates...")
    headers = auth_headers(sandbox=sandbox)
    out: list[dict] = []
    start = None
    page = 0
    while True:
        page += 1
        params = {"limit": PAGE_LIMIT, "orderby": "-created"}
        if start:
            params["start"] = start
        status, text = http_request("GET", TEMPLATES_URL, headers, params=params)
        if status == 401:
            step("ERROR", "401 Unauthorized - token expired/invalid.")
            sys.exit(1)
        if status == 403:
            step("SKIP", f"  no Query Service access in sandbox '{sandbox}' "
                          f"(token lacks the right scope/role here).")
            _NO_ACCESS_SANDBOXES.append(sandbox)
            return []
        if status < 200 or status >= 300:
            step("ERROR", f"  page {page}: HTTP {status} {text[:200]}")
            break
        body = json.loads(text)
        batch = body.get("templates", [])
        for t in batch:
            t["_sandbox"] = sandbox
        out.extend(batch)
        step("FETCH", f"  page {page}: got {len(batch)} (sandbox total {len(out)}).")
        next_cursor = (body.get("_page") or {}).get("next")
        if not batch or len(batch) < PAGE_LIMIT or not next_cursor:
            break
        start = next_cursor
    step("FETCH", f"  sandbox '{sandbox}' done - {len(out)} templates.")
    return out


def discover_sandboxes() -> list[str]:
    """Resolve which sandboxes to scan.

    SANDBOX="all" tries the sandbox-management API. If that fails (typically
    HTTP 403 on tokens without management scope), fall back to SANDBOX_NAMES
    so the script still works on minimally-scoped tokens. Hard-fails only when
    both the API call AND the configured fallback list are empty."""
    if SANDBOX != "all":
        return [SANDBOX]
    sandboxes = list_sandboxes()
    if sandboxes:
        step("PREP", f"Using {len(sandboxes)} sandbox(es) from sandbox-management API.")
        return sandboxes
    if SANDBOX_NAMES:
        step("PREP", f"Sandbox-management API returned nothing; using "
                     f"configured SANDBOX_NAMES fallback: {SANDBOX_NAMES}")
        return list(SANDBOX_NAMES)
    step("ERROR", "Sandbox listing returned empty AND SANDBOX_NAMES is empty. "
                  "Either get a token with sandbox-management read scope, or "
                  "fill in sandbox_names in config.json.")
    sys.exit(1)


def prepare_folder_structure(sandboxes: list[str]) -> None:
    """Create sql/<sandbox>/ for every sandbox upfront so the layout is visible
    before we fetch anything."""
    SQL_DIR.mkdir(parents=True, exist_ok=True)
    for sb in sandboxes:
        (SQL_DIR / sanitize_filename(sb)).mkdir(parents=True, exist_ok=True)
    step("PREP", f"Folder structure ready under {SQL_DIR}: {sandboxes}")


def fetch_all_templates(sandboxes: list[str]) -> list[dict]:
    """Fetch templates from each of the given sandboxes."""
    _NO_ACCESS_SANDBOXES.clear()
    all_templates: list[dict] = []
    for sb in sandboxes:
        all_templates.extend(fetch_templates_in_sandbox(sb))
    accessed = len(sandboxes) - len(_NO_ACCESS_SANDBOXES)
    summary = (f"Done - {len(all_templates)} templates from {accessed} of "
               f"{len(sandboxes)} sandbox(es)")
    if _NO_ACCESS_SANDBOXES:
        summary += (f"; no Query Service access in: "
                    f"{', '.join(_NO_ACCESS_SANDBOXES)}")
    step("FETCH", summary + ".")
    return all_templates


def main() -> None:
    print_banner()
    step("START", f"{SCRIPT_NAME} starting (read-only -- no writes back to AEP).")
    print_user_id_map()

    # 1. Resolve sandboxes and lay out sql/<sandbox>/ folders BEFORE fetching,
    #    so the structure is visible (and any listing failure stops us early).
    sandboxes = discover_sandboxes()
    prepare_folder_structure(sandboxes)

    # 2. Fetch every template from every accessible sandbox (no owner filter --
    #    a fetcher pulls everything it can see).
    templates = fetch_all_templates(sandboxes)

    # 2b. Drop system-owned templates (Adobe service accounts: AEP Insights
    #     Enablement loaders, Bizible, sample_data, ...). We want only
    #     human-authored queries in the export.
    from collections import Counter
    system = [t for t in templates if _is_system_query(t)]
    templates = [t for t in templates if not _is_system_query(t)]
    if system:
        owners = ", ".join(f"{uid} ({n})" for uid, n
                           in Counter(t.get("userId", "") for t in system).most_common())
        step("FILTER", f"Excluded {len(system)} system-owned template(s): {owners}")
        step("FILTER", f"{len(templates)} human-authored template(s) remain.")

    # 3. Print the summary table.
    rows = []
    for t in templates:
        tid     = t.get("id", "")
        name    = t.get("name", "") or "(unnamed)"
        sandbox = t.get("_sandbox", "?")
        client  = t.get("clientId", "") or ""
        created = (t.get("created", "") or "")[:19]
        userid  = t.get("userId", "") or ""
        sql     = (t.get("sql", "") or "").strip().replace("\n", " ")[:80]
        rows.append((created, sandbox, client, userid, tid, name, sql))

    print()
    print(f"{'CREATED':<20} {'SANDBOX':<10} {'CLIENT':<25} {'USERID':<55} {'ID':<38} {'NAME':<40} SQL")
    print("-" * 230)
    for created, sandbox, client, userid, tid, name, sql in rows:
        owner = _display_owner(userid) if userid else ""
        print(f"{created:<20} {sandbox:<10} {client:<25} {owner:<55} {tid:<38} {name:<40} {sql}")
    print(f"\nTotal: {len(rows)} templates")

    if not templates:
        return

    # 4. Save each template's SQL to disk. Pure local writes -- nothing is
    #    sent back to AEP.
    for t in templates:
        path = save_template_sql(t, SQL_DIR)
        step("SAVE", f"  -> {path.relative_to(SQL_DIR.parent)}")

    # 5. Snapshot this run's full template list to sql/<tenant>/_snapshot.json,
    #    then rebuild the cross-tenant mega file at sql/all_queries_mega_file.md
    #    by reading every tenant's snapshot. This way one file accumulates every
    #    org you've run against (one label per tenant) instead of a per-tenant
    #    file each time.
    snap_path = write_tenant_snapshot(templates, SQL_DIR)
    step("SAVE", f"Snapshot: {snap_path.relative_to(SQL_DIR.parent.parent)} "
                  f"({len(templates)} templates from {len(sandboxes)} sandbox(es))")

    sql_root = SQL_DIR.parent
    md_path = write_cross_tenant_mega_markdown(sql_root)
    step("SAVE", f"Mega file (cross-tenant): "
                  f"{md_path.relative_to(SQL_DIR.parent.parent)}")
    rtf_path = write_cross_tenant_rtf(sql_root)
    step("SAVE", f"RTF (cross-tenant):       "
                  f"{rtf_path.relative_to(SQL_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
