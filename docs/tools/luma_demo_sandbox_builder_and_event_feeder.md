# ingestion/luma_demo_sandbox_builder_and_event_feeder.py

Builds Adobe's **Luma** demo in one Experience Platform sandbox, then keeps it alive with
fresh synthetic events, so profiles, audiences and dashboards look like a busy business.
It is meant for filming and training in **demo sandboxes only**.

| Command | What it does |
|---|---|
| `setup` | Creates the Luma structure: 3 identity namespaces, 1 data type, 2 classes, 4 field groups, 7 schemas, 13 identity and relationship descriptors, 7 datasets. |
| `load` | Loads Adobe's 7 Luma files (6,104 rows) by batch ingestion, with event timestamps moved up to now. |
| `feed` | Sends N synthetic events (web, offline purchase, inventory) onto the loaded profiles. Run it hourly. |
| `audiences` | Creates 19 demo audiences (`Luma: ...`) that all have members on the loaded data, and evaluates them once. |

**It writes to Adobe, so it is read-only by default (house rule 12).** Without `--apply`, each
command only reads and prints exactly what it would create or send. With `--apply` it writes.

## Before the first run

1. **Store the credential** with
   `platform_access_and_audit/credential_keyring_manager_and_access_probe.py store`. Its
   technical account needs a product profile that can manage schemas, datasets, identity
   namespaces and ingestion in the sandbox.
2. **List it as a demo credential** in the gitignored `config.json`, or the tool refuses to
   start:

   ```json
   "luma_demo_sandbox": {
       "allowed_credentials": ["<your-demo-credential-name>"],
       "allowed_org_ids": ["<your-demo-org-id>@AdobeOrg"],
       "allowed_sandboxes": ["<your-demo-sandbox-name>"],
       "streaming_inlet_url": "",
       "streaming_inlet_urls": {},
       "streaming_authenticated": false
   }
   ```

   `allowed_org_ids` is optional but recommended: it stops the tool even if a client
   credential is ever renamed into the list. `allowed_sandboxes` is optional too: once set,
   every other sandbox is refused before any Adobe call.
3. **Pick the sandbox**: the **Name** column in AEP → Sandboxes → Browse (not the title). Use
   a development sandbox you can reset.

## Run it

```
python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> setup --sandbox=<name>           # what's missing
python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> setup --sandbox=<name> --apply   # create it
python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> setup --sandbox=<name>           # now: 37 exist

python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> load --sandbox=<name>            # the 7 batches
python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> load --sandbox=<name> --apply    # ingest, wait for Catalog

python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> feed --sandbox=<name> --n=20 --apply
```

Every `--apply` run says how many Adobe calls it will make and roughly how long they will
take before it starts, and keeps the PC awake while it runs. `load` ends with an
endpoint health report.

The ids that `setup` finds or creates, and the tenant id, are kept in
`output/luma_demo_sandbox_state_<credential>_<sandbox>.json`. `load` and `feed` read them
from there.

### setup

Adobe publishes the Luma sample as `platform-utils-main.zip`: seven JSON files plus Postman
collections that build the schemas and datasets. The pristine download is in
`demo/luma/_raw/platform-utils-main/`.

[`ingestion/luma_demo_sandbox/port_postman.py`](../../ingestion/luma_demo_sandbox/port_postman.py)
turned collections 1–7 into `ingestion/luma_demo_sandbox/specs/`: one JSON payload per
request, verbatim, with the order and the hand-offs between steps in `index.json`. `setup`
replays those requests, so the sandbox ends up exactly as Adobe's tutorial leaves it.

Before creating anything, `setup` looks each object up:
- schema registry objects by title
- namespaces by code
- datasets by name
- descriptors by type, schema and field

Run it twice and the second run reports all 37 objects as `exists`. That also makes it
safe to rerun after a partial failure.

### load

- `_yourTenantId` becomes the sandbox's tenant id, which is read once from the Schema
  Registry.
- Event files are shifted so the latest event is now (UTC):
  - The web (29 days) and offline-purchase (15 days) files keep their original spacing.
  - The inventory file spans a year, so it is squeezed into `--window-days` (default 30).
  - `--window-days=0` never squeezes.
- Every event gets a fresh `_id`. Profile and product files are loaded as they are.
- Each file is one batch: create → upload → COMPLETE → poll Catalog until `success` or
  `failed`, printing Adobe's reasons for any failure.
- `--only=<dataset>` loads one file: `loyalty`, `crm`, `products`, `test-profiles`,
  `offline-purchases`, `web-events` or `inventory-events`.

### feed

- `--n=50` events per run, split `--web=0.5 --offline=0.4 --inventory=0.1`, with
  timestamps in the last `--window-minutes=60`.
- Identities come from the Luma files, so the events stitch onto existing profiles:
  - Offline purchases carry a real `loyaltyId`, priced from the product catalogue.
  - Web events carry a real `lumaCrmId` plus the ECID that CRM id already has in Adobe's
    web file.
  - Every SKU is a real product.
- Events are streamed to an HTTP API inlet. `--batch` uses batch ingestion instead and
  needs no inlet.
- **The inlet:** `inlet --apply` creates it, and `feed` reads its URL from the state file.
  You can also pass `--inlet=` or set `streaming_inlet_url` in `config.json`.
- If each dataflow is pinned to one dataset, give one URL per dataset in
  `streaming_inlet_urls`, keyed `web-events`, `offline-purchases` and `inventory-events`.
- Set `streaming_authenticated` to `true` if the connection was created with
  authentication.
- **Schedule it** with Windows Task Scheduler on the machine that holds the credential:

  ```
  schtasks /Create /SC HOURLY /TN luma-demo-feed /TR "cmd /c cd /d C:\python\swiss-army-knife && python ingestion\luma_demo_sandbox_builder_and_event_feeder.py <credential> feed --sandbox=<name> --apply"
  ```

### inlet

Creates the HTTP API streaming inlet for the three event datasets:

- one unauthenticated streaming connection named `Luma demo streaming`, which is one inlet URL;
- for each event dataset, a source connection, a Data Lake target connection and a
  *Stream data without transformation* dataflow named `Luma demo streaming: <dataset>`.

The URL and flow ids go in the state file. `feed` sends each event with its dataflow's
`x-adobe-flow-id`. A new inlet answers `400 Inlet does not exist` for about a minute.
Because it is unauthenticated, keep the URL to yourself.

To send one event by hand on Windows, use `curl.exe`. In PowerShell 5.1, `curl` is
`Invoke-WebRequest`. Send the JSON from a file:

```
curl.exe -X POST "<inlet url>" -H "Content-Type: application/json" -H "x-adobe-flow-id: <flow id>" --data-binary "@order.json"
```

### audiences

19 batch audiences, 12 child audiences and 6 streaming ones, all named `Luma: ...`, on the default Profile merge policy. The rules
are in [`ingestion/luma_demo_sandbox/audiences.py`](../../ingestion/luma_demo_sandbox/audiences.py):

- **Purchases**: all buyers, web buyers (30 days), in-store buyers (90 days), omnichannel,
  high-value in-store (order $200+), big basket (3+ of one item).
- **Browsing**: browsers who never bought, browsed in the last 7 days with no purchase,
  save-for-later with no purchase in 14 days, interest in gear / men's / women's without
  buying it. The SKU says the category: `24-` gear, `LLM` men's, `LLW` women's.
- **Lifecycle**: new customers (first purchase in the last 14 days).
- **Profile**: California residents, California buyers, loyalty gold or platinum,
  points-rich (500,000+) with no purchase in 60 days, Gen Z (born 1997+), email-contactable
  but never purchased.

- **Children** (`Luma: <parent> > <condition>`): audiences built on a buyer audience with
  `inSegment("<parent id>")` plus one condition.
  - All Buyers: California, gold or platinum, platinum, Gen Z, email-contactable and
    points-rich.
  - In-store Buyers (90 days) and Web Buyers (30 days): California, and gold or platinum.
  - Omnichannel and High-Value In-store Buyers: gold or platinum.

  They are created after their parent. The Data Dictionary shows them as the parent's children.
- **Streaming** (`Luma: Streaming - ...`): an in-store order, a web purchase, an in-store
  order of $200+, a gear purchase or a product view in the last 24 hours, and gold/platinum
  members with an in-store order in the last 24 hours. They are evaluated as events
  arrive, so `feed` fills them. Adobe refuses them (`NEBULA-100042-400`) unless the
  Profile merge policy is **Active-On-Edge**.

An in-store purchase is an event carrying a loyalty id; a web purchase is
`eventType = commerce.purchases`. Web and in-store people join through the identity graph
(the loyalty file links loyalty ids to CRM ids).

Event rules are written in the rule builder's own form,
`CHAIN(xEvent, timestamp, [C0: WHAT(...) WHEN(...)])`, so the UI's Rule view can open and
edit them. Rules in `select X from xEvent` form are code-view only, and Adobe refuses them
for streaming. The rules are stored as `pql/json`, converted by Adobe's conversion
endpoint, the way the UI stores them. The Data Dictionary reads the fields an audience
uses from that form. An audience whose stored rule differs from the tool's (older form,
or still text) is repaired in place with `--apply`. Its id stays the same, so children
and activations keep pointing at it.

Without `--apply` it lists what exists and what is missing. With `--apply` it creates the
missing ones, then runs one batch segment job and prints each audience's members, so the
counts are there without waiting for the nightly run. `--no-evaluate` skips the job.
Orgs with Adobe's *B2B simplification* refuse on-demand segment jobs; the tool then says so,
and the audiences fill at the next daily batch run.

Left out on purpose: lapsed, dormant, win-back, one-time and repeat buyers. The loaded data
spans about 30 days and most people have one event, so they would be empty. They fill up
after weeks of `feed`.

## Check it worked

- AEP → Monitoring → Batches: 7 batches `success` after `load`.
- AEP → Profiles → Browse → identity namespace *Luma Loyalty Id* → `5625458`: loyalty and
  CRM attributes, plus offline purchase events.
- After `feed --n=20 --apply`: new rows in the three event datasets within a few minutes.

## Reset

1. AEP → Sandboxes → Browse → the development sandbox → **Reset**.
2. Delete its `output/luma_demo_sandbox_state_*` file.
3. Run `setup --apply`, then `load --apply`.

## Tests

`python tests/test_luma_demo_sandbox.py`: offline, with fake HTTP.

## Choices made

- **Seven schemas**: Adobe's collections build 7 (Loyalty, CRM, Product Catalog, Offline
  Purchases, Inventory, Test Profiles, Web Events), and all 7 are built.
- **Field group endpoint**: two collections post to the deprecated `/tenant/mixins`. Every
  field group goes to `/tenant/fieldgroups`, with the same payload.
- **Profile enablement** is done exactly as Adobe does it: the `union` tag on schemas, and
  `unifiedProfile`/`unifiedIdentity` tags on datasets. As in Adobe's original, the
  inventory schema is not in Profile.
- **Batch file layout**: a JSON array with one record per line, as in Adobe's files, which
  their Postman flow ingests with plain `format: json`.
- **Stock event names**: the field group's enum is `restock` / `outOfStock`, so those are
  the values used.
- **Synthetic SKUs**: only catalogue SKUs are used. About 22% of the SKUs in Adobe's own
  event files are not in the product catalogue.
- **Offline baskets**: 1–4 lines with quantity 1–3. `priceTotal` is the catalogue price ×
  quantity.
- **Web event types** are the three in Adobe's web file (`productViews`, `saveForLaters`,
  `purchases`), weighted 50% / 25% / 25%, so audiences built on the loaded data also catch
  the fed events. Purchases also get `commerce.order`, which Adobe's own web events lack.
- **The event mix** is split by exact counts (20 → 10/8/2), not random draws.
- **No `.env` and no GitHub Actions schedule.** Credentials come only from the credential
  loader (house rule 10), so `feed` is scheduled on the PC.
