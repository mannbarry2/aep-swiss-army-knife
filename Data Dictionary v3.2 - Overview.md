# AEP Data Dictionary v3.2 — how it works

> **STRICTLY CONFIDENTIAL** — with `--data-dict` the output workbook contains real sampled customer data.

`data_dictionary_v3.py`  (AEP Swiss Army Knife)  ·  June 2026

## What it produces

Data Dictionary v3.2 authenticates to Adobe Experience Platform with a chosen credential set, reads a chosen sandbox (Enter = production), pulls every XDM schema, filters out the noise, and writes one strictly-confidential Excel workbook to `./output` (the previous copy is moved to `./output/archive` first). Tabs:

- **Summary** — counts per sandbox and a tab-colour key.
- **Field Index** — every field across all schemas; look up an exact dot-notation path with Ctrl-F.
- **Schemas** — one row per kept schema (class, dataset count, **SQL table name(s)**, field/identity/relationship counts).
- **Datasets** — every dataset mapped to its **SQL table (system) name** — see below.
- **One tab per schema** — every field (dot-notation path, type, identity, relationship), plus the schema's SQL table name(s) in the header block. Ready to paste into Claude (via MCP) for a Mermaid ERD.

The Profile schema's tab is coloured **purple** so the post-merge union stands out from the event and lookup schemas. The file is named `Data Dictionary - <Client> - <date>.xlsx`.

## SQL table (system) names

To write SQL against AEP Query Service you query a **dataset**, and a dataset's queryable table name is *not* its friendly name — it is a normalized **system name** (e.g. friendly `Acme CJA Order Event Dataset` → table `acme_cja_acme_order_event_dataset`). AEP stores it on the Catalog dataSet at `tags["adobe/pqs/table"]`.

v3.2 surfaces that mapping so the workbook is a valid SQL input:

- A **Datasets** tab lists every dataset: *Sandbox · Schema · Friendly Name · Table Name (SQL/system) · Profile · Dataset ID*.
- The **Profile** column (from `tags.unifiedProfile`) flags every dataset **enabled for Unified Profile** and the **Profile Snapshot Export(s)** — so you know which tables to query for whole profiles. Profile-related rows sort to the top.
- The **Schemas** index and **Field Index** each carry a *SQL table name(s)* column.
- Each **schema tab** prints its `SQL table name(s):` in the header, right next to the field list — so a query can be formed from a single tab (the table name for `FROM`, the field paths for `SELECT`).

A schema can be fed by several datasets, so it can map to several tables; all are listed.

## Data completeness — never read as gospel

With `--data-dict`, any schema whose coverage is **missing, partial, or empty** is called out, so an incomplete sample is never mistaken for fact:

- The **Summary** tab has a red *DATA COMPLETENESS* block listing every such schema and why (e.g. *MISSING — data exists but could not be sampled (504/timeout); coverage is unknown, not 0%*).
- Each affected **schema tab** carries a red coverage banner in its header.
- The **Profile Snapshot Export** is large; under load its file manifest can 504. It is sampled once per run, and if it fails the result is cached so the other Profile schemas fail fast (flagged MISSING) rather than each re-hitting the dead snapshot. Re-running off-peak normally resolves it.

## Why v3 — safer than v2

v2 (the standalone `aep_data_dictionary` repo) staged copies of JSON files dropped onto an FTP location and read them from disk. v3 is **ephemeral**: it talks to the AEP APIs directly and streams everything through memory — no FTP, no intercept, nothing written to disk except the final workbook. That is materially safer for confidential customer data.

## How the filtering works

The prod sandbox holds a couple of thousand schemas, almost all machine-generated. The tool keeps only schemas with at least one dataset, then drops — with a logged reason for each — ad-hoc / auto-generated (incl. many `Schema for audience...`), Adobe Journey Optimizer, Adobe system plumbing (Offer Decisioning, CJA, Audience Portal, Journey Orchestration…), and test / placeholder schemas. On prod this leaves a few dozen kept (from several hundred datasets). All match rules are constants at the top of the script and easy to tune.

## The data dictionary (`--data-dict`)

AEP has no API that returns a value distribution for a field, and Query Service per field is too slow. So the tool **samples real ingested data**: it finds the schema's datasets, downloads recent non-empty batch files (Snappy-Parquet) via Data Access, parses only the rows it needs, and tallies locally — adding **Coverage %** and **Top-5 values** (`value(count)`) per field. A 0-row schema is reported as either genuinely **EMPTY** or **"data exists but unreadable"**, so the two are never confused.

A bare `--data-dict` samples **every** kept schema (one coverage pass per tab); narrow it to a single schema with `--data-dict=<substr>` (e.g. `--data-dict=profile`). Expect an all-schemas run to take a long time — it is dominated by Data Access downloads — and it logs overall progress (schema *i/N*, minutes elapsed, rough ETA) as it goes.

### Event schemas vs Profile — two different sampling sources

**EVENT schemas** (ExperienceEvent: Order, Slot, Promotion…) are immutable, fully-populated records, so sampling their batches is rich and correct: Order Event came back 19/25 fields populated with real values (fulfilmentType, GTINs, prices, order states).

**PROFILE is different and must NOT be sampled from its feeding datasets.** A profile is the post-merge union (identity-deduped, time-ordered, last-write-wins); each feeding dataset only writes its own slice as a sparse delta, so batch sampling tallies pre-merge fragments and a union-100% field (e.g. first name) reads near-empty. The correct source is the **Profile Snapshot Export** dataset — the merged union, one row per identity. For a profile-class schema the tool auto-resolves the org's **default merge policy**, locates the snapshot-export dataset belonging to it (detected by tag `unifiedProfile = ups_snapshot_type:*` and schema `context/profile__union`, matched on the policy id), and routes the schema to that snapshot — reusing the same sampler. No picker is needed; override the dataset with `--profile-snapshot=<datasetId>` if the default is not the one you want. Snapshots are huge (tens of millions of rows) on a daily ~04:08 cut, so the tool downloads the smallest non-empty partition file, with longer timeouts and retries to ride out the cold-start 504s the manifest server throws on first access.

## Version history

- **v3.1** — Profile coverage fix (sample the Profile Snapshot Export union, not pre-merge feeds).
- **v3.2** — bundled Luma demo dataset (`demo/luma/`) **and** SQL table (system) names: a Datasets tab plus *SQL table name(s)* columns, so SQL can be formed against the right table straight from the workbook.

## Known limitations — for review

- Coverage = what the **sample** shows; representative for events and for the profile snapshot, understated only in the fallback case where a Profile schema cannot resolve a snapshot and is read from its feeding datasets.
- Runtime is dominated by Data Access downloads (event files 20–100 MB; profile snapshot partitions are larger). The schema/ERD export itself is seconds.
- Some batches 504 / time out server-side; skipped after 3 consecutive failures and logged.
- Top-5 come from the sample, so rare values may be missed; high-cardinality fields (IDs) are not informative.
- A dataset that has not yet been assigned a Query Service table name shows a blank *SQL table name* (rare — new/unactivated datasets).
- AJO / system / test filters and identity flags are heuristic and may need per-tenant tuning.
- Planned: DULE data-governance labels as a per-field column.
