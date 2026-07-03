# dd_audience_countdown.py

A read-only **expiry countdown for Data Distiller audiences** in Adobe
Experience Platform (AEP). It answers the one question worth asking before a
Data Distiller audience silently lapses:

- Which Data Distiller audiences have stopped refreshing and are heading toward
  their data-expiry date, and how many days has each got left?

Data Distiller audiences carry a data TTL and their profile data lapses once
that TTL passes without a refresh. This tool lists every Data Distiller
audience, works out an estimated expiry for each, sorts them by urgency, and
flags any tagged to be kept alive — so you can tell a genuine imminent expiry
from one that is being deliberately maintained.

It is **read-only** — it only ever issues `GET` requests and never creates,
edits, extends, or deletes anything in AEP (no `PUT`/`POST`/`PATCH`/`DELETE`
anywhere). The fetch/auth path uses the **Python standard library only** (no
`pip install`), so it runs on a locked-down/VDI machine; the formatted `.xlsx`
export additionally needs `openpyxl`, and falls back to CSV without it.

---

## How expiry is worked out (important)

The AEP audiences API exposes **no absolute expiry date** for Data Distiller
audiences — only a TTL (`ttlInDays`). So this tool **derives** the expiry:

```text
dataExpiryDate = lastRefresh + ttlInDays
daysRemaining  = dataExpiryDate − today   (whole UTC calendar days)
```

- **`ttlInDays`** — taken from the audience when present and positive; otherwise
  the Data Distiller product **default of 30 days** is used. (In practice most
  audiences don't carry the field, and a stored `0` is treated as "unset" →
  default, so an unconfigured audience isn't falsely flagged as expiring today.)
- **`lastRefresh`** — the most recent of the audience's profile-metrics update,
  its record-export (Halo) update, and its own last-modified time. This is the
  best available "the data was touched" signal.

**What this means in practice:** an audience that keeps refreshing (its metrics
update each day) always sits at roughly a full TTL out and is *not* flagged. An
audience whose refresh **stalls** stops moving its `lastRefresh`, so its
`daysRemaining` counts down — and *that* is the thing the report surfaces. On a
healthy, actively-refreshed estate you should expect most rows to sit near the
full TTL; a row dropping toward zero is the signal.

Every derived expiry is **auditable in the output**: the `Created`,
`Last Refresh`, and `TTL (days)` columns show exactly what the estimate was
built from. If your tenant's refresh cadence means a different anchor is more
truthful, it's a one-line change in `_last_refresh()`.

> **On the endpoint:** the original brief named `GET /core/ais/external-audiences`,
> but that is not a readable list endpoint — the AIS ("Audience Import Service")
> only accepts `POST` (create) there and returns 404/405 on a `GET` list. The
> authoritative read source is the Real-Time Customer Profile audiences API,
> **`GET /core/ups/audiences`** (the same one the other tools in this repo use),
> filtered to Data Distiller origin.

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
   py -3 dd_audience_countdown.py prod --keep-alive-only  # only keep-alive-tagged
   py -3 dd_audience_countdown.py prod --yes      # skip the confirmation prompt
   ```

Outputs are written to `./output/`. That folder is git-ignored — it holds live
tenant data and must not be committed.

### Confirmation before it touches the tenant

Before it queries anything, the tool asks once per credential set:

```text
Query tenant '<tenant>' now? Read-only -- no changes are made to the tenant. [y/N]:
```

**The default is NO** — pressing Enter (or any answer that is not an explicit
`y`/`yes`) aborts that credential set without making a single request. Nothing
hits the tenant until you type `y`. Pass `--yes`/`-y` to skip the prompt for
unattended runs; a non-interactive run without `--yes` defaults to no and skips.
(The run is read-only regardless — this gate is just a deliberate "not yet"
safety catch.)

---

## What it does

1. `GET /core/ups/audiences`, **fully paginated** (cursor via `_page.next`), for
   each selected sandbox. This lists *every* audience in the sandbox.
2. Keeps only the **Data Distiller** ones — `originName == "DATA_DISTILLER"`
   (or namespace `DDA`).
3. For each one captures: **name, id, createdDate, lastRefresh, ttlDays,
   dataExpiryDate** (derived), **daysRemaining** (calculated), **profileCount,
   lifecycleState, tags**, and **origin**.
4. Adds a **keepAlive** flag — `TRUE` when the audience carries the
   `KEEP_ALIVE` tag (case-insensitive; `keep-alive` / `keep_alive` all count).
5. **Sorts ascending by daysRemaining** (the most urgent at the top; anything
   with no derivable refresh time sorts last).
6. Writes the report and prints a console summary.

### Tags are stored as IDs — resolved via the Unified Tags API

Audiences store their tags as **tag-ID (UUID) references**, not names — the
`KEEP_ALIVE` chip you see in the Audience Portal is a UUID on the audience
object. So the tool first calls the org-level **Unified Tags API**
(`GET https://experience.adobe.io/unifiedtags/tags`, a different host) to build a
`{tagId: name}` map, then resolves each audience's tag UUIDs to names. Without
this step keep-alive detection silently finds nothing. Internal
`audience_portal_*` housekeeping markers are dropped from the Tags column. If the
Unified Tags call fails the tool carries on and falls back to matching literal
tag strings (a warning is logged).

Use **`--keep-alive-only`** to narrow the report to just the audiences that carry
the keep-alive tag — the ones meant to persist, and therefore the ones whose
expiry actually matters.

### Field sources (from the `/core/ups/audiences` object)

| Report column | Comes from |
|---------------|------------|
| Name / ID | `name` / `audienceId` (falls back to `id`) |
| Created | `createEpoch` (epoch **seconds**) / `creationTime` |
| Last Refresh | latest of `metrics.updateEpoch`, `recordMetrics.updateEpoch`, `updateEpoch` |
| TTL (days) | `ttlInDays` if positive, else default `30` |
| Data Expiry (est) | derived: Last Refresh + TTL |
| Profile Count | `metrics.data.totalProfiles` (then record count, then flatter fallbacks) |
| Lifecycle State | `lifecycleState` / `lifecycle` |
| Tags | `tags` UUIDs resolved to names via the Unified Tags API; `audience_portal_*` markers hidden |
| Keep-Alive | `TRUE` when a resolved tag is `KEEP_ALIVE` |
| Origin | `originName` (e.g. `DATA_DISTILLER`) |

### Output

- **XLSX** — `output/dd_audience_countdown_<tenant>_<UTCstamp>.xlsx`, one row
  per Data Distiller audience with **conditional formatting** on the
  *Days Remaining* column:

  | Days remaining | Colour |
  |----------------|--------|
  | ≤ 7 (incl. already-expired negatives) | **red** |
  | ≤ 14 | **amber** |
  | > 14 | none |

  Columns: `Sandbox, Name, ID, Created, Last Refresh, TTL (days),
  Data Expiry (est), Days Remaining, Profile Count, Lifecycle State, Keep-Alive,
  Tags, Origin`.

- **CSV fallback** — `output/dd_audience_countdown_<tenant>_<UTCstamp>.csv`,
  same rows, written only when `openpyxl` is not installed.

- **Console summary** — always printed:

  ```text
  Total Data Distiller audiences      : <n>
  Expiring within 7 days              : <n>
  Tagged keep-alive                   : <n>
  ```

---

## Reading the report

- **A red/amber row is not always a problem.** Cross-check the **Keep-Alive**
  column: a flagged audience tagged `keep-alive` is expected to be maintained,
  whereas a flagged audience *without* the tag is the one at risk of lapsing.
- **daysRemaining is whole calendar days** from today (UTC) to the *estimated*
  expiry. Negative means the estimate has **already passed**; blank means no
  refresh timestamp could be found to anchor the estimate (and it sorts last).
- **Cross-check Last Refresh + TTL.** Because expiry is derived, always read
  `daysRemaining` alongside `Last Refresh` and `TTL (days)` — they show exactly
  why the number is what it is.
- **Everything near full TTL is the healthy case.** When the whole estate is
  refreshing daily, every row sits near the TTL and nothing is flagged; the
  report earns its keep the day one audience stops refreshing.

---

## Command reference

| Argument | Meaning |
|----------|---------|
| `<stem>` | Credential file stem in `./creds/` (positional; spaces/hyphens tolerated) |
| `--all` / `-a` | Run for every credential set in `./creds/` |
| `--sandbox=<name>` / `-s <name>` | Scan only this sandbox (repeatable); skips the sandbox menu |
| `--keep-alive-only` | Keep only audiences carrying the `KEEP_ALIVE` tag |
| `--yes` / `-y` | Skip the "query tenant?" confirmation (default is no) |

With no positional name and no `--all`, an interactive credential menu is shown
(when run in a terminal); non-interactive runs must pass a name or `--all`.
`--sandbox` may be given more than once to scan several named sandboxes.

All timestamps are UTC. Every mode is read-only.

---

## Note on API field names

Audience-object field names have drifted across AEP versions, so each field is
read tolerantly (several candidate keys per field, matching the defensive style
used elsewhere in this toolkit). If a future deployment renames a field, add the
new key to the relevant list near the top of the fetch section in
`dd_audience_countdown.py`.
