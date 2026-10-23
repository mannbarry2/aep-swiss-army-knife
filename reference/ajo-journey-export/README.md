# AJO Journey Exporter

A small, dependency-free Node script that pulls **Adobe Journey Optimizer journey
definitions** — every node/step, i.e. the same content as the UI's *Copy technical
details* — for an entire sandbox, straight from the AJO authoring API.

For each journey it saves the raw definition JSON, plus a `index.csv` summarising
node counts and any segments referenced in conditions (`inSegment` / `inAudience`).

## ⚠️ Important: this uses an unsupported endpoint

`journey-private.adobe.io/authoring` is the **private API behind the AJO UI**. It is
not an official/public Adobe API and can change without notice. Use it for
inspection/reporting only — don't build critical automation on it. The script is
strictly read-only (GET only); it never modifies a journey.

There is a supported **Journey Public API** in development at Adobe
(`GET /ajo/journey/{id}?include=...`) which is the long-term route once it ships.

## Requirements

- **Node 18+** (uses the built-in `fetch` — no `npm install` needed).

## Getting the two things you need

You need a **bearer token** and your **IMS org id**. Easiest way — grab them from
the AJO UI's own network traffic:

1. Open Adobe Journey Optimizer in the browser and go to the Journeys list.
2. Open DevTools → **Network** tab, filter to **Fetch/XHR**.
3. Click any request to `journey-private.adobe.io/authoring/...`.
4. In **Request Headers**, copy:
   - `authorization:` value after `Bearer ` → that's your token.
   - `x-gw-ims-org-id:` value → that's your org id.

> The token is **short-lived** (typically expires within ~24h) — grab a fresh one
> each time you run the export.

## Usage

```bash
export AJO_BEARER_TOKEN="eyJhbGci...your token..."
export AJO_ORG_ID="XXXXXXXXXXXXXXXXXXXXXXXX@AdobeOrg"
export AJO_SANDBOX="prod"          # optional, default 'prod' (must be lowercase)

# Export every journey in the sandbox:
node ajo-journey-export.mjs

# Export a single journey by its journey UID:
node ajo-journey-export.mjs --journey a72b089b-cd73-44b6-884c-3fe288957435

# Choose an output directory (default ./ajo-journeys):
node ajo-journey-export.mjs --out ./export-2026-09
```

## Output

```
ajo-journeys/
  index.csv                              # one row per journey (see below)
  <JourneyName>__<uid>.json              # full definition per journey (all nodes)
  ...
```

`index.csv` columns:

| Column | Meaning |
|---|---|
| `journeyUid` | The journey's UID |
| `displayName` | Current version name (what you see in the AJO UI) |
| `listName` | Name from the journeys list (can be a stale duplicated name) |
| `authoringFormatVersion` | Journey authoring format (`1.0` / `2.0`) |
| `nodeCount` | Number of steps/nodes in the definition |
| `segmentRefCount` | Distinct segments referenced in `inSegment`/`inAudience` conditions |
| `segmentIds` | Space-separated segment UUIDs referenced |
| `file` | The per-journey JSON filename |

## How it works

1. `GET /authoring/journeys/` (paginated, 100/page) to list every journey — using
   the base64 `x-vyg-query-*` headers the UI sends.
2. `GET /authoring/journeys/{uid}/latest` per journey (throttled, 5 at a time) to
   pull the full latest-version definition.
3. Writes each definition to disk and builds the summary CSV. Segment references
   are extracted by walking the definition for `inSegment` / `inAudience` functions.

## Gotchas we hit (worth knowing)

- **Sandbox name is case-sensitive** — it must be lowercase `prod`. `Prod` returns
  an empty result set silently.
- **`x-api-key` is `voyager_ui`** — the UI's client id. Your normal Adobe I/O /
  service-account API key is not entitled for this endpoint; it must be paired with
  a **user (shell) bearer token**, which is what you copied above.
- **Journey display name vs list name** — the `/journeys/` list can return a stale
  journey-level name (e.g. from duplication, `..._Copy_Copy90`). The real name you
  see in the UI is `result.name` from `/latest`. The script reports both.
- **Authoring format 1.0 vs 2.0** — both carry the segment references in structured
  `inSegment`/`inAudience` conditions within `steps`, so extraction works for both.
