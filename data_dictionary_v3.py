#!/usr/bin/env python3
"""
data_dictionary_v3.py  (AEP Swiss Army Knife)
=============================================
Data Dictionary v3.4. Sucks every XDM schema out of an AEP sandbox, filters down
to the ones that actually matter, and writes a tabbed Excel workbook: a master
field index, one tab per schema (full field list, ready to paste into Claude
for a Mermaid ERD), a Datasets tab mapping every dataset's friendly name to its
SQL table name, an Audiences tab, two Snapshot tabs (every snapshot export
table with its merge policy, system evaluation time and time to run -- folded
in from snapshot_tables.py), and -- with --data-dict -- real field coverage +
top-10 example values sampled from ingested data (PII is obfuscated -- see below).

The workbook is marked STRICTLY CONFIDENTIAL: with --data-dict it contains
real sampled customer data.

Pick a credential set (keyring-first from the OS vault, or a plaintext creds/
folder where keyring is unavailable) and the tool will:
  1. Authenticate against IMS (client_credentials).
  2. GET /sandboxes -- every sandbox the credential can see.
  3. Prompt you to pick which sandbox(es) to read (Enter = prod by default).
  4. Per chosen sandbox: list all tenant schemas, count datasets per schema
     (Catalog), and pull every descriptor (identities, relationships, and the
     friendly-name labels).
  5. FILTER the schemas (see below), print the full list to screen with a
     KEEP / DROP verdict per schema so you can see exactly what was excluded.
  6. For each KEPT schema, resolve its full field list (dot notation + data
     type), join identities / relationships / friendly-name labels, and write
        output/Data Dictionary - <Client> - <YYYY-MM-DD>.xlsx
     (Summary, a master Field Index, a Schemas index, a Datasets / table-name
     map, then one tab per schema).

Filtering (phase 1 -- transparent and reported, tune the constants below):
  KEEP   schema is referenced by >= 1 dataset (the UI "DATASETS" column).
  DROP   no-dataset   nothing ingests into it (ad-hoc audience schemas,
                      unused drafts) -- not interesting yet.
  DROP   adhoc        the schema extends the ad-hoc behaviour (auto-generated
                      ingestion schemas). Custom classes built on record /
                      time-series (product, store, promotion lookups) are KEPT.
  DROP   ajo          Adobe Journey Optimizer-managed schema (title / extends
                      heuristic) -- out of scope for now.

The friendly-name label (alternateDisplayInfo) count is reported per sandbox so
you can see whether this tenant uses friendly field labels at all. The Friendly
Name column is always present (blank when a field has no alternate label).
These are NOT DULE labels: DULE (Data Usage Labeling and Enforcement) is
Adobe's governance framework that controls access to objects and data via
labels such as C8 / C12, and is not in the workbook yet.

Phase 2 -- data dictionary (--data-dict): for each field, sample real ingested
records (Snappy-Parquet) via the Data Access API and tally COVERAGE (% of
sampled rows where the field is populated) and the TOP-10 values
(value(count), pipe-separated). One download covers every field at once -- no
Query Service, no per-field calls. Two extra columns are added to each sampled
schema's tab. Defaults to ALL kept schemas (a bare --data-dict samples every
tab -- expect this to run for a long while); --data-dict=<substr> narrows it to
schemas whose title matches (e.g. --data-dict=profile for the Profile schema).

v3.1 -- Profile coverage fix: a Profile-class schema is the post-merge UNION
(identity-deduped, last-write-wins), so sampling its feeding datasets tallies
pre-merge fragments and reads falsely sparse. For Profile-class schemas the tool
now samples the Profile Snapshot Export dataset belonging to the DEFAULT merge
policy instead (auto-resolved: default merge policy -> the snapshot dataset
tagged with that policy id whose schemaRef is profile__union). Snapshots are
huge, so it downloads the smallest non-empty partition file. Override the dataset
with --profile-snapshot=<datasetId> (e.g. for orgs where the default isn't the
one you want).

v3.2 -- Two things ship under this label:
  * Bundled Luma demo dataset (demo/luma/): Adobe's public platform-utils sample
    data, organised + tenant-normalised for offline demos and tests.
  * SQL table (system) names. Each dataset carries an AEP Query Service table name
    in tags['adobe/pqs/table'] (e.g. "acme_cja_acme_order_event_dataset") -- the
    normalized SYSTEM name you SELECT ... FROM, distinct from the friendly dataset
    name. The workbook now surfaces it so SQL can be formed against the right table
    straight from this file: a new "Datasets" tab maps every dataset's friendly
    name -> table name -> schema; the Schemas index and Field Index each gain a
    "Table name(s)" column; and every schema tab's header block lists its SQL
    table name(s) next to the field list.
  * Profile column on the Datasets tab (tags.unifiedProfile): flags every dataset
    enabled for Unified Profile and the Profile Snapshot Export(s) -- so you know
    which tables to query for whole profiles. Profile-related rows sort to the top.
  * Data-completeness warnings so a partial dictionary is never read as gospel:
    any schema whose --data-dict coverage is missing/partial/empty is listed in a
    Summary "DATA COMPLETENESS" block and flagged with a red banner on its own tab
    (coverage MISSING != 0%). The Profile Snapshot Export is sampled once per run:
    if its (huge) file manifest 504s under load, the failure is cached so the
    other Profile schemas fail fast instead of each re-hitting the dead snapshot.

v3.4.16 -- Field history: every unreadable month explained, and the question
that matters for audiences answered. The 126 months v3.4.15 left as '?' on
roi-prod were (a) data-lake files that really hold 0 rows -- Profile-loaded
datasets leave a schema-only file in the lake -- now labelled "data-lake files
empty"; (b) the identity service's '-INGEST' marker batches (DTAC-4043 on the
file list), now skipped like Profile-side ones; (c) months whose readable
batches were hidden behind a busy month's newest 10 -- the month is now asked
for 100 at a time, paging back up to 5 pages, and up to 4 batches are tried.
A Profile-only month records how many records Profile received. NEW: "In
Profile now (days)" per field -- days of unbroken data, capped by the Profile
retention set on its datasets (on roi-prod: 90 days on most event datasets,
45 on orders, 60 on promotion interactions), with the reason in words.

v3.4.15 -- The cause of the history pass's 504s found and removed. Every one of
the 328 file lists that 504'd on the 2026-10-23 roi-prod run (5 h 15 min) was
the PROFILE-SIDE half of a batch ingestion: an old-style 24-hex batch whose
metrics are recordsRead / recordsWritten / profileFragments. Catalog lists it as
an ordinary success batch, but it has no data-lake files, and Data Access hangs
to the 60 s gateway limit instead of saying so -- the next morning, again, and on
retry 30 s and 90 s later. Its data-lake batches read in seconds. These batches
are now skipped wherever data is sampled (coverage and history), and a month
holding nothing else is marked "Profile only" at once, not read. A month that
cannot be read for good (Profile only, empty files, files gone: HTTP 404) is
cached like any finished month; only a timeout or 5xx is retried next run.
Batches with a known record count are tried before ones without.

v3.4.14 -- History engine hardened after the first UK prod run (7 h, 38% of
months unreadable, 12 datasets lost to one 504). No dataset-wide "oldest batch"
query any more: months are walked with the fast newest-first Catalog query and
the exact start is looked up only inside the first month with data. ONE file
list per batch read (was up to 40) with a 90 s wait (was 30). A locked dataset
(HTTP 403) is recognised at once and skipped. When most recent Data Access
calls fail, history pauses 3 min instead of piling up unreadable months. Every
request is now counted per Adobe endpoint: retries name the endpoint and the
reason, and each pass ends with an endpoint health report.

v3.4.13 -- BETA: "Have we got it?" (--history). How many months of UNBROKEN
data each field has, up to now -- for building audiences. Per dataset and
month, Catalog finds that month's batches and one is read through the Data
Access API; that one download answers for every field, so the cost is datasets
x months, not fields x months. A thin, odd or gappy month is sampled again from
earlier in the month before it is believed. Three columns on the end of the
Field Index and every schema tab (months, plain-English detail, a heartbeat
with one bar per month) and a Field History tab, month by month, shaded.
Test/perf/temp datasets are left out; months go by LOAD date. Finished months
are cached in output/field_history_cache/, so only the first run is long.
See add_field_history.

v3.4.12 -- Two changes to the --data-dict sample columns. (1) TOP values go
from 5 to 10. (2) PII obfuscation: any sampled value that is an email address,
or that sits in an identifier / contact field (user id, CRM id, phone, ...), is
starred by two characters BEFORE it is tallied -- whether or not the field
carries a DULE governance label. The workbook never reproduces a whole real
identifier. See _mask_pii / _PII_TOKENS.

v3.4.11 -- The run holds Windows awake (system and screen) until it ends, so
Modern Standby can't freeze a long run part-way. The hold dies with the
process; power settings are untouched.

v3.4.10 -- A hidden Dropped Field Index: every field of every dropped schema
(path, type, display name, the schema, why it was dropped, its tables), so the
dictionary is a full census. Inventory only -- no coverage. Prod: ~244k rows,
five or six extra minutes of schema fetches.

v3.4.9 -- A hidden Dropped Schemas tab: every schema the filters left out,
with the reason in plain English, its dataset count and SQL table name(s).
Schemas that still hold data are listed first. A missing table can now be
looked up instead of guessed at (which is how v3.4.8's bug went unseen).

v3.4.8 -- Lookup schemas are back. Any class whose id ended in a long hex
string was dropped as ad-hoc, but a custom class made in the UI gets the same
kind of id -- so every lookup (Tesco Product Lookup, Marketplace Product,
Store, Promotion, GTIN/TPNB, Recipes, Weather ...) went out with the ad-hoc
dumps. The test is now the behaviour the schema extends: ad-hoc is dropped,
record / time-series is kept.

v3.4.6 -- Audience complexity score (beta). Not all audiences are created
equal: one badly built audience can double the nightly batch run on its own,
and AEP never reports what an audience cost to compute. Every rule-based
audience now carries a 0-100 score and a RED / AMBER / GREEN on the Audiences
tab, with the reasons in plain English and the raw features in their own
columns. The drivers, after the miaprova.com write-ups on expensive segments:
an event scan and how far back it looks (no time limit scores highest), lookup
joins to another class, aggregations that cannot short-circuit (sum / count /
"occurs N times"), and the size of the base audience it depends on. The
formula is printed above the columns; the weights live in
audience_complexity.py (developed standalone, folded in).

v3.4.5 -- Full-PQL sidecar and the dictionary's own folder. Excel caps a cell
at 32,767 characters; every audience's complete rule now goes to
output/data_dictionary/pql/audiences_pql_<sandbox>_<yyyymmdd>.jsonl and a cut
cell is marked, with PQL length / truncated / format columns. The workbook,
its archive and the sidecars live under output/data_dictionary/.

v3.4.4 -- Snapshot Summary and Snapshot Tables tabs (every snapshot export
table, its merge policy, system evaluation time and time to run), written by
snapshot_tables.add_sheets().

v3.3 -- Readability and provenance:
  * An Audiences tab: every audience with tags, owner, last-modified and its
    segmentation rule (PQL) rendered readable.
  * A provenance line on every tab (when it ran, which script version, which
    commit, and a loud warning when the working tree was dirty).
  * A How to Use tab, filters + frozen headers everywhere, and the credential
    name dropped from the FILENAME in favour of the sandbox.

v3.4 -- The credential name is gone from the workbook itself. The titles inside
the file used to fall back to the credential's service name, so a workbook read
with a key stored as "<name>" was headed "Data Dictionary - <Name>" -- the name
of an entry in our own vault, printed where the reader expects the subject of
the report.
Titles now use the client name ONLY when the credential record carries an
explicit "client" key, and otherwise identify the workbook by SANDBOX, matching
what the filename has said since v3.3. Every tab also carries a link to the
release notes, so a dictionary found months later can be traced to what the
version that produced it actually did.

v3.4.3 -- The friendly-label headings are renamed. The alternateDisplayInfo
friendly-name count used column headings that read like a misspelling of DULE
-- Adobe's Data Usage Labeling and Enforcement framework for object and data
access control -- which is a different thing entirely. The columns now say
"Friendly labels" / "Friendly-labelled fields", and the How to Use tab explains
what DULE is and that DULE labels are not in this workbook yet.
Also: friendly names and descriptions everywhere. Every field row (schema tabs
and the Field Index) gains a Friendly Path (display names from the root down,
"Person > Full name > First name") and a Description (the field's own, or its
alternateDisplayInfo description where the field has none). Schemas, datasets
and audiences each gain a Description column, and every schema tab shows its
schema's description under the title.

v3.4.2 -- A tab colour system that means something. Purple used to mean only
"built on the Profile class": a Profile-class schema whose datasets were all
DISABLED for Unified Profile was still purple, and -- worse -- its coverage was
sampled from the Profile Snapshot Export, where its rows never land, so it read
as empty. Every schema tab is now coloured on two axes: HUE = class (purple
Profile record, blue ExperienceEvent, green other/lookup), SHADE = dark when at
least one dataset is enabled for Profile, light when none is. Only dark purple
is in the merged union and sampled from the snapshot; everything else samples
its own datasets. A six-swatch key on the Summary tab explains it.
Also: data-hygiene columns. The Schemas and Datasets tabs now carry, from
Catalog's extensions, the data lake size (GB) and row count, the data lake
retention TTL, the Profile store size and the Profile TTL (days), plus a Kind
(Profile / Event / Custom) and In Profile (Y/N) flag -- so the sheet can be
sorted by GB or TTL and filtered by kind, which the UI makes hard to do.

v3.4.1 -- Complete coverage on Profile schemas. Every Profile-class schema reads
the SAME union dataset, but each one used to re-download the snapshot partition
(~127MB) for itself: slow, and one gateway 504 anywhere in that sequence was
cached into MISSING coverage for every Profile schema behind it. The snapshot is
now sampled ONCE per sandbox and the rows reused. A second pass then revisits
anything still UNREADABLE and tries it once more before the workbook is written,
because a 504 is usually transient.

Full version history: RELEASE_NOTES.md (link in the workbook's How to Use tab).

openpyxl is needed for the XLSX; pyarrow + tzdata for --data-dict (all optional,
pip install -r requirements.txt).

Usage:
    python data_dictionary_v3.py                       # interactive menus
    python data_dictionary_v3.py "acme-beta"          # pick creds by service name
    python data_dictionary_v3.py "acme-beta" --sandbox=prod,dev1
    python data_dictionary_v3.py "acme-beta" --sandbox=all
    python data_dictionary_v3.py "acme-k" --sandbox=prod --data-dict       # ALL schemas
    python data_dictionary_v3.py "acme-k" --sandbox=prod --data-dict --dd-rows=2000
    python data_dictionary_v3.py "acme-k" --sandbox=prod --data-dict=profile  # one schema
    python data_dictionary_v3.py "acme-k" --sandbox=prod --data-dict \
        --profile-snapshot=67dc0539eaafb02aeeb92ed7   # override snapshot dataset
    python data_dictionary_v3.py "acme-k" --sandbox=prod --history     # BETA: months of data
    python data_dictionary_v3.py "acme-k" --sandbox=prod --history=order --history-months=36

Credentials come from the OS keyring (Windows Credential Manager) via aep_creds,
falling back to a plaintext creds/ folder where keyring is unavailable; manage
them with aep_credentials.py. Service names are the keys you stored --
run `aep_credentials.py list` to see them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aep_creds  # keyring-backed credential store (replaces creds/*.json)
# Tools are developed standalone and then folded in: the dictionary calls the
# tool's own collector and tab writer, so there is one copy of the logic and the
# standalone script keeps working.
import snapshot_tables
import audience_complexity     # beta complexity score for the Audiences tab
import overlap_report          # dataset overlap -> Overlap cut-list / full report tabs
from house_xlsx import Book

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_NAME    = "data_dictionary_v3"
SCRIPT_VERSION = "3.4.16"
SCRIPT_DATE    = "2026-10-24"
SCRIPT_AUTHOR  = "Barry Mann (barrymann.com)"
AUTHOR_SITE     = "https://barrymann.com"
AUTHOR_LINKEDIN = "https://www.linkedin.com/in/barrymann/"

# Where the version history lives. Surfaced in the workbook (provenance line +
# How to Use tab) so whoever opens a dictionary months from now can find out
# what the version that produced it actually did.
RELEASE_NOTES_URL = ("https://github.com/mannbarry2/aep-swiss-army-knife"
                     "/blob/main/RELEASE_NOTES.md")

SCRIPT_DIR = Path(__file__).resolve().parent
# The dictionary (and its sidecars / archive) gets its own folder: output/ is
# shared with every other tool in the repo and this is the file that matters.
OUTPUT_DIR = Path(os.environ.get("AEP_OUTPUT_DIR") or SCRIPT_DIR / "output") / "data_dictionary"
PQL_DIR = OUTPUT_DIR / "pql"          # full-PQL sidecars, one per sandbox per run

IMS_URL = "https://ims-na1.adobelogin.com/ims/token"
PLATFORM = "https://platform.adobe.io"
SANDBOX_LIST_URL = f"{PLATFORM}/data/foundation/sandbox-management/sandboxes"
SCHEMAS_URL = f"{PLATFORM}/data/foundation/schemaregistry/tenant/schemas"
DESCRIPTORS_URL = f"{PLATFORM}/data/foundation/schemaregistry/tenant/descriptors"
DATASETS_URL = f"{PLATFORM}/data/foundation/catalog/dataSets"
CATALOG_BATCHES_URL = f"{PLATFORM}/data/foundation/catalog/batches"
EXPORT_URL = f"{PLATFORM}/data/foundation/export"   # Data Access (batch files)
UPS_MERGE_POLICIES_URL = f"{PLATFORM}/data/core/ups/config/mergePolicies"
UPS_SEGMENT_DEFS_URL = f"{PLATFORM}/data/core/ups/segment/definitions"

# Profile coverage (v3.1): a Profile-class schema's coverage must be sampled from
# the merged UNION (Profile Snapshot Export), not its pre-merge feeding datasets.
PROFILE_CLASS = "https://ns.adobe.com/xdm/context/profile"
PROFILE_UNION = "https://ns.adobe.com/xdm/context/profile__union"

# Worksheet tab colour for Profile-class schemas (they stand out from the
# event/lookup tabs; their coverage is sampled from the Profile Snapshot Export
# union, not pre-merge feeds). Explained in the Summary tab's colour key.
# Tab colour system (explained in the Summary tab's colour key):
#   HUE   = the schema's class   -- purple: XDM Individual Profile (record),
#                                   blue: XDM ExperienceEvent, green: any other
#                                   (lookup / custom) class.
#   SHADE = whether its data reaches Unified Profile -- dark: at least one of
#           its datasets is enabled for Profile (tags.unifiedProfile), light:
#           none is. Only dark-purple schemas are in the merged union, so only
#           they sample coverage from the Profile Snapshot Export.
EVENT_CLASS = "https://ns.adobe.com/xdm/context/experienceevent"
TAB_COLOURS = {
    ("profile", True):  "7030A0",   # dark purple
    ("profile", False): "C9B8E8",   # light purple
    ("event",   True):  "0070C0",   # dark blue
    ("event",   False): "9DC3E6",   # light blue
    ("other",   True):  "548235",   # dark green
    ("other",   False): "C5E0B4",   # light green
}
TAB_COLOUR_KEY = [
    (("profile", True),  "Profile-class schema whose data IS in Unified Profile "
                         "(at least one dataset enabled for Profile). Its fields "
                         "are part of the merged profile, so coverage is sampled "
                         "from the Profile Snapshot Export union, not the "
                         "pre-merge feeding datasets."),
    (("profile", False), "Profile-class schema NOT in Unified Profile (none of its "
                         "datasets is enabled for Profile: raw feeds, backups, "
                         "experiments). Its rows never reach the union; coverage "
                         "is sampled from its own datasets."),
    (("event",   True),  "ExperienceEvent-class schema whose datasets feed "
                         "Unified Profile (events are attached to profiles and "
                         "usable in audiences). Coverage is sampled from its own "
                         "datasets."),
    (("event",   False), "ExperienceEvent-class schema not enabled for Profile "
                         "(data lake / query only)."),
    (("other",   True),  "Other class (lookup / custom) enabled for Profile -- "
                         "reference data joined to profiles via a relationship."),
    (("other",   False), "Other class (lookup / custom) not enabled for Profile."),
]
PROFILE_TAB_COLOR = TAB_COLOURS[("profile", True)]


# Human labels for schema_kind(), used as the filterable "Kind" column on the
# Schemas and Datasets tabs (same axis as the tab colour hue).
KIND_LABEL = {"profile": "Profile", "event": "Event", "other": "Custom"}


def schema_kind(meta_class: str) -> str:
    """'profile' / 'event' / 'other' -- the HUE axis of the tab colour system."""
    if meta_class == PROFILE_CLASS:
        return "profile"
    if meta_class == EVENT_CLASS:
        return "event"
    return "other"


def tab_colour(k: dict) -> str:
    """Hex tab colour for a kept schema: hue from its class, shade from whether
    any of its datasets is enabled for Unified Profile."""
    return TAB_COLOURS[(schema_kind(k.get("meta_class") or ""),
                        bool(k.get("profile_enabled")))]

# Data dictionary (phase 2): default schema scope when --data-dict is given
# with no value, and the default sample size. Default is "all" -- a bare
# --data-dict samples EVERY kept schema (one coverage/top-values pass per tab).
# Narrow it with --data-dict=<substr> (e.g. --data-dict=profile) for one schema.
DD_DEFAULT_SCOPE = "all"
DD_DEFAULT_ROWS = 1000
# Profile Snapshot Export partitions are large; give each file download longer.
DD_SNAPSHOT_TIMEOUT = 120

# Field history -- "Have we got it?" (v3.4.13, BETA). How many months of
# UNBROKEN data each field has, read from one to three batches per dataset per
# month. See add_field_history.
HISTORY_MONTHS = 24
HISTORY_ROWS = 500             # records read per sample
HISTORY_MIN_ROWS = 50          # a month this thin is looked at again
HISTORY_RESAMPLES = 2          # extra samples for a thin / odd / gappy month
HISTORY_SPARSE = 10            # % -- under this, a one-month hole is a likely miss
HISTORY_WORKERS = 3            # datasets read in parallel
# v3.4.14: how a history read talks to Data Access. ONE file list per batch
# (the reader used to fetch up to 40 before choosing a file -- history needs
# one), and 90 s for it: the first request for a batch's file list was measured
# at 17.8 s on 2026-10-23 and often passed 30 s, while the repeat took 0.6 s.
HISTORY_LISTING = {"max_entries": 1, "meta_timeout": 90, "meta_attempts": 2,
                   "meta_pause": 5}
# Back-off: when this many of the last 20 Data Access calls failed, every
# history worker pauses this long instead of piling up unreadable months.
HISTORY_TRIP_FAILS = 12
HISTORY_PAUSE_SECS = 180
# v3.4.16: a month's batches are asked for 100 at a time (10 hid the readable
# ones: a busy month's newest 10 can all be Profile-side or empty), paging back
# up to 5 pages when the first holds nothing readable, and up to 4 batches are
# tried when files come back empty (each such read takes about 2 s).
HISTORY_PAGE = 100
HISTORY_PAGES = 5
HISTORY_TRIES = 4
# The usual Profile retention for event data: a dataset keeping less is called out.
HISTORY_PROFILE_DAYS = 90
# 2 (v3.4.16): v1 caches judged a month from its newest 10 batches only, so
# their 'none' and unreadable verdicts are not trusted -- every month is re-read.
HISTORY_CACHE_VERSION = 2
# A finished month's batches never change, so each one is cached and a later run
# reads only the current month. Kept beside the repo: a working file, not a
# deliverable, so never under AEP_OUTPUT_DIR.
HISTORY_CACHE_DIR = SCRIPT_DIR / "output" / "field_history_cache"
# Test / perf / temp datasets can make a field look older than the real feed.
_HISTORY_SKIP_RE = re.compile(
    r"(?:^|[\s_-])(?:test|tests|testing|perftest|perf|loadtest|temp|tmp|dummy|"
    r"poc|jotestmode)(?:$|[\s_-])", re.I)
SPARK_NONE, SPARK_EMPTY, SPARK_UNSURE = "░", "▁", "?"     # ░ ▁ ?
SPARK_LEVELS = "▂▃▄▅▆▇█"          # ▂ to █
# Months as a number that reads naturally: 0, <1, then whole months.
HISTORY_MONTHS_FMT = '[=0]"0";[<1]"<1";0'

DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,session"
)

# Schema Registry media types.
XED_LIST = "application/vnd.adobe.xed+json"            # full(ish) resource list
XED_FULL = "application/vnd.adobe.xed-full+json; version=1"  # allOf resolved
# Descriptors come back grouped by @type, each value a list of full objects.
DESCRIPTOR_ACCEPT = "application/vnd.adobe.xdm+json"

# --- Filtering knobs --------------------------------------------------------
ADHOC_CLASS = "https://ns.adobe.com/xdm/data/adhoc"
# Ad-hoc / auto-generated schemas: the canonical adhoc class, OR a per-schema
# generated class whose id tail is a long hex blob (Adobe Campaign / audience
# / import dumps -- the bulk of a tenant), OR an auto-generated title.
ADHOC_CLASS_HEX = re.compile(r"^[0-9a-fA-F]{24,}$")
# Behaviours of a real (non-ad-hoc) custom class. A hex-tailed class whose
# schema extends one of these is a customer lookup / custom class, so KEEP it.
REAL_BEHAVIOURS = ("https://ns.adobe.com/xdm/data/record",
                   "https://ns.adobe.com/xdm/data/time-series")
ADHOC_TITLE_SUBSTRINGS = [
    "xdm schema for dataset",   # " XDM Schema for dataset xxx. Random identifier:"
]
ADHOC_TITLE_PREFIXES = [
    "adhoc xdm schema",
    "schema for audience",      # auto-created audience schemas (hundreds)
]

# Title markers that flag an Adobe Journey Optimizer-managed schema.
# Case-insensitive. Tune these if a real schema gets caught, or an AJO one
# slips through -- every DROP:ajo is printed so you can check.
AJO_TITLE_PREFIXES = ["ao ", "ajo "]
AJO_TITLE_SUBSTRINGS = [
    "ajo",
    "journey optimizer",
    "secondary recipient",
    "aooutput",
]
# extends ($id) substrings -- specific to AJO namespaces so a customer schema
# that merely uses the word "journey" (e.g. "Acme Custom Journey Schema") is
# NOT mis-flagged.
AJO_EXTENDS_SUBSTRINGS = [
    "customerjourneymanagement",
    "/ajo/",
    "messagefeedback",
    "messageexecution",
]

# Adobe-managed "plumbing" schemas (Offer Decisioning, CJA, Audience Portal,
# Journey Orchestration, Unified Profile segment defs, Adobe Campaign
# Recipients) -- real schemas with datasets, but not the customer's own data
# model, so they're dropped before the ERD. Matched on the standard class tail
# OR a title marker.
SYSTEM_CLASS_TAILS = {"decisionevent", "offeritem", "segmentdefinition",
                      "journey"}
SYSTEM_TITLE_SUBSTRINGS = [
    "audience portal",
    "cja audiences",
    "orchestrated campaign",
    "journey inbound",
    "journey orchestration",
    "experience decisioning",
    "decisionevents",
]
SYSTEM_TITLE_EXACT = {"recipients"}

# Test / placeholder schemas. Tunable -- every DROP:test is printed so you can
# check none of your real schemas were caught.
TEST_TITLE_PATTERNS = [
    r"\btest\b",
    r"test profiles",
    r"exclusionstest",
    r"schema for test",
    r"\bsto \d",
    r"\bpoc\b",
    r"^jai ",
]
_TEST_RE = re.compile("|".join(TEST_TITLE_PATTERNS), re.I)

# Plain-English reason for each DROP verdict, on the hidden Dropped Schemas tab.
DROP_REASONS = {
    "DROP:adhoc":      "Auto-generated ad-hoc schema: built on the ad-hoc "
                       "behaviour, or an auto-created title ('Schema for "
                       "audience...', 'XDM Schema for dataset...').",
    "DROP:ajo":        "Managed by Adobe Journey Optimizer (AO / AJO title, or "
                       "an AJO class or field group).",
    "DROP:system":     "Adobe-managed plumbing, not the customer's own model "
                       "(Offer Decisioning, CJA, Audience Portal, Journey "
                       "Orchestration, Campaign recipients).",
    "DROP:test":       "Title looks like a test, POC or placeholder.",
    "DROP:no-dataset": "No dataset uses it, so it holds no data.",
}

KNOWN_CLASSES = {
    "https://ns.adobe.com/xdm/context/profile": "XDM Individual Profile",
    "https://ns.adobe.com/xdm/context/experienceevent": "XDM ExperienceEvent",
    "https://ns.adobe.com/xdm/data/adhoc": "Ad Hoc",
    "https://ns.adobe.com/xdm/data/record": "XDM Record",
    "https://ns.adobe.com/xdm/data/time-series": "XDM Time-series",
    "https://ns.adobe.com/xdm/classes/segment-definition": "Segment definition",
}

# ----------------------------------------------------------------------------
# ANSI / logging - matches the house style (aep_credentials.py et al.)
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
logger = logging.getLogger("data_dictionary_v3")
SSL_CTX = ssl._create_unverified_context()


# ----------------------------------------------------------------------------
# HTTP / IMS / credential helpers  (shared house style)
# ----------------------------------------------------------------------------
# Every request is timed and counted per Adobe endpoint, so a slow or failing
# run can say WHICH endpoint is the problem -- not just that "something timed
# out" -- and the history pass can back off when Data Access is struggling.
_EP_LOCK = threading.Lock()
_EP_STATS = {}                     # endpoint -> {"calls", "outcomes": Counter, "secs": []}
_EP_RECENT = deque(maxlen=20)      # recent Data Access outcomes, for the back-off
_PAUSE_UNTIL = [0.0]


def _endpoint_of(url: str) -> str:
    """Plain-English name of the Adobe endpoint a URL belongs to."""
    if "/export/files/" in url:
        return ("Data Access: file download (GET /export/files/{id}?path=)"
                if "path=" in url else "Data Access: file list (GET /export/files/{id})")
    if "/export/batches/" in url:
        return "Data Access: batch's files (GET /export/batches/{id}/files)"
    if "/catalog/batches" in url:
        return "Catalog: batches (GET /catalog/batches)"
    if "/catalog/dataSets" in url:
        return "Catalog: datasets (GET /catalog/dataSets)"
    if "/schemaregistry/" in url:
        return "Schema Registry"
    if "/ups/" in url:
        return "Profile / Segmentation (/data/core/ups)"
    if "adobelogin.com" in url:
        return "IMS token"
    return "other"


def _record(ep: str, outcome: str, secs: float) -> None:
    with _EP_LOCK:
        s = _EP_STATS.setdefault(ep, {"calls": 0, "outcomes": Counter(), "secs": []})
        s["calls"] += 1
        s["outcomes"][outcome] += 1
        s["secs"].append(secs)
        if ep.startswith("Data Access"):
            _EP_RECENT.append(outcome)


def endpoint_report(title: str) -> None:
    """Log one line per Adobe endpoint used since the last report -- calls,
    failures by kind, and response times -- then start counting afresh."""
    with _EP_LOCK:
        stats = dict(_EP_STATS)
        _EP_STATS.clear()
    if not stats:
        return
    logger.info(f"  {ANSI['bold']}Adobe endpoint health -- {title}{ANSI['reset']}")
    for ep, s in sorted(stats.items(), key=lambda kv: -kv[1]["calls"]):
        secs = sorted(s["secs"])
        bad = {k: v for k, v in s["outcomes"].items() if k != "ok"}
        p95 = secs[min(len(secs) - 1, int(len(secs) * 0.95))]
        logger.info(f"    {ep:<62} {s['calls']:>6,} calls  "
                    f"{s['outcomes'].get('ok', 0):>6,} ok  "
                    f"{', '.join(f'{v} {k}' for k, v in sorted(bad.items())) or 'no failures':<26} "
                    f"median {secs[len(secs) // 2]:5.1f}s  p95 {p95:5.1f}s  max {secs[-1]:5.1f}s")


def http(url, method="GET", headers=None, data=None, timeout=60):
    t0, outcome = time.perf_counter(), "ok"
    try:
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
            return r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        outcome = f"HTTP {e.code}"
        raise
    except (TimeoutError, OSError) as e:
        outcome = ("no answer in time" if "timed out" in str(e).lower()
                   or isinstance(e, TimeoutError) else type(e).__name__)
        raise
    finally:
        _record(_endpoint_of(url), outcome, time.perf_counter() - t0)


def flatten_err(text: str, limit: int = 200) -> str:
    return " ".join((text or "").split())[:limit]


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


def aep_headers(token, conf, sandbox=None, accept="application/json"):
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": conf.get("api_key") or conf["client_id"],
        "x-gw-ims-org-id": conf["org_id"],
        "Accept": accept,
    }
    if sandbox:
        headers["x-sandbox-name"] = sandbox
    return headers


def list_sandboxes(token, conf):
    try:
        body, _ = http(SANDBOX_LIST_URL, headers=aep_headers(token, conf))
        data = json.loads(body)
        return True, data.get("sandboxes") or []
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {flatten_err(e.read().decode(errors='replace'))}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------------------
# AEP reads -- schemas / datasets / descriptors
# ----------------------------------------------------------------------------
def _abs(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return PLATFORM + href


def _next_url(data):
    """Pull the 'next page' URL out of a registry response. _links.next may be
    an object ({'href': ...}) or a bare href string; _page.next is a string
    cursor. Returns an absolute URL or None."""
    if not isinstance(data, dict):
        return None
    nxt = (data.get("_links") or {}).get("next")
    if isinstance(nxt, str) and nxt:
        return _abs(nxt)
    if isinstance(nxt, dict) and nxt.get("href"):
        return _abs(nxt["href"])
    return None


def get_all_schemas(token, conf, sandbox):
    """Page through GET /tenant/schemas (xed+json) following _links.next.
    Returns a list of raw schema-resource dicts."""
    out = []
    url = f"{SCHEMAS_URL}?limit=300"
    headers = aep_headers(token, conf, sandbox, accept=XED_LIST)
    while url:
        body, _ = http(url, headers=headers)
        data = json.loads(body)
        results = data.get("results") if isinstance(data, dict) else data
        out.extend(r for r in (results or []) if isinstance(r, dict))
        url = _next_url(data)
    return out


def get_full_schema(token, conf, sandbox, ref):
    """GET one schema resolved (allOf merged) so it carries a flat 'properties'
    tree. 'ref' is the meta:altId (preferred) or the $id."""
    url = f"{SCHEMAS_URL}/{urllib.parse.quote(ref, safe='')}"
    headers = aep_headers(token, conf, sandbox, accept=XED_FULL)
    body, _ = http(url, headers=headers)
    return json.loads(body)


def _iso_duration_days(v) -> int | None:
    """ISO-8601 duration (P13M, P2Y, P90D, P6W) -> whole days, for sorting.
    Months count as 30 days and years as 365 -- an approximation, but the UI
    shows the same value as '13 months', and what matters here is ordering."""
    if not v or not isinstance(v, str):
        return None
    mt = re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?", v.strip())
    if not mt or not any(mt.groups()):
        return None
    y, mo, w, d = (int(g) if g else 0 for g in mt.groups())
    return y * 365 + mo * 30 + w * 7 + d


def _dataset_metrics(ds: dict) -> dict:
    """Data-hygiene facts Catalog keeps under extensions.*, surfaced so the
    workbook can be sorted by size and retention without opening the UI:
        adobe_lakeHouse.metrics.storageSize / rowCount   -> data lake GB / rows
        adobe_lakeHouse.rowExpiration.ttlValue           -> data lake TTL
        adobe_unifiedProfile.metrics.storageSize         -> profile store GB
        adobe_unifiedProfile.rowExpiration.ttlValue      -> profile TTL
    GB is bytes / 1024^3. Missing metrics come back as None, not 0."""
    ext = ds.get("extensions") or {}
    lake = ext.get("adobe_lakeHouse") or {}
    prof = ext.get("adobe_unifiedProfile") or {}
    lm, pm = lake.get("metrics") or {}, prof.get("metrics") or {}
    def gb(b):
        return round(b / 1024 ** 3, 3) if isinstance(b, (int, float)) else None
    lttl = (lake.get("rowExpiration") or {}).get("ttlValue")
    pttl = (prof.get("rowExpiration") or {}).get("ttlValue")
    return {
        "lake_gb": gb(lm.get("storageSize")),
        "lake_rows": lm.get("rowCount") if isinstance(lm.get("rowCount"), int) else None,
        "lake_ttl": lttl or "",
        "lake_ttl_days": _iso_duration_days(lttl),
        "profile_gb": gb(pm.get("storageSize")),
        "profile_ttl": pttl or "",
        "profile_ttl_days": _iso_duration_days(pttl),
    }


def _pqs_table(ds: dict) -> str:
    """The dataset's AEP Query Service table name (tags['adobe/pqs/table']) -- the
    normalized SYSTEM name you SELECT ... FROM, distinct from the friendly 'name'
    (e.g. "Acme CJA Order Event Dataset" -> "acme_cja_acme_order_event_dataset").
    '' when the dataset hasn't been assigned one yet."""
    v = (ds.get("tags") or {}).get("adobe/pqs/table")
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v) if v else ""


def _profile_status(ds: dict) -> str:
    """Whether a dataset participates in Unified Profile, from tags.unifiedProfile
    -- needed to know which tables to query for Profile:
        'Profile-enabled'           contributes to the merged profile (enabled:true)
        'snapshot (merge policy X)'  a Profile/Segment Snapshot Export (the merged
                                     UNION you query for whole profiles)
        ''                          not profile-related
    """
    tags = ds.get("tags") or {}
    up = tags.get("unifiedProfile")
    flat = []
    if isinstance(up, list):
        flat = [str(x) for x in up]
    elif isinstance(up, dict):
        for v in up.values():
            flat += [str(x) for x in (v if isinstance(v, list) else [v])]
    elif up:
        flat = [str(up)]
    if any(t.startswith("ups_snapshot_type:") for t in flat):
        mp = next((t.split(":", 1)[1] for t in flat
                   if t.startswith("mergePolicyId:")), "")
        return f"snapshot (merge policy {mp})" if mp else "snapshot export"
    if any(t.startswith("enabled:true") for t in flat):
        return "Profile-enabled"
    return ""


def get_all_datasets(token, conf, sandbox):
    """Page through Catalog /dataSets. Returns (counts, datasets):
        counts    {schema_id: dataset_count}   -- the UI 'DATASETS' column
        datasets  [ {name, table, id, schema_id, profile}, ... ]  -- EVERY dataset

    'table' is the Query Service table name (see _pqs_table); 'profile' flags
    Profile enablement (see _profile_status). Together they let correct SQL be
    formed against the right table -- including the Profile snapshot + its feeds."""
    counts, datasets = {}, []
    headers = aep_headers(token, conf, sandbox)
    start, limit = 0, 100
    while True:
        url = (f"{DATASETS_URL}?limit={limit}&start={start}"
               f"&properties=name,description,schemaRef,tags,extensions")
        body, _ = http(url, headers=headers)
        data = json.loads(body)
        if not isinstance(data, dict) or not data:
            break
        for dsid, ds in data.items():
            if not isinstance(ds, dict):
                continue
            ref = (ds.get("schemaRef") or {}).get("id")
            if ref:
                counts[ref] = counts.get(ref, 0) + 1
            datasets.append({
                "name": ds.get("name") or dsid,
                "description": _plain_text(ds.get("description")),
                "table": _pqs_table(ds),
                "id": dsid,
                "schema_id": ref or "",
                "profile": _profile_status(ds),
                **_dataset_metrics(ds),
            })
        if len(data) < limit:
            break
        start += limit
    return counts, datasets


def get_all_descriptors(token, conf, sandbox):
    """GET every descriptor for the sandbox. Returns three dicts keyed by
    (schema_id, lowercased_dot_path):
        identities      -> {"primary": bool, "namespace": str}
        relationships   -> {"dest_id": str, "dest_prop": str, "cardinality": str}
        labels          -> {"title": str, "description": str}
    """
    identities, relationships, labels = {}, {}, {}
    headers = aep_headers(token, conf, sandbox, accept=DESCRIPTOR_ACCEPT)
    body, _ = http(DESCRIPTORS_URL, headers=headers)
    data = json.loads(body)

    # The endpoint returns either a flat list or a dict grouped by @type
    # ({"xdm:descriptorIdentity": [...], "xdm:descriptorOneToOne": [...], ...}),
    # each value a list of full descriptor objects.
    descriptors = []
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                descriptors.extend(x for x in v if isinstance(x, dict))
    elif isinstance(data, list):
        descriptors = [x for x in data if isinstance(x, dict)]

    for d in descriptors:
        dtype = d.get("@type") or ""
        sid = d.get("xdm:sourceSchema") or ""
        if not sid:
            continue

        if dtype == "xdm:descriptorPrimaryKey":
            # sourceProperty is a list of paths; mark each as a primary key.
            raw_props = d.get("xdm:sourceProperty")
            props = raw_props if isinstance(raw_props, list) else [raw_props]
            for p in props:
                prop = _norm_prop(p)
                if not prop:
                    continue
                cur = identities.setdefault((sid, prop),
                                            {"primary": False, "namespace": ""})
                cur["primary"] = True
            continue

        prop = _norm_prop(d.get("xdm:sourceProperty"))
        if not prop:
            continue
        key = (sid, prop)

        if dtype == "xdm:descriptorIdentity":
            cur = identities.setdefault(key, {"primary": False, "namespace": ""})
            cur["primary"] = cur["primary"] or bool(d.get("xdm:isPrimary"))
            cur["namespace"] = (d.get("xdm:namespace")
                                or d.get("xdm:identityNamespace")
                                or cur["namespace"])
        elif dtype in ("xdm:descriptorRelationship", "xdm:descriptorOneToOne"):
            relationships[key] = {
                "dest_id": d.get("xdm:destinationSchema") or "",
                "dest_prop": _norm_prop(d.get("xdm:destinationProperty")),
                "cardinality": d.get("xdm:cardinality") or "1:1",
                "namespace": "",
            }
        elif dtype == "xdm:descriptorReferenceIdentity":
            # A field that references another entity via an identity namespace.
            relationships.setdefault(key, {
                "dest_id": "", "dest_prop": "", "cardinality": "ref",
                "namespace": (d.get("xdm:identityNamespace")
                              or d.get("xdm:namespace") or ""),
            })
        elif dtype == "xdm:alternateDisplayInfo":
            labels[key] = {
                "title": _localized(d.get("xdm:title")),
                "description": _localized(d.get("xdm:description")),
            }
    return identities, relationships, labels


def _norm_prop(p):
    """'/_tenant/person/name/firstName' -> '_tenant.person.name.firstname'
    (leading slash stripped, slashes -> dots, lowercased for matching)."""
    if not isinstance(p, str):
        return ""
    return p.strip("/").replace("/", ".").lower()


def _localized(v):
    """alternateDisplayInfo titles/descriptions can be a plain string or a
    locale map ({'en_us': '...'}). Pull a single readable string out."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for loc in ("en_us", "en_US", "en"):
            if loc in v:
                return _localized(v[loc])
        for val in v.values():
            return _localized(val)
    return str(v)


# ----------------------------------------------------------------------------
# Field flattening
# ----------------------------------------------------------------------------
def _plain_text(v) -> str:
    """A schema title/description as one readable line: strings pass through,
    locale maps pick English, anything else is stringified. Newlines collapse
    so a description never breaks an Excel row."""
    s = _localized(v) if v is not None else ""
    return " ".join(str(s).split()) if s else ""


def flatten_fields(node, prefix="", depth=0, tprefix=""):
    """Walk a resolved xed-full 'properties' tree to leaf dot-notation paths.
    Returns [(path, data_type, title, friendly_path, description, required_bool),
    ...], where 'title' is the field's display name carried in the schema
    itself (blank if absent), 'friendly_path' is the chain of display names
    from the root down to the field (e.g. "Person > Full name > First name" for
    person.name.firstName -- a level with no title falls back to its key), and
    'description' is the field's own description from the schema.
    Array-of-object paths get a '[]' marker (e.g. orders[].id)."""
    rows = []
    if depth > 18:
        return rows
    props = node.get("properties")
    if not isinstance(props, dict):
        return rows
    required = set(node.get("required") or [])
    for key, sub in props.items():
        if not isinstance(sub, dict):
            continue
        path = f"{prefix}.{key}" if prefix else key
        req = key in required
        title = _plain_text(sub.get("title"))
        desc = _plain_text(sub.get("description"))
        fpath = f"{tprefix} > {title or key}" if tprefix else (title or key)
        t = sub.get("type")
        if t == "object" and isinstance(sub.get("properties"), dict):
            rows.extend(flatten_fields(sub, path, depth + 1, fpath))
        elif t == "array":
            items = sub.get("items") or {}
            if (isinstance(items, dict) and items.get("type") == "object"
                    and isinstance(items.get("properties"), dict)):
                rows.extend(flatten_fields(items, path + "[]", depth + 1, fpath))
            else:
                itype = (items.get("meta:xdmType") or items.get("format")
                         or items.get("type") or "any")
                rows.append((path + "[]", f"array<{itype}>", title, fpath,
                             desc, req))
        else:
            disp = sub.get("meta:xdmType") or sub.get("format") or t or "object"
            rows.append((path, disp, title, fpath, desc, req))
    return rows


# ----------------------------------------------------------------------------
# Data dictionary (phase 2): sample real records, tally coverage + top values
#
# AEP has no API that returns a value distribution for a single field, and
# Query Service GROUP BY per field is too slow. Instead we download a sample of
# the schema's actual ingested records (Snappy-Parquet) via the Data Access
# API -- ONE download covers every field at once -- and tally locally.
# ----------------------------------------------------------------------------
# Synthetic / load-test datasets pollute example values and tend to carry huge
# files; skip them when sampling.
_SKIP_DS_RE = re.compile(r"perf[\s_-]?test|load[\s_-]?test", re.I)


def get_dataset_ids_by_schema(token, conf, sandbox):
    """{schema $id: [datasetId, ...]} -- which datasets feed each schema
    (synthetic perf/load-test datasets excluded)."""
    out = {}
    headers = aep_headers(token, conf, sandbox)
    start, limit = 0, 100
    while True:
        url = f"{DATASETS_URL}?limit={limit}&start={start}&properties=name,schemaRef"
        data = json.loads(http(url, headers=headers)[0])
        if not isinstance(data, dict) or not data:
            break
        for dsid, v in data.items():
            if not isinstance(v, dict):
                continue
            if _SKIP_DS_RE.search(v.get("name") or ""):
                continue
            ref = (v.get("schemaRef") or {}).get("id")
            if ref:
                out.setdefault(ref, []).append(dsid)
        if len(data) < limit:
            break
        start += limit
    return out


# ----------------------------------------------------------------------------
# Profile coverage (v3.1): default merge policy -> its snapshot export dataset
# ----------------------------------------------------------------------------
def get_default_merge_policy(token, conf, sandbox):
    """The org's DEFAULT Profile merge policy in this sandbox, or None.

    GET /data/core/ups/config/mergePolicies (cursor-paged at _page.next; results
    live under 'children' or 'mergePolicies'). Returns the policy dict flagged
    default==True, preferring one whose schema.name is _xdm.context.profile."""
    headers = aep_headers(token, conf, sandbox)
    pols, start, pages = [], None, 0
    while pages < 50:
        pages += 1
        params = {"limit": 100}
        if start:
            params["start"] = start
        url = f"{UPS_MERGE_POLICIES_URL}?{urllib.parse.urlencode(params)}"
        data = json.loads(http(url, headers=headers)[0]) or {}
        batch = []
        for key in ("children", "mergePolicies"):
            if isinstance(data.get(key), list):
                batch = data[key]
                break
        pols.extend(p for p in batch if isinstance(p, dict))
        nxt = (data.get("_page") or {}).get("next")
        if not batch or len(batch) < 100 or not nxt:
            break
        start = nxt

    defaults = [p for p in pols if p.get("default") is True]
    if not defaults:
        logger.warning(f"  profile: no merge policy is flagged default in "
                       f"{sandbox} ({len(pols)} policies seen).")
        return None
    # Prefer the one whose schema is the Profile class (a default Identity-map
    # policy can also exist); otherwise the first default.
    for p in defaults:
        if (p.get("schema") or {}).get("name") == "_xdm.context.profile":
            return p
    return defaults[0]


def _ups_snapshot_tags(ds):
    """The dataset's tags.unifiedProfile entries as a flat list of strings.
    Handles the dict-of-lists / flat-list / single-value shapes AEP uses."""
    tags = ds.get("tags") or {}
    up = tags.get("unifiedProfile")
    if up is None:
        return []
    if isinstance(up, list):
        return [str(x) for x in up]
    if isinstance(up, dict):
        out = []
        for v in up.values():
            out.extend(v if isinstance(v, list) else [v])
        return [str(x) for x in out]
    return [str(up)]


def find_profile_snapshot_dataset(token, conf, sandbox, merge_policy_id):
    """(dataset_id, name) of the Profile Snapshot Export dataset for the given
    merge policy, or None. A snapshot export dataset has schemaRef.id ==
    profile__union and a tags.unifiedProfile list carrying a ups_snapshot_type:*
    entry; the owning merge policy id appears among those same tag strings."""
    headers = aep_headers(token, conf, sandbox)
    matches = []
    start, limit = 0, 100
    while True:
        url = (f"{DATASETS_URL}?limit={limit}&start={start}"
               f"&properties=name,schemaRef,tags")
        data = json.loads(http(url, headers=headers)[0])
        if not isinstance(data, dict) or not data:
            break
        for dsid, v in data.items():
            if not isinstance(v, dict):
                continue
            if (v.get("schemaRef") or {}).get("id") != PROFILE_UNION:
                continue
            ups = _ups_snapshot_tags(v)
            if not any(t.startswith("ups_snapshot_type:") for t in ups):
                continue
            if merge_policy_id and not any(merge_policy_id in t for t in ups):
                continue
            matches.append((dsid, v.get("name") or dsid))
        if len(data) < limit:
            break
        start += limit

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(f"  profile: {len(matches)} snapshot dataset(s) match the "
                       f"default merge policy; using {matches[0][1]!r}.")
    return matches[0]


def _batch_record_count(b):
    """Records in a batch from its catalog metrics. -1 when unknown."""
    m = b.get("metrics") or {}
    for v in (m.get("outputRecordCount"), m.get("inputRecordCount"),
              b.get("recordCount"), m.get("recordCount")):
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return -1
    return -1


def _is_consolidation_batch(b):
    """True for a batch AEP's own compaction job produced by merging others.

    Catalog advertises these as ordinary status=success batches carrying the
    combined record count -- so they sort to the top of our richest-first list
    -- but the Data Access API refuses them outright:

        DTAC-4000  "The queried batch is a consolidation batch, which is not
                    supported by Data Access API now."

    Nothing is lost by skipping them: the batches they absorbed are listed in
    their own replay.predecessors and Catalog still serves those individually.
    """
    tags = b.get("tags") or {}
    workflow = tags.get("acp_workflow") or []
    if isinstance(workflow, str):
        workflow = [workflow]
    if any("consolidation" in str(w).lower() for w in workflow):
        return True
    return "compaction" in str(b.get("createdClient") or "").lower()


def _is_profile_side_batch(b):
    """True for the Profile-store half of a batch ingestion.

    Catalog lists it as an ordinary status=success batch of the dataset: an
    old-style 24-hex id, metrics recordsRead / recordsWritten / profileFragments
    and no file counts, tagged adobe/batchIngestion/status/targetSummaries. It
    has no data-lake files -- and Data Access does not say so: the file list
    (GET /export/files/{id}) hangs to the 60 s gateway limit and 504s, every
    time. 328 of 328 such file lists on roi-prod on 2026-10-23, and again the
    next morning, and on retry 30 s and 90 s later. The dataset's data-lake
    batches (ULID ids, file counts in their metrics) read in seconds.
    """
    if "adobe/batchIngestion/status/targetSummaries" in (b.get("tags") or {}):
        return True
    m = b.get("metrics") or {}
    return (("recordsWritten" in m or "profileFragments" in m)
            and not any(k in m for k in ("inputFileCount", "outputFileCount",
                                         "outputRecordCount")))


def _lake_readable(bid, b):
    """Could Data Access serve this batch's files? Not for three kinds Catalog
    lists as ordinary success batches: Adobe's consolidation batches (DTAC-4000),
    the Profile-side half of an ingestion (hangs to a 504), and the identity
    service's '<id>-...-INGEST' markers (createdClient acp_core_identity_data),
    whose file list is DTAC-4043 "DataSetFile not found in Catalog"."""
    return not (_is_consolidation_batch(b) or _is_profile_side_batch(b)
                or str(bid).endswith("-INGEST"))


def _read_parquet_rows(raw_bytes, limit):
    """Parse only up to `limit` rows (row-group streaming) so a million-row
    file isn't fully materialised into memory just to sample a handful."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(pa.BufferReader(raw_bytes))
    out = []
    for batch in pf.iter_batches(batch_size=max(1, min(limit, 1024))):
        out.extend(batch.to_pylist())
        if len(out) >= limit:
            break
    return out[:limit]


def _get_json_retry(url, headers, timeout, attempts=3, pause=8, label=""):
    """GET + parse JSON, retrying on gateway 5xx / read timeouts. The Data Access
    file-manifest endpoint cold-504s on the FIRST access to a huge Profile
    Snapshot Export file (it warms the manifest server-side, exceeding the ~60s
    gateway limit); the next attempt hits the warm cache and returns in ~1s."""
    last = None
    for i in range(attempts):
        try:
            return json.loads(http(url, headers=headers, timeout=timeout)[0])
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (502, 503, 504) or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if i == attempts - 1:
                raise
        why = (f"HTTP {last.code}" if isinstance(last, urllib.error.HTTPError)
               else f"no answer in {timeout}s" if "timed out" in str(last).lower()
               or isinstance(last, TimeoutError) else type(last).__name__)
        logger.info(f"      {ANSI['dim']}{label or _endpoint_of(url)}: {why}; "
                    f"retry {i + 2}/{attempts} in {pause}s{ANSI['reset']}")
        time.sleep(pause)
    raise last


def _list_batch_parquet_files(headers, bid, max_entries=40, meta_timeout=30,
                              meta_attempts=3, meta_pause=8):
    """Enumerate a batch's physical .parquet files as [(fid, name, length)].
    `length` is the file size in bytes (None if AEP omits it). Bounded to
    max_entries dataSetFileId entries so a snapshot with thousands of partitions
    isn't fully walked just to find a small one. meta_timeout is raised for huge
    Profile Snapshot Export batches whose file listings are slow to return."""
    out = []
    # Page the batch-files listing with an explicit limit. The per-file manifest
    # call (/files/{fid}) is left bare: a ?limit on THAT endpoint forces a full
    # enumeration that cold-504s, whereas the bare call returns the first ~100
    # physical files fast. Both are retried to ride out the cold-start 504.
    url = f"{EXPORT_URL}/batches/{bid}/files?limit={max_entries}"
    files = _get_json_retry(url, headers, meta_timeout, attempts=meta_attempts,
                            pause=meta_pause,
                            label=f"Data Access batch's files {bid[:26]}")
    entries = (files.get("data") if isinstance(files, dict) else files) or []
    for fe in entries[:max_entries]:
        fid = fe.get("dataSetFileId")
        if not fid:
            continue
        meta = _get_json_retry(f"{EXPORT_URL}/files/{fid}", headers, meta_timeout,
                               attempts=meta_attempts, pause=meta_pause,
                               label=f"Data Access file list {fid[:30]}")
        for phys in (meta.get("data") if isinstance(meta, dict) else meta) or []:
            name = phys.get("name") or ""
            if not name.endswith(".parquet"):
                continue
            length = phys.get("length")
            try:
                length = int(length) if length is not None else None
            except (TypeError, ValueError):
                length = None
            out.append((fid, name, length))
    return out


def _download_batch_rows(headers, bid, need, smallest_first=False,
                         file_timeout=75, quiet=False, patient=None,
                         listing=None):
    """Download a SUCCESS batch's Parquet file(s) and return (rows, bytes). Logs
    each physical file it streams (name, size, rows parsed).

    smallest_first: for huge datasets (Profile Snapshot Export -- tens of
    millions of rows split into many partitions), download the SMALLEST partition
    files first. http() pulls a whole file into memory before parsing, so a giant
    partition would time out; the smallest one yields the sample cheaply.
    patient: how hard to retry the file listings; defaults to smallest_first.
    quiet: per-file lines go to the debug log (the history pass reads hundreds).
    listing: explicit kwargs for _list_batch_parquet_files (max_entries,
    meta_timeout, meta_attempts, meta_pause), overriding `patient`."""
    rows, total_bytes = [], 0
    log = logger.debug if quiet else logger.info
    patient = smallest_first if patient is None else patient
    # Snapshot listings are large/slow; give the metadata calls the same patience
    # as the file downloads when we're in the smallest-first (snapshot) path.
    # A 504 on the SNAPSHOT manifest is expensive far beyond this one batch: the
    # failure is cached and every remaining Profile-class schema is then skipped
    # as coverage MISSING. Worth being markedly more patient on that path.
    cand = _list_batch_parquet_files(
        headers, bid,
        **(listing or {"meta_timeout": file_timeout if patient else 30,
                       "meta_attempts": 6 if patient else 3,
                       "meta_pause": 20 if patient else 8}))
    if smallest_first:
        # Unknown sizes (length is None) sort last so we prefer a known-small one.
        cand.sort(key=lambda c: (c[2] is None, c[2] if c[2] is not None else 0))
        sizes = [f"{c[2]/1024/1024:.0f}MB" if c[2] else "?" for c in cand[:5]]
        log(f"      {len(cand)} partition file(s) listed; smallest: "
            f"{', '.join(sizes)}")
    for fid, name, length in cand:
        if len(rows) >= need:
            break
        t0 = time.perf_counter()
        # Event data files run to tens of MB; snapshot partitions far more.
        # Dead batches are bounded by the consecutive-failure bail-out upstream.
        raw = http(f"{EXPORT_URL}/files/{fid}?path={urllib.parse.quote(name)}",
                   headers=headers, timeout=file_timeout)[0]
        total_bytes += len(raw)
        parsed = _read_parquet_rows(raw, need - len(rows))
        rows.extend(parsed)
        log(f"      file {name[:46]} "
            f"{ANSI['dim']}{len(raw)/1024:.0f} KB -> "
            f"{len(parsed)} rows in "
            f"{time.perf_counter() - t0:.1f}s{ANSI['reset']}")
    return rows[:need], total_bytes


def sample_schema_rows(token, conf, sandbox, dsids, target, max_batches=12,
                       smallest_first=False, file_timeout=75):
    """Pull up to `target` real records from a schema's datasets. The quota is
    spread ACROSS the datasets (newest SUCCESS batches first within each), so a
    schema fed by several datasets -- each populating different fields -- gets
    representative coverage rather than draining one. Returns nested row dicts.

    smallest_first/file_timeout are forwarded to _download_batch_rows -- set for
    the Profile Snapshot Export path (smallest partition, longer timeout)."""
    headers = aep_headers(token, conf, sandbox)
    if not dsids:
        return [], {"available": 0, "failed": 0, "empty_reads": 0,
                    "list_failed": 0}
    per_ds = max(1, -(-target // len(dsids)))      # ceil(target / n)
    rows, total_bytes, total_batches = [], 0, 0
    available = failed = empty_reads = 0     # diagnostics: empty vs unreadable
    list_failed = 0     # datasets whose batch LIST never returned (504/timeout):
                        # we learned nothing about them, so 0 rows != empty
    t_start = time.perf_counter()
    logger.info(f"    sampling across {len(dsids)} dataset(s), "
                f"~{per_ds} rows each (target {target}); streaming Parquet "
                f"through memory...")
    for di, dsid in enumerate(dsids, 1):
        if len(rows) >= target:
            break
        need = min(per_ds, target - len(rows))
        # status=success is filtered SERVER-side, and it matters far beyond
        # tidiness: on a continuously-landing feed (an Adobe Analytics one, say)
        # asking Catalog to sort every batch it has ever held blows the gateway
        # timeout, and the dataset reads as permanently unavailable. Filtering
        # first turns a reliable 504 into a ~16s response. We still re-check
        # status below, since this only narrows what we have to sort.
        # ...and when even that times out, ask for FEWER batches. Adobe says as
        # much in the 504 body ("This request timed out on database, please
        # optimize your request"): it is the PAGE SIZE that decides whether the
        # query returns, not the sort. Measured on the heaviest datasets --
        # limit=20 dies, limit=5 answers in ~5s, limit=1 always answers. Fewer
        # batches means a smaller sample pool, which is a real cost, but a
        # narrower sample beats no coverage at all. Earlier rungs retry only
        # twice: dropping to a smaller page beats waiting on a page size this
        # dataset has already outgrown.
        base = f"{CATALOG_BATCHES_URL}?dataSet={dsid}&status=success&orderBy=desc:created"
        shapes = [(f"{base}&limit=20", "limit 20", 2),
                  (f"{base}&limit=5", "limit 5", 2),
                  (f"{base}&limit=1", "limit 1", 3)]
        batches, last_err = None, None
        for url, shape, tries in shapes:
            try:
                # Retried like the file manifests: Catalog gateway-504s under
                # load, and a bare failure here used to read as "no batches"
                # -> false EMPTY.
                batches = _get_json_retry(url, headers, 60, attempts=tries,
                                          label=f"batch list {dsid[:24]} ({shape})")
                if shape != shapes[0][1]:
                    logger.info(f"      {ANSI['dim']}{shape} succeeded -- sample "
                                f"pool for this dataset is narrower than "
                                f"usual{ANSI['reset']}")
                break
            except Exception as e:
                last_err = e
                if shape != shapes[-1][1]:
                    logger.info(f"      {ANSI['dim']}batch list {shape} failed "
                                f"({type(e).__name__}); asking for fewer "
                                f"batches{ANSI['reset']}")
        if batches is None:
            list_failed += 1
            logger.warning(f"    dataset {di}/{len(dsids)} {dsid}: "
                           f"batch list failed ({last_err}); skipping -- this "
                           f"dataset's contents are UNKNOWN, not empty.")
            continue
        if not isinstance(batches, dict):
            list_failed += 1        # malformed response: also tells us nothing
            continue
        # Success batches that actually carry records (skip empty/control
        # partitions and still-loading batches), RICHEST first -- one big batch
        # fills the per-dataset quota in a single download instead of churning
        # through dozens of tiny 1-row batches.
        succ_items = [(bid, b) for bid, b in batches.items()
                      if isinstance(b, dict) and b.get("status") == "success"
                      and _batch_record_count(b) != 0
                      and _lake_readable(bid, b)]
        succ_items.sort(key=lambda kv: _batch_record_count(kv[1]), reverse=True)
        succ = [bid for bid, _ in succ_items]
        available += len(succ)
        logger.info(f"    dataset {di}/{len(dsids)} {dsid}: "
                    f"{len(succ)} non-empty success batch(es) available, "
                    f"want {need} rows")
        got, batches_seen, consec_fail = 0, 0, 0
        for bid in succ:
            if got >= need or batches_seen >= max_batches:
                break
            if consec_fail >= 3:
                logger.info(f"      {ANSI['dim']}3 consecutive failures in this "
                            f"dataset -- moving on.{ANSI['reset']}")
                break
            batches_seen += 1
            total_batches += 1
            try:
                chunk, nbytes = _download_batch_rows(
                    headers, bid, need - got,
                    smallest_first=smallest_first, file_timeout=file_timeout)
            except Exception as e:
                consec_fail += 1
                failed += 1
                logger.warning(f"      batch {bid}: skipped ({e})")
                continue
            total_bytes += nbytes
            if not chunk:
                consec_fail += 1
                empty_reads += 1
                continue
            consec_fail = 0
            rows.extend(chunk)
            got += len(chunk)
            logger.info(f"      batch {bid} -> {len(chunk)} rows "
                        f"{ANSI['dim']}(dataset {got}/{need}, "
                        f"overall {len(rows)}/{target}){ANSI['reset']}")
    logger.info(f"    sampled {len(rows)} rows from {total_batches} batch(es), "
                f"{total_bytes/1024/1024:.1f} MB streamed, "
                f"{time.perf_counter() - t_start:.1f}s elapsed.")
    return rows[:target], {"available": available, "failed": failed,
                           "empty_reads": empty_reads,
                           "list_failed": list_failed}


def _extract_values(row, path):
    """All scalar/array leaf values at a dot-notation path (handles nested
    structs and '[]' array markers). Returns a flat list (empty if absent)."""
    cur = [row]
    for seg in path.split("."):
        arr = seg.endswith("[]")
        key = seg[:-2] if arr else seg
        nxt = []
        for c in cur:
            if not isinstance(c, dict):
                continue
            v = c.get(key)
            if v is None:
                continue
            if arr:
                if isinstance(v, list):
                    nxt.extend(x for x in v if x is not None)
            else:
                nxt.append(v)
        cur = nxt
        if not cur:
            break
    return cur


def _fmt_val(v):
    return str(v).replace("|", "/").replace("\n", " ").strip()[:40]


# ----------------------------------------------------------------------------
# PII obfuscation (v3.4.12)
# ----------------------------------------------------------------------------
# Star out two characters of any sampled value that is an email address, or that
# sits in an identifier / contact field (user id, CRM id, phone, ...), WHETHER
# OR NOT the field carries a DULE governance label -- the workbook never
# reproduces a whole real identifier. Light-touch by design (exactly two
# characters, per request): the value's shape and the top-N ranking survive, and
# masking happens before tallying so counts reflect the masked value.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Leaf / path tokens that mark a field as PII even with no DULE label. Matched
# against the lowercased leaf segment (and, for identityMap, the whole path).
_PII_TOKENS = (
    "email", "userid", "user_id", "username", "user_name",
    "customerid", "customer_id", "crmid", "crm_id", "loyaltyid", "loyalty_id",
    "membershipid", "membership_id", "accountid", "account_id",
    "personid", "person_id", "subscriberid", "subscriber_id",
    "ecid", "aaid", "mcid", "guid", "uuid",
    "phone", "mobile", "msisdn", "telephone", "fax",
    "ssn", "nationalid", "passport", "taxid",
)


def _star_two(s, start=1):
    """Replace up to two characters of `s` with '*', starting at index `start`
    (keeping the first character for readability)."""
    if not s:
        return s
    i = min(start, len(s))
    masked = min(2, len(s) - i)
    return s[:i] + "*" * masked + s[i + masked:]


def _mask_pii(s, fpath):
    """Obfuscate PII in a single already-formatted sampled value. Emails lose
    two characters of the local part; identifier / contact fields (matched by
    name) lose two characters. Returns `s` unchanged when it reads as non-PII."""
    if not s:
        return s
    # Email by VALUE -- regardless of field name or any DULE label.
    if _EMAIL_RE.match(s):
        local, at, domain = s.partition("@")
        return _star_two(local, 1) + at + domain
    # Identifier / contact fields by PATH.
    low = fpath.lower()
    leaf = low.rsplit(".", 1)[-1]
    if "identitymap" in low or any(tok in leaf for tok in _PII_TOKENS):
        return _star_two(s, 1)
    return s


def build_data_dict(rows, fields, top_n=10):
    """{field_path: {"coverage": int_pct, "top": "value(count) | ..."}}.
    Coverage = % of sampled rows where the field is populated. Sampled values
    pass through _mask_pii first, so emails and identifier fields are starred."""
    from collections import Counter
    n = len(rows) or 1
    out = {}
    for field in fields:
        fpath = field[0]
        present = 0
        counter = Counter()
        for r in rows:
            vals = _extract_values(r, fpath)
            if vals:
                present += 1
            for v in vals:
                if isinstance(v, (str, int, float, bool)):
                    counter[_mask_pii(_fmt_val(v), fpath)] += 1
        out[fpath] = {
            "coverage": round(100 * present / n),
            "top": " | ".join(f"{val}({cnt})"
                               for val, cnt in counter.most_common(top_n)),
        }
    return out


# ----------------------------------------------------------------------------
# Schema metadata helpers
# ----------------------------------------------------------------------------
def class_name(class_id: str) -> str:
    if not class_id:
        return "?"
    if class_id in KNOWN_CLASSES:
        return KNOWN_CLASSES[class_id]
    tail = class_id.rstrip("/").rsplit("/", 1)[-1]
    return tail.replace("-", " ").replace("_", " ").title()


def fmt_epoch_ms(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M")
    except Exception:
        return ""


def last_modified(raw: dict) -> str:
    meta = raw.get("meta:registryMetadata") or {}
    for k in ("repo:lastModifiedDate", "repo:lastModified", "repo:updatedDate"):
        if meta.get(k):
            return fmt_epoch_ms(meta[k])
    return ""


def extends_refs(raw: dict):
    """All class / field-group ids a schema references -- meta:extends plus any
    allOf $refs present in the unresolved resource."""
    refs = list(raw.get("meta:extends") or [])
    for entry in raw.get("allOf") or []:
        if isinstance(entry, dict) and entry.get("$ref"):
            refs.append(entry["$ref"])
    return refs


def is_adhoc(raw: dict) -> bool:
    cls = raw.get("meta:class") or ""
    if cls == ADHOC_CLASS:
        return True
    # The behaviour the schema extends is the real test: a generated ad-hoc
    # class extends xdm/data/adhoc. A custom class made in the UI (the product,
    # store and promotion lookups) ALSO gets a hex id tail, but extends
    # xdm/data/record or time-series -- that is the customer's own model.
    extends = raw.get("meta:extends") or []
    if ADHOC_CLASS in extends:
        return True
    tail = cls.rstrip("/").rsplit("/", 1)[-1]
    if ADHOC_CLASS_HEX.match(tail) and not any(b in extends for b in REAL_BEHAVIOURS):
        return True                       # generated class, behaviour unknown
    title = (raw.get("title") or "").strip().lower()
    if any(title.startswith(p) for p in ADHOC_TITLE_PREFIXES):
        return True
    return any(s in title for s in ADHOC_TITLE_SUBSTRINGS)


def behaviour(raw: dict) -> str:
    """The XDM behaviour a schema extends: ad-hoc / record / time-series, or ''
    when the listing doesn't say."""
    extends = raw.get("meta:extends") or []
    if raw.get("meta:class") == ADHOC_CLASS or ADHOC_CLASS in extends:
        return "ad-hoc"
    for b in REAL_BEHAVIOURS:
        if b in extends:
            return b.rsplit("/", 1)[-1]
    return ""


def is_ajo(raw: dict) -> bool:
    title = (raw.get("title") or "").strip().lower()
    if any(title.startswith(p) for p in AJO_TITLE_PREFIXES):
        return True
    if any(s in title for s in AJO_TITLE_SUBSTRINGS):
        return True
    refs = " ".join(extends_refs(raw)).lower()
    return any(s in refs for s in AJO_EXTENDS_SUBSTRINGS)


def is_system(raw: dict) -> bool:
    cls = (raw.get("meta:class") or "").rstrip("/").rsplit("/", 1)[-1].lower()
    if cls in SYSTEM_CLASS_TAILS:
        return True
    title = (raw.get("title") or "").strip().lower()
    if title in SYSTEM_TITLE_EXACT:
        return True
    return any(s in title for s in SYSTEM_TITLE_SUBSTRINGS)


def is_test(raw: dict) -> bool:
    return bool(_TEST_RE.search(raw.get("title") or ""))


# ----------------------------------------------------------------------------
# Credential menu + sandbox picker
# ----------------------------------------------------------------------------
def pretty_name(stem: str) -> str:
    return stem.replace("-", " ").replace("_", " ").title()


def client_label(conf: dict) -> str:
    """Human client name for workbook titles, or "" when none is configured.

    ONLY the explicit "client" key in the credential record is used. It used to
    fall back to the credential's own service name, which put the name of a key
    in our own vault at the top of a document that goes out to the client.
    That name identifies whose credential read the sandbox,
    which is our own key-management artefact and nobody else's business; worse,
    it reads like the subject of the report. Where no client name is configured
    the workbook now identifies itself by SANDBOX instead (see write_xlsx),
    which is what the filename has said since v3.3 and is what the reader
    actually needs to know.
    """
    c = (conf.get("client") or "").strip()
    return c.replace("-", " ").replace("_", " ").strip().title()


def short_type(raw: str) -> str:
    t = (raw or "").lower()
    if "prod" in t:
        return "prod"
    if "dev" in t:
        return "dev"
    return raw or "?"


def menu(services):
    """House-style numbered picker over keyring service names. Returns the
    chosen service name string, or None."""
    print()
    bar = ANSI["cyan"] + "=" * 60 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}Credential bank{ANSI['reset']}  "
          f"{ANSI['dim']}(OS keyring vault){ANSI['reset']}")
    print(ANSI["cyan"] + "-" * 60 + ANSI["reset"])
    for i, name in enumerate(services, 1):
        print(f"  {ANSI['bold']}[{i}]{ANSI['reset']} "
              f"{ANSI['yellow']}{pretty_name(name):<20}{ANSI['reset']} "
              f"{ANSI['dim']}{name}{ANSI['reset']}")
    print(bar)
    raw = input(
        f"\nChoose a credential set by number "
        f"({ANSI['cyan']}1{ANSI['reset']}-{ANSI['cyan']}{len(services)}{ANSI['reset']}), "
        "blank to quit: "
    ).strip()
    if not raw or not raw.isdigit():
        return None
    idx = int(raw)
    if 1 <= idx <= len(services):
        return services[idx - 1]
    logger.warning(f"Choice {idx} out of range.")
    return None


def _default_prod(sandboxes):
    """The sandbox to use when the user just hits Enter: one literally named
    'prod', else the first production-type sandbox, else the first sandbox."""
    for sb in sandboxes:
        if sb.get("name") == "prod":
            return sb
    for sb in sandboxes:
        if short_type(sb.get("type", "")) == "prod":
            return sb
    return sandboxes[0] if sandboxes else None


def pick_sandboxes(sandboxes):
    """Prompt for which sandbox(es) to read. Enter = prod (default).
    Accepts numbers ('1', '1,3'), 'all', or Enter for prod."""
    default = _default_prod(sandboxes)
    print()
    bar = ANSI["cyan"] + "=" * 60 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}Sandboxes visible to this credential{ANSI['reset']}")
    print(ANSI["cyan"] + "-" * 60 + ANSI["reset"])
    for i, sb in enumerate(sandboxes, 1):
        name = sb.get("name", "?")
        title = sb.get("title") or name
        env = short_type(sb.get("type", ""))
        star = " (default)" if sb is default else ""
        print(f"  {ANSI['bold']}[{i:>2}]{ANSI['reset']} "
              f"{ANSI['yellow']}{title:<22}{ANSI['reset']} "
              f"{ANSI['dim']}{name:<16}{ANSI['reset']} {env}{star}")
    print(bar)
    dlabel = default.get("name", "?") if default else "?"
    raw = input(
        f"\nPick sandbox(es) "
        f"({ANSI['cyan']}1{ANSI['reset']}, {ANSI['cyan']}1,3{ANSI['reset']}, "
        f"{ANSI['cyan']}all{ANSI['reset']}, or Enter for "
        f"{ANSI['green']}{dlabel}{ANSI['reset']}): "
    ).strip()
    if not raw:
        return [default] if default else []
    if raw.lower() == "all":
        return list(sandboxes)
    chosen = []
    for tok in raw.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(sandboxes):
            sb = sandboxes[int(tok) - 1]
            if sb not in chosen:
                chosen.append(sb)
        else:
            logger.warning(f"Ignoring invalid choice: {tok}")
    return chosen


def resolve_sandboxes_arg(arg: str, sandboxes):
    """Map a --sandbox=... value (names, comma-separated, or 'all') to dicts."""
    if arg.lower() == "all":
        return list(sandboxes)
    wanted = [t for t in arg.replace(",", " ").split() if t]
    by_name = {sb.get("name"): sb for sb in sandboxes}
    out = []
    for w in wanted:
        sb = by_name.get(w)
        if sb and sb not in out:
            out.append(sb)
        elif not sb:
            logger.warning(f"--sandbox: no sandbox named {w!r}; ignoring.")
    return out


# ----------------------------------------------------------------------------
# Per-sandbox collection + filtering
# ----------------------------------------------------------------------------
def collect_sandbox(token, conf, sb):
    """Read one sandbox, filter, and resolve fields for kept schemas.

    Returns dict:
      title, name, env
      verdicts  -> list of (title, class_name, datasets, last_mod, status)
                   status is KEEP / DROP:no-dataset / DROP:adhoc / DROP:ajo
      kept      -> list of kept schema dicts (see build below)
      dropped   -> list of dropped schema dicts (title, status, class,
                   behaviour, datasets, tables, last_mod, id, fields) for the
                   hidden Dropped Schemas / Dropped Field Index tabs; fields
                   is [(path, type, display name)], or None if unreadable
      labels_n  -> count of alternateDisplayInfo (friendly-name) labels in the
                   sandbox -- not DULE governance labels
      stats     -> {total, kept, no_dataset, adhoc, ajo, fields, relationships}
    """
    name = sb.get("name", "?")
    title = sb.get("title") or name
    env = short_type(sb.get("type", ""))

    raws = get_all_schemas(token, conf, name)
    ds_counts, ds_all = get_all_datasets(token, conf, name)
    identities, relationships, labels = get_all_descriptors(token, conf, name)

    # id -> title, for resolving relationship destinations to readable names.
    id_to_title = {r.get("$id"): (r.get("title") or r.get("$id")) for r in raws}
    # id -> Profile / Event / Custom, so the Datasets tab can be filtered by
    # the kind of schema a dataset feeds (the same axis as the tab colours).
    id_to_kind = {r.get("$id"): KIND_LABEL[schema_kind(r.get("meta:class") or "")]
                  for r in raws}

    # SQL table names: every dataset's (friendly name, system table name, id)
    # grouped by the schema it feeds, so each schema knows which table(s) to query.
    tables_by_schema = {}
    for d in ds_all:
        d["schema_title"] = (id_to_title.get(d["schema_id"])
                             or _id_tail(d["schema_id"]) or "(no schema)")
        d["schema_kind"] = id_to_kind.get(d["schema_id"], "")
        if d["schema_id"]:
            tables_by_schema.setdefault(d["schema_id"], []).append(
                (d["name"], d["table"], d["id"]))
    for v in tables_by_schema.values():
        v.sort(key=lambda t: t[0].lower())

    # Which schemas actually feed Unified Profile: at least one of their
    # datasets has tags.unifiedProfile enabled (or is a snapshot export). A
    # Profile-CLASS schema whose datasets are all disabled for Profile never
    # reaches the merged union -- it must not be coloured or sampled as if it did.
    profile_by_schema = {d["schema_id"] for d in ds_all
                         if d["schema_id"] and d.get("profile")}
    # Data-hygiene roll-up per schema across its datasets: sizes SUM, TTLs take
    # the LONGEST retention (the value that actually governs how long any of the
    # schema's data lives). None where no dataset reports the metric.
    metrics_by_schema = {}
    for d in ds_all:
        if not d["schema_id"]:
            continue
        agg = metrics_by_schema.setdefault(d["schema_id"], {
            "lake_gb": None, "lake_rows": None, "lake_ttl_days": None,
            "profile_gb": None, "profile_ttl_days": None, "ttls": set()})
        for key in ("lake_gb", "lake_rows", "profile_gb"):
            if d.get(key) is not None:
                agg[key] = (agg[key] or 0) + d[key]
        for key in ("lake_ttl_days", "profile_ttl_days"):
            if d.get(key) is not None:
                agg[key] = max(agg[key] or 0, d[key])
        for key in ("lake_ttl", "profile_ttl"):
            if d.get(key):
                agg["ttls"].add(d[key])

    verdicts = []
    kept = []
    dropped = []
    stats = {"total": len(raws), "kept": 0, "no_dataset": 0, "adhoc": 0,
             "audience": 0, "ajo": 0, "system": 0, "test": 0, "fields": 0,
             "relationships": 0}

    # Sort by title for a stable, readable listing.
    for raw in sorted(raws, key=lambda r: (r.get("title") or "").lower()):
        sid = raw.get("$id") or ""
        stitle = raw.get("title") or raw.get("meta:altId") or sid or "?"
        cls = class_name(raw.get("meta:class") or "")
        n_ds = ds_counts.get(sid, 0)
        lmod = last_modified(raw)

        if is_adhoc(raw):
            status = "DROP:adhoc"
            stats["adhoc"] += 1
            if stitle.strip().lower().startswith("schema for audience"):
                stats["audience"] += 1
        elif is_ajo(raw):
            status = "DROP:ajo"
            stats["ajo"] += 1
        elif is_system(raw):
            status = "DROP:system"
            stats["system"] += 1
        elif is_test(raw):
            status = "DROP:test"
            stats["test"] += 1
        elif n_ds < 1:
            status = "DROP:no-dataset"
            stats["no_dataset"] += 1
        else:
            status = "KEEP"

        verdicts.append((stitle, cls, n_ds, lmod, status))
        if status != "KEEP":
            # Kept for the hidden Dropped Schemas tab, so a missing schema can
            # be looked up instead of wondered about.
            dropped.append({"title": stitle, "status": status, "class": cls,
                            "behaviour": behaviour(raw), "datasets": n_ds,
                            "tables": tables_by_schema.get(sid, []),
                            "last_mod": lmod, "id": sid,
                            "ref": raw.get("meta:altId") or sid})
            continue

        # --- Resolve the kept schema's fields + descriptor joins -------------
        ref = raw.get("meta:altId") or sid
        try:
            full = get_full_schema(token, conf, name, ref)
        except Exception as e:
            logger.warning(f"  {stitle}: full-schema fetch failed ({e}); "
                           "fields will be empty.")
            full = {}
        fields = flatten_fields(full)

        field_rows = []
        n_identities = n_rels = n_labels = 0
        for path, dtype, ftitle, fpath, desc, req in fields:
            mkey = (sid, path.replace("[]", "").lower())
            ident = identities.get(mkey)
            rel = relationships.get(mkey)
            lab = labels.get(mkey)
            if ident:
                n_identities += 1
                id_disp = ("PRIMARY" if ident["primary"] else "identity")
                if ident["namespace"]:
                    id_disp += f" ({ident['namespace']})"
            else:
                id_disp = ""
            if rel:
                n_rels += 1
                if rel["dest_id"]:
                    dest = id_to_title.get(rel["dest_id"]) or _id_tail(rel["dest_id"])
                    rel_disp = dest + (f".{rel['dest_prop']}" if rel["dest_prop"] else "")
                    if rel["cardinality"]:
                        rel_disp += f"  [{rel['cardinality']}]"
                elif rel.get("namespace"):
                    rel_disp = f"ref-identity ({rel['namespace']})"
                else:
                    rel_disp = "ref-identity"
            else:
                rel_disp = ""
            # Display name lives on the field itself; fall back to an
            # alternateDisplayInfo descriptor only where the field has none.
            friendly = ftitle or (lab["title"] if lab else "")
            if friendly:
                n_labels += 1
            # Same rule for the description: the schema's own text first, an
            # alternateDisplayInfo description only where the field has none.
            desc = desc or (_plain_text(lab["description"]) if lab else "")
            field_rows.append((path, dtype, friendly, fpath, desc,
                               "Y" if req else "", id_disp, rel_disp))

        stats["kept"] += 1
        stats["fields"] += len(field_rows)
        stats["relationships"] += n_rels

        kept.append({
            "title": stitle,
            # The description as typed into the UI's "Schema properties"
            # panel. The listing carries it; the resolved schema is a fallback.
            "description": _plain_text(raw.get("description")
                                       or full.get("description")),
            "class": cls,
            "meta_class": raw.get("meta:class") or "",
            "profile_enabled": sid in profile_by_schema,
            "kind": KIND_LABEL[schema_kind(raw.get("meta:class") or "")],
            **{key: (metrics_by_schema.get(sid) or {}).get(key)
               for key in ("lake_gb", "lake_rows", "lake_ttl_days",
                           "profile_gb", "profile_ttl_days")},
            "datasets": n_ds,
            "tables": tables_by_schema.get(sid, []),
            "last_mod": lmod,
            "id": sid,
            "fields": field_rows,
            "n_fields": len(field_rows),
            "n_identities": n_identities,
            "n_relationships": n_rels,
            "n_labels": n_labels,
        })

    # Inventory of what was left out: the field list of every dropped schema
    # (path, type, display name -- no coverage, no descriptor joins) for the
    # hidden Dropped Field Index tab. About 0.1s a schema; prod's ~2,800 take
    # five or six minutes. A failed fetch is recorded, not fatal.
    if dropped:
        logger.info(f"  {name}: listing the fields of {len(dropped)} dropped "
                    f"schema(s) for the Dropped Field Index (no coverage) ...")
        n_failed = 0
        for i, d in enumerate(dropped, 1):
            try:
                full = get_full_schema(token, conf, name, d["ref"])
                d["fields"] = [(p, t, ft) for p, t, ft, *_ in flatten_fields(full)]
            except Exception as e:
                d["fields"] = None
                n_failed += 1
                logger.warning(f"    {d['title']}: field list fetch failed ({e})")
            if i % 500 == 0:
                logger.info(f"    {i}/{len(dropped)} dropped schemas listed ...")
        n_rows = sum(len(d["fields"] or []) for d in dropped)
        logger.info(f"  {name}: {n_rows:,} fields across {len(dropped)} dropped "
                    f"schema(s)" + (f"; {n_failed} could not be read" if n_failed
                                    else "") + ".")

    return {
        "title": title, "name": name, "env": env,
        "verdicts": verdicts, "kept": kept, "dropped": dropped,
        "datasets": ds_all,
        "labels_n": len(labels), "stats": stats,
    }


def _id_tail(sid: str) -> str:
    return sid.rsplit("/", 1)[-1] if sid else ""


# ----------------------------------------------------------------------------
# Console output
# ----------------------------------------------------------------------------
STATUS_COLOR = {
    "KEEP": ANSI["green"],
    "DROP:no-dataset": ANSI["dim"],
    "DROP:adhoc": ANSI["yellow"],
    "DROP:ajo": ANSI["magenta"],
    "DROP:system": ANSI["blue"],
    "DROP:test": ANSI["cyan"],
}


def print_sandbox(result):
    title = result["title"]
    bar = ANSI["cyan"] + "=" * 78 + ANSI["reset"]
    print()
    print(bar)
    print(f"  {ANSI['bold']}{title}{ANSI['reset']}  "
          f"{ANSI['dim']}({result['name']}, {result['env']}){ANSI['reset']}")
    print(bar)

    header = (f"{'Schema':<44} {'Class':<22} {'DS':>3} "
              f"{'Last Modified':<16} Status")
    print(ANSI["bold"] + header + ANSI["reset"])
    print(ANSI["dim"] + "-" * 100 + ANSI["reset"])
    for stitle, cls, n_ds, lmod, status in result["verdicts"]:
        color = STATUS_COLOR.get(status, "")
        disp_title = stitle if len(stitle) <= 44 else stitle[:41] + "..."
        disp_cls = cls if len(cls) <= 22 else cls[:19] + "..."
        ds_disp = str(n_ds) if n_ds else "-"
        print(f"{disp_title:<44} {ANSI['dim']}{disp_cls:<22}{ANSI['reset']} "
              f"{ds_disp:>3} {lmod:<16} {color}{status}{ANSI['reset']}")

    s = result["stats"]
    print(ANSI["dim"] + "-" * 100 + ANSI["reset"])
    print(f"  {ANSI['bold']}{s['total']}{ANSI['reset']} schemas seen  ->  "
          f"{ANSI['green']}{s['kept']} KEEP{ANSI['reset']}  "
          f"{ANSI['dim']}({s['adhoc']} adhoc, {s['ajo']} ajo, "
          f"{s['system']} system, {s['test']} test, "
          f"{s['no_dataset']} no-dataset dropped){ANSI['reset']}")
    print(f"  {ANSI['dim']}Of the adhoc, {ANSI['reset']}{s['audience']}"
          f"{ANSI['dim']} were auto-created 'Schema for audience...' schemas "
          f"-- filtered out.{ANSI['reset']}")
    friendly = result["labels_n"]
    fcolor = ANSI["green"] if friendly else ANSI["yellow"]
    print(f"  Friendly-name labels (alternateDisplayInfo) in sandbox: "
          f"{fcolor}{friendly}{ANSI['reset']}"
          f"{'' if friendly else '  -- this tenant has none'}")
    print(f"  Kept fields: {s['fields']}   relationships: {s['relationships']}")


# ----------------------------------------------------------------------------
# XLSX output
# ----------------------------------------------------------------------------
# Columns for the Schemas index tab (one row per kept schema).
SCHEMA_CSV_COLUMNS = ["Sandbox", "Schema", "Description", "Class", "Kind",
                      "In Profile",
                      "Datasets", "Data lake GB", "Data lake rows",
                      "Data lake TTL (days)", "Profile GB", "Profile TTL (days)",
                      "SQL table name(s)", "Fields", "Identities",
                      "Relationships", "Friendly-labelled fields", "Last Modified",
                      "Schema $id"]


# Per-schema tab: one field per row (the schema's own metadata lives in the
# tab's title block, so it isn't repeated on every row).
SCHEMA_FIELD_COLUMNS = ["Field (dot notation)", "Data Type", "Friendly Name",
                        "Friendly Path", "Description", "Required", "Identity",
                        "Relationship -> target"]
_HEADER_BG = "1F4E78"


def _safe_sheet_name(name: str, used: set) -> str:
    s = re.sub(r"[\[\]\:\*\?\/\\]", "-", name).strip()[:31] or "sheet"
    base, i = s, 2
    while s.lower() in used:
        suffix = f"-{i}"
        s = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(s.lower())
    return s


def _sheet_label(title: str) -> str:
    """A concise, readable tab name from a schema title: drop the common
    'Acme ' prefix and ' Schema' suffix so the 31-char tab name carries the
    distinctive part. The full title stays in the tab's title block + index."""
    t = (title or "").strip()
    low = t.lower()
    if low.startswith("acme "):
        t = t[len("acme "):]
    if t.lower().endswith(" schema"):
        t = t[:-7]
    return t.strip() or (title or "").strip() or "schema"


def _sql_table_names(k) -> str:
    """Comma-joined Query Service table (system) names for a kept schema's
    datasets -- the FROM targets for SQL, blank if none assigned yet."""
    return ", ".join(sys_nm for _, sys_nm, _ in (k.get("tables") or []) if sys_nm)


def _coverage_status(k):
    """(code, label) describing whether --data-dict coverage is trustworthy for a
    kept schema. So a partial/missing dictionary is never mistaken for gospel:
        ok          sampled fine
        partial     sampled, but some batches failed -- coverage understated
        unreadable  data EXISTS but couldn't be read (504 / 403) -- MISSING
        empty       genuinely no ingested records
        not_sampled out of --data-dict scope, or no dataset to sample
    """
    if k.get("datadict"):
        st = k.get("dd_sample_stats") or {}
        # Only READ failures make coverage untrustworthy. A partition that
        # parses to 0 rows (empty_reads) is normal -- control / empty batches
        # -- and the rows that WERE sampled are still a fair sample.
        if st.get("failed") or st.get("list_failed"):
            lf = st.get("list_failed", 0)
            return ("partial", f"PARTIAL -- {st.get('failed', 0)} batch(es) failed "
                    f"to read"
                    + (f"; {lf} dataset(s) never listed (contents unknown)"
                       if lf else "")
                    + "; coverage is understated")
        return ("ok", "")
    reason = k.get("dd_empty_reason")
    if reason == "unreadable":
        return ("unreadable", "MISSING -- data exists but could not be read "
                "(gateway timeout, or the credential is denied the dataset's "
                "labelled fields -- see the run log); coverage is unknown, not 0%")
    if reason == "empty":
        return ("empty", "empty -- no records ingested into this schema")
    return ("not_sampled", "not sampled (out of scope or no dataset)")


CONFIDENTIAL = "STRICTLY CONFIDENTIAL"
FIELD_INDEX_COLUMNS = ["Field (dot notation)", "Data Type", "Friendly Name",
                       "Friendly Path", "Description", "Identity", "Schema",
                       "SQL table name(s)", "Tab", "Coverage %",
                       "Top values (count)"]



# ----------------------------------------------------------------------------
# Audiences / segments (lifted from journey_audience_census.py)
# ----------------------------------------------------------------------------
# The dictionary describes the data; this describes what the business does WITH
# it. Same sandbox, same run, so schemas -> datasets -> audiences -> the rule
# behind each audience can be read top to bottom in one workbook.
#
# NOTE: this block is a copy of the same code in journey_audience_census.py, by
# request. A fix made here does NOT reach that script, or vice versa.
AUDIENCES_URL = "https://platform.adobe.io/data/core/ups/audiences"
UNIFIED_TAGS_URL = "https://experience.adobe.io/unifiedtags/tags"

SYSTEM_TAG_PREFIXES = ("audience_portal_",)

def fetch_tag_vocabulary(token, conf, sandbox):
    """{tag_id: tag_name} for the whole org, paged from the Unified Tags service.
    One bulk walk instead of a GET per tag id -- the vocabulary is small (~140)
    while the audiences referencing it run to thousands."""
    vocab, cursor, guard = {}, None, 0
    while guard < 100:
        guard += 1
        url = f"{UNIFIED_TAGS_URL}?limit=100"
        if cursor:
            url += f"&start={urllib.parse.quote(str(cursor))}"
        try:
            body, _ = http(url, headers=aep_headers(token, conf, sandbox),
                           timeout=60)
            page = json.loads(body) or {}
        except Exception as e:
            logger.warning(f"  tag vocabulary page {guard} failed "
                           f"({type(e).__name__}: {e}); names may fall back to ids.")
            break
        items = page.get("tags") or []
        if not items:
            break
        for t in items:
            if t.get("id"):
                vocab[t["id"]] = t.get("name") or t["id"][:8]
        cursor = (page.get("_page") or {}).get("next")
        if not cursor:
            break
    return vocab

USER_DIRECTORY_URL = "https://usermanagement.adobe.io/v2/usermanagement/users"

def fetch_user_directory(token, conf):
    """{ims_user_id: email} for the org, paged from User Management.

    Audiences record who created/modified them as an opaque IMS id
    (D82F225C...@805f1e8e...), which tells a reader nothing. The directory is a
    couple of pages for a few thousand users, so it is fetched once per run and
    used to turn those ids into people. Empty dict on failure -- the columns
    then show raw ids rather than the report dying over a nicety.
    """
    users, page = {}, 0
    while page < 50:
        url = f"{USER_DIRECTORY_URL}/{conf['org_id']}/{page}"
        try:
            # Org-level endpoint: no sandbox header (and urllib rejects a None
            # header value, so it is dropped rather than passed empty).
            hdrs = {k: v for k, v in aep_headers(token, conf, "prod").items()
                    if k != "x-sandbox-name"}
            body, _ = http(url, headers=hdrs, timeout=60)
            r = json.loads(body) or {}
        except Exception as e:
            logger.warning(f"  user directory page {page} failed "
                           f"({type(e).__name__}); creator columns will show "
                           f"raw ids.")
            break
        for u in r.get("users") or []:
            if u.get("id"):
                users[u["id"]] = u.get("email") or u.get("username") or ""
        if r.get("lastPage"):
            break
        page += 1
    return users

def resolve_actor(actor_id, directory):
    """An IMS id turned into something a human can act on.

    Three kinds appear on audiences and only the first is a person:
      * a directory user        -> their email
      * @techacct.adobe.com     -> an API integration, not somebody's doing
      * @AdobeID service names  -> Adobe's own automation (halo refreshes etc)
    """
    if not actor_id:
        return ""
    actor_id = str(actor_id)
    if actor_id in directory and directory[actor_id]:
        return directory[actor_id]
    if actor_id.endswith("@techacct.adobe.com"):
        return f"(API integration {actor_id.split('@')[0][:8]})"
    if actor_id.endswith("@AdobeID"):
        return f"(Adobe service: {actor_id.split('@')[0]})"
    return actor_id

def fetch_all_audiences(token, conf, sandbox):
    """Every audience in a sandbox, paged in full. Returns (audiences, complete).

    `complete` is False when the walk ended before totalCount -- the caller must
    say so rather than presenting a short list as the whole estate.
    """
    out, seen, cursor, guard, total = [], set(), None, 0, None
    while guard < 500:
        guard += 1
        url = f"{AUDIENCES_URL}?limit=100"
        if cursor is not None:
            url += f"&start={urllib.parse.quote(str(cursor))}"
        try:
            body, _ = http(url, headers=aep_headers(token, conf, sandbox),
                           timeout=90)
            page = json.loads(body) or {}
        except Exception as e:
            logger.warning(f"  {sandbox}: audience page {guard} failed "
                           f"({type(e).__name__}: {e}); list is INCOMPLETE.")
            return out, False
        kids = page.get("children") or []
        meta = page.get("_page") or {}
        if total is None:
            total = meta.get("totalCount")
        for c in kids:
            if c.get("id") and c["id"] not in seen:
                seen.add(c["id"])
                out.append(c)
        if not kids:
            break
        cursor = meta.get("next")
        if cursor is None:
            break
    complete = total is None or len(out) >= int(total)
    if not complete:
        logger.warning(f"  {sandbox}: collected {len(out)} of {total} audience(s) "
                       f"-- list is INCOMPLETE.")
    return out, complete

# ----------------------------------------------------------------------------
# PQL rendering
# ----------------------------------------------------------------------------
# An audience's rule comes back as 'pql/json': Adobe's syntax TREE, not the PQL
# you see in the UI. There is no supported way to ask for the text form -- every
# format/expressionFormat parameter still returns json, and the conversion
# endpoint is not available to a read-only credential -- so we render it here.
#
# The grammar is large (15 node types, 34 functions, trees up to 17 deep). We
# render the shapes that make up the overwhelming majority -- boolean logic,
# comparisons, field paths, segment references -- and REFUSE to guess at the
# rest. Event-sequence and time-window nodes emit a visible <...> marker and set
# the "partial" flag, so a half-rendered rule can never be mistaken for a whole
# one. The raw tree is always kept in its own column.
PQL_INFIX = {"and": "and", "or": "or", "=": "=", ">": ">", "<": "<",
             ">=": ">=", "<=": "<=", "!=": "!=", "equals": "=",
             "notEqualTo": "!="}

PQL_WORDY = {"startsWith": "starts with", "endsWith": "ends with",
             "contains": "contains", "doesNotContain": "does not contain",
             "in": "in", "notIn": "not in"}

# Nodes describing event sequences / time windows. Rendering these faithfully is
# a project in itself; misrendering one would quietly change what the rule means.
PQL_COMPLEX = {"chain", "occurs", "timeQualification", "duration", "select",
               "varDecl", "element", "gap", "range"}

XL_CELL_LIMIT = 32000          # Excel's hard ceiling is 32767
# A cell that had to be cut ends with this, so it can never pass for a whole
# rule. The full text is in the sidecar (see write_pql_sidecar).
PQL_TRUNCATED_MARK = "…[TRUNCATED – see sidecar]"


def fit_cell(text: str) -> tuple[str, bool]:
    """(cell_text, truncated). Cuts to the Excel limit with the marker on the
    end; leaves shorter text untouched."""
    if len(text) <= XL_CELL_LIMIT:
        return text, False
    return text[:XL_CELL_LIMIT - len(PQL_TRUNCATED_MARK)] + PQL_TRUNCATED_MARK, True


def _pql_literal(node):
    v = node.get("value")
    lt = node.get("literalType")
    if lt == "List" and isinstance(v, list):
        return "[" + ", ".join(_pql_literal({"value": x, "literalType": "String"}
                                            if isinstance(x, str) else {"value": x})
                               for x in v) + "]"
    if isinstance(v, str) and lt in ("String", "Timestamp", "TimeDirection",
                                     "TimeUnit", "Comparison", None):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)

def _pql_path(node, state):
    """Dotted field path. The base is a parameter/var reference and contributes
    nothing readable, so it is dropped: v0._experience.x -> _experience.x"""
    parts = []
    cur = node
    while isinstance(cur, dict) and cur.get("nodeType") == "fieldLookup":
        parts.append(cur.get("fieldName") or "?")
        cur = cur.get("object")
    if isinstance(cur, dict) and cur.get("nodeType") not in (
            "parameterReference", "varRef", None):
        head = _render_node(cur, state)
        if head:
            parts.append(head)
    return ".".join(reversed(parts))

def _render_node(node, state):
    if node is None:
        return ""
    if isinstance(node, list):
        return ", ".join(_render_node(n, state) for n in node)
    if not isinstance(node, dict):
        return str(node)

    nt = node.get("nodeType")
    if nt in PQL_COMPLEX:
        state["partial"] = True
        state["complex"].add(nt)
        return f"<{nt}: see raw>"
    if nt == "literal":
        return _pql_literal(node)
    if nt == "fieldLookup":
        return _pql_path(node, state)
    if nt in ("parameterReference", "varRef"):
        return ""                       # the implicit profile/event being tested
    if nt == "lambda":
        return _render_node(node.get("body"), state)
    if nt != "fnApply":
        state["partial"] = True
        state["complex"].add(nt or "unknown")
        return f"<{nt or 'unknown'}: see raw>"

    fn = node.get("fnName")
    params = node.get("params") or []

    if fn == "inSegment" and params:
        sid = (params[0] or {}).get("value")
        name = state["segments"].get(sid)
        return f'inSegment("{name}")' if name else f'inSegment({sid})'
    if fn == "not" and len(params) == 1:
        return f"not ({_render_node(params[0], state)})"
    if fn in ("exists", "isNotNull") and params:
        return f"{_render_node(params[0], state)} exists"
    if fn == "isNull" and params:
        return f"{_render_node(params[0], state)} is null"
    # stringCompare NAMES its comparison in the first parameter -- it is
    # stringCompare(<op>, <field>, <value>), not an infix pair. Treating it as
    # infix renders the operator itself as the left-hand operand.
    if fn == "stringCompare" and len(params) >= 3:
        op = (params[0] or {}).get("value") or "compares"
        return (f"{_render_node(params[1], state)} {op} "
                f"{_render_node(params[2], state)}")
    if fn == "get" and len(params) >= 2:
        base = _render_node(params[0], state)
        fld = (params[1] or {}).get("value") or _render_node(params[1], state)
        return f"{base}.{fld}" if base else str(fld)
    if fn in PQL_INFIX and len(params) >= 2:
        op = PQL_INFIX[fn]
        # equals/stringCompare carry a trailing case-sensitivity flag
        operands = params[:2] if fn in ("equals", "stringCompare") else params
        rendered = [_render_node(p, state) for p in operands]
        joined = f" {op} ".join(r for r in rendered if r != "")
        return f"({joined})" if op in ("and", "or") and len(rendered) > 1 else joined
    if fn in PQL_WORDY and len(params) >= 2:
        return (f"{_render_node(params[0], state)} {PQL_WORDY[fn]} "
                f"{_render_node(params[1], state)}")
    args = ", ".join(r for r in (_render_node(p, state) for p in params) if r != "")
    return f"{fn}({args})"

def render_pql(expression, segments):
    """(readable_text, raw_text, partial, format) for an audience's expression.

    partial=True means at least one node could not be rendered faithfully and
    appears as a <marker>; the row is labelled so nobody reads it as complete.
    format is 'json' (Adobe's pql/json syntax tree) or 'pql-text' (the rule as
    PQL text -- complete, but not parseable as JSON), '' when there is no rule.
    """
    if not expression:
        return "", "", False, ""
    raw = expression.get("value")
    raw_text = raw if isinstance(raw, str) else json.dumps(raw)
    if expression.get("format") == "pql/text":
        return str(raw_text), str(raw_text), False, "pql-text"
    try:
        tree = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
    except (ValueError, TypeError):
        # Declared json but isn't: PQL text under the wrong label. Keep it as
        # the readable form too, since that is what it is.
        return str(raw_text), str(raw_text), False, "pql-text"
    state = {"segments": segments, "partial": False, "complex": set()}
    text = _render_node(tree, state)
    if state["partial"] and state["complex"]:
        text = f"[PARTIAL: {', '.join(sorted(state['complex']))}] {text}"
    return text, str(raw_text), state["partial"], "json"

def audience_eval_type(aud: dict) -> str:
    """Streaming / Edge / Batch from the evaluationInfo carried on the LIST
    payload (no per-audience GET needed). '' when the record omits it."""
    ei = aud.get("evaluationInfo") or {}
    if (ei.get("continuous") or {}).get("enabled"):
        return "Streaming"
    if (ei.get("synchronous") or {}).get("enabled"):
        return "Edge"
    if (ei.get("batch") or {}).get("enabled"):
        return "Batch"
    return ""

def split_audience_tags(aud: dict, vocab: dict):
    """(named_tags, system_tags, unresolved_ids) for one audience.

    named  -- resolved against the Unified Tags vocabulary: the human tagging
              this report is actually about.
    system -- machine-written key:value stamps, kept out of the counts.
    unresolved -- UUIDs with no vocabulary entry (deleted tag, or a tag the
              credential cannot see). Reported rather than silently dropped.
    """
    named, system, unresolved = [], [], []
    for t in (aud.get("tags") or []):
        t = str(t)
        if ":" in t or t.startswith(SYSTEM_TAG_PREFIXES):
            system.append(t)
        elif t in vocab:
            named.append(vocab[t])
        else:
            unresolved.append(t)
    return sorted(set(named)), system, unresolved


def attach_audiences(token, conf, res, caches):
    """Read the sandbox's audiences onto res['audiences'].

    Never fatal: the dictionary's job is schemas and fields, so a failure here
    costs the Audiences tab and nothing else. res['audiences_complete'] records
    whether the listing finished, so a short list is never presented as the
    whole estate.
    """
    sandbox = res["name"]
    res["audiences"], res["audiences_complete"] = [], True
    try:
        if caches.get("vocab") is None:
            caches["vocab"] = fetch_tag_vocabulary(token, conf, sandbox)
            logger.info(f"  tag vocabulary: {len(caches['vocab'])} tag(s).")
        if caches.get("directory") is None:
            caches["directory"] = fetch_user_directory(token, conf)
            logger.info(f"  user directory: {len(caches['directory'])} user(s).")
        auds, complete = fetch_all_audiences(token, conf, sandbox)
    except Exception as e:
        logger.warning(f"  {sandbox}: audiences unavailable "
                       f"({type(e).__name__}: {e}); the Audiences tab will be "
                       f"empty for this sandbox.")
        res["audiences_complete"] = False
        return

    vocab = caches["vocab"] or {}
    directory = caches["directory"] or {}
    # inSegment() names another audience by id; resolve so a combined rule reads
    # as prose. Sandbox-scoped, so built from this sandbox's own listing.
    seg_names = {}
    for a in auds:
        for key in ("id", "audienceId"):
            if a.get(key):
                seg_names[a[key]] = a.get("name") or ""

    # Complexity (beta): an audience built on inSegment() of a big base
    # audience has to scan every one of those profiles, so the size of each
    # base audience is read once (its latest evaluation count) and remembered.
    base_pops = fetch_base_populations(token, conf, sandbox, auds, caches)

    rows, full, n_pql, n_partial, n_trunc, tagged = [], [], 0, 0, 0, 0
    rag_tally = {"RED": 0, "AMBER": 0, "GREEN": 0}
    for a in auds:
        named, system, unresolved = split_audience_tags(a, vocab)
        tagged += bool(named)
        pql, pql_raw, partial, fmt = render_pql(a.get("expression"), seg_names)
        tree = None
        if fmt == "json":
            try:
                tree = json.loads(pql_raw)
            except (ValueError, TypeError):
                tree = None
        cx = audience_complexity.score_audience(a, tree, base_pops, seg_names)
        cf = cx["features"]
        if cx["rag"]:
            rag_tally[cx["rag"]] += 1
        if pql:
            n_pql += 1
            n_partial += bool(partial)
        # The sheet gets what fits in a cell; the sidecar gets everything.
        pql_cell, cut_readable = fit_cell(pql)
        raw_cell, cut_raw = fit_cell(pql_raw)
        truncated = cut_readable or cut_raw
        n_trunc += truncated
        full.append({"audience_id": a.get("id") or "",
                     "audience_name": a.get("name") or "",
                     "format": fmt, "raw_pql": pql_raw, "readable_pql": pql})
        rows.append([
            sandbox, a.get("name") or "", _plain_text(a.get("description")),
            a.get("id") or "",
            audience_eval_type(a), a.get("lifecycleState") or "",
            ", ".join(named), len(named), a.get("namespace") or "",
            resolve_actor(a.get("createdBy"), directory),
            resolve_actor(a.get("lastModifiedBy"), directory),
            pql_cell,
            "partial" if partial else ("yes" if pql else ""),
            raw_cell,
            len(pql_raw), "yes" if truncated else "no", fmt,
            ", ".join(t[:8] for t in unresolved), len(system),
            cx["score"], cx["rag"], cx["why"],
            cf["event_scan"], cf["lookback_days"], cf["lookup_joins"],
            cf["lookup_fields"], cf["aggregations"], cf["depends_on"],
            cf["largest_base_name"], cf["largest_base"], cf["sequence_steps"],
            cf["conditions"], cf["max_list"], cf["merge_policies"],
            cf["dependents"],
        ])
    res["audiences"] = rows
    res["audiences_pql"] = full            # one entry per row, same order
    res["complexity_tally"] = rag_tally
    res["audiences_complete"] = complete
    logger.info(f"  audiences: {len(rows)} ({tagged} tagged, {n_pql} with a "
                f"rule, {n_partial} of those only partly renderable, "
                f"{n_trunc} too long for a cell -- full text in the sidecar).")


def fetch_base_populations(token, conf, sandbox, auds, caches) -> dict:
    """{audience id: profiles} for every audience some other audience depends
    on (inSegment). The list payload carries no counts, so each base audience
    is read once from /segment/definitions/<id> (metrics.data.totalProfiles).
    A few hundred small GETs; cached per sandbox for the run. Never fatal."""
    deps = sorted({str(d) for a in auds for d in (a.get("dependencies") or [])})
    if not deps:
        return {}
    store = caches.setdefault("base_pops", {}).setdefault(sandbox, {})
    todo = [d for d in deps if d not in store]
    if todo:
        logger.info(f"  complexity: reading the size of {len(todo)} base "
                    f"audience(s) that others depend on...")
        headers = aep_headers(token, conf, sandbox)
        for sid in todo:
            try:
                body, _ = http(f"{UPS_SEGMENT_DEFS_URL}/"
                               f"{urllib.parse.quote(sid, safe='')}",
                               headers=headers, timeout=30)
                det = json.loads(body) or {}
                store[sid] = ((det.get("metrics") or {}).get("data") or {}).get("totalProfiles")
            except Exception as e:
                logger.debug(f"  base audience {sid}: {type(e).__name__}: {e}")
                store[sid] = None
    return store


def write_pql_sidecar(res, out_dir: Path, datestr: str) -> Path | None:
    """Every audience's FULL rule, untruncated, one JSON object per line, next
    to the workbook: audiences_pql_<sandbox>_<yyyymmdd>.jsonl. Excel cuts a
    cell at 32,767 characters and a hard-coded store list can pass that, so
    the sheet cannot be the audit copy -- this is. The PQL is written exactly
    as Adobe returned it (json.dumps escapes only what JSONL needs)."""
    entries = res.get("audiences_pql") or []
    if not entries:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"audiences_pql_{res['name']}_{datestr.replace('-', '')}.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


def script_provenance() -> dict:
    """Where this workbook came from, for the "who produced this?" conversation.

    The script's own SCRIPT_DATE is hand-maintained and goes stale the moment
    someone edits without touching it, so the last-changed date is taken from
    git -- the commit that last touched THIS file -- and only falls back to the
    file's mtime when git can't answer (no repo, git absent, exported copy).

    `dirty` matters: if the working tree has uncommitted edits then the commit
    id does NOT describe the code that produced the file, and the workbook says
    so rather than implying a provenance it cannot support.
    """
    import subprocess

    me = Path(__file__).resolve()
    prov = {"script": me.name, "version": SCRIPT_VERSION,
            "generated": f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
            "changed": "", "commit": "", "dirty": False, "source": "file mtime"}

    def git(*args):
        return subprocess.run(("git", "-C", str(me.parent)) + args,
                              capture_output=True, text=True, timeout=15)

    try:
        r = git("log", "-1", "--format=%cI%x00%h", "--", str(me))
        if r.returncode == 0 and r.stdout.strip():
            iso, _, short = r.stdout.strip().partition("\x00")
            prov["changed"] = iso.strip()[:16].replace("T", " ")
            prov["commit"] = short.strip()
            prov["source"] = "git"
        d = git("status", "--porcelain", "--", str(me))
        prov["dirty"] = bool(d.returncode == 0 and d.stdout.strip())
    except Exception:
        pass                      # any git trouble -> fall through to mtime

    if not prov["changed"]:
        mtime = datetime.fromtimestamp(me.stat().st_mtime, timezone.utc)
        prov["changed"] = f"{mtime:%Y-%m-%d %H:%M UTC}"
    return prov


def provenance_line(prov: dict) -> str:
    """One line naming the script, when it ran, and when it last changed."""
    bits = [f"Generated {prov['generated']}",
            f"by {prov['script']} v{prov['version']}"]
    if prov["commit"]:
        bits.append(f"commit {prov['commit']}")
    bits.append(f"script last changed {prov['changed']}"
                + ("" if prov["source"] == "git" else " (file timestamp)"))
    bits.append(f"release notes: {RELEASE_NOTES_URL}")
    line = "  |  ".join(bits)
    if prov["dirty"]:
        line += ("  |  WARNING: the script had UNCOMMITTED changes when this ran "
                 "-- the commit id above does not describe the code that "
                 "produced this file.")
    return line


def _archive_previous(out_dir: Path, safe_client: str,
                      kind: str = "dictionary") -> int:
    """Move any prior dictionary for this client (or, kind='sidecar', any prior
    audiences_pql sidecar for this sandbox) into out_dir/archive/ so the output
    folder only ever holds the newest. Returns how many were moved."""
    pattern = (f"audiences_pql_{safe_client}_*.jsonl" if kind == "sidecar"
               else f"Data Dictionary - {safe_client} - *.xlsx")
    prev = sorted(out_dir.glob(pattern))
    if not prev:
        return 0
    arch = out_dir / "archive"
    arch.mkdir(exist_ok=True)
    moved = 0
    for p in prev:
        dest = arch / p.name
        i = 2
        while dest.exists():               # never clobber an archived copy
            dest = arch / f"{p.stem} ({i}){p.suffix}"
            i += 1
        try:
            p.rename(dest)
        except OSError as e:
            # Almost always the previous workbook still open in Excel. Tidying
            # is a courtesy; it must never destroy the run that just spent
            # forty minutes sampling data.
            logger.warning(f"Could not archive {p.name} ({e.__class__.__name__})"
                           f" -- it is probably open. Leaving it where it is.")
            continue
        moved += 1
    return moved


def write_xlsx(results, client: str, datestr: str, snapshots=None, overlaps=None):
    """`snapshots` is (rows, unreadable, sandboxes) from snapshot_tables, or None
    to leave the Snapshot tabs out. `overlaps` is {sandbox: overlap_report.collect()
    result} for the Overlap tabs, or None."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        logger.warning("openpyxl not installed -- skipping XLSX "
                       "(pip install -r requirements.txt).")
        return None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # The filename says WHAT this is, WHERE it came from and WHEN -- not which
    # credential happened to read it. The credential label is an artefact of our
    # own key management and means nothing to whoever opens the file.
    sbs = [str(res.get("name") or "") for res in results if res.get("name")]
    where = "-".join(sbs) if 0 < len(sbs) <= 2 else f"{len(sbs)}-sandboxes"
    safe_client = re.sub(r"[^0-9A-Za-z _-]+", "", where).strip() or "sandbox"
    path = OUTPUT_DIR / f"Data Dictionary - {safe_client} - {datestr}.xlsx"

    # ...and the titles INSIDE the workbook say the same thing as the filename.
    # `client` is filled only when the credential record carries an explicit
    # client name; otherwise we identify the workbook by sandbox rather than by
    # whichever key happened to read it.
    label = client or where

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=_HEADER_BG)
    title_font = Font(bold=True, size=14)
    conf_font = Font(bold=True, size=11, color="C00000")
    center = Alignment(horizontal="center")
    any_dd = any(k.get("datadict") for res in results for k in res["kept"])
    # Field history (BETA): three columns on the end of the Field Index and the
    # schema tabs, plus the Field History tab -- only when --history ran.
    any_hist = any(k.get("history") for res in results for k in res["kept"])
    hist_labels = next((k["history_labels"] for res in results for k in res["kept"]
                        if k.get("history_labels")), [])
    hist_cols = (["Have we got it? (months, BETA)", "History detail (BETA)",
                  "In Profile now (days, BETA)", "Profile detail (BETA)",
                  f"History {hist_labels[0]} > {hist_labels[-1]} (BETA)"]
                 if hist_labels else [])
    hist_widths = [13, 46, 12, 46, len(hist_labels) + 4]
    spark_font = Font(name="Consolas")

    def put_history(ws, row, col, h):
        """Write one field's five history cells from column `col`."""
        if not h:
            return
        c = ws.cell(row, col, h["months"])
        c.number_format = HISTORY_MONTHS_FMT
        c.alignment = center
        ws.cell(row, col + 1, h["detail"])
        ws.cell(row, col + 2, h.get("profile_days")).alignment = center
        ws.cell(row, col + 3, h.get("profile_detail"))
        ws.cell(row, col + 4, h["spark"]).font = spark_font

    def style_header(ws, ncols, row=1):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=row, column=c)
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[row].height = 28
        ws.freeze_panes = ws.cell(row=row + 1, column=1)
        # Remember where the table starts so filters can be applied once every
        # row has been written -- the range needs the LAST row, which isn't
        # known yet here. Applied in one sweep just before saving, so no sheet
        # can be forgotten.
        ws._dd_table = (row, ncols)

    def autofit(ws, widths):
        for idx, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = w

    def confidential(ws, cell="A1"):
        # Visible banner + a print header on every page.
        ws[cell] = CONFIDENTIAL
        ws[cell].font = conf_font
        ws.oddHeader.center.text = f'&"-,Bold"&12&KC00000{CONFIDENTIAL}'
        ws.evenHeader.center.text = ws.oddHeader.center.text

    wb = Workbook()
    # House rule: the file's author is Barry, not the library ('openpyxl').
    wb.properties.creator = wb.properties.lastModifiedBy = SCRIPT_AUTHOR

    # Unique worksheet name per kept schema (Excel: <=31 chars, unique). Built
    # up front so the Field Index and Schemas index can name each schema's tab.
    used = {"summary", "how to use", "schemas", "field index", "datasets",
            "audiences", "snapshot summary", "snapshot tables",
            "overlap cut-list", "overlap full report", "dropped schemas",
            "dropped field index", "field history"}
    tabbed = []  # (res, k, sheet_name)
    for res in results:
        for k in res["kept"]:
            tabbed.append((res, k, _safe_sheet_name(_sheet_label(k["title"]),
                                                    used)))

    # ---- Summary tab --------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    confidential(ws)
    ws["A2"] = f"Data Dictionary v{SCRIPT_VERSION}  -  {label}"
    ws["A2"].font = title_font
    prov = script_provenance()
    ws["A3"] = provenance_line(prov)
    ws["A3"].font = Font(italic=True,
                         color="C00000" if prov["dirty"] else "666666")
    ws["A4"] = (
        "WHAT THIS IS: a top-to-bottom picture of this Adobe Experience Platform "
        "sandbox, read straight from the platform -- the SCHEMAS that define the "
        "data, the DATASETS that hold it (and the SQL table name to query each "
        "one), and the AUDIENCES built on top of it with the rule behind each. "
        "Kept = a schema referenced by at least one dataset and not ad-hoc, AJO, "
        "system or test. See the How to Use tab if you're not sure where to "
        "start.")
    ws["A4"].font = Font(italic=True, color="666666")
    ws["A4"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[4].height = 46

    # Tab colour key: one swatch + meaning per colour, so readers know what
    # each schema tab's colour signifies without having to ask. Two axes:
    # hue = class, shade = whether the data reaches Unified Profile.
    key_font = Font(italic=True, color="666666")
    ws["A5"] = "Tab colour key:"
    ws["A5"].font = Font(italic=True, bold=True, color="666666")
    ws["C5"] = ("Every schema tab is coloured. HUE = the schema's class "
                "(purple = XDM Individual Profile record, blue = "
                "ExperienceEvent, green = any other / lookup class). SHADE = "
                "whether its data reaches Unified Profile (dark = at least one "
                "of its datasets is enabled for Profile, light = none is). "
                "'Enabled for Profile' is read from each dataset's "
                "tags.unifiedProfile -- see the Profile column on the Datasets "
                "tab.")
    ws["C5"].font = key_font
    r = 6
    for (kind, enabled), meaning in TAB_COLOUR_KEY:
        ws.cell(r, 2).fill = PatternFill("solid",
                                         fgColor=TAB_COLOURS[(kind, enabled)])
        ws.cell(r, 3, meaning).font = key_font
        r += 1

    hdr = ["Sandbox", "Env", "Schemas seen", "Kept", "Dropped: no-dataset",
           "Dropped: adhoc", "Dropped: AJO", "Dropped: system",
           "Dropped: test", "Kept fields", "Relationships", "Friendly labels"]
    r += 1
    for c, nm in enumerate(hdr, 1):
        ws.cell(r, c, nm)
    style_header(ws, len(hdr), row=r)
    r += 1
    for res in results:
        s = res["stats"]
        ws.cell(r, 1, res["title"])
        ws.cell(r, 2, res["env"]).alignment = center
        for c, val in enumerate([s["total"], s["kept"], s["no_dataset"],
                                 s["adhoc"], s["ajo"], s["system"], s["test"],
                                 s["fields"], s["relationships"],
                                 res["labels_n"]], 3):
            ws.cell(r, c, val).alignment = center
        r += 1
    autofit(ws, [24, 6, 13, 7, 19, 15, 13, 15, 13, 12, 14, 12])
    ws.cell(r, 1, "Every dropped schema -- why it was left out, and its SQL "
            "table name(s) -- is on the hidden 'Dropped Schemas' tab, and "
            "every one of their fields on the hidden 'Dropped Field Index' "
            "(right-click any tab > Unhide).").font = Font(italic=True,
                                                          color="666666")
    r += 1
    if any_hist:
        ws.cell(r, 1, f"Field history (BETA): how many months of unbroken data "
                f"each field has, read from batches back to {_mon(hist_labels[0])} "
                f"-- the 'Have we got it?' columns on the Field Index and every "
                f"schema tab, and month by month on the Field History tab. Test, "
                f"perf and temp datasets are left out.").font = Font(
                    italic=True, color="6E3FA3")
        r += 1
        # What an audience can look back over: Profile keeps event data only as
        # long as each dataset's Profile retention.
        keeps = sorted((d["profile_ttl_days"], res["title"], d.get("table") or d.get("name"),
                        _ttl_words(d.get("lake_ttl")))
                       for res in results for d in res.get("datasets") or []
                       if str(d.get("profile") or "").startswith("Profile")
                       and d.get("profile_ttl_days"))
        if keeps:
            r += 1
            ws.cell(r, 1, f"PROFILE RETENTION -- an audience can look back only as far "
                    f"as Profile keeps a dataset's events (red: under "
                    f"{HISTORY_PROFILE_DAYS} days). Profile-enabled datasets not "
                    f"listed keep their data until it is replaced.").font = Font(
                        bold=True, color="6E3FA3")
            r += 1
            for days, sb, name, lake in keeps:
                ws.cell(r, 1, f"{name}{f'  ({sb})' if len(results) > 1 else ''}:  "
                        f"Profile keeps {days} days  (data lake: {lake})").font = Font(
                            color="C00000" if days < HISTORY_PROFILE_DAYS else "444444",
                            bold=days < HISTORY_PROFILE_DAYS)
                r += 1

    # ---- Data completeness: what's MISSING ----------------------------------
    # On a --data-dict run, list every schema whose coverage is not trustworthy
    # (missing / partial / empty) so the dictionary is never taken as gospel.
    if any_dd:
        r += 2
        ws.cell(r, 1, "DATA COMPLETENESS -- coverage is NOT complete; "
                "the schemas below are missing or partial:").font = Font(
                    bold=True, color="C00000")
        r += 1
        gap_hdr = ["Schema", "Tab", "Sandbox", "Coverage status"]
        for c, nm in enumerate(gap_hdr, 1):
            cell = ws.cell(r, c, nm)
            cell.font = head_font
            cell.fill = head_fill
        r += 1
        listed = 0
        for res, k, nm in tabbed:
            code, label = _coverage_status(k)
            if code == "ok":
                continue
            ws.cell(r, 1, k["title"])
            ws.cell(r, 2, nm)
            ws.cell(r, 3, res["title"])
            cell = ws.cell(r, 4, label)
            if code in ("unreadable", "partial"):
                cell.font = Font(color="C00000", bold=True)
            r += 1
            listed += 1
        if not listed:
            ws.cell(r, 1, "(every sampled schema returned coverage)").font = Font(
                italic=True, color="666666")
        r += 1

    # Author / provenance footer, so the file can be traced back to a person as
    # well as to a script.
    r += 1
    ws.cell(r, 1, f"Produced by {SCRIPT_AUTHOR}").font = Font(bold=True,
                                                              color="666666")
    for link in (AUTHOR_SITE, AUTHOR_LINKEDIN):
        r += 1
        c = ws.cell(r, 1, link)
        c.font = Font(color="0563C1", underline="single")
        c.hyperlink = link

    # ---- How to Use: for the reader who has never seen this before ----------
    # Second tab deliberately, so it sits next to the Summary rather than being
    # buried behind thirty schema tabs.
    hu = wb.create_sheet("How to Use")
    confidential(hu)
    hu["A2"] = "How to use this workbook"
    hu["A2"].font = title_font
    hu["A3"] = ("Everything here was read directly from Adobe Experience "
                "Platform. Nothing is hand-maintained, so it is as current as "
                "the date on the Summary tab -- and no more.")
    hu["A3"].font = Font(italic=True, color="666666")

    HOW_TO = [
        ("START HERE", "", ""),
        ("I want to...", "Go to", "What you'll find"),
        ("...see what data exists at all",
         "Schemas",
         "Every schema kept, its class, how many datasets feed it, and which "
         "tab describes it."),
        ("...find one particular field",
         "Field Index",
         "Every field across every schema in one list. Filter the Field column "
         "to search; it tells you which schema and tab it belongs to."),
        ("...write SQL against the data",
         "Datasets",
         "Maps each dataset's friendly name to its SQL table name. Use the "
         "TABLE NAME in FROM, not the friendly name -- they differ. The "
         "Profile column flags what feeds Real-Time Customer Profile."),
        ("...find a schema or table that has no tab",
         "Dropped Schemas (hidden)",
         "Every schema left out of this workbook and why: ad-hoc dumps, AJO, "
         "Adobe plumbing, tests, and schemas no dataset uses. Right-click any "
         "tab > Unhide to see it, then filter the SQL table name column. "
         "Schemas that still hold data are listed first."),
        ("...find a field that isn't in the Field Index",
         "Dropped Field Index (hidden)",
         "Every field of every dropped schema: dot-notation path, type and "
         "display name, with the schema, why it was dropped and its tables. "
         "An inventory only -- no coverage or sample values."),
        ("...understand one schema in detail",
         "Its own tab",
         "The schema's description, then every field with its type, friendly "
         "name, friendly path (the display names from the root down, e.g. "
         "Person > Full name > First name), description, whether it is an "
         "identity, and (when the coverage pass ran) how often it is actually "
         "populated plus its ten commonest values (PII starred)."),
        ("...know how far back a field's data goes (BETA)",
         "'Have we got it?' columns / Field History",
         "How many months of UNBROKEN data each field has, up to now -- what "
         "you need to know before building an audience on it. The Field Index "
         "and every schema tab end with the number of months, a plain-English "
         "detail ('since 28 Sep 2026', 'stopped -- last seen Aug 2026') and a "
         "heartbeat with one bar per month, oldest on the left. The Field "
         "History tab has the same months as columns, % filled, shaded green. "
         "Only there when the run was given --history."),
        ("...read it in plain English",
         "Friendly Name / Description",
         "Every tab carries the human-readable name next to the technical one: "
         "fields (Friendly Name, Friendly Path), schemas, datasets and "
         "audiences. Descriptions are shown wherever Adobe holds one -- a "
         "blank means nobody has written one yet, not that we dropped it."),
        ("...see who is being targeted, and how",
         "Audiences",
         "Every audience with its tags, who built it, who last changed it, and "
         "the rule behind it in readable form."),
        ("...know which Profile snapshot table to query",
         "Snapshot Tables",
         "Every snapshot export table with its merge policy. The GREEN rows "
         "are the correct ones -- on the DEFAULT merge policy. The rest sit on "
         "other merge policies (added for debugging etc.) or carry none."),
        ("...know which route profiles arrived by, and what expiring a dataset saves",
         "Overlap cut-list / Overlap full report",
         "The Profile store's dataset overlap report. The cut-list is the profiles "
         "ONLY one dataset contributes (expire it, save exactly that many) with a "
         "running total against the Addressable Audience licence. The full report "
         "is every combination of datasets a profile is built from, largest first "
         "-- a sudden new combination is a new route in."),
        ("...know how fresh the snapshot is",
         "Snapshot Summary",
         "Per sandbox: when the system evaluation is scheduled, when it last "
         "actually ran, when the snapshot was written and how long that took, "
         "tagged RED / AMBER / GREEN. Production first, development / PPE at "
         "the bottom. All times UTC."),
        ("", "", ""),
        ("THINGS THAT WILL CATCH YOU OUT", "", ""),
        ("More than one Profile snapshot table",
         "Snapshot Tables",
         "A sandbox has one snapshot table PER MERGE POLICY, all with similar "
         "names. Query the one on the default merge policy (green) unless you "
         "mean to use another."),
        ("Coverage % is a SAMPLE",
         "schema tabs",
         "Taken from up to a thousand real records, not the whole dataset. "
         "Treat it as 'roughly how often this is filled in', not an exact "
         "figure."),
        ("'Have we got it?' is a spot-check (BETA)",
         "Field Index / schema tabs / Field History",
         "Each month is judged from one to three real batches per dataset, not "
         "every record, so a field that is rarely filled in can be missed in a "
         f"month. Heartbeat: {SPARK_NONE} no data at all that month, "
         f"{SPARK_EMPTY} data arrived but the field was empty, "
         f"{SPARK_LEVELS[0]} to {SPARK_LEVELS[-1]} how often it was filled, "
         f"{SPARK_UNSURE} could not be checked. Test, perf and temp datasets "
         "are left out. Months go by LOAD date: data backfilled later counts "
         "from when it was loaded, and the schema tab says when a sample showed "
         "a backfill."),
        ("'In Profile now' is what an audience can see (BETA)",
         "Field Index / schema tabs / Field History",
         "Audiences are built on Profile, not the data lake, and Profile keeps "
         "event data only as long as the Profile retention set on its dataset "
         "(often 90 days, sometimes less). So: the field's days of unbroken "
         "data, capped at that retention. 'Profile keeps only 45 days' means a "
         "longer look-back cannot work from Profile, however far back the lake "
         "goes. Datasets with no Profile expiry keep everything. Some datasets "
         "load to Profile only, with no data-lake copy to check; their months "
         "show ? and the schema tab says how many records Profile received."),
        ("MISSING is not the same as 0%",
         "Summary",
         "0% means we looked and the field was empty. MISSING means we could "
         "not read the data at all. Any schema in that state is listed in the "
         "DATA COMPLETENESS block on the Summary tab and banner-flagged on its "
         "own tab."),
        ("Some rules are only PARTLY shown",
         "Audiences",
         "Rules marked 'partial' contain an event-sequence or time-window "
         "clause that is shown as <...>. What IS displayed is correct, but "
         "there is more to the rule -- the raw column holds the full "
         "definition."),
        ("Long rules are cut off in the sheet",
         "Audiences",
         "Excel cannot hold more than 32,767 characters in a cell. A rule "
         "longer than that (typically a hard-coded store list) is marked "
         "'yes' in 'PQL truncated in sheet' and ends with "
         f"'{PQL_TRUNCATED_MARK}'. The FULL text of every audience's rule is "
         "in pql/audiences_pql_<sandbox>_<date>.jsonl beside this workbook: one "
         "JSON line per audience with audience_id, audience_name, format, "
         "raw_pql and readable_pql."),
        ("Some audiences cost far more to run than others",
         "Audiences (Complexity columns)",
         "Not all audiences are equal: one badly built audience can double the "
         "nightly batch run on its own. The Complexity score (0-100, beta) and "
         "its RED / AMBER / GREEN read the definition for the known cost "
         "drivers: an event scan with a long or NO lookback window, a lookup "
         "join to another class (products via gtin), an aggregation such as "
         "sum() / count() / 'occurs N times', and building on a very large "
         "base audience. 'Why this score' spells out the points. The fix for "
         "a RED is usually to shorten the lookback, or move the sum / count "
         "upstream into a computed attribute and test that instead."),
        ("Not every audience has a rule",
         "Audiences",
         "Only Origin=AEPSegments audiences are rule-based. Uploads, Data "
         "Distiller and Audience Orchestration compositions are built "
         "elsewhere, so their rule column is legitimately blank."),
        ("'Last modified by' is often a robot",
         "Audiences",
         "Entries like (Adobe service: pathos) are Adobe's own automation, not "
         "a colleague. Only email addresses are people."),
        ("Friendly labels are not DULE labels",
         "Summary / Schemas",
         "'Friendly labels' counts fields that carry a human-readable display "
         "name (Adobe's alternateDisplayInfo) -- the Friendly Name column on "
         "each schema tab. DULE (Data Usage Labeling and Enforcement) is a "
         "different thing: Adobe's governance "
         "framework that controls access to objects and data through labels "
         "such as C8 and C12. DULE labels are not in this workbook yet."),
        ("", "", ""),
        ("EVERY TAB", "", ""),
        ("Filters and frozen headers are on",
         "all tabs",
         "Click any header arrow to filter. The header row stays put as you "
         "scroll."),
        ("PII is obfuscated",
         "sample values",
         "In the TOP-10 sample values, email addresses and identifier / contact "
         "fields (user id, CRM id, phone, ...) have two characters starred out, "
         "whether or not the field carries a DULE label. Shapes and rankings "
         "remain; whole real identifiers do not."),
        ("Confidential",
         "all tabs",
         "Even with PII obfuscated, the coverage pass means this workbook holds "
         "real sampled customer data. Treat it accordingly."),
    ]
    hr = 5
    for c, nm in enumerate(("I want to...", "Go to", "What you'll find"), 1):
        hu.cell(hr, c, nm)
    style_header(hu, 3, row=hr)
    rr = hr + 1
    section_fill = PatternFill("solid", fgColor="DCE6F1")
    for left, mid, right in HOW_TO[1:]:
        if left in ("I want to...",):
            continue
        a = hu.cell(rr, 1, left)
        b = hu.cell(rr, 2, mid)
        c_ = hu.cell(rr, 3, right)
        if left and not mid and not right:            # a section heading row
            for cell in (a, b, c_):
                cell.fill = section_fill
            a.font = Font(bold=True, color="1F4E78")
        else:
            a.font = Font(bold=True)
            b.font = Font(color="1F4E78")
        for cell in (a, b, c_):
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        hu.row_dimensions[rr].height = 30 if right else 18
        rr += 1
    autofit(hu, [34, 18, 86])
    hu._dd_table = None          # prose, not a table -- no filter dropdowns
    hu.cell(rr + 1, 1, f"Generated by {SCRIPT_NAME}.py  -  {SCRIPT_AUTHOR}")
    hu.cell(rr + 1, 1).font = Font(italic=True, color="666666")
    hu.cell(rr + 2, 1, AUTHOR_SITE).font = Font(color="0563C1", underline="single")
    hu.cell(rr + 2, 1).hyperlink = AUTHOR_SITE
    hu.cell(rr + 3, 1, AUTHOR_LINKEDIN).font = Font(color="0563C1",
                                                    underline="single")
    hu.cell(rr + 3, 1).hyperlink = AUTHOR_LINKEDIN
    # What changed in this version, and every version before it.
    hu.cell(rr + 5, 1, f"What's new in v{SCRIPT_VERSION} -- release notes:").font = (
        Font(bold=True))
    hu.cell(rr + 6, 1, RELEASE_NOTES_URL).font = Font(color="0563C1",
                                                      underline="single")
    hu.cell(rr + 6, 1).hyperlink = RELEASE_NOTES_URL

    # ---- Master Field Index (every field across all schemas, for lookup) ----
    fi = wb.create_sheet("Field Index")
    confidential(fi)
    fi["A2"] = f"Field Index  -  {label}"
    fi["A2"].font = title_font
    fi["A3"] = ("Every field across all schemas. Look up an exact dot-notation "
                "path (Ctrl-F); the Tab column says which sheet it lives on. "
                "Not here? Fields of the schemas left out of this workbook are "
                "on the hidden 'Dropped Field Index' tab (right-click any tab "
                "> Unhide).")
    fi["A3"].font = Font(italic=True, color="666666")
    hr = 5
    fi_cols = FIELD_INDEX_COLUMNS + (hist_cols if any_hist else [])
    for c, nm in enumerate(fi_cols, 1):
        fi.cell(hr, c, nm)
    style_header(fi, len(fi_cols), row=hr)
    index_rows = []
    for res, k, name in tabbed:
        dd = k.get("datadict") or {}
        hist = k.get("history") or {}
        tnames = _sql_table_names(k)
        for (fpath, dtype, friendly, fpath_t, desc, req, ident, rel) in k["fields"]:
            info = dd.get(fpath, {})
            index_rows.append((fpath, dtype, friendly, fpath_t, desc, ident,
                               k["title"], tnames, name, info.get("coverage"),
                               info.get("top"), hist.get(fpath)))
    index_rows.sort(key=lambda x: (x[0].lower(), x[6].lower()))
    ridx = hr + 1
    for (fpath, dtype, friendly, fpath_t, desc, ident, sch, tnames, tab, cov,
         top, h) in index_rows:
        fi.cell(ridx, 1, fpath)
        fi.cell(ridx, 2, dtype)
        fi.cell(ridx, 3, friendly)
        fi.cell(ridx, 4, fpath_t)
        fi.cell(ridx, 5, desc)
        fi.cell(ridx, 6, ident)
        fi.cell(ridx, 7, sch)
        fi.cell(ridx, 8, tnames)
        fi.cell(ridx, 9, tab)
        if cov is not None:
            cc = fi.cell(ridx, 10, cov)
            cc.alignment = center
            cc.number_format = '0"%"'
        fi.cell(ridx, 11, top)
        put_history(fi, ridx, 12, h)
        ridx += 1
    autofit(fi, [52, 18, 28, 40, 50, 20, 38, 34, 24, 11, 60]
            + (hist_widths if any_hist else []))

    # ---- Field History (BETA): the heartbeat as numbers, month by month -----
    if any_hist:
        from openpyxl.formatting.rule import ColorScaleRule
        fh = wb.create_sheet("Field History")
        fh.sheet_properties.tabColor = "6E3FA3"
        confidential(fh)
        fh["A2"] = f"Field History (BETA)  -  {label}"
        fh["A2"].font = title_font
        fh["A3"] = ("How far back each field's data goes. Each month column is "
                    "the % of sampled records carrying the field: blank = no "
                    "data at all that month, 0 = data arrived but the field was "
                    "empty, ? = could not be checked. Read from one to three "
                    "batches per dataset per month (test, perf and temp datasets "
                    "left out), by the month the data was LOADED.")
        fh["A3"].font = Font(italic=True, color="666666")
        hr = 5
        fh_cols = (["Field (dot notation)", "Schema", "Tab"] + hist_cols[:3]
                   + [_mon(lab) for lab in hist_labels])
        for c, nm in enumerate(fh_cols, 1):
            fh.cell(hr, c, nm)
        style_header(fh, len(fh_cols), row=hr)
        fh.freeze_panes = fh.cell(hr + 1, 2)
        none_fill = PatternFill("solid", fgColor="F2F2F2")
        first_m = 7
        ridx = hr + 1
        for (fpath, _t, _f, _ft, _d, _i, sch, _tn, tab, _c, _top, h) in index_rows:
            if not h:
                continue
            fh.cell(ridx, 1, fpath)
            fh.cell(ridx, 2, sch)
            fh.cell(ridx, 3, tab)
            mc = fh.cell(ridx, 4, h["months"])
            mc.number_format = HISTORY_MONTHS_FMT
            mc.alignment = center
            fh.cell(ridx, 5, h["detail"])
            fh.cell(ridx, 6, h.get("profile_days")).alignment = center
            for i, v in enumerate(h["cells"]):
                cell = fh.cell(ridx, first_m + i, v)
                cell.alignment = center
                if v is None:
                    cell.fill = none_fill
                elif isinstance(v, int):
                    cell.number_format = '0"%"'
            ridx += 1
        last_col = get_column_letter(first_m + len(hist_labels) - 1)
        fh.conditional_formatting.add(
            f"{get_column_letter(first_m)}{hr + 1}:{last_col}{max(ridx - 1, hr + 1)}",
            ColorScaleRule(start_type="num", start_value=0, start_color="FFFFFF",
                           end_type="num", end_value=100, end_color="63BE7B"))
        autofit(fh, [52, 38, 24, 13, 46, 12] + [9] * len(hist_labels))

    # ---- Schemas index tab (one row per kept schema) ------------------------
    sh = wb.create_sheet("Schemas")
    confidential(sh)
    index_cols = ["Tab"] + SCHEMA_CSV_COLUMNS
    hr = 3
    for c, nm in enumerate(index_cols, 1):
        sh.cell(hr, c, nm)
    style_header(sh, len(index_cols), row=hr)
    rr = hr + 1
    for res, k, name in tabbed:
        row = [name, res["title"], k["title"], k.get("description", ""),
               k["class"], k.get("kind"),
               "Y" if k.get("profile_enabled") else "N", k["datasets"],
               k.get("lake_gb"), k.get("lake_rows"), k.get("lake_ttl_days"),
               k.get("profile_gb"), k.get("profile_ttl_days"),
               _sql_table_names(k), k["n_fields"], k["n_identities"],
               k["n_relationships"], k["n_labels"], k["last_mod"], k["id"]]
        for c, val in enumerate(row, 1):
            cell = sh.cell(rr, c, val)
            if c in (9, 12) and val is not None:
                cell.number_format = "#,##0.00"
            elif c in (10, 11, 13) and val is not None:
                cell.number_format = "#,##0"
        rr += 1
    autofit(sh, [26, 18, 38, 50, 22, 9, 10, 9, 13, 15, 13, 11, 13, 40, 7, 10,
                 13, 16, 16, 58])

    # ---- Datasets tab: friendly name -> SQL table (system) name -------------
    # EVERY dataset in each sandbox, so a query can be aimed at the right table.
    # The table name (tags['adobe/pqs/table']) is what Query Service uses in
    # FROM -- distinct from the friendly dataset name shown in the UI.
    dt = wb.create_sheet("Datasets")
    confidential(dt)
    dt["A2"] = f"Datasets / SQL table names  -  {label}"
    dt["A2"].font = title_font
    dt["A3"] = ("Every dataset and its AEP Query Service table name. SQL uses the "
                "SYSTEM table name, not the friendly name: SELECT ... FROM "
                "<Table Name>. Kind = the class of the schema the dataset feeds "
                "(Profile / Event / Custom). The Profile column flags datasets "
                "enabled for Unified Profile and the Profile Snapshot Export (the "
                "merged union to query for whole profiles). Data hygiene columns "
                "come from Catalog: data lake size and row count, the data lake "
                "retention (TTL) and the Profile store size and TTL -- TTLs in "
                "days (months = 30, years = 365), blank where no expiration is "
                "set. Use the header filters to sort by GB or TTL.")
    dt["A3"].font = Font(italic=True, color="666666")
    DATASET_COLUMNS = ["Sandbox", "Schema", "Kind", "Friendly Name (dataset)",
                       "Description", "Table Name (SQL / system)", "Profile",
                       "Data lake GB", "Data lake rows", "Data lake TTL (days)",
                       "Profile GB", "Profile TTL (days)", "Dataset ID"]
    hr = 5
    for c, nm in enumerate(DATASET_COLUMNS, 1):
        dt.cell(hr, c, nm)
    style_header(dt, len(DATASET_COLUMNS), row=hr)
    rr = hr + 1
    # Profile-related datasets first (snapshot, then enabled), then the rest --
    # so the tables you need for Profile queries sit at the top.
    def _prof_rank(p):
        return 0 if p.startswith("snapshot") else (1 if p else 2)
    for res in results:
        for d in sorted(res.get("datasets") or [],
                        key=lambda x: (_prof_rank(x.get("profile") or ""),
                                       (x.get("schema_title") or "~").lower(),
                                       (x.get("name") or "").lower())):
            dt.cell(rr, 1, res["title"])
            dt.cell(rr, 2, d.get("schema_title"))
            dt.cell(rr, 3, d.get("schema_kind"))
            dt.cell(rr, 4, d.get("name"))
            dt.cell(rr, 5, d.get("description", ""))
            dt.cell(rr, 6, d.get("table"))
            pcell = dt.cell(rr, 7, d.get("profile"))
            if (d.get("profile") or "").startswith("snapshot"):
                pcell.font = Font(bold=True, color="7030A0")
            elif d.get("profile"):
                pcell.font = Font(color="2E7D32")
            for c, key, fmt in ((8, "lake_gb", "#,##0.00"),
                                (9, "lake_rows", "#,##0"),
                                (10, "lake_ttl_days", "#,##0"),
                                (11, "profile_gb", "#,##0.00"),
                                (12, "profile_ttl_days", "#,##0")):
                if d.get(key) is not None:
                    dt.cell(rr, c, d[key]).number_format = fmt
            dt.cell(rr, 13, d.get("id"))
            rr += 1
    autofit(dt, [18, 40, 9, 42, 50, 44, 26, 13, 15, 13, 11, 13, 34])

    # ---- Dropped Schemas (hidden): everything the filters left out ----------
    # Hidden because it runs to thousands of ad-hoc rows nobody reads day to
    # day -- but when a table is missing, this says why instead of leaving it
    # to be guessed. Schemas that still hold data come first.
    dr = wb.create_sheet("Dropped Schemas")
    dr.sheet_state = "hidden"
    confidential(dr)
    dr["A2"] = f"Dropped schemas  -  {label}"
    dr["A2"].font = title_font
    dr["A3"] = ("Every schema the filters left out of this workbook, and why. "
                "Rows with datasets come first: those hold data, so if a table "
                "you query has no tab, it is here. Filter the SQL table name "
                "column to find it. The filter rules are in README.md "
                "(Filtering).")
    dr["A3"].font = Font(italic=True, color="666666")
    DROPPED_COLUMNS = ["Sandbox", "Schema", "Drop reason", "Why",
                       "Datasets", "SQL table name(s)", "Fields", "Class",
                       "Behaviour", "Last Modified", "Schema $id"]
    hr = 5
    for c, nm in enumerate(DROPPED_COLUMNS, 1):
        dr.cell(hr, c, nm)
    style_header(dr, len(DROPPED_COLUMNS), row=hr)
    rr = hr + 1
    for res in results:
        for d in sorted(res.get("dropped") or [],
                        key=lambda x: (0 if x["datasets"] else 1, x["status"],
                                       x["title"].strip().lower())):
            nf = d.get("fields")
            row = [res["title"], d["title"], d["status"].split(":", 1)[1],
                   DROP_REASONS.get(d["status"], ""), d["datasets"],
                   _sql_table_names(d),
                   len(nf) if nf is not None else "could not be read",
                   d["class"], d["behaviour"], d["last_mod"], d["id"]]
            for c, val in enumerate(row, 1):
                dr.cell(rr, c, val)
            rr += 1
    autofit(dr, [18, 50, 12, 60, 9, 44, 9, 30, 12, 16, 58])

    # ---- Dropped Field Index (hidden): the fields of every dropped schema ---
    # The census half of the story: names and types only -- no coverage, no
    # identities -- so a field that lives in a dropped schema still shows up
    # somewhere when searched for. A quarter of a million rows in prod, so it
    # is hidden.
    dfi = wb.create_sheet("Dropped Field Index")
    dfi.sheet_state = "hidden"
    confidential(dfi)
    dfi["A2"] = f"Dropped Field Index  -  {label}"
    dfi["A2"].font = title_font
    dfi["A3"] = ("Every field of every schema left out of this workbook (see "
                 "Dropped Schemas for why). An inventory only: dot-notation "
                 "path, type and display name, with no coverage or sample "
                 "values. Filter Datasets > 0 for the schemas that still hold "
                 "data.")
    dfi["A3"].font = Font(italic=True, color="666666")
    DROPPED_FIELD_COLUMNS = ["Field (dot notation)", "Data Type",
                             "Friendly Name", "Schema", "Drop reason",
                             "Datasets", "SQL table name(s)", "Sandbox"]
    hr = 5
    for c, nm in enumerate(DROPPED_FIELD_COLUMNS, 1):
        dfi.cell(hr, c, nm)
    style_header(dfi, len(DROPPED_FIELD_COLUMNS), row=hr)
    drows = []
    for res in results:
        for d in res.get("dropped") or []:
            reason = d["status"].split(":", 1)[1]
            tnames = _sql_table_names(d)
            for fpath, dtype, friendly in d.get("fields") or []:
                drows.append((fpath, dtype, friendly, d["title"], reason,
                              d["datasets"], tnames, res["title"]))
    drows.sort(key=lambda x: (x[0].lower(), x[3].strip().lower()))
    # Explicit rows, not append(): style_header's freeze pane has already
    # touched the row under the header, so append() would leave it blank.
    for rr, row in enumerate(drows, hr + 1):
        for c, val in enumerate(row, 1):
            dfi.cell(rr, c, val)
    autofit(dfi, [52, 18, 28, 50, 12, 9, 44, 18])

    # ---- Audiences tab: what the business does WITH the data ----------------
    # Completes the chain: schema -> dataset -> audience -> the rule behind it.
    # Only the rule-based audiences (Origin = AEPSegments) carry PQL; uploads,
    # Data Distiller and Audience Orchestration compositions have none, which is
    # why that column is legitimately blank for a good number of rows.
    aud_rows = [r for res in results for r in (res.get("audiences") or [])]
    aud_incomplete = [res["title"] for res in results
                      if not res.get("audiences_complete", True)]
    if aud_rows or aud_incomplete:
        at = wb.create_sheet("Audiences")
        confidential(at)
        at["A2"] = f"Audiences / segments  -  {label}"
        at["A2"].font = title_font
        note = ("Every audience in the sandbox, with its tags, who created and "
                "last changed it, and its segmentation rule. PQL is rendered "
                "from Adobe's syntax tree: rows marked 'partial' contain an "
                "event-sequence or time-window clause shown as <...> -- the raw "
                "column holds the complete definition. Only Origin=AEPSegments "
                "audiences have a rule at all. Excel cuts a cell at 32,767 "
                "characters: a rule longer than that is marked 'yes' under "
                "'PQL truncated in sheet' and ends with "
                f"'{PQL_TRUNCATED_MARK}' -- the full text of EVERY rule is in "
                "the pql/audiences_pql_<sandbox>_<date>.jsonl file in the folder "
                "next to this workbook. 'PQL format' is json (Adobe's syntax tree) or "
                "pql-text (complete, but not JSON). COMPLEXITY (beta): a 0-100 "
                "score of how much work the audience costs the nightly batch "
                "run, read off its definition -- event scans and how far back "
                "they look, joins to other classes (products via gtin), "
                "aggregations (sum / count / 'occurs N times'), the size of "
                "the base audience it depends on, sequences, condition count. "
                f"RED >= {audience_complexity.RAG_RED}, AMBER >= "
                f"{audience_complexity.RAG_AMBER}, else GREEN. 'Why this score' "
                "shows the points. Weights are a first cut: argue with them.")
        if aud_incomplete:
            note += (f"  WARNING: the listing was INCOMPLETE for "
                     f"{', '.join(aud_incomplete)} -- those counts understate "
                     f"the estate.")
        at["A3"] = note
        at["A3"].font = Font(italic=True,
                             color="C00000" if aud_incomplete else "666666")
        # The complexity formula, in full, right above the columns it feeds --
        # generated from the weights in audience_complexity.py.
        at["A4"] = audience_complexity.formula_text()
        at["A4"].font = Font(bold=True, color="1F4E78")
        at["A4"].alignment = Alignment(wrap_text=True, vertical="top")
        at.merge_cells("A4:T4")
        at.row_dimensions[4].height = 92
        AUDIENCE_COLUMNS = ["Sandbox", "Audience name", "Description",
                            "Audience id",
                            "Evaluation", "Lifecycle", "Tags", "Tag count",
                            "Origin", "Created by", "Last modified by",
                            "PQL (readable)", "PQL rendered",
                            "PQL (raw pql/json)", "PQL length",
                            "PQL truncated in sheet", "PQL format",
                            "Unresolved tag ids", "System tags",
                            "Complexity score (0-100)", "Complexity RAG",
                            "Why this score", "Event scan",
                            "Lookback (days)", "Lookup joins",
                            "Joined fields", "Aggregations",
                            "Depends on (audiences)", "Largest base audience",
                            "Largest base (profiles)", "Sequence steps",
                            "Conditions", "Longest value list",
                            "Merge policies", "Used by (audiences)"]
        hr = 5
        for c, nm in enumerate(AUDIENCE_COLUMNS, 1):
            at.cell(hr, c, nm)
        style_header(at, len(AUDIENCE_COLUMNS), row=hr)
        rr = hr + 1
        # Tagged and rule-bearing audiences first: the ones anyone came to read.
        for row in sorted(aud_rows,
                          key=lambda r: (0 if r[6] else 1, 0 if r[11] else 1,
                                         str(r[1]).lower())):
            for c, val in enumerate(row, 1):
                cell = at.cell(rr, c, val)
                if c == 16 and val == "yes":
                    cell.font = Font(bold=True, color="C00000")
                # Complexity score + RAG cells carry the traffic light.
                if c in (20, 21) and row[20]:
                    bg, fg = snapshot_tables.RAG_COLOURS[row[20]]
                    cell.fill = PatternFill("solid", fgColor=bg)
                    cell.font = Font(bold=True, color=fg)
                if c == 22:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                if c == 30 and isinstance(val, int):
                    cell.number_format = "#,##0"
            rr += 1
        autofit(at, [16, 46, 50, 36, 12, 14, 34, 10, 20, 32, 32, 70, 13, 50, 11,
                     12, 10, 22, 12, 12, 12, 90, 10, 12, 10, 10, 20, 12, 40, 14,
                     10, 11, 12, 10, 12])
        at.freeze_panes = at.cell(hr + 1, 1).coordinate
        at.auto_filter.ref = (f"A{hr}:"
                              f"{get_column_letter(len(AUDIENCE_COLUMNS))}{rr - 1}")

    # ---- Snapshot tabs: which snapshot table to query, and how fresh it is ---
    # Folded in from snapshot_tables.py, which owns the layout -- the tabs here
    # are the same ones its standalone workbook carries.
    if snapshots:
        snap_rows, snap_unreadable, snap_sandboxes = snapshots
        snapshot_tables.add_sheets(
            wb, snap_rows, snap_unreadable, snap_sandboxes,
            title=f"Snapshot tables  -  {label}",
            scope="each sandbox in this workbook")

    # ---- Overlap tabs: which route profiles arrived by, and what a cut saves -
    # Folded in from overlap_report.py, which owns the layout.
    if overlaps:
        book = Book(f"Data Dictionary v{SCRIPT_VERSION}  -  {label}", wb=wb)
        for sb_name, ov in overlaps.items():
            suffix = sb_name if len(overlaps) > 1 else ""
            overlap_report.add_sheets(book, ov["rows"], ov["report_dt"], sb_name,
                                      "dataset", ov["total"], suffix=suffix,
                                      error=ov["error"])

    # ---- One tab per schema, listing its individual fields ------------------
    for res, k, name in tabbed:
        sheet = wb.create_sheet(name)
        # Every schema tab is coloured: hue = class (purple Profile, blue
        # ExperienceEvent, green other), shade = dark when at least one of its
        # datasets is enabled for Unified Profile, light when none is. Only
        # dark purple is in the merged union and sampled from the snapshot.
        # The system is explained in the Summary tab's colour key.
        sheet.sheet_properties.tabColor = tab_colour(k)
        confidential(sheet)
        dd = k.get("datadict")
        sheet["A2"] = k["title"]
        sheet["A2"].font = title_font
        a3 = (f'{k["class"]}   |   {k["datasets"]} dataset(s)   |   '
              f'{k["n_fields"]} fields   |   {k["n_identities"]} identity'
              f'   |   {k["n_relationships"]} relationship'
              f'   |   modified {k["last_mod"]}   |   sandbox: {res["title"]}')
        if dd:
            a3 += f'   |   data dictionary: sampled {k.get("dd_rows", 0)} records'
            if k.get("dd_source"):
                a3 += f'   |   source: {k["dd_source"]}'
        hist = k.get("history")
        if hist or k.get("history_note"):
            srcs, skipped = k.get("history_sources") or [], k.get("history_skipped") or []
            a3 += (f'   |   history (BETA): {len(srcs)} dataset(s)'
                   + (f', {len(skipped)} test/temp left out' if skipped else ''))
            if k.get("history_backfill"):
                a3 += (f' -- backfill seen: events dated back to '
                       f'{k["history_backfill"]} were loaded later, and history '
                       f'goes by load month')
            if k.get("history_note"):
                a3 += f'   |   /!\\ HISTORY {k["history_note"]}'
        # Loud flag when coverage is missing/partial so this tab is never read as
        # gospel: data exists but couldn't be sampled, or only some batches read.
        a3_color = "666666"
        if any_dd:
            code, label = _coverage_status(k)
            if code != "ok":
                a3 += f'   |   /!\\ COVERAGE {label}'
                if code in ("unreadable", "partial"):
                    a3_color = "C00000"
        sheet["A3"] = a3
        sheet["A3"].font = Font(italic=True, color=a3_color,
                                bold=(a3_color == "C00000"))
        # The schema's own description, as written in the Schema Registry --
        # blank line kept when there is none, so the layout is the same on
        # every tab.
        sheet["A4"] = k.get("description") or ""
        sheet["A4"].font = Font(italic=True, color="444444")
        sheet["A5"] = k["id"]
        sheet["A5"].font = Font(italic=True, color="999999", size=9)
        # SQL table (system) name(s) for this schema's dataset(s) -- the FROM
        # target(s), right next to the field list so a query can be formed here.
        tnames = _sql_table_names(k)
        if tnames:
            sheet["A6"] = f"SQL table name(s):  {tnames}"
            sheet["A6"].font = Font(italic=True, bold=True, color="C55A11")

        cols = list(SCHEMA_FIELD_COLUMNS)
        if dd:
            cols += ["Coverage %", "Top values (count)"]
        hist_at = len(cols) + 1
        if hist:
            cols += hist_cols
        hr = 7
        for c, nm in enumerate(cols, 1):
            sheet.cell(hr, c, nm)
        style_header(sheet, len(cols), row=hr)
        ridx = hr + 1
        for (fpath, dtype, friendly, fpath_t, desc, req, ident, rel) in k["fields"]:
            sheet.cell(ridx, 1, fpath)
            sheet.cell(ridx, 2, dtype)
            sheet.cell(ridx, 3, friendly)
            sheet.cell(ridx, 4, fpath_t)
            sheet.cell(ridx, 5, desc)
            sheet.cell(ridx, 6, req).alignment = center
            sheet.cell(ridx, 7, ident)
            sheet.cell(ridx, 8, rel)
            if dd:
                info = dd.get(fpath, {})
                cov = sheet.cell(ridx, 9, info.get("coverage"))
                cov.alignment = center
                cov.number_format = '0"%"'
                sheet.cell(ridx, 10, info.get("top"))
            if hist:
                put_history(sheet, ridx, hist_at, hist.get(fpath))
            ridx += 1
        autofit(sheet, [50, 20, 28, 40, 50, 9, 22, 42]
                + ([11, 70] if dd else [])
                + (hist_widths if hist else []))

    # ---- Filters on every table, in one sweep -------------------------------
    # Done here rather than per sheet so a tab added later can't quietly ship
    # without them.
    for sheet in wb.worksheets:
        table = getattr(sheet, "_dd_table", None)
        if not table:
            continue
        hrow, ncols = table
        if sheet.max_row > hrow:
            sheet.auto_filter.ref = (f"A{hrow}:"
                                     f"{get_column_letter(ncols)}{sheet.max_row}")

    archived = _archive_previous(OUTPUT_DIR, safe_client)
    if archived:
        logger.info(f"Archived {archived} previous dictionary file(s) -> "
                    f"{OUTPUT_DIR / 'archive'}")
    # The full-PQL sidecar travels with the workbook: written beside it, and
    # any earlier one for the same sandbox archived with the old dictionary.
    for res in results:
        _archive_previous(PQL_DIR, res["name"], kind="sidecar")
        res["pql_sidecar"] = write_pql_sidecar(res, PQL_DIR, datestr)
    try:
        wb.save(path)
    except PermissionError:
        # The file is open in Excel. A long sampling run must not be thrown away
        # over that -- write alongside it and say so plainly.
        alt = path.with_name(f"{path.stem} (new){path.suffix}")
        wb.save(alt)
        logger.warning(f"{path.name} is locked (open in Excel?) -- wrote "
                       f"{alt.name} instead.")
        return alt
    return path


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------
def _in_dd_scope(title: str, scope: str) -> bool:
    return scope == "all" or scope in (title or "").lower()


def fmt_report_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "unknown date"


def _resolve_profile_snapshot(token, conf, sandbox, profile_snapshot):
    """Decide which dataset to sample for a Profile-class schema's coverage.
    Returns (dataset_id, source_label) or None to signal a fallback to feeding
    datasets. An explicit --profile-snapshot=<id> override wins; otherwise the
    default merge policy -> its snapshot export dataset."""
    if profile_snapshot:
        logger.info(f"  profile: using --profile-snapshot override dataset "
                    f"{profile_snapshot}.")
        return profile_snapshot, f"Profile Snapshot Export (override {profile_snapshot})"
    mp = get_default_merge_policy(token, conf, sandbox)
    if not mp:
        return None
    mp_id, mp_name = str(mp.get("id")), (mp.get("name") or str(mp.get("id")))
    logger.info(f"  profile: default merge policy '{mp_name}' ({mp_id}); "
                f"locating its Profile Snapshot Export dataset...")
    found = find_profile_snapshot_dataset(token, conf, sandbox, mp_id)
    if not found:
        logger.warning(f"  profile: no Profile Snapshot Export dataset matches the "
                       f"default merge policy ({mp_id}).")
        return None
    snap_dsid, snap_name = found
    logger.info(f"  profile: snapshot dataset '{snap_name}' ({snap_dsid}).")
    return snap_dsid, f"Profile Snapshot Export (default merge policy '{mp_name}')"


def add_data_dictionary(token, conf, results, dd_scope, dd_rows,
                        profile_snapshot=None):
    """Sample real records and attach k['datadict'] + k['dd_rows'] to each kept
    schema in scope. Mutates `results` in place.

    Profile-class schemas (the post-merge union) are sampled from the Profile
    Snapshot Export dataset of the default merge policy rather than their
    pre-merge feeding datasets -- see _resolve_profile_snapshot."""
    try:
        import pyarrow  # noqa: F401  (fail early if the reader is missing)
    except ImportError:
        logger.error("--data-dict needs pyarrow (pip install pyarrow). "
                     "Skipping the data dictionary; the rest still ran.")
        return

    for res in results:
        in_scope = [k for k in res["kept"] if _in_dd_scope(k["title"], dd_scope)]
        if not in_scope:
            logger.warning(f"  data dict: no kept schema in {res['name']} matches "
                           f"scope '{dd_scope}'. Kept titles include: "
                           f"{', '.join(k['title'] for k in res['kept'][:3])}...")
            continue
        logger.info(f"  data dict: {len(in_scope)} schema(s) in scope for "
                    f"{res['name']}: {', '.join(k['title'] for k in in_scope)}")
        ds_index = get_dataset_ids_by_schema(token, conf, res["name"])
        snap_resolved = None     # per-sandbox cache: (dsid, label) | False
        # Every Profile-class schema reads the SAME union dataset, so the
        # snapshot is sampled ONCE per sandbox and the rows reused for all of
        # them. Until v3.4.1 each re-downloaded a ~127MB partition: slow, and as
        # many separate chances to hit a gateway 504 as there were Profile
        # schemas -- the first failure being cached into MISSING coverage for
        # every one behind it. On a real prod run that cost 16 of 33 tabs.
        snap_sample = None       # (rows, sstats) | None
        t_dd_start = time.perf_counter()         # wall clock for the whole sweep

        # Pass 1 covers everything in scope; pass 2 revisits only what came back
        # UNREADABLE. A gateway 504 is usually transient, so a few extra minutes
        # at the end of the run is far cheaper than finding the hole tomorrow.
        for dd_pass in (1, 2):
            if dd_pass == 1:
                targets = in_scope
            else:
                targets = [k for k in in_scope
                           if k.get("dd_empty_reason") == "unreadable"]
                if not targets:
                    break
                snap_sample = None      # give the union one genuine second go
                logger.info(f"  {ANSI['bold']}data dict: RETRY PASS{ANSI['reset']} "
                            f"-- {len(targets)} schema(s) were unreadable on the "
                            f"first pass; trying each once more before writing "
                            f"the workbook.")
            n_scope = len(targets)
            for i, k in enumerate(targets, 1):
                # Overall progress so an overnight all-schemas run is easy to
                # track: how far through we are and how long we've been going.
                elapsed = time.perf_counter() - t_dd_start
                eta = (elapsed / (i - 1) * (n_scope - (i - 1))) if i > 1 else 0
                logger.info(f"  data dict: {ANSI['bold']}schema {i}/{n_scope}"
                            f"{ANSI['reset']} in {res['name']}"
                            f"{' (RETRY)' if dd_pass == 2 else ''}  "
                            f"{ANSI['dim']}({elapsed/60:.1f} min elapsed"
                            f"{f', ~{eta/60:.0f} min left' if eta else ''})"
                            f"{ANSI['reset']}")
                k.pop("dd_empty_reason", None)   # this attempt re-decides it
                # Profile-class coverage must come from the merged union
                # snapshot, not the pre-merge feeds. Falls back to feeding
                # datasets if unresolvable -- and deliberately uses the feeding
                # datasets when the schema is Profile-CLASS but none of its
                # datasets is enabled for Profile: those rows never reach the
                # union, so the snapshot would (wrongly) read 0% for them.
                smallest_first, file_timeout = False, 75
                if (k.get("meta_class") == PROFILE_CLASS
                        and not k.get("profile_enabled")):
                    dsids = ds_index.get(k["id"], [])
                    k["dd_source"] = ("own datasets (Profile-class schema, but "
                                      "none of its datasets is enabled for "
                                      "Profile -- not in the merged union)")
                elif k.get("meta_class") == PROFILE_CLASS:
                    if snap_resolved is None:
                        snap_resolved = _resolve_profile_snapshot(
                            token, conf, res["name"], profile_snapshot) or False
                    if snap_resolved:
                        snap_dsid, source_label = snap_resolved
                        k["dd_source"] = source_label
                        dsids = [snap_dsid]
                        smallest_first, file_timeout = True, DD_SNAPSHOT_TIMEOUT
                    else:
                        dsids = ds_index.get(k["id"], [])
                        k["dd_source"] = ("feeding datasets (PRE-MERGE -- snapshot "
                                          "not found; coverage may read sparse)")
                        logger.warning(f"  data dict [{i}/{n_scope}]: {k['title']} "
                                       f"is Profile-class but no snapshot "
                                       f"resolved; falling back to feeding "
                                       f"datasets.")
                else:
                    dsids = ds_index.get(k["id"], [])
                if not dsids:
                    logger.warning(f"  data dict [{i}/{n_scope}]: {k['title']} has "
                                   "no (non-perf) datasets to sample; skipping.")
                    continue

                # The snapshot is shared across every Profile schema: download it
                # once, then reuse the rows in memory.
                if smallest_first and snap_sample is not None:
                    rows, sstats = snap_sample
                    if not rows:
                        k["dd_empty_reason"] = "unreadable"
                        logger.warning(f"  data dict [{i}/{n_scope}]: {k['title']} "
                                       f"-- snapshot unreadable on this pass; "
                                       f"skipping, coverage MISSING.")
                        continue
                    logger.info(f"  data dict [{i}/{n_scope}]: '{k['title']}' -- "
                                f"{k['n_fields']} fields; reusing the snapshot "
                                f"sample already downloaded this run "
                                f"({len(rows)} rows, no re-download).")
                else:
                    logger.info(f"  data dict [{i}/{n_scope}]: '{k['title']}' -- "
                                f"{len(dsids)} dataset(s), {k['n_fields']} fields; "
                                f"sampling up to {dd_rows} records ...")
                    try:
                        rows, sstats = sample_schema_rows(
                            token, conf, res["name"], dsids, dd_rows,
                            smallest_first=smallest_first,
                            file_timeout=file_timeout)
                    except Exception as e:
                        logger.warning(f"  data dict: sampling failed for "
                                       f"{k['title']}: {type(e).__name__}: {e}")
                        logger.debug(traceback.format_exc())
                        # Unreadable, NOT empty -- we learned nothing about the
                        # contents. Classified by the block below.
                        rows, sstats = [], {"available": 0, "failed": 1,
                                            "empty_reads": 0, "list_failed": 1}
                    if smallest_first:      # cache success OR failure, once
                        snap_sample = (rows, sstats)
                k["dd_sample_stats"] = sstats
                if not rows:
                    # Distinguish "genuinely empty" from "data exists but
                    # unreadable" so a 0% schema is never mistaken for an empty one.
                    if sstats.get("list_failed"):
                        # The batch LIST never returned for some dataset(s) -- we
                        # never learned whether they carry records: MISSING, not 0%.
                        logger.warning(f"  data dict [{i}/{n_scope}]: {k['title']} "
                                       f"-- 0 records, but the batch list failed "
                                       f"for {sstats['list_failed']} of "
                                       f"{len(dsids)} dataset(s) "
                                       f"(gateway/timeout). NOT confirmed empty -- "
                                       f"contents unknown, coverage MISSING.")
                        k["dd_empty_reason"] = "unreadable"
                    elif sstats["available"] == 0:
                        logger.warning(f"  data dict [{i}/{n_scope}]: {k['title']} "
                                       f"-- 0 records: EMPTY (no batch in any of "
                                       f"its {len(dsids)} dataset(s) carries "
                                       f"records). Genuinely not populated.")
                        k["dd_empty_reason"] = "empty"
                    else:
                        logger.warning(f"  data dict [{i}/{n_scope}]: {k['title']} "
                                       f"-- 0 records but {sstats['available']} "
                                       f"batch(es) HAD records; {sstats['failed']} "
                                       f"read(s) failed, {sstats['empty_reads']} "
                                       f"parsed empty. NOT confirmed empty -- data "
                                       f"exists but couldn't be sampled.")
                        k["dd_empty_reason"] = "unreadable"
                    continue
                # Sampled OK, but flag if some batches failed (partial read).
                if sstats["failed"]:
                    logger.warning(f"  data dict [{i}/{n_scope}]: {k['title']} -- "
                                   f"note: {sstats['failed']} batch(es) failed to "
                                   f"read; coverage is from the {len(rows)} rows "
                                   f"that succeeded.")
                t0 = time.perf_counter()
                k["datadict"] = build_data_dict(rows, k["fields"])
                k["dd_rows"] = len(rows)
                populated = sum(1 for v in k["datadict"].values()
                                if v["coverage"] > 0)
                logger.info(f"  data dict [{i}/{n_scope}]: '{k['title']}' done -- "
                            f"{len(rows)} records, {populated}/{len(k['fields'])} "
                            f"fields populated (>0% coverage); "
                            f"tally {time.perf_counter() - t0:.1f}s.")


# ----------------------------------------------------------------------------
# Field history -- "Have we got it?" (v3.4.13, BETA)
# ----------------------------------------------------------------------------
# How many months of UNBROKEN data each field has, up to now -- the question
# behind building an audience on it. Query Service would need a query per field
# per month. Instead, for each dataset and month, Catalog finds that month's
# batches (createdAfter/createdBefore, ~0.2 s) and ONE of them is read through
# the Data Access API with the coverage pass's reader. That one download answers
# for every field at once, so the cost is datasets x months, not fields x months.
# Months before a dataset's first batch, or past its TTL, cost nothing.
#
# Measured on prod on 2026-10-22: slot expiry's first batch is 2026-09-28, so its
# fields read "<1"; order events go back to 2025-04-23 under a 2-year TTL. One
# sample a month can fake a gap -- April's order sample was an odd 101-row batch
# carrying 9 of 16 fields -- so a month that is thin, short of fields, or missing
# a field both neighbours have is sampled again from earlier in the month.
def _ms(dt):
    return int(dt.timestamp() * 1000)


def _mon(label: str) -> str:
    """'2026-04' -> 'Apr 2026'."""
    return datetime.strptime(label, "%Y-%m").strftime("%b %Y")


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def _history_months(n, now=None):
    """[(label 'YYYY-MM', start, end)] for the last n calendar months, oldest
    first, in UTC. The current month is partial: it ends now."""
    now = now or datetime.now(timezone.utc)
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        start = datetime(y, m, 1, tzinfo=timezone.utc)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{y:04d}-{m:02d}", start,
                    min(datetime(ny, nm, 1, tzinfo=timezone.utc), now)))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def _history_batches(headers, dsid, extra, limits=(HISTORY_PAGE, 10, 3, 1)):
    """Success batches of a dataset matching `extra` (a Catalog filter and sort
    suffix), as [(batchId, meta)]. The page size steps down on a timeout, as in
    sample_schema_rows: Catalog answers a small page where a large one 504s."""
    base = f"{CATALOG_BATCHES_URL}?dataSet={dsid}&status=success{extra}"
    last = None
    for lim in limits:
        try:
            d = _get_json_retry(f"{base}&limit={lim}", headers, 60, attempts=2,
                                label=f"history batches {dsid[:24]}")
        except Exception as e:
            last = e
            continue
        return [(bid, b) for bid, b in (d.items() if isinstance(d, dict) else [])
                if isinstance(b, dict)]
    raise last


def _typical_batches(cands, used):
    """Readable batches not yet read, most TYPICAL first: a known record count
    before an unknown one, big enough to prove something, then nearest the
    median size. A month's odd batch (a handful of records of some other kind)
    is what fakes a gap."""
    pool = [(bid, b) for bid, b in cands if bid not in used
            and _batch_record_count(b) != 0 and _lake_readable(bid, b)]
    med = _median([n for n in (_batch_record_count(b) for _, b in pool) if n > 0])

    def key(kv):
        n = _batch_record_count(kv[1])
        return (n < 0, 0 <= n < HISTORY_MIN_ROWS, abs(n - med))
    return sorted(pool, key=key)


class _DatasetLocked(Exception):
    """Data Access refuses this dataset outright (HTTP 403) -- every month would
    fail the same way, so the dataset is skipped at once and says why."""


def _why(e: Exception) -> str:
    """Short reason for a failed read, naming the endpoint where we can."""
    if isinstance(e, urllib.error.HTTPError):
        return f"{_endpoint_of(e.url or '').split(' (')[0]}: HTTP {e.code}"
    if "timed out" in str(e).lower() or isinstance(e, TimeoutError):
        return "Data Access: no answer in time"
    return type(e).__name__


def _history_backoff() -> None:
    """Pause every history worker when Data Access is struggling: if most of the
    last 20 Data Access calls timed out or 5xx'd, wait HISTORY_PAUSE_SECS rather
    than carry on collecting unreadable months. 4xx (a locked dataset) does not
    count -- that is a permission, not a struggling platform."""
    with _EP_LOCK:
        now = time.time()
        wait = _PAUSE_UNTIL[0] - now
        if wait <= 0:
            recent = list(_EP_RECENT)
            fails = [o for o in recent if o == "no answer in time"
                     or o.startswith("HTTP 5")]
            if len(recent) >= _EP_RECENT.maxlen and len(fails) >= HISTORY_TRIP_FAILS:
                _PAUSE_UNTIL[0] = now + HISTORY_PAUSE_SECS
                _EP_RECENT.clear()
                wait = HISTORY_PAUSE_SECS
                logger.warning(f"  Adobe Data Access is struggling: {len(fails)} of the "
                               f"last {len(recent)} calls failed "
                               f"({', '.join(f'{v} {k}' for k, v in Counter(fails).items())}). "
                               f"Pausing history reads for {HISTORY_PAUSE_SECS // 60} min.")
    if wait > 0:
        time.sleep(wait)


def _history_read(headers, bid, paths):
    """Read one batch -> (records read, {path: records carrying it}, earliest
    event date seen as 'YYYY-MM-DD' or None). One file list, 90 s wait."""
    _history_backoff()
    rows, _ = _download_batch_rows(headers, bid, HISTORY_ROWS, smallest_first=True,
                                   file_timeout=75, quiet=True,
                                   listing=HISTORY_LISTING)
    counts = {}
    for p in paths:
        n = sum(1 for r in rows if _extract_values(r, p))
        if n:
            counts[p] = n
    stamps = [str(r.get("timestamp"))[:10] for r in rows if r.get("timestamp")]
    return len(rows), counts, (min(stamps) if stamps else None)


def _history_sample(headers, dsid, a, b, paths, rec=None, whole_month=True):
    """Read one more typical batch created in [a, b) into the month record
    `rec` (a fresh one when None) and return it. A whole month with no batches
    stays 'none'; a part-month window that finds nothing changes nothing.
    rec["retry"] marks an unreadable month worth asking again next run (a
    timeout or 5xx); one that can never be read is cached like any other."""
    if rec is None:
        rec = {"state": "none", "rows": 0, "fields": {}, "ts": None, "batches": []}
    a_ms, b_ms = _ms(a), _ms(b)
    used = set(rec["batches"])
    cands, pick = [], []
    for page in range(HISTORY_PAGES):
        try:
            got = _history_batches(headers, dsid, f"&createdAfter={a_ms}"
                                   f"&createdBefore={b_ms}&orderBy=desc:created"
                                   f"&start={page * HISTORY_PAGE}")
        except Exception as e:
            logger.debug(f"      history: batch list {dsid} {a:%Y-%m-%d} failed ({e})")
            if not cands:
                if whole_month:
                    rec["state"], rec["why"] = "unsure", f"Catalog batch list: {_why(e)}"
                    rec["retry"] = True
                return rec
            break
        cands += [(bid, x) for bid, x in got
                  if a_ms <= int(x.get("created") or 0) < b_ms]
        pick = _typical_batches(cands, used)[:HISTORY_TRIES]
        if pick or len(got) < HISTORY_PAGE:
            break
    if whole_month:
        # What Profile received this month, whether or not the lake can show it.
        rec["profile_records"] = sum(int((x.get("metrics") or {}).get("recordsWritten") or 0)
                                     for _, x in cands if _is_profile_side_batch(x))
    if whole_month and all(_batch_record_count(x) == 0 for _, x in cands):
        return rec          # no batches, or only empty ones: 'none'
    if whole_month and not pick:
        if any(_is_profile_side_batch(x) for _, x in cands):
            rec["state"], rec["why"] = "unsure", "Profile only: no data-lake copy to read"
        elif any(_is_consolidation_batch(x) for _, x in cands):
            rec["state"], rec["why"] = "unsure", "consolidated by Adobe: Data Access cannot read it"
        return rec          # otherwise only empty / identity batches: 'none'
    for bid, _ in pick:
        rec["batches"].append(bid)
        try:
            n, counts, ts = _history_read(headers, bid, paths)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise _DatasetLocked(_why(e)) from None
            rec["why"] = _why(e)
            rec["retry"] = rec.get("retry") or e.code >= 500
            logger.debug(f"      history: batch {bid} unreadable ({rec['why']})")
            continue
        except Exception as e:
            rec["why"] = _why(e)
            rec["retry"] = True
            logger.debug(f"      history: batch {bid} unreadable ({rec['why']})")
            continue
        if not n:
            rec["why"] = rec.get("why") or "data-lake files empty"
            continue
        rec["rows"] += n
        rec.pop("why", None)
        for p, c in counts.items():
            rec["fields"][p] = rec["fields"].get(p, 0) + c
        if ts and (rec["ts"] is None or ts < rec["ts"]):
            rec["ts"] = ts
        break
    rec["state"] = "sampled" if rec["rows"] else "unsure"
    return rec


def dataset_history(headers, ds, months, paths, cached):
    """One dataset, month by month:
        {"first": 'YYYY-MM-DD' | None, "months": {label: rec}, "read", "cached"}
    rec["state"] is 'none' (no batches that month, or before the dataset's first
    batch, or past its TTL), 'sampled' ("rows" records read, "fields" = records
    carrying each path) or 'unsure' (batches exist, none could be read).
    `cached` is this dataset's finished months from earlier runs.

    v3.4.14: no dataset-wide "oldest batch" question any more. Sorting a huge
    dataset's batches oldest-first 504'd on 12 prod datasets on 2026-10-23 and
    sank each of them; the newest-first, month-bounded Catalog query answers in
    ~0.2 s, so every month is simply asked in turn. The exact first date is
    looked up only inside the first month that has data."""
    dsid = ds["id"]
    ttl = ds.get("lake_ttl_days")
    cutoff = months[-1][2] - timedelta(days=ttl) if ttl else None
    current = months[-1][0]
    out, read, n_cached, locked = {}, [], 0, None
    for label, a, b in months:
        if cutoff and b <= cutoff:
            out[label] = {"state": "none"}
        elif label != current and label in cached:
            out[label] = cached[label]
            n_cached += 1
        elif locked:
            out[label] = {"state": "unsure", "why": locked, "batches": [], "retry": True}
        else:
            try:
                out[label] = _history_sample(headers, dsid, a, b, paths)
                read.append((label, a, b))
            except _DatasetLocked as e:
                locked = f"locked: {e}"
                out[label] = {"state": "unsure", "why": locked, "batches": [],
                              "retry": True}
    if locked:
        return {"first": None, "first_month": None, "months": out, "read": len(read),
                "cached": n_cached, "locked": locked}

    # A second look at any month read this run that looks doubtful: unreadable,
    # thin where the dataset is usually full, carrying far fewer fields than
    # usual, or missing a well-filled field that both neighbours have.
    sampled = [r for r in out.values() if r["state"] == "sampled"]
    med_rows = _median([r["rows"] for r in sampled])
    med_fields = _median([len(r["fields"]) for r in sampled])

    def strong(r):
        return {p for p, n in r["fields"].items()
                if 100 * n / r["rows"] >= HISTORY_SPARSE}
    seq = [lab for lab, _, _ in months if out[lab]["state"] == "sampled"]
    gappy = {c for p, c, n in zip(seq, seq[1:], seq[2:])
             if (strong(out[p]) & strong(out[n])) - set(out[c]["fields"])}
    for label, a, b in read:
        r = out[label]
        if str(r.get("why") or "").startswith("Profile only"):
            continue        # nothing in the month a second look could read
        if not (r["state"] == "unsure"
                or (r["state"] == "sampled"
                    and ((med_rows >= HISTORY_MIN_ROWS and r["rows"] < HISTORY_MIN_ROWS)
                         or (med_fields and len(r["fields"]) < 0.6 * med_fields)
                         or label in gappy))):
            continue
        span = (b - a) / (HISTORY_RESAMPLES + 1)
        for i in range(HISTORY_RESAMPLES):
            try:
                _history_sample(headers, dsid, a + i * span, a + (i + 1) * span,
                                paths, rec=r, whole_month=False)
            except _DatasetLocked:
                break
        r["resampled"] = True

    # Where the data starts. Only the first month that has any batches is
    # searched for its earliest batch -- a bounded query that answers -- and
    # only when that month is not the edge of the window or the TTL (data may
    # go back further than we looked). If even that fails, the month stands.
    first, first_month = None, None
    lab0 = next((lab for lab, _, _ in months
                 if out[lab].get("state") in ("sampled", "unsure")
                 and out[lab].get("batches")), None)
    if lab0:
        a0, b0 = next((a, b) for lab, a, b in months if lab == lab0)
        at_edge = lab0 == months[0][0] or (cutoff and a0 <= cutoff)
        if not at_edge:
            first_month = lab0
            try:
                fb = _history_batches(headers, dsid, f"&createdAfter={_ms(a0)}"
                                      f"&createdBefore={_ms(b0)}&orderBy=asc:created",
                                      limits=(1,))
                fms = next((int(x.get("created") or 0) for _, x in fb), 0)
                if fms:
                    first = datetime.fromtimestamp(fms / 1000, timezone.utc)
            except Exception as e:
                logger.debug(f"      history: first batch in {lab0} for {dsid}: {_why(e)}")
    return {"first": f"{first:%Y-%m-%d}" if first else None,
            "first_month": first_month, "months": out,
            "read": len(read), "cached": n_cached}


def _history_cache_file(sandbox: str, dsid: str) -> Path:
    return (HISTORY_CACHE_DIR / re.sub(r"[^0-9A-Za-z_-]+", "_", sandbox)
            / f"{dsid}.json")


def _history_cache_load(sandbox: str, dsid: str) -> dict:
    try:
        d = json.loads(_history_cache_file(sandbox, dsid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if d.get("v") != HISTORY_CACHE_VERSION or d.get("rows") != HISTORY_ROWS:
        return {}
    return d.get("months") or {}


def _history_cache_save(sandbox: str, dsid: str, months: dict, current: str):
    """Keep the finished months that were actually looked at. The current month
    is still filling, and a month that timed out deserves another try next run;
    one that can never be read (Profile only, empty files, files gone) is kept."""
    keep = {lab: r for lab, r in months.items()
            if lab != current and "batches" in r
            and not (r["state"] == "unsure" and r.get("retry"))}
    path = _history_cache_file(sandbox, dsid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"v": HISTORY_CACHE_VERSION, "rows": HISTORY_ROWS,
                               "months": keep}), encoding="utf-8")
    tmp.replace(path)


def _spark(state: str, cov) -> str:
    if state == "data":
        return SPARK_LEVELS[min(len(SPARK_LEVELS) - 1,
                                int(cov * len(SPARK_LEVELS) / 100))]
    return {"empty": SPARK_EMPTY, "unsure": SPARK_UNSURE}.get(state, SPARK_NONE)


def _history_verdict(cells, months, firsts, cutoff, now, first_months=()):
    """(months of unbroken data up to now, plain-English detail) for one field.
    cells: [(state, coverage%)] oldest first; state is data / empty / unsure /
    none. Months are counted from the real start where it is known -- the
    dataset's first batch, or the edge of its retention -- not whole months."""
    i = len(cells) - 1
    # This month is still filling: no batch yet, or none readable, says nothing
    # against the field.
    if i > 0 and cells[i][0] in ("none", "unsure"):
        i -= 1
    if cells[i][0] != "data":
        seen = [j for j, (s, _) in enumerate(cells) if s == "data"]
        if seen:
            return 0, f"stopped -- last seen {_mon(months[seen[-1]][0])}"
        if any(s == "empty" for s, _ in cells):
            return 0, "never seen in the data"
        if any(s == "unsure" for s, _ in cells):
            return None, "unknown -- the data could not be read"
        return 0, "no data in the window"
    j, unsure_n = i, 0
    while j > 0:
        s = cells[j - 1][0]
        if s == "data":
            j -= 1
        elif s == "unsure" and j > 1 and cells[j - 2][0] == "data":
            j -= 1
            unsure_n += 1
        else:
            break
    label, a, b = months[j]
    hit = [f for f in firsts if a <= f < b]
    if hit:
        start, why = min(hit), f"since {min(hit):%d %b %Y}"
    elif label in first_months:          # the data starts here; exact day unknown
        start, why = a, f"since {_mon(label)}"
    elif cutoff and a <= cutoff < b:
        start, why = cutoff, "everything kept -- older data is past the TTL"
    elif j == 0:
        start, why = a, (f"the whole {len(months)}-month window -- "
                         f"may go back further")
    else:
        prev = _mon(months[j - 1][0])
        start = a
        why = {"empty": f"since {_mon(label)} -- missing in {prev}",
               "none": f"since {_mon(label)} -- no data at all in {prev}",
               "unsure": f"since {_mon(label)} -- {prev} could not be checked",
               }[cells[j - 1][0]]
    if unsure_n:
        why += f" ({unsure_n} month{'s' if unsure_n != 1 else ''} unsure)"
    return round(max(0.0, (now - start).days / 30.44), 1), why


def _ttl_words(iso):
    """'P2Y' -> '2 years', 'P13M' -> '13 months', None -> 'no expiry'."""
    m = re.fullmatch(r"P(\d+)([YMWD])", str(iso or ""))
    if not m:
        return iso or "no expiry"
    n, unit = int(m.group(1)), {"Y": "year", "M": "month", "W": "week", "D": "day"}[m.group(2)]
    return f"{n} {unit}{'s' if n != 1 else ''}"


def _profile_now(months, detail, carriers):
    """(days, plain-English detail): how much of a field Profile holds now --
    its days of unbroken data up to now, capped by the Profile retention set on
    the datasets that carry it (adobe_unifiedProfile.rowExpiration: on roi-prod
    90 days on most event datasets, 45 on orders). (None, why) when it can't be
    said."""
    prof = [d for d in carriers if str(d.get("profile") or "").startswith("Profile")]
    if not prof:
        return None, "not in Profile -- its dataset is not enabled for Profile"
    if months is None:
        return None, detail
    days = round(months * 30.44)
    ttls = [d.get("profile_ttl_days") for d in prof]
    if not months:
        return 0, detail
    if any(t is None for t in ttls):
        return days, f"no Profile expiry on its dataset -- {detail}"
    keep = max(ttls)
    if days >= keep - 1:
        if keep < HISTORY_PROFILE_DAYS:
            short = ", ".join(sorted({d.get("table") or d.get("name") for d in prof
                                      if d.get("profile_ttl_days") == keep}))
            return keep, (f"Profile keeps only {keep} days ({short}); "
                          f"the data goes back further")
        return keep, f"all {keep} days Profile keeps"
    return days, f"{days} of the {keep} days Profile keeps -- {detail}"


def _field_cells(path, recs, labels):
    """[(state, coverage%)] per month for one field across the month records
    `recs`: a month counts if ANY of them carried the field."""
    cells = []
    for lab in labels:
        covs, unsure = [], False
        for r in recs:
            m = r["months"].get(lab) or {}
            if m.get("state") == "sampled" and m.get("rows"):
                covs.append(100 * m["fields"].get(path, 0) / m["rows"])
            elif m.get("state") == "unsure":
                unsure = True
        if any(c > 0 for c in covs):
            cells.append(("data", max(covs)))
        elif unsure:
            cells.append(("unsure", None))
        elif covs:
            cells.append(("empty", 0))
        else:
            cells.append(("none", None))
    # A rarely-filled field can simply be missed by a sample: a one-month
    # hole between two months that have it is called unsure, not a break.
    if max((c for s, c in cells if s == "data"), default=0) < HISTORY_SPARSE:
        for i in range(1, len(cells) - 1):
            if (cells[i][0] == "empty" and cells[i - 1][0] == "data"
                    and cells[i + 1][0] == "data"):
                cells[i] = ("unsure", None)
    return cells


def _verdict_args(recs, dsets, now):
    """(firsts, first_months, cutoff) for _history_verdict over these datasets."""
    firsts = [datetime.strptime(r["first"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
              for r in recs if r.get("first")]
    first_months = {r["first_month"] for r in recs
                    if r.get("first_month") and not r.get("first")}
    ttls = [d.get("lake_ttl_days") for d in dsets]
    # Only when EVERY dataset expires data has the oldest of it truly gone.
    cutoff = now - timedelta(days=max(ttls)) if ttls and all(ttls) else None
    return firsts, first_months, cutoff


def _schema_profile_now(path, recs, dsets, months, labels, now):
    """'In Profile now' for one field of a schema: dataset by dataset over its
    PROFILE-ENABLED datasets only (a lake-only dataset's data is not in
    Profile), each one's own unbroken run capped by its own Profile retention,
    and the best of them. Unknown rather than a confident 0 when one of those
    datasets loads to Profile only, so the lake cannot show what it holds."""
    best, hidden = None, []
    for r, d in zip(recs, dsets):
        if not str(d.get("profile") or "").startswith("Profile"):
            continue
        po = [m for m in r["months"].values()
              if str(m.get("why") or "").startswith("Profile only")]
        if po:
            hidden.append((d.get("table") or d.get("name"),
                           sum(m.get("profile_records") or 0 for m in po)))
        fd, fmd, cd = _verdict_args([r], [d], now)
        nd, det = _history_verdict(_field_cells(path, [r], labels), months,
                                   fd, cd, now, fmd)
        p = _profile_now(nd, det, [d])
        if best is None or (-1 if p[0] is None else p[0]) > (-1 if best[0] is None else best[0]):
            best = p
    if best is None:
        return None, "not in Profile -- no dataset of this schema is enabled for Profile"
    if not best[0] and hidden:
        name, n = hidden[0]
        return None, (f"unknown -- {name} loads to Profile only ({n:,} records "
                      f"written), which the data lake cannot show")
    return best


def schema_history(k, recs, dsets, months, now):
    """Per field of kept schema `k`, from its datasets' month records `recs`
    (parallel to `dsets`): {path: {"months", "detail", "profile_days",
    "profile_detail", "spark", "cells"}}. A month counts for a field if ANY of
    the schema's datasets carried it. 'In Profile now' goes dataset by dataset
    -- each one's own unbroken run, capped by its own Profile retention -- and
    takes the best: a 90-day backup dataset with one month of data must not
    lend its retention to a 45-day dataset's long run."""
    labels = [m[0] for m in months]
    firsts, first_months, cutoff = _verdict_args(recs, dsets, now)
    out = {}
    for f in k["fields"]:
        path = f[0]
        cells = _field_cells(path, recs, labels)
        n, detail = _history_verdict(cells, months, firsts, cutoff, now, first_months)
        p_days, p_detail = _schema_profile_now(path, recs, dsets, months, labels, now)
        out[path] = {
            "months": n, "detail": detail,
            "profile_days": p_days, "profile_detail": p_detail,
            "spark": "".join(_spark(s, c) for s, c in cells),
            "cells": [max(1, round(c)) if s == "data" else
                      0 if s == "empty" else "?" if s == "unsure" else None
                      for s, c in cells],
        }
    return out


def add_field_history(token, conf, results, scope, n_months=HISTORY_MONTHS):
    """Attach k['history'] -- {path: months / detail / spark / cells} -- to every
    kept schema in scope: the 'Have we got it?' columns (BETA). Test, perf and
    temp datasets are left out so they cannot make a field look older than the
    real feed. Mutates `results` in place."""
    try:
        import pyarrow  # noqa: F401  (fail early if the reader is missing)
    except ImportError:
        logger.error("--history needs pyarrow (pip install pyarrow). Skipping "
                     "field history; the rest still ran.")
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed

    months = _history_months(n_months)
    now = months[-1][2]
    for res in results:
        sandbox = res["name"]
        headers = aep_headers(token, conf, sandbox)
        by_id = {d["id"]: d for d in res.get("datasets") or []}
        in_scope = [k for k in res["kept"] if _in_dd_scope(k["title"], scope)]
        if not in_scope:
            logger.warning(f"  field history: no kept schema in {sandbox} matches "
                           f"scope '{scope}'.")
            continue
        jobs = {}                               # dataset id -> (dataset, paths)
        for k in in_scope:
            use, skipped = [], []
            for nm, table, dsid in k.get("tables") or []:
                ds = by_id.get(dsid) or {"id": dsid, "name": nm, "table": table}
                if (_HISTORY_SKIP_RE.search(nm or "") or _HISTORY_SKIP_RE.search(table or "")
                        or _SKIP_DS_RE.search(nm or "")
                        or str(ds.get("profile") or "").startswith("snapshot")):
                    skipped.append(table or nm)
                    continue
                use.append(ds)
            k["history_dsids"] = [d["id"] for d in use]
            k["history_sources"] = [d.get("table") or d.get("name") for d in use]
            k["history_skipped"] = skipped
            paths = [f[0] for f in k["fields"]]
            for d in use:
                jobs[d["id"]] = (d, paths)
        logger.info(f"  field history (BETA): {len(jobs)} dataset(s) behind "
                    f"{len(in_scope)} schema(s) in {sandbox}, {n_months} months "
                    f"back; finished months cached in "
                    f"{_history_cache_file(sandbox, 'x').parent}")

        def work(dsid):
            ds, paths = jobs[dsid]
            r = dataset_history(headers, ds, months, paths,
                                _history_cache_load(sandbox, dsid))
            _history_cache_save(sandbox, dsid, r["months"], months[-1][0])
            return r

        recs, t0 = {}, time.perf_counter()
        with ThreadPoolExecutor(max_workers=HISTORY_WORKERS) as pool:
            futs = {pool.submit(work, dsid): dsid for dsid in jobs}
            for i, fut in enumerate(as_completed(futs), 1):
                dsid = futs[fut]
                ds = jobs[dsid][0]
                name = ds.get("table") or ds.get("name") or dsid
                try:
                    r = fut.result()
                except Exception as e:
                    logger.warning(f"  history [{i}/{len(jobs)}] {name}: FAILED "
                                   f"({_why(e)}) -- its fields read as unknown; "
                                   f"it is retried in full next run.")
                    continue
                recs[dsid] = r
                ms = [r["months"].get(lab) or {} for lab, _, _ in months]
                beat = "".join(
                    SPARK_NONE if m.get("state") == "none" else
                    SPARK_UNSURE if m.get("state") == "unsure" else SPARK_LEVELS[-1]
                    for m in ms)
                start = (f"data since {r['first']}" if r.get("first") else
                         f"data since {_mon(r['first_month'])}" if r.get("first_month") else
                         "data before the window" if any(m.get("state") == "sampled" for m in ms)
                         else "no data in the window")
                bad = [m for m in ms if m.get("state") == "unsure"]
                why = Counter(m.get("why") or "no rows" for m in bad)
                again = sum(1 for m in bad if m.get("retry"))
                tail = (f"  {ANSI['yellow']}LOCKED ({r['locked']}) -- skipped"
                        f"{ANSI['reset']}" if r.get("locked") else
                        f"  {ANSI['yellow']}{len(bad)} month(s) unreadable: "
                        + "; ".join(f"{k} x{v}" for k, v in why.most_common(3))
                        + (f" -- {again} retried next run" if again else "")
                        + ANSI["reset"] if bad else "")
                logger.info(f"  history [{i}/{len(jobs)}] {name}: {start}, TTL "
                            f"{ds.get('lake_ttl') or 'none'}; {r['read']} month(s) read, "
                            f"{r['cached']} cached  {beat}  {ANSI['dim']}"
                            f"({(time.perf_counter() - t0) / 60:.1f} min){ANSI['reset']}"
                            + tail)

        buckets = {"<1": 0, "1-5": 0, "6-11": 0, "12-23": 0, "24+": 0, "none": 0}
        for k in in_scope:
            if not k["history_dsids"]:
                k["history_note"] = "no dataset left to check once test/temp ones are set aside"
                continue
            got = [(recs[d], jobs[d][0]) for d in k["history_dsids"] if d in recs]
            if not got:
                k["history_note"] = "UNREADABLE -- no dataset could be checked"
                continue
            notes = []
            if len(got) < len(k["history_dsids"]):
                notes.append(f"{len(k['history_dsids']) - len(got)} of "
                             f"{len(k['history_dsids'])} dataset(s) could not be checked")
            locked = [d.get("table") or d.get("name") for r, d in got if r.get("locked")]
            if locked:
                notes.append(f"Data Access refuses {', '.join(locked)} (HTTP 403)")
            for r, d in got:
                po = [m for m in r["months"].values()
                      if str(m.get("why") or "").startswith("Profile only")]
                empty = sum(1 for m in r["months"].values()
                            if m.get("why") == "data-lake files empty")
                name = d.get("table") or d.get("name")
                if po:
                    written = sum(m.get("profile_records") or 0 for m in po)
                    notes.append(f"{name}: {len(po)} month(s) loaded to Profile only "
                                 f"({written:,} records written to Profile), with no "
                                 f"data-lake copy to check (shown as ?)")
                if empty:
                    notes.append(f"{name}: {empty} month(s) whose data-lake files hold "
                                 f"no rows (shown as ?)")
            if notes:
                k["history_note"] = "; ".join(notes)
            k["history"] = schema_history(k, [r for r, _ in got],
                                          [d for _, d in got], months, now)
            k["history_labels"] = [m[0] for m in months]
            # Data loaded long after the events it records: history is by LOAD
            # month, so say so where the samples show it.
            old = [m["ts"] for r, _ in got for lab, a, _ in months
                   if (m := r["months"].get(lab) or {}).get("ts")
                   and m["ts"] < f"{a - timedelta(days=31):%Y-%m-%d}"]
            if old:
                k["history_backfill"] = min(old)
            for h in k["history"].values():
                n = h["months"]
                key = ("none" if not n else "<1" if n < 1 else "1-5" if n < 6
                       else "6-11" if n < 12 else "12-23" if n < 24 else "24+")
                buckets[key] += 1
        logger.info(f"  field history done in {sandbox} "
                    f"({(time.perf_counter() - t0) / 60:.1f} min): fields with "
                    + ", ".join(f"{k} months: {v:,}" for k, v in buckets.items()
                                if k != "none")
                    + f"; none now: {buckets['none']:,}")
        bad = [m for r in recs.values() for m in r["months"].values()
               if m.get("state") == "unsure"]
        if bad:
            logger.info(f"  unreadable dataset-months in {sandbox}: {len(bad)} -- "
                        + "; ".join(f"{k} x{v}" for k, v in Counter(
                            m.get("why") or "no rows" for m in bad).most_common())
                        + f"; {sum(1 for m in bad if m.get('retry'))} retried next run")


def prompt_data_dict(dd_rows: int = DD_DEFAULT_ROWS) -> str | None:
    """Naked-run opt-in: ask whether to also run the slow data-dictionary pass
    (coverage % + top values). Returns the scope string ('all') on yes, else
    None. Defaults to No so a stray Enter never kicks off the long download."""
    print()
    bar = ANSI["yellow"] + "-" * 60 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}Full data dictionary?{ANSI['reset']}")
    print(f"  {ANSI['dim']}Samples up to {dd_rows} real records per kept schema "
          f"(downloads + parses\n  Parquet) to add COVERAGE % and TOP-10 values "
          f"per field (PII starred).\n  This can take several minutes -- much longer on big "
          f"tenants.{ANSI['reset']}")
    print(bar)
    try:
        raw = input(
            f"\nRun the full data dictionary now? "
            f"({ANSI['cyan']}y{ANSI['reset']}/{ANSI['cyan']}N{ANSI['reset']}, "
            f"default No): "
        ).strip().lower()
    except EOFError:
        return None
    if raw in ("y", "yes"):
        return DD_DEFAULT_SCOPE
    logger.info(f"{ANSI['dim']}Skipping data dictionary -- metadata only. "
                f"(Pass --data-dict next time to skip this prompt.){ANSI['reset']}")
    return None


def run(service: str, sandbox_arg: str | None,
        dd_scope: str | None = None, dd_rows: int = DD_DEFAULT_ROWS,
        profile_snapshot: str | None = None,
        history_scope: str | None = None, history_months: int = HISTORY_MONTHS):
    bar = ANSI["cyan"] + "=" * 60 + ANSI["reset"]
    print()
    print(bar)
    print(f"  {ANSI['bold']}Data Dictionary v{SCRIPT_VERSION} for "
          f"{ANSI['yellow']}{pretty_name(service)}{ANSI['reset']}  "
          f"{ANSI['dim']}({service}){ANSI['reset']}")
    print(bar)

    try:
        conf = aep_creds.load_creds(service)
    except aep_creds.CredsError as e:
        logger.error(f"Failed to load credentials for {service!r}: {e}")
        return

    try:
        resp = authenticate(conf)
    except urllib.error.HTTPError as e:
        logger.error(f"IMS auth FAILED: HTTP {e.code} "
                     f"{e.read().decode(errors='replace')[:300]}")
        return
    except Exception as e:
        logger.error(f"IMS auth FAILED: {type(e).__name__}: {e}")
        return
    token = resp["access_token"]
    logger.info(f"IMS authenticated (org {conf['org_id']}).")

    ok, result = list_sandboxes(token, conf)
    if not ok:
        logger.error(f"GET /sandboxes failed: {result}")
        return
    if not result:
        logger.warning("Authenticated, but 0 sandboxes visible.")
        return

    if sandbox_arg:
        chosen = resolve_sandboxes_arg(sandbox_arg, result)
    else:
        chosen = pick_sandboxes(result)
    if not chosen:
        logger.info("No sandboxes chosen. Exiting.")
        return
    logger.info(f"Reading {len(chosen)} sandbox(es): "
                f"{', '.join(sb.get('name', '?') for sb in chosen)}")

    results = []
    # Org-level lookups shared across sandboxes: the tag vocabulary and the user
    # directory are the same wherever you stand, so they are fetched once.
    aud_caches = {"vocab": None, "directory": None}
    # Snapshot tables and the overlap report are read for every chosen sandbox,
    # including one whose schema collection fails below -- the reads are
    # independent.
    snap_rows, snap_unreadable, overlaps = [], [], {}
    overlap_report.ensure_opener()
    for sb in chosen:
        name = sb.get("name", "?")
        logger.info(f"Overlap report for {name} ...")
        try:
            ov = overlap_report.collect(overlap_report.aep_headers(token, conf, name), name)
        except Exception as e:
            ov = {"rows": [], "report_dt": None, "total": None, "requested_date": None,
                  "error": f"{type(e).__name__}: {e}"}
        if ov["error"]:
            logger.warning(f"  {name}: overlap report -- {ov['error']}")
        else:
            n_ex = sum(1 for r in ov["rows"] if r["exclusive"])
            logger.info(f"  {name}: overlap report of {fmt_report_dt(ov['report_dt'])}: "
                        f"{len(ov['rows']):,} rows, {n_ex} exclusive; profiles "
                        f"{ov['total']:,}" if isinstance(ov["total"], int) else
                        f"  {name}: overlap report: {len(ov['rows']):,} rows")
        overlaps[name] = ov
        try:
            got, err = snapshot_tables.collect_sandbox(token, conf, sb)
        except Exception as e:
            got, err = [], f"{type(e).__name__}: {e}"
        if err:
            logger.warning(f"  {sb.get('name')}: snapshot tables UNREADABLE "
                           f"-- {err}")
            snap_unreadable.append((sb.get("name"), err))
        snap_rows.extend(got)
        logger.info(f"Collecting {sb.get('name', '?')} ...")
        try:
            res = collect_sandbox(token, conf, sb)
        except urllib.error.HTTPError as e:
            logger.error(f"  {sb.get('name')}: HTTP {e.code} "
                         f"{flatten_err(e.read().decode(errors='replace'))}")
            continue
        except Exception as e:
            logger.error(f"  {sb.get('name')}: {type(e).__name__}: {e}")
            logger.debug(traceback.format_exc())
            continue
        attach_audiences(token, conf, res, aud_caches)
        print_sandbox(res)
        results.append(res)

    if not results:
        logger.warning("No sandbox data collected. Nothing to write.")
        return

    # Naked run (no --data-dict flag): offer the slow coverage pass rather than
    # silently skipping it. Only when interactive -- piped/automated runs keep
    # the old behaviour and are never blocked on input().
    if dd_scope is None and sys.stdin.isatty():
        dd_scope = prompt_data_dict(dd_rows)

    if dd_scope is not None:
        logger.info(f"{ANSI['bold']}Data dictionary ENABLED{ANSI['reset']} "
                    f"(scope: '{dd_scope}', up to {dd_rows} records per schema). "
                    f"This downloads and parses Parquet -- expect minutes.")
        add_data_dictionary(token, conf, results, dd_scope, dd_rows,
                             profile_snapshot=profile_snapshot)
    elif not sys.stdin.isatty():
        # Non-interactive run that wasn't offered the prompt above.
        logger.info(f"{ANSI['dim']}Data dictionary not requested -- pass "
                    f"--data-dict to sample real values + coverage "
                    f"(adds minutes).{ANSI['reset']}")
    endpoint_report("schemas, datasets, audiences"
                    + (" and the coverage pass" if dd_scope is not None else ""))

    if history_scope is not None:
        logger.info(f"{ANSI['bold']}Field history (BETA) ENABLED{ANSI['reset']} "
                    f"(scope: '{history_scope}', {history_months} months back). "
                    f"The first run on a sandbox reads every month -- hours on a "
                    f"big one; later runs read only the current month.")
        # A beta must never cost the dictionary: if the history pass falls over,
        # say so and write the workbook without it.
        try:
            add_field_history(token, conf, results, history_scope, history_months)
        except Exception as e:
            logger.error(f"Field history (BETA) failed -- writing the dictionary "
                         f"without it: {type(e).__name__}: {e}")
            logger.debug(traceback.format_exc())
            for res in results:
                for k in res["kept"]:
                    k.pop("history", None)
        endpoint_report("the field history pass")

    datestr = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    client = client_label(conf)
    xlsx_path = write_xlsx(results, client, datestr,
                           snapshots=(snap_rows, snap_unreadable, chosen),
                           overlaps=overlaps)
    if xlsx_path:
        n_tabs = sum(len(r["kept"]) for r in results)
        has_hist = any(k.get("history") for r in results for k in r["kept"])
        logger.info(f"XLSX written: {xlsx_path}  "
                    f"({n_tabs} schema tab(s) + Summary + Field Index + "
                    + ("Field History (BETA) + " if has_hist else "")
                    + f"Schemas index + Datasets + Snapshot Summary + "
                    f"Snapshot Tables + Overlap cut-list + Overlap full report)")

    # Audience rules: how many, how many the sheet could not hold whole, and
    # where the complete text went.
    for res in results:
        rows = res.get("audiences") or []
        if not rows:
            continue
        n_trunc = sum(1 for r in rows if r[15] == "yes")
        fmts = {}
        for r in rows:
            fmts[r[16] or "(no rule)"] = fmts.get(r[16] or "(no rule)", 0) + 1
        by_fmt = ", ".join(f"{k}={v}" for k, v in sorted(fmts.items()))
        logger.info(f"Audiences in {res['name']}: {len(rows)} total, "
                    f"{n_trunc} truncated in the sheet; format: {by_fmt}; "
                    f"full PQL sidecar: {res.get('pql_sidecar') or '(none)'}")
        t = res.get("complexity_tally") or {}
        if t:
            logger.info(f"Audience complexity (beta) in {res['name']}: "
                        f"{ANSI['red']}{ANSI['bold']}RED {t['RED']}{ANSI['reset']}, "
                        f"{ANSI['yellow']}AMBER {t['AMBER']}{ANSI['reset']}, "
                        f"{ANSI['green']}GREEN {t['GREEN']}{ANSI['reset']} "
                        f"of {sum(t.values())} rule-based audiences.")

    total_kept = sum(r["stats"]["kept"] for r in results)
    total_seen = sum(r["stats"]["total"] for r in results)
    logger.info(f"Done. {total_kept} kept of {total_seen} schemas across "
                f"{len(results)} sandbox(es).")


def keep_awake(on: bool) -> bool:
    """Hold Windows awake for the length of the run, or release the hold.

    A full run is long enough for the PC to fall asleep part-way: on
    2026-10-16 Modern Standby froze prod's run for 76 minutes. On a Modern
    Standby machine the screen going off IS the trigger, so the display is
    held on as well as the system. The hold belongs to this process and dies
    with it, so nothing about the machine's power settings changes. Closing
    the lid or pressing the power button still sleeps. Returns True when the
    hold was taken (Windows only)."""
    if sys.platform != "win32":
        return False
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
    flags = ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0)
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def print_banner() -> None:
    bar = ANSI["cyan"] + "=" * 72 + ANSI["reset"]
    print(bar)
    print(f"  {ANSI['bold']}{SCRIPT_NAME} v{SCRIPT_VERSION}{ANSI['reset']}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  {ANSI['dim']}Extracts AEP XDM schemas into a tabbed Excel data dictionary "
          f"with field coverage + sample values.{ANSI['reset']}")
    print(bar)
    print(aep_creds.source_banner())


def main():
    print_banner()
    args = sys.argv[1:]
    sandbox_arg = None
    dd_scope = None
    dd_rows = DD_DEFAULT_ROWS
    profile_snapshot = None
    history_scope = None
    history_months = HISTORY_MONTHS
    positional = []
    for a in args:
        if a.startswith("--sandbox="):
            sandbox_arg = a.split("=", 1)[1]
        elif a == "--history":
            history_scope = DD_DEFAULT_SCOPE      # ALL kept schemas, by default
        elif a.startswith("--history-months="):
            try:
                history_months = max(1, int(a.split("=", 1)[1]))
            except ValueError:
                logger.warning(f"Ignoring bad --history-months value in {a!r}.")
        elif a.startswith("--history="):
            history_scope = a.split("=", 1)[1].strip().lower() or DD_DEFAULT_SCOPE
        elif a in ("--data-dict", "--dd"):
            dd_scope = DD_DEFAULT_SCOPE          # ALL kept schemas, by default
        elif a.startswith("--data-dict="):
            dd_scope = a.split("=", 1)[1].strip().lower()
        elif a.startswith("--profile-snapshot="):
            profile_snapshot = a.split("=", 1)[1].strip() or None
        elif a.startswith("--dd-rows="):
            try:
                dd_rows = max(1, int(a.split("=", 1)[1]))
            except ValueError:
                logger.warning(f"Ignoring bad --dd-rows value in {a!r}.")
        elif a in ("--debug", "-v"):
            logger.setLevel(logging.DEBUG)
        elif a.startswith("-"):
            continue
        else:
            positional.append(a)

    services = aep_creds.list_services()
    if not services:
        logger.error("No credentials found in the keyring vault or the creds/ "
                     "folder. Add one with aep_credentials.py store "
                     "(or migrate), or drop a <service>.json in creds/.")
        return

    if positional:
        try:
            chosen = aep_creds.pick_service(positional[0])
        except aep_creds.CredsError as e:
            logger.error(str(e))
            return
    else:
        chosen = menu(services)

    if not chosen:
        logger.info("Nothing chosen. Exiting.")
        return

    # Held from here (after any menus, so an unattended prompt doesn't keep the
    # screen on) to the end of the run, error or not.
    if keep_awake(True):
        logger.info("Holding the PC awake for this run (screen stays on; "
                    "released when the run ends).")
    try:
        run(chosen, sandbox_arg, dd_scope, dd_rows,
            profile_snapshot=profile_snapshot,
            history_scope=history_scope, history_months=history_months)
    finally:
        keep_awake(False)


if __name__ == "__main__":
    main()
