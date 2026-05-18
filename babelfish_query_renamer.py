#!/usr/bin/env python3
"""
babelfish_query_renamer.py
==========================
Lists, saves and renames AEP Query Service /query-templates owned by
MY_USER_ID. Authenticates with a Bearer token pasted from Postman.

These are the named/saved queries you see in the AEP Query Editor's
"Templates" panel -- NOT the execution history (which lives at /queries).

VDI-friendly: stdlib only, no pip install required.

First-time setup:
    1. Copy `config.example.json` to `config.json` (next to this script).
    2. Fill in client_id / client_secret / org_id (and optionally my_user_ids).
    3. python babelfish_query_renamer.py

`config.json` is gitignored -- never commit it. It contains the bearer token
and/or client_secret, which are credentials. A `sql\\` folder (also gitignored)
is created next to the script for the SQL exports, with one subfolder per
tenant and per sandbox.
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
#   my_user_ids        -- user IDs you own. Two formats supported:
#                            ["abc...", "def..."]                    (legacy)
#                            [{"id": "abc...", "label": "primary"}]  (preferred)
# Optional keys (Claude-API naming, added in v0.5):
#   anthropic_api_key  -- Anthropic API key. If set, Claude is used to suggest
#                         names from the SQL itself. Empty = skip, fall through
#                         to local heuristic.
#   anthropic_model    -- Claude model ID. Defaults to "claude-opus-4-7".
#   naming_config      -- Optional dict shaping Claude's output:
#                            {"style": "kebab-case", "max_length": 60,
#                             "instructions": "<extra rules>"}
# client_secret wins over bearer_token when both are set.
# ============================================================================

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _load_config() -> dict:
    """Read config.json next to this script. Hard-fail with a clear message
    if missing, malformed, or missing required keys."""
    if not CONFIG_PATH.exists():
        print(f"[ERROR] Config file not found: {CONFIG_PATH}", file=sys.stderr)
        print("[ERROR] Required JSON keys: bearer_token, client_id, "
              "client_secret, org_id, oauth_url, scopes, sandbox, "
              "sandbox_names, my_user_ids", file=sys.stderr)
        sys.exit(1)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[ERROR] {CONFIG_PATH} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    required = ["client_id", "org_id", "oauth_url", "scopes",
                "sandbox", "sandbox_names", "my_user_ids"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"[ERROR] {CONFIG_PATH} is missing required keys: {missing}",
              file=sys.stderr)
        sys.exit(1)
    return cfg


def _normalize_user_ids(raw: list) -> list[dict]:
    """Accept either ['abc', 'def'] (legacy) or [{'id': 'abc', 'label': '...'}]
    (preferred). Returns a uniform list of dicts so the rest of the code can
    rely on `entry['id']` / `entry['label']`."""
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
SANDBOX_NAMES      = list(_CFG["sandbox_names"])
MY_USER_IDS        = _normalize_user_ids(_CFG["my_user_ids"])
_LABELS_BY_ID      = {e["id"]: e["label"] for e in MY_USER_IDS}

# Optional Claude-API naming integration.
ANTHROPIC_API_KEY  = (_CFG.get("anthropic_api_key") or "").strip()
ANTHROPIC_MODEL    = _CFG.get("anthropic_model") or "claude-opus-4-7"
NAMING_CONFIG      = _CFG.get("naming_config") or {}

# ============================================================================

# Script identity (shown in the startup banner).
SCRIPT_NAME   = "babelfish_query_renamer"
SCRIPT_VERSION = "0.4.0"
SCRIPT_DATE   = "2026-05-07"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

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
    "FILTER": "\033[35m",    # magenta
    "PICK":   "\033[33m",    # yellow
    "SAVE":   "\033[92m",    # bright green
    "RENAME": "\033[93m",    # bright yellow
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
    print( "  Lists, saves, and renames AEP Query Service templates owned by you.")
    print( "  Auto-suggests names from each query's SQL; output goes to sql/<tenant>/<sandbox>/.")
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
    """Pretty display for a userId.

    When labelled in my_user_ids, return JUST the label -- the underlying
    hex adds nothing readable. The mapping is dumped once at startup
    (print_user_id_map) so you can still see what maps to what.

    When unlabelled, return the full userId so you can still recognise it
    by activity context and add it to my_user_ids in config.json."""
    if not uid:
        return "(no userId)"
    label = _LABELS_BY_ID.get(uid, "")
    if label:
        return label
    return uid


def _is_foreign(uid: str) -> bool:
    """A template is 'foreign' if its owner isn't one of your labelled IDs
    in my_user_ids -- could be a system account (aep_insights_enablement,
    sample_data, etc.), a colleague's, or just an unidentified user.

    Used to add extra friction before renaming and to tag the new name with
    a 'system' suffix so foreign-owned templates stand out in AEP later."""
    return bool(uid) and uid not in _LABELS_BY_ID


def print_user_id_map() -> None:
    """One-time printout near startup: which labels in config.json map to
    which userIds. So after this, the rest of the run can use just the label
    everywhere without losing the audit trail."""
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
    or renaming a template that lives in sandbox 'dev')."""
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


def suggest_from_sql(sql: str) -> str:
    """Generate a name like '<dataset> - <what it does>' by reading the SQL.

    Heuristic:
      1. Find the primary dataset (FROM / INSERT INTO / UPDATE / CREATE TABLE).
      2. Identify the operation and notable shape (aggregations, GROUP BY,
         WHERE, JOIN, LIMIT) and turn that into a short phrase.
    """
    if not sql or not sql.strip():
        return "(empty query)"

    sql_clean = sql.strip()
    sql_lower = sql_clean.lower()

    # ---- 1. Primary dataset ------------------------------------------------
    table = None
    patterns = [
        r"\binsert\s+(?:overwrite\s+)?into\s+([a-zA-Z_][\w.]*)",
        r"\bupdate\s+([a-zA-Z_][\w.]*)",
        r"\bdelete\s+from\s+([a-zA-Z_][\w.]*)",
        r"\bcreate\s+(?:or\s+replace\s+)?(?:temp\s+|temporary\s+)?"
        r"(?:table|view)\s+(?:if\s+not\s+exists\s+)?([a-zA-Z_][\w.]*)",
        r"\bfrom\s+([a-zA-Z_][\w.]*)",
    ]
    for pat in patterns:
        m = re.search(pat, sql_lower)
        if m:
            table = m.group(1)
            break

    if not table:
        # No FROM / INSERT / etc. -- typically SHOW TABLES, DESCRIBE foo,
        # USE bar, SET x=y. Use the first 1-2 words verbatim so the suggestion
        # actually describes the query, e.g. "show tables" rather than "(show)".
        words = [w.strip(";,()").lower() for w in sql_clean.split() if w.strip(";,()")]
        if not words:
            return "query"
        first = words[0]
        keyword_pairs = {"show", "describe", "desc", "explain", "use", "set",
                         "reset", "analyze", "optimize", "vacuum"}
        if first in keyword_pairs and len(words) >= 2:
            return f"{first} {words[1]}"
        return first

    dataset = table.split(".")[-1]  # strip schema/db prefix

    # ---- 2. Operation + shape ---------------------------------------------
    first_word = sql_lower.split()[0]
    if first_word in ("select", "with"):
        is_count    = bool(re.search(r"\bcount\s*\(", sql_lower))
        is_sum      = bool(re.search(r"\bsum\s*\(", sql_lower))
        is_avg      = bool(re.search(r"\bavg\s*\(", sql_lower))
        is_min_max  = bool(re.search(r"\b(min|max)\s*\(", sql_lower))
        is_distinct = bool(re.search(r"\bselect\s+distinct\b", sql_lower))
        is_star     = bool(re.search(r"\bselect\s+\*", sql_lower))
        has_group   = bool(re.search(r"\bgroup\s+by\b", sql_lower))
        has_where   = bool(re.search(r"\bwhere\b", sql_lower))
        has_join    = bool(re.search(r"\bjoin\b", sql_lower))
        m_limit     = re.search(r"\blimit\s+(\d+)", sql_lower)

        if is_count and has_group:
            verb = "count by group"
        elif is_count:
            verb = "row count"
        elif is_sum:
            verb = "sum"
        elif is_avg:
            verb = "average"
        elif is_min_max:
            verb = "min/max"
        elif is_distinct:
            verb = "distinct values"
        elif is_star:
            verb = "select all"
        else:
            verb = "select columns"

        modifiers = []
        if has_join:
            modifiers.append("with join")
        if has_where:
            modifiers.append("filtered")
        if m_limit:
            modifiers.append(f"top {m_limit.group(1)}")

        description = f"{verb} ({', '.join(modifiers)})" if modifiers else verb
    elif first_word == "insert":
        description = "insert overwrite" if "overwrite" in sql_lower else "insert"
    elif first_word == "update":
        description = "update"
    elif first_word == "delete":
        description = "delete"
    elif first_word == "create":
        if re.search(r"\bcreate\s+(?:or\s+replace\s+)?(?:temp\s+|temporary\s+)?table\b", sql_lower):
            description = "create table"
        elif re.search(r"\bcreate\s+(?:or\s+replace\s+)?view\b", sql_lower):
            description = "create view"
        elif re.search(r"\bcreate\s+(?:or\s+replace\s+)?procedure\b", sql_lower):
            description = "create procedure"
        else:
            description = "create"
    elif first_word == "drop":
        description = "drop"
    else:
        description = first_word

    return f"{dataset} - {description}"


def _detect_description(sql: str) -> str | None:
    """Look for an explicit name/description in the leading SQL comments.

    Recognises:
        -- name: <text>
        -- description: <text>
        /* name: <text> */ or /* description: <text> */ at the very top
    Returns the value if found, None otherwise. The script's own header
    (-- Template ID :, -- Sandbox :, etc.) is ignored -- we look only for
    name/description-prefixed comments."""
    if not sql or not sql.strip():
        return None
    for line in sql.splitlines()[:20]:
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(
            r"^\s*--\s*(?:name|description)\s*[:=]\s*(.+?)\s*$",
            line,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()
        if not stripped.startswith("--"):
            break
    m = re.match(
        r"^\s*/\*\s*(?:name|description)\s*[:=]\s*([^*]+?)\s*\*/",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).strip().splitlines()[0].strip()
    return None


_CLAUDE_CACHE: dict[str, str] = {}


def _claude_suggest_name(sql: str) -> str | None:
    """Call Claude's Messages API to suggest a name from the SQL. Returns the
    name on success, None on any failure (no API key, network error, empty
    response). Cached per-SQL for the lifetime of the run.

    Uses stdlib urllib so the script keeps its no-pip-install promise.
    Marks the system prompt as cacheable on the Anthropic side -- a no-op for
    short prompts (4096-token min on Opus 4.7) but ready when naming_config
    grows into a longer rules document."""
    if not ANTHROPIC_API_KEY:
        return None
    cache_key = sql.strip()
    if cache_key in _CLAUDE_CACHE:
        return _CLAUDE_CACHE[cache_key]

    style       = NAMING_CONFIG.get("style") or "kebab-case"
    max_length  = NAMING_CONFIG.get("max_length") or 60
    rules       = (NAMING_CONFIG.get("instructions") or "").strip()

    system_text = (
        "You suggest concise, descriptive names for AEP Query Service templates "
        f"based on their SQL. Return ONLY the name -- no quotes, no explanation, "
        f"no markdown, no trailing punctuation. Use {style}. "
        f"Maximum {max_length} characters."
    )
    if rules:
        system_text += f"\n\nAdditional rules:\n{rules}"

    body = json.dumps({
        "model":      ANTHROPIC_MODEL,
        "max_tokens": 64,
        "system":     [{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{
            "role": "user",
            "content": f"SQL:\n\n{sql.strip()[:2000]}",
        }],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key":          ANTHROPIC_API_KEY,
            "anthropic-version":  "2023-06-01",
            "Content-Type":       "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")[:200]
        step("ERROR", f"Claude API HTTP {e.code}: {text}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        step("ERROR", f"Claude API call failed: {e}")
        return None

    for block in payload.get("content", []):
        if block.get("type") == "text":
            name = (block.get("text") or "").strip().strip('"').strip("'")
            if name:
                _CLAUDE_CACHE[cache_key] = name
                return name
    return None


def suggest_name_with_source(sql: str, owner: str = "") -> tuple[str, str]:
    """Pick the best name suggestion plus a label for the source. Returns
    (suggestion, source) where source is 'description', 'AI', or 'heuristic'.

    AI-generated names are tagged with a suffix so they stand out in AEP's
    Templates panel later. Two suffixes:
      - naming_config.ai_suffix (default ' [babelfish]') for templates owned
        by your labelled user IDs.
      - naming_config.ai_foreign_suffix (default ' [babelfish system]') for
        templates owned by anyone else (system accounts, colleagues, etc.).
        This makes it visible in the UI that you renamed a foreign-owned
        table.

    Order:
      1. Explicit description in SQL (-- name:/-- description:/...).
      2. Claude API (if ANTHROPIC_API_KEY is configured).
      3. Local heuristic from suggest_from_sql -- always works."""
    desc = _detect_description(sql)
    if desc:
        return desc, "description"
    ai = _claude_suggest_name(sql)
    if ai:
        if owner and _is_foreign(owner):
            suffix = NAMING_CONFIG.get("ai_foreign_suffix", " [babelfish system]")
        else:
            suffix = NAMING_CONFIG.get("ai_suffix", " [babelfish]")
        # Don't double-tag if Claude already included the suffix or if the
        # SQL was previously auto-named on a prior run.
        if suffix and not ai.rstrip().endswith(suffix.strip()):
            ai = f"{ai}{suffix}"
        return ai, "AI"
    return suggest_from_sql(sql), "heuristic"


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
        f"-- (saved by babelfish_query_renamer)\n\n"
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
    '2026-05-07T14:00:00+01:00'. Goes into snapshot + mega-file headers."""
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


def rename_template(template_id: str, new_name: str, sql: str, sandbox: str) -> bool:
    url = f"{TEMPLATES_URL}/{template_id}"
    status, text = http_request("PUT", url, auth_headers(sandbox=sandbox),
                                body={"name": new_name, "sql": sql})
    if status < 200 or status >= 300:
        step("ERROR", f"PUT {url} (sandbox={sandbox}) -> HTTP {status}: {text[:300]}")
        return False
    step("RENAME", f"OK - sandbox '{sandbox}' template {template_id} -> '{new_name}'.")
    return True


def pick_user_ids(templates: list[dict]) -> list[str]:
    """Multi-select picker. Shows the userIds present in `templates` along
    with any label from MY_USER_IDS, plus a date range and sample template
    names for each user so you can recognise your own activity (Adobe
    doesn't expose an API to resolve IMS IDs to emails on this auth setup).
    Accepts comma- or space-separated indices (e.g. '1,3' or '1 3'), or
    'a' for no filter. Returns the list of chosen userIds (empty list = no
    filter)."""
    from collections import Counter
    counts = Counter(t.get("userId", "") for t in templates if t.get("userId"))
    if not counts:
        step("PICK", "No userIds in the response; using no filter.")
        return []

    # Build per-user enrichment: date range + a couple of sample template names.
    per_user: dict[str, dict] = {}
    for t in templates:
        uid = t.get("userId", "")
        if not uid:
            continue
        info = per_user.setdefault(uid, {"dates": [], "names": []})
        c = (t.get("created") or "")[:10]
        if c:
            info["dates"].append(c)
        nm = t.get("name") or ""
        if nm:
            info["names"].append(nm)

    items = counts.most_common()
    print()
    step("PICK", "Choose userId(s) to filter by (you can pick more than one):")
    step("PICK", "Adobe doesn't expose an API to resolve these IDs to emails "
                 "on this auth setup, so use the date range + sample names "
                 "below to recognise your own activity.")
    for i, (uid, n) in enumerate(items, 1):
        label = _LABELS_BY_ID.get(uid, "")
        label_str = f"  -- {label}" if label else ""
        print(f"  [{i:>2}] {n:>5} templates    {uid}{label_str}")
        info = per_user.get(uid, {})
        dates = info.get("dates") or []
        names = info.get("names") or []
        if dates:
            date_range = (
                f"{min(dates)}..{max(dates)}" if min(dates) != max(dates)
                else f"on {min(dates)}"
            )
        else:
            date_range = "no dates"
        sample = " | ".join(n[:40] for n in names[:3])
        print(f"            activity {date_range}")
        if sample:
            print(f"            recent names: {sample}")
    print( "  [ a]                  (no filter -- show every template)")
    print()
    while True:
        try:
            raw = input("  Pick number(s) (e.g. '1' or '1,3' or 'a'): ")
        except EOFError:
            step("ERROR", "stdin closed; cannot pick. Set my_user_ids in config.json.")
            sys.exit(1)
        choice = raw.replace(chr(0xfeff), "").strip().lower()
        if choice == "a":
            return []
        try:
            nums = [int(x) for x in re.split(r"[,\s]+", choice) if x]
            if not nums or not all(1 <= n <= len(items) for n in nums):
                raise ValueError
            picked_uids = [items[n - 1][0] for n in nums]
            picked_uids = list(dict.fromkeys(picked_uids))  # de-dupe, preserve order
            step("PICK", f"Selected {len(picked_uids)} userId(s):")
            for uid in picked_uids:
                step("PICK", f"  - {_display_owner(uid)}")
            step("PICK", "Tip: add these (with labels) to my_user_ids in "
                         "config.json to skip this menu next time.")
            return picked_uids
        except ValueError:
            pass
        print(f"  Invalid choice '{choice}'. Try again.")


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


def confirm_user_ids(templates: list[dict]) -> list[str]:
    """Always prompt to confirm which user(s) to filter by. Returns a list of
    userIds (empty = no filter).

    If any MY_USER_IDS entries appear in the response, pre-select all of them
    and ask for confirmation -- this handles cases like 'I have an old
    decommissioned account AND a current one in the same org'. 'p' drops to
    the multi-select picker. 'a' = no filter."""
    from collections import Counter
    counts = Counter(t.get("userId", "") for t in templates if t.get("userId"))
    if not counts:
        step("PICK", "No userIds in the response; no filter applied.")
        return []

    matching_ids = [e["id"] for e in MY_USER_IDS if e["id"] in counts]

    if not matching_ids:
        step("PICK", "None of my_user_ids appear in this response (likely a "
                     "different tenant). Showing every userId found -- pick "
                     "yours, then add them to my_user_ids in config.json.")
        return pick_user_ids(templates)

    # Per-sandbox breakdown across ALL matching IDs -- shows the combined total
    # and what's coming from where, so a number like "62" isn't a mystery.
    per_sb: dict[str, int] = {}
    matching_set = set(matching_ids)
    for t in templates:
        if t.get("userId") in matching_set:
            sb = t.get("_sandbox", "?")
            per_sb[sb] = per_sb.get(sb, 0) + 1
    total = sum(per_sb.values())

    print()
    step("PICK", f"Found {len(matching_ids)} of your known userId(s) in this response:")
    for uid in matching_ids:
        step("PICK", f"  - {_display_owner(uid)}  ({counts[uid]} templates)")

    sb_w = max((len(s) for s in per_sb), default=14)
    print(f"        {'SANDBOX':<{sb_w}}  COUNT")
    print(f"        {'-' * sb_w}  -----")
    for sb in sorted(per_sb):
        print(f"        {sb:<{sb_w}}  {per_sb[sb]:>5}")
    print(f"        {'TOTAL':<{sb_w}}  {total:>5}")

    try:
        raw = input("  Enter=use all of these, 'p'=pick from full list, 'a'=no filter: ")
    except EOFError:
        step("ERROR", "stdin closed; cannot confirm. Set my_user_ids in config.json.")
        sys.exit(1)
    choice = raw.replace(chr(0xfeff), "").strip().lower()
    if choice == "":
        return matching_ids
    if choice == "a":
        return []
    return pick_user_ids(templates)


def ask_sandbox_filter(mine: list[dict]) -> set[str] | None:
    """Ask which sandbox to focus the rename loop on. Returns a set of
    sandbox names (always one entry), or None for 'all'. Skipped silently
    when only one sandbox is in scope -- nothing to pick.

    When `mine` includes foreign-owned templates (because 'a' = no user
    filter was selected, or you picked an unlabelled user from the picker),
    the per-sandbox count splits into 'yours / foreign' so you can see
    exactly what's about to be renamed in each sandbox."""
    from collections import Counter
    counts = Counter(t.get("_sandbox", "?") for t in mine)
    if len(counts) <= 1:
        return None
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    print()
    step("PICK", "Which sandbox to rename in?")
    for i, (sb, total) in enumerate(items, 1):
        sb_templates = [t for t in mine if t.get("_sandbox") == sb]
        yours = sum(1 for t in sb_templates if not _is_foreign(t.get("userId", "")))
        foreign = total - yours
        if foreign and yours:
            descr = f"{total} total -- {yours} yours, {foreign} FOREIGN"
        elif foreign:
            descr = f"{foreign} FOREIGN (none of your labelled user IDs)"
        else:
            descr = f"{yours} of your templates"
        print(f"  [{i:>2}] {sb} ({descr})")
    print( "  [ a]  all sandboxes")
    print()
    while True:
        try:
            raw = input("  Pick a number or 'a' (default 'a'): ").strip().lower()
        except EOFError:
            return None
        if raw in ("", "a"):
            return None
        try:
            n = int(raw)
            if 1 <= n <= len(items):
                return {items[n - 1][0]}
        except ValueError:
            pass
        print(f"  Invalid choice '{raw}'. Try again.")


def ask_rename_mode(mine: list[dict]) -> tuple[bool, bool]:
    """Ask interactive vs batch. Returns (auto_accept, exclude_foreign).

    auto_accept=True means apply the suggestion to every template without
    per-template prompting.

    exclude_foreign=True means drop templates owned by users not in
    my_user_ids before processing. Renaming foreign-owned templates is
    risky -- they could be system tables (e.g. aep_insights_enablement) or
    a colleague's. Batch mode requires typing 'YES' (uppercase, exact) to
    include them; anything else excludes."""
    from collections import Counter
    count = len(mine)
    print()
    step("PICK", f"Rename mode for {count} template(s):")
    print( "  [enter]  interactive  - review each suggestion individually")
    print( "  [batch]  batch        - auto-accept every suggestion without asking")
    print()
    try:
        raw = input("  Mode (default interactive): ").strip().lower()
    except EOFError:
        return False, False
    if raw not in ("batch", "b"):
        return False, False

    owners = Counter(t.get("userId") or "(no userId)" for t in mine)
    foreign_count = sum(n for uid, n in owners.items() if _is_foreign(uid))

    print()
    step("PICK", f"BATCH MODE will rename these {count} template(s):")
    for uid, n in owners.most_common():
        owner_disp = _display_owner(uid)
        flag = "   [!] FOREIGN -- not in your my_user_ids" if _is_foreign(uid) else ""
        print(f"    {n:>3} owned by  {owner_disp}{flag}")
    print()

    exclude_foreign = False
    if foreign_count > 0:
        step("PICK", f"WARNING: {foreign_count} of these are NOT owned by your "
                      "labelled user IDs.")
        step("PICK", "Renaming them will change tables that may belong to system "
                      "accounts (aep_insights_enablement, sample_data, etc.) or "
                      "to colleagues. Foreign renames will be tagged with the "
                      "'[babelfish system]' suffix so they're identifiable later.")
        try:
            inc = input("  Type 'YES' (uppercase, exact) to INCLUDE foreign-owned "
                         "templates, or anything else to exclude them: ").strip()
        except EOFError:
            return False, True
        if inc != "YES":
            exclude_foreign = True
            step("PICK", f"Excluding {foreign_count} foreign-owned templates "
                          f"from this batch run.")
        else:
            step("PICK", f"Including {foreign_count} foreign-owned templates -- "
                          f"you accepted responsibility.")

    try:
        confirm = input("  Final confirm -- proceed with batch rename? (y/N): ").strip().lower()
    except EOFError:
        return False, exclude_foreign
    if confirm in ("y", "yes"):
        step("PICK", "Batch mode confirmed -- auto-accepting every suggestion.")
        return True, exclude_foreign
    step("PICK", "Cancelled batch mode; falling back to interactive.")
    return False, exclude_foreign


def main() -> None:
    print_banner()
    step("START", f"{SCRIPT_NAME} starting.")
    print_user_id_map()

    # 1. Resolve sandboxes and lay out sql/<sandbox>/ folders BEFORE fetching,
    #    so the structure is visible (and any listing failure stops us early).
    sandboxes = discover_sandboxes()
    prepare_folder_structure(sandboxes)

    # 2. Fetch templates from each sandbox.
    templates = fetch_all_templates(sandboxes)

    # 3. Confirm whose templates to act on (always asks for confirmation).
    selected_ids = confirm_user_ids(templates)
    if selected_ids:
        selected_set = set(selected_ids)
        mine = [t for t in templates if t.get("userId") in selected_set]
        step("FILTER", f"User filter: {len(selected_set)} ID(s) selected -> "
                        f"{len(mine)} template(s) of {len(templates)} in this tenant.")
    else:
        mine = list(templates)
        step("FILTER", f"NO USER FILTER -- including every template "
                        f"({len(templates)} total, regardless of owner).")

    # 3b. Optionally narrow to a single sandbox (skipped when only one sandbox
    #     is in the picture anyway, or when stdin isn't a TTY).
    interactive = sys.stdin.isatty()
    if interactive and mine:
        chosen_sbs = ask_sandbox_filter(mine)
        if chosen_sbs is not None:
            before = len(mine)
            mine = [t for t in mine if t.get("_sandbox") in chosen_sbs]
            step("FILTER", f"Sandbox filter: {sorted(chosen_sbs)} -> "
                            f"{len(mine)} template(s) of {before} remaining.")

    # 3c. Interactive vs batch mode for the rename loop. Batch mode also
    #     asks whether to include foreign-owned templates (system accounts,
    #     colleagues) -- defaults to excluding them unless you type YES.
    auto_accept = False
    exclude_foreign = False
    if interactive and mine:
        auto_accept, exclude_foreign = ask_rename_mode(mine)
    if exclude_foreign:
        before = len(mine)
        mine = [t for t in mine if not _is_foreign(t.get("userId", ""))]
        step("FILTER", f"Excluded {before - len(mine)} foreign-owned template(s); "
                        f"{len(mine)} remaining.")

    # 4. Print the summary table.
    rows = []
    for t in mine:
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
        print(f"{created:<20} {sandbox:<10} {client:<25} {userid:<55} {tid:<38} {name:<40} {sql}")
    print(f"\nTotal: {len(rows)} of {len(templates)} templates")

    if not mine:
        return

    # 5. For each template: pick a name (interactively or via batch auto-accept),
    #    apply it, save the .sql to disk. Save happens AFTER any rename so the
    #    filename reflects the new name; skipped templates still get saved with
    #    their old name.
    if auto_accept:
        step("RENAME", f"Batch mode: auto-accepting suggestions for {len(mine)} template(s).")
    elif interactive:
        step("RENAME", "Interactive: Enter=accept suggestion, type a new name, "
                       "or 's'=skip rename (still saved).")
    else:
        step("RENAME", "stdin is not a TTY; saving with current names without renaming.")

    # AEP rejects duplicate names within a sandbox. Track every existing
    # name (including templates owned by other users) so we can skip PUTs
    # that would trivially clash, instead of round-tripping AEP for the 400.
    def _norm(s: str) -> str:
        s = (s or "").replace(chr(0xfeff), "")
        return re.sub(r"\s+", " ", s).strip().casefold()

    existing_by_sandbox: dict[str, set[str]] = {}
    for tpl in templates:
        sb = tpl.get("_sandbox", "")
        nm = _norm(tpl.get("name", "") or "")
        if nm:
            existing_by_sandbox.setdefault(sb, set()).add(nm)

    for t in mine:
        tid     = t.get("id", "")
        old     = t.get("name", "") or "(unnamed)"
        sql     = t.get("sql", "") or ""
        sandbox = t.get("_sandbox", "")
        owner   = t.get("userId", "") or "(no userId)"
        owner_disp = _display_owner(owner)
        is_foreign = _is_foreign(owner)
        owner_line = (
            f"{owner_disp}   [!] FOREIGN -- not in your my_user_ids"
            if is_foreign else owner_disp
        )

        new_name: str | None = None
        if auto_accept:
            suggest, source = suggest_name_with_source(sql, owner)
            new_name = suggest
            step("RENAME", f"[batch] {sandbox} | owner {owner_line} | "
                            f"{old!r} -> {suggest!r} (source: {source})")
        elif interactive:
            suggest, source = suggest_name_with_source(sql, owner)
            print()
            print(f"  Owner       : {owner_line}")
            print(f"  Sandbox     : {sandbox}")
            print(f"  Current name: {old}")
            print(f"  Suggestion  : {suggest}")
            print(f"  Source      : {source}")
            print(f"  SQL preview : {sql.strip()[:120]}")
            try:
                raw = input("  New name (Enter=accept, 's'=skip rename): ")
            except EOFError:
                step("RENAME", "stdin closed; saving remainder with current names.")
                interactive = False
                raw = "s"
            answer = raw.replace(chr(0xfeff), "").strip()
            if answer.lower() == "s":
                step("RENAME", "Skipped rename.")
            else:
                new_name = answer if answer else suggest

        if new_name is not None:
            new_norm = _norm(new_name)
            old_norm = _norm(old)
            existing = existing_by_sandbox.get(sandbox, set())
            if new_norm == old_norm:
                step("RENAME", f"Name unchanged ('{old}'); no PUT needed.")
            elif new_norm in existing:
                step("RENAME", f"Skip PUT - '{new_name}' already exists in "
                               f"sandbox '{sandbox}' on a different template.")
            else:
                step("RENAME", f"PUT '{old}' -> '{new_name}'")
                if rename_template(tid, new_name, sql, sandbox):
                    t["name"] = new_name  # save below uses the new name
                    existing.discard(old_norm)
                    existing.add(new_norm)
                    existing_by_sandbox[sandbox] = existing

        path = save_template_sql(t, SQL_DIR)
        step("SAVE", f"  -> {path.relative_to(SQL_DIR.parent)}")

    # 6. Snapshot this run's full template list to sql/<tenant>/_snapshot.json,
    #    then rebuild the cross-tenant mega file at sql/all_queries_mega_file.md
    #    by reading every tenant's snapshot. This way one file accumulates every
    #    org you've run against (one label per tenant) instead of a per-tenant
    #    file each time.
    snap_path = write_tenant_snapshot(templates, SQL_DIR)
    step("SAVE", f"Snapshot: {snap_path.relative_to(SQL_DIR.parent.parent)} "
                  f"({len(templates)} templates from {len(sandboxes)} sandbox(es))")

    sql_root = SQL_DIR.parent
    # Clean up the previous-version per-tenant mega file if it's still there.
    old_per_tenant = SQL_DIR / "all_queries_mega_file.md"
    if old_per_tenant.exists():
        old_per_tenant.unlink()
    md_path = write_cross_tenant_mega_markdown(sql_root)
    step("SAVE", f"Mega file (cross-tenant): "
                  f"{md_path.relative_to(SQL_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
