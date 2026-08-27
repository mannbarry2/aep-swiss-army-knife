# Data Dictionary — release notes

Version history for [`data_dictionary_v3.py`](data_dictionary_v3.py), newest
first. Every workbook the tool writes carries its version, the commit that
produced it, and a link back to this file, so a dictionary found months later
can be traced to exactly what the code did at the time.

---

## v3.8 — 2026-08-27

**Readability: measured column widths, and a clearer filename.**

- **Columns are sized to what is in them.** The widths were a list of numbers
  guessed once and never revisited, so every sandbox got the same layout
  whatever its data — a column of two-character codes sitting 20 wide while a
  column of long dataset names was cut off. Both cost the reader the same
  thing: dragging column edges before they can read the sheet. Widths are now
  measured from the content of each column in each workbook.
- **Sized to the 90th percentile, not the maximum.** One freak value — a single
  merge-policy GUID in a column of the word `Profile-enabled` — would otherwise
  drag a column out to its cap and push everything after it off screen. Fitting
  the 90th percentile and letting the few outliers overflow is what someone
  widening one column by hand would have done anyway. On a live sandbox this
  took the Field Index from 315 characters of total width to 228.
- Widths come from the content, not the header, since a long title over short
  values is the classic way a column ends up three times wider than it needs to
  be. Headers already wrap, so they lose nothing.
- **Files are now `AEP Data Dictionary - <sandbox> - <date>.xlsx`.** They land
  in a shared folder next to dictionaries for other systems, and
  `Data Dictionary - prod` alone does not say whose. The archive sweep still
  recognises the old prefix, so upgrading does not strand the previous run in
  the output folder.

---

## v3.7 — 2026-08-25

**The Journeys half of the audience census, finally merged in.**

v3.3 lifted the *audiences* half of `journey_audience_census.py` into the
dictionary. The *journeys* half never came across, so the workbook could say
who was being targeted but not what went and targeted them — and there was no
way to answer the question people actually ask, which is *"is this audience
still used by anything?"*

Both halves now live here, self-contained. The dictionary no longer needs the
census script for anything.

- **New Journeys tab.** Every Journey Optimizer journey in the sandbox and the
  audiences it acts on — one row per journey-audience link, so it filters from
  either end. Carries status, version, tags, and full authorship (created /
  last modified / last deployed, by whom and when — the AJO list resolves actor
  names itself, unlike the audiences API). `Via` distinguishes *read_audience*
  (the journey reads the audience as a batch) from *qualification* (a profile
  entering the audience triggers the journey).
- **The Audiences tab reads the link the other way.** Two new columns, *Used in
  journeys* and *Journeys using it*, and audiences a journey depends on now
  sort to the top. That is the schema → dataset → audience → journey chain
  closed in one workbook.
- **No new API cost for the join.** The census paid one call per audience for
  its evaluation type and one per tag for its name. Both are already loaded
  here for the Audiences tab, so the journey join is done in memory.
- **The expensive half is budgeted, like sampling.** Audiences and tags live
  only on the individual journey resource, ~2.3s each — over an hour on an
  1,800-journey estate. The listing is cheap and always runs, so every journey
  reaches the tab; the detail pass then works through them **deployed journeys
  first**, under `--journey-budget` (default 45 min). Every row's `Detail`
  column says `read`, `NOT READ (budget)` or `unreadable`, so a blank Audience
  is never mistaken for "this journey targets nobody" — and the Audiences tab
  says the same about a zero usage count.

New flags: `--no-journeys`, `--journey-budget=<minutes>` (0 = no ceiling).

Note: journeys need a credential whose API key is subscribed to Journey
Optimizer. An unsubscribed key returns 403 and the tab says so rather than
appearing empty.

---

## v3.6 — 2026-08-21

**Stop guessing which schemas matter. Gate on evidence instead.**

The filter deciding what went into the workbook rested on a rule written at the
very beginning and never revisited: any schema whose `meta:class` id ended in a
long hex blob was assumed to be an auto-generated ingestion dump. It was a
reasonable first guess that quietly became load-bearing — and it is wrong.
Adobe hex-ids **every** tenant-created custom class, so a hand-modelled lookup
schema sitting on a shared custom class looked exactly like an import dump.

On a live prod tenant that rule dropped 20 modelled schemas — most of the
estate's lookup tables among them — from the Field Index. Their fields were
simply absent, with nothing anywhere to say they were missing. Of 2,495 schemas
and 850 datasets, **36 schemas survived the filter.**

- **Adobe already labels this, and we were ignoring it.** `schemaType` is
  `standard` for a modelled schema and `adhoc` for a generated one;
  `meta:tenantNamespace` vs `meta:datasetNamespace` says the same thing again.
  Those are read first now. The hex-class heuristic survives only as a last
  resort, for records carrying neither marker.
- **KEEP/DROP became `--width`, and taxonomy became a column rather than a
  gate.** Every dataset's batch history is read to establish which ones
  actually hold data, and *that* — not what a schema looks like — decides what
  the workbook covers. `core` is the modelled data model only; `wide` (the
  default) is everything whose datasets hold records, whoever created them;
  `all` adds never-written datasets and schemas with no dataset. Where the
  evidence is merely inconclusive the schema is kept, not dropped: a spurious
  extra schema costs seconds, a missing one costs an afternoon.
- **The Datasets tab is now the honest inventory.** Every dataset in the
  sandbox, in or out, with **Included YES/NO**, its tier and kind, recent
  record count, when it was last written to, and the reason it is in or out.
  Filter `Included = NO` to see precisely what the workbook does not cover.
- **Widening is nearly free, so the Field Index no longer gates at all.** A
  full-schema fetch is ~0.15s; the whole schemas-with-datasets population of a
  large prod tenant resolves in about two minutes. Tabs are the scarce
  resource, not fields, so they are capped (`--max-tabs`, default 120) and
  everything else is index-only — the Field Index is what anyone actually
  searches.
- **Sampling is prioritised and budgeted.** `--data-dict` now works in priority
  order — the data model first, then most-recently-written, then largest — under
  a wall-clock ceiling (`--dd-budget`, default 180 minutes). Whatever it does
  not reach is named in the log and marked **NOT SAMPLED** in the workbook,
  never reported as 0% coverage.

New flags: `--width=core|wide|all`, `--dd-budget=<minutes>` (0 = no ceiling),
`--max-tabs=<n>`, `--no-activity` (skip the batch sweep; behaves as
`--width=all`).

---

## v3.5 — 2026-08-17

**An Ingestion tab: how each dataset actually gets populated.**

The workbook could say what the data *is* (schema tabs) and where it *lives*
(Datasets tab), but nothing said how it *got there* — and that changes what the
numbers mean. A field sitting at 0% coverage read identically whether its
dataflow was disabled, failing every night, or was a one-off file upload two
years ago.

Every dataset is now traced through Adobe's **Flow Service** to the dataflow
that writes it, with a plain-English verdict per dataset: *Manual file upload*,
*Query Service output (Data Distiller)*, *Source connector — azure-blob*,
*Streaming — Web/Mobile SDK (datastream)*, *No batches ever ingested*, and so
on — plus the source connector, the dataflow name, its schedule, its state and
how its last run went.

- **The join is `targetConnection → dataset`, and it has two shapes in the
  wild.** `params.dataSetId` (sources) and `params.datasets[].datasetId`
  (Query Service) — and the Query Service one sometimes carries the SQL *table*
  name instead of a dataset id. All three keys are matched, which resolves the
  large majority of a real estate; matching only the documented `dataSetId`
  would have quietly mislabelled every Data Distiller output as unknown.
- **Datasets no dataflow writes fall back to batch history** — the ingesting
  client on their newest batch. That is what answers the AJO and Decisioning
  system datasets, which have no dataflow at all.
- **Problems sort to the top**: failed last run, then disabled dataflow, then
  never-written-to. The first live run surfaced a run of failing dataflows and
  a set of datasets that have never received a batch at all — none of which
  were visible anywhere in the workbook before.
- Six paged list calls per sandbox, joined in memory — not one call per
  dataset. Only the no-dataflow tail costs a call each, capped at 400 and
  **stated on the tab** when it truncates.
- Degrades rather than fails: a credential without the Sources permission gets
  a batch-history-only tab that says so in red, not a missing tab.
- Skip the sweep entirely with `--no-ingestion`.

**Top values went from 5 to 10.** On a coded field — status, channel, country,
event type — the tail is where the surprises are, and five was routinely cut
off mid-distribution. The tally already counted every value, so this costs
column width, not runtime. Tunable via `DD_TOP_N`.

**Fixed: every tab was reporting the wrong sandbox.**

The sandbox column on Summary, Schemas, Datasets and Audiences — and the
`sandbox:` line in each schema tab's header block — showed *Timestamp* instead
of *Prod*.

`collect_sandbox` held the sandbox title in a local called `title`, and the
per-field loop inside it also unpacked into `title`. A Python for-loop leaks its
variable into the enclosing scope, so by the time the function returned, the
sandbox title had been overwritten by **the display name of the last field of
the last kept schema** — which happened to be `Timestamp`. Every workbook this
tool has written since the sandbox column was added has carried a field name
where the sandbox should be. The loop variable is now `fld_title`.

**Fixed: daily schedules were shown as raw cron.** AEP writes `*/1` where `*`
would do (`0 4 */1 * *` for an 04:00 daily), which fell straight through the
humaniser. `*/1` is now normalised to `*` before matching, so the commonest
schedule in the estate reads as *daily at 04:00 UTC*. Unrecognised expressions
are still shown raw — better a cron a reader can look up than a confident wrong
guess.

---

## v3.4.1 — 2026-08-14

**Complete coverage on Profile schemas.**

A prod run came back with 16 of 33 schema tabs flagged `MISSING`. All 16 were
Profile-class, and all 16 failed for one reason: a single gateway 504.

Every Profile-class schema samples the *same* Profile Snapshot Export union —
but each one re-downloaded the ~127 MB partition for itself. That was both
slow (sixteen downloads of identical data) and fragile (sixteen independent
chances to hit a 504). The `snap_unreadable` cache added in v3.2 kept the
runtime sane by refusing to retry a snapshot that had already failed, but it
also meant **the first failure became MISSING coverage for every Profile schema
behind it**.

- The snapshot is now sampled **once per sandbox** and the rows reused in
  memory for every Profile-class schema. One download, not sixteen.
- A **retry pass** runs at the end: any schema still `UNREADABLE` is tried once
  more before the workbook is written, with the snapshot cache cleared so the
  union gets a genuine second attempt. It costs nothing when the first pass was
  clean.
- A sampling **exception** is now classified as `unreadable` rather than
  falling through unlabelled, so it lands in the DATA COMPLETENESS block
  instead of reading as "not sampled".

*Reading the log:* Profile schemas after the first now say `reusing the
snapshot sample already downloaded this run (N rows, no re-download)`, and the
retry pass announces itself as `RETRY PASS`.

---

## v3.4.0 — 2026-08-14

**The credential name is gone from the workbook.**

Titles inside the file used to fall back to the credential's *service name* —
the key under which a credential is filed in the OS vault. A workbook read with
a key stored as `<name>` was headed **“Data Dictionary — `<Name>`”**, putting an
artefact of our own key management exactly where the reader expects the subject
of the report. It also implied the wrong thing: the credential says who *read*
the sandbox, not what the document is *about*.

- Workbook titles now use a client name **only** when the credential record
  carries an explicit `client` key.
- With no client configured, every title identifies the workbook by **sandbox**
  — the same thing the filename has said since v3.3. A prod run is headed
  *“Data Dictionary v3.4.0 — prod”*.
- Applies to all four titled tabs: Summary, Field Index, Datasets, Audiences.
- The console banner still names the credential set, which is operationally
  useful and never leaves your machine.

**Release notes are linked from the workbook.**

- The provenance line on every tab now ends with a link to this file.
- The How to Use tab gains a clickable **“What's new in v3.4.0”** link.

*Upgrade note:* if you want a real client name in the titles, add a `client`
key to that credential record. Otherwise you get the sandbox, which is usually
what you wanted anyway.

---

## v3.3.0 — 2026-08-13

**Readability, provenance, and the first half of the credential-name fix.**

- **Audiences tab** — every audience with its tags, who built it, who last
  changed it, and the segmentation rule (PQL) rendered readable. Rules
  containing event-sequence or time-window clauses are marked `partial` and
  show `<...>` rather than pretending to be complete; the raw definition is
  kept alongside.
- **Provenance line on every tab** — when the run happened, which script
  version, which commit last touched the script, and a loud red warning when
  the working tree had uncommitted changes (in which case the commit id does
  *not* describe the code that produced the file).
- **How to Use tab**, placed second so it sits beside the Summary rather than
  behind thirty schema tabs. Written for someone who has never seen the
  workbook before, including the traps: coverage is a *sample*, and `MISSING`
  is not the same as `0%`.
- **Filters and frozen headers on every tab.**
- **Credential name dropped from the filename** in favour of the sandbox:
  `Data Dictionary - prod - 2026-08-14.xlsx`. (v3.4 finished the job inside the
  file.)
- Tag vocabulary and user directory are fetched **once per org** rather than
  per sandbox.

---

## v3.2.0

**SQL table names, Profile flags, and honest gaps.**

- **Datasets tab** mapping every dataset's friendly name → **Query Service
  table name** (`tags['adobe/pqs/table']`) → schema. The system name is what
  you `SELECT ... FROM`, and it differs from the friendly name; without this
  the workbook could not be used to write SQL.
- *Table name(s)* columns on the Schemas index and Field Index, and the SQL
  table name in each schema tab's header block.
- **Profile column** on the Datasets tab (`tags.unifiedProfile`), flagging both
  Profile-enabled datasets and the Profile Snapshot Export(s), so you know
  which tables hold whole profiles. Profile rows sort to the top.
- **Data-completeness warnings**, so a partial dictionary is never read as
  gospel. Any schema whose coverage is missing, partial or empty is listed in a
  Summary **DATA COMPLETENESS** block and banner-flagged on its own tab.
  `MISSING` is deliberately distinguished from `0%`.
- The Profile Snapshot Export is sampled **once per run**: if its (huge) file
  manifest times out under load, the failure is cached so the remaining
  Profile schemas fail fast instead of each re-hitting a dead snapshot.
- Bundled Luma demo dataset (`demo/luma/`) — Adobe's public sample data,
  organised and tenant-normalised for offline demos and tests.

---

## v3.1.0

**Profile coverage fix.**

A Profile-class schema is the post-merge **union** — identity-deduped,
last-write-wins — so sampling its *feeding* datasets tallies pre-merge
fragments and reads falsely sparse. Every Profile schema was under-reporting
coverage.

- Profile-class schemas are now sampled from the **Profile Snapshot Export**
  dataset belonging to the **default merge policy**, auto-resolved: default
  merge policy → the snapshot dataset tagged with that policy id whose
  `schemaRef` is `profile__union`.
- Snapshots are enormous, so the tool downloads the **smallest non-empty
  partition file** rather than whichever comes first.
- Override with `--profile-snapshot=<datasetId>` where the default isn't the
  one you want.

---

## v3.0.0

**The baseline.** Every tenant XDM schema pulled from an AEP sandbox, filtered
down to the ones that matter, and written to a tabbed, strictly-confidential
Excel workbook.

- Filtering with a printed **KEEP / DROP verdict per schema**, so exclusions are
  auditable rather than silent: `no-dataset`, `adhoc`, `ajo`, `system`, `test`.
  On a large tenant this is the difference between 2,400 schemas and the ~30
  that are real.
- One tab per kept schema — full field list in dot notation with data types,
  identities, relationships and friendly (`alternateDisplayInfo`) labels —
  ready to paste into Claude for a Mermaid ERD.
- Master **Field Index** across every schema, and a **Schemas** index.
- `--data-dict`: real **coverage %** and **top-5 example values** per field,
  tallied from actual ingested records sampled as Snappy-Parquet through the
  Data Access API. One download covers every field at once — no Query Service,
  no per-field calls.

### Notes on reading coverage

- Coverage is sampled from up to `--dd-rows` records (default 1000), not the
  whole dataset. It means *roughly how often this is populated*, not an exact
  figure.
- Busy datasets make Catalog time out when asked to sort every batch they have
  ever held. The tool filters `status=success` server-side and then steps the
  page size down (20 → 5 → 1) rather than giving up; a narrower sample beats no
  coverage at all, and it says so in the log when it happens.
- Adobe's own consolidation batches are skipped — Catalog advertises them with
  the combined record count, but the Data Access API refuses them outright
  (`DTAC-4000`). Nothing is lost: the batches they absorbed are still served
  individually.
