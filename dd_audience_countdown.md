# dd_audience_countdown.py

A read-only **expiry countdown for Data Distiller audiences** in Adobe
Experience Platform (AEP). It answers the one question that matters before an
audience silently lapses:

- Which Data Distiller audiences are about to hit their **data-expiry date**,
  and how many days has each got left?

Data Distiller audiences carry a time-to-live and are dropped when their
`dataExpiryDate` passes. This tool lists every one, sorts them by urgency, and
flags the ones tagged to be kept alive so you can tell a genuine imminent
expiry from one that is meant to be extended.

It is **read-only** — it only ever issues `GET` requests and never creates,
edits, extends, or deletes anything in AEP (no `PUT`/`POST`/`PATCH`/`DELETE`
anywhere). The fetch/auth path uses the **Python standard library only** (no
`pip install`), so it runs on a locked-down/VDI machine; the formatted `.xlsx`
export additionally needs `openpyxl`, and falls back to CSV without it.

---

## Setup

1. Drop a credential JSON into `./creds/` (the same bank the other tools in this
   repo use). Each file needs at least:

   ```json
   {
     "client_id": "…",
     "client_secret": "…",
     "org_id": "…@AdobeOrg",
     "sandbox": "prod"
   }
   ```

2. (Optional, for the formatted workbook) install openpyxl:

   ```bash
   pip install openpyxl
   ```

3. Run it. On Windows use the `py` launcher:

   ```bash
   py -3 dd_audience_countdown.py                 # interactive credential + sandbox menus
   py -3 dd_audience_countdown.py prod            # pick creds/prod.json by filename stem
   py -3 dd_audience_countdown.py "my creds" --sandbox=prod
   py -3 dd_audience_countdown.py --all           # every credential set in ./creds/
   ```

Outputs are written to `./output/`. That folder is git-ignored — it holds live
tenant data and must not be committed.

---

## What it does

1. `GET /core/ais/external-audiences`, **fully paginated**, for each selected
   sandbox.
2. Keeps only audiences whose **origin/source is Data Distiller**.
3. For each one captures: **name, id, createdDate, dataExpiryDate,
   daysRemaining** (calculated), **profileCount, tags, lifecycleState**.
4. Adds a **keepAlive** flag — `TRUE` when the tags contain `keep-alive`
   (case-insensitive; `keep_alive` / `Keep Alive` also count).
5. **Sorts ascending by daysRemaining** (the most urgent at the top; audiences
   with no expiry date sort last).
6. Writes the report and prints a console summary.

### Output

- **XLSX** — `output/dd_audience_countdown_<tenant>_<UTCstamp>.xlsx`, one row
  per Data Distiller audience with **conditional formatting** on the
  *Days Remaining* column:

  | Days remaining | Colour |
  |----------------|--------|
  | ≤ 7 (incl. already-expired negatives) | **red** |
  | ≤ 14 | **amber** |
  | > 14 | none |

  Columns: `Sandbox, Name, ID, Created, Data Expiry, Days Remaining,
  Profile Count, Lifecycle State, Keep-Alive, Tags, Origin`.

- **CSV fallback** — `output/dd_audience_countdown_<tenant>_<UTCstamp>.csv`,
  same rows, written only when `openpyxl` is not installed.

- **Console summary** — always printed:

  ```
  Total Data Distiller audiences      : <n>
  Expiring within 7 days              : <n>
  Tagged keep-alive                   : <n>
  ```

---

## Reading the report

- **A red row is not always a problem.** Cross-check the **Keep-Alive** column:
  a red audience tagged `keep-alive` is expected to be extended, whereas a red
  audience *without* the tag is the one about to be lost.
- **daysRemaining is whole calendar days** from today (UTC) to the expiry date.
  Negative means it has **already expired**; blank means the audience has **no
  expiry date** set (and it sorts to the bottom).
- **Origin column** shows the source string the Data Distiller filter matched
  on, so you can see *why* an audience was included.

---

## Command reference

| Argument | Meaning |
|----------|---------|
| `<stem>` | Credential file stem in `./creds/` (positional; spaces/hyphens tolerated) |
| `--all` / `-a` | Run for every credential set in `./creds/` |
| `--sandbox=<name>` / `-s <name>` | Scan only this sandbox (repeatable); skips the sandbox menu |

With no positional name and no `--all`, an interactive credential menu is shown
(when run in a terminal); non-interactive runs must pass a name or `--all`.
`--sandbox` may be given more than once to scan several named sandboxes.

All timestamps are UTC. Every mode is read-only.

---

## Note on API field names

The audiences API's exact field spellings and response envelope have varied
across AEP versions, so each field is read tolerantly (several candidate keys
per field, matching the defensive style used elsewhere in this toolkit). On the
first run against a live sandbox, glance at the console/XLSX to confirm the
fields resolved as expected — if the origin filter or a column comes back empty,
the raw key simply needs adding to the relevant list near the top of the fetch
section in `dd_audience_countdown.py`.
