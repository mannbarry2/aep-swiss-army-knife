#!/usr/bin/env python3
"""
check_my_setup.py  (AEP Swiss Army Knife)
=========================================
Is this machine ready to run the toolkit? Checks, in order:

  1. Python       3.10 or newer, and whether the repo's .venv is active.
  2. Packages     the ones in requirements.txt, and which tools need each.
  3. Credentials  Windows Credential Manager reachable, and which credential
                  sets are stored (names only -- no values are shown).
  4. Output       where workbooks will go (AEP_OUTPUT_DIR, else output/).
  5. Adobe        only when you name a credential: mint a token and list the
                  sandboxes it can see -- proof the credential works.

Read-only: it installs nothing, writes nothing, and changes no settings. Each
problem comes with the command that fixes it.

Usage:
    python check_my_setup.py              # checks 1-4
    python check_my_setup.py aep-prod     # also 5, for the credential named aep-prod

Exit code 0 when everything needed is in place, 1 when something is missing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

SCRIPT_NAME = "check_my_setup"
SCRIPT_VERSION = "1.0.1"
SCRIPT_DATE = "2026-10-29"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

# (import name, pip name, needed?, what uses it)
PACKAGES = [
    ("keyring", "keyring", True, "every tool: reads credentials from Windows Credential Manager"),
    ("requests", "requests", True, "the credential manager's token and probe calls"),
    ("openpyxl", "openpyxl", True, "every tool: writes the .xlsx workbooks"),
    ("pyarrow", "pyarrow", False, "sampled values: Data Dictionary --data-dict / --history, single-schema extractor"),
    ("tzdata", "tzdata", False, "needed alongside pyarrow on Windows"),
    ("pptx", "python-pptx", False, "the guardrail scoreboard slide"),
    ("docx", "python-docx", False, "the Word edition of the Data Dictionary overview"),
]
SANDBOXES_URL = "https://platform.adobe.io/data/foundation/sandbox-management/sandboxes"
DEFAULT_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"

problems = 0


def say(mark: str, text: str, fix: str = "") -> None:
    global problems
    if mark in ("MISSING", "FAIL"):
        problems += 1
    print(f"  [{mark:^7}] {text}")
    if fix:
        print(f"            fix: {fix}")


def check_python() -> None:
    print("\n1. Python")
    v = sys.version_info
    if v >= (3, 10):
        say("OK", f"Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})")
    else:
        say("FAIL", f"Python {v.major}.{v.minor} -- the toolkit needs 3.10 or newer",
            "install Python 3.11 from python.org")
    if sys.prefix != sys.base_prefix:
        say("OK", f"virtual environment active  ({sys.prefix})")
    elif (REPO / ".venv").exists():
        say("WARN", "the repo's .venv exists but is not active, so its packages are not in use",
            r".venv\Scripts\activate   then run this again")
    else:
        say("WARN", "no virtual environment -- packages go into the system Python",
            r"python -m venv .venv   then   .venv\Scripts\activate")


def check_packages() -> None:
    print("\n2. Packages")
    missing = []
    for mod, pip_name, needed, used_for in PACKAGES:
        if importlib.util.find_spec(mod):
            say("OK", f"{pip_name:<12} {used_for}")
        else:
            say("MISSING" if needed else "OPTION", f"{pip_name:<12} {used_for}")
            missing.append(pip_name)
    if missing:
        print("            fix: pip install -r requirements.txt")


def check_credentials() -> None:
    print("\n3. Credentials")
    try:
        from toolkit_internals import credential_loader
    except ImportError as e:
        say("FAIL", f"cannot load the credential loader ({e})", "pip install -r requirements.txt")
        return
    if credential_loader.keyring_available():
        say("OK", f"Windows Credential Manager reachable ({credential_loader.backend_name()})")
    else:
        say("WARN", "keyring unavailable -- credentials can only come from the creds/ folder "
                    "(plaintext; keep it on encrypted storage)")
    services = credential_loader.list_services()
    if services:
        say("OK", f"{len(services)} credential set(s) stored: {', '.join(services)}")
    else:
        say("MISSING", "no credential sets stored",
            "python platform_access_and_audit/credential_keyring_manager_and_access_probe.py "
            "store --service <name>")


def check_output() -> None:
    print("\n4. Output")
    out = os.environ.get("AEP_OUTPUT_DIR")
    if out:
        p = Path(out)
        if p.is_dir() and os.access(p, os.W_OK):
            say("OK", f"workbooks go to AEP_OUTPUT_DIR: {p}")
        elif p.is_dir():
            say("FAIL", f"AEP_OUTPUT_DIR is not writable: {p}")
        else:
            say("WARN", f"AEP_OUTPUT_DIR does not exist yet (tools create it): {p}")
    else:
        say("OK", f"workbooks go to the repo's output/ folder ({REPO / 'output'})")
        print("            to send them elsewhere (e.g. corporate storage): "
              "setx AEP_OUTPUT_DIR \"<folder>\"  and open a new terminal")
    print(f"            caches always stay in {REPO / 'output'}")


def check_adobe(service: str) -> None:
    print(f"\n5. Adobe, with credential '{service}'")
    from toolkit_internals import credential_loader
    try:
        conf = credential_loader.load_creds(service)
    except credential_loader.CredsError as e:
        say("FAIL", str(e))
        return
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": conf["client_id"],
        "client_secret": conf["client_secret"],
        "scope": conf.get("scopes") or credential_loader.DEFAULT_SCOPES}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                conf.get("oauth_url") or DEFAULT_TOKEN_URL, data=form, method="POST"), timeout=30) as r:
            token = json.loads(r.read())["access_token"]
        say("OK", f"token minted for org {conf['org_id']}")
    except urllib.error.HTTPError as e:
        say("FAIL", f"Adobe refused the token: HTTP {e.code} {e.read().decode(errors='replace')[:200]}",
            "check the client id and secret in Developer Console, then store them again")
        return
    except OSError as e:
        say("FAIL", f"could not reach Adobe IMS: {e}", "check the network / proxy")
        return
    headers = {"Authorization": f"Bearer {token}", "x-api-key": conf.get("api_key") or conf["client_id"],
               "x-gw-ims-org-id": conf["org_id"]}
    try:
        with urllib.request.urlopen(urllib.request.Request(SANDBOXES_URL, headers=headers), timeout=30) as r:
            names = sorted(s.get("name", "?") for s in json.loads(r.read()).get("sandboxes", []))
        say("OK", f"{len(names)} sandbox(es) visible: {', '.join(names)}")
    except urllib.error.HTTPError as e:
        say("FAIL", f"token works but Experience Platform refused it: HTTP {e.code}",
            "add the credential's technical account to an AEP product profile / role")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        return
    print(f"{SCRIPT_NAME} v{SCRIPT_VERSION} ({SCRIPT_DATE})  by {SCRIPT_AUTHOR}")
    print("Is this machine ready to run the AEP Swiss Army Knife?  (read-only)")
    check_python()
    check_packages()
    check_credentials()
    check_output()
    if args:
        check_adobe(args[0])
    else:
        print("\n5. Adobe: skipped. Name a credential to test it, e.g.  python check_my_setup.py aep-prod")
    print()
    if problems:
        print(f"{problems} thing(s) to fix -- see 'fix:' above.")
        sys.exit(1)
    print("Ready. Pick a tool from the README's 'Which tool do I want?' table.")


if __name__ == "__main__":
    main()
