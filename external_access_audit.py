#!/usr/bin/env python3
"""
external_access_audit.py  (AEP Swiss Army Knife)
================================================
Who can reach Experience Platform who should not, and are they using it?

Two different questions, and the distinction is the whole point of this tool:

    ENTITLEMENT  what the Admin Console says a person CAN reach, read from
                 the User Management API (every user in the org, their product
                 profiles, their identity type).
    USE          what they have ACTUALLY done, read from the AEP audit log
                 per user, across every sandbox the credential can see.

An account with entitlement and no use is a standing risk nobody is currently
exploiting. An account with both is an incident. An account with use and no
entitlement should be impossible. Reporting only one of the two gets the
severity wrong in both directions, so this reports both, side by side, and
says plainly which it could not establish.

SCOPE -- the AEP umbrella only
------------------------------
Experience Platform / RTCDP, Customer Journey Analytics, Journey Optimizer,
Data Collection and Privacy Service. Workfront, Analytics, Creative Cloud and
Acrobat are deliberately OUT of scope: they are somebody else's estate. They
are still counted on the Summary tab, so you can see they were considered and
set aside rather than missed.

HONEST LIMITS, stated on the Summary tab of every run
-----------------------------------------------------
  * The audit log retains 90 DAYS. Adobe refuses any wider window. "No
    activity" therefore means "none in the last 90 days", never "never".
  * A sandbox the credential cannot read is listed as UNREADABLE, not as
    clean. Silence from a door you could not open is not an all-clear.
  * CJA keeps its OWN audit trail, which this does not read. A CJA-only user
    can show entitlement here with no activity and still be active in CJA.
  * Product profiles are read as a user's DIRECT memberships. Access inherited
    by nesting a user group inside a profile is not resolved, so the entitled
    count is a floor.

Output: ONE workbook, output/aep-user-logs/external_access_audit_<YYYYMMDD>.xlsx
  Summary          the numbers, the scope and the limits above
  External users   every account on a non-corporate domain, entitlement + use
  AEP access       every account entitled to anything under the AEP umbrella
  Profiles         the AEP product profiles and how many hold each
  Tech accounts    @techacct.adobe.com service accounts by audit volume

READ-ONLY, AND ENFORCED
    This tool decides whether people keep their access, so it must never be
    able to change anything. That is not left to the code's good intentions:
    install_readonly_guard() wraps urllib for the whole process, so ANY
    request that is not a GET raises before it leaves the machine -- including
    requests made by imported modules. The single exception is the IMS token
    POST, matched on an exact URL, which mints a credential and alters nothing
    in Adobe. There is no flag to disable this.

Usage:
    python external_access_audit.py                      # default credential
    python external_access_audit.py kuba
    python external_access_audit.py kuba --days 30
    python external_access_audit.py kuba --no-activity   # entitlement only, fast
    python external_access_audit.py kuba --home-domain example.com
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import logging
import re
import sys
import urllib.error
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aep_creds
import aep_usage_log as ul

SCRIPT_NAME = "external_access_audit"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-10-19"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

SCRIPT_DIR = Path(__file__).resolve().parent
# Where workbooks are written. Configurable, and deliberately NOT hard-coded
# to any particular corporate folder: this repo is public-ish and a client's
# OneDrive path is a client identifier. Set AEP_AUDIT_OUT (or pass --out-dir)
# to send output somewhere governed -- a corporate OneDrive or SharePoint
# library rather than personal cloud storage, which is the right home for
# anything carrying names and access rights.
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output" / "aep-user-logs"
OUTPUT_DIR = Path(os.environ.get("AEP_AUDIT_OUT") or DEFAULT_OUTPUT_DIR)
USER_MGMT = "https://usermanagement.adobe.io/v2/usermanagement"
DEFAULT_SERVICE = "aep-prod"
# The corporate domain is DETECTED, not hard-coded: it is whichever domain
# most of the roster sits on. That keeps a client's name out of this repo and
# makes the tool work for any tenant. Override with --home-domain.
DEFAULT_HOME = None
TECHACCT = "@techacct.adobe.com"
AUDIT_RETENTION_DAYS = 90      # Adobe's hard ceiling; a wider window is a 422
# The audit API caps page.totalElements at 1,000 for a single query. A count
# of exactly this therefore means "at least this many", NOT a measurement.
# Zero is still a true zero, so the purge list is unaffected -- but any usage
# figure at the cap must be shown as a floor, never as a number.
AUDIT_PAGE_CAP = 1000
# Requests per second against the audit API. The first run of the per-email
# check fired ~2,300 requests in 150 seconds; Adobe rate-limited most of them,
# every retry was exhausted, and the failures were read as "no activity". That
# turned a throttling problem into 222 accounts wrongly marked for deletion.
# Pace the calls instead.
MAX_RPS = 8.0
# Keep the start date this far inside the retention boundary. Adobe measures
# "90 days ago" from the moment each request lands, so a window computed once
# at the start of a long run slides out of range part-way through.
WINDOW_MARGIN = timedelta(hours=2)
CONFIDENTIAL = "STRICTLY CONFIDENTIAL"
_HEADER_BG = "1F4E78"

# What counts as the AEP umbrella. Matched against the product a profile
# belongs to AND the profile's own name, because a few profiles (the GG-* AD
# groups) carry the product in their name and nothing in productName.
AEP_RE = re.compile(
    r"experience platform|journey optimi|real.?time customer|rtcdp|"
    r"ajo|aep", re.I)
# Customer Journey Analytics is OUT OF SCOPE, and is tested BEFORE the
# include above because "Customer Journey Analytics" trips the "journey"
# match. CJA is a separate responsibility; it also keeps its own audit
# trail this tool cannot read, so reporting it here would be guesswork.
CJA_RE = re.compile(r"cja|customer journey analytics", re.I)
# NOTE on the bare "aep": most people here do NOT get Platform access through
# a product profile. They get it through an AD group nested into one --
# names like "GG-UK-<org>-PROD-AdobeAEP-Analyst". Those groups carry no
# productName, so matching only on product names missed 120 distinct groups
# and produced 67 accounts that were visibly using Platform every day while
# the report claimed they had no entitlement at all. "aep" is matched as a
# substring rather than a whole word precisely so "AdobeAEP" is caught.
# Everything deliberately OUT of scope -- somebody else's estate. Checked
# FIRST so a loose match elsewhere can never drag one of these back in, and
# counted on the Summary tab so you can see they were considered, not missed.
OUT_RE = re.compile(
    r"workfront|analytics|acrobat|creative cloud|photoshop|illustrator|"
    r"indesign|premiere|after effects|dreamweaver|audition|animate|"
    r"lightroom|fresco|dimension|captivate|xd|express|firefly|"
    r"aem|assets essentials|genstudio|target|campaign|"
    r"data collection|privacy service|dynamic tag", re.I)

# ----------------------------------------------------------------------------
# Read-only guard
# ----------------------------------------------------------------------------
# This tool decides whether people keep their access. It must never be able to
# CHANGE anything, and "we only wrote GETs" is a promise about the code as it
# stands, not a property of it -- one careless edit, or one imported helper
# that posts, and the promise is silently void.
#
# So the guarantee is enforced at the socket instead of asserted in a comment.
# Every HTTP call in this process, including ones made by imported modules we
# do not own, goes through urllib; wrapping urlopen means ANY request that is
# not a bare GET raises before it leaves the machine. There is no flag to turn
# this off: to make this tool write something, you would have to delete this
# block, which is exactly the amount of deliberation that change deserves.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The single exception, and it is not a data write: OAuth requires the token
# request itself to be a POST. It mints a credential; it changes nothing in
# Adobe. Matched on exact scheme+host+path so that "contains adobelogin" can
# never be stretched into something broader.
AUTH_ENDPOINTS = frozenset({
    "https://ims-na1.adobelogin.com/ims/token",
    "https://ims-na1.adobelogin.com/ims/token/v3",
})


class ReadOnlyViolation(RuntimeError):
    """Raised when anything in this process tries a non-GET request."""


def _is_auth_endpoint(url: str) -> bool:
    """True only for an exact IMS token URL -- no substring matching, no
    wildcards, query string ignored."""
    try:
        u = urllib.parse.urlsplit(str(url))
    except Exception:
        return False
    base = f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")
    return base in AUTH_ENDPOINTS


def install_readonly_guard() -> None:
    """Make every outbound HTTP request in this process provably read-only."""
    import urllib.request as _ur

    if getattr(_ur.urlopen, "_readonly_guarded", False):
        return
    _real_urlopen = _ur.urlopen

    def guarded(req, *args, **kwargs):
        method = "GET"
        url = req if isinstance(req, str) else getattr(req, "full_url", "?")
        body = None
        if not isinstance(req, str):
            try:
                method = (req.get_method() or "GET").upper()
            except Exception:
                method = "GET"
            body = getattr(req, "data", None)
        if method == "POST" and _is_auth_endpoint(url):
            return _real_urlopen(req, *args, **kwargs)   # token mint only
        if method not in READ_ONLY_METHODS:
            raise ReadOnlyViolation(
                f"BLOCKED: {method} {url}  --  "
                f"  {SCRIPT_NAME} is read-only. It may only ever GET. "
                f"If a write is genuinely wanted, remove "
                f"install_readonly_guard() deliberately -- do not widen it.")
        if body:
            raise ReadOnlyViolation(
                f"BLOCKED: request to {url} carries a body ({len(body)} bytes). "
                f"{SCRIPT_NAME} is read-only.")
        return _real_urlopen(req, *args, **kwargs)

    guarded._readonly_guarded = True
    _ur.urlopen = guarded


ANSI = {"reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "cyan": "\033[36m", "magenta": "\033[35m"}
logger = logging.getLogger(SCRIPT_NAME)

_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle() -> None:
    """Space requests out to MAX_RPS. Cheap insurance: being rate-limited is
    indistinguishable from an empty result unless every failure is tracked,
    and the cost of confusing the two here is someone's access."""
    with _rate_lock:
        gap = 1.0 / MAX_RPS
        wait = gap - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _log_setup() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-7s] %(message)s",
                                     "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    # aep_usage_log configures the root logger too; without this every
    # line would be printed twice.
    logger.propagate = False


# ----------------------------------------------------------------------------
# Admin Console: the roster and the profile catalogue
# ----------------------------------------------------------------------------
def _um_headers(token: str, conf: dict) -> dict:
    """User Management is an ORG-level API: no sandbox header."""
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "Accept": "application/json",
    }


def _um_paged(token: str, conf: dict, kind: str, key: str) -> list:
    """Page a User Management collection ('users' or 'groups')."""
    out, page = [], 0
    while page < 100:
        url = f"{USER_MGMT}/{kind}/{conf['org_id']}/{page}"
        try:
            body = ul.http(url, headers=_um_headers(token, conf), timeout=90)
            r = json.loads(body) or {}
        except urllib.error.HTTPError as e:
            detail = ul.flatten_err(e.read().decode(errors="replace"))
            raise RuntimeError(
                f"User Management /{kind} page {page} failed (HTTP {e.code}): "
                f"{detail[:160]}"
                + ("  -- this credential's API key is probably not entitled to "
                   "the User Management API." if e.code in (401, 403) else "")
            ) from None
        out += r.get(key) or []
        if r.get("lastPage"):
            break
        page += 1
    return out


def fetch_roster(token: str, conf: dict) -> tuple[list, list]:
    logger.info("Admin Console: reading the user roster ...")
    users = _um_paged(token, conf, "users", "users")
    logger.info(f"  {len(users)} user(s).")
    logger.info("Admin Console: reading the product profile catalogue ...")
    groups = _um_paged(token, conf, "groups", "groups")
    logger.info(f"  {len(groups)} group(s) / profile(s).")
    return users, groups


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------
def build_profile_index(groups: list) -> dict:
    """{group_name: (product_name, in_aep_scope)}."""
    idx = {}
    for g in groups:
        name = g.get("groupName") or ""
        product = g.get("productName") or ""
        both = f"{product} {name}"
        # AEP wins over the exclusions, and must: "Customer Journey
        # Analytics" contains the word Analytics, so testing the exclusion
        # first would throw CJA out of its own report.
        idx[name] = (product, bool(AEP_RE.search(both))
                     and not CJA_RE.search(both))
    return idx


def split_groups(user: dict, idx: dict) -> tuple[list, list, list]:
    """(eep_profiles, out_of_scope_profiles, other_groups) for one user."""
    aep, out, other = [], [], []
    for g in user.get("groups") or []:
        product, is_eep = idx.get(g, ("", bool(AEP_RE.search(g))
                                      and not OUT_RE.search(g)))
        if is_eep:
            aep.append(g)
        elif OUT_RE.search(f"{product} {g}"):
            out.append(g)
        else:
            other.append(g)
    return aep, out, other


def domain_of(user: dict) -> str:
    return (user.get("email") or "@?").rsplit("@", 1)[-1].lower()


# ----------------------------------------------------------------------------
# Audit log: did they actually DO anything
# ----------------------------------------------------------------------------
def readable_sandboxes(token: str, conf: dict) -> tuple[list, list]:
    """(readable, unreadable) sandbox names. A sandbox we cannot read is
    reported as unreadable rather than quietly treated as having no events --
    silence from a door you could not open is not an all-clear."""
    import data_dictionary_v3 as dd
    ok, sbs = dd.list_sandboxes(token, conf)
    if not ok:
        return ["prod"], []
    names = [s.get("name") for s in sbs if s.get("name")]
    good, bad = [], []
    b = datetime.now(timezone.utc)
    a = b - timedelta(minutes=5)
    for n in names:
        try:
            ul.AuditClient(token, conf, n).get(ul.window_params(a, b, [], 3))
            good.append(n)
        except Exception:
            bad.append(n)
    return good, bad


class _SweepOpts:
    """The subset of aep_usage_log's CLI options that collect() reads."""
    def __init__(self, sandbox, days):
        self.sandbox = sandbox
        self.days = days
        self.noise_user = None          # auto-detect the bulk tech account
        self.noise_asset_type = ""
        self.no_noise_count = False
        self.exclude_techacct = False   # keep tech events in 'kept' so they tally


def sweep_sandbox(token: str, conf: dict, sandbox: str, days: int) -> dict:
    """Every user's activity in one sandbox, as {email: {...}}.

    One SWAEP of the audit log, not one query per person. Asking per user
    would be 3,900 accounts x 9 sandboxes = 35,000 requests; walking the log
    once per sandbox and tallying locally is the same answer in a fraction of
    the time, and aep_usage_log already does the hard part -- splitting any
    window that hits the 1,000-event page cap, and caching finished days so a
    re-run costs almost nothing."""
    client = ul.AuditClient(token, conf, sandbox)
    got = ul.collect(client, _SweepOpts(sandbox, days))
    per = {"__incomplete__": got.get("incomplete") or []}
    for e in got["kept"]:
        em = (e.get("userEmail") or "").lower()
        if not em:
            continue
        rec = per.setdefault(em, {"events": 0, "first": None, "last": None,
                                  "actions": Counter(), "assets": Counter()})
        rec["events"] += 1
        ts = e.get("timestamp") or ""
        if ts:
            if rec["first"] is None or ts < rec["first"]:
                rec["first"] = ts
            if rec["last"] is None or ts > rec["last"]:
                rec["last"] = ts
        rec["actions"][e.get("action") or "?"] += 1
        rec["assets"][e.get("assetType") or "?"] += 1
    # The bulk tech account is COUNTED rather than fetched, so its events are
    # in the noise tally instead of the event list. Fold it back in: leaving it
    # out would show the single busiest account on the platform as idle.
    for (day, user, asset, how), n in got["noise"].items():
        em = (user or "").lower()
        if not em:
            continue
        rec = per.setdefault(em, {"events": 0, "first": None, "last": None,
                                  "actions": Counter(), "assets": Counter()})
        rec["events"] += n
        rec["assets"][asset or "?"] += n
        if rec["last"] is None or day > (rec["last"] or ""):
            rec["last"] = day
        if rec["first"] is None or day < (rec["first"] or "~"):
            rec["first"] = day
    return per


def merge_activity(target: dict, sandbox: str, per: dict) -> None:
    """Fold one sandbox's tally into the cross-sandbox picture."""
    for em, rec in per.items():
        if em == "__incomplete__":
            target.setdefault("__incomplete__", {})[sandbox] = rec
            continue
        t = target.setdefault(em, {"events": 0, "first": None, "last": None,
                                   "per_sandbox": {}, "actions": Counter(),
                                   "assets": Counter()})
        t["events"] += rec["events"]
        t["per_sandbox"][sandbox] = rec["events"]
        t["actions"].update(rec["actions"])
        t["assets"].update(rec["assets"])
        for k in ("first", "last"):
            v = rec[k]
            if not v:
                continue
            if t[k] is None or (v < t[k] if k == "first" else v > t[k]):
                t[k] = v


def verify_unused(token: str, conf: dict, sandboxes: list, candidates: list,
                  days: int) -> dict:
    """Re-check each candidate ONE EMAIL AT A TIME, the way a person would do
    it in the audit log UI. Returns {email: exact_event_count}.

    This exists because the sweep and this check fail in opposite directions,
    and only one of them is safe to terminate an account on.

    The sweep pulls every event and tallies locally. It is fast, but Adobe
    caps any single query at 1,000 events, and a window still at that cap
    after being split to one-second granularity loses the overflow. Lost
    events make a busy account look idle -- the exact error that gets a
    working account cut off.

    A per-email query cannot make that mistake: totalElements is the server's
    own count for that user, whole, however many events there are. It is far
    too slow to run for every account in the org, but the removal list is a
    fraction of that, so each candidate for deletion gets the accurate check.
    """
    exact, clients = {}, {sb: ul.AuditClient(token, conf, sb) for sb in sandboxes}
    logger.info(f"{ANSI['bold']}Verifying {len(candidates)} removal "
                f"candidate(s){ANSI['reset']} one email at a time across "
                f"{len(sandboxes)} sandbox(es) -- this is the number the "
                f"decision rests on.")
    for i, p in enumerate(candidates, 1):
        # Recompute the window per account, with a margin inside Adobe's
        # retention boundary.
        #
        # The boundary is relative to NOW, and NOW moves while the run does.
        # Computing the window once at the start means that after a few
        # minutes the fixed start date has drifted past "90 days ago" and
        # Adobe rejects every subsequent request with a 422. In the first
        # version that happened at request 86 of 2,286: the remaining
        # accounts all errored, the errors were read as "no activity", and
        # 242 people were queued for deletion on the strength of a question
        # that was never actually asked.
        b = datetime.now(timezone.utc)
        a = b - timedelta(days=days) + WINDOW_MARGIN
        total, per, newest, capped = 0, {}, None, False
        for sb, client in clients.items():
            try:
                # The filter is CASE-SENSITIVE and the audit log stores
                # addresses lower-cased, while the Admin Console preserves
                # whatever case the account was created with. Querying
                # "Firstname.Lastname@..." verbatim returns 0 events for a
                # user who is in there every day.
                _throttle()
                r = client.get(ul.window_params(
                    a, b, [f"user=={p['email'].lower()}"], 3))
                n = (r.get("page") or {}).get("totalElements") or 0
                # Events come back newest-first, so the first one is the
                # person's last activity. Reading it here is free -- the
                # query has already been paid for.
                ev = (r.get("_embedded") or {}).get("events") or []
                if ev and ev[0].get("timestamp"):
                    ts = ev[0]["timestamp"]
                    if newest is None or ts > newest:
                        newest = ts
            except Exception as e:
                # A sandbox that errored is NOT a zero. Mark the account
                # unverifiable rather than quietly counting it as unused.
                per[sb] = None
                logger.debug(f"    {p['email']} in {sb}: {type(e).__name__}")
                continue
            if n:
                per[sb] = n
                total += n
                # totalElements is CAPPED at 1,000 per query. A count of
                # exactly 1,000 therefore means "1,000 or more" -- it is a
                # ceiling, not a measurement. Zero is still a real zero, which
                # is why the purge list is unaffected.
                if n >= AUDIT_PAGE_CAP:
                    capped = True
        unver = [k for k, v in per.items() if v is None]
        exact[p["email"]] = {"events": total, "per": per, "last": newest,
                             "capped": capped, "unverifiable": unver,
                             # Only a clean sweep of every sandbox counts as
                             # proof of absence. Anything less is "unknown",
                             # and unknown must never reach a deletion list.
                             "conclusive": not unver}
        if i % 25 == 0:
            logger.info(f"  ...verified {i}/{len(candidates)}")
    return exact


def verdict_for(p: dict, checked: bool) -> str:
    """The one-word answer per account. Usage is what matters; entitlement
    only changes what that usage (or its absence) MEANS."""
    if not checked:
        return "usage not checked"
    used, ent = p["events"] > 0, bool(p["aep"])
    if used and ent:
        return "ACTIVE"
    if used and not ent:
        return "ACTIVE WITHOUT AEP ENTITLEMENT"   # should be impossible
    if ent and not used:
        # Entitled, nothing found -- but "nothing found" only means "unused"
        # when every sandbox actually answered. A query that errored is not
        # evidence of absence, and must not reach a deletion list.
        if p.get("checked_conclusive"):
            return "PURGE -- no usage found"
        return "UNKNOWN -- could not check"
    return "no AEP access, no usage"


# How long since last use, in the bands an access review actually works in.
# A reviewer asking somebody "do you still need this?" wants to know whether
# they used it last week or not since July, not a raw day count.
LAST_USED_BANDS = ((7, "this week"), (30, "last 30 days"),
                   (60, "30-60 days"), (90, "60-90 days"))


def last_used_band(ts: str | None, entitled: bool) -> str:
    """A plain-English bucket for when somebody last used the platform."""
    if not entitled:
        return ""
    d = days_since(ts)
    if d == "":
        return f"NEVER (no use in {AUDIT_RETENTION_DAYS}d)"
    d = int(d)
    for limit, label in LAST_USED_BANDS:
        if d <= limit:
            return label
    return f"over 90 days"


def days_since(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        d = datetime.fromisoformat(ts[:19].replace("Z", ""))
        return str((datetime.utcnow() - d).days)
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# Workbook
# ----------------------------------------------------------------------------
def write_xlsx(data: dict, path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=_HEADER_BG)
    title_font = Font(bold=True, size=14, color=_HEADER_BG)
    conf_font = Font(bold=True, color="C00000")
    red = Font(bold=True, color="C00000")
    amber = Font(bold=True, color="B26B00")
    green = Font(color="2E7D32")
    centre = Alignment(horizontal="center")

    wb = Workbook()
    tables = []

    def sheet(name, title, note, columns, hrow=5):
        ws = wb.create_sheet(name)
        ws["A1"] = CONFIDENTIAL
        ws["A1"].font = conf_font
        ws.oddHeader.center.text = f'&"-,Bold"&12&KC00000{CONFIDENTIAL}'
        ws["A2"] = title
        ws["A2"].font = title_font
        ws["A3"] = note
        ws["A3"].font = Font(italic=True, color="666666")
        for c, nm in enumerate(columns, 1):
            cell = ws.cell(hrow, c, nm)
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[hrow].height = 28
        ws.freeze_panes = ws.cell(hrow + 1, 1)
        tables.append((ws, hrow, len(columns)))
        return ws

    def fit(ws, hrow, caps=None):
        """Measured widths, 90th percentile -- see data_dictionary_v3.fit."""
        caps = caps or {}
        lens, heads = {}, {}
        for cell in ws[hrow]:
            if cell.value is not None:
                heads[cell.column] = len(str(cell.value))
        for row in ws.iter_rows(min_row=hrow + 1):
            for cell in row:
                if cell.value is None:
                    continue
                v = cell.value
                ln = len(f"{v:,.0f}") if isinstance(v, (int, float)) and not isinstance(v, bool) else len(str(v))
                lens.setdefault(cell.column, []).append(ln)
        for col in set(lens) | set(heads):
            vals = sorted(lens.get(col, []))
            body = vals[int(len(vals) * 0.9)] if vals else 0
            floor = max(7, min(heads.get(col, 0), 15))
            ws.column_dimensions[get_column_letter(col)].width = round(
                min(max(body + 2.4, floor), caps.get(col, 52)), 1)

    # ---- Summary -----------------------------------------------------------
    s = data["stats"]
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = CONFIDENTIAL
    ws["A1"].font = conf_font
    ws.oddHeader.center.text = f'&"-,Bold"&12&KC00000{CONFIDENTIAL}'
    ws["A2"] = f"External access audit  -  {data['org_label']}"
    ws["A2"].font = title_font
    ws["A3"] = (f"Run {data['run_at']} by {SCRIPT_AUTHOR}  |  "
                f"{SCRIPT_NAME} v{SCRIPT_VERSION}  |  "
                f"activity window: last {data['days']} day(s)")
    ws["A3"].font = Font(italic=True, color="666666")

    r = 5
    ws.cell(r, 1, "THE QUESTION").font = Font(bold=True, color=_HEADER_BG)
    r += 1
    ws.cell(r, 1, "Who can reach Experience Platform who should not -- and are "
                  "they actually using it? Entitlement and use are different "
                  "questions and are reported separately below.")
    ws.cell(r, 1).font = Font(italic=True, color="666666")
    r += 2

    for label, value, note in data["headline"]:
        ws.cell(r, 1, label).font = Font(bold=True)
        c = ws.cell(r, 2, value)
        c.alignment = centre
        if isinstance(value, int) and value and "ACTIVE" in label.upper():
            c.font = red
        elif isinstance(value, int) and value and "access" in label.lower():
            c.font = amber
        ws.cell(r, 3, note).font = Font(italic=True, color="666666")
        r += 1

    r += 1
    ws.cell(r, 1, "SCOPE").font = Font(bold=True, color=_HEADER_BG)
    r += 1
    ws.cell(r, 1, "IN scope: Experience Platform / RTCDP, Customer Journey "
                  "Analytics, Journey Optimizer, Data Collection, Privacy "
                  "Service.")
    r += 1
    ws.cell(r, 1, f"OUT of scope: Workfront, Analytics, Creative Cloud, "
                  f"Acrobat. {s['out_of_scope_users']} account(s) hold only "
                  f"those and are listed but not flagged.")
    r += 2

    ws.cell(r, 1, "READ-ONLY").font = Font(bold=True, color="2E7D32")
    r += 1
    ws.cell(r, 1, "This report was produced by a tool that cannot write to "
                  "Adobe. Every non-GET request is blocked at the network "
                  "layer, not merely avoided by convention; the only POST "
                  "permitted is the IMS token request, which mints a "
                  "credential and changes nothing.").font = Font(
        italic=True, color="2E7D32")
    r += 2
    ws.cell(r, 1, "WHAT THIS CANNOT TELL YOU").font = red
    r += 1
    for line in data["limits"]:
        ws.cell(r, 1, line).font = Font(italic=True, color="C00000")
        r += 1
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 92

    # ---- All accounts: THE list -------------------------------------------
    one_sandbox = len(data.get("sandboxes") or []) <= 1
    cols = ["Email", "Domain", "Name", "ID type", "Status",
            f"Events in {data['days']}d", "Last active", "Days ago",
            "Last used"]
    if not one_sandbox:
        cols.append("Where")
    cols += ["AEP entitled", "AEP profile names", "Admin roles", "Verdict"]
    ws = sheet("All accounts",
               f"Every account, with what it actually DID in {data['days']} days",
               "One row per account in the Admin Console. Sorted by verdict: "
               "anything active without entitlement first, then PURGE "
               "candidates (entitled to AEP but no recorded usage), then "
               "active users, then the rest. Filter Verdict = PURGE for the "
               "removal list.   |   'Events' counts actions in the audit log "
               f"- opening a schema, running a segment, editing a journey. "
               f"Adobe caps a single query at {AUDIT_PAGE_CAP}, so "
               f"'{AUDIT_PAGE_CAP}+' means at least that many, not exactly "
               "that many; zero is a true zero, which is why the purge list "
               "is unaffected.   |   'Last active' is the most recent of "
               "those actions, and 'Days ago' how long since. Both are blank "
               "when there is no activity at all.", cols)
    rr = 6
    for u in data["all_accounts"]:
        # A count at the cap is a floor, not a number -- show it as such.
        ev = (f"{AUDIT_PAGE_CAP}+" if u.get("capped")
              else (u["events"] if u["events"] else ""))
        vals = [u["email"], u["domain"], u["name"], u["type"], u["status"],
                ev, (u["last"] or "")[:16].replace("T", " "),
                days_since(u["last"]),
                last_used_band(u["last"], bool(u["aep"]))]
        if not one_sandbox:
            vals.append(", ".join(f"{k} ({v})"
                                  for k, v in u["per_sandbox"].items()))
        vals += ["yes" if u["aep"] else "", ", ".join(u["aep"]),
                 ", ".join(u["admin"]) or "", u["verdict"]]
        vcol = len(vals)
        for c, v in enumerate(vals, 1):
            cell = ws.cell(rr, c, v)
            if c in (6, 8):
                cell.alignment = centre
            if c == 9 and str(v).startswith("NEVER"):
                cell.font = amber
            if c == 4 and v == "adobeID":
                cell.font = amber
            if c == 2 and u["domain"] != data["home_domain"]:
                cell.font = amber
            if c == vcol:
                cell.font = (red if "WITHOUT" in str(v) else
                             amber if str(v).startswith("PURGE") else
                             green if v == "ACTIVE" else Font(color="888888"))
        rr += 1
    fit(ws, 5, caps={1: 40, 3: 26, 10: 46, 12: 30})

    # ---- Access review: the list you send out for validation ---------------
    # Built for the conversation "do you still need this?", so it carries only
    # what a reviewer needs to ask that question and nothing else: who, what
    # they can reach, when they last used it. Sorted so the easiest cases to
    # challenge -- never used, then longest dormant -- come first.
    cols = ["Email", "Name", "Organisation", "What they can access",
            "Last active", "Days ago", "Last used", "Events", "Admin roles",
            "Recommendation"]
    ws = sheet("Access review", "Everyone with AEP / AJO access",
               "One row per person who can reach the platform, for sending "
               "out for validation. 'What they can access' lists the profiles "
               "and groups granting it. Sorted with never-used first, then "
               "longest dormant, so the clearest cases are at the top.", cols)
    rr = 6
    for u in data["review"]:
        band = last_used_band(u["last"], True)
        rec = ("Challenge - no use on record" if band.startswith("NEVER")
               else "Challenge - dormant" if band == "over 90 days"
               else "Confirm with owner" if band == "60-90 days"
               else "Keep")
        ev = (f"{AUDIT_PAGE_CAP}+" if u.get("capped")
              else (u["events"] if u["events"] else "0"))
        vals = [u["email"], u["name"], u["domain"], ", ".join(u["aep"]),
                (u["last"] or "")[:16].replace("T", " "),
                days_since(u["last"]), band, ev,
                ", ".join(u["admin"]) or "", rec]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(rr, c, v)
            if c in (6, 8):
                cell.alignment = centre
            if c == 3 and u["domain"] != data["home_domain"]:
                cell.font = amber
            if c in (7, 10):
                cell.font = (red if str(v).startswith(("NEVER", "Challenge - no"))
                             else amber if "Challenge" in str(v) or "over 90" in str(v)
                             else green if v in ("Keep", "this week") else Font())
        rr += 1
    fit(ws, 5, caps={1: 40, 2: 26, 4: 52, 10: 30})

    # ---- Purge list --------------------------------------------------------
    cols = ["Email", "Domain", "Name", "ID type", "AEP profile names",
            "Admin roles", f"Events ({data['days']}d)", "Verified",
            "Sandboxes not checkable"]
    ws = sheet("Purge list",
               f"Entitled to AEP, no usage in {data['days']} days",
               "These accounts can reach Experience Platform and have not "
               "touched it in the window. Safe to remove on that evidence -- "
               "but read the Summary tab's limits first: the log holds only "
               f"{AUDIT_RETENTION_DAYS} days, CJA keeps a separate log this "
               "does not read, and any unreadable sandbox is not an all-clear.",
               cols)
    rr = 6
    for u in data["purge"]:
        unver = ", ".join(u["unverifiable"])
        for c, v in enumerate([u["email"], u["domain"], u["name"], u["type"],
                               ", ".join(u["aep"]), ", ".join(u["admin"]) or "",
                               u["events"],
                               "no - sandbox error" if unver else
                               ("yes" if data["verified"] else "NOT VERIFIED"),
                               unver], 1):
            cell = ws.cell(rr, c, v)
            if c == 7:
                cell.alignment = centre
            if c == 2 and u["domain"] != data["home_domain"]:
                cell.font = red
            if c == 8:
                cell.font = green if v == "yes" else red
        rr += 1
    fit(ws, 5, caps={1: 40, 3: 26, 5: 50})

    # ---- External accounts -------------------------------------------------
    cols = ["Email", "Domain", "Name", "ID type",
            f"Events ({data['days']}d)", "Last seen", "AEP entitled",
            "AEP profile names", "Verdict"]
    ws = sheet("External accounts",
               f"Every account outside {data['home_domain']}",
               "All non-corporate domains -- consultancies, agencies, personal "
               "addresses. adobeID in the ID type column means a personal "
               "Adobe account that corporate SSO does not control.", cols)
    rr = 6
    for u in data["external"]:
        for c, v in enumerate([u["email"], u["domain"], u["name"], u["type"],
                               u["events"], (u["last"] or "")[:19].replace("T", " "),
                               "yes" if u["aep"] else "", ", ".join(u["aep"]),
                               u["verdict"]], 1):
            cell = ws.cell(rr, c, v)
            if c in (5, 7):
                cell.alignment = centre
            if c == 4 and v == "adobeID":
                cell.font = red
            if c == 9:
                cell.font = (red if "WITHOUT" in v else
                             amber if v.startswith("PURGE") else green
                             if v == "ACTIVE" else Font(color="888888"))
        rr += 1
    fit(ws, 5, caps={1: 40, 3: 26, 8: 46, 9: 30})

    # ---- Profiles ----------------------------------------------------------
    # Which profiles and AD groups actually grant Platform access, and how
    # many outsiders hold each. The external column is the one to read: a
    # profile with outside members is where a third party got in.
    cols = ["Profile / AD group", "Adobe product", "Type", "Members (org)",
            "Members on external domains"]
    ws = sheet("Profiles", "What grants AEP access",
               "Every product profile or Active Directory group that confers "
               "Experience Platform, CJA or Journey Optimizer access, with "
               "how many people hold it. Most access here comes via the "
               "GG-*-AdobeAEP-* AD groups rather than Adobe product profiles, "
               "which is why they are listed alongside. Sorted by external "
               "members first.", cols)
    rr = 6
    for pr in data["profiles"]:
        for c, v in enumerate([pr["name"], pr["product"] or "(AD group - no "
                               "Adobe product)", pr["type"] or "USER_GROUP",
                               pr["members"], pr["external"]], 1):
            cell = ws.cell(rr, c, v)
            if c in (4, 5):
                cell.alignment = centre
            if c == 5 and v:
                cell.font = red
        rr += 1
    fit(ws, 5, caps={1: 50, 2: 46})

    # ---- Tech accounts -----------------------------------------------------
    cols = ["Technical account", f"Events ({data['days']}d)", "Last seen",
            "Sandboxes used", "Top activity", "In roster?"]
    ws = sheet("Tech accounts", "Service / API accounts by volume",
               "@techacct.adobe.com integrations seen in the log, busiest "
               "first -- the credentials worth reviewing. Not people.", cols)
    rr = 6
    for t in data["tech"]:
        for c, v in enumerate([t["email"], t["events"],
                               (t["last"] or "")[:19].replace("T", " "),
                               ", ".join(f"{k} ({v2})" for k, v2 in t["per_sandbox"].items()),
                               ", ".join(f"{k} x{v2}" for k, v2 in t["top_assets"]),
                               t["in_roster"]], 1):
            cell = ws.cell(rr, c, v)
            if c == 2:
                cell.alignment = centre
        rr += 1
    fit(ws, 5, caps={1: 50, 4: 30, 5: 40})

    for ws_, hrow, ncols in tables:
        if ws_.max_row > hrow:
            ws_.auto_filter.ref = (f"A{hrow}:"
                                   f"{get_column_letter(ncols)}{ws_.max_row}")

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Who can reach AEP who should not, and are they using it.")
    p.add_argument("service", nargs="?", default=DEFAULT_SERVICE,
                   help=f"keyring credential service (default {DEFAULT_SERVICE})")
    p.add_argument("--days", type=int, default=AUDIT_RETENTION_DAYS,
                   help=f"activity window, max {AUDIT_RETENTION_DAYS} "
                        f"(Adobe's retention limit)")
    p.add_argument("--home-domain", default=DEFAULT_HOME,
                   help="the corporate domain; everything else counts as "
                        "external (default: whichever domain most of the "
                        "roster uses)")
    p.add_argument("--client", default=None,
                   help="organisation name for the filename and titles "
                        "(default: derived from the detected corporate "
                        "domain)")
    p.add_argument("--out-dir", default=None,
                   help="where to write the workbook. Defaults to "
                        "$AEP_AUDIT_OUT, else output/aep-user-logs/. Point "
                        "this at corporate storage, not personal cloud "
                        "storage -- the output carries names and access "
                        "rights.")
    p.add_argument("--sandboxes", default="prod",
                   help="comma-separated sandboxes to check, or 'all' for "
                        "every one the credential can read (default: prod). "
                        "One sandbox instead of nine is a 9x saving, and prod "
                        "is where the access that matters lives.")
    p.add_argument("--no-sweep", action="store_true",
                   help="skip the whole-log sweep and go straight to the "
                        "per-email check of every AEP-entitled account. Much "
                        "faster, and the removal list is just as accurate; "
                        "what you lose is usage for accounts with no AEP "
                        "entitlement.")
    p.add_argument("--no-verify", action="store_true",
                   help="skip the per-email re-check of removal candidates "
                        "(FASTER, but the purge list is then only as good as "
                        "the sweep -- not safe to terminate on)")
    p.add_argument("--no-activity", action="store_true",
                   help="entitlement only; skip the per-user audit queries")
    return p.parse_args(argv)


def main():
    install_readonly_guard()
    _log_setup()
    opts = parse_args(sys.argv[1:])
    if opts.days > AUDIT_RETENTION_DAYS:
        logger.warning(f"--days {opts.days} exceeds Adobe's "
                       f"{AUDIT_RETENTION_DAYS}-day audit retention; "
                       f"using {AUDIT_RETENTION_DAYS}.")
        opts.days = AUDIT_RETENTION_DAYS

    print(f"{ANSI['cyan']}{'='*72}{ANSI['reset']}")
    print(f"  {ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}  ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {ANSI['dim']}Entitlement vs actual use, for everyone who can "
          f"reach Experience Platform.{ANSI['reset']}")
    print(f"{ANSI['cyan']}{'='*72}{ANSI['reset']}")
    print(aep_creds.source_banner())

    conf = aep_creds.load_creds(opts.service)
    token = ul.authenticate(conf)
    logger.info(f"IMS authenticated (org {conf['org_id']}).")

    users, groups = fetch_roster(token, conf)
    idx = build_profile_index(groups)

    people = []
    for u in users:
        email = (u.get("email") or "").strip()
        if not email:
            continue
        aep, out, other = split_groups(u, idx)
        people.append({
            "email": email, "domain": domain_of(u),
            "name": f"{u.get('firstname','')} {u.get('lastname','')}".strip(),
            "type": u.get("type") or "", "status": u.get("status") or "",
            "admin": u.get("adminRoles") or [], "aep": aep, "out": out,
            "other": other, "events": 0, "per_sandbox": {}, "checked": False,
            "last": None, "top_assets": [], "sweep_missed": False,
            "capped": False,
            "unverifiable": [], "checked_conclusive": False,
        })

    home = (opts.home_domain or "").lower()
    if not home:
        home = Counter(domain_of(u) for u in users
                       if u.get("email")).most_common(1)[0][0]
        logger.info(f"Corporate domain detected as '{home}' "
                    f"(override with --home-domain).")
    external = [p for p in people if p["domain"] != home]
    aep_users = [p for p in people if p["aep"]]
    logger.info(f"{len(people)} account(s): {len(aep_users)} entitled to AEP, "
                f"{len(external)} on a non-{home} domain.")

    # ---- Usage: one sweep per sandbox, then join ---------------------------
    activity, sandboxes, unreadable = {}, [], []
    if not opts.no_activity:
        sandboxes, unreadable = readable_sandboxes(token, conf)
        if (opts.sandboxes or "").strip().lower() != "all":
            wanted = [x.strip() for x in opts.sandboxes.split(",") if x.strip()]
            missing = [w for w in wanted if w not in sandboxes]
            skipped = [x for x in sandboxes if x not in wanted]
            sandboxes = [w for w in wanted if w in sandboxes]
            if missing:
                logger.warning(f"  requested but NOT readable: "
                               f"{', '.join(missing)}")
            if skipped:
                logger.info(f"  not checked (outside --sandboxes): "
                            f"{', '.join(skipped)}")
            if not sandboxes:
                logger.error("None of the requested sandboxes are readable.")
                return
        # prod first: it is the one that matters, so if a later sandbox stalls
        # the important half of the answer is already in hand.
        sandboxes.sort(key=lambda n: (n != "prod", "prod" not in n, n))
        logger.info(f"Audit log readable in {len(sandboxes)} sandbox(es): "
                    f"{', '.join(sandboxes)}"
                    + (f"  |  UNREADABLE: {', '.join(unreadable)}"
                       if unreadable else ""))
        if opts.no_sweep:
            logger.info("--no-sweep: skipping the whole-log sweep; every "
                        "AEP-entitled account will be checked one email at a "
                        "time instead.")
        for sb in ([] if opts.no_sweep else sandboxes):
            logger.info(f"{ANSI['bold']}Sweeping {sb}{ANSI['reset']} "
                        f"({opts.days} days) ...")
            try:
                merge_activity(activity, sb,
                               sweep_sandbox(token, conf, sb, opts.days))
            except Exception as e:
                logger.error(f"  {sb}: sweep failed ({type(e).__name__}: {e}); "
                             f"treated as UNREADABLE.")
                unreadable.append(sb)
                continue
            logger.info(f"  {sb}: {len(activity):,} distinct account(s) seen "
                        f"so far.")

    # The sweep stashes its lost-window report under a reserved key. Take it
    # out BEFORE anything iterates `activity` as if every value were a user
    # record -- leaving it in crashed the tech-account tally at the very end
    # of a two-hour run, after all the work was done and before anything was
    # written.
    incomplete = activity.pop("__incomplete__", {})

    for p in people:
        act = activity.get(p["email"].lower())
        if act:
            p["events"] = act["events"]
            p["per_sandbox"] = act["per_sandbox"]
            p["last"] = act["last"]
            p["top_assets"] = act["assets"].most_common(3)
        p["checked"] = not opts.no_activity

    # Technical accounts never appear in the human roster, so they come
    # straight from the sweep.
    roster_emails = {p["email"].lower() for p in people}
    tech_rows = []
    for em, act in sorted(activity.items(), key=lambda kv: -kv[1]["events"]):
        if not em.endswith(TECHACCT):
            continue
        tech_rows.append({"email": em, "events": act["events"],
                          "last": act["last"],
                          "per_sandbox": act["per_sandbox"],
                          "top_assets": act["assets"].most_common(3),
                          "in_roster": "yes" if em in roster_emails else "no"})

    # Anyone active who is NOT in the roster at all -- a deleted account whose
    # events survive, or an identity the Admin Console does not list.
    ghosts = [em for em, act in activity.items()
              if em not in roster_emails and not em.endswith(TECHACCT)
              and act["events"]]

    for p in people:
        p["verdict"] = verdict_for(p, p["checked"])

    # ---- Verify the removal list the accurate way --------------------------
    verified = {}
    if not opts.no_activity and not opts.no_verify:
        candidates = [p for p in people
                      if p["aep"] and not p["events"]]
        if candidates:
            verified = verify_unused(token, conf, sandboxes, candidates,
                                     opts.days)
            rescued = 0
            for p in candidates:
                v = verified.get(p["email"]) or {}
                if v.get("events"):
                    # The sweep missed these. Trust the per-email count.
                    p["events"] = v["events"]
                    p["per_sandbox"] = {k: n for k, n in v["per"].items() if n}
                    p["sweep_missed"] = True
                    rescued += 1
                p["unverifiable"] = v.get("unverifiable") or []
                p["checked_conclusive"] = bool(v.get("conclusive"))
                if v.get("last") and not p.get("last"):
                    p["last"] = v["last"]
                p["capped"] = bool(v.get("capped"))
            if rescued:
                logger.warning(
                    f"{ANSI['red']}{rescued} account(s) the sweep showed as "
                    f"unused DO have activity{ANSI['reset']} -- the per-email "
                    f"check found events the 1,000-per-query cap had hidden. "
                    f"They have been taken OFF the removal list.")
            else:
                logger.info("Verification agreed with the sweep on every "
                            "candidate.")
            for p in people:
                p["verdict"] = verdict_for(p, p["checked"])

    # ---- Profiles ----------------------------------------------------------
    ext_emails = {p["email"] for p in external}
    prof_members, prof_ext = Counter(), Counter()
    for p in people:
        for g in p["aep"]:
            prof_members[g] += 1
            if p["email"] in ext_emails:
                prof_ext[g] += 1
    gmeta = {g.get("groupName"): g for g in groups}
    profiles = [{"name": n,
                 "product": (gmeta.get(n, {}) or {}).get("productName") or "",
                 "type": (gmeta.get(n, {}) or {}).get("type") or "",
                 "members": prof_members[n], "external": prof_ext[n]}
                for n in sorted(prof_members, key=lambda x: (-prof_ext[x],
                                                             -prof_members[x],
                                                             x.lower()))]

    # ---- Sort: worst first -------------------------------------------------
    ORDER = {"ACTIVE WITHOUT AEP ENTITLEMENT": 0, "PURGE -- entitled, unused": 1,
             "ACTIVE": 2}
    def rank(p):
        return (ORDER.get(p["verdict"], 3), -p["events"], p["email"].lower())
    people.sort(key=rank)
    external = [p for p in people if p["domain"] != home]
    def _review_rank(p):
        d = days_since(p.get("last"))
        return (0 if d == "" else 1, -int(d) if d != "" else 0,
                p["email"].lower())
    review = sorted([p for p in people if p["aep"]], key=_review_rank)
    purge = [p for p in people if p["verdict"].startswith("PURGE")]
    unknown = [p for p in people if p["verdict"].startswith("UNKNOWN")]
    active = [p for p in people if p["events"]]
    anomalies = [p for p in people if "WITHOUT" in p["verdict"]]

    headline = [
        ("Accounts in the Admin Console", len(people), "every identity in the org"),
        (f"Used AEP in the last {opts.days} days", len(active),
         "accounts with at least one recorded audit event"),
        ("Entitled to AEP", len(aep_users),
         "can reach AEP / CJA / AJO / Data Collection / Privacy Service"),
        ("PURGE: entitled, no usage found", len(purge),
         "every READABLE sandbox answered and found nothing"),
        ("UNKNOWN: entitled, could not check", len(unknown),
         "a sandbox query failed; NOT safe to remove until re-run"),
        ("Active WITHOUT entitlement", len(anomalies),
         "should be impossible; investigate each one"),
        (f"Accounts outside {home}", len(external), "any non-corporate domain"),
        ("Active accounts not in the roster", len(ghosts),
         "events from an identity the Admin Console does not list"),
    ]
    limits = [
        f"The audit log retains {AUDIT_RETENTION_DAYS} days. 'No usage' means "
        f"none in the last {opts.days} day(s), not 'never'.",
        ("Sandboxes that could NOT be read: "
         + (", ".join(sorted(set(unreadable))) if unreadable else "none")
         + ". A sandbox that refused us is not an all-clear."),
        "CJA keeps its own audit trail, which this does not read. A CJA-only "
        "user can show zero usage here and still be active in CJA.",
        "Product profiles are read as DIRECT memberships; access inherited via "
        "a nested user group is not resolved, so entitled counts are a floor.",
        ("Sandboxes swept: " + (", ".join(sandboxes) if sandboxes else "none")),
        (("Every account on the Purge list was re-checked ONE EMAIL AT A TIME "
          "against the audit log -- the same query you would run by hand -- so "
          "its counts do not depend on the sweep.") if verified else
         ("The Purge list was NOT re-checked per email (--no-verify). Its "
          "counts come from the sweep alone and may understate activity.")),
    ]
    if ghosts:
        limits.append("Accounts seen in the log but absent from the roster: "
                      + ", ".join(sorted(ghosts)[:12])
                      + (" ..." if len(ghosts) > 12 else ""))
    bad = {sb: w for sb, w in (incomplete or {}).items() if w}
    if bad:
        limits.insert(0, "SWEEP LOSS: " + "; ".join(
            f"{sb}: {len(w)} window(s) still at the 1,000-event cap"
            for sb, w in bad.items())
            + ". Those events were not retrieved. The Purge list is unaffected "
              "(it is verified per email); other usage counts may understate.")
    if opts.no_activity:
        limits.insert(0, "--no-activity was used: NO usage was checked at all.")

    data = {
        "org_label": (opts.client or home.split(".")[0]).title(),
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "days": opts.days, "home_domain": home,
        "all_accounts": people, "purge": purge, "external": external,
        "profiles": profiles, "tech": tech_rows, "sandboxes": sandboxes,
        "review": review,
        "verified": bool(verified), "incomplete": incomplete,
        "stats": {"out_of_scope_users": sum(1 for p in people
                                            if p["out"] and not p["aep"])},
        "headline": headline, "limits": limits,
    }
    out_dir = Path(opts.out_dir) if opts.out_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # Name the file after the organisation it describes, so it still makes
    # sense months later in a folder of other people's reports. The name is
    # DERIVED from the corporate domain this run detected, never hard-coded:
    # a client's name does not belong in this repo, but it very much belongs
    # on their report. --client overrides.
    org = (opts.client or home.split(".")[0]).strip().title() or "Org"
    where = ",".join(sandboxes) if sandboxes else "no-sandbox"
    safe = re.sub(r"[^0-9A-Za-z _,-]+", "", f"{org} - {where}").strip()
    path = out_dir / (f"AEP Access Audit - {safe} - "
                      f"{datetime.now():%Y-%m-%d}.xlsx")
    path = write_xlsx(data, path)

    print()
    for label, value, _ in headline:
        colour = (ANSI["red"] if ("ACTIVE" in label.upper() and value)
                  else ANSI["yellow"] if value and "..." in label
                  else ANSI["reset"])
        print(f"  {label:<44} {colour}{ANSI['bold']}{value}{ANSI['reset']}")
    if anomalies:
        print()
        print(f"  {ANSI['red']}{ANSI['bold']}ACTIVE WITHOUT ENTITLEMENT:{ANSI['reset']}")
        for p in anomalies[:15]:
            print(f"    {p['email']:<42} {p['events']:>6} events")
    ext_aep = [p for p in external if p["aep"]]
    if ext_aep:
        print()
        print(f"  {ANSI['bold']}External accounts entitled to AEP:{ANSI['reset']}")
        for p in ext_aep:
            mark = (ANSI["red"] + "ACTIVE " if p["events"] else
                    ANSI["yellow"] + "unused ")
            print(f"    {mark}{ANSI['reset']} {p['email']:<40} "
                  f"{p['type']:<13} {', '.join(p['aep'])[:46]}")
    logger.info(f"XLSX written: {path}")


if __name__ == "__main__":
    main()
