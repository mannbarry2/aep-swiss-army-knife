![AEP Swiss Army Knife Banner](docs/images/banner.png)

# AEP Swiss Army Knife

A small toolkit of single-file command-line tools for working with Adobe
Experience Platform (AEP) from a locked-down VDI. They're deliberately
lightweight — every tool here today runs on the Python standard library
alone, so it needs no `pip install` — but that's a convenience, not a hard
rule: new tools may pull in a dependency where it genuinely earns its keep.

## What's where

The tools live in **theme folders**. Run any of them from the repo root, e.g.
`python query_service/query_service_templates_export.py`. Each folder has a
README listing its tools in one line each.

| Folder / file | What it holds |
|---|---|
| [`data_dictionary/`](data_dictionary/) | **What exists in a sandbox:** schemas, fields, datasets, audiences, journeys, snapshots, overlap. The Data Dictionary itself (`data_dictionary_v3.py`, at the root for now) joins this folder once its current run has finished. |
| [`platform_access_and_audit/`](platform_access_and_audit/) | **Who can reach the platform and what they do there:** access audit, user activity log, permissions, the role grid, audit-log investigations, and the credential manager. |
| [`query_service/`](query_service/) | **Query Service:** saved query templates (export, rename), scheduled queries, external PostgreSQL clients. |
| [`performance_and_guardrails/`](performance_and_guardrails/) | **How the platform is performing:** the guardrail scoreboard, batch evaluation timing, snapshot timing history, the 8am audience counts. |
| [`ingestion/`](ingestion/) | **Getting data in:** failed batches, and what drove a jump in Profile. |
| [`toolkit_internals/`](toolkit_internals/) | **Internal to this toolkit, not for other people to use:** the plumbing every tool imports: the credential loader and the house-style Excel writer. |
| `aep_creds.py`, `house_xlsx.py`, `snapshot_tables.py`, `overlap_report.py`, `audience_complexity.py` | Temporary forwarding files, only for the Data Dictionary run that was in progress when everything moved (23 Oct 2026). They go when the Data Dictionary moves into its folder. |
| [`RELEASE_NOTES.md`](RELEASE_NOTES.md) / `.html` | Data Dictionary release notes (the HTML edition is the page on GitHub Pages). |
| [`docs/`](docs/) | Everything else to read: [Adobe API gotchas](docs/adobe-api-gotchas.md), the [external access audit](docs/external-access-audit.md), the [Data Dictionary overview](docs/data-dictionary-overview.md), per-tool notes in [`docs/tools/`](docs/tools/), plans in [`docs/plans/`](docs/plans/), images in `docs/images/`. |
| [`parked/`](parked/) | Kept, but not for use: unfinished or one-off work. See its [README](parked/README.md). |
| [`reference/`](reference/) | Reference material, not tools: Craig's original [AJO journey export](reference/ajo-journey-export/) (Node) and the AutoLID / Luma diagrams. |
| `demo/` | The bundled Luma sample data the Data Dictionary can be tried on. |
| `baselines/` | Hand-measured guardrail baselines the guardrail audit compares against. |
| `creds/`, `output/`, `config.json` | Never committed: credentials, generated workbooks and caches, local labels. Old outputs live in `output/archive/`. |

### Renamed on 23 October 2026

Every tool was renamed to say what it does and moved into its theme folder. If you knew the old name:

| Old name | Now |
|---|---|
| `aep_credentials.py` | [`platform_access_and_audit/credential_keyring_manager_and_access_probe.py`](platform_access_and_audit/credential_keyring_manager_and_access_probe.py) |
| `aep_creds.py` | [`toolkit_internals/credential_loader.py`](toolkit_internals/credential_loader.py) |
| `house_xlsx.py` | [`toolkit_internals/house_style_excel_writer.py`](toolkit_internals/house_style_excel_writer.py) |
| `aep_usage_log.py` | [`platform_access_and_audit/user_activity_log_from_audit_events.py`](platform_access_and_audit/user_activity_log_from_audit_events.py) |
| `external_access_audit.py` | [`platform_access_and_audit/external_access_entitlement_and_usage_audit.py`](platform_access_and_audit/external_access_entitlement_and_usage_audit.py) |
| `permissions_audit.py` | [`platform_access_and_audit/who_can_do_what_permissions_audit.py`](platform_access_and_audit/who_can_do_what_permissions_audit.py) |
| `role_permissions_grid.py` | [`platform_access_and_audit/role_to_permission_grid.py`](platform_access_and_audit/role_to_permission_grid.py) |
| `audit_hunt.py` | [`platform_access_and_audit/who_loaded_data_into_datasets_investigation.py`](platform_access_and_audit/who_loaded_data_into_datasets_investigation.py) |
| `babelfish_query_fetcher.py` | [`query_service/query_service_templates_export.py`](query_service/query_service_templates_export.py) |
| `babelfish_query_renamer.py` | [`query_service/query_service_templates_renamer.py`](query_service/query_service_templates_renamer.py) |
| `audit_batch_schedules_v2.py` | [`query_service/query_service_scheduled_queries_audit.py`](query_service/query_service_scheduled_queries_audit.py) |
| `qs_postgres_hunt.py` | [`query_service/query_service_external_postgres_client_finder.py`](query_service/query_service_external_postgres_client_finder.py) |
| `journey_audience_census.py` | [`data_dictionary/journeys_and_audiences_census.py`](data_dictionary/journeys_and_audiences_census.py) |
| `ajo_journey_export.py` | [`data_dictionary/journey_definitions_full_export.py`](data_dictionary/journey_definitions_full_export.py) |
| `audit_streaming_schedules.py` | [`data_dictionary/streaming_audiences_high_throughput_upgrade_triage.py`](data_dictionary/streaming_audiences_high_throughput_upgrade_triage.py) |
| `dd_audience_countdown.py` | [`data_dictionary/data_distiller_audience_expiry_countdown.py`](data_dictionary/data_distiller_audience_expiry_countdown.py) |
| `single_schema_extractor.py` | [`data_dictionary/single_schema_and_sample_data_extractor.py`](data_dictionary/single_schema_and_sample_data_extractor.py) |
| `snapshot_tables.py` | [`data_dictionary/profile_snapshot_export_tables_inventory.py`](data_dictionary/profile_snapshot_export_tables_inventory.py) |
| `overlap_report.py` | [`data_dictionary/profile_dataset_overlap_report.py`](data_dictionary/profile_dataset_overlap_report.py) |
| `audience_complexity.py` | [`data_dictionary/audience_complexity_score.py`](data_dictionary/audience_complexity_score.py) |
| `release_notes_html.py` | [`data_dictionary/release_notes_html_builder.py`](data_dictionary/release_notes_html_builder.py) |
| `guardrail_audit.py` | [`performance_and_guardrails/platform_guardrails_scoreboard.py`](performance_and_guardrails/platform_guardrails_scoreboard.py) |
| `guardrail_audit_pptx.py` | [`performance_and_guardrails/platform_guardrails_scoreboard_slide.py`](performance_and_guardrails/platform_guardrails_scoreboard_slide.py) |
| `dataset_census.py` | [`performance_and_guardrails/profile_enabled_datasets_per_class_census.py`](performance_and_guardrails/profile_enabled_datasets_per_class_census.py) |
| `batch_eval_timing.py` | [`performance_and_guardrails/batch_audience_evaluation_timing.py`](performance_and_guardrails/batch_audience_evaluation_timing.py) |
| `snapshot_history.py` | [`performance_and_guardrails/profile_snapshot_timing_history.py`](performance_and_guardrails/profile_snapshot_timing_history.py) |
| `sluggishness_detector.py` | [`performance_and_guardrails/audience_count_board_real_counts_at_8am.py`](performance_and_guardrails/audience_count_board_real_counts_at_8am.py) |
| `failed_batch_report.py` | [`ingestion/failed_batches_summary_report.py`](ingestion/failed_batches_summary_report.py) |
| `batch_fetcher.py` | [`ingestion/failed_batch_file_downloader.py`](ingestion/failed_batch_file_downloader.py) |
| `profile_jump.py` | [`ingestion/profile_count_jump_investigation.py`](ingestion/profile_count_jump_investigation.py) |
| `audience_renamer.py, adhoc_schema_patch_probe.py` | `parked/ (not for use)` |

`data_dictionary_v3.py` keeps its name and place until its current run finishes, then moves into `data_dictionary/`.

**One credential model for the whole toolkit.** Every tool authenticates the
same way: it reads a per-tenant JSON from `creds/`, lets you pick which set to
use, and mints a fresh OAuth token each run. See **[Get started](#get-started)**
for the shared framework — learn it once and every script below behaves the
same.

The toolkit spans two product ranges:

**Babelfish range** — Query Service template tooling:

| Tool | What it does |
|------|--------------|
| [`query_service/query_service_templates_renamer.py`](query_service/query_service_templates_renamer.py) | Tidies up messy Query Service SQL template names (optionally AI-suggested via Claude) and pushes renames back. |
| [`query_service/query_service_templates_export.py`](query_service/query_service_templates_export.py) | Strict read-only export of Query Service templates to local Markdown + RTF. Never writes back. |

**AEP Swiss Army range** — batch / credential / audit utilities:

| Tool | What it does |
|------|--------------|
| [`platform_access_and_audit/external_access_entitlement_and_usage_audit.py`](platform_access_and_audit/external_access_entitlement_and_usage_audit.py) | Who can reach Experience Platform / AJO, and which of them actually use it — the removal list for access that has outlived its purpose. Read-only, enforced. See **[docs/external-access-audit.md](docs/external-access-audit.md)**. |
| [`platform_access_and_audit/credential_keyring_manager_and_access_probe.py`](platform_access_and_audit/credential_keyring_manager_and_access_probe.py) | **The one credential tool.** Store a credential in the keyring (typed, or imported from a legacy JSON or an Adobe Postman export), check it authenticates, and probe which of 12 Adobe services it can actually reach. Also lists, deletes, migrates and sweeps. **Run this first on any new credential set.** |
| [`ingestion/failed_batch_file_downloader.py`](ingestion/failed_batch_file_downloader.py) | Lists recent batches, then downloads a chosen batch's files locally. |
| [`ingestion/failed_batches_summary_report.py`](ingestion/failed_batches_summary_report.py) | Exports a CSV summary of every batch that failed in the last N hours. |
| [`data_dictionary/journeys_and_audiences_census.py`](data_dictionary/journeys_and_audiences_census.py) | Census of **journeys and audiences** (audiences formerly = segments). `--list` maps every AJO journey to the **audience behind it** (read-audience or qualification); `--audiences` inventories the **whole audience estate** across sandboxes with tags, evaluation type and the segmentation rule (PQL) rendered readable. |
| [`query_service/query_service_scheduled_queries_audit.py`](query_service/query_service_scheduled_queries_audit.py) | Audits every sandbox's Query Service schedules, classifies each (SEGMENTATION / QUERY / CRON), flags anomalies (ODD_TIME, ONCE, DISABLED, LATE), and writes a tabbed XLSX + CSV. |
| [`data_dictionary/streaming_audiences_high_throughput_upgrade_triage.py`](data_dictionary/streaming_audiences_high_throughput_upgrade_triage.py) | Catalogues and triages streaming audiences/segments in a sandbox (read-only) — live from AEP or from a local file dump. |
| [`performance_and_guardrails/batch_audience_evaluation_timing.py`](performance_and_guardrails/batch_audience_evaluation_timing.py) | Measures how long batch audience evaluation actually takes in a sandbox (read-only). |
| [`platform_access_and_audit/user_activity_log_from_audit_events.py`](platform_access_and_audit/user_activity_log_from_audit_events.py) | Who is actively using AEP: per-user **activity** (not sign-ins) from the audit log over the last N days, with the tech-account suppression-list noise excluded and counted separately. |
| [`parked/audience_renamer.py`](parked/audience_renamer.py) | **IN DEVELOPMENT — DO NOT USE.** Parked, unfinished: renaming a development sandbox's audiences to a naming convention. Refuses to run. |
| [`data_dictionary_v3.py`](data_dictionary_v3.py) | **Data Dictionary v3.4.** Sucks out every XDM schema, filters to the ones that matter, and writes a tabbed, *strictly-confidential* workbook: a master field index, one tab per schema (ready for Claude → Mermaid ERDs), a Datasets/SQL-table map, an Audiences tab, with `--data-dict` real field coverage + top-10 example values sampled in-memory (PII starred), and with `--history` (beta) how many months of unbroken data each field has. [Release notes](RELEASE_NOTES.md). |
| [`data_dictionary/release_notes_html_builder.py`](data_dictionary/release_notes_html_builder.py) | Builds `RELEASE_NOTES.html`, the colour edition of the Data Dictionary release notes (laid out like Adobe Experience League release notes), from `RELEASE_NOTES.md`. Re-run it after editing the Markdown. Stdlib only. |

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
python platform_access_and_audit/credential_keyring_manager_and_access_probe.py my-tenant
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

`platform_access_and_audit/credential_keyring_manager_and_access_probe.py` and `query_service/query_service_templates_export.py` also accept `--all`
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
  profile — `platform_access_and_audit/credential_keyring_manager_and_access_probe.py` names this explicitly and tells you the
  fix.
- **Nothing secret is committed.** `creds/*.json` and all generated exports are
  gitignored; only `creds/example.json` is tracked.

### House rules for generated files

Every tool that writes a workbook or deck follows these:

- **Author is Barry Mann ([barrymann.com](https://barrymann.com)).** Set the file's author and
  last-modified-by properties to `SCRIPT_AUTHOR` straight after creating it.
  Left alone, the library signs the file as itself (`openpyxl` /
  `python-pptx`), which is what shows under File > Info in Office.

  ```python
  wb = Workbook()
  wb.properties.creator = wb.properties.lastModifiedBy = SCRIPT_AUTHOR
  ```

- **Data Dictionary style.** Confidential banner, title and italic note, blue
  header row with filters, frozen panes, fixed column widths and uniform row
  heights. A Summary tab first, then the detail tabs.
- **One file, overwritten.** Every tool writes a single workbook with a stable
  name and replaces it on each run: no timestamped copies, no CSV or JSON
  alongside, so `output/` doesn't fill up. The shared writer is
  [`toolkit_internals/house_style_excel_writer.py`](toolkit_internals/house_style_excel_writer.py) (`Book(...).sheet(...).save(...)`): it
  applies the banner, author, header style, filters, frozen panes, widths and
  RAG fills, and wraps an existing workbook when a tool folds its tabs into the
  Data Dictionary. The only non-workbook files left are caches and inputs
  (`snapshot_history_cache/`, `aep_usage_log_cache/`, the guardrail history
  and baseline JSON) and the Data Dictionary's full-PQL sidecar, which exists
  because Excel cannot hold a long rule.

---

# Babelfish range

## query_service/query_service_templates_renamer.py

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
python query_service/query_service_templates_renamer.py                 # interactive: pick a credential set
python query_service/query_service_templates_renamer.py my-tenant       # pick by stem
```

## query_service/query_service_templates_export.py

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
python query_service/query_service_templates_export.py                      # interactive menus
python query_service/query_service_templates_export.py my-tenant --sandbox prod
python query_service/query_service_templates_export.py --all                # every set in creds/
```

# AEP Swiss Army range

## ingestion/failed_batch_file_downloader.py

Pulls a single AEP batch's files down to your local machine. It lists the
most recent batches in the sandbox, you paste (or pick) the batch ID you
want, and the script downloads every file in that batch and surfaces any
embedded error payloads. Replaces three earlier scripts (`auth.py`,
`authandret.py`, `fetchbatch.py`). GBR9 region headers are sent by default
(override via `region` in the creds JSON).

```
python ingestion/failed_batch_file_downloader.py                 # interactive: pick a credential set
python ingestion/failed_batch_file_downloader.py my-tenant       # pick by stem; then choose a batch
```

## ingestion/failed_batches_summary_report.py

Exports a CSV summary of every batch that FAILED in the last N hours
(default 24) in the chosen sandbox — a quick estate-wide health
snapshot. Use `ingestion/failed_batch_file_downloader.py` instead when you need to drill into one
batch and download its failed-record files. Reports are written under
`./output/` (gitignored).

```
python ingestion/failed_batches_summary_report.py                       # interactive: pick a credential set
python ingestion/failed_batches_summary_report.py my-tenant --hours=72 --sandbox=prod
```

## platform_access_and_audit/credential_keyring_manager_and_access_probe.py

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
time, so a fresh token is required). The three layers of access (scopes vs
provisioned products vs technical-account permissions) and the AJO rules are
explained in the tool's own `--help` and in
[docs/adobe-api-gotchas.md](docs/adobe-api-gotchas.md).

```
python platform_access_and_audit/credential_keyring_manager_and_access_probe.py            # interactive menu
python platform_access_and_audit/credential_keyring_manager_and_access_probe.py --all      # validate every set in ./creds/
```

## data_dictionary/journeys_and_audiences_census.py

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
just use `--creds`. Read-only, stdlib-only.

Run it **bare** (`python data_dictionary/journeys_and_audiences_census.py`) and it prompts you through the
choices — pick the token credential, then pick the AJO-subscribed credential
for the `x-api-key` — and lists the whole estate. The header names both
credentials (e.g. `Token from: acme alpha … Api-key from: acme beta`) and a
live `[n/N] journey → audience` line prints per journey. `--limit N` samples the
first N; `--xlsx` writes `output/ajo_journeys_<creds>_<sandbox>_<date>.xlsx`
(needs `openpyxl`).

```
python data_dictionary/journeys_and_audiences_census.py                                   # bare: prompts for both creds, lists all
python data_dictionary/journeys_and_audiences_census.py --creds "acme alpha" --api-key <key> --list --limit 10 --xlsx
python data_dictionary/journeys_and_audiences_census.py --creds "acme alpha" --api-key <key> <journeyId> [<journeyId> ...]
```

## query_service/query_service_scheduled_queries_audit.py

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
python query_service/query_service_scheduled_queries_audit.py            # interactive menus
python query_service/query_service_scheduled_queries_audit.py prod       # pick a credential set by stem
```

## data_dictionary/streaming_audiences_high_throughput_upgrade_triage.py

Catalogues and triages the **streaming audiences/segments** in a sandbox — a
read-only inventory used to plan the move to streaming (HTS) evaluation. Pick a
credential set; by default (`--source=api`) it reads live from AEP, or point it
at a local export with `--source=files` to work offline. It lists each audience
with the detail needed to decide what to migrate, and writes the catalogue to
`./output/`.

```
python data_dictionary/streaming_audiences_high_throughput_upgrade_triage.py                 # interactive: pick a credential set, live from AEP
python data_dictionary/streaming_audiences_high_throughput_upgrade_triage.py my-tenant       # pick by stem
python data_dictionary/streaming_audiences_high_throughput_upgrade_triage.py my-tenant --source=files   # offline, from a local dump
```

## performance_and_guardrails/batch_audience_evaluation_timing.py

Measures how long **batch audience evaluation** actually takes in a sandbox —
read-only timing telemetry for spotting slow or drifting evaluation windows.
Pick a credential set and it reports the timing per the sandbox's batch
evaluation activity.

```
python performance_and_guardrails/batch_audience_evaluation_timing.py                 # interactive: pick a credential set
python performance_and_guardrails/batch_audience_evaluation_timing.py my-tenant       # pick by stem
```

## performance_and_guardrails/profile_enabled_datasets_per_class_census.py

Counts the **profile-enabled datasets** in a sandbox and groups them by the XDM
base class behind each one — the fast answer to "how close am I to the
per-class Profile enablement guardrail?". Pick a credential set and a sandbox
(`--sandbox=all` sweeps every sandbox); it pages the whole Catalog, resolves
each dataset's `schemaRef` against the Schema Registry (cached — datasets share
schemas heavily), and buckets them Profile / ExperienceEvent / Other /
Unclassified, shouting if a guarded class is at or over the threshold.

`Other` is broken down by class (ad-hoc schemas — which mint one throwaway class
per dataset — are collapsed into a single line), and `Unclassified` says *why*:
either the dataset carries no `schemaRef`, or the schema is an Adobe system
schema with no `meta:class`. Read-only; `--csv` writes to `./output/`.

```
python performance_and_guardrails/profile_enabled_datasets_per_class_census.py                              # interactive: creds + sandbox
python performance_and_guardrails/profile_enabled_datasets_per_class_census.py my-tenant --sandbox=prod
python performance_and_guardrails/profile_enabled_datasets_per_class_census.py my-tenant --sandbox=all --csv
python performance_and_guardrails/profile_enabled_datasets_per_class_census.py my-tenant --all-datasets     # census every dataset, not just profile-enabled
python performance_and_guardrails/profile_enabled_datasets_per_class_census.py my-tenant --limit=25 --warn=22   # your guardrail, not the default 20
```

## reference/ajo-journey-export/  *(Node, contributed by Craig)*

A dependency-free **Node 18+** script, `ajo-journey-export.mjs`, that pulls
every **Adobe Journey Optimizer journey definition** — all nodes/steps, the same
content as the UI's *Copy technical details* — for a whole sandbox, and writes
one JSON per journey plus an `index.csv` (node counts, audiences referenced in
`inSegment` / `inAudience` conditions). Read-only.

It is a **UI hack**: it calls `journey-private.adobe.io/authoring`, the private
API behind the AJO canvas, using a user bearer token and org id lifted from the
browser's DevTools and the UI's own `x-api-key` (`voyager_ui`). Service-account
credentials are not entitled for it, the token dies within ~24h, and Adobe can
change the endpoint without notice — so it's for inspection and reporting, not
automation. See [its README](reference/ajo-journey-export/README.md) for the token dance
and the gotchas (lowercase sandbox name, stale list names vs `/latest` names).
Complements `data_dictionary/journeys_and_audiences_census.py`, which uses the supported gateway but
cannot see inside the journey.

```
cd ajo-journey-export
export AJO_BEARER_TOKEN="eyJ..." AJO_ORG_ID="...@AdobeOrg" AJO_SANDBOX=prod
node ajo-journey-export.mjs --out ./export-2026-09
```

**Python port: `data_dictionary/journey_definitions_full_export.py`.** Same endpoint, same raw output
(per-journey JSON + `index.csv` with Craig's columns — the two are
interchangeable), in the knife's house style, **plus a readable workbook**
`output/AJO Journeys - <sandbox> - <date>.xlsx` in the Data Dictionary style:

- **Journeys** — one row per journey: state, description, how it starts
  (schedule / event / audience qualification, in plain English), channels used,
  the audiences it reads **by name** (UUIDs alongside), step counts by type,
  who created / changed / deployed / stopped it and when, and the stale list
  name where duplication left one.
- **Steps** — one row per node in flow order from the start: type, name, a
  plain-English detail (`wait 15 min`, `email message (marketing) -- tracking:
  click, open`, conditions with each branch's rule rendered and the audiences
  named), and where each branch leads. Filter on a journey to read it top to
  bottom.
- **Audiences** — every audience any journey references: name, description,
  evaluation (Batch / Streaming / Edge), lifecycle, UUID, which journeys use it.
- **Summary** and **Failed** (journeys the list knew but `/latest` could not
  return — abandoned duplications, typically).

Audience names come from the audiences API, which *does* accept the keyring
credential; only the journey definitions need the browser token. What does
**not** change is that authentication: this endpoint only accepts a *user*
session token lifted from the browser, so you paste one in each run (masked
prompt, `AJO_BEARER_TOKEN`, or `--token=`) — the script prints the DevTools
steps at the prompt and again on a 401/403. A screenshot of exactly where the
token sits in DevTools is still to be added (it needs redacting first).

```
python data_dictionary/journey_definitions_full_export.py aep-prod --sandbox=prod            # paste token at the prompt
python data_dictionary/journey_definitions_full_export.py aep-prod --sandbox=dev --journey=<uid>
python data_dictionary/journey_definitions_full_export.py --org-id=...@AdobeOrg --sandbox=prod --token=eyJ...
python data_dictionary/journey_definitions_full_export.py aep-prod --from-dir=output/ajo-journeys_dev_2026-09-25   # rebuild the workbook, no token
python data_dictionary/journey_definitions_full_export.py aep-prod --sandbox=prod --no-xlsx  # Craig's raw output only
```

**Known limit — lineage.** Because this is scraped from the UI's private API,
a journey's audience references are UUIDs that the workbook resolves to names,
but they do not join through to the rest of the Data Dictionary's lineage
(schema → dataset → audience → journey) the way the supported endpoints do.
**Where this is heading:** an extra tab at the end of the Data Dictionary
workbook, with the dictionary's workflow asking for the browser token when you
want journeys included.

## performance_and_guardrails/platform_guardrails_scoreboard.py  *(beta)*

**AEP Guardrail Estimator.** Re-measures the guardrail scoreboard for a sandbox
straight from the APIs — the monthly "are we still inside Adobe's guardrails?"
check that used to be done by hand — and writes a before/after one-pager.
Read-only. Every check is tagged by *how* it can be measured:

- **api** — measured on every run: profile-enabled datasets per class (customer
  scope by default; `--include-system` counts Adobe's hidden AJO/Journey
  datasets too), multi-entity relationships, largest audience as % of the
  profile base, total / streaming / edge / batch audiences with a growth
  forecast, Profile store size (customer and all), batches per day into
  Profile-enabled feeds, batch ingest throughput, hard size-limit drops over 30
  days, and streaming ingestion RPS. Streaming is separated from batch using
  Catalog's own marker: streaming micro-batches are tagged
  `acp_workflow=ValveWorkflow`, real batch loads are not.
- **query** — events per profile and identities per graph need a Query
  Service pass over the data; the tool leaves the row *pending* and puts the
  SQL to run in the JSON (deliberately not run — it costs compute).
- **manual** — Edge segmentation throughput and Adobe's new capacity metric
  have no API surface; carried forward.

Each run writes `output/guardrail_audit_<sandbox>_<date>.md` (scoreboard with
RAG: red over the guardrail, amber within 20%, green clear, grey not measured),
a `.json` with the detail behind every number, and appends to
`output/guardrail_audit_history_<sandbox>.json` so the next run shows the
previous one as its "before" column. A hand-measured baseline in
`baselines/guardrail_baseline_<sandbox>.json` is shown as extra columns when
present. Guardrails are Adobe's published defaults in `GUARDRAILS` — edit them
if your contract differs.

```
python performance_and_guardrails/platform_guardrails_scoreboard.py aep-prod --sandbox=prod
python performance_and_guardrails/platform_guardrails_scoreboard.py aep-prod --sandbox=prod --days=7 --include-system
python performance_and_guardrails/platform_guardrails_scoreboard.py aep-prod --sandbox=prod --no-batches     # skip the batch scans
python performance_and_guardrails/platform_guardrails_scoreboard_slide.py                                     # one-slide deck from the latest audit
```

`performance_and_guardrails/platform_guardrails_scoreboard_slide.py` (needs `python-pptx`) turns the latest audit JSON
into a single-slide **scoreboard deck** in `output/`: RAG dot, check, how things
stood at the start of the project (first baseline column), and Now on the right.
`--all-columns` adds every baseline column and the previous run.

## platform_access_and_audit/user_activity_log_from_audit_events.py

**Who is actively using AEP**, per person, over the last N days (default 90),
read from the Audit events API. It replaces the UI export, which stops at
10,000 events and is swamped by a tech account deleting from the AJO
Suppression List. The report says **activity, not sign-ins**: an audit event is
something a user did, so someone who logged in and did nothing won't appear.

How the API actually behaves, which the tool is built around:

- **1,000-event cap.** A query returns at most 1,000 events, and
  `totalElements` stops at 1,000 too. Each day is queried as one window, and
  any window at the cap is split (1 h → 10 min → 1 min → 10 s → 1 s) until
  every piece is under it.
- **Noise.** The suppression-list account is about 90% of all events (41,047
  of 44,924 on 30 Sep 2026). It is detected automatically and excluded
  server-side (`user!=`). Its suppression deletes are counted only (fetching
  them would be millions of rows), and its other work is fetched. Every
  tech-account event lands in the `excluded_noise` sheet, by day and asset
  type.
- **Core events only.** Enhanced events are nested inside each Core event, so
  each audit action is counted once.

Workbook `output/aep_usage_log_<sandbox>_<YYYYMMDD>.xlsx`, with sheets
`summary`, `raw_events`, `by_user`, `by_user_month`, `by_assetType_action` and
`excluded_noise`. A 90-day run takes roughly 35–40 minutes, most of it counting
the noise; `--no-noise-count` skips that. Read-only.

```
python platform_access_and_audit/user_activity_log_from_audit_events.py                         # aep-prod, prod, last 90 days
python platform_access_and_audit/user_activity_log_from_audit_events.py --days=30
python platform_access_and_audit/user_activity_log_from_audit_events.py --days=90 --no-noise-count
```

## parked/audience_renamer.py  *(in development — do not use)*

> **Parked 2026-10-02.** This project was stopped part-way and is kept only so
> it can be picked up later. It is not a working tool: it refuses to run, and
> nobody should use it. What follows describes what it was meant to do.

The `query_service_templates_renamer` idea, for audiences: gives every rule-based
audience in a sandbox a friendly name built from a naming convention. It does
no fetching of its own — it **follows on from a Data Dictionary run** and reads
that run's PQL sidecar and workbook (Audiences tab, Field Index friendly names).

Each audience gets three header terms — `prefix A`, `prefix B` (placeholders
until the real convention arrives) and a short **description written by
Claude** from the audience's current name, its rule and the dictionary's
friendly names. Descriptions are kept in
`output/audience_renamer_<sandbox>_descriptions.json` and reused until a rule
changes. Audiences with no rule (uploads, Data Distiller) are left alone.

Dry run by default; `--apply` renames in AEP, **development sandboxes only**.
Old names are saved to `output/audience_renamer_<sandbox>_previous_names.json`
first, and `--restore` puts them back.

**Where it was left — not proven end to end:** the rename and restore calls have never
been sent to AEP, and the tool's own Claude step has not been run (it needs
`pip install anthropic` and an Anthropic API key; a key that is not scoped to a
workspace also needs a workspace id, which the tool does not send yet). The dev
descriptions on file were written in a Claude Code session instead.

```
python audience_renamer.py        # refuses: "IN DEVELOPMENT and parked -- it is not for use"
```

## platform_access_and_audit/who_can_do_what_permissions_audit.py

Who can do what in AEP, and how they got it. Reads the Access Control API —
every role with its permission sets, sandboxes and labels, and every subject
in each role (users, user groups exploded to their members, API credentials)
— and flattens it to one row per subject × role × permission set, so "who has
manage-datasets", "who can see any dashboard", "what can this person do" and
"who is in this role" are each a filter. Also lists the audit-log events on
roles and permissions for the last `--days`. One workbook,
`output/permissions_<org>.xlsx`: *Who has what*, *Permission sets* (the
catalogue, with how many roles and people hold each), *Roles*, *Role members*,
*Role changes*. The console prints the answer to the filter you gave.

Names: the Access Control API returns IMS ids, not emails. They are resolved
through Adobe's User Management API when the credential is entitled to it
(add the **User Management API** to the integration in the Developer Console
if the run says the directory is unavailable), and every name learned is kept
in `output/user_directory_cache.json` so it survives a day the directory
refuses. Group ids stay numeric until the same API can name them.

```
python platform_access_and_audit/who_can_do_what_permissions_audit.py --permission=manage-datasets
python platform_access_and_audit/who_can_do_what_permissions_audit.py --permission=dashboard --sandbox=prod
python platform_access_and_audit/who_can_do_what_permissions_audit.py --category="Data Management"
python platform_access_and_audit/who_can_do_what_permissions_audit.py --user=someone@tesco.com
python platform_access_and_audit/who_can_do_what_permissions_audit.py --role="Sandbox Administrators" --days=90
```

## data_dictionary/profile_dataset_overlap_report.py  *(folded into the Data Dictionary)*

The Profile store's **dataset overlap report**, turned from a wall of
comma-separated dataset ids into something readable. Every row of that report
is the set of profiles built from EXACTLY one combination of datasets, so the
rows are mutually exclusive and sum to the profile count. Two tabs:
**Overlap cut-list** — the profiles ONLY one dataset contributes (expire it
and you save exactly that many), with a running total against the Addressable
Audience licence and the point where an overage would clear — and **Overlap
full report**, every combination, largest first. The full report is also how
you see which *route* a jump in profiles came in by: a combination that was
tiny last week and huge today is a new route (the 3.1M shell profiles of 5–6
Oct 2026 showed up as "Tesco Profile Dataset only"). `--date=YYYY-MM-DD`
reads an earlier day's report for a before/after. Output is one workbook,
`output/overlap_<kind>_<sandbox>.xlsx`, overwritten each run; the same tabs
are folded into the Data Dictionary (v3.4.7), so this is the way to run just
the overlap report on its own. `probe` tests which preview-sample-status
endpoints answer and writes `overlap_probe_<sandbox>.xlsx`.

```
python data_dictionary/profile_dataset_overlap_report.py dataset aep-prod --sandbox=prod
python data_dictionary/profile_dataset_overlap_report.py dataset aep-prod --sandbox=prod --date=2026-10-04
python data_dictionary/profile_dataset_overlap_report.py identity aep-prod --sandbox=prod       # namespace distribution
```

## data_dictionary/audience_complexity_score.py  *(beta, folded into the Data Dictionary)*

A crude 0-100 **complexity score** for an AEP audience, read off its PQL and
the audience record, with a RED / AMBER / GREEN on top. Not all audiences are
created equal: one badly built audience can double the nightly batch run, and
AEP never reports what an audience cost to compute. The weights follow the
miaprova.com write-ups on expensive segments: event scan and how far back it
looks (an unlimited scan scores highest), lookup joins to another class
(products via gtin), aggregations that cannot short-circuit (sum / count /
"occurs N times"), and the size of the base audience it depends on; plus
sequences, condition count, huge value lists and multiple merge policies.
Pure functions, no network: `score_audience(audience, pql_tree,
base_populations)` returns the score, the RAG, a plain-English *why* and the
raw features. The Data Dictionary calls it for every rule-based audience and
puts the results in the Audiences tab. Edit the weights at
the top of the file.

## data_dictionary_v3.py

**Data Dictionary v3.4.** Sucks every XDM schema out of an AEP sandbox, **filters
down to the ones that matter**, and writes a tabbed, **strictly-confidential**
workbook: schema tabs are ready to paste into Claude (via the MCP connector) to
generate **Mermaid ERDs**, and `--data-dict` adds real field coverage + example
values. See the **[release notes](RELEASE_NOTES.md)** for what changed in each
version. The successor to the standalone
[`aep_data_dictionary`](https://github.com/mannbarry2/aep_data_dictionary) (v2,
which staged JSON files off an FTP drop) — v3 is **ephemeral, in-memory, and
writes nothing to disk but the workbook**. Pick a credential set, then pick
sandbox(es) — **Enter defaults to prod**.

It lists *every* tenant schema to screen with a per-schema **KEEP / DROP**
verdict, so you can see exactly what was excluded and why:

- **KEEP** — the schema is referenced by ≥ 1 dataset (the UI *DATASETS*
  column; computed by cross-referencing Catalog `/dataSets`) and survives every
  drop rule below.
- **DROP: adhoc** — the schema extends the ad-hoc behaviour
  (`xdm/data/adhoc`: Adobe Campaign / audience / import dumps, the bulk of a
  tenant), or has an auto-created title. This includes the hundreds of
  `Schema for audience…` schemas, which are counted and reported on their own
  line. A custom class built in the UI also gets a hex id, but extends
  *record* or *time-series* — those (the product, store and promotion
  lookups) are **kept**.
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
silently excluded. Since v3.4.9 every dropped schema is also in the workbook,
on a **hidden `Dropped Schemas` tab** (right-click any tab › Unhide): the
reason in plain English, how many datasets use it and their SQL table names,
with the schemas that still hold data listed first. If a table you query has
no tab, filter that tab's table-name column and it says why. Since v3.4.10 a
second hidden tab, **`Dropped Field Index`**, lists every field of every
dropped schema (dot-notation path, type, display name, the schema, why it was
dropped and its tables) so the workbook is a full census: a field that isn't
in the Field Index is still findable. Inventory only, with no coverage
sampling. In prod that is about 244,000 rows and five or six extra minutes
of schema fetches. All the match
lists are constants at the top of the script; tune them and re-run. (On the
large prod sandbox this takes a couple of thousand schemas down to a few
dozen.)

For each kept schema it resolves the **full field list** (dot notation + data
type), and joins the sandbox's **descriptors**: identity fields, relationships
(the ERD edges → target schema), and the **friendly-name labels**
(`alternateDisplayInfo`). The friendly-label count is reported per
sandbox so you can see whether the tenant uses them at all; the *Friendly
Name* column is always present (blank when absent), alongside a *Friendly
Path* (display names from the root down, e.g. *Person > Full name > First
name*) and a *Description*; schemas, datasets and audiences carry their
descriptions too. These are not **DULE**
labels — Data Usage Labeling and Enforcement, Adobe's governance framework
for object and data access control (C8, C12 …) — which the workbook does
not yet carry.

The output is a single tabbed workbook in its own folder,
`./output/data_dictionary/`,
**`Data Dictionary - <Client> - <YYYY-MM-DD>.xlsx`** (client name from the creds
`client` key, or derived from the filename; the credential set itself is not in
the name). On each run the previous same-client workbook is moved into
`./output/data_dictionary/archive/` so the folder only holds the newest. Tabs,
in order: **Summary** (per-sandbox filter stats), **How to Use**, **Field
Index** (a master list of *every* field across all schemas — look up an exact
dot-notation path with Ctrl-F; the Tab column says which sheet it's on),
**Schemas** (one row per kept schema), **Datasets** (friendly name → SQL table
name), **Audiences** (every audience, its rule, and — new in v3.4.6 — its
**complexity score**, see below), **Snapshot Summary** / **Snapshot Tables**
(folded in from `data_dictionary/profile_snapshot_export_tables_inventory.py`), **Overlap cut-list** / **Overlap full
report** (folded in from `data_dictionary/profile_dataset_overlap_report.py`, v3.4.7: which datasets the
profiles are built from, what expiring one would save, and — because a sudden
new combination is a new route in — where a jump in the profile count came
from), then **one tab per schema**. Each schema tab opens with a title block
(class, dataset count, field count, identities, relationships, modified date,
`$id`) followed by every field — dot-notation path, data type, friendly name,
required flag, identity, and relationship → target. Every sheet is marked
**STRICTLY CONFIDENTIAL** (banner + print header). Paste a schema tab into
Claude to generate that entity's Mermaid ERD. Needs `openpyxl`.

### Audiences: full PQL sidecar and the complexity score (beta)

The **Audiences** tab holds every audience in the sandbox with its tags, who
built and last changed it, and its segmentation rule rendered readable from
Adobe's `pql/json` syntax tree, with the raw tree alongside. Two things were
added in v3.4.5 / v3.4.6:

- **Full PQL sidecar.** Excel caps a cell at 32,767 characters and a rule with
  a hard-coded store list can pass that, so the sheet alone cannot be the
  audit copy. Every audience's complete rule is written to
  `./output/data_dictionary/pql/audiences_pql_<sandbox>_<yyyymmdd>.jsonl`
  (one JSON line per audience: `audience_id`, `audience_name`, `format`,
  `raw_pql`, `readable_pql`). Three columns say what happened in the sheet:
  *PQL length*, *PQL truncated in sheet* (yes/no, red when yes) and *PQL
  format* (`json`, or `pql-text` for a rule held as PQL text — complete, but
  not JSON). A cut cell ends with `…[TRUNCATED – see sidecar]`.

- **Complexity score (beta).** Not all audiences are created equal: one badly
  built audience can double the nightly batch run on its own, and AEP never
  reports what an audience cost to compute — only how many profiles qualified.
  So every rule-based audience gets a **0–100 score** and a **RED / AMBER /
  GREEN**, read off its definition, with the reasons in plain English (*Why
  this score*) and the raw features in their own columns: event scan,
  lookback days, lookup joins, joined fields, aggregations, how many audiences
  it depends on, the largest base audience and its size, sequence steps,
  conditions, longest hard-coded value list, merge policies, and how many
  audiences use it. The formula, generated from the weights in the code, is
  printed in full at the top of the tab. The rules follow the miaprova.com
  write-ups on expensive AEP segments (*Why your most expensive AEP audience
  might be your newest one*; *The right fix for expensive AEP segments: move
  the math upstream*):

  | Cost driver | Why it costs | Points |
  |---|---|---|
  | Event scan and lookback | Events are read per profile, per night; 720 days is ~2× the work of 365, 4× of 180. A scan with **no** time limit reads the whole history. | no limit 35; ≤30 d 6, ≤90 d 12, ≤180 d 18, ≤365 d 25, ≤730 d 30, longer 33 |
  | Lookup joins | A field reached through a relationship (products via gtin) is resolved per event, per profile — "potentially millions of times across the job run". | first join 18, +4 per further class or field, max 25 |
  | Aggregations | `sum()` / `average()` / `count()` cannot short-circuit: every qualifying event is gathered before the threshold test. "Occurs at least N times" is a count in disguise. | sum/avg/min/max 15, count 10, occurs-N 6, forall 4, max 20 |
  | Base audience size | `inSegment()` on a big audience means every one of those profiles gets the full scan. | 2 per dependency (max 6) + base ≥1M +3, ≥5M +6, ≥20M +9 |
  | Breadth / other | Ordered event sequences, very many conditions, lists of 100+ values, more than one merge policy, a negated event step, Adobe's performance warning overridden at save. | small amounts, capped |

  RED is 55 and over, AMBER 30 to 54. The scoring is pure functions in
  [`data_dictionary/audience_complexity_score.py`](data_dictionary/audience_complexity_score.py) (developed standalone,
  folded in); the weights are constants at the top of that file and are a
  first cut to be argued with. The usual fix for a RED is to shorten the
  lookback, or move the sum / count upstream into a computed attribute and
  test that instead. First prod run: 2,205 rule-based audiences — 64 RED, 482
  AMBER, 1,659 GREEN; every RED was an unlimited event scan + a product lookup
  join + a base audience of 13–23 million profiles. The size of each base
  audience is read from `/segment/definitions/<id>` once per run (a few
  hundred small reads; ~70 s on prod).

### Data dictionary (`--data-dict`)

Adds field-level **coverage** and **top-10 values** by sampling real ingested
data. AEP has no API that returns a value distribution for a field, and Query
Service `GROUP BY` per field is too slow, so the tool finds the schema's
datasets, downloads recent successful batch files (Snappy-Parquet) via the
**Data Access** API, and tallies locally — **one download covers every field**.
Two columns are added to the schema tab: **Coverage %** (share of sampled rows
where the field is populated) and **Top values (count)** (the ten most common
values, pipe-separated; email addresses and identifier / contact fields such as
user ids have two characters starred, DULE label or not). It skips empty
batches via `recordCount`, spreads the
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
title. Needs `pyarrow` + `tzdata`. See the
[Data Dictionary overview](docs/data-dictionary-overview.md) for the design
write-up and known limitations.

```
python data_dictionary_v3.py                       # interactive menus
python data_dictionary_v3.py "acme beta"          # pick a credential set by stem
python data_dictionary_v3.py "acme k" --sandbox=prod,dev1
python data_dictionary_v3.py "acme k" --sandbox=all
python data_dictionary_v3.py "acme k" --sandbox=prod --data-dict="acme order event schema"
python data_dictionary_v3.py "acme k" --sandbox=prod --data-dict=all --dd-rows=2000
```

A full prod run takes the best part of an hour, so on Windows the script holds
the PC awake (system and screen) until it finishes, and releases the hold
however it ends (v3.4.11). Nothing in the power settings changes; closing the
lid still sleeps.

### Field history (`--history`, beta)

**"Have we got it?"** How many months of *unbroken* data each field has, up to
now — what you need to know before building an audience on it (the slot fields,
repaired in September 2026, read "<1"; order fields go back to April 2025).
Querying each field month by month in Query Service would take forever, so for
each dataset and month Catalog finds that month's batches and one is read
through the Data Access API, the same reader as `--data-dict`. One download
answers for every field, so the cost is datasets × months, not fields × months.
A month that looks thin, odd, or is missing a well-filled field both neighbours
have is sampled again from earlier in the month before it is believed.

Three columns go on the end of the Field Index and every schema tab: **Have we
got it? (months)**, a plain-English **detail** (`since 28 Sep 2026`, `stopped --
last seen Aug 2026`, `since May 2026 -- missing in Apr 2026`) and a
**heartbeat**, one bar per month, oldest first: `░` no data at all, `▁` data
arrived but the field was empty, `▂`–`█` how often it was filled, `?` could not
be checked. A **Field History** tab has the months as columns, shaded green.

Test, perf and temp datasets are left out, so they can't make a field look
older than the real feed. Months go by *load* date. Default 24 months
(`--history-months=N`), capped by each dataset's TTL; `--history=<substr>`
limits it to schemas whose title matches. Finished months are cached in
`output/field_history_cache/` (local, never synced), so only the first run on a
sandbox is long — hours on a big one — and later runs read the current month.

```
python data_dictionary_v3.py "acme k" --sandbox=prod --data-dict --history
python data_dictionary_v3.py "acme k" --sandbox=prod --history=order --history-months=36
```

### Check by hand (SQL)

The last column of the Field Index and every schema tab, **SQL to check it**
(under the band *Check it yourself*), holds one read-only Query Service statement for that field, on one line,
ready to copy into the Query Service editor and run (v3.4.21). It reads the data
itself, not through the dictionary's batch sampling, so it is the way to confirm
or question any coverage or history figure:

- **event fields** — records, records carrying the field, and the % filled,
  per *event* month (`timestamp`) in each of the schema's dataset tables, from
  the start of the history window;
- **Profile record fields** (a Profile-enabled XDM Individual Profile schema) —
  the same three numbers across the default merge policy's Profile snapshot
  table: what an audience sees now;
- **anything else** (lookups, record data not in Profile) — the three numbers
  across all of each table's records in the data lake; a record has no date.

Each statement opens with a `/* Data Dictionary: … */` comment, so whoever reads
the Query Service log can see what it was. On a very large event table, shorten
the window (the `timestamp >=` date) if Query Service times out.

### How the workbook is laid out

It goes to people who do not work in AEP, so a finishing pass tidies every tab
before it is saved (v3.4.22): column widths fitted to what is in them, header
rows tall enough for their words, descriptions wrapped, long notes wrapped
rather than running off the screen, and thousands separators. On the Field
Index and every schema tab, a coloured band above the header names each group
of columns (*The field*, *In the data now*, *How far back it goes (beta)*,
*Check it yourself*). Colour is used only where it means something:
coverage runs red to green, months and Profile days shade deeper the more
there are, audiences carry their RED / AMBER / GREEN. Ids, raw rule text and
other technical columns are folded away behind a **+** above the column
letters. A schema's name in a **Tab** column is a link to its tab, and each
schema tab links back to the list.
