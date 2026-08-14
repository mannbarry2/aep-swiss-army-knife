# Data Dictionary — release notes

Version history for [`data_dictionary_v3.py`](data_dictionary_v3.py), newest
first. Every workbook the tool writes carries its version, the commit that
produced it, and a link back to this file, so a dictionary found months later
can be traced to exactly what the code did at the time.

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
