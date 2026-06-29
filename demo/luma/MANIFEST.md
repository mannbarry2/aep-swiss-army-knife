# Luma Demo Data — MANIFEST

Source: Adobe's public **platform-utils** sample-data download (`platform-utils-main.zip`).
This is Adobe's fictitious "Luma" retail demo data — safe to commit to a public repo.

- `_raw/platform-utils-main/` — pristine, never-edited extract of the download (includes `.DS_Store`).
- `data/` — working copies of the seven record files. **Tenant-normalised**: every `_yourTenantId`
  has been replaced with `_lumademo` (see counts below). The `_raw` copies retain `_yourTenantId`.
- `postman/` — the environment + 8 collections (0–7). These collections are the source of truth for
  all schema/dataset structure (they create the classes, field groups, schemas and datasets in AEP).
- `structure/` — empty placeholder for the next stage (extracted schema/class/FG structure).
- `fixtures/` — empty placeholder for the next stage (trimmed/derived sample fixtures).

The diagram/ERD assets (mermaid + PNG + the AutoLID schema exports) were set aside under the
repo-root `AutoLID/` folder, keeping `demo/luma/` free of images.

## Postman collections (structure source)

| # | Collection | Builds |
|---|------------|--------|
| 0 | `0-Authentication` | OAuth token request for the env |
| 1 | `1-Luma-Loyalty-Data` | Luma Loyalty schema + dataset + ingest |
| 2 | `2-Luma-CRM-Data` | Luma CRM schema + dataset + ingest |
| 3 | `3-Luma-Product-Catalog` | Product Catalog class/schema + dataset + ingest |
| 4 | `4-Luma-Offline-Purchase-Events` | Offline Purchase ExperienceEvent schema + dataset + ingest |
| 5 | `5-Luma-Product-Inventory-Events` | Product Inventory business-event schema + dataset + ingest |
| 6 | `6-Luma-Test-Profiles` | AJO Test Profiles schema + dataset + ingest |
| 7 | `7-Luma-Web-Events` | Web Events ExperienceEvent schema + dataset + ingest |

Environment: `DataInExperiencePlatform.postman_environment.json`.

## Datasets

Per-dataset detail. "Records" = length of the top-level JSON array in `data/<file>`.
"Tenant tokens" = number of `_lumademo` occurrences after normalisation (was `_yourTenantId`).

### 1 · Luma Loyalty — **Profile**
- **Data file:** `data/luma-loyalty.json`
- **Records:** 1000 · **Tenant tokens:** 1000
- **Schema title:** Luma Loyalty Members · **Dataset:** Luma Loyalty Dataset
- **Base class:** XDM Individual Profile
- **Standard field groups:** Demographic Details (`person.name`)
- **Custom field groups / datatypes:** custom Loyalty field group (`loyalty.points`, `loyalty.tier`,
  `loyalty.joinDate`) backed by a custom Loyalty datatype; custom identity field group
  (`_lumademo.systemIdentifier.loyaltyId`, `.crmId`)
- **Identity namespace(s):** Luma Loyalty Id (`loyaltyId`, primary); CRM Id (`crmId`)

### 2 · Luma CRM — **Profile**
- **Data file:** `data/luma-crm.json`
- **Records:** 1000 · **Tenant tokens:** 1000
- **Schema title:** Luma CRM · **Dataset:** Luma CRM Dataset
- **Base class:** XDM Individual Profile
- **Standard field groups:** Demographic Details (`person.name`, `person.gender`, `person.birthYear`),
  Personal Contact Details (`personalEmail`, `mobilePhone`, `homeAddress`), Preference Details
- **Custom field groups / datatypes:** custom identity field group (`_lumademo.systemIdentifier.crmId`)
- **Identity namespace(s):** CRM Id (`crmId`); Email (`personalEmail.address`)

### 3 · Luma Product Catalog — **Record (custom class, lookup)**
- **Data file:** `data/luma-products.json`
- **Records:** 101 · **Tenant tokens:** 101
- **Schema title:** Luma Product Catalog · **Dataset:** Luma Product Catalog Dataset
- **Base class:** custom Product Catalog class
- **Standard field groups:** —
- **Custom field groups / datatypes:** custom Product Catalog field group
  (`_lumademo.product.sku/name/category/color/size/price/description/imageUrl/url/stockQuantity`)
- **Identity namespace(s):** none (keyed by product `sku`; used as a lookup dataset)

### 4 · Luma Offline Purchase Events — **Event**
- **Data file:** `data/luma-offline-purchases.json`
- **Records:** 1000 · **Tenant tokens:** 1000
- **Schema title:** Luma Offline Purchase Events · **Dataset:** Luma Offline Purchase Events Dataset
- **Base class:** XDM ExperienceEvent
- **Standard field groups:** Commerce Details (`commerce.order.purchaseID`, `commerce.order.priceTotal`),
  Product List Items (`productListItems[].SKU`, `.quantity`)
- **Custom field groups / datatypes:** custom identity field group (`_lumademo.systemIdentifier.loyaltyId`)
- **Identity namespace(s):** Luma Loyalty Id (`loyaltyId`)

### 5 · Luma Product Inventory Events — **Event (business event)**
- **Data file:** `data/luma-inventory-events.json`
- **Records:** 1000 · **Tenant tokens:** 1000
- **Schema title:** Luma Product Inventory Events · **Dataset:** Luma Product Inventory Events Dataset
- **Base class:** custom business-event class (not tied to a person identity)
- **Standard field groups:** Timestamp (`timestamp`), Id (`_id`)
- **Custom field groups / datatypes:** custom Inventory Event field group
  (`_lumademo.inventoryEvent.sku`, `.stockEventType`)
- **Identity namespace(s):** none (keyed by product `sku`)

### 6 · Luma Test Profiles — **Profile**
- **Data file:** `data/luma-test-profiles.json`
- **Records:** 3 · **Tenant tokens:** 3
- **Schema title:** Luma Test Profiles · **Dataset:** Luma Test Profiles Dataset
- **Base class:** XDM Individual Profile
- **Standard field groups:** Personal Contact Details (`personalEmail.address`)
- **Custom field groups / datatypes:** custom identity field group
  (`_lumademo.systemIdentifier.crmId`); test-profile flag (`testProfile`)
- **Identity namespace(s):** CRM Id (`crmId`); Email (`personalEmail.address`)
- **Note:** small set of AJO test profiles used for journey testing.

### 7 · Luma Web Events — **Event**
- **Data file:** `data/luma-web-events.json`
- **Records:** 1000 · **Tenant tokens:** 0 (uses standard XDM only — no tenant-namespaced fields)
- **Schema title:** Luma Web Events · **Dataset:** Luma Web Events Dataset
- **Base class:** XDM ExperienceEvent
- **Standard field groups:** Id (`_id`), Timestamp (`timestamp`), Experience Event Type (`eventType`),
  Commerce Details (`commerce`), Product List Items (`productListItems[].SKU`), IdentityMap
- **Custom field groups / datatypes:** —
- **Identity namespace(s):** ECID (`identityMap.ECID`, primary); lumaCrmId (`identityMap.lumaCrmId`)
- **Note:** simple historical web data. Identities carried in the standard `identityMap`, not in a
  tenant field group — hence zero `_lumademo` tokens.

## Record-count / tenant-token summary

| Dataset | File | Records | `_lumademo` tokens | Profile/Event |
|---------|------|--------:|-------------------:|---------------|
| Luma Loyalty | `luma-loyalty.json` | 1000 | 1000 | Profile |
| Luma CRM | `luma-crm.json` | 1000 | 1000 | Profile |
| Luma Product Catalog | `luma-products.json` | 101 | 101 | Record (lookup) |
| Luma Offline Purchase Events | `luma-offline-purchases.json` | 1000 | 1000 | Event |
| Luma Product Inventory Events | `luma-inventory-events.json` | 1000 | 1000 | Event |
| Luma Test Profiles | `luma-test-profiles.json` | 3 | 3 | Profile |
| Luma Web Events | `luma-web-events.json` | 1000 | 0 | Event |

> Optional / not yet done (noted only): event timestamps in the offline, inventory and web files are
> dated to ~2022 and may later be shifted to the current month so the demo looks "live".
