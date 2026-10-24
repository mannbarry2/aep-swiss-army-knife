# Data Dictionary release notes

**Last update:** October 24, 2026 · **Latest version:** v3.4.16 · **Script:** [`data_dictionary_v3.py`](data_dictionary_v3.py)

> [!TIP]
> Every workbook the Data Dictionary writes carries its version, the commit
> that produced it and a link back to this page, so a dictionary found months
> later can be traced to exactly what the code did at the time. The notes
> come in two editions: this Markdown file, and a colour HTML edition,
> `RELEASE_NOTES.html`, built from it by
> [`data_dictionary/release_notes_html_builder.py`](data_dictionary/release_notes_html_builder.py). How to run the tool: the
> [README](README.md).

**Labels:** ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) a new capability · ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) a change to something that already existed · ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) a bug fix · ![Beta](https://img.shields.io/badge/Beta-6E3FA3?style=flat-square) works, but still being tuned

| Release date | Version | Highlights |
| --- | --- | --- |
| [October 24, 2026](#october-24-2026) | v3.4.15 – v3.4.16 | Field history's timeouts fixed; every unreadable month explained; "In Profile now" — what an audience can see |
| [October 23, 2026](#october-23-2026) | v3.4.14 | Field history hardened after its first prod run; endpoint health report |
| [October 22, 2026](#october-22-2026) | v3.4.12 – v3.4.13 | "Have we got it?" field history (beta); top 10 values with PII starred |
| [October 16, 2026](#october-16-2026) | v3.4.8 – v3.4.11 | Lookup schemas kept; Dropped Schemas and Dropped Field Index tabs; the run keeps the PC awake |
| [October 8, 2026](#october-8-2026) | v3.4.7 | Overlap tabs; one workbook per tool |
| [October 6, 2026](#october-6-2026) | v3.4.6 | Audience complexity score (beta) |
| [September 30, 2026](#september-30-2026) | v3.4.5 | No more silent PQL truncation; the dictionary's own folder |
| [September 29, 2026](#september-29-2026) | v3.4.4 | Snapshot tabs |
| [September 25, 2026](#september-25-2026) | v3.4.3 | Friendly names and descriptions everywhere; friendly-label columns renamed |
| [September 21, 2026](#september-21-2026) | v3.4.2 | Tab colour system; data-hygiene columns |
| [August 14, 2026](#august-14-2026) | v3.4.0 – v3.4.1 | Complete Profile coverage; credential name gone from the workbook |
| [August 13, 2026](#august-13-2026) | v3.3.0 | Audiences tab; provenance line; How to Use tab |
| [Earlier releases](#earlier-releases) | v3.0.0 – v3.2.0 | The baseline; Profile coverage; SQL table names |

---

## October 24, 2026

**Release date: October 24, 2026** · v3.4.15 – v3.4.16

New features and updates to existing features in the Data Dictionary:

- **Field history:** the cause of its timeouts found and removed, and every month it cannot read now explained (beta).
- **In Profile now:** how much of each field an audience can actually see, given the Profile retention on its dataset (beta).

### In Profile now

Audiences are built on Profile, not the data lake, and Profile keeps event data
only as long as the **Profile retention set on each dataset** -- which is not
the same everywhere. On ROI prod most event datasets keep 90 days, but
`tesco_order_event_dataset` keeps **45** and promotion interactions 60, while
their data lake copies go back two years. A "bought in the last 90 days"
audience on order events can only see 45 of them, however far back the lake goes.

**New features**

| Feature | Description |
| --- | --- |
| In Profile now (days) ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) ![Beta](https://img.shields.io/badge/Beta-6E3FA3?style=flat-square)<br>`v3.4.16` | Two columns beside "Have we got it?" on the Field Index and every schema tab (and the days on the Field History tab): the field's days of unbroken data, capped by the Profile retention of the dataset that carries it, with the reason in words -- *all 90 days Profile keeps*, *Profile keeps only 45 days (tesco_order_event_dataset)*, *24 of the 90 days Profile keeps -- since 30 Sep 2026*. Worked out dataset by dataset, so a backup dataset with a longer retention but one month of data cannot lend its retention to another dataset's long run. |
| Profile retention on the Summary ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.16` | Every Profile-enabled dataset with a Profile retention, how many days Profile keeps and how long the data lake keeps; under 90 days in red. |

### Field history

The first full ROI prod run of `--history` took 5 h 15 min, and 656 of its
1,227 Data Access file lists ended in 504 Gateway Timeout at exactly 60 s. It
was not a bad night at Adobe. **Every one** of the 328 batches behind those
timeouts was the *Profile-side* half of a batch ingestion: Catalog lists it as an
ordinary successful batch of the dataset, but it has no data-lake files, and
Data Access hangs until the gateway gives up instead of saying so. The same
batches still timed out the next morning, and on retry 30 s and 90 s later; the
datasets' data-lake batches read in seconds.

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| Profile-side batches skipped ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square)<br>`v3.4.15` | A batch with an old-style 24-character id and Profile write metrics (records read / written, profile fragments, no file counts) is no longer read, in history or in coverage sampling -- the same way consolidation batches are already skipped. Each one cost two minutes of waiting. |
| "Profile only" months ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.15` | A month that holds nothing but Profile-side batches is marked at once -- *Profile only: no data-lake copy to read* -- instead of being read. It still shows as **?**, and the schema tab names the datasets it affects. |
| Only the fixable months are retried ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square)<br>`v3.4.15` | A month that can never be read -- Profile only, empty files, or files Adobe has deleted (HTTP 404) -- is now cached like any finished month. Only a timeout or 5xx is asked again next run. The log says how many will be. |
| Batches with a known size first ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square)<br>`v3.4.15` | Within a month, batches whose record count Catalog knows are tried before those it doesn't, which are more often empty. |
| Every unreadable month explained ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square)<br>`v3.4.16` | After v3.4.15, 126 dataset-months on ROI prod still showed **?**. Each kind was traced: **(1)** data-lake files that really hold no rows -- a 970 MB load into the product lookup left a 4 KB, zero-row file, because the records went to Profile -- now labelled *data-lake files empty*; **(2)** the identity service's `…-INGEST` marker batches, whose file list Data Access answers with DTAC-4043 *DataSetFile not found*, now skipped like Profile-side ones; **(3)** months whose readable batches were hidden: only the newest 10 batches of a month were asked for, and in a busy month those can all be Profile-side or empty. |
| A month's batches, 100 at a time ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square)<br>`v3.4.16` | Catalog is asked for 100 batches per month instead of 10, paging back up to five pages when the first holds nothing readable, and up to four batches are tried when files come back empty (each such read takes about two seconds). A Profile-only month now records how many records Profile received, and the schema tab says so. |
| Earlier caches re-checked ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square)<br>`v3.4.16` | Months cached by earlier versions were judged from 10 batches, so they are not trusted: the first v3.4.16 run reads every month again. |

For more information, read the [README](README.md).

---

## October 23, 2026

**Release date: October 23, 2026** · v3.4.14

New features and updates to existing features in the Data Dictionary:

- **Field history:** fixes from its first full prod run (beta).
- **Running the dictionary:** every pass ends with an Adobe endpoint health report.

### Field history

The first full prod run of `--history` took seven hours, of which six were the
history pass; 38% of months could not be read and 12 datasets were lost
outright. Adobe's Data Access file lists were slow all morning, but the engine
made it worse than it had to be.

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| No dataset lost to one timeout ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square)<br>`v3.4.14` | Each dataset began by asking Catalog for its oldest batch. On very large datasets that sort returned 504 Gateway Timeout, and the whole dataset was given up -- 12 in prod, including the Analytics feed and every orders and returns dataset. Months are now walked with Catalog's newest-first query bounded to the month, which answers in about 0.2 s, and the exact start date is looked up only inside the first month that has data. |
| One file list per read ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square)<br>`v3.4.14` | The reader fetched a file list for up to 40 files of a batch before choosing one; history needs one. It now asks for one, and waits 90 s for it instead of 30: the first request for a batch's file list was measured at 17.8 s and often passed 30 s, while the repeat took 0.6 s. |
| Locked datasets skipped ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square)<br>`v3.4.14` | A dataset Data Access refuses outright (HTTP 403, e.g. the semi-smart coupon dataset) is recognised on its first read and skipped, with the reason on its schema tab, instead of being retried month after month. |
| Back-off when Adobe struggles ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.14` | When most of the last 20 Data Access calls time out or return 5xx, every history worker pauses for three minutes rather than piling up unreadable months. Unreadable months are never cached, so the next run retries them. |

### Running the dictionary

**New features**

| Feature | Description |
| --- | --- |
| Adobe endpoint health ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.14` | Every request is counted per Adobe endpoint (Catalog, Data Access file lists, downloads, Schema Registry, Profile). The run log shows a health table after the schema and coverage passes and after history: calls, failures by kind (no answer in time, HTTP 504 ...), median, 95th percentile and slowest response. Retry messages now name the endpoint and the reason, e.g. *Data Access file list ...: no answer in 90s; retry 2/2*. |

For more information, read the [README](README.md).

---

## October 22, 2026

**Release date: October 22, 2026** · v3.4.12 – v3.4.13

New features and updates to existing features in the Data Dictionary:

- **Field history:** how many months of unbroken data each field has, for building audiences (beta).
- **Sample values:** the top 10 instead of the top 5, with personal data starred.

### Field history

Coverage % says how often a field is filled in *now*. Building an audience also
needs to know how far back the field goes: a "purchased in the last 12 months"
audience is no good on a field that has only been arriving for six weeks.

**New features**

| Feature | Description |
| --- | --- |
| "Have we got it?" ![Beta](https://img.shields.io/badge/Beta-6E3FA3?style=flat-square)<br>`v3.4.13` | `--history` adds three columns to the end of the Field Index and every schema tab: **months** of unbroken data up to now, a plain-English **detail** (*since 28 Sep 2026*, *stopped -- last seen Aug 2026*, *since May 2026 -- missing in Apr 2026*) and a **heartbeat**, one bar per month, oldest first: ░ no data at all, ▁ data arrived but the field was empty, ▂ to █ how often it was filled, ? could not be checked. The start is the real date where it is known (the dataset's first batch, or the edge of its TTL), not a whole month. In prod the slot expiry fields read *<1* (first batch September 28) while order events go back to April 2025. |
| Field History tab ![Beta](https://img.shields.io/badge/Beta-6E3FA3?style=flat-square)<br>`v3.4.13` | The same months as columns, the % of sampled records carrying each field, shaded green. Blank is no data at all that month, 0 is data without the field. |

**How it works.** Querying each field month by month in Query Service would mean
a query per field per month. Instead, for each dataset and month, Catalog finds
that month's batches (`createdAfter` / `createdBefore`, about 0.2 s) and one of
them is read through the Data Access API with the coverage pass's reader. That
one download answers for every field at once, so the cost is datasets × months,
not fields × months; months before a dataset's first batch, or past its TTL,
cost nothing. Test, perf and temp datasets are left out so they cannot make a
field look older than the real feed. Finished months are cached locally in
`output/field_history_cache/`, so only the first run on a sandbox is long.

**Known issues**

| Issue | Description |
| --- | --- |
| A sample can miss | Each month is judged from one to three batches, not every record. One sample a month faked a gap in testing (April's order sample was an odd 101-row batch carrying 9 of 16 fields), so a month that is thin, short of fields or missing a well-filled field both neighbours have is sampled again from earlier in the month. A rarely-filled field's one-month hole is shown as ? rather than a break. |
| Months go by load date | History follows when data was *loaded*. Events backfilled later count from the load; the schema tab says when a sample showed events much older than their batch. |
| The first run is long | Every month of every dataset is read once: hours on a big sandbox. Later runs read only the current month. |

### Sample values

**Updates**

| Update | Description |
| --- | --- |
| Top 10 values ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square)<br>`v3.4.12` | The sample columns show the ten commonest values instead of five. |
| Personal data starred ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.12` | Email addresses (by value) and identifier / contact fields (by name: user id, CRM id, ECID, phone and the like) have two characters starred before they are counted, whether or not the field carries a DULE label: `j**n.doe@acme.com`. The shape and the ranking survive; a whole real identifier does not. A How to Use row says so. |

For more information, read the [README](README.md).

---

## October 16, 2026

**Release date: October 16, 2026** · v3.4.8 – v3.4.11

New features and updates to existing features in the Data Dictionary:

- **Schema filtering:** custom lookup schemas get their tabs back.
- **Workbook:** two hidden tabs that account for everything the filters leave out.
- **Running the dictionary:** a long run can no longer be frozen by the PC going to sleep.
- **Known issues.**

### Schema filtering

The filters decide which of a sandbox's schemas get a tab. In prod they take
2,830 schemas down to 64.

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| Lookup schemas are kept ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square)<br>`v3.4.8` | Every class whose id ended in a long hex string was dropped as ad-hoc. Generated ad-hoc classes look like that, but so does any custom class made in the UI, so every lookup schema went out with them. In prod that was 20 schemas with datasets on 15 custom classes, among them **Tesco Product Lookup** (`tesco_product_lookup_dataset`, the product hierarchy behind `commercialHierarchyClassCode`), Marketplace Product, Baby Products, Store, Store Group, Store Manager, Promotion, GTIN/TPNB mapping, Recipes, Weather and Bank Suppression. The test is now the behaviour the schema extends: `xdm/data/adhoc` is dropped, `xdm/data/record` and `xdm/data/time-series` are kept. The restored tabs are green (the "other / lookup" hue) and sample coverage from their own datasets. |

### Workbook

The schemas the filters drop are now accounted for in the workbook itself, on
two hidden tabs (right-click any tab › **Unhide**). v3.4.8's bug went unseen
because a dropped schema left no trace: its reason was printed to the console
and then gone.

**New features**

| Feature | Description |
| --- | --- |
| Dropped Schemas tab ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.9` | Every schema the filters leave out, with the drop reason in plain English, how many datasets use it, their SQL table names, its class and behaviour (ad-hoc / record / time-series). Schemas that still hold data come first, so a table you query that has no tab is easy to find, along with the reason it was dropped. The Summary and How to Use tabs point to it. |
| Dropped Field Index tab ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.10` | The dictionary is meant to be a census, so the dropped schemas are inventoried too: every field of every dropped schema, with its dot-notation path, type and display name, the schema, why it was dropped, its dataset count and SQL table names. An inventory only, with no coverage or sample values. The Field Index header points to it for a field that isn't there, and the Dropped Schemas tab gains a **Fields** count per schema ("could not be read" where the fetch failed). In prod: about 244,000 rows, from about 2,800 extra schema fetches at ~0.13 s each, which adds five or six minutes. |

### Running the dictionary

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| The run keeps the PC awake ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square)<br>`v3.4.11` | On October 16 the machine went into Modern Standby part-way through prod's run and froze it for 76 minutes (18:43 to 20:01). Nothing failed; it simply stopped. The dictionary now asks Windows to stay awake from the moment a credential is chosen until the run ends, however it ends. On a Modern Standby machine the screen turning off is what triggers standby, so the screen is held on too. The hold belongs to the process and goes when it exits, so the power settings are never changed. Closing the lid or pressing the power button still sleeps. |

### Known issues

| Issue | Description |
| --- | --- |
| Creator columns show raw ids | The credential the dictionary runs with has lost access to the User Management API, so the user directory read fails and *Created by* / *Last modified by* on the Audiences tab show ids instead of names. |
| Some coverage can't be read | Data Access file listings sometimes time out under load. A schema whose data exists but couldn't be read is marked "not confirmed empty" in the DATA COMPLETENESS block, never 0%. On October 16 this hit the Tesco Bank Suppression Lookup Schema. |
| Complexity weights are uncalibrated | The audience complexity score is beta: its weights are judgement, not measurement, because AEP never reports what an audience cost to compute. |

For more information, read the [README](README.md).

---

## October 8, 2026

**Release date: October 8, 2026** · v3.4.7

New features and updates to existing features in the Data Dictionary:

- **Workbook:** the dataset overlap report, as two tabs.
- **All tools:** one workbook per tool, and nothing else.

### Workbook

The dataset overlap report is folded in from
[`overlap_report.py`](overlap_report.py), which stays runnable on its own.

**New features**

| Feature | Description |
| --- | --- |
| Overlap cut-list ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | The profiles ONLY one dataset contributes (expire it, save exactly that many), with a running total against the Addressable Audience licence and the point where an overage would clear. |
| Overlap full report ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | Every combination of datasets a profile is built from, largest first. A sudden new combination is a new route in: this is how the 3.1M shell profiles of October 5–6 showed up, as "Tesco Profile Dataset only". |

### All tools

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| One workbook per tool ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) | House rule applied across the library: every tool writes **one workbook with a stable name, overwritten each run**, and nothing else. The CSV and JSON side-files are gone from `overlap_report`, `batch_eval_timing`, `failed_batch_report`, `dataset_census`, `sluggishness_detector`, `audit_batch_schedules_v2`, `dd_audience_countdown`, `audit_hunt`, `profile_jump` and `guardrail_audit`. The shared writer is `house_xlsx.py`. |

For more information, read the [README](README.md).

---

## October 6, 2026

**Release date: October 6, 2026** · v3.4.6

New features and updates to existing features in the Data Dictionary:

- **Audiences:** a complexity score for every rule-based audience.

### Audiences

Not all audiences are created equal: one badly built audience can double the
nightly batch run on its own, and AEP's own metrics never say what an audience
cost to compute.

**New features**

| Feature | Description |
| --- | --- |
| Audience complexity score ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) ![Beta](https://img.shields.io/badge/Beta-6E3FA3?style=flat-square) | The Audiences tab gains a **Complexity score (0-100)** and a **RED / AMBER / GREEN** on it, plus the reasons: *Why this score* (the points, in plain English), event scan, lookback days, lookup joins, joined fields, aggregations, depends-on count, largest base audience and its size, sequence steps, conditions, longest hard-coded value list, merge policies, and how many audiences use it. The run log reports the RED / AMBER / GREEN counts per sandbox. The How to Use tab explains the score and the usual fix: shorten the lookback, or move the sum / count upstream into a computed attribute. |

**How the score is built**

The rules follow the two miaprova.com write-ups on expensive AEP segments
(*Why your most expensive AEP audience might be your newest one*; *The right
fix for expensive AEP segments: move the math upstream*). Each cost driver is
read off the PQL and the audience record.

| Cost driver | Points |
| --- | --- |
| Event scan and lookback | The longer the window, the more events are read per profile per night. A scan with *no* time limit scores highest (35). |
| Lookup joins | A join to another class (products via gtin) runs per event per profile: 18, +4 per extra field, capped at 25. |
| Aggregations that cannot short-circuit | sum / average 15, count 10, "occurs N times" 6, forall 4, capped at 20. |
| Base audience size | `inSegment()` on a big audience scans all of it: +2 per dependency, +3 / +6 / +9 for a base of 1M / 5M / 20M profiles. |
| Smaller signals | Sequences, condition count, lists of 100+ values, more than one merge policy, a negated event step and an overridden performance warning. |

RED is 55 and over, AMBER 30 to 54. The weights live in
`audience_complexity.py` (developed standalone, folded in) and are a first cut
to be argued with. The size of each base audience is read from
`/segment/definitions/<id>` once per run (a few hundred small reads).

> [!NOTE]
> First prod run: 2,205 rule-based audiences scored: 64 RED, 482 AMBER,
> 1,659 GREEN. Every RED is the same shape: an unlimited event scan, a product
> lookup join, and a base audience of 13 to 23 million profiles.

For more information, read the [README](README.md).

---

## September 30, 2026

**Release date: September 30, 2026** · v3.4.5

New features and updates to existing features in the Data Dictionary:

- **Audiences:** no more silent PQL truncation.
- **Output:** the dictionary gets its own folder.

### Audiences

Excel caps a cell at 32,767 characters, and the Audiences tab used to cut a
rule at 32,000 without saying so. Eleven published batch audiences (store-list
rules, mostly) were incomplete in the August 21 run.

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| Full-PQL sidecar file ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | The full, untruncated rule of every audience is written to `output/data_dictionary/pql/audiences_pql_<sandbox>_<yyyymmdd>.jsonl`: one JSON line per audience with `audience_id`, `audience_name`, `format`, `raw_pql` and `readable_pql`. Old sidecars are archived with the old dictionary. |
| Three new Audiences columns ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | After *PQL (raw pql/json)*: **PQL length**, **PQL truncated in sheet** (yes/no, red when yes) and **PQL format** (`json` for Adobe's syntax tree, `pql-text` for a rule held as PQL text, which is complete but not JSON). *Unresolved tag ids* and *System tags* move three columns right. |
| Truncation is marked ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) | A cut cell now ends with `…[TRUNCATED – see sidecar]`. |
| Run log summary ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) | The run log closes with a per-sandbox line: audiences, how many were cut, count by format, and the sidecar path. The How to Use tab explains it. |

### Output

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| The dictionary's own folder ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) | The dictionary now writes to `output/data_dictionary/` (with its `archive/` beneath it) instead of the shared `output/` folder. |

For more information, read the [README](README.md).

---

## September 29, 2026

**Release date: September 29, 2026** · v3.4.4

New features and updates to existing features in the Data Dictionary:

- **Workbook:** snapshot tabs, the first standalone tool folded in.

### Workbook

The first standalone tool to be folded into the dictionary:
[`snapshot_tables.py`](snapshot_tables.py). The dictionary calls the tool's own
collector and tab writer rather than copying them, so the standalone script
produces identical tabs and there is one copy of the logic. This is the
pattern for folding in further tools.

**New features**

| Feature | Description |
| --- | --- |
| Snapshot Summary ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | One row per sandbox: the correct snapshot tables, when the system evaluation is scheduled, when it last actually ran, when the snapshot was written and how long that took. **Time to run** is tagged RED / AMBER / GREEN (under 60 min, 60 to 180 min, 180 min or more). Production sandboxes come first, development / PPE at the bottom. All times are UTC. |
| Snapshot Tables ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | One row per snapshot export table, with its Query Service table name and merge policy. Rows on the sandbox's **default merge policy** are green: those are the correct tables. The rest sit on other merge policies (added for debugging etc.) or carry none. |
| Workbook author ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) | The workbook's author property is now *Barry Mann ([barrymann.com](https://barrymann.com))* rather than the library's name. |

For more information, read the [README](README.md).

---

## September 25, 2026

**Release date: September 25, 2026** · v3.4.3

New features and updates to existing features in the Data Dictionary:

- **Workbook:** friendly names and descriptions next to every technical name; the friendly-label columns renamed.

### Workbook

**New features**

| Feature | Description |
| --- | --- |
| Friendly Path and Description on every field ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | On every schema tab and the Field Index. Friendly Path is the display names from the root down, e.g. *Person > Full name > First name* for `person.name.firstName`, with a level's key standing in where no title is set. Both come from the schema itself; where a field carries no description of its own, its `alternateDisplayInfo` description is used. |
| Descriptions for schemas, datasets and audiences ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | A Description column on the Schemas tab, and each schema's description under the title on its own tab (the field header row moves from row 6 to row 7). A Description column on the Datasets tab (Catalog's `description`) and on the Audiences tab, next to the audience name. A blank description means Adobe holds none for that object, not that it was dropped. |

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| Friendly-label columns renamed ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) | The count of fields carrying an `alternateDisplayInfo` friendly display name used column headings that read like a misspelling of **DULE** (Data Usage Labeling and Enforcement, Adobe's governance framework that controls access to objects and data through labels such as C8 and C12), which is an entirely different thing. The columns are now **Friendly labels** and **Friendly-labelled fields**, and the run log says *Friendly-name labels*. The How to Use tab gains a "Friendly labels are not DULE labels" row explaining what DULE is and that DULE labels are not in the workbook yet (still planned as a per-field column). |
| Tab names keep their first letter ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) | A schema titled "Acme …" lost the first letter of its tab name: the prefix strip took one character too many. |

For more information, read the [README](README.md).

---

## September 21, 2026

**Release date: September 21, 2026** · v3.4.2

New features and updates to existing features in the Data Dictionary:

- **Workbook:** a tab colour system that means something; data-hygiene columns.
- **Coverage sampling:** honest completeness statuses.

### Workbook

Purple used to mean only "built on the XDM Individual Profile class". A
Profile-class schema whose datasets were all disabled for Unified Profile was
still purple (five tabs in a prod run: raw feeds, backups, pattern
experiments) and, worse, its coverage was sampled from the Profile Snapshot
Export, where its rows never land, so it read as empty.

**New features**

| Feature | Description |
| --- | --- |
| Tab colour system ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) | Every schema tab is coloured on two axes: the hue is the class, the shade is whether the data reaches Unified Profile (see the table below). "In Profile" is read from each dataset's `tags.unifiedProfile`, the same signal the Datasets tab's Profile column shows. Only **dark purple** schemas are in the merged union and sample coverage from the Profile Snapshot Export; every other schema samples its own datasets (the tab header's `source:` note says which). A six-swatch colour key at the top of the Summary tab explains the system. |
| Data-hygiene columns ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | On the Schemas and Datasets tabs, straight from Catalog's `extensions`: **Kind** (Profile / Event / Custom) and, on Schemas, **In Profile** (Y/N); **Data lake GB** and **Data lake rows** (`adobe_lakeHouse.metrics`); **Data lake TTL (days)** (`adobe_lakeHouse.rowExpiration.ttlValue`); **Profile GB** and **Profile TTL (days)** (`adobe_unifiedProfile.metrics` / `rowExpiration`). TTLs are in days (months = 30, years = 365) so they sort; blank means no expiration is set. Schemas roll their datasets up: sizes sum, TTL is the longest. Reverse-sort by GB or TTL and filter by Kind straight from the header row. |

| Class | In Profile (≥1 dataset enabled) | Not in Profile |
| --- | --- | --- |
| XDM Individual Profile | dark purple | light purple |
| XDM ExperienceEvent | dark blue | light blue |
| Other / lookup | dark green | light green |

### Coverage sampling

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| No false PARTIAL ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) | A partition that parses to 0 rows no longer marks a schema PARTIAL ("0 batch(es) failed to read"). Only real read failures do. |
| MISSING says why ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) | MISSING no longer blames "504/timeout" unconditionally. The same status is produced by a 403 when the credential is denied a dataset's labelled fields, and the text now says so. |
| Full batch ids in the log ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) | The run log prints full batch ids. They were cut to 24 of 26 characters, so they could not be pasted into the API. |

For more information, read the [README](README.md).

---

## August 14, 2026

**Release date: August 14, 2026** · v3.4.0 – v3.4.1

New features and updates to existing features in the Data Dictionary:

- **Coverage sampling:** complete coverage on Profile schemas.
- **Workbook:** the credential name is gone from the workbook; release notes are linked from it.

### Coverage sampling

A prod run came back with 16 of 33 schema tabs flagged `MISSING`. All 16 were
Profile-class, and all 16 failed for one reason: a single gateway 504. Every
Profile-class schema samples the *same* Profile Snapshot Export union, but
each one re-downloaded the ~127 MB partition for itself: sixteen downloads of
identical data, and sixteen chances to hit a 504. The `snap_unreadable` cache
added in v3.2 refused to retry a snapshot that had already failed, so **the
first failure became MISSING coverage for every Profile schema behind it**.

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| One snapshot download per sandbox ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square)<br>`v3.4.1` | The snapshot is sampled **once per sandbox** and the rows reused in memory for every Profile-class schema. One download, not sixteen. In the log, Profile schemas after the first say `reusing the snapshot sample already downloaded this run (N rows, no re-download)`. |
| Retry pass ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.1` | At the end of sampling, any schema still `UNREADABLE` is tried once more before the workbook is written, with the snapshot cache cleared so the union gets a genuine second attempt. It costs nothing when the first pass was clean, and announces itself in the log as `RETRY PASS`. |
| Sampling errors are classified ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square)<br>`v3.4.1` | A sampling **exception** is now `unreadable` rather than falling through unlabelled, so it lands in the DATA COMPLETENESS block instead of reading as "not sampled". |

### Workbook

Titles inside the file used to fall back to the credential's *service name*,
the key under which a credential is filed in the OS vault. A workbook read
with a key stored as `<name>` was headed "Data Dictionary — `<Name>`", putting
an artefact of our own key management exactly where the reader expects the
subject of the report. It also implied the wrong thing: the credential says
who *read* the sandbox, not what the document is *about*.

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| The credential name is gone ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square)<br>`v3.4.0` | Workbook titles use a client name **only** when the credential record carries an explicit `client` key. With no client configured, every title identifies the workbook by **sandbox**, the same thing the filename has said since v3.3: a prod run is headed "Data Dictionary v3.4.0 — prod". This applies to all four titled tabs: Summary, Field Index, Datasets, Audiences. The console banner still names the credential set, which is operationally useful and never leaves your machine. |
| Release notes are linked ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square)<br>`v3.4.0` | The provenance line on every tab ends with a link to this page, and the How to Use tab gains a clickable **"What's new in v3.4.0"** link. |

> [!IMPORTANT]
> To show a real client name in the titles, add a `client` key to that
> credential record. Otherwise you get the sandbox, which is usually what you
> wanted anyway.

For more information, read the [README](README.md).

---

## August 13, 2026

**Release date: August 13, 2026** · v3.3.0

New features and updates to existing features in the Data Dictionary:

- **Workbook:** readability, provenance, and the first half of the credential-name fix.

### Workbook

**New features**

| Feature | Description |
| --- | --- |
| Audiences tab ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | Every audience with its tags, who built it, who last changed it, and the segmentation rule (PQL) rendered readable. Rules containing event-sequence or time-window clauses are marked `partial` and show `<...>` rather than pretending to be complete; the raw definition is kept alongside. |
| Provenance line on every tab ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | When the run happened, which script version, which commit last touched the script, and a loud red warning when the working tree had uncommitted changes (in which case the commit id does *not* describe the code that produced the file). |
| How to Use tab ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | Placed second, so it sits beside the Summary rather than behind thirty schema tabs. Written for someone who has never seen the workbook before, including the traps: coverage is a *sample*, and `MISSING` is not the same as `0%`. |
| Filters and frozen headers ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | On every tab. |

**Fixes and improvements**

| Fix | Description |
| --- | --- |
| Credential name dropped from the filename ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) | In favour of the sandbox: `Data Dictionary - prod - 2026-08-14.xlsx`. v3.4 finished the job inside the file. |
| Org-level reads once per run ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) | The tag vocabulary and user directory are fetched **once per org** rather than per sandbox. |

For more information, read the [README](README.md).

---

## Earlier releases

**Release date: not recorded** · v3.0.0 – v3.2.0

### v3.2.0

SQL table names, Profile flags, and honest gaps.

| Feature | Description |
| --- | --- |
| Datasets tab ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | Maps every dataset's friendly name → **Query Service table name** (`tags['adobe/pqs/table']`) → schema. The system name is what you `SELECT ... FROM`, and it differs from the friendly name; without it the workbook could not be used to write SQL. *Table name(s)* columns on the Schemas index and Field Index, and the SQL table name in each schema tab's header block. |
| Profile column ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | On the Datasets tab (`tags.unifiedProfile`), flagging both Profile-enabled datasets and the Profile Snapshot Export(s), so you know which tables hold whole profiles. Profile rows sort to the top. |
| Data-completeness warnings ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | So a partial dictionary is never read as gospel: any schema whose coverage is missing, partial or empty is listed in a Summary **DATA COMPLETENESS** block and banner-flagged on its own tab. `MISSING` is deliberately distinguished from `0%`. |
| Snapshot sampled once per run ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) | If the Profile Snapshot Export's (huge) file manifest times out under load, the failure is cached so the remaining Profile schemas fail fast instead of each re-hitting a dead snapshot. |
| Luma demo dataset ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | `demo/luma/`: Adobe's public sample data, organised and tenant-normalised for offline demos and tests. |

### v3.1.0

Profile coverage fix. A Profile-class schema is the post-merge **union**
(identity-deduped, last-write-wins), so sampling its *feeding* datasets
tallies pre-merge fragments and reads falsely sparse. Every Profile schema
was under-reporting coverage.

| Fix | Description |
| --- | --- |
| Profile schemas sample the snapshot ![Fixed](https://img.shields.io/badge/Fixed-B35A00?style=flat-square) | Profile-class schemas are sampled from the **Profile Snapshot Export** dataset belonging to the **default merge policy**, resolved automatically: default merge policy → the snapshot dataset tagged with that policy id whose `schemaRef` is `profile__union`. |
| Smallest partition first ![Updated](https://img.shields.io/badge/Updated-1F5FA8?style=flat-square) | Snapshots are enormous, so the tool downloads the **smallest non-empty partition file** rather than whichever comes first. |
| `--profile-snapshot` override ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | `--profile-snapshot=<datasetId>` for when the default isn't the one you want. |

### v3.0.0

The baseline: every tenant XDM schema pulled from an AEP sandbox, filtered
down to the ones that matter, and written to a tabbed, strictly-confidential
Excel workbook.

| Feature | Description |
| --- | --- |
| Auditable filtering ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | A printed **KEEP / DROP verdict per schema**, so exclusions are auditable rather than silent: `no-dataset`, `adhoc`, `ajo`, `system`, `test`. On a large tenant this is the difference between 2,400 schemas and the ~30 that are real. |
| One tab per schema ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | The full field list in dot notation with data types, identities, relationships and friendly (`alternateDisplayInfo`) labels, ready to paste into Claude for a Mermaid ERD. |
| Field Index and Schemas index ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | A master Field Index across every schema, and a Schemas index. |
| Coverage and top values ![New](https://img.shields.io/badge/New-2D7D46?style=flat-square) | `--data-dict`: real **coverage %** and **top-5 example values** per field, tallied from actual ingested records sampled as Snappy-Parquet through the Data Access API. One download covers every field at once: no Query Service, no per-field calls. |

> [!NOTE]
> **Reading coverage.** Coverage is sampled from up to `--dd-rows` records
> (default 1000), not the whole dataset: it means *roughly how often this is
> populated*, not an exact figure. Busy datasets make Catalog time out when
> asked to sort every batch they have ever held, so the tool filters
> `status=success` server-side and steps the page size down (20 → 5 → 1)
> rather than giving up; a narrower sample beats no coverage, and the log
> says when it happens. Adobe's own consolidation batches are skipped: Catalog
> advertises them with the combined record count, but the Data Access API
> refuses them outright (`DTAC-4000`). Nothing is lost, because the batches
> they absorbed are still served individually.

For more information, read the [README](README.md).
