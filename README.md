![AEP Swiss Army Knife Banner](banner.png)

# AEP Swiss Army Knife

A small toolkit of single-file command-line tools for working with Adobe
Experience Platform (AEP) from a locked-down VDI. They're deliberately
lightweight — every tool here today runs on the Python standard library
alone, so it needs no `pip install` — but that's a convenience, not a hard
rule: new tools may pull in a dependency where it genuinely earns its keep.

**One credential model for the whole toolkit.** Every tool authenticates the
same way: it reads a per-tenant JSON from `creds/`, lets you pick which set to
use, and mints a fresh OAuth token each run. See **[Get started](#get-started)**
for the shared framework — learn it once and every script below behaves the
same.

The toolkit spans two product ranges:

**Babelfish range** — Query Service template tooling:

| Tool | What it does |
|------|--------------|
| [`babelfish_query_renamer.py`](babelfish_query_renamer.py) | Tidies up messy Query Service SQL template names (optionally AI-suggested via Claude) and pushes renames back. |
| [`babelfish_query_fetcher.py`](babelfish_query_fetcher.py) | Strict read-only export of Query Service templates to local Markdown + RTF. Never writes back. |

**AEP Swiss Army range** — batch / credential / audit utilities:

| Tool | What it does |
|------|--------------|
| [`credential_validator.py`](credential_validator.py) | Quickly checks whether an IMS/AEP credential set is alive and what it can see. **Run this first on any new credential set.** |
| [`batch_fetcher.py`](batch_fetcher.py) | Lists recent batches, then downloads a chosen batch's files locally. |
| [`failed_batch_report.py`](failed_batch_report.py) | Exports a CSV summary of every batch that failed in the last N hours. |
| [`ajo_journey_checker.py`](ajo_journey_checker.py) | Lists every Adobe Journey Optimizer journey and extracts the **audience behind each** (read-audience or qualification) into a journey → audience table. |
| [`audit_batch_schedules_v2.py`](audit_batch_schedules_v2.py) | Audits every sandbox's Query Service schedules, classifies each (SEGMENTATION / QUERY / CRON), flags anomalies (ODD_TIME, ONCE, DISABLED, LATE), and writes a tabbed XLSX + CSV. |
| [`audit_streaming_schedules.py`](audit_streaming_schedules.py) | Catalogues and triages streaming audiences/segments in a sandbox (read-only) — live from AEP or from a local file dump. |
| [`batch_eval_timing.py`](batch_eval_timing.py) | Measures how long batch audience evaluation actually takes in a sandbox (read-only). |
| [`data_dictionary_v3.py`](data_dictionary_v3.py) | **Data Dictionary v3.2.** Sucks out every XDM schema, filters to the ones that matter, and writes a tabbed, *strictly-confidential* workbook: a master field index, one tab per schema (ready for Claude → Mermaid ERDs), and — with `--data-dict` — real field coverage + top-5 example values sampled in-memory. |

The tools are credential-driven and tenant-aware, so the same scripts run
cleanly across multiple Adobe orgs without folder collisions. `creds/*.json`
and generated export files are gitignored — never commit them; they hold
credentials and client/tenant identities.

## Get started

### 1. Add a credential set

Credentials live as one JSON file per tenant/integration in `creds/`. Copy the
template and fill in your own values:

```
cp creds/example.json "creds/my-tenant.json"
```

Required keys: `client_id`, `client_secret`, `org_id`. Optional: `api_key`
(the `x-api-key`, when it differs from `client_id` — e.g. AJO-subscribed
keys), `oauth_url`, `scopes`, `sandbox`. Any key starting with `_` is treated
as a comment and ignored, so you can annotate freely. The filename stem (here
`my-tenant`) is how you select the set on the command line.

`creds/*.json` is gitignored (only `creds/example.json` is tracked), so your
real client IDs, secrets, and even the *client names* never enter version
control.

### 2. Sanity-check it

```
python credential_validator.py my-tenant
```

This authenticates, decodes the token (scopes, org, expiry, technical
account), lists the sandboxes the credential can see, and sweeps the major AEP
product APIs — the fastest way to confirm a new set works before using it
elsewhere.

### 3. Run any tool

Every script uses the **same** credential selection. Run one bare to get an
interactive numbered menu of your `creds/` bank, or name a set by its filename
stem to skip the menu:

```
python <tool>.py                 # interactive: pick a credential set from a menu
python <tool>.py my-tenant       # pick by filename stem (no menu)
```

`credential_validator.py` and `babelfish_query_fetcher.py` also accept `--all`
to run across every set in the bank.

### 4. Optional dependencies

`pip install -r requirements.txt` — only needed for tools that produce richer
output (`openpyxl` for the tabbed XLSX workbooks; `pyarrow` + `tzdata` for the
data-dictionary's `--data-dict` sampling). The stdlib-only tools need nothing.

### The shared authentication framework

Every tool in this repo follows the same rules, so once you know one you know
them all:

- **One bank, many tenants.** All credentials live in `creds/*.json`, one file
  per tenant/integration. Drop a new file in and every tool picks it up.
- **Pick by menu or by name.** Bare run → numbered picker; `python tool.py
  <stem>` → that set directly. Selection is identical across every script.
- **Fresh token every run.** Each run mints a new OAuth `client_credentials`
  token from the chosen set. Adobe snapshots permissions at *mint* time, so a
  newly-granted product profile takes effect on the very next run — no caching
  surprises.
- **`api_key` vs `client_id`.** Requests send `x-api-key = api_key or
  client_id`, so a credential whose subscribed API key differs from its IMS
  client (e.g. AJO hybrids) just sets `api_key`.
- **Token mints but 403s?** That's the technical account missing from a product
  profile — `credential_validator.py` names this explicitly and tells you the
  fix.
- **Nothing secret is committed.** `creds/*.json` and all generated exports are
  gitignored; only `creds/example.json` is tracked.

---

# Babelfish range

## babelfish_query_renamer.py

Tidies up SQL query templates in AEP's Query Service (nicknamed *Babelfish*)
by pulling them down, suggesting sensible names — optionally via the Claude
API — and pushing the renames back.

Babelfish makes it easy to fire off a lot of queries during exploration and
iterative development. There's no enforced naming convention, so over a few
weeks the Templates panel ends up full of `33333`, `xxxxx`,
`testsite_c - select all`, half-finished experiments, and several "v2"s of
the same idea. This script lists every template you own, proposes a clean
kebab-case name (tagged `[babelfish]` so you can always tell which were
AI-renamed), and lets you accept, edit, or skip per-template — or run the
whole thing in batch mode. Each run also writes a snapshot and rebuilds a
single cross-tenant Markdown mega-file with every query's SQL.

Pick a credential set like every other tool (menu, or by stem). The optional
Claude naming step reads `anthropic_api_key` / `anthropic_model` /
`naming_config` from the **same** chosen creds JSON, so AI suggestions are
per-tenant. Since it writes back to AEP, it targets one credential set per run
(no `--all`).

```
python babelfish_query_renamer.py                 # interactive: pick a credential set
python babelfish_query_renamer.py my-tenant       # pick by stem
```

## babelfish_query_fetcher.py

A strict read-only fork of the renamer: it **never writes back to AEP** —
no PUT, no rename, no name suggestions. It authenticates,
discovers every accessible sandbox, fetches all templates, excludes
system/service-account queries (Adobe's `@AdobeID` namespace), and writes
the human-authored queries to local `.sql` files plus a cross-tenant
Markdown mega-file and a formatted RTF (titled header, per-sandbox
sections, monospace SQL). It prompts for a credential set and sandbox(es) by
default; pass them on the CLI (plus `--all` for every set) to run unattended
on a schedule.

```
python babelfish_query_fetcher.py                      # interactive menus
python babelfish_query_fetcher.py my-tenant --sandbox prod
python babelfish_query_fetcher.py --all                # every set in creds/
```

# AEP Swiss Army range

## batch_fetcher.py

Pulls a single AEP batch's files down to your local machine. It lists the
most recent batches in the sandbox, you paste (or pick) the batch ID you
want, and the script downloads every file in that batch and surfaces any
embedded error payloads. Replaces three earlier scripts (`auth.py`,
`authandret.py`, `fetchbatch.py`). GBR9 region headers are sent by default
(override via `region` in the creds JSON).

```
python batch_fetcher.py                 # interactive: pick a credential set
python batch_fetcher.py my-tenant       # pick by stem; then choose a batch
```

## failed_batch_report.py

Exports a CSV summary of every batch that FAILED in the last N hours
(default 24) in the chosen sandbox — a quick estate-wide health
snapshot. Use `batch_fetcher.py` instead when you need to drill into one
batch and download its failed-record files. Reports are written under
`./output/` (gitignored).

```
python failed_batch_report.py                       # interactive: pick a credential set
python failed_batch_report.py my-tenant --hours=72 --sandbox=prod
```

## credential_validator.py

Quickly checks whether an Adobe IMS / AEP credential set is alive. Pick a
credential JSON from `./creds/`; it authenticates via OAuth
server-to-server, inspects the returned access token (granted scopes, org,
expiry, technical account), lists the **provisioned products** the
integration is entitled to (`projectedProductContext` — read from the token
or, since S2S tokens rarely embed it, a best-effort `/ims/profile/v1` call),
and lists the sandboxes the credential can actually see — a useful proxy for
tenancy and admin breadth. **Scopes say what a credential may ask for;
provisioned products say what the org has granted — but note that apps built
on AEP (Adobe Journey Optimizer especially) ride on the `acp` context and
have no `serviceCode` of their own, so an absence there is a hint, never a
verdict.**

It then sweeps the major AEP product API surfaces — Query Service, Catalog,
Schema Registry, Flow Service, Data Ingestion, Real-Time Profile, Identity,
Privacy, Customer Journey Analytics, Adobe Journey Optimizer, and Offer
Decisioning — firing one lightweight GET at each and reporting per product
whether it **stood up** for this credential: `UP` (reachable + authorized),
`NO-PERM` (reachable but the credential lacks access — e.g. an AEP-only
credential hitting CJA), `INCONC` (a best-effort endpoint whose exact path
can't be confirmed, so a 404 isn't a reliable verdict), `ERR` (a 5xx — and
for Adobe Journey Optimizer a 500 is often *not* an outage: AJO returns 500
for a journey id that isn't in that sandbox, or a sandbox where AJO isn't
provisioned, so it's a sandbox/id signal, not proof of no entitlement), or
`DOWN` (unreachable). A quick "what can these creds actually talk to?" map.

A token minting fine does **not** mean it can do anything: a credential whose
**technical account** was never added to a product profile authenticates but
then `403`s on the very first call ("does not have READ permission"). The tool
calls that out explicitly and names the fix (an admin adds the tech account to
the product profile, then re-run — permissions are snapshotted at **mint**
time, so a fresh token is required). See
[`credential_validator - Overview.docx`](credential_validator%20-%20Overview.docx)
for how to read every verdict, the three layers of access (scopes vs
provisioned products vs technical-account permissions), and the AJO rules.

```
python credential_validator.py            # interactive menu
python credential_validator.py --all      # validate every set in ./creds/
```

## ajo_journey_checker.py

Lists every **Adobe Journey Optimizer** journey in a sandbox and pulls out the
**audience behind each one** — whether the journey *reads* an audience or is
triggered by *audience qualification* — into a journey → audience table. The
list call is `GET /ajo/journey` (singular, no id — the no-input collection
endpoint, the AJO equivalent of list-sandboxes; the plural `/ajo/journeys`
is a 404 red herring); each journey is then fetched by id and its audience id +
name extracted.

The wrinkle is the **hybrid credential**. An AJO journey GET checks two things
independently: the Bearer **token** (whose technical account must have AJO
journey permission) and the **`x-api-key`** (which must be *subscribed* to AJO,
else `403 "Api Key is invalid"`). When no single credential has both halves,
mint the token from the credential that has the *permission* and send the
api-key that's *AJO-subscribed* — `--creds` picks the token credential,
`--api-key` (or an `api_key` field in the creds JSON) supplies the subscribed
key. Once an admin enables one credential fully for AJO, drop the hybrid and
just use `--creds`. Read-only, stdlib-only. See
[`ajo_journey_checker - Overview.docx`](ajo_journey_checker%20-%20Overview.docx)
for the full write-up.

Run it **bare** (`python ajo_journey_checker.py`) and it prompts you through the
choices — pick the token credential, then pick the AJO-subscribed credential
for the `x-api-key` — and lists the whole estate. The header names both
credentials (e.g. `Token from: acme alpha … Api-key from: acme beta`) and a
live `[n/N] journey → audience` line prints per journey. `--limit N` samples the
first N; `--xlsx` writes `output/ajo_journeys_<creds>_<sandbox>_<date>.xlsx`
(needs `openpyxl`).

```
python ajo_journey_checker.py                                   # bare: prompts for both creds, lists all
python ajo_journey_checker.py --creds "acme alpha" --api-key <key> --list --limit 10 --xlsx
python ajo_journey_checker.py --creds "acme alpha" --api-key <key> <journeyId> [<journeyId> ...]
```

## audit_batch_schedules_v2.py

Audits the Query Service schedules across an org's sandboxes, then
**classifies and flags** each one. It authenticates, lists every sandbox
(`GET /sandboxes`), and **prompts you to pick which sandbox(es) to audit**
(one, several, or all) before calling `GET /schedules` per sandbox. The base
table is one row per schedule (sandbox, env, enabled, run time UTC, cron,
schedule ID — a fixed `HH:MM` is derived from the cron where it pins a single
daily time, falling back to `startDate` for recurring/wildcard crons; sandboxes
with no schedules still appear, so the table always covers the whole estate),
plus a **Type** and a **Flags** column. Every schedule is typed
`QUERY` (its ID embeds a human-readable query name), `CRON` (UUID-only ID on
a recurring cron with no single daily time), or `SEGMENTATION` (UUID-only ID
at a fixed clock time) — a named query wins over cron, since the name is the
more useful signal. Anomalies are flagged `ODD_TIME` (not on a round or half
hour), `ONCE` (`@once`), `DISABLED` (enabled = false), and `LATE` (a
SEGMENTATION schedule at ≥ 05:00 UTC on a prod sandbox). It closes with a
summary roll-up — counts per type, the prod/dev split for SEGMENTATION, and
the total anomalies.

Outputs land in `./output/`: a flat `batch_schedules_v2_<creds>.csv` **and**
a tabbed `batch_schedules_v2_<creds>.xlsx` workbook — a Summary tab (totals +
a per-sandbox breakdown) plus **one worksheet per sandbox**. This
tabbed-workbook layout is the house style for export-producing tools going
forward. The XLSX needs `openpyxl` (`pip install -r requirements.txt`); if
it's missing, the CSV is still written.

```
python audit_batch_schedules_v2.py            # interactive menus
python audit_batch_schedules_v2.py prod       # pick a credential set by stem
```

## audit_streaming_schedules.py

Catalogues and triages the **streaming audiences/segments** in a sandbox — a
read-only inventory used to plan the move to streaming (HTS) evaluation. Pick a
credential set; by default (`--source=api`) it reads live from AEP, or point it
at a local export with `--source=files` to work offline. It lists each audience
with the detail needed to decide what to migrate, and writes the catalogue to
`./output/`.

```
python audit_streaming_schedules.py                 # interactive: pick a credential set, live from AEP
python audit_streaming_schedules.py my-tenant       # pick by stem
python audit_streaming_schedules.py my-tenant --source=files   # offline, from a local dump
```

## batch_eval_timing.py

Measures how long **batch audience evaluation** actually takes in a sandbox —
read-only timing telemetry for spotting slow or drifting evaluation windows.
Pick a credential set and it reports the timing per the sandbox's batch
evaluation activity.

```
python batch_eval_timing.py                 # interactive: pick a credential set
python batch_eval_timing.py my-tenant       # pick by stem
```

## data_dictionary_v3.py

**Data Dictionary v3.2.** Sucks every XDM schema out of an AEP sandbox, **filters
down to the ones that matter**, and writes a tabbed, **strictly-confidential**
workbook: schema tabs are ready to paste into Claude (via the MCP connector) to
generate **Mermaid ERDs**, and `--data-dict` adds real field coverage + example
values. The successor to the standalone
[`aep_data_dictionary`](https://github.com/mannbarry2/aep_data_dictionary) (v2,
which staged JSON files off an FTP drop) — v3 is **ephemeral, in-memory, and
writes nothing to disk but the workbook**. Pick a credential set, then pick
sandbox(es) — **Enter defaults to prod**.

It lists *every* tenant schema to screen with a per-schema **KEEP / DROP**
verdict, so you can see exactly what was excluded and why:

- **KEEP** — the schema is referenced by ≥ 1 dataset (the UI *DATASETS*
  column; computed by cross-referencing Catalog `/dataSets`) and survives every
  drop rule below.
- **DROP: adhoc** — the canonical ad-hoc class, a per-schema *generated* class
  (id tail is a long hex blob — Adobe Campaign / audience / import dumps, the
  bulk of a tenant), or an auto-created title. This includes the hundreds of
  `Schema for audience…` schemas, which are counted and reported on their own
  line.
- **DROP: ajo** — Adobe Journey Optimizer-managed (`AO`/`AJO` title, or
  `meta:extends` in an AJO namespace).
- **DROP: system** — Adobe-managed plumbing, not the customer's own model:
  Offer Decisioning, CJA Audiences, Audience Portal, Journey Orchestration,
  Unified Profile segment definitions, Adobe Campaign Recipients.
- **DROP: test** — test / placeholder schemas (`test`, `poc`, `sto NNNN`, …).
- **DROP: no-dataset** — nothing ingests into it (unused drafts).

When in doubt the rules drop, not keep — every dropped schema is printed with
its reason, and the per-reason counts (including the `Schema for audience…`
tally) are echoed to screen and written to the Summary tab, so nothing is
silently excluded. All the match lists are constants at the top of the script;
tune them and re-run. (On the large prod sandbox this takes a couple of thousand schemas
down to 27.)

For each kept schema it resolves the **full field list** (dot notation + data
type), and joins the sandbox's **descriptors**: identity fields, relationships
(the ERD edges → target schema), and the **dual labels**
(`alternateDisplayInfo` friendly names). The dual-label count is reported per
sandbox so you can see whether the tenant uses them at all; the *Friendly
Name* column is always present (blank when absent).

The output is a single tabbed workbook in `./output/`,
**`Data Dictionary - <Client> - <YYYY-MM-DD>.xlsx`** (client name from the creds
`client` key, or derived from the filename; the credential set itself is not in
the name). On each run the previous same-client workbook is moved into
`./output/archive/` so the folder only holds the newest. Tabs, in order:
**Summary** (per-sandbox filter stats), **Field Index** (a master list of
*every* field across all schemas — look up an exact dot-notation path with
Ctrl-F; the Tab column says which sheet it's on), **Schemas** (one row per kept
schema), then **one tab per schema**. Each schema tab opens with a title block
(class, dataset count, field count, identities, relationships, modified date,
`$id`) followed by every field — dot-notation path, data type, friendly name,
required flag, identity, and relationship → target. Every sheet is marked
**STRICTLY CONFIDENTIAL** (banner + print header). Paste a schema tab into
Claude to generate that entity's Mermaid ERD. Needs `openpyxl`.

### Data dictionary (`--data-dict`)

Adds field-level **coverage** and **top-5 values** by sampling real ingested
data. AEP has no API that returns a value distribution for a field, and Query
Service `GROUP BY` per field is too slow, so the tool finds the schema's
datasets, downloads recent successful batch files (Snappy-Parquet) via the
**Data Access** API, and tallies locally — **one download covers every field**.
Two columns are added to the schema tab: **Coverage %** (share of sampled rows
where the field is populated) and **Top values (count)** (the five most common
values, pipe-separated). It skips empty batches via `recordCount`, spreads the
sample across the schema's datasets, and streams Parquet through memory —
**nothing is written to disk**; only the sampled rows (≤ the row target) are
held, then dropped.

**Pick an event schema, not Profile.** This works best on **ExperienceEvent**
schemas (Order, Slot, Promotion …): events are immutable, fully-populated
records, so a batch sample is rich (e.g. Order Event → ~19/25 fields populated,
`fulfilmentType = IN_STORE | HOME_DELIVERY …`). **Profile** is a slowly-changing
*merged* entity whose batch feeds are sparse identity deltas, so batch coverage
reads near-empty even though the merged profile is full — measuring Profile
coverage properly needs the Real-Time Customer Profile (Profile Access) store,
not batch files. Defaults to the Acme Profile Schema for safety; in practice
pass an event schema, e.g. `--data-dict="acme order event schema"`.
`--data-dict=all` does every kept schema, `--data-dict=<substr>` matches by
title. Needs `pyarrow` + `tzdata`. See
[`Data Dictionary v3 - Overview.docx`](Data%20Dictionary%20v3%20-%20Overview.docx)
for the design write-up and known limitations.

```
python data_dictionary_v3.py                       # interactive menus
python data_dictionary_v3.py "acme beta"          # pick a credential set by stem
python data_dictionary_v3.py "acme k" --sandbox=prod,dev1
python data_dictionary_v3.py "acme k" --sandbox=all
python data_dictionary_v3.py "acme k" --sandbox=prod --data-dict="acme order event schema"
python data_dictionary_v3.py "acme k" --sandbox=prod --data-dict=all --dd-rows=2000
```
