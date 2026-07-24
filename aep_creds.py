"""Shared AEP credential layer -- keyring first, encrypted-folder fallback.

Every script in this repo used to read plaintext ``creds/*.json``. Secrets now
prefer the OS credential store (Windows Credential Manager) via ``keyring``.
Because some locked-down corporate laptops ship without a usable keyring
backend, this module falls back to a local ``creds/`` folder when -- and only
when -- keyring can't serve the credential. Import it instead of hand-rolling a
loader:

    import aep_creds
    service = aep_creds.resolve_service(args.service)   # arg, or interactive pick
    conf    = aep_creds.load_creds(service)             # dict of fields

`conf` has the same shape the old JSON files produced (client_id, client_secret,
org_id, plus optional tech_account_id / scopes / api_key / oauth_url / sandbox),
so a consuming script's `authenticate()` / `aep_headers()` need not change.

Resolution order for a service, most secure first:
    1. keyring vault (nothing on disk)                      -- preferred
    2. creds/<service>.json  (plaintext on disk)            -- fallback only

Every load prints one line to stderr saying which store it used (silence with
AEP_QUIET_CREDS=1), and `source_banner()` gives a startup summary of what's in
play. Control the source with AEP_CREDS_SOURCE (or per call, `load_creds(...,
prefer=...)`):
    auto     (default) keychain-first, silently fall back to the folder
    keyring  keychain only -- error rather than read disk
    folder   local folder only -- ignore keychain
    ask      when a service exists in BOTH and you're at a TTY, prompt which

===========================  SECURITY NOTICE  ==============================
The folder fallback keeps secrets in PLAINTEXT on disk. It exists for machines
where keyring is unavailable -- it is not the happy path. Whenever a credential
is read from the folder this module prints a one-time warning to stderr. To
keep that exposure contained:
  * Prefer keyring wherever it works (`credential_validator_v2.py migrate`).
  * Put the folder on encrypted and/or removable storage, NOT your own machine's
    plain disk. Override the location with the AEP_CREDS_DIR env var.
  * `creds/*.json` is gitignored (only creds/example.json is tracked) -- never
    commit a real set. This repo is secret-scanned; a leak has forced a history
    rewrite before.
  * Delete the folder (and any backups) once you are back on a keyring machine.
Set AEP_SUPPRESS_CREDS_WARNING=1 to silence the runtime warning if you have
accepted this risk.
============================================================================

Dependencies: ``keyring`` (optional at runtime -- if it's missing or has no
working backend we go straight to the folder). ``win32ctypes`` is used lazily
for vault enumeration. This module never writes a secret value to disk.

The authoritative store/validate/migrate/sweep CLI lives in
credential_validator_v2.py; this module is the read/pick layer scripts share.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import keyring
    import keyring.errors
    _HAVE_KEYRING = True
except ImportError:
    keyring = None  # type: ignore[assignment]
    _HAVE_KEYRING = False


# ----------------------------------------------------------------------------
# Field / service model  (kept in lockstep with credential_validator_v2.py)
# ----------------------------------------------------------------------------
# Required fields mint a token; the rest are optional (api_key defaults to
# client_id, scopes has a default, etc.).
REQUIRED_KEYS = ["client_id", "client_secret", "org_id"]
OPTIONAL_KEYS = ["tech_account_id", "scopes", "api_key", "oauth_url", "sandbox"]
ALL_KEYS      = REQUIRED_KEYS + OPTIONAL_KEYS

# keyring can't enumerate its own services, so we also keep a registry entry
# listing the known service names for discovery / interactive pick.
REGISTRY_SERVICE = "aep-cred-index"
REGISTRY_KEY     = "services"

DEFAULT_SERVICE = "aep-prod"

DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

# Plaintext fallback folder. Default = ./creds next to this module; override with
# AEP_CREDS_DIR to point at encrypted/removable storage.
CREDS_DIR = Path(os.environ.get("AEP_CREDS_DIR")
                 or (Path(__file__).resolve().parent / "creds"))

# Source preference, via the AEP_CREDS_SOURCE env var:
#   "auto"    (default) keychain-first, silently fall back to the folder
#   "keyring" keychain only -- error rather than touch disk
#   "folder"  local folder only -- ignore keychain
#   "ask"     when BOTH sources have the service and it's interactive, prompt
# The env var is the machine-wide default; a script may still pass prefer=... .
SOURCE_PREF = os.environ.get("AEP_CREDS_SOURCE", "auto").strip().lower()
_VALID_PREFS = {"auto", "keyring", "folder", "ask"}


class CredsError(Exception):
    """A credential set is missing, incomplete, or unreadable from every source.

    Carries a human-readable message; consuming scripts typically log it and
    exit rather than trace it."""


# ----------------------------------------------------------------------------
# keyring availability + thin wrappers
# ----------------------------------------------------------------------------
_keyring_ok: bool | None = None  # cached functional-backend probe


def keyring_available() -> bool:
    """True only if keyring is importable AND has a working backend.

    Locked-down machines often resolve to keyring's ``fail``/``null`` backend,
    which raises on every call -- we treat that as unavailable so callers drop
    to the folder instead of crashing. Result is cached for the process."""
    global _keyring_ok
    if _keyring_ok is not None:
        return _keyring_ok
    if not _HAVE_KEYRING:
        _keyring_ok = False
        return False
    try:
        backend_mod = keyring.get_keyring().__class__.__module__.lower()
        if "fail" in backend_mod or "null" in backend_mod:
            _keyring_ok = False
            return False
        # Functional probe: a real backend returns None for a missing key; a
        # non-functional one raises.
        keyring.get_password(REGISTRY_SERVICE, "__probe__")
        _keyring_ok = True
    except Exception:
        _keyring_ok = False
    return _keyring_ok


def backend_name() -> str:
    """Human name of the active keyring backend (or why there isn't one)."""
    if not _HAVE_KEYRING:
        return "keyring not installed"
    try:
        return keyring.get_keyring().__class__.__name__
    except Exception:
        return "unknown backend"


def source_banner() -> str:
    """One-line summary of where credentials will come from, for scripts to
    print at startup so it's always clear which store is in play."""
    if keyring_available():
        return (f"Credentials: keyring vault ({backend_name()}) "
                f"first, local folder fallback at {CREDS_DIR}")
    return (f"Credentials: keyring UNAVAILABLE ({backend_name()}) -- "
            f"using PLAINTEXT local folder {CREDS_DIR}")


def kr_set(service: str, key: str, value: str) -> None:
    keyring.set_password(service, key, value)


def kr_get(service: str, key: str) -> str | None:
    if not keyring_available():
        return None
    try:
        return keyring.get_password(service, key)
    except Exception:
        return None


def kr_del(service: str, key: str) -> bool:
    """Delete one entry. Returns True if it existed and was removed."""
    if not keyring_available():
        return False
    try:
        keyring.delete_password(service, key)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


# ----------------------------------------------------------------------------
# Loading -- keyring source
# ----------------------------------------------------------------------------
def _load_from_keyring(service: str) -> dict[str, str]:
    """All present fields for `service` from the vault (empty dict if none)."""
    if not keyring_available():
        return {}
    conf: dict[str, str] = {}
    for key in ALL_KEYS:
        val = kr_get(service, key)
        if val:
            conf[key] = val.strip()
    return conf


# ----------------------------------------------------------------------------
# Loading -- plaintext folder fallback
# ----------------------------------------------------------------------------
def _is_placeholder(value: str) -> bool:
    """True for template values like '<adobe-ims-client-id>' in example.json."""
    v = str(value).strip()
    return v.startswith("<") and v.endswith(">")


def _folder_path(service: str) -> Path:
    return CREDS_DIR / f"{service}.json"


def _load_from_folder(service: str) -> dict[str, str]:
    """Read creds/<service>.json into a config dict (empty dict if the file is
    absent). Underscore-prefixed keys (e.g. `_comment`) and placeholder values
    are skipped. Raises CredsError only if the file exists but is malformed."""
    path = _folder_path(service)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as ex:
        raise CredsError(f"Could not read {path.name}: {type(ex).__name__}: {ex}")
    if not isinstance(raw, dict):
        raise CredsError(f"{path.name} is not a JSON object.")
    conf: dict[str, str] = {}
    for key, val in raw.items():
        if key.startswith("_"):
            continue
        if isinstance(val, str):
            val = val.strip()
        if not val or (isinstance(val, str) and _is_placeholder(val)):
            continue
        conf[key] = val
    return conf


# ----------------------------------------------------------------------------
# Security warning (one-time per service per process)
# ----------------------------------------------------------------------------
_warned_plaintext: set[str] = set()


def _warn_plaintext(service: str) -> None:
    if service in _warned_plaintext:
        return
    _warned_plaintext.add(service)
    if os.environ.get("AEP_SUPPRESS_CREDS_WARNING"):
        return
    path = _folder_path(service)
    why = ("keyring has no working backend on this machine"
           if not keyring_available()
           else f"{service!r} is not in the keyring vault")
    # ANSI yellow if the terminal can take it; plain otherwise.
    hot = "\033[33m" if sys.stderr.isatty() else ""
    off = "\033[0m" if sys.stderr.isatty() else ""
    print(
        f"{hot}[SECURITY] Loaded credentials for {service!r} from PLAINTEXT "
        f"{path}\n"
        f"           ({why}). Secrets are unencrypted on disk. Prefer keyring "
        f"(credential_validator_v2.py migrate), keep this folder on\n"
        f"           encrypted/removable storage, and delete it when done.{off}",
        file=sys.stderr,
    )


# ----------------------------------------------------------------------------
# Source selection + announce
# ----------------------------------------------------------------------------
_announced: set[str] = set()


def _announce(service: str, source: str) -> None:
    """One-time, per-service note of which store a credential came from, so it's
    always visible where the tool looked. Silence with AEP_QUIET_CREDS=1."""
    if service in _announced or os.environ.get("AEP_QUIET_CREDS"):
        return
    _announced.add(service)
    dim = "\033[2m" if sys.stderr.isatty() else ""
    off = "\033[0m" if sys.stderr.isatty() else ""
    where = (f"keyring vault ({backend_name()})" if source == "keyring"
             else f"local folder {_folder_path(service)}")
    print(f"{dim}[creds] {service!r} loaded from {where}.{off}", file=sys.stderr)


def _prompt_source(service: str) -> str:
    """Ask which store to use when the service exists in BOTH and pref='ask'."""
    hot = "\033[36m" if sys.stderr.isatty() else ""
    off = "\033[0m" if sys.stderr.isatty() else ""
    print(f"\n  {hot}{service!r} exists in both the keyring vault and "
          f"{_folder_path(service).name}.{off}", file=sys.stderr)
    while True:
        try:
            raw = input("  Use [k]eychain (secure) or [l]ocal file? "
                        "(Enter = keychain): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return "keyring"
        if raw in ("", "k", "keychain", "keyring"):
            return "keyring"
        if raw in ("l", "local", "file", "folder"):
            return "folder"
        print("  Enter k or l.", file=sys.stderr)


# ----------------------------------------------------------------------------
# Public loader -- keyring first, folder fallback
# ----------------------------------------------------------------------------
def load_creds(service: str, *, require: list[str] | None = None,
               prefer: str | None = None) -> dict[str, str]:
    """Resolve `service`'s credentials. Keyring-first by default; falls back to
    the plaintext folder when keyring lacks the service or has no backend.

    `prefer` ('keyring' | 'folder' | 'ask') overrides the AEP_CREDS_SOURCE env
    default for this call. Values are stripped. A folder read triggers a
    one-time security warning; every load notes its source on stderr. Raises
    CredsError if the chosen source can't supply the service, or if a required
    field (default: REQUIRED_KEYS) is missing -- same fail-fast the old JSON
    loaders gave, so callers behave identically."""
    require = REQUIRED_KEYS if require is None else require
    pref = (prefer or SOURCE_PREF or "auto").lower()
    if pref not in _VALID_PREFS:
        pref = "auto"

    kr = _load_from_keyring(service)          # {} if absent / keyring down
    folder_has = _folder_path(service).exists()

    # Decide the source.
    if pref == "keyring":
        source = "keyring"
    elif pref == "folder":
        source = "folder"
    elif pref == "ask" and kr and folder_has and sys.stdin.isatty():
        source = _prompt_source(service)
    else:  # auto (or 'ask' with only one source): keychain-first
        source = "keyring" if kr else "folder"

    if source == "keyring":
        conf = kr
        if not conf and pref == "keyring":
            raise CredsError(
                f"Service {service!r} is not in the keyring vault "
                f"(AEP_CREDS_SOURCE=keyring forbids the folder fallback).")
    else:
        conf = _load_from_folder(service)
        if conf:
            _warn_plaintext(service)

    if not conf:
        known = list_services()
        if known:
            hint = f" Known services: {', '.join(known)}."
        elif not keyring_available():
            hint = (f" keyring is unavailable and {CREDS_DIR} has no usable "
                    f"*.json. Drop a <service>.json there or set AEP_CREDS_DIR.")
        else:
            hint = (" No services stored yet -- run "
                    "credential_validator_v2.py store (or migrate).")
        raise CredsError(f"No credentials found for service {service!r}.{hint}")

    missing = [k for k in require if not conf.get(k)]
    if missing:
        where = ("the keyring vault" if source == "keyring"
                 else _folder_path(service).name)
        raise CredsError(
            f"Service {service!r} (from {where}) is missing required "
            f"field(s): {', '.join(missing)}.")

    _announce(service, source)
    return conf


def peek_creds(service: str) -> dict[str, str]:
    """Best-effort read of a service's fields from whichever store has it,
    keyring first -- WITHOUT announcing, warning, or validating required keys.

    For scans that inspect many services at once (e.g. matching an x-api-key
    across the whole bank), where the per-load announce/security lines would be
    noise. Returns {} if the service is unreadable from every source."""
    try:
        conf = _load_from_keyring(service)
        if conf:
            return conf
        return _load_from_folder(service)
    except CredsError:
        return {}


# ----------------------------------------------------------------------------
# Service discovery
# ----------------------------------------------------------------------------
def registry_list() -> list[str]:
    raw = kr_get(REGISTRY_SERVICE, REGISTRY_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return sorted(data) if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def registry_add(service: str) -> None:
    services = set(registry_list())
    if service not in services:
        services.add(service)
        kr_set(REGISTRY_SERVICE, REGISTRY_KEY, json.dumps(sorted(services)))


def registry_remove(service: str) -> None:
    services = set(registry_list())
    if service in services:
        services.discard(service)
        kr_set(REGISTRY_SERVICE, REGISTRY_KEY, json.dumps(sorted(services)))


def vault_services() -> tuple[set[str], bool]:
    """Scan Windows Credential Manager for keyring's `key@service` entries.

    keyring's Windows backend stores each field as a Generic Credential named
    ``f"{key}@{service}"``. We enumerate them via the same win32ctypes shim
    keyring already depends on, so this needs no extra dependency. Returns
    (services, scanned_ok); scanned_ok is False when the vault can't be
    enumerated (keyring down, non-Windows, or the shim is missing) and the
    caller should fall back to the cred-index registry alone. Read-only."""
    if not keyring_available():
        return set(), False
    try:
        from win32ctypes.pywin32 import win32cred
    except Exception:
        return set(), False
    try:
        creds = win32cred.CredEnumerate(None, 0)
    except Exception:
        # CredEnumerate raises 'element not found' (1168) on an empty vault.
        return set(), True
    known = set(ALL_KEYS)
    found: set[str] = set()
    for cred in creds:
        target = cred.get("TargetName") or ""
        key, sep, service = target.partition("@")
        # Only our own field names count, so we don't sweep up unrelated apps'
        # credentials that happen to contain an '@'. Exclude the index itself.
        if sep and key in known and service and service != REGISTRY_SERVICE:
            found.add(service)
    return found, True


def folder_services() -> list[str]:
    """Service names available as plaintext creds/<name>.json (minus example)."""
    if not CREDS_DIR.exists():
        return []
    return sorted(p.stem for p in CREDS_DIR.glob("*.json") if p.stem != "example")


def discover_services() -> tuple[list[str], set[str], bool]:
    """KEYRING-ONLY discovery: union the cred-index registry with a live vault
    scan (NO folder). Returns (all_services_sorted, orphans, scanned_ok), where
    orphans are in the vault but missing from the registry (index drifted). This
    is what the keyring admin tool's sweep/purge operate on -- kept separate
    from list_services() so folder creds never leak into a vault sweep."""
    registered = set(registry_list())
    scanned, scanned_ok = vault_services()
    orphans = scanned - registered
    return sorted(registered | scanned), orphans, scanned_ok


def list_services() -> list[str]:
    """Sorted union of every credential source: keyring registry, a live vault
    scan, AND the plaintext folder. This is the canonical 'what credentials do
    I have' list for consumer-script pickers, regardless of where each lives.
    (Contrast discover_services(), which is keyring-only for vault admin.)"""
    registered = set(registry_list())
    scanned, _ = vault_services()
    return sorted(registered | scanned | set(folder_services()))


# ----------------------------------------------------------------------------
# Interactive selection  (replaces the old creds/*.json folder picker)
# ----------------------------------------------------------------------------
def pick_service(preselect: str | None = None, *,
                 prompt: str = "Select a credential set") -> str | None:
    """Resolve which service to use.

    - `preselect` given and resolvable -> return it (no prompt).
    - `preselect` given but unknown     -> raise CredsError (fail fast on a typo).
    - exactly one service available     -> auto-select it.
    - several, and a TTY               -> numbered menu on stderr, read a choice.
    - several, no TTY                  -> raise CredsError (ambiguous, pass --service).
    - none available anywhere          -> raise CredsError.
    """
    services = list_services()

    if preselect:
        if preselect in services:
            return preselect
        # Allow a bare service that exists in a source but isn't indexed yet.
        if _load_from_keyring(preselect) or _folder_path(preselect).exists():
            return preselect
        raise CredsError(
            f"Unknown credential service {preselect!r}. "
            f"Known: {', '.join(services) or '(none available)'}")

    if not services:
        raise CredsError(
            "No credentials available from keyring or the creds folder. Add one "
            "with credential_validator_v2.py store, or drop a <service>.json in "
            f"{CREDS_DIR}.")

    if len(services) == 1:
        return services[0]

    if not sys.stdin.isatty():
        raise CredsError(
            f"Multiple credential services and no TTY to choose. "
            f"Pass --service <name>. Options: {', '.join(services)}")

    print(f"\n  {prompt}:", file=sys.stderr)
    for i, name in enumerate(services, 1):
        default = "  (default)" if name == DEFAULT_SERVICE else ""
        print(f"    {i}. {name}{default}", file=sys.stderr)
    while True:
        try:
            raw = input("  Number (or Enter for 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return None
        if not raw:
            return services[0]
        if raw.isdigit() and 1 <= int(raw) <= len(services):
            return services[int(raw) - 1]
        print(f"  Enter 1-{len(services)}.", file=sys.stderr)


def resolve_service(arg: str | None = None, *,
                    prompt: str = "Select a credential set") -> str:
    """Convenience for CLI scripts: turn an optional --service value into a
    concrete service name, prompting if needed. Raises CredsError if none can
    be resolved (never returns None -- callers can assume a usable service)."""
    service = pick_service(arg, prompt=prompt)
    if not service:
        raise CredsError("No credential service selected.")
    return service


def client_label(conf: dict[str, str], service: str) -> str:
    """Best human label for output filenames/headers: a stored friendly name if
    present, else the service name. Replaces the old `path.stem` usage."""
    return conf.get("client") or conf.get("client_name") or service
