#!/usr/bin/env python3
"""
babelfish_query_renamer.py
==========================
Lists, saves and renames AEP Query Service /query-templates owned by
MY_USER_IDS. Mints a fresh IMS token each run via client_credentials.

These are the named/saved queries you see in the AEP Query Editor's
"Templates" panel -- NOT the execution history (which lives at /queries).

VDI-friendly: stdlib only, no pip install required.

Credentials come from the shared credential bank in ./creds/ (the same
<tenant>.json files credential_validator.py and babelfish_query_fetcher.py
use). On a normal run it prompts you to pick which credential set to use
(e.g. acme-insurance, acme alpha); the name can also be supplied on the CLI
to run unattended (see Usage).

Usage:
    python babelfish_query_renamer.py                 # interactive menu
    python babelfish_query_renamer.py acme-insurance   # cred set by name (stem)
    python babelfish_query_renamer.py acme-alpha

The creds JSONs are gitignored -- never commit them. They contain the
client_secret, which is a credential. A `sql\\` folder (also gitignored) is
created next to the script for the SQL exports, with one subfolder per tenant
and per sandbox.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

# ============================================================================
# CONFIG -- shared credential bank (./creds/)
# ----------------------------------------------------------------------------
# Credentials are read from the same ./creds/<tenant>.json bank that
# credential_validator.py uses, picked interactively (or by name on the CLI).
# Recognised keys per credential file:
#   client_id          -- Adobe IMS client ID
#   client_secret      -- IMS client_credentials secret (REQUIRED -- mints a
#                         fresh token every run)
#   api_key            -- x-api-key, if different from client_id (optional)
#   org_id             -- Adobe org ID (e.g. "ABC@AdobeOrg")
#   oauth_url          -- IMS token endpoint (optional -- sensible default)
#   scopes             -- IMS scopes, comma-separated (optional -- default)
#   sandbox            -- "all" or a specific sandbox name (optional)
#   sandbox_names      -- fallback list when sandbox-management API is denied
#   org_labels         -- {org_id: friendly label} for sql/<tenant>/ folders
#   my_user_ids        -- user IDs you own. Two formats supported:
#                            ["abc...", "def..."]                    (legacy)
#                            [{"id": "abc...", "label": "primary"}]  (preferred)
#   bearer_token       -- pasted access token; fallback when no client_secret
# Optional keys (Claude-API naming):
#   anthropic_api_key  -- Anthropic API key. If set, Claude is used to suggest
#                         names from the SQL itself. Empty = skip, fall through
#                         to local heuristic.
#   anthropic_model    -- Claude model ID. Defaults to "claude-opus-4-7".
#   naming_config      -- Optional dict shaping Claude's output:
#                            {"style": "kebab-case", "max_length": 60,
#                             "instructions": "<extra rules>"}
# Keys "_"-prefixed are treated as comments and ignored.
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
CREDS_DIR  = SCRIPT_DIR / "creds"

DEFAULT_OAUTH_URL = "https://ims-na1.adobelogin.com/ims/token"
DEFAULT_SCOPES    = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)


def load_creds(path: Path) -> dict:
    """Read one credential JSON from ./creds/. Strips "_"-prefixed comment keys
    and trims whitespace from string values. Hard-requires the three keys we
    cannot run without."""
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


def discover_creds() -> list[Path]:
    """Return the ordered list of credential JSONs in ./creds/ (skipping the
    example template)."""
    paths: list[Path] = []
    if CREDS_DIR.exists():
        for p in sorted(CREDS_DIR.glob("*.json")):
            if p.stem == "example":
                continue
            paths.append(p)
    return paths


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


# ---- Coloured logging --------------------------------------------------------
# Standard timestamped, level-coloured logging (matches credential_validator.py).
# We enable VT processing on Windows so PowerShell/cmd render the ANSI colours.
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
SCRIPT_NAME = "babelfish_query_renamer"
logger = logging.getLogger(SCRIPT_NAME)


# These are populated per-run by apply_config() once a credential set has been
# chosen, rather than at import time. A run targets exactly one credential set.
CRED_PATH: Path | None = None
_CFG: dict           = {}
BEARER_TOKEN         = ""
CLIENT_ID            = ""
CLIENT_SECRET        = ""
API_KEY              = ""
ORG_ID               = ""
OAUTH_URL            = DEFAULT_OAUTH_URL
SCOPES               = DEFAULT_SCOPES
SANDBOX              = "all"
SANDBOX_NAMES: list[str] = []
MY_USER_IDS: list[dict]  = []
_LABELS_BY_ID: dict  = {}
ANTHROPIC_API_KEY    = ""
ANTHROPIC_MODEL      = "claude-opus-4-7"
NAMING_CONFIG: dict  = {}
TENANT               = ""
SQL_DIR: Path | None = None


def apply_config(conf: dict, path: Path) -> None:
    """Load one chosen credential set into the module-level globals every other
    function reads. Also derives the tenant label and sql/<tenant>/ output
    folder, reads the optional Anthropic naming keys, and clears any cached
    token from a previous credential set."""
    global CRED_PATH, _CFG, BEARER_TOKEN, CLIENT_ID, CLIENT_SECRET, API_KEY
    global ORG_ID, OAUTH_URL, SCOPES, SANDBOX, SANDBOX_NAMES
    global MY_USER_IDS, _LABELS_BY_ID, ANTHROPIC_API_KEY, ANTHROPIC_MODEL
    global NAMING_CONFIG, TENANT, SQL_DIR, _CACHED_TOKEN
    CRED_PATH     = path
    _CFG          = conf
    BEARER_TOKEN  = conf.get("bearer_token", "")
    CLIENT_ID     = conf["client_id"]
    CLIENT_SECRET = conf.get("client_secret", "")
    API_KEY       = conf.get("api_key") or CLIENT_ID
    ORG_ID        = conf["org_id"]
    OAUTH_URL     = conf.get("oauth_url") or DEFAULT_OAUTH_URL
    SCOPES        = conf.get("scopes") or DEFAULT_SCOPES
    SANDBOX       = conf.get("sandbox") or "all"
    SANDBOX_NAMES = list(conf.get("sandbox_names") or [])
    MY_USER_IDS   = _normalize_user_ids(conf.get("my_user_ids") or [])
    _LABELS_BY_ID = {e["id"]: e["label"] for e in MY_USER_IDS}
    ANTHROPIC_API_KEY = (conf.get("anthropic_api_key") or "").strip()
    ANTHROPIC_MODEL   = conf.get("anthropic_model") or "claude-opus-4-7"
    NAMING_CONFIG     = conf.get("naming_config") or {}
    TENANT        = tenant_for_org(ORG_ID)
    SQL_DIR       = SCRIPT_DIR / "sql" / TENANT
    _CACHED_TOKEN = None

# ============================================================================

# Script identity (shown in the startup banner).
SCRIPT_VERSION = "0.4.0"
SCRIPT_DATE   = "2026-05-07"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

TEMPLATES_URL = "https://platform.adobe.io/data/foundation/query/query-templates"
SANDBOX_URL   = "https://platform.adobe.io/data/foundation/sandbox-management/sandboxes"
PAGE_LIMIT    = 50

# Adobe doesn't expose org names via API, so we map org_id -> a friendly label
# to namespace sql/<tenant>/<sandbox>/... so two orgs with the same sandbox
# name (e.g. both have 'prod') don't collide. Real org_id<->label pairs live
# in the gitignored creds file under "org_labels" (keeps real org IDs and
# client descriptors out of version control). The dict below is example-only.
ORG_LABELS: dict[str, str] = {
    # "<your-org-id>@AdobeOrg": "acme-insurance",  # real pairs go in the creds file
}


def tenant_for_org(org_id: str) -> str:
    """Friendly label for an Adobe org_id. Consults the creds file "org_labels"
    first (local, gitignored), then the example map, then falls back to a
    short prefix so different unknown orgs still get distinct folders."""
    local_labels = _CFG.get("org_labels") or {}
    if org_id in local_labels:
        return local_labels[org_id]
    if org_id in ORG_LABELS:
        return ORG_LABELS[org_id]
    return f"org-{org_id.split('@')[0][:8]}"


def print_banner() -> None:
    """Print a short header so each run is self-identifying in the log."""
    bar    = "=" * 72
    head   = f"{ANSI['cyan']}{ANSI['bold']}{bar}{ANSI['reset']}"
    title  = f"{ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}"
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
    by activity context and add it to my_user_ids in the creds file."""
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
    """One-time printout near startup: which labels in the creds file map to
    which userIds. So after this, the rest of the run can use just the label
    everywhere without losing the audit trail."""
    if not MY_USER_IDS:
        return
    logger.info("userId labels (from creds file my_user_ids):")
    for entry in MY_USER_IDS:
        label = entry.get("label") or "(no label)"
        logger.info(f"  {label}  =  {entry['id']}")


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
    logger.info(f"POST {OAUTH_URL} (client_credentials, "
                f"client_id={CLIENT_ID}, client_secret={_mask_secret(CLIENT_SECRET)})...")
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")[:300]
        logger.error(f"IMS auth failed: HTTP {e.code} {text}")
        sys.exit(1)
    except urllib.error.URLError as e:
        logger.error(f"IMS auth network error: {e.reason}")
        sys.exit(1)
    token = payload.get("access_token")
    if not token:
        logger.error(f"IMS response had no access_token: {payload}")
        sys.exit(1)
    expires_in = payload.get("expires_in", "?")
    logger.info(f"OK - got access token (expires in {expires_in}s).")
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
            logger.info(f"No client_secret configured; using fallback "
                        f"bearer_token={_mask_secret(BEARER_TOKEN)} "
                        f"(may be expired).")
            _CACHED_TOKEN = BEARER_TOKEN
        else:
            logger.error("Neither client_secret nor bearer_token is set in "
                         "the creds file - cannot authenticate.")
            sys.exit(1)
    return _CACHED_TOKEN


def auth_headers(sandbox: str | None = None) -> dict:
    """Build the standard request headers. `sandbox` overrides SANDBOX for
    requests that need to target a specific sandbox (e.g. fetching templates
    or renaming a template that lives in sandbox 'dev')."""
    headers = {
        "Authorization":   f"Bearer {get_token()}",
        "x-api-key":       API_KEY or CLIENT_ID,
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
    logger.info(f"GET {SANDBOX_URL} (listing all sandboxes)...")
    # Sandbox-management endpoint does NOT take x-sandbox-name itself.
    headers = auth_headers(sandbox="")
    headers.pop("x-sandbox-name", None)
    status, text = http_request("GET", SANDBOX_URL, headers)
    if status == 403:
        logger.warning("Sandbox-management API denied (403) -- token lacks the "
                       "management read scope. Falling back to sandbox_names "
                       "from the creds file. This is expected on minimally-scoped "
                       "Query Service tokens.")
        return []
    if status < 200 or status >= 300:
        logger.error(f"Sandbox list failed: HTTP {status} {text[:200]}")
        return []
    body = json.loads(text)
    names = [s.get("name") for s in body.get("sandboxes", []) if s.get("name")]
    logger.info(f"  -> sandboxes available: {names}")
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
        logger.error(f"Claude API HTTP {e.code}: {text}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.error(f"Claude API call failed: {e}")
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
            logger.error(f"Skipping malformed snapshot {snap_path}: {e}")

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
        logger.error(f"PUT {url} (sandbox={sandbox}) -> HTTP {status}: {text[:300]}")
        return False
    logger.info(f"OK - sandbox '{sandbox}' template {template_id} -> '{new_name}'.")
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
        logger.info("No userIds in the response; using no filter.")
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
    logger.info("Choose userId(s) to filter by (you can pick more than one):")
    logger.info("Adobe doesn't expose an API to resolve these IDs to emails "
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
            logger.error("stdin closed; cannot pick. Set my_user_ids in the creds file.")
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
            logger.info(f"Selected {len(picked_uids)} userId(s):")
            for uid in picked_uids:
                logger.info(f"  - {_display_owner(uid)}")
            logger.info("Tip: add these (with labels) to my_user_ids in "
                        "the creds file to skip this menu next time.")
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
    logger.info(f"Sandbox '{sandbox}': listing templates...")
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
            logger.error("401 Unauthorized - token expired/invalid.")
            sys.exit(1)
        if status == 403:
            logger.warning(f"  no Query Service access in sandbox '{sandbox}' "
                           f"(token lacks the right scope/role here).")
            _NO_ACCESS_SANDBOXES.append(sandbox)
            return []
        if status < 200 or status >= 300:
            logger.error(f"  page {page}: HTTP {status} {text[:200]}")
            break
        body = json.loads(text)
        batch = body.get("templates", [])
        for t in batch:
            t["_sandbox"] = sandbox
        out.extend(batch)
        logger.info(f"  page {page}: got {len(batch)} (sandbox total {len(out)}).")
        next_cursor = (body.get("_page") or {}).get("next")
        if not batch or len(batch) < PAGE_LIMIT or not next_cursor:
            break
        start = next_cursor
    logger.info(f"  sandbox '{sandbox}' done - {len(out)} templates.")
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
        logger.info(f"Using {len(sandboxes)} sandbox(es) from sandbox-management API.")
        return sandboxes
    if SANDBOX_NAMES:
        logger.info(f"Sandbox-management API returned nothing; using "
                    f"configured SANDBOX_NAMES fallback: {SANDBOX_NAMES}")
        return list(SANDBOX_NAMES)
    logger.error("Sandbox listing returned empty AND SANDBOX_NAMES is empty. "
                 "Either get a token with sandbox-management read scope, or "
                 "fill in sandbox_names in the creds file.")
    sys.exit(1)


def prepare_folder_structure(sandboxes: list[str]) -> None:
    """Create sql/<sandbox>/ for every sandbox upfront so the layout is visible
    before we fetch anything."""
    SQL_DIR.mkdir(parents=True, exist_ok=True)
    for sb in sandboxes:
        (SQL_DIR / sanitize_filename(sb)).mkdir(parents=True, exist_ok=True)
    logger.info(f"Folder structure ready under {SQL_DIR}: {sandboxes}")


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
    logger.info(summary + ".")
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
        logger.info("No userIds in the response; no filter applied.")
        return []

    matching_ids = [e["id"] for e in MY_USER_IDS if e["id"] in counts]

    if not matching_ids:
        logger.info("None of my_user_ids appear in this response (likely a "
                    "different tenant). Showing every userId found -- pick "
                    "yours, then add them to my_user_ids in the creds file.")
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
    logger.info(f"Found {len(matching_ids)} of your known userId(s) in this response:")
    for uid in matching_ids:
        logger.info(f"  - {_display_owner(uid)}  ({counts[uid]} templates)")

    sb_w = max((len(s) for s in per_sb), default=14)
    print(f"        {'SANDBOX':<{sb_w}}  COUNT")
    print(f"        {'-' * sb_w}  -----")
    for sb in sorted(per_sb):
        print(f"        {sb:<{sb_w}}  {per_sb[sb]:>5}")
    print(f"        {'TOTAL':<{sb_w}}  {total:>5}")

    try:
        raw = input("  Enter=use all of these, 'p'=pick from full list, 'a'=no filter: ")
    except EOFError:
        logger.error("stdin closed; cannot confirm. Set my_user_ids in the creds file.")
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
    logger.info("Which sandbox to rename in?")
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
    logger.info(f"Rename mode for {count} template(s):")
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
    logger.info(f"BATCH MODE will rename these {count} template(s):")
    for uid, n in owners.most_common():
        owner_disp = _display_owner(uid)
        flag = "   [!] FOREIGN -- not in your my_user_ids" if _is_foreign(uid) else ""
        print(f"    {n:>3} owned by  {owner_disp}{flag}")
    print()

    exclude_foreign = False
    if foreign_count > 0:
        logger.warning(f"WARNING: {foreign_count} of these are NOT owned by your "
                       "labelled user IDs.")
        logger.warning("Renaming them will change tables that may belong to system "
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
            logger.info(f"Excluding {foreign_count} foreign-owned templates "
                        f"from this batch run.")
        else:
            logger.info(f"Including {foreign_count} foreign-owned templates -- "
                        f"you accepted responsibility.")

    try:
        confirm = input("  Final confirm -- proceed with batch rename? (y/N): ").strip().lower()
    except EOFError:
        return False, exclude_foreign
    if confirm in ("y", "yes"):
        logger.info("Batch mode confirmed -- auto-accepting every suggestion.")
        return True, exclude_foreign
    logger.info("Cancelled batch mode; falling back to interactive.")
    return False, exclude_foreign


def run_for_cred(path: Path) -> None:
    """Run the full fetch + rename + export pipeline for one credential set."""
    try:
        conf = load_creds(path)
    except Exception as e:
        logger.error(f"Failed to load {path.name}: {e}")
        return
    apply_config(conf, path)

    print_banner()
    logger.info(f"{SCRIPT_NAME} starting for '{TENANT}'.")
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
        logger.info(f"User filter: {len(selected_set)} ID(s) selected -> "
                    f"{len(mine)} template(s) of {len(templates)} in this tenant.")
    else:
        mine = list(templates)
        logger.info(f"NO USER FILTER -- including every template "
                    f"({len(templates)} total, regardless of owner).")

    # 3b. Optionally narrow to a single sandbox (skipped when only one sandbox
    #     is in the picture anyway, or when stdin isn't a TTY).
    interactive = sys.stdin.isatty()
    if interactive and mine:
        chosen_sbs = ask_sandbox_filter(mine)
        if chosen_sbs is not None:
            before = len(mine)
            mine = [t for t in mine if t.get("_sandbox") in chosen_sbs]
            logger.info(f"Sandbox filter: {sorted(chosen_sbs)} -> "
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
        logger.info(f"Excluded {before - len(mine)} foreign-owned template(s); "
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
        logger.info(f"Batch mode: auto-accepting suggestions for {len(mine)} template(s).")
    elif interactive:
        logger.info("Interactive: Enter=accept suggestion, type a new name, "
                    "or 's'=skip rename (still saved).")
    else:
        logger.info("stdin is not a TTY; saving with current names without renaming.")

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
            logger.info(f"[batch] {sandbox} | owner {owner_line} | "
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
                logger.info("stdin closed; saving remainder with current names.")
                interactive = False
                raw = "s"
            answer = raw.replace(chr(0xfeff), "").strip()
            if answer.lower() == "s":
                logger.info("Skipped rename.")
            else:
                new_name = answer if answer else suggest

        if new_name is not None:
            new_norm = _norm(new_name)
            old_norm = _norm(old)
            existing = existing_by_sandbox.get(sandbox, set())
            if new_norm == old_norm:
                logger.info(f"Name unchanged ('{old}'); no PUT needed.")
            elif new_norm in existing:
                logger.info(f"Skip PUT - '{new_name}' already exists in "
                            f"sandbox '{sandbox}' on a different template.")
            else:
                logger.info(f"PUT '{old}' -> '{new_name}'")
                if rename_template(tid, new_name, sql, sandbox):
                    t["name"] = new_name  # save below uses the new name
                    existing.discard(old_norm)
                    existing.add(new_norm)
                    existing_by_sandbox[sandbox] = existing

        path = save_template_sql(t, SQL_DIR)
        logger.info(f"  -> {path.relative_to(SQL_DIR.parent)}")

    # 6. Snapshot this run's full template list to sql/<tenant>/_snapshot.json,
    #    then rebuild the cross-tenant mega file at sql/all_queries_mega_file.md
    #    by reading every tenant's snapshot. This way one file accumulates every
    #    org you've run against (one label per tenant) instead of a per-tenant
    #    file each time.
    snap_path = write_tenant_snapshot(templates, SQL_DIR)
    logger.info(f"Snapshot: {snap_path.relative_to(SQL_DIR.parent.parent)} "
                f"({len(templates)} templates from {len(sandboxes)} sandbox(es))")

    sql_root = SQL_DIR.parent
    # Clean up the previous-version per-tenant mega file if it's still there.
    old_per_tenant = SQL_DIR / "all_queries_mega_file.md"
    if old_per_tenant.exists():
        old_per_tenant.unlink()
    md_path = write_cross_tenant_mega_markdown(sql_root)
    logger.info(f"Mega file (cross-tenant): "
                f"{md_path.relative_to(SQL_DIR.parent.parent)}")


def _interactive() -> bool:
    """True when we can prompt the user (stdin is a real terminal)."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _match_cred(name: str, creds: list[Path]) -> Path | None:
    """Resolve a credential name from the CLI to a path. Matches on the file
    stem, tolerating hyphen/space/underscore differences and case (so
    'acme-alpha' finds 'acme alpha.json')."""
    def norm(s: str) -> str:
        return s.lower().replace(" ", "").replace("-", "").replace("_", "")
    target = norm(name)
    for p in creds:
        if norm(p.stem) == target:
            return p
    return None


def cred_menu(creds: list[Path]) -> Path | None:
    """Interactive single-pick picker for the credential bank. The renamer
    writes back to AEP, so a run targets exactly ONE credential set. Returns
    the chosen path, or None to quit."""
    bar = ANSI["cyan"] + "=" * 70 + ANSI["reset"]
    print()
    print(bar)
    print(f"  {ANSI['bold']}Credential bank{ANSI['reset']}  "
          f"{ANSI['dim']}({CREDS_DIR}){ANSI['reset']}")
    print(ANSI["cyan"] + "-" * 70 + ANSI["reset"])
    for i, p in enumerate(creds, 1):
        print(f"  {ANSI['bold']}{i:>2}{ANSI['reset']}  "
              f"{ANSI['yellow']}{p.stem:<24}{ANSI['reset']} "
              f"{ANSI['dim']}{p.name}{ANSI['reset']}")
    print(bar)
    raw = input("\nPick ONE set by number, blank to quit: ").strip()
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(creds):
        return creds[int(raw) - 1]
    logger.warning(f"Invalid choice: {raw}")
    return None


def main() -> None:
    # Positional arg (if any) is a credential-set name (filename stem). The
    # renamer writes to AEP, so it targets exactly one credential set per run.
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    creds = discover_creds()
    if not creds:
        logger.error(f"No credential JSONs found in {CREDS_DIR}. "
                     f"Drop your <tenant>.json files there.")
        sys.exit(1)

    if names:
        path = _match_cred(names[0], creds)
        if not path:
            logger.error(f"No credential set named {names[0]!r} in {CREDS_DIR}.")
            sys.exit(1)
    elif _interactive():
        path = cred_menu(creds)
    else:
        logger.error("No credential set specified and not running "
                     "interactively. Pass a credential-set name.")
        sys.exit(1)

    if not path:
        logger.info("Nothing chosen. Exiting.")
        return

    run_for_cred(path)


if __name__ == "__main__":
    main()
