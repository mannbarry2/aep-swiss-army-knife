# ingestion/luma_demo_sandbox_builder_and_event_feeder.py

Builds Adobe's **Luma** demo in one Experience Platform sandbox, then keeps it alive with
fresh synthetic events, so profiles, audiences and dashboards look like a busy business.
It is meant for filming and training in **demo sandboxes only**.

| Command | What it does |
|---|---|
| `setup` | Creates the Luma structure: 3 identity namespaces, 1 data type, 2 classes, 4 field groups, 7 schemas, 13 identity and relationship descriptors, 7 datasets. |
| `load` | Loads Adobe's 7 Luma files (6,104 rows) by batch ingestion, with event timestamps moved up to now. |
| `feed` | Sends N synthetic events (web, offline purchase, inventory) onto the loaded profiles. Run it hourly. |

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
- **Finding the inlet URL:**
  1. AEP → Sources → *Streaming* → **HTTP API** → *Add data*.
  2. Tick **XDM compatible**, pick the event dataset and finish the dataflow.
  3. The dataflow's **Streaming endpoint** is the URL.
  4. Put it in `config.json` (`streaming_inlet_url`), or pass `--inlet=`.
- If each dataflow is pinned to one dataset, give one URL per dataset in
  `streaming_inlet_urls`, keyed `web-events`, `offline-purchases` and `inventory-events`.
- Set `streaming_authenticated` to `true` if the connection was created with
  authentication.
- **Schedule it** with Windows Task Scheduler on the machine that holds the credential:

  ```
  schtasks /Create /SC HOURLY /TN luma-demo-feed /TR "cmd /c cd /d C:\python\swiss-army-knife && python ingestion\luma_demo_sandbox_builder_and_event_feeder.py <credential> feed --sandbox=<name> --apply"
  ```

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
- **Web event types** are weighted like a funnel: views 50%, adds 25%, checkouts 15%,
  purchases 10%. Purchases also get `commerce.order`.
- **The event mix** is split by exact counts (20 → 10/8/2), not random draws.
- **No `.env` and no GitHub Actions schedule.** Credentials come only from the credential
  loader (house rule 10), so `feed` is scheduled on the PC.
